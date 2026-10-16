"""USB/IP server: exports selected local devices to any USB/IP client (e.g. usbip-win2).

Each attached device uses its own TCP connection, so any number of devices can be
shared and used at the same time, by one client machine or by several.
"""

from __future__ import annotations

import ipaddress
import logging
import queue
import re
import socket
import threading
import time
from typing import Callable, Iterable, Protocol

from .protocol import (
    BUSID_SIZE, CMD_SUBMIT, CMD_SUBMIT_BODY, CMD_UNLINK, CMD_UNLINK_BODY,
    DEVLIST_COUNT, DIR_OUT, ECONNRESET, HEADER_BASIC, HEADER_SIZE, ISO_DESC,
    OP_COMMON, OP_REP_DEVLIST, OP_REP_IMPORT, OP_REQ_DEVLIST, OP_REQ_IMPORT,
    ST_DEV_BUSY, ST_DEV_ERR, ST_NODEV, ST_OK, Completion, DeviceInfo, IsoPacket,
    Submit, pack_op, pack_ret_submit, pack_ret_unlink,
)

log = logging.getLogger(__name__)


class Device(Protocol):
    info: DeviceInfo

    def submit(self, sub: Submit, done) -> None: ...
    def cancel(self, seqnum: int) -> None: ...
    def close(self) -> None: ...


class Backend(Protocol):
    def scan(self) -> list[DeviceInfo]: ...
    def open(self, busid: str) -> Device: ...


class ProtocolError(Exception):
    pass


def recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks, remaining = [], size
    while remaining:
        chunk = sock.recv(min(remaining, 1 << 20))
        if not chunk:
            raise EOFError
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


ALIAS_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,31}")  # fits busid[32]; no ',' '/' or spaces


def parse_selector(selector: str) -> tuple[str, str]:
    """'1-6=camera' -> ('1-6', 'camera'); '1-6' -> ('1-6', '')."""
    match, _, alias = selector.partition("=")
    alias = alias.strip()
    if alias and not ALIAS_PATTERN.fullmatch(alias):
        raise ValueError(f"alias {alias!r}: use 1-31 letters, digits, '-', '_' or '.'")
    return match.strip().lower(), alias


ANYONE = ("", "0.0.0.0", "0.0.0.0/0", "*", "any")

Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_trusted(entries: Iterable[str]) -> list[Network] | None:
    """Trusted client addresses/networks; None means anyone may connect.

    Each entry is an IP ('192.168.1.20') or a network ('192.168.1.0/24');
    '0.0.0.0' (or an empty list) allows every client. Raises ValueError.
    """
    networks: list[Network] = []
    for entry in entries:
        entry = entry.strip()
        if entry.lower() in ANYONE:
            if entry:
                return None
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            raise ValueError(f"{entry!r} is not an IP address or network like 192.168.1.0/24") from None
    return networks or None


def _close_now(sock: socket.socket) -> None:
    # On Windows shutdown() alone does not wake a recv() blocked in another thread;
    # closing the socket does. shutdown() first so the client gets a FIN.
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    sock.close()


def matches(info: DeviceInfo, selector: str) -> bool:
    """Does this device fit the selector? Selectors (before any '=ALIAS'):

    all                  every device
    1-4                  whatever is plugged into that port
    046d:c52b            that model in any port
    046d:c52b:SERIAL     that exact device (USB serial number) in any port
    046d:c52b@1-4        that model, only in that port
    """
    selector = parse_selector(selector)[0]
    vid_pid = f"{info.vendor:04x}:{info.product:04x}"
    if selector == "all":
        return True
    if "@" in selector:
        model, _, busid = selector.partition("@")
        return model == vid_pid and busid == info.busid
    parts = selector.split(":", 2)
    if len(parts) == 3:  # a serial we cannot read yet never matches
        return f"{parts[0]}:{parts[1]}" == vid_pid and info.serial is not None \
            and info.serial.lower() == parts[2]
    return selector == info.busid or selector == vid_pid


class Session:
    """URB traffic for one imported device over one connection."""

    def __init__(self, sock: socket.socket, device: Device, peer: str):
        self.sock = sock
        self.device = device
        self.peer = peer
        self._lock = threading.Lock()
        self._pending: set[int] = set()
        self._unlinks: dict[int, int] = {}  # submit seqnum -> unlink seqnum waiting on it
        self._outbox: queue.SimpleQueue[bytes | None] = queue.SimpleQueue()
        self._writer = threading.Thread(target=self._write, name=f"tx-{peer}", daemon=True)

    def _write(self) -> None:
        while (data := self._outbox.get()) is not None:
            try:
                self.sock.sendall(data)
            except OSError:
                return

    def run(self) -> None:
        self._writer.start()
        try:
            while True:
                try:
                    head = recv_exact(self.sock, HEADER_SIZE)
                except (EOFError, OSError):
                    return
                command, seqnum, devid, direction, ep = HEADER_BASIC.unpack_from(head)
                if command == CMD_SUBMIT:
                    self._on_submit(head, seqnum, devid, direction, ep)
                elif command == CMD_UNLINK:
                    self._on_unlink(head, seqnum)
                else:
                    raise ProtocolError(f"unexpected command {command:#x}")
        finally:
            self._outbox.put(None)
            self._writer.join(timeout=2)

    def _on_submit(self, head: bytes, seqnum: int, devid: int, direction: int, ep: int) -> None:
        flags, length, start_frame, packets, interval, setup = CMD_SUBMIT_BODY.unpack_from(
            head, HEADER_BASIC.size,
        )
        if length < 0:
            raise ProtocolError(f"negative transfer length {length}")
        data = recv_exact(self.sock, length) if direction == DIR_OUT and length else b""
        iso: list[IsoPacket] = []
        if packets > 0:  # non-iso URBs carry 0 or -1 here
            raw = recv_exact(self.sock, packets * ISO_DESC.size)
            iso = [IsoPacket(*ISO_DESC.unpack_from(raw, i * ISO_DESC.size)) for i in range(packets)]
        sub = Submit(
            seqnum, devid, direction, ep, flags, length, start_frame, packets,
            interval, setup, data, iso,
        )
        with self._lock:
            self._pending.add(seqnum)
        self.device.submit(sub, lambda done: self._complete(sub, done))

    def _complete(self, sub: Submit, done: Completion) -> None:
        with self._lock:
            self._pending.discard(sub.seqnum)
            unlink_seqnum = self._unlinks.pop(sub.seqnum, None)
        if unlink_seqnum is None:
            self._outbox.put(pack_ret_submit(sub, done))
        else:
            # The client gave up on this URB: answer the unlink instead, as Linux does.
            self._outbox.put(pack_ret_unlink(unlink_seqnum, -ECONNRESET))

    def _on_unlink(self, head: bytes, seqnum: int) -> None:
        (target,) = CMD_UNLINK_BODY.unpack_from(head, HEADER_BASIC.size)
        with self._lock:
            pending = target in self._pending
            if pending:
                self._unlinks[target] = seqnum
        if pending:
            self.device.cancel(target)  # the reply is sent from _complete
        else:
            self._outbox.put(pack_ret_unlink(seqnum, 0))  # already completed


class UsbIpServer:
    def __init__(self, backend: Backend, selectors: Iterable[str],
                 host: str = "0.0.0.0", port: int = 3240,
                 namer: Callable[[list[DeviceInfo]], None] | None = None,
                 trusted: Iterable[str] = ()):
        self.backend = backend
        self.selectors = list(selectors)
        for selector in self.selectors:
            parse_selector(selector)  # reject bad aliases at startup
        self.namer = namer  # fills DeviceInfo.description, e.g. WindowsNames.fill
        self.host = host
        self.port = port
        self._lock = threading.Lock()
        self._in_use: dict[str, str] = {}  # busid -> peer
        self._attached: dict[str, socket.socket] = {}  # busid -> its session socket
        self._connections: dict[socket.socket, tuple[threading.Thread, str]] = {}  # -> (thread, ip)
        self._trusted = parse_trusted(trusted)
        self._refused_logged: dict[str, float] = {}  # ip -> last time we logged a refusal
        self._stopping = threading.Event()
        self.ready = threading.Event()

    def shared_devices(self) -> list[DeviceInfo]:
        present = self.backend.scan()
        if self.namer is not None and present:
            try:
                self.namer(present)  # before matching: serial selectors need it
            except Exception:  # names are cosmetic; never fail a request over them
                log.exception("device name lookup failed")
        devices, taken = [], set()
        for device in present:
            selector = next((s for s in self.selectors if matches(device, s)), None)
            if selector is None:
                continue
            alias = parse_selector(selector)[1]
            if alias:
                # a VID:PID alias can match several identical devices: camera, camera-2, ...
                n, candidate = 1, alias
                while candidate in taken:
                    n += 1
                    candidate = f"{alias[:28]}-{n}"
                device.alias = candidate
            taken.add(device.exported_busid)
            devices.append(device)
        return devices

    def in_use(self) -> dict[str, str]:
        with self._lock:
            return dict(self._in_use)

    def set_selectors(self, selectors: Iterable[str]) -> None:
        """Change what is shared while running. Already attached devices stay attached."""
        selectors = list(selectors)
        for selector in selectors:
            parse_selector(selector)
        self.selectors = selectors

    def set_trusted(self, entries: Iterable[str]) -> None:
        """Change who may connect; clients no longer trusted are disconnected at once."""
        self._trusted = parse_trusted(entries)
        with self._lock:
            dropped = [(s, ip) for s, (_, ip) in self._connections.items() if not self.is_trusted(ip)]
        for sock, ip in dropped:
            log.warning("disconnected %s: no longer a trusted client", ip)
            _close_now(sock)

    @property
    def trusted(self) -> list[Network] | None:
        """Trusted client networks, or None when anyone may connect."""
        return self._trusted

    def is_trusted(self, ip: str) -> bool:
        trusted = self._trusted
        if trusted is None:
            return True
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(address in network for network in trusted)

    def _refuse(self, sock: socket.socket, ip: str) -> None:
        now = time.monotonic()
        if now - self._refused_logged.get(ip, float("-inf")) > 60:  # clients retry; don't flood
            self._refused_logged[ip] = now
            log.warning("refused %s: not a trusted client", ip)
        sock.close()

    def disconnect(self, busid: str) -> bool:
        """Drop the client using this local bus id; the device returns to Windows."""
        with self._lock:
            sock = self._attached.get(busid)
        if sock is None:
            return False
        _close_now(sock)
        return True

    def serve_forever(self) -> None:
        listener = socket.create_server((self.host, self.port), reuse_port=False)
        # A blocking accept() cannot be interrupted on Windows, which would swallow
        # Ctrl+C; wake up regularly instead so signals and shutdown() take effect.
        listener.settimeout(0.5)
        self.port = listener.getsockname()[1]
        self.ready.set()
        log.info("listening on %s:%d", self.host, self.port)
        try:
            while not self._stopping.is_set():
                try:
                    sock, address = listener.accept()
                except socket.timeout:
                    continue
                if not self.is_trusted(address[0]):
                    self._refuse(sock, address[0])
                    continue
                sock.settimeout(None)
                peer = f"{address[0]}:{address[1]}"
                thread = threading.Thread(
                    target=self._handle, args=(sock, peer), name=f"conn-{peer}", daemon=True,
                )
                with self._lock:
                    self._connections[sock] = (thread, address[0])
                thread.start()
        finally:
            listener.close()

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop accepting, disconnect every client and wait until their devices are released."""
        self._stopping.set()
        with self._lock:
            connections = list(self._connections.items())
        for sock, _ in connections:
            _close_now(sock)
        for _, (thread, _) in connections:
            thread.join(timeout)

    def _handle(self, sock: socket.socket, peer: str) -> None:
        try:
            self._serve_connection(sock, peer)
        finally:
            with self._lock:
                self._connections.pop(sock, None)

    def _serve_connection(self, sock: socket.socket, peer: str) -> None:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        try:
            _, code, _ = OP_COMMON.unpack(recv_exact(sock, OP_COMMON.size))
            if code == OP_REQ_DEVLIST:
                devices = self.shared_devices()
                sock.sendall(
                    pack_op(OP_REP_DEVLIST) + DEVLIST_COUNT.pack(len(devices))
                    + b"".join(d.pack(with_interfaces=True) for d in devices)
                )
                log.info("%s listed %d device(s)", peer, len(devices))
            elif code == OP_REQ_IMPORT:
                raw = recv_exact(sock, BUSID_SIZE)
                self._import(sock, peer, raw.split(b"\0", 1)[0].decode(errors="replace"))
            else:
                log.warning("%s sent unknown operation %#x", peer, code)
        except (EOFError, ConnectionError):
            pass
        except ProtocolError as error:
            log.warning("%s: protocol error: %s", peer, error)
        except Exception:
            log.exception("%s: connection failed", peer)
        finally:
            sock.close()

    def _import(self, sock: socket.socket, peer: str, requested: str) -> None:
        shared = next((d for d in self.shared_devices() if d.exported_busid == requested), None)
        if shared is None:
            log.warning("%s asked for %s, which is not shared", peer, requested)
            sock.sendall(pack_op(OP_REP_IMPORT, ST_NODEV))
            return
        busid = shared.busid  # the local port, whatever name the client used
        with self._lock:
            holder = self._in_use.get(busid)
            if holder is None:
                self._in_use[busid] = peer
        if holder is not None:
            log.warning("%s asked for %s, already attached by %s", peer, busid, holder)
            sock.sendall(pack_op(OP_REP_IMPORT, ST_DEV_BUSY))
            return
        try:
            try:
                device = self.backend.open(busid)
            except Exception as error:
                log.error("%s: cannot open %s: %s", peer, busid, error)
                sock.sendall(pack_op(OP_REP_IMPORT, ST_DEV_ERR))
                return
            try:
                device.info.description = device.info.description or shared.description
                device.info.alias = shared.alias  # the reply must echo the busid the client asked for
                sock.sendall(pack_op(OP_REP_IMPORT, ST_OK) + device.info.pack(with_interfaces=False))
                log.info("%s attached %s (%04x:%04x) %s", peer, shared.exported_busid,
                         device.info.vendor, device.info.product, device.info.description)
                with self._lock:
                    self._attached[busid] = sock
                Session(sock, device, peer).run()
            finally:
                with self._lock:
                    self._attached.pop(busid, None)
                device.close()  # cancels whatever is still in flight, hands the device back
            log.info("%s detached %s", peer, shared.exported_busid)
        finally:
            with self._lock:
                self._in_use.pop(busid, None)
