"""Read-only checks against libvirt for the current, real state of the world.

Nothing here writes state to a file or waits on a filesystem watcher -- every check
asks libvirt directly.
"""

from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET

VIRSH = ["virsh", "--connect", "qemu:///system"]

# How the VM's power state is shown: "on" and "off" follow whether it's running
# or stopped, and "other" covers everything in between (paused, in shutdown,
# pmsuspended, no state) plus anything virsh can't report.
POWER_ON = "on"
POWER_OFF = "off"
POWER_OTHER = "other"

_DOMSTATE_POWER = {
    "running": POWER_ON,
    "idle": POWER_ON,  # guest CPU waiting on something -- still running, not paused
    "shut off": POWER_OFF,
    "crashed": POWER_OFF,
}


def attached_usb_ids(vm_name: str) -> set[tuple[int, int]] | None:
    """(vendor, product) of every USB hostdev in the VM's live XML, or None if the
    XML can't be read (VM not defined, virsh not reachable, ...)."""
    try:
        dump = subprocess.run(
            [*VIRSH, "dumpxml", vm_name],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None

    try:
        root = ET.fromstring(dump)
    except ET.ParseError:
        return None

    ids: set[tuple[int, int]] = set()
    for hostdev in root.findall(".//hostdev[@type='usb']"):
        vendor = hostdev.find("./source/vendor")
        product = hostdev.find("./source/product")
        if vendor is None or product is None:
            continue
        try:
            ids.add((int(vendor.get("id", "0"), 16), int(product.get("id", "0"), 16)))
        except ValueError:
            continue
    return ids


def vm_domstate(vm_name: str) -> str:
    """Returns libvirt's domstate string (e.g. "running", "shut off"), or "unknown"."""
    try:
        result = subprocess.run(
            [*VIRSH, "domstate", vm_name],
            capture_output=True, text=True, check=True, timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def power_of(domstate: str) -> str:
    """Maps a vm_domstate() string to POWER_ON, POWER_OFF or POWER_OTHER."""
    return _DOMSTATE_POWER.get(domstate, POWER_OTHER)


def vm_is_running(vm_name: str) -> bool:
    return vm_domstate(vm_name) == "running"
