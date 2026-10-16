"""Access real USB devices through libusb (WinUSB/libusbK or UsbDk backend)."""

from __future__ import annotations

import ctypes
import logging
import queue
import threading
import time
from typing import Callable

import usb1
from usb1 import _libusb1

from .protocol import (
    DIR_IN, DIR_OUT, ECONNRESET, EINVAL, ENODEV, EOVERFLOW, EPIPE, EPROTO,
    EREMOTEIO, ESHUTDOWN, ETIMEDOUT, SPEED_FULL, SPEED_HIGH, SPEED_LOW,
    SPEED_SUPER, SPEED_SUPER_PLUS, SPEED_UNKNOWN, URB_SHORT_NOT_OK,
    URB_ZERO_PACKET, Completion, DeviceInfo, InterfaceInfo, IsoPacket, Submit,
    parse_setup,
)

log = logging.getLogger(__name__)

DoneCallback = Callable[[Completion], None]

_SPEED = {
    usb1.SPEED_LOW: SPEED_LOW,
    usb1.SPEED_FULL: SPEED_FULL,
    usb1.SPEED_HIGH: SPEED_HIGH,
    usb1.SPEED_SUPER: SPEED_SUPER,
    getattr(usb1, "SPEED_SUPER_PLUS", -1): SPEED_SUPER_PLUS,
}

_STATUS = {
    usb1.TRANSFER_COMPLETED: 0,
    usb1.TRANSFER_ERROR: -EPROTO,
    usb1.TRANSFER_TIMED_OUT: -ETIMEDOUT,
    usb1.TRANSFER_CANCELLED: -ECONNRESET,
    usb1.TRANSFER_STALL: -EPIPE,
    usb1.TRANSFER_NO_DEVICE: -ESHUTDOWN,
    usb1.TRANSFER_OVERFLOW: -EOVERFLOW,
}

EP_CONTROL, EP_ISO, EP_BULK, EP_INTERRUPT = 0, 1, 2, 3
HUB_CLASS = 9


def _error_status(error: usb1.USBError) -> int:
    if isinstance(error, usb1.USBErrorPipe):
        return -EPIPE
    if isinstance(error, usb1.USBErrorNoDevice):
        return -ENODEV
    if isinstance(error, usb1.USBErrorTimeout):
        return -ETIMEDOUT
    return -EPROTO


def busid_of(device: usb1.USBDevice) -> str | None:
    ports = device.getPortNumberList()
    if not ports:
        return None  # root hub
    return f"{device.getBusNumber()}-{'.'.join(str(p) for p in ports)}"


def describe(device: usb1.USBDevice) -> DeviceInfo | None:
    busid = busid_of(device)
    if busid is None or device.getDeviceClass() == HUB_CLASS:
        return None
    interfaces: list[InterfaceInfo] = []
    config_value = 1
    try:
        configs = list(device.iterConfigurations())
        if configs:
            config_value = configs[0].getConfigurationValue()
            for interface in configs[0]:
                for setting in interface:  # alternate setting 0 describes the interface
                    interfaces.append(InterfaceInfo(
                        setting.getClass(), setting.getSubClass(), setting.getProtocol(),
                    ))
                    break
    except usb1.USBError as error:
        log.debug("cannot read configuration of %s: %s", busid, error)
    return DeviceInfo(
        busid=busid,
        busnum=device.getBusNumber(),
        devnum=device.getDeviceAddress(),
        speed=_SPEED.get(device.getDeviceSpeed(), SPEED_UNKNOWN),
        vendor=device.getVendorID(),
        product=device.getProductID(),
        bcd_device=device.getbcdDevice(),
        device_class=device.getDeviceClass(),
        device_subclass=device.getDeviceSubClass(),
        device_protocol=device.getDeviceProtocol(),
        configuration_value=config_value,
        num_configurations=device.getNumConfigurations(),
        interfaces=interfaces,
    )


class LibusbBackend:
    """Owns the libusb context and the single thread that dispatches completions."""

    def __init__(self, use_usbdk: bool = False):
        self.use_usbdk = use_usbdk
        self.context = usb1.USBContext()
        self.context.open()
        if use_usbdk:
            self._enable_usbdk()
        self._running = True
        self._finished: queue.SimpleQueue[tuple[usb1.USBTransfer, LibusbDevice]] = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._events, name="libusb-events", daemon=True)
        self._thread.start()

    def _enable_usbdk(self) -> None:
        # Passing USE_USBDK to libusb_init_context fails with NOT_FOUND because libusb
        # applies options before its Windows backend has probed for UsbDk; setting it
        # on the initialised context works. usb1 does not wrap libusb_set_option.
        set_option = _libusb1.libusb.libusb_set_option
        set_option.restype = ctypes.c_int
        result = set_option(
            self.context._USBContext__context_p,  # pylint: disable=protected-access
            ctypes.c_int(_libusb1.LIBUSB_OPTION_USE_USBDK),
        )
        if result != 0:
            self.context.close()
            raise RuntimeError(
                f"UsbDk backend not available (libusb error {result}). Install UsbDk from "
                "https://github.com/daynix/UsbDk/releases and reboot, or run without --usbdk."
            )

    def _events(self) -> None:
        while self._running:
            try:
                self.context.handleEventsTimeout(0.5)
            except usb1.USBErrorInterrupted:
                continue
            except Exception:  # keep dispatching for the other devices
                log.exception("libusb event handling failed")
                time.sleep(0.1)
            self._free_finished()

    def _free_finished(self) -> None:
        # Runs on the event thread after handleEvents returned, so every callback for
        # these transfers has fully unwound and usb1 no longer touches them.
        while True:
            try:
                transfer, owner = self._finished.get_nowait()
            except queue.Empty:
                return
            transfer.close()
            owner._transfer_freed()  # pylint: disable=protected-access

    def retire(self, transfer: usb1.USBTransfer, owner: "LibusbDevice") -> None:
        self._finished.put((transfer, owner))

    def close(self) -> None:
        self._running = False
        self.context.interruptEventHandler()
        self._thread.join(timeout=2)
        self._free_finished()
        self.context.close()

    def scan(self) -> list[DeviceInfo]:
        found = []
        for device in self.context.getDeviceList(skip_on_error=True):
            info = describe(device)
            if info is not None:
                found.append(info)
        return found

    def open(self, busid: str) -> "LibusbDevice":
        for device in self.context.getDeviceList(skip_on_error=True):
            if busid_of(device) == busid:
                return LibusbDevice(device, describe(device), self)
        raise LookupError(f"no device at {busid}")


class LibusbDevice:
    """One opened device. submit() never blocks on the bus; results arrive via callback."""

    def __init__(self, device: usb1.USBDevice, info: DeviceInfo, backend: LibusbBackend):
        self.device = device
        self.info = info
        self.backend = backend
        self.handle = device.open()
        self._lock = threading.Lock()
        self._transfers: dict[int, usb1.USBTransfer] = {}  # in flight, by seqnum
        self._live = 0  # allocated and not yet freed
        self._all_freed = threading.Condition(self._lock)
        self._claimed: list[int] = []
        self._endpoint_types: dict[int, int] = {}
        self._config = self._current_config()
        self._claim_all()

    # -- configuration -------------------------------------------------

    def _current_config(self) -> int:
        try:
            value = self.handle.getConfiguration()
        except usb1.USBError:
            value = 0
        # UsbDk always reports 0; Windows has already configured the device by then.
        return value or self.info.configuration_value

    def _claim_all(self) -> None:
        self._endpoint_types = {}
        config = next(
            (c for c in self.device.iterConfigurations()
             if c.getConfigurationValue() == self._config),
            None,
        )
        if config is None:
            return
        for interface in config:
            number = None
            for setting in interface:
                number = setting.getNumber()
                for endpoint in setting:
                    self._endpoint_types[endpoint.getAddress()] = endpoint.getAttributes() & 3
            if number is None:
                continue
            try:
                self.handle.claimInterface(number)
                self._claimed.append(number)
            except usb1.USBError as error:
                log.warning("%s: cannot claim interface %d: %s", self.info.busid, number, error)

    def _release_all(self) -> None:
        for number in self._claimed:
            try:
                self.handle.releaseInterface(number)
            except usb1.USBError:
                pass
        self._claimed = []

    def _set_configuration(self, value: int) -> None:
        if value == self._config:
            return
        self._release_all()
        try:
            self.handle.setConfiguration(value)
        finally:
            self._config = self._current_config()
            self._claim_all()

    def _intercept(self, sub: Submit, done: DoneCallback) -> bool:
        """Handle standard requests that must change host-side state, not just reach the device."""
        request_type, request, value, index, _ = parse_setup(sub.setup)
        try:
            if request_type == 0x00 and request == 0x05:  # SET_ADDRESS: the real bus already did it
                pass
            elif request_type == 0x00 and request == 0x09:  # SET_CONFIGURATION
                self._set_configuration(value & 0xFF)
            elif request_type == 0x01 and request == 0x0B:  # SET_INTERFACE
                self.handle.setInterfaceAltSetting(index, value)
            elif request_type == 0x02 and request == 0x01 and value == 0:  # CLEAR_FEATURE(ENDPOINT_HALT)
                self.handle.clearHalt(index & 0xFF)
            elif request_type == 0x23 and request == 0x03 and value == 4:  # SET_FEATURE(PORT_RESET)
                log.debug("%s: port reset requested, device already enumerated", self.info.busid)
            else:
                return False
        except usb1.USBError as error:
            log.debug("%s: request %02x/%02x failed: %s", self.info.busid, request_type, request, error)
            done(Completion(status=_error_status(error), start_frame=sub.start_frame))
            return True
        done(Completion(start_frame=sub.start_frame))
        return True

    # -- transfers -----------------------------------------------------

    def submit(self, sub: Submit, done: DoneCallback) -> None:
        if sub.ep == 0:
            if not self._intercept(sub, done):
                self._submit_control(sub, done)
            return
        address = sub.endpoint_address
        kind = self._endpoint_types.get(address, EP_BULK)
        if kind == EP_ISO:
            self._submit_iso(sub, done, address)
            return
        payload = sub.length if sub.direction == DIR_IN else sub.data
        if kind == EP_INTERRUPT:
            self._start(sub, done, lambda t, cb: t.setInterrupt(address, payload, callback=cb))
        else:
            self._start(sub, done, lambda t, cb: t.setBulk(address, payload, callback=cb))

    def _submit_control(self, sub: Submit, done: DoneCallback) -> None:
        request_type, request, value, index, length = parse_setup(sub.setup)
        if request_type & 0x80:
            payload: int | bytes = min(length, sub.length)
        else:
            payload = sub.data[:length]
        self._start(
            sub, done,
            lambda t, cb: t.setControl(request_type, request, value, index, payload, callback=cb),
        )

    def _submit_iso(self, sub: Submit, done: DoneCallback, address: int) -> None:
        lengths = [p.length for p in sub.iso]
        if not lengths or min(lengths) <= 0:
            done(Completion(status=-EINVAL, start_frame=sub.start_frame))
            return
        if sub.direction == DIR_IN:
            payload: int | bytes = sum(lengths)
        else:
            payload = b"".join(sub.data[p.offset:p.offset + p.length] for p in sub.iso)
        self._start(
            sub, done,
            lambda t, cb: t.setIsochronous(
                address, payload, callback=cb, iso_transfer_length_list=lengths,
            ),
            iso_packets=len(lengths),
        )

    def _start(self, sub: Submit, done: DoneCallback, configure, iso_packets: int = 0) -> None:
        transfer = self.handle.getTransfer(
            iso_packets=iso_packets,
            add_zero_packet=sub.direction == DIR_OUT and bool(sub.transfer_flags & URB_ZERO_PACKET),
        )

        def on_complete(t: usb1.USBTransfer) -> None:
            with self._lock:
                self._transfers.pop(sub.seqnum, None)
            try:
                done(self._result(sub, t))
            finally:
                self.backend.retire(t, self)

        with self._lock:
            self._live += 1
        try:
            configure(transfer, on_complete)
            with self._lock:
                self._transfers[sub.seqnum] = transfer
            transfer.submit()
        except (usb1.USBError, ValueError) as error:
            with self._lock:
                self._transfers.pop(sub.seqnum, None)
            transfer.close()
            self._transfer_freed()
            status = _error_status(error) if isinstance(error, usb1.USBError) else -EINVAL
            done(Completion(status=status, start_frame=sub.start_frame))

    def _transfer_freed(self) -> None:
        with self._lock:
            self._live -= 1
            self._all_freed.notify_all()

    def _result(self, sub: Submit, t: usb1.USBTransfer) -> Completion:
        status = _STATUS.get(t.getStatus(), -EPROTO)
        if t.getType() == usb1.TRANSFER_TYPE_ISOCHRONOUS:
            packets, chunks = [], []
            for requested, setup, buffer in zip(sub.iso, t.getISOSetupList(), t.getISOBufferList()):
                actual = setup["actual_length"]
                packets.append(IsoPacket(
                    requested.offset, requested.length, actual,
                    _STATUS.get(setup["status"], -EPROTO),
                ))
                if sub.direction == DIR_IN:
                    chunks.append(bytes(buffer[:actual]))  # usbip sends iso IN data packed
            return Completion(
                status, sum(p.actual_length for p in packets), b"".join(chunks),
                sub.start_frame, packets,
            )
        actual = t.getActualLength()
        data = bytes(t.getBuffer()[:actual]) if sub.direction == DIR_IN else b""
        if (status == 0 and sub.direction == DIR_IN
                and sub.transfer_flags & URB_SHORT_NOT_OK and actual < sub.length):
            status = -EREMOTEIO
        return Completion(status, actual, data, sub.start_frame)

    def cancel(self, seqnum: int) -> None:
        with self._lock:
            transfer = self._transfers.get(seqnum)
        if transfer is not None:
            try:
                transfer.cancel()
            except usb1.USBError:
                pass  # already completing; its callback still runs

    def close(self) -> None:
        with self._lock:
            pending = list(self._transfers.values())
        for transfer in pending:
            try:
                transfer.cancel()
            except usb1.USBError:
                pass
        with self._lock:
            if not self._all_freed.wait_for(lambda: self._live == 0, timeout=5):
                log.warning("%s: %d transfer(s) did not finish", self.info.busid, self._live)
        self._release_all()
        self.handle.close()
