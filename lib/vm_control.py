"""VM power operations, run via `virsh` on request from a Touch Portal action.

Unlike state_detect.py (read-only, swallows errors into "unknown"/False so one bad
query never breaks a scan), functions here perform a real side effect and must not
hide failures -- the caller needs to know if a `virsh start` actually failed.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass

from . import state_detect
from .devices import Device
from .state_detect import VIRSH

# Maps the value of the action's "operation" choice field (see entry.tp) to the
# virsh subcommand that implements it. "toggle" isn't a real virsh subcommand --
# it's resolved to "shutdown" or "start" in run_operation() below, based on
# whether the VM is currently on or off, so a single button can both show state
# color and act as a power toggle without any logic built in Touch Portal itself.
OPERATIONS = {
    "start": "start",
    "shutdown": "shutdown",   # graceful ACPI shutdown request
    "destroy": "destroy",     # immediate, hard power-off
    "reboot": "reboot",       # graceful ACPI reboot request
}

# At the Windows login screen with the display off, the first ACPI shutdown request
# only wakes the display; a second one while it's on shuts down. So if the VM is
# still running this long after the first request, the request is sent once more.
SHUTDOWN_RESEND_SECONDS = 5
POLL_SECONDS = 0.5


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    message: str
    operation: str  # what actually ran, e.g. "shutdown" if "toggle" resolved to it


def _run_virsh(subcommand: str, vm_name: str, operation: str) -> CommandResult:
    try:
        result = subprocess.run(
            [*VIRSH, subcommand, vm_name],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return CommandResult(ok=False, message=str(exc), operation=operation)

    if result.returncode != 0:
        return CommandResult(ok=False, message=result.stderr.strip() or result.stdout.strip(), operation=operation)
    return CommandResult(ok=True, message=result.stdout.strip(), operation=operation)


def _still_running_after(vm_name: str, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not state_detect.vm_is_running(vm_name):
            return False
        time.sleep(POLL_SECONDS)
    return state_detect.vm_is_running(vm_name)


def run_operation(operation: str, vm_name: str) -> CommandResult:
    """Runs the operation. Blocks for up to SHUTDOWN_RESEND_SECONDS on shutdown,
    so callers should not run this on a thread that must stay responsive."""
    if operation == "toggle":
        domstate = state_detect.vm_domstate(vm_name)
        power = state_detect.power_of(domstate)
        if power == state_detect.POWER_OTHER:
            # Paused, mid-shutdown, suspended or unknown: a paused guest can't act
            # on a shutdown request, one already shutting down doesn't need
            # another, and a start would fail -- so leave it be.
            return CommandResult(
                ok=False,
                message=f"VM is {domstate!r} -- toggle only starts a stopped VM "
                        "or shuts down a running one",
                operation=operation,
            )
        operation = "shutdown" if power == state_detect.POWER_ON else "start"

    subcommand = OPERATIONS.get(operation)
    if subcommand is None:
        return CommandResult(ok=False, message=f"unknown operation: {operation!r}", operation=operation)

    result = _run_virsh(subcommand, vm_name, operation)
    if operation != "shutdown" or not result.ok:
        return result

    if not _still_running_after(vm_name, SHUTDOWN_RESEND_SECONDS):
        return result
    resent = _run_virsh(subcommand, vm_name, operation)
    if not resent.ok and state_detect.vm_is_running(vm_name):
        # A failure only counts if the VM is still up -- it may simply have
        # finished shutting down between the check and the resend.
        return CommandResult(
            ok=False, message=f"resend after {SHUTDOWN_RESEND_SECONDS}s failed: {resent.message}",
            operation=operation,
        )
    return CommandResult(
        ok=True, message=f"{result.message} (resent after {SHUTDOWN_RESEND_SECONDS}s)",
        operation=operation,
    )


# Maps the value of the USB Device action's "operation" choice field to the virsh
# subcommand that implements it. "toggle" isn't a real virsh subcommand -- like VM
# Power's toggle, it's resolved to "attach" or "detach" in run_usb_operation()
# below, based on whether the device is currently attached.
USB_OPERATIONS = {
    "attach": "attach-device",
    "detach": "detach-device",
}


def run_usb_operation(operation: str, vm_name: str, device: Device) -> CommandResult:
    """Attaches or detaches a single hostdev descriptor to/from the VM.

    Uses --live (needs the VM running) together with --config (persists across
    reboots) while the VM is running, so a hot-plug also survives a restart --
    and --config alone while it's off, since --live would fail with no running
    domain to apply to.
    """
    if operation == "toggle":
        attached_ids = state_detect.attached_usb_ids(vm_name)
        is_attached = attached_ids is not None and (device.vendor_id, device.product_id) in attached_ids
        operation = "detach" if is_attached else "attach"

    subcommand = USB_OPERATIONS.get(operation)
    if subcommand is None:
        return CommandResult(ok=False, message=f"unknown operation: {operation!r}", operation=operation)

    flags = ["--live", "--config"] if state_detect.vm_is_running(vm_name) else ["--config"]
    try:
        result = subprocess.run(
            [*VIRSH, subcommand, vm_name, str(device.xml_path), *flags],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return CommandResult(ok=False, message=str(exc), operation=operation)

    if result.returncode != 0:
        return CommandResult(ok=False, message=result.stderr.strip() or result.stdout.strip(), operation=operation)
    return CommandResult(ok=True, message=result.stdout.strip(), operation=operation)
