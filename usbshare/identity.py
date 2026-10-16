"""Remember shared devices by what they are, not by which USB port they are in.

A saved entry describes one device:
  {"vid": 0x352f, "pid": 0x0414, "serial": "MIC0123456789", "busid": "1-4",
   "share": True, "alias": "mic", "name": "Maono Wireless Mic RX"}

- With a serial number, the entry follows the device to any port, and nothing else
  ever matches it.
- Without one (many cheap devices), it means "this model in this port". If that
  model shows up alone in a different port, the entry moves with it.
- Entries saved by older versions were keyed by port only; they are converted the
  first time a device is seen in that port.
"""

from __future__ import annotations

from .protocol import DeviceInfo

Entry = dict


def identity_key(vid: int, pid: int, serial: str, busid: str) -> str:
    return f"{vid:04x}:{pid:04x}:{serial}" if serial else f"{vid:04x}:{pid:04x}@{busid}"


def entry_key(entry: Entry) -> str:
    return identity_key(entry["vid"], entry["pid"], entry.get("serial", ""), entry.get("busid", ""))


def is_legacy(entry: Entry) -> bool:
    return "vid" not in entry


def entry_selector(entry: Entry) -> str | None:
    """Server selector for an entry, or None until a legacy entry is converted."""
    if is_legacy(entry):
        return None  # never share "whatever is in port X" again
    selector = entry_key(entry)
    return f"{selector}={entry['alias']}" if entry.get("alias") else selector


def entry_matches(key: str, entry: Entry, info: DeviceInfo) -> bool:
    if info.serial is None:  # Windows has not told us yet; decide on a later scan
        return False
    if is_legacy(entry):
        return info.busid == key
    if (entry["vid"], entry["pid"]) != (info.vendor, info.product):
        return False
    if entry.get("serial"):
        return info.serial.lower() == entry["serial"].lower()
    return info.busid == entry.get("busid")


def new_entry(info: DeviceInfo) -> Entry:
    return {
        "vid": info.vendor, "pid": info.product, "serial": info.serial or "",
        "busid": info.busid, "share": False, "alias": "", "name": info.description,
    }


def reconcile(entries: dict[str, Entry], devices: list[DeviceInfo]) -> tuple[dict[str, Entry], dict[str, str], list[str]]:
    """Match saved entries to the devices present.

    Returns (entries, matched, messages): the updated entries (re-keyed when a
    legacy entry is converted or a device changed port), matched maps the bus id of
    each present device to its entry key, and messages describe what changed.
    """
    entries = {k: dict(v) for k, v in entries.items()}
    matched: dict[str, str] = {}
    messages: list[str] = []
    renames: dict[str, str] = {}

    for info in devices:
        key = next((k for k, e in entries.items() if entry_matches(k, e, info)), None)
        if key is not None:
            matched[info.busid] = key

    # Convert legacy port entries, and record ports/names of matched devices.
    for busid, key in list(matched.items()):
        info = next(d for d in devices if d.busid == busid)
        entry = entries[key]
        if is_legacy(entry):
            converted = new_entry(info)
            converted.update(share=entry.get("share", False), alias=entry.get("alias", ""),
                             name=info.description or entry.get("name", ""))
            entries[key] = converted
            messages.append(f"{converted['name'] or busid}: now remembered as this device, not port {busid}")
        entry = entries[key]
        entry["busid"] = busid
        entry["name"] = info.description or entry.get("name", "")
        new = entry_key(entry)
        if new != key:
            renames[key] = new

    # A serial-less device that moved: follow it when it is the only one of its model.
    claimed = set(matched)
    for key, entry in entries.items():
        if is_legacy(entry) or entry.get("serial") or key in matched.values():
            continue
        model = (entry["vid"], entry["pid"])
        candidates = [d for d in devices if (d.vendor, d.product) == model
                      and d.serial == "" and d.busid not in claimed]
        rivals = [k for k, e in entries.items() if k != key and not is_legacy(e)
                  and (e["vid"], e["pid"]) == model and not e.get("serial")
                  and k not in matched.values()]
        if len(candidates) == 1 and not rivals:
            info = candidates[0]
            messages.append(f"{entry.get('name') or info.busid}: moved from port "
                            f"{entry.get('busid')} to {info.busid}")
            entry["busid"] = info.busid
            matched[info.busid] = key
            claimed.add(info.busid)
            renames[key] = entry_key(entry)

    if renames:
        entries = {renames.get(k, k): e for k, e in entries.items()}
        matched = {b: renames.get(k, k) for b, k in matched.items()}
    return entries, matched, messages
