"""USBShare: portable Windows GUI for sharing USB devices over the LAN (USB/IP)."""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import tkinter as tk
import webbrowser
import winreg
from pathlib import Path
from tkinter import messagebox, scrolledtext, simpledialog, ttk
from typing import Callable

import pystray
from PIL import ImageTk

from . import __version__
from .icon import make_icon
from .identity import entry_key, entry_selector, new_entry, reconcile
from .names import WindowsNames, query_windows
from .protocol import DEFAULT_PORT, DeviceInfo
from .server import ALIAS_PATTERN, UsbIpServer, parse_trusted
from .usb_backend import LibusbBackend

log = logging.getLogger("usbshare.gui")

FIREWALL_RULE = "usbshare (USB/IP)"
USBDK_URL = "https://github.com/daynix/UsbDk/releases"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SETTINGS_FILE = "usbshare.json"
SCAN_EVERY_MS = 3000
TICK_MS = 1000

CHECKED, UNCHECKED = "☑", "☐"


# -- system helpers ------------------------------------------------------------

def app_dir() -> Path:
    if getattr(sys, "frozen", False):  # PyInstaller exe: keep settings beside it
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def fallback_dir() -> Path:
    return Path(os.environ.get("APPDATA", Path.home())) / "usbshare"


def lan_address() -> str | None:
    """The address of the default-route interface (skips Hyper-V/WSL virtual adapters)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # UDP connect sends nothing; it only picks a route
        return probe.getsockname()[0]
    except OSError:
        pass
    finally:
        probe.close()
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return None
    addresses = sorted({i[4][0] for i in infos if not i[4][0].startswith(("127.", "169.254."))})
    return addresses[0] if addresses else None


def firewall_rule_ports() -> set[int] | None:
    """Ports allowed by our firewall rule; empty if there is no rule, None if unknown."""
    try:
        out = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={FIREWALL_RULE}"],
            capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return set()
    ports = set()
    for line in out.stdout.splitlines():
        label, _, value = line.partition(":")
        if label.strip().lower() == "localport":
            ports.update(int(p) for p in value.split(",") if p.strip().isdigit())
    return ports or None  # rule exists but output is localized: assume it fits


def add_firewall_rule(port: int) -> bool:
    """Replace our inbound rule for `port`, via a UAC prompt. Returns False if refused."""
    command = (
        f'/c netsh advfirewall firewall delete rule name="{FIREWALL_RULE}" & '
        f'netsh advfirewall firewall add rule name="{FIREWALL_RULE}" dir=in action=allow '
        f"protocol=TCP localport={port} profile=private,domain"
    )
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", command, None, 0) > 32


def is_keyboard_or_mouse(info: DeviceInfo) -> bool:
    return any(i.cls == 3 and i.subclass == 1 and i.protocol in (1, 2) for i in info.interfaces)


def split_trusted(text: str) -> list[str]:
    return [entry for entry in re.split(r"[,;\s]+", text) if entry]


# Start with Windows: a per-user Run entry, so no admin rights are needed.
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_VALUE = "USBShare"


def startup_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pythonw}" -m usbshare.gui --minimized'


def get_startup(name: str = STARTUP_VALUE) -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return str(winreg.QueryValueEx(key, name)[0])
    except OSError:
        return None


def set_startup(enabled: bool, name: str = STARTUP_VALUE) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, name)
            except FileNotFoundError:
                pass


# One running copy: a second launch (e.g. from the Start menu while the login copy
# sits in the tray) signals the first to show its window, then exits.
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateEventW.restype = ctypes.c_void_p
_kernel32.CreateEventW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_bool, ctypes.c_wchar_p)
_kernel32.SetEvent.argtypes = (ctypes.c_void_p,)
_kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
_kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
_kernel32.WaitForSingleObject.restype = ctypes.c_uint32
SHOW_EVENT = "Local\\USBShare.ShowWindow"
ERROR_ALREADY_EXISTS = 183
INFINITE = 0xFFFFFFFF


def claim_instance(name: str = SHOW_EVENT) -> int | None:
    """Event handle if we are the first copy; otherwise wake the first copy and return None."""
    ctypes.set_last_error(0)
    handle = _kernel32.CreateEventW(None, False, False, name)
    if handle and ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        _kernel32.SetEvent(handle)
        _kernel32.CloseHandle(handle)
        return None
    return handle or 0


# -- settings ------------------------------------------------------------------

class Settings:
    def __init__(self) -> None:
        self.port = DEFAULT_PORT
        self.usbdk = True
        self.autostart = False
        self.tray = True
        self.trusted = ""  # comma-separated IPs/networks; empty = anyone
        # identity key -> entry; see identity.py. Older versions keyed entries by port.
        self.devices: dict[str, dict] = {}

    @classmethod
    def load(cls) -> "Settings":
        settings = cls()
        for folder in (app_dir(), fallback_dir()):
            path = folder / SETTINGS_FILE
            try:
                # utf-8-sig: editors such as PowerShell or Notepad may add a BOM
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as error:
                # Keep the user's file before the next save replaces it with defaults.
                backup = path.with_name(SETTINGS_FILE + ".broken")
                try:
                    backup.write_bytes(path.read_bytes())
                    log.warning("could not read %s (%s); a copy was saved as %s", path, error, backup.name)
                except OSError:
                    log.warning("could not read %s (%s)", path, error)
                continue
            settings.port = int(data.get("port", settings.port))
            settings.usbdk = bool(data.get("usbdk", settings.usbdk))
            settings.autostart = bool(data.get("autostart", settings.autostart))
            settings.tray = bool(data.get("tray", settings.tray))
            settings.trusted = str(data.get("trusted", settings.trusted))
            settings.devices = {str(k): dict(v) for k, v in data.get("devices", {}).items()}
            break
        return settings

    def save(self) -> None:
        data = json.dumps({
            "port": self.port, "usbdk": self.usbdk, "autostart": self.autostart,
            "tray": self.tray, "trusted": self.trusted, "devices": self.devices,
        }, indent=2)
        for folder in (app_dir(), fallback_dir()):  # portable first, %APPDATA% if read-only
            try:
                folder.mkdir(parents=True, exist_ok=True)
                (folder / SETTINGS_FILE).write_text(data, encoding="utf-8")
                return
            except OSError:
                continue
        log.warning("could not save settings")

    def selectors(self) -> list[str]:
        return [s for e in self.devices.values() if e.get("share") and (s := entry_selector(e))]

    def has_shared(self) -> bool:
        return any(e.get("share") for e in self.devices.values())


class UiLogHandler(logging.Handler):
    def __init__(self, post: Callable[[Callable[[], None]], None], sink: Callable[[str], None]):
        super().__init__()
        self._post, self._sink = post, sink
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        line = self.format(record)
        self._post(lambda: self._sink(line))


# -- window --------------------------------------------------------------------

class App:
    COLUMNS = ("share", "busid", "vidpid", "name", "alias", "status")
    _TRUSTED_HINT = "IPs or networks, comma-separated. 0.0.0.0 = anyone"

    def __init__(self, root: tk.Tk, minimized: bool = False):
        self.root = root
        # Log to the window from the start (lines are queued until it exists), so
        # problems reading the settings file show up there.
        self._events: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
        logging.getLogger().addHandler(UiLogHandler(self._events.put, self._append_log))
        logging.getLogger().setLevel(logging.INFO)
        self.settings = Settings.load()
        self.names = WindowsNames()
        self.backend: LibusbBackend | None = None
        self.server: UsbIpServer | None = None
        self.server_error: str | None = None
        self.state = "stopped"  # stopped | starting | running | stopping
        self.devices: dict[str, DeviceInfo] = {}  # present devices by bus id
        self.matched: dict[str, str] = {}  # bus id -> settings entry key
        self._autostart_pending = False
        self.usbdk_installed: bool | None = None
        self._scanning = False
        self._scan_countdown = 0
        self.tray: pystray.Icon | None = None
        self._tray_key: tuple | None = None
        self._tray_hint_shown = minimized  # no balloon at every login

        self._build()
        log.info("USBShare %s, settings in %s", __version__, app_dir() / SETTINGS_FILE)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Unmap>", self._on_unmap)
        self.root.after(100, self._pump)
        self._sync_tray()
        self._refresh_startup_path()
        if minimized:
            if self.tray is not None:
                self.root.withdraw()
            else:
                self.root.iconify()
        threading.Thread(target=self._probe_system, daemon=True).start()

    # -- layout --------------------------------------------------------

    def _build(self) -> None:
        root = self.root
        root.title("USBShare - share USB devices over the LAN")
        root.geometry("980x620")
        root.minsize(760, 460)
        ttk.Style().theme_use("vista")
        self._window_icon = ImageTk.PhotoImage(make_icon(64))
        root.iconphoto(True, self._window_icon)

        top = ttk.Frame(root, padding=(10, 10, 10, 4))
        top.pack(fill="x")
        self.start_button = ttk.Button(top, text="Start sharing", width=16, command=self._toggle_server)
        self.start_button.pack(side="left")
        ttk.Label(top, text="Port").pack(side="left", padx=(16, 4))
        self.port_var = tk.StringVar(value=str(self.settings.port))
        self.port_entry = ttk.Entry(top, textvariable=self.port_var, width=7)
        self.port_entry.pack(side="left")
        self.usbdk_var = tk.BooleanVar(value=self.settings.usbdk)
        self.usbdk_check = ttk.Checkbutton(top, text="Use UsbDk", variable=self.usbdk_var,
                                           command=self._usbdk_changed)
        self.usbdk_check.pack(side="left", padx=(16, 0))
        ttk.Label(top, text="Trusted clients").pack(side="left", padx=(16, 4))
        self.trusted_var = tk.StringVar(value=self.settings.trusted or "0.0.0.0")
        self.trusted_entry = ttk.Entry(top, textvariable=self.trusted_var, width=34)
        self.trusted_entry.pack(side="left")
        self.trusted_entry.bind("<Return>", lambda _: self._trusted_changed())
        self.trusted_entry.bind("<FocusOut>", lambda _: self._trusted_changed())
        self.trusted_hint = ttk.Label(top, text=self._TRUSTED_HINT, foreground="#555555")
        self.trusted_hint.pack(side="left", padx=(6, 0))

        options = ttk.Frame(root, padding=(10, 0, 10, 4))
        options.pack(fill="x")
        self.autostart_var = tk.BooleanVar(value=self.settings.autostart)
        ttk.Checkbutton(options, text="Start sharing when the app opens", variable=self.autostart_var,
                        command=self._autostart_changed).pack(side="left")
        self.tray_var = tk.BooleanVar(value=self.settings.tray)
        ttk.Checkbutton(options, text="Minimize and close to the tray", variable=self.tray_var,
                        command=self._tray_changed).pack(side="left", padx=(16, 0))
        self.startup_var = tk.BooleanVar(value=get_startup() is not None)
        ttk.Checkbutton(options, text="Start with Windows (hidden in the tray)", variable=self.startup_var,
                        command=self._startup_changed).pack(side="left", padx=(16, 0))

        self.status_var = tk.StringVar(value="Stopped")
        self.status_label = ttk.Label(root, textvariable=self.status_var, padding=(10, 2),
                                      font=("Segoe UI", 10, "bold"))
        self.status_label.pack(fill="x")

        checks = ttk.Frame(root, padding=(10, 0, 10, 4))
        checks.pack(fill="x")
        self.usbdk_info = ttk.Label(checks, text="Checking UsbDk...")
        self.usbdk_info.pack(side="left")
        self.usbdk_link = ttk.Button(checks, text="Get UsbDk", command=lambda: webbrowser.open(USBDK_URL))
        self.firewall_button = ttk.Button(checks, text="Allow in firewall...", command=self._fix_firewall)
        self.firewall_button.pack(side="right")
        self.firewall_info = ttk.Label(checks, text="Checking firewall...")
        self.firewall_info.pack(side="right", padx=(0, 8))

        table = ttk.Frame(root, padding=(10, 0, 10, 0))
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=self.COLUMNS, show="headings", selectmode="browse")
        for column, title, width, stretch in (
            ("share", "Share", 56, False), ("busid", "Bus ID", 70, False),
            ("vidpid", "VID:PID", 90, False), ("name", "Device", 280, True),
            ("alias", "Alias (client uses)", 150, False), ("status", "Status", 230, True),
        ):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, stretch=stretch,
                             anchor="center" if column == "share" else "w")
        self.tree.tag_configure("attached", foreground="#0a7d20")
        self.tree.tag_configure("offline", foreground="#888888")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<<TreeviewSelect>>", lambda _: self._update_buttons())

        actions = ttk.Frame(root, padding=(10, 6, 10, 4))
        actions.pack(fill="x")
        ttk.Button(actions, text="Refresh", command=self._scan).pack(side="left")
        self.alias_button = ttk.Button(actions, text="Set alias...", command=self._edit_alias)
        self.alias_button.pack(side="left", padx=(6, 0))
        self.disconnect_button = ttk.Button(actions, text="Disconnect client", command=self._disconnect)
        self.disconnect_button.pack(side="left", padx=(6, 0))
        self.forget_button = ttk.Button(actions, text="Forget", command=self._forget)
        self.forget_button.pack(side="left", padx=(6, 0))
        ttk.Label(actions, text="Tick 'Share' for each device to offer. Double-click an alias to rename.",
                  foreground="#555555").pack(side="right")

        self.log_text = scrolledtext.ScrolledText(root, height=8, state="disabled",
                                                  font=("Consolas", 9), relief="flat")
        self.log_text.pack(fill="x", padx=10, pady=(4, 10))
        self._update_buttons()

    # -- plumbing ------------------------------------------------------

    def _pump(self) -> None:
        """Run callbacks posted by worker threads, on the Tk thread."""
        try:
            while True:
                self._events.get_nowait()()
        except queue.Empty:
            pass
        self.root.after(100, self._pump)

    def _worker(self, work: Callable[[], object], then: Callable[[object], None] | None = None) -> None:
        def run() -> None:
            try:
                result = work()
            except Exception as error:  # surface on the UI thread
                log.exception("background task failed")
                result = error
            if then is not None:
                self._events.put(lambda: then(result))
        threading.Thread(target=run, daemon=True).start()

    def _append_log(self, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        if int(self.log_text.index("end-1c").split(".")[0]) > 500:
            self.log_text.delete("1.0", "100.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # -- startup -------------------------------------------------------

    def _probe_system(self) -> None:
        usbdk, _ = query_windows()
        ports = firewall_rule_ports()
        self._events.put(lambda: self._system_probed(usbdk, ports))

    def _system_probed(self, usbdk: bool, ports: set[int] | None) -> None:
        self.usbdk_installed = usbdk
        if usbdk:
            self.usbdk_info.configure(text="UsbDk installed: any device can be shared.")
        else:
            self.usbdk_info.configure(
                text="UsbDk is not installed: only devices using the WinUSB driver can be shared.")
            self.usbdk_link.pack(side="left", padx=(8, 0))
            self.usbdk_var.set(False)
            self.usbdk_check.state(["disabled"])
        self._show_firewall(ports)
        self._open_backend()
        self._autostart_pending = self.settings.autostart  # after the first scan has matched devices
        self._scan()
        self.root.after(TICK_MS, self._tick)

    def _open_backend(self) -> None:
        want = bool(self.usbdk_var.get() and self.usbdk_installed)
        if self.backend is not None and self.backend.use_usbdk == want:
            return
        if self.backend is not None:
            self.backend.close()
            self.backend = None
        try:
            self.backend = LibusbBackend(use_usbdk=want)
        except RuntimeError as error:
            log.error("%s", error)
            self.usbdk_var.set(False)
            self.backend = LibusbBackend(use_usbdk=False)

    # -- firewall ------------------------------------------------------

    def _show_firewall(self, ports: set[int] | None) -> None:
        port = self._port_or_none() or self.settings.port
        if ports is None or port in ports:
            self.firewall_info.configure(text=f"Firewall: TCP {port} allowed.", foreground="#0a7d20")
            self.firewall_button.pack_forget()
        else:
            self.firewall_info.configure(
                text=f"Firewall: TCP {port} is not allowed, other PCs cannot connect.", foreground="#b00020")
            self.firewall_button.pack(side="right")

    def _fix_firewall(self) -> None:
        port = self._port_or_none()
        if port is None:
            return
        if not add_firewall_rule(port):
            messagebox.showwarning("USBShare", "The firewall change was cancelled.")
            return
        log.info("firewall rule for TCP %d requested", port)
        self.root.after(3000, lambda: self._worker(firewall_rule_ports, self._show_firewall))

    # -- devices -------------------------------------------------------

    def _scan(self) -> None:
        backend = self.backend
        if backend is None or self._scanning:
            return
        self._scanning = True

        def work() -> list[DeviceInfo]:
            infos = backend.scan()
            self.names.fill(infos)
            return infos

        def done(result: object) -> None:
            self._scanning = False
            if isinstance(result, list):
                found = {d.busid: d for d in result}
                # A device a client holds through UsbDk vanishes from Windows' list
                # until it is released; keep its row instead of showing "Not connected".
                in_use = self.server.in_use() if self.server is not None else {}
                for busid in in_use:
                    if busid not in found and busid in self.devices:
                        found[busid] = self.devices[busid]
                self.devices = found
                self._apply_identities()
                self._render()
                if self._autostart_pending:
                    self._autostart_pending = False
                    self._start(quiet=True)

        self._worker(work, done)

    def _apply_identities(self) -> None:
        entries, self.matched, messages = reconcile(self.settings.devices, list(self.devices.values()))
        for message in messages:
            log.info("%s", message)
        if entries != self.settings.devices:
            self.settings.devices = entries
            self.settings.save()
            self._push_selectors()

    def _push_selectors(self) -> None:
        if self.server is not None and self.state == "running":
            self.server.set_selectors(self.settings.selectors())

    def _tick(self) -> None:
        self._check_server()
        self._render()
        self._update_tray()
        if self._scan_countdown <= 0:
            self._scan_countdown = SCAN_EVERY_MS // TICK_MS
            self._scan()
        self._scan_countdown -= 1
        self.root.after(TICK_MS, self._tick)

    def _render(self) -> None:
        in_use = self.server.in_use() if self.server is not None and self.state == "running" else {}
        rows: list[tuple[str, DeviceInfo | None, dict]] = [
            (busid, info, self.settings.devices.get(self.matched.get(busid, ""), {}))
            for busid, info in self.devices.items()
        ]
        present = set(self.matched.values())
        rows += [(f"saved:{key}", None, entry) for key, entry in self.settings.devices.items()
                 if entry.get("share") and key not in present]
        selected = self.tree.selection()
        existing = set(self.tree.get_children())
        for iid, info, entry in rows:
            shared = bool(entry.get("share"))
            if info is not None and info.busid in in_use:
                status, tag = f"In use by {in_use[info.busid].rsplit(':', 1)[0]}", "attached"
            elif info is None:
                status, tag = "Not connected", "offline"
            elif not shared:
                status, tag = "Not shared", ""
            elif self.state == "running":
                status, tag = "Shared, waiting for a client", ""
            else:
                status, tag = "Will be shared when started", ""
            if info is not None:
                busid, vid_pid = info.busid, f"{info.vendor:04x}:{info.product:04x}"
            else:  # last port it was seen in; legacy entries are keyed by it
                busid = entry.get("busid") or iid.removeprefix("saved:")
                vid_pid = f"{entry['vid']:04x}:{entry['pid']:04x}" if "vid" in entry else ""
            values = (
                CHECKED if shared else UNCHECKED,
                busid,
                vid_pid,
                (info.description if info else "") or entry.get("name", ""),
                entry.get("alias", ""),
                status,
            )
            if iid in existing:
                self.tree.item(iid, values=values, tags=(tag,))
            else:
                self.tree.insert("", "end", iid=iid, values=values, tags=(tag,))
        for stale in existing - {row[0] for row in rows}:
            self.tree.delete(stale)
        if selected and self.tree.exists(selected[0]):
            self.tree.selection_set(selected)
        self._update_buttons()

    def _selected(self) -> str | None:
        selection = self.tree.selection()
        return selection[0] if selection else None

    def _row(self, iid: str) -> tuple[DeviceInfo | None, str | None]:
        """(present device or None, settings entry key or None) for a table row."""
        if iid.startswith("saved:"):
            return None, iid.removeprefix("saved:")
        return self.devices.get(iid), self.matched.get(iid)

    def _entry_for_row(self, iid: str) -> tuple[DeviceInfo | None, str, dict] | None:
        """The row's settings entry, created for a device seen for the first time."""
        info, key = self._row(iid)
        if key is not None and key in self.settings.devices:
            return info, key, self.settings.devices[key]
        if info is None:
            return None
        if info.serial is None:
            messagebox.showinfo("USBShare", "Still reading this device's details from Windows. "
                                            "Try again in a few seconds.")
            return None
        entry = new_entry(info)
        key = entry_key(entry)
        self.settings.devices[key] = entry
        self.matched[info.busid] = key
        return info, key, entry

    def _update_buttons(self) -> None:
        iid = self._selected()
        info, key = self._row(iid) if iid else (None, None)
        attached = bool(info and self.server and info.busid in self.server.in_use())
        self.alias_button.state(["!disabled"] if iid else ["disabled"])
        self.disconnect_button.state(["!disabled"] if attached else ["disabled"])
        self.forget_button.state(["!disabled"] if info is None and key else ["disabled"])

    def _on_click(self, event: tk.Event) -> str | None:
        if self.tree.identify_region(event.x, event.y) != "cell":
            return None
        iid = self.tree.identify_row(event.y)
        if iid and self.tree.identify_column(event.x) == "#1":
            self.tree.selection_set(iid)
            self._toggle_share(iid)
            return "break"
        return None

    def _on_double_click(self, event: tk.Event) -> None:
        iid = self.tree.identify_row(event.y)
        if iid and self.tree.identify_column(event.x) != "#1":
            self.tree.selection_set(iid)
            self._edit_alias()

    def _toggle_share(self, iid: str) -> None:
        found = self._entry_for_row(iid)
        if found is None:
            return
        info, _, entry = found
        if not entry.get("share"):
            if info is not None and is_keyboard_or_mouse(info) and not messagebox.askyesno(
                "USBShare",
                f"'{info.description or info.busid}' looks like a keyboard or mouse.\n\n"
                "While a client uses it, this PC cannot. Share it anyway?",
            ):
                return
            entry["share"] = True
        else:
            entry["share"] = False
            if (info is not None and self.server is not None and info.busid in self.server.in_use()
                    and messagebox.askyesno(
                        "USBShare", "A client is using this device. Disconnect it now?\n\n"
                                    "(Otherwise it stays attached until the client detaches.)")):
                self.server.disconnect(info.busid)
        self._settings_changed()

    def _edit_alias(self) -> None:
        iid = self._selected()
        found = self._entry_for_row(iid) if iid else None
        if found is None:
            return
        info, key, entry = found
        label = entry.get("name") or (info.busid if info else key)
        others = {e.get("alias") for k, e in self.settings.devices.items() if k != key}
        others |= set(self.devices) - ({info.busid} if info else set())  # real bus ids
        while True:
            alias = simpledialog.askstring(
                "Alias",
                f"Name clients use for {label} (usbip attach -b NAME).\n"
                "Letters, digits, '-', '_' or '.'; leave empty to use the bus id.",
                initialvalue=entry.get("alias", ""), parent=self.root,
            )
            if alias is None:
                return
            alias = alias.strip()
            if alias and not ALIAS_PATTERN.fullmatch(alias):
                messagebox.showerror("Alias", "Use 1-31 letters, digits, '-', '_' or '.'.")
            elif alias and alias in others:
                messagebox.showerror("Alias", f"'{alias}' is already used.")
            else:
                break
        entry["alias"] = alias
        self._settings_changed()

    def _disconnect(self) -> None:
        iid = self._selected()
        info = self._row(iid)[0] if iid else None
        if info is not None and self.server is not None and self.server.disconnect(info.busid):
            log.info("disconnected the client of %s", info.description or info.busid)

    def _forget(self) -> None:
        iid = self._selected()
        info, key = self._row(iid) if iid else (None, None)
        if info is None and key:
            self.settings.devices.pop(key, None)
            self._settings_changed()

    def _settings_changed(self) -> None:
        self.settings.save()
        self._push_selectors()
        self._render()

    def _usbdk_changed(self) -> None:
        self.settings.usbdk = bool(self.usbdk_var.get())
        self.settings.save()
        self._open_backend()
        self._scan()

    def _autostart_changed(self) -> None:
        self.settings.autostart = bool(self.autostart_var.get())
        self.settings.save()

    # -- server --------------------------------------------------------

    def _port_or_none(self) -> int | None:
        try:
            port = int(self.port_var.get())
        except ValueError:
            port = 0
        if not 1 <= port <= 65535:
            messagebox.showerror("USBShare", "Port must be a number from 1 to 65535.")
            return None
        return port

    def _toggle_server(self) -> None:
        if self.state == "stopped":
            self._start()
        elif self.state == "running":
            self._stop()

    def _start(self, quiet: bool = False) -> None:
        port = self._port_or_none()
        if port is None:
            return
        if not self.settings.has_shared():
            if quiet:
                log.info("nothing is ticked to share yet")
            else:
                messagebox.showinfo("USBShare", "Tick 'Share' for at least one device first.")
            return
        if not self._trusted_changed():
            messagebox.showerror("USBShare", "Fix the trusted clients field first.")
            return
        self.settings.port = port
        self.settings.save()
        self._open_backend()
        self.server = UsbIpServer(self.backend, self.settings.selectors(), port=port,
                                  namer=self.names.fill, trusted=split_trusted(self.settings.trusted))
        self.server_error = None
        server = self.server

        def run() -> None:
            try:
                server.serve_forever()
            except OSError as error:
                self.server_error = (
                    f"Port {port} is already in use (is another USBShare running?)"
                    if getattr(error, "winerror", None) == 10048 else str(error))
                log.error("cannot listen on port %d: %s", port, error)

        self.state = "starting"
        threading.Thread(target=run, name="usbip-server", daemon=True).start()
        self._set_controls()
        self._worker(firewall_rule_ports, self._show_firewall)

    def _trusted_changed(self) -> bool:
        """Validate and apply the trusted clients field. Returns False if it is invalid."""
        entries = split_trusted(self.trusted_var.get())
        try:
            networks = parse_trusted(entries)
        except ValueError as error:
            self.trusted_hint.configure(text=str(error), foreground="#b00020")
            return False
        self.trusted_hint.configure(text=self._TRUSTED_HINT, foreground="#555555")
        normalized = ", ".join(
            str(n.network_address) if n.prefixlen == n.max_prefixlen else str(n)
            for n in networks or ()
        )
        self.trusted_var.set(normalized or "0.0.0.0")
        if normalized != self.settings.trusted:
            self.settings.trusted = normalized
            self.settings.save()
            log.info("trusted clients: %s", normalized or "anyone")
            if self.server is not None and self.state == "running":
                self.server.set_trusted(split_trusted(normalized))
                self._set_controls()  # the status line names the trusted clients
        return True

    def _check_server(self) -> None:
        if self.state == "starting" and self.server is not None:
            if self.server.ready.is_set():
                self.state = "running"
                self._set_controls()
            elif self.server_error:
                messagebox.showerror("USBShare", self.server_error)
                self.server, self.state = None, "stopped"
                self._set_controls()

    def _stop(self) -> None:
        server = self.server
        if server is None:
            return
        self.state = "stopping"
        self._set_controls()

        def done(_: object) -> None:
            self.server, self.state = None, "stopped"
            log.info("sharing stopped, devices returned to Windows")
            self._set_controls()
            self._render()

        self._worker(server.shutdown, done)

    def _set_controls(self) -> None:
        running = self.state in ("starting", "running", "stopping")
        self.start_button.configure(text={
            "stopped": "Start sharing", "starting": "Starting...",
            "running": "Stop sharing", "stopping": "Stopping...",
        }[self.state])
        self.start_button.state(["!disabled"] if self.state in ("stopped", "running") else ["disabled"])
        self.port_entry.state(["disabled"] if running else ["!disabled"])
        if self.usbdk_installed:
            self.usbdk_check.state(["disabled"] if running else ["!disabled"])
        if self.state == "running":
            address = lan_address() or "<this-PC-ip>"
            who = f"Trusted clients: {self.settings.trusted}." if self.settings.trusted else "Any client may connect."
            self.status_var.set(
                f"Sharing on {address} port {self.server.port}. {who}  "
                f"Client: usbip attach -r {address} -b <Alias or Bus ID>")
            self.status_label.configure(foreground="#0a7d20")
        else:
            self.status_var.set({"stopped": "Stopped", "starting": "Starting...",
                                 "stopping": "Stopping: disconnecting clients..."}[self.state])
            self.status_label.configure(foreground="")
        self._render()
        self._update_tray()

    # -- tray, start with Windows, window state -------------------------

    def _sync_tray(self) -> None:
        if self.settings.tray and self.tray is None:
            post = self._events.put
            self.tray = pystray.Icon("USBShare", make_icon(64, active=False), "USBShare", menu=pystray.Menu(
                pystray.MenuItem("Open USBShare", lambda icon, item: post(self.show_window), default=True),
                pystray.MenuItem(
                    lambda item: "Stop sharing" if self.state == "running" else "Start sharing",
                    lambda icon, item: post(self._toggle_server),
                    enabled=lambda item: self.state in ("running", "stopped"),
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Exit", lambda icon, item: post(self.quit)),
            ))
            self.tray.run_detached()
            self._tray_key = None
            self._update_tray()
        elif not self.settings.tray and self.tray is not None:
            self._remove_tray()
            self.show_window()  # never leave the window unreachable

    def _update_tray(self) -> None:
        if self.tray is None:
            return
        running = self.state == "running"
        in_use = len(self.server.in_use()) if running and self.server is not None else 0
        title = (f"USBShare: sharing, {in_use} device(s) in use" if running
                 else f"USBShare: {self.state}")
        if (running, title) != self._tray_key:
            self._tray_key = (running, title)
            self.tray.icon = make_icon(64, active=running)
            self.tray.title = title
            self.tray.update_menu()

    def _tray_changed(self) -> None:
        self.settings.tray = bool(self.tray_var.get())
        self.settings.save()
        self._sync_tray()

    def _startup_changed(self) -> None:
        enabled = bool(self.startup_var.get())
        try:
            set_startup(enabled)
        except OSError as error:
            messagebox.showerror("USBShare", f"Could not change the startup setting:\n{error}")
            self.startup_var.set(get_startup() is not None)
            return
        log.info("start with Windows: %s", "on" if enabled else "off")

    def _refresh_startup_path(self) -> None:
        """A portable exe may have moved since 'Start with Windows' was set: follow it."""
        registered = get_startup()
        if registered is not None and registered != startup_command():
            try:
                set_startup(True)
                log.info("start with Windows now points to %s", startup_command())
            except OSError:
                pass

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()
        self.root.attributes("-topmost", True)  # Windows won't raise a background app otherwise
        self.root.after(200, lambda: self.root.attributes("-topmost", False))

    def hide_to_tray(self) -> None:
        self.root.withdraw()
        if self.tray is not None and not self._tray_hint_shown:
            self._tray_hint_shown = True
            try:
                self.tray.notify("USBShare is still running here. Right-click the icon to open or exit.",
                                 "USBShare")
            except Exception:  # balloons are optional
                pass

    def watch_show_requests(self, handle: int) -> None:
        if not handle:
            return

        def wait() -> None:
            while _kernel32.WaitForSingleObject(handle, INFINITE) == 0:
                self._events.put(self.show_window)

        threading.Thread(target=wait, name="show-requests", daemon=True).start()

    def _on_unmap(self, event: tk.Event) -> None:
        if event.widget is self.root and self.tray is not None and self.root.state() == "iconic":
            self.root.after(0, self.hide_to_tray)

    def _on_close(self) -> None:
        if self.tray is not None:
            self.hide_to_tray()
        else:
            self.quit()

    def quit(self) -> None:
        in_use = self.server.in_use() if self.server is not None else {}
        if in_use:
            self.show_window()
            if not messagebox.askyesno(
                "USBShare", f"Clients are using {len(in_use)} device(s). Disconnect them and exit?"):
                return
        if self.server is not None:
            self.server.shutdown()
        if self.backend is not None:
            self.backend.close()
        self._remove_tray()
        for pending in self.root.tk.call("after", "info"):  # _pump, _tick, ...
            self.root.after_cancel(pending)
        self.root.destroy()

    def _remove_tray(self) -> None:
        if self.tray is not None:
            # pystray's stop() only ends its message loop; without hiding first the
            # icon lingers in the notification area until the mouse passes over it.
            self.tray.visible = False
            self.tray.stop()
            self.tray = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="USBShare", add_help=False)
    parser.add_argument("--minimized", action="store_true", help="start hidden (used at login)")
    args, _ = parser.parse_known_args(argv)  # no console to print usage errors to
    instance = claim_instance()
    if instance is None:
        return 0  # already running: that copy now shows its window
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on scaled displays
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    app = App(root, minimized=args.minimized)
    app.watch_show_requests(instance)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
