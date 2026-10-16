# usbip-win-server

**USBShare**: a USB/IP server for Windows. Share USB devices from one Windows
PC and use them on another Windows PC over the LAN, with no extra hardware.
You can attach several devices at the same time, from one client or from
several clients.

It provides the missing server half of a Windows-to-Windows USB/IP setup: the
client side is the existing
[usbip-win2](https://github.com/vadimgrn/usbip-win2), which provides a
client but no server.

> This is an independent project. It isn't affiliated with usbip-win2, UsbDk,
> libusb or the Linux USB/IP project. It implements the open USB/IP protocol.

```
 SERVER PC (device plugged in)                 CLIENT PC (device appears here)
 ┌──────────────────────────────┐   TCP 3240   ┌───────────────────────────────┐
 │ USB device ─ UsbDk ─ libusb  │◄────────────►│ usbip-win2 virtual USB host   │
 │        └─ USBShare.exe       │   USB/IP     │   (signed driver) ─ Windows   │
 └──────────────────────────────┘              └───────────────────────────────┘
```

- **Server**: `USBShare.exe` (portable GUI) or `usbshare-cli.exe`
  (command line), built in this repo. It's a user-mode USB/IP server and
  needs no custom driver.
- **Client**: [usbip-win2](https://github.com/vadimgrn/usbip-win2). Its
  virtual USB host controller driver is signed, so test mode isn't needed.

usbshare speaks the open USB/IP protocol (v1.1.1), so Linux `usbip attach`
clients work too.

## Server PC (where the devices are plugged in)

1. Install **UsbDk** (signed MSI, x64):
   <https://github.com/daynix/UsbDk/releases>.
   UsbDk lets usbshare take over any device (keyboard, mouse, storage,
   serial, audio) without replacing its driver. The device goes back to
   Windows when the client detaches or the server stops.
   *(Without UsbDk, only devices bound to WinUSB, for example with Zadig,
   can be shared.)*
2. Copy **`dist\USBShare.exe`** anywhere, such as a USB stick or the
   Desktop, and run it. Nothing gets installed. Settings are saved in
   `usbshare.json` next to the exe, or in `%APPDATA%\usbshare` if that
   folder is read-only.
3. In the window:
   - Tick **Share** for each device to offer. It warns before sharing a
     keyboard or mouse.
   - Double-click a row (or use **Set alias...**) to give it the name clients
     attach by, for example `ugreen-camera`.
   - Click **Start sharing**. The green line shows this PC's address and the
     attach command for the client.
   - If it says the firewall blocks the port, click **Allow in firewall...**
     and approve the admin prompt. If Windows asks whether to let USBShare
     use networks, allow **Private** networks.
   - The **Status** column shows which client is using each device.
     **Disconnect client** drops one client. **Stop sharing** drops them all
     and gives every device back to Windows.
   - **Start sharing when the app opens** makes it start right away next
     time.
   - **Trusted clients**: only these IPs or networks may connect, for
     example `192.168.1.20, 192.168.1.0/24`. `0.0.0.0` (the default) allows
     anyone. Other addresses are refused before they can see any device,
     and narrowing the list while sharing disconnects clients that no
     longer match.
   - **Minimize and close to the tray** (on by default): the window hides
     to the notification area, and sharing keeps running. Right-click the
     tray icon for **Open**, **Start/Stop sharing** and **Exit**. The icon
     is blue while sharing, and its tooltip shows how many devices are in
     use.
   - **Start with Windows** adds a per-user login entry (no admin needed)
     that starts USBShare hidden in the tray. Together with **Start sharing
     when the app opens**, devices are shared as soon as you log in. If you
     move the exe, the entry follows it the next time you run it.
   - Only one copy runs at a time: launching it again brings up the
     running window.

> Don't share the keyboard and mouse you use on the server PC. While a client
> has a device attached, the server PC can't use it.

### How shared devices are remembered

USBShare remembers **the device, not the USB port**:

- A device with a **serial number** (`usbshare-cli list` shows it) stays
  shared, with its alias, whichever port you plug it into. Nothing else is
  ever shared in its place.
- A device **without a serial number** is remembered as "this model in this
  port". If it turns up alone in another port, the setting moves with it. If
  two identical serial-less devices are present, each stays tied to its own
  port.
- A **different device** plugged into a shared device's old port is not
  shared until you tick it.
- An unplugged shared device stays in the list as **Not connected**, with its
  alias, and is offered again as soon as it's plugged back in. **Forget**
  removes it.

Settings saved by older versions, which went by port, are converted the
first time each device is seen in its port.

### Command line instead of the GUI

```powershell
usbshare-cli.exe list
usbshare-cli.exe serve --usbdk --share 352f:0414:MIC0123456789=mic --share 0c45:636f@1-6 --allow 192.168.1.0/24
```
`--share` takes `VID:PID:SERIAL` (that exact device, in any port;
recommended), `VID:PID@BUSID` (that model, only in that port), `VID:PID`
(that model, in any port), a bus id like `1-4` (anything in that port), or
`all`. Any of these can take an optional `=ALIAS`.
`--allow` (repeatable) limits which clients may connect; without it, anyone
can. Press
Ctrl+C to stop. Allow the port in the firewall once (in an admin
PowerShell):
```powershell
New-NetFirewallRule -DisplayName "usbshare (USB/IP)" -Direction Inbound -Protocol TCP -LocalPort 3240 -Action Allow -Profile Private
```

## Client PC (where you use the devices)

1. Install **usbip-win2** from
   <https://github.com/vadimgrn/usbip-win2/releases>, then reboot if it asks.
2. In a terminal:
   ```powershell
   usbip list -r 192.168.1.50            # what the server offers
   usbip attach -r 192.168.1.50 -b 1-4   # attach the mic
   usbip attach -r 192.168.1.50 -b 1-6   # and the camera, at the same time
   usbip port                            # what's attached
   usbip detach -p 1                     # detach one (port from 'usbip port')
   ```
   Each device then shows up in Device Manager as if it were plugged in
   locally.

## Options

```
usbshare-cli serve --share X[=ALIAS] [--share Y ...] [--allow IP|NETWORK ...] [--usbdk] [--host 0.0.0.0] [--port 3240] [-v]
```

- `--share 1-6=camera` shares the device under the busid `camera`
  (1-31 letters, digits, `-`, `_` or `.`). Clients then run
  `usbip attach -b camera`, and `usbip port` shows
  `usbip://<server>:3240/camera`. The real port number is no longer
  offered. If a VID:PID alias matches several identical devices, they're
  named `camera`, `camera-2`, and so on.
- Clients show the Windows device name in `usbip list -r`. `usbip port`
  shows only the busid and its built-in vendor/product lookup, so use an
  alias if you want a name there. The usbip-win2 GUI (`wusbip`) also lets
  you add per-device Notes (Ctrl+N).
- `-v` prints every request and reply for debugging.
- Devices picked by VID:PID are offered again after you unplug and replug
  them.
- Each device can be attached by one client at a time. A second client gets
  "busy".

## Limits and security

- **There is no authentication or encryption**, the same as standard USB/IP.
  Run it only on a trusted LAN, set **Trusted clients** (`--allow`) to your
  client PCs, keep the firewall rule on the *Private* profile, and share only
  the devices you mean to share. A trusted-clients list checks IP addresses,
  so it doesn't stop someone on your LAN who takes over a trusted address.
- Isochronous transfers (webcams, audio) work, but they're sensitive to
  latency. Wired Ethernet works much better than Wi-Fi for them.
- Port resets from the client are acknowledged but not passed to the real
  device.

## Development

```powershell
python -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\python -m unittest discover -s tests -v
.venv\Scripts\pip install pyinstaller
.venv\Scripts\pyinstaller --noconfirm --onefile --windowed --name USBShare --icon packaging\usbshare.ico --paths . --collect-binaries usb1 --hidden-import pystray._win32 --distpath dist --workpath build\pyi-gui --specpath build packaging\gui_entry.py
.venv\Scripts\pyinstaller --noconfirm --onefile --name usbshare-cli --icon packaging\usbshare.ico --paths . --collect-binaries usb1 --exclude-module PIL --exclude-module tkinter --exclude-module pystray --distpath dist --workpath build\pyi --specpath build packaging\entry.py
```

Layout:

| File | Contents |
|---|---|
| `usbshare/protocol.py` | USB/IP wire format |
| `usbshare/server.py` | connections, URB sessions, device selectors, trusted clients |
| `usbshare/usb_backend.py` | device access through libusb (WinUSB or UsbDk) |
| `usbshare/identity.py` | remembering shared devices by serial number and model |
| `usbshare/names.py` | device names and serial numbers from Windows |
| `usbshare/gui.py` | the USBShare window, tray icon and settings |
| `usbshare/__main__.py` | the command-line tool |

## License

MIT, see [LICENSE](LICENSE).

The released `.exe` files are built with PyInstaller and include these
third-party components under their own licenses:

| Component | License |
|---|---|
| [libusb](https://libusb.info) | LGPL-2.1-or-later |
| [python-libusb1](https://github.com/vpelletier/python-libusb1) | LGPL-2.1-or-later |
| [pystray](https://github.com/moses-palmer/pystray) (GUI only) | LGPL-3.0 |
| [Pillow](https://python-pillow.org) (GUI only) | MIT-CMU |
| Python and Tcl/Tk | PSF License, Tcl/Tk License |
| [PyInstaller](https://pyinstaller.org) bootloader | GPL-2.0 with the bootloader exception |

You can rebuild the executables with your own versions of these libraries
using the commands under [Development](#development).

You install these separately (they aren't included):
[UsbDk](https://github.com/daynix/UsbDk) (Apache-2.0) on the server, and
[usbip-win2](https://github.com/vadimgrn/usbip-win2) (GPL-3.0) on the client.
