"""USB/IP wire format (protocol version 1.1.1, as used by Linux and usbip-win2).

All multi-byte fields are big-endian, except the 8-byte USB control setup
packet, which is passed through untouched (little-endian, as on the bus).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

USBIP_VERSION = 0x0111
DEFAULT_PORT = 3240

# Operation codes used before a device is imported.
OP_REQ_DEVLIST = 0x8005
OP_REP_DEVLIST = 0x0005
OP_REQ_IMPORT = 0x8003
OP_REP_IMPORT = 0x0003

# Operation status codes.
ST_OK = 0
ST_NA = 1
ST_DEV_BUSY = 2
ST_DEV_ERR = 3
ST_NODEV = 4
ST_ERROR = 5

# URB commands used after a device is imported.
CMD_SUBMIT = 1
CMD_UNLINK = 2
RET_SUBMIT = 3
RET_UNLINK = 4

DIR_OUT = 0
DIR_IN = 1

# Linux URB transfer_flags we act on.
URB_SHORT_NOT_OK = 0x0001
URB_ZERO_PACKET = 0x0040

# Linux usb_device_speed values.
SPEED_UNKNOWN = 0
SPEED_LOW = 1
SPEED_FULL = 2
SPEED_HIGH = 3
SPEED_WIRELESS = 4
SPEED_SUPER = 5
SPEED_SUPER_PLUS = 6

# Linux errno values reported in RET_SUBMIT / RET_UNLINK status (negated).
ENOENT = 2
ENODEV = 19
EINVAL = 22
EPIPE = 32
ETIME = 62
EPROTO = 71
EOVERFLOW = 75
ECONNRESET = 104
ESHUTDOWN = 108
ETIMEDOUT = 110
EREMOTEIO = 121

OP_COMMON = struct.Struct(">HHI")  # version, code, status
BUSID_SIZE = 32
OP_IMPORT_REQUEST = struct.Struct(">32s")
# path, busid, busnum, devnum, speed, idVendor, idProduct, bcdDevice,
# bDeviceClass, bDeviceSubClass, bDeviceProtocol, bConfigurationValue,
# bNumConfigurations, bNumInterfaces
USB_DEVICE = struct.Struct(">256s32sIIIHHHBBBBBB")
USB_INTERFACE = struct.Struct(">BBBx")
DEVLIST_COUNT = struct.Struct(">I")

HEADER_BASIC = struct.Struct(">IIIII")  # command, seqnum, devid, direction, ep
CMD_SUBMIT_BODY = struct.Struct(">Iiiii8s")  # flags, length, start_frame, packets, interval, setup
RET_SUBMIT_BODY = struct.Struct(">iiiii8x")  # status, actual, start_frame, packets, error_count
CMD_UNLINK_BODY = struct.Struct(">I24x")  # seqnum to unlink
RET_UNLINK_BODY = struct.Struct(">i24x")  # status
ISO_DESC = struct.Struct(">IIIi")  # offset, length, actual_length, status

HEADER_SIZE = HEADER_BASIC.size + CMD_SUBMIT_BODY.size  # 48 for every URB PDU
assert HEADER_SIZE == HEADER_BASIC.size + RET_SUBMIT_BODY.size == 48
assert USB_DEVICE.size == 312


@dataclass
class InterfaceInfo:
    cls: int
    subclass: int
    protocol: int


@dataclass
class DeviceInfo:
    busid: str
    busnum: int
    devnum: int
    speed: int
    vendor: int
    product: int
    bcd_device: int
    device_class: int
    device_subclass: int
    device_protocol: int
    configuration_value: int
    num_configurations: int
    interfaces: list[InterfaceInfo] = field(default_factory=list)
    description: str = ""
    alias: str = ""  # bus id clients see instead of the local one, e.g. "camera"
    # USB serial number as Windows reports it: "" if the device has none, None if not known yet
    serial: str | None = None

    @property
    def exported_busid(self) -> str:
        return self.alias or self.busid

    @property
    def devid(self) -> int:
        return (self.busnum << 16) | self.devnum

    @property
    def path(self) -> str:
        # USB/IP has no name field. Clients print the path verbatim, so carry the name
        # there: unknown VID/PIDs otherwise show up as "unknown vendor : unknown product".
        return self.description or f"/windows/usbshare/{self.busid}"

    def pack(self, with_interfaces: bool) -> bytes:
        path = self.path.encode()[:255].decode(errors="ignore").encode()  # keep a NUL, whole chars
        data = USB_DEVICE.pack(
            path, self.exported_busid.encode(), self.busnum, self.devnum,
            self.speed, self.vendor, self.product, self.bcd_device,
            self.device_class, self.device_subclass, self.device_protocol,
            self.configuration_value, self.num_configurations,
            len(self.interfaces),
        )
        if with_interfaces:
            data += b"".join(
                USB_INTERFACE.pack(i.cls, i.subclass, i.protocol)
                for i in self.interfaces
            )
        return data


@dataclass
class IsoPacket:
    offset: int
    length: int
    actual_length: int = 0
    status: int = 0

    def pack(self) -> bytes:
        return ISO_DESC.pack(self.offset, self.length, self.actual_length, self.status)


@dataclass
class Submit:
    """One CMD_SUBMIT from the client."""

    seqnum: int
    devid: int
    direction: int
    ep: int
    transfer_flags: int
    length: int
    start_frame: int
    number_of_packets: int
    interval: int
    setup: bytes
    data: bytes = b""
    iso: list[IsoPacket] = field(default_factory=list)

    @property
    def endpoint_address(self) -> int:
        return self.ep | (0x80 if self.direction == DIR_IN else 0)


@dataclass
class Completion:
    """Result of a Submit, sent back as RET_SUBMIT."""

    status: int = 0
    actual_length: int = 0
    data: bytes = b""
    start_frame: int = 0
    iso: list[IsoPacket] | None = None


def pack_op(code: int, status: int = ST_OK) -> bytes:
    return OP_COMMON.pack(USBIP_VERSION, code, status)


def pack_ret_submit(sub: Submit, done: Completion) -> bytes:
    iso = done.iso or []
    packets = len(iso) if done.iso is not None else sub.number_of_packets
    error_count = sum(1 for p in iso if p.status != 0)
    # Like the Linux stub driver: replies carry only the seqnum; devid/direction/ep are zero.
    out = HEADER_BASIC.pack(RET_SUBMIT, sub.seqnum, 0, 0, 0)
    out += RET_SUBMIT_BODY.pack(
        done.status, done.actual_length, done.start_frame, packets, error_count,
    )
    if sub.direction == DIR_IN:
        out += done.data
    out += b"".join(p.pack() for p in iso)
    return out


def pack_ret_unlink(seqnum: int, status: int) -> bytes:
    return (
        HEADER_BASIC.pack(RET_UNLINK, seqnum, 0, 0, 0)
        + RET_UNLINK_BODY.pack(status)
    )


def parse_setup(setup: bytes) -> tuple[int, int, int, int, int]:
    """Return (bmRequestType, bRequest, wValue, wIndex, wLength)."""
    return struct.unpack("<BBHHH", setup)
