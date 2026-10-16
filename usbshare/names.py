"""Friendly device names and bound drivers, as Windows reports them."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
import time

from .protocol import DeviceInfo

log = logging.getLogger(__name__)

_QUERY = r"""
$ErrorActionPreference = 'SilentlyContinue'
$keys = 'DEVPKEY_Device_BusReportedDeviceDesc','DEVPKEY_Device_LocationInfo','DEVPKEY_Device_Service'
$rows = Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -match '^USB\\VID_[0-9A-F]{4}&PID_[0-9A-F]{4}\\' } | ForEach-Object {
  $p = Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName $keys
  [pscustomobject]@{
    id = $_.InstanceId; name = $_.FriendlyName
    desc = ($p | Where-Object KeyName -eq $keys[0]).Data
    location = ($p | Where-Object KeyName -eq $keys[1]).Data
    service = ($p | Where-Object KeyName -eq $keys[2]).Data
  }
}
[pscustomobject]@{ usbdk = [bool](Get-Service UsbDk); devices = @($rows) } | ConvertTo-Json -Depth 3 -Compress
"""


def query_windows() -> tuple[bool, list[dict]]:
    """Return (UsbDk installed, PnP entries for USB devices). Best effort."""
    shell = shutil.which("powershell")
    if shell is None:
        return False, []
    try:
        out = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", _QUERY],
            capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
        data = json.loads(out)
    except (subprocess.SubprocessError, ValueError, OSError):
        return False, []
    return bool(data.get("usbdk")), data.get("devices") or []


def entry_for(info: DeviceInfo, entries: list[dict]) -> dict:
    """Pick the PnP entry of this device: same VID/PID, and the same hub port if ambiguous."""
    prefix = f"USB\\VID_{info.vendor:04X}&PID_{info.product:04X}\\"
    candidates = [e for e in entries if str(e.get("id", "")).upper().startswith(prefix)]
    port = int(re.split(r"[-.]", info.busid)[-1])
    for entry in candidates:
        found = re.search(r"Port_#(\d+)", str(entry.get("location") or ""))
        if found and int(found.group(1)) == port:
            return entry
    return candidates[0] if len(candidates) == 1 else {}


def display_name(entry: dict) -> str:
    return str(entry.get("desc") or entry.get("name") or "")


def serial_of(entry: dict) -> str | None:
    """Serial number from a USB instance id: USB\\VID_x&PID_y\\<serial>.

    Devices without a serial get a Windows-made id containing '&' instead ("" here);
    None means the entry is missing, so we do not know yet.
    """
    parts = str(entry.get("id") or "").split("\\")
    if len(parts) < 3 or not parts[2]:
        return None
    return "" if "&" in parts[2] else parts[2]


class WindowsNames:
    """Fills name and serial per device; queries Windows (at most every few seconds) for new ones."""

    def __init__(self, min_refresh_seconds: float = 2.0):
        self._lock = threading.Lock()
        self._entries: list[dict] = []
        self._known: dict[tuple[str, int, int, int], tuple[str, str | None]] = {}
        self._last_query = float("-inf")
        self._min_refresh = min_refresh_seconds

    def fill(self, infos: list[DeviceInfo]) -> None:
        with self._lock:
            missing = [i for i in infos if self._key(i) not in self._known]
            if missing and time.monotonic() - self._last_query >= self._min_refresh:
                _, self._entries = query_windows()
                self._last_query = time.monotonic()
            for info in missing:
                entry = entry_for(info, self._entries)
                if entry:
                    self._known[self._key(info)] = (display_name(entry), serial_of(entry))
            for info in infos:
                known = self._known.get(self._key(info))
                if known is not None:
                    info.description = known[0] or info.description
                    info.serial = known[1]

    @staticmethod
    def _key(info: DeviceInfo) -> tuple[str, int, int, int]:
        # devnum changes on every plug-in, so a different device in the same port
        # (even the same model) is looked up afresh instead of inheriting a serial
        return info.busid, info.vendor, info.product, info.devnum
