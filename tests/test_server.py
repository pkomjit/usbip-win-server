import socket
import struct
import threading
import time
import unittest

from usbshare.protocol import (
    CMD_SUBMIT, CMD_SUBMIT_BODY, CMD_UNLINK, CMD_UNLINK_BODY, DEVLIST_COUNT,
    DIR_IN, DIR_OUT, ECONNRESET, HEADER_BASIC, ISO_DESC, OP_COMMON, OP_REP_DEVLIST,
    OP_REP_IMPORT, OP_REQ_DEVLIST, OP_REQ_IMPORT, RET_SUBMIT, RET_SUBMIT_BODY,
    RET_UNLINK, RET_UNLINK_BODY, ST_DEV_BUSY, ST_NODEV, ST_OK, USB_DEVICE,
    USB_INTERFACE, USBIP_VERSION, Completion, DeviceInfo, InterfaceInfo, IsoPacket,
    parse_setup,
)
from usbshare.server import UsbIpServer, recv_exact


def make_info(busid, vendor, product):
    return DeviceInfo(
        busid=busid, busnum=1, devnum=int(busid.split("-")[1]) + 10, speed=3,
        vendor=vendor, product=product, bcd_device=0x0100, device_class=0,
        device_subclass=0, device_protocol=0, configuration_value=1,
        num_configurations=1, interfaces=[InterfaceInfo(3, 1, 1), InterfaceInfo(8, 6, 80)],
    )


class FakeDevice:
    """Answers GET_DESCRIPTOR(device), echoes bulk OUT into bulk IN, holds interrupt IN forever."""

    def __init__(self, info):
        self.info = info
        self.closed = False
        self.received = []
        self._held = {}
        self._queued = []

    def submit(self, sub, done):
        if sub.ep == 0:
            request_type, request, value, _, length = parse_setup(sub.setup)
            if request_type == 0x80 and request == 6 and value >> 8 == 1:
                descriptor = struct.pack("<BBHBBBBHHHBBBB", 18, 1, 0x200, 0, 0, 0, 64,
                                         self.info.vendor, self.info.product, 0x100, 1, 2, 3, 1)
                data = descriptor[:length]
                done(Completion(0, len(data), data))
            else:
                done(Completion(-32))  # stall
        elif sub.ep == 1 and sub.direction == DIR_OUT:
            self.received.append(sub.data)
            self._queued.append(sub.data)
            done(Completion(0, len(sub.data)))
        elif sub.ep == 1 and sub.direction == DIR_IN:
            data = self._queued.pop(0) if self._queued else b""
            done(Completion(0, len(data), data))
        elif sub.ep == 2:  # interrupt IN that only completes when cancelled
            self._held[sub.seqnum] = (sub, done)
        elif sub.ep == 3:  # iso IN: every packet returns half its length
            packets, chunks = [], []
            for p in sub.iso:
                actual = p.length // 2
                packets.append(IsoPacket(p.offset, p.length, actual, 0))
                chunks.append(bytes([p.offset & 0xFF]) * actual)
            done(Completion(0, sum(x.actual_length for x in packets), b"".join(chunks), 0, packets))

    def cancel(self, seqnum):
        held = self._held.pop(seqnum, None)
        if held:
            threading.Thread(target=held[1], args=(Completion(-ECONNRESET),)).start()

    def close(self):
        self.closed = True


class FakeBackend:
    def __init__(self, infos):
        self.infos = infos
        self.opened = []

    def scan(self):
        return list(self.infos)

    def open(self, busid):
        device = FakeDevice(next(i for i in self.infos if i.busid == busid))
        self.opened.append(device)
        return device


class Client:
    """Minimal USB/IP client, framing requests the same way Linux vhci / usbip-win2 do."""

    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.seq = 0

    def close(self):
        self.sock.close()

    def devlist(self):
        self.sock.sendall(OP_COMMON.pack(USBIP_VERSION, OP_REQ_DEVLIST, 0))
        _, code, status = OP_COMMON.unpack(recv_exact(self.sock, OP_COMMON.size))
        assert (code, status) == (OP_REP_DEVLIST, ST_OK)
        (count,) = DEVLIST_COUNT.unpack(recv_exact(self.sock, 4))
        devices = []
        for _ in range(count):
            fields = USB_DEVICE.unpack(recv_exact(self.sock, USB_DEVICE.size))
            interfaces = [USB_INTERFACE.unpack(recv_exact(self.sock, 4)) for _ in range(fields[-1])]
            devices.append((fields, interfaces))
        return devices

    def attach(self, busid):
        self.sock.sendall(OP_COMMON.pack(USBIP_VERSION, OP_REQ_IMPORT, 0) + struct.pack(">32s", busid.encode()))
        _, code, status = OP_COMMON.unpack(recv_exact(self.sock, OP_COMMON.size))
        assert code == OP_REP_IMPORT
        if status != ST_OK:
            return status, None
        return status, USB_DEVICE.unpack(recv_exact(self.sock, USB_DEVICE.size))

    def submit(self, direction, ep, length, setup=b"\0" * 8, data=b"", iso=None):
        self.seq += 1
        packets = len(iso) if iso else 0
        self.sock.sendall(
            HEADER_BASIC.pack(CMD_SUBMIT, self.seq, 0x1000B, direction, ep)
            + CMD_SUBMIT_BODY.pack(0, length, 0, packets, 0, setup) + data
            + b"".join(ISO_DESC.pack(o, l, 0, 0) for o, l in (iso or []))
        )
        return self.seq

    def unlink(self, target):
        self.seq += 1
        self.sock.sendall(HEADER_BASIC.pack(CMD_UNLINK, self.seq, 0x1000B, 0, 0) + CMD_UNLINK_BODY.pack(target))
        return self.seq

    def reply(self, iso_packets=0):
        command, seqnum, devid, direction, ep = HEADER_BASIC.unpack(recv_exact(self.sock, HEADER_BASIC.size))
        rest = recv_exact(self.sock, 28)
        if command == RET_UNLINK:
            return command, seqnum, RET_UNLINK_BODY.unpack(rest)[0], b"", []
        assert command == RET_SUBMIT
        assert (devid, direction, ep) == (0, 0, 0)
        status, actual, _, packets, _ = RET_SUBMIT_BODY.unpack(rest)
        return command, seqnum, status, actual, packets

    def read(self, size):
        return recv_exact(self.sock, size)


def descriptor_request(length=18):
    return struct.pack("<BBHHH", 0x80, 6, 0x0100, 0, length)


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.infos = [make_info("1-4", 0x046D, 0xC52B), make_info("1-7", 0x0781, 0x5567),
                      make_info("1-9", 0x1234, 0x5678)]
        self.backend = FakeBackend(self.infos)
        self.server = UsbIpServer(self.backend, ["1-4", "0781:5567"], host="127.0.0.1", port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.assertTrue(self.server.ready.wait(5))
        self.clients = []

    def tearDown(self):
        for client in self.clients:
            client.close()
        self.server.shutdown()

    def assertDisconnected(self, client):
        # FIN, or RST when Windows closes a socket that still had unread request bytes
        try:
            self.assertEqual(client.sock.recv(1024), b"")
        except ConnectionResetError:
            pass

    def client(self):
        c = Client(self.server.port)
        self.clients.append(c)
        return c

    def test_devlist_offers_only_selected_devices(self):
        devices = self.client().devlist()
        self.assertEqual([d[0][1].rstrip(b"\0") for d in devices], [b"1-4", b"1-7"])
        fields, interfaces = devices[0]
        self.assertEqual((fields[5], fields[6]), (0x046D, 0xC52B))
        self.assertEqual(interfaces, [(3, 1, 1), (8, 6, 80)])

    def test_unshared_and_busy_devices_are_refused(self):
        self.assertEqual(self.client().attach("1-9")[0], ST_NODEV)
        self.assertEqual(self.client().attach("1-4")[0], ST_OK)
        self.assertEqual(self.client().attach("1-4")[0], ST_DEV_BUSY)

    def test_several_devices_attached_and_used_at_once(self):
        a, b = self.client(), self.client()
        self.assertEqual(a.attach("1-4")[0], ST_OK)
        self.assertEqual(b.attach("1-7")[0], ST_OK)
        for client, vendor in ((a, 0x046D), (b, 0x0781), (a, 0x046D)):
            seq = client.submit(DIR_IN, 0, 18, setup=descriptor_request())
            _, got_seq, status, actual, _ = client.reply()
            self.assertEqual((got_seq, status, actual), (seq, 0, 18))
            self.assertEqual(struct.unpack_from("<H", client.read(18), 8)[0], vendor)
        self.assertEqual(sorted(self.server.in_use()), ["1-4", "1-7"])

    def test_bulk_out_then_in(self):
        c = self.client()
        c.attach("1-4")
        c.submit(DIR_OUT, 1, 5, data=b"hello")
        self.assertEqual(c.reply()[2:4], (0, 5))
        c.submit(DIR_IN, 1, 512)
        self.assertEqual(c.reply()[2:4], (0, 5))
        self.assertEqual(c.read(5), b"hello")
        self.assertEqual(self.backend.opened[0].received, [b"hello"])

    def test_unlink_pending_urb_answers_unlink_not_submit(self):
        c = self.client()
        c.attach("1-4")
        held = c.submit(DIR_IN, 2, 8)
        unlink = c.unlink(held)
        command, seqnum, status, _, _ = c.reply()
        self.assertEqual((command, seqnum, status), (RET_UNLINK, unlink, -ECONNRESET))
        # the connection keeps working and no stray RET_SUBMIT for `held` follows
        seq = c.submit(DIR_IN, 0, 18, setup=descriptor_request())
        self.assertEqual(c.reply()[1], seq)

    def test_unlink_of_completed_urb_returns_zero(self):
        c = self.client()
        c.attach("1-4")
        done = c.submit(DIR_IN, 0, 18, setup=descriptor_request())
        c.reply()
        c.read(18)
        unlink = c.unlink(done)
        self.assertEqual(c.reply()[:3], (RET_UNLINK, unlink, 0))

    def test_iso_in_packs_data_and_returns_descriptors(self):
        c = self.client()
        c.attach("1-4")
        c.submit(DIR_IN, 3, 24, iso=[(0, 8), (8, 8), (16, 8)])
        _, _, status, actual, packets = c.reply()
        self.assertEqual((status, actual, packets), (0, 12, 3))
        self.assertEqual(c.read(12), b"\x00" * 4 + b"\x08" * 4 + b"\x10" * 4)
        descs = [ISO_DESC.unpack(c.read(16)) for _ in range(3)]
        self.assertEqual(descs, [(0, 8, 4, 0), (8, 8, 4, 0), (16, 8, 4, 0)])

    def test_names_reach_client_in_devlist_and_import(self):
        def namer(infos):
            for info in infos:
                info.description = {"1-4": "Maono Wireless Mic RX"}.get(info.busid, "")
        self.server.namer = namer
        paths = [d[0][0].rstrip(b"\0") for d in self.client().devlist()]
        self.assertEqual(paths, [b"Maono Wireless Mic RX", b"/windows/usbshare/1-7"])
        status, fields = self.client().attach("1-4")
        self.assertEqual((status, fields[0].rstrip(b"\0")), (ST_OK, b"Maono Wireless Mic RX"))

    def test_alias_replaces_busid_for_list_attach_and_reply(self):
        self.server.selectors = ["1-4=mic", "1-7"]
        devices = self.client().devlist()
        self.assertEqual([d[0][1].rstrip(b"\0") for d in devices], [b"mic", b"1-7"])
        self.assertEqual(self.client().attach("1-4")[0], ST_NODEV)  # only the alias is offered
        status, fields = self.client().attach("mic")
        self.assertEqual((status, fields[1].rstrip(b"\0")), (ST_OK, b"mic"))
        self.assertEqual(self.backend.opened[-1].info.busid, "1-4")  # opened the real port
        self.assertEqual(self.client().attach("mic")[0], ST_DEV_BUSY)

    def test_vid_pid_alias_numbers_identical_devices(self):
        self.backend.infos.append(make_info("1-8", 0x0781, 0x5567))
        self.server.selectors = ["0781:5567=stick"]
        names = [d[0][1].rstrip(b"\0") for d in self.client().devlist()]
        self.assertEqual(names, [b"stick", b"stick-2"])
        self.assertEqual(self.client().attach("stick-2")[0], ST_OK)
        self.assertEqual(self.backend.opened[-1].info.busid, "1-8")

    def test_bad_alias_is_rejected_at_startup(self):
        for bad in ("1-6=my camera", "1-6=a,b", "1-6=" + "x" * 32, "1-6=cam/1"):
            with self.assertRaises(ValueError):
                UsbIpServer(self.backend, [bad])

    def test_failing_namer_does_not_break_listing(self):
        def namer(infos):
            raise RuntimeError("powershell missing")
        self.server.namer = namer
        self.assertEqual(len(self.client().devlist()), 2)

    def test_long_name_is_truncated_on_a_character_boundary(self):
        info = make_info("1-4", 1, 2)
        info.description = "ไมค์" * 100  # 3-byte UTF-8 characters
        path = USB_DEVICE.unpack(info.pack(with_interfaces=False))[0]
        self.assertEqual(path[255], 0)  # still NUL-terminated
        path.rstrip(b"\0").decode()  # no half character left behind

    def test_shutdown_disconnects_clients_and_releases_devices(self):
        a, b = self.client(), self.client()
        a.attach("1-4")
        b.attach("1-7")
        a.submit(DIR_IN, 2, 8)  # left pending on the device
        deadline = time.monotonic() + 5
        while len(self.server.in_use()) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        started = time.monotonic()
        self.server.shutdown()
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(self.server.in_use(), {})
        self.assertTrue(all(d.closed for d in self.backend.opened))
        self.assertDisconnected(a)

    def test_untrusted_client_is_refused_before_any_reply(self):
        self.server.set_trusted(["10.0.0.0/8", "192.168.1.5"])
        c = self.client()
        with self.assertRaises((EOFError, ConnectionError)):
            c.devlist()

    def test_trusted_client_and_anyone_defaults(self):
        self.server.set_trusted(["127.0.0.0/8"])
        self.assertEqual(len(self.client().devlist()), 2)
        for anyone in ([], ["0.0.0.0"], ["", " "]):
            self.server.set_trusted(anyone)
            self.assertIsNone(self.server.trusted)
            self.assertTrue(self.server.is_trusted("8.8.8.8"))

    def test_removing_trust_disconnects_attached_client(self):
        c = self.client()
        c.attach("1-4")
        deadline = time.monotonic() + 5
        while not self.server.in_use() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.server.set_trusted(["10.1.2.3"])
        self.assertDisconnected(c)
        while self.server.in_use() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.server.in_use(), {})
        self.assertTrue(self.backend.opened[0].closed)

    def test_bad_trusted_entry_is_rejected(self):
        with self.assertRaises(ValueError):
            self.server.set_trusted(["192.168.1"])
        with self.assertRaises(ValueError):
            UsbIpServer(self.backend, ["all"], trusted=["not-an-ip"])

    def test_disconnect_closes_device_and_frees_it(self):
        c = self.client()
        c.attach("1-7")
        c.close()
        deadline = time.monotonic() + 5
        while self.server.in_use() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.backend.opened[0].closed)
        self.assertEqual(self.client().attach("1-7")[0], ST_OK)


if __name__ == "__main__":
    unittest.main()
