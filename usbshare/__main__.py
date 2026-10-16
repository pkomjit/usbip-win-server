"""usbshare: share USB devices from this Windows PC over the LAN (USB/IP protocol).

  usbshare list  [--usbdk]
  usbshare serve --share VID:PID:SERIAL|VID:PID@BUSID|VID:PID|BUSID|all[=ALIAS] [--share ...] [--usbdk] [--port 3240]
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys

from .names import WindowsNames, display_name, entry_for, query_windows, serial_of
from .protocol import DEFAULT_PORT

_SPEED_NAMES = {1: "low", 2: "full", 3: "high", 5: "super", 6: "super+"}


def cmd_list(args: argparse.Namespace) -> int:
    from .usb_backend import LibusbBackend

    backend = LibusbBackend(use_usbdk=args.usbdk)
    try:
        devices = backend.scan()
    finally:
        backend.close()
    usbdk_installed, entries = query_windows()
    print(f"{'BUSID':<10} {'VID:PID':<10} {'SPEED':<7} {'DRIVER':<10} {'SERIAL':<16} NAME")
    for info in devices:
        entry = entry_for(info, entries)
        name = display_name(entry)
        print(
            f"{info.busid:<10} {info.vendor:04x}:{info.product:04x}  "
            f"{_SPEED_NAMES.get(info.speed, '?'):<7} {str(entry.get('service') or '?'):<10} "
            f"{(serial_of(entry) if entry else None) or '-':<16} {name}"
        )
    print()
    if usbdk_installed:
        print("UsbDk is installed: run 'serve --usbdk' to share any of these devices.")
    else:
        print("UsbDk is not installed. Without it, only devices whose DRIVER is WinUSB can be shared")
        print("(install UsbDk to share any device, e.g. keyboards, mice, storage, serial adapters).")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import UsbIpServer
    from .usb_backend import LibusbBackend

    backend = LibusbBackend(use_usbdk=args.usbdk)
    server = UsbIpServer(backend, args.share, host=args.host, port=args.port,
                         namer=WindowsNames().fill, trusted=args.allow or ())
    if server.trusted is not None:
        logging.info("trusted clients only: %s", ", ".join(str(n) for n in server.trusted))
    shared = server.shared_devices()
    if not shared:
        logging.warning("no connected device matches %s yet (they will be offered once plugged in)",
                        ", ".join(args.share))
    for info in shared:
        logging.info("sharing %s%s (%04x:%04x) %s", info.busid,
                     f" as {info.alias}" if info.alias else "", info.vendor, info.product,
                     info.description)
    if hasattr(signal, "SIGBREAK"):  # Ctrl+Break on Windows: stop the same way as Ctrl+C
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    logging.info("press Ctrl+C to stop sharing")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        logging.info("stopping: disconnecting clients and returning devices to Windows")
        server.shutdown()
        backend.close()
        logging.info("stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="usbshare", description="Share USB devices over the LAN (USB/IP).")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="show local USB devices and their bus ids")
    p_list.add_argument("--usbdk", action="store_true", help="use the UsbDk libusb backend")
    p_list.set_defaults(func=cmd_list)

    p_serve = sub.add_parser("serve", help="share devices with USB/IP clients")
    p_serve.add_argument("--share", action="append", required=True, metavar="DEVICE[=ALIAS]",
                         help="device to share; repeat for several. DEVICE is VID:PID:SERIAL (that exact "
                              "device, any port; recommended), VID:PID@BUSID (that model in that port), "
                              "VID:PID (that model, any port), BUSID (anything in that port) or all. "
                              "'=ALIAS' exports it under that name (e.g. 0c45:636f:SN0001=camera, "
                              "then 'usbip attach -b camera')")
    p_serve.add_argument("--usbdk", action="store_true",
                         help="use UsbDk: share any device without replacing its driver")
    p_serve.add_argument("--allow", action="append", metavar="IP|NETWORK",
                         help="only accept these clients, e.g. 192.168.1.20 or 192.168.1.0/24; "
                              "repeat for several (default: anyone)")
    p_serve.add_argument("--host", default="0.0.0.0", help="address to listen on (default all)")
    p_serve.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"TCP port (default {DEFAULT_PORT})")
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
    )
    try:
        return args.func(args)
    except (RuntimeError, OSError, ValueError) as error:
        logging.error("%s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
