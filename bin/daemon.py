#!/usr/bin/env python3
"""Entry point Touch Portal launches (via entry.tp's plugin_start_cmd).

Lifecycle:
  1. Connect + pair with Touch Portal immediately -- no local config file is
     needed or read to do this.
  2. Touch Portal sends the plugin's settings (entry.tp's "settings" array, filled
     in under Settings -> Plugins -> libvirt Bridge) right after pairing, and again
     whenever they're saved. Once "VM Name" and "XML Directory" are both set and
     valid, and whenever a setting changes after that, a full rescan runs: discover
     devices from xml_dir, check each against the VM's live XML, check the VM's
     domstate, push every state. While either is empty, missing or invalid, the
     plugin is idle: no state is updated, VM Power presses are ignored, and every
     state is left exactly as it is (nothing is removed), so buttons pick up where
     they left off once the settings are fixed.
  3. `virsh event --all --loop` runs for the plugin's whole lifetime, and every
     event for the configured VM requests a rescan, so a device attach/detach or VM
     start/stop is noticed immediately no matter what triggered it (a Touch Portal
     button, a raw `virsh` command, or virt-manager) -- never dependent on this
     plugin's own buttons being pressed. It watches every domain rather than just
     the configured one, so changing VM Name doesn't need a new watcher.
  4. Handle the "VM Power" action (start/shutdown/destroy/reboot/toggle), declared
     in entry.tp, by running the corresponding `virsh` command against the
     configured VM -- see lib/vm_control.py.
  5. Handle the "USB Device" action (attach/detach/toggle), also declared in
     entry.tp: its "Device" field is a `choice` list Touch Portal lets the user
     pick from when configuring the button, kept in sync with the XML directory
     via a `choiceUpdate` message sent on every rescan -- the action-data
     equivalent of createState/removeState above.
  6. Exit cleanly on Touch Portal's "closePlugin" message, the connection to it
     dropping, or SIGTERM/SIGINT -- stopping the `virsh event` watcher with it.

This process never writes to, executes, or otherwise modifies anything in the
configured xml_dir -- it only reads the *.xml hostdev descriptors there. The VM
Power and USB Device actions are the only places this plugin changes anything
outside itself; every other code path is read-only.

The one thing that can't come from Touch Portal's own settings UI is the port to
connect to it on in the first place -- that's needed before any pairing happens, so
it's a TP_PORT environment variable (see entry.tp's plugin_start_cmd), not a config
file or a Touch Portal setting.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lib import state_detect, vm_control  # noqa: E402
from lib.devices import discover_devices  # noqa: E402
from lib.tp_client import DEFAULT_PORT, TouchPortalClient  # noqa: E402

LOG_PATH = REPO_ROOT / "daemon.log"
DEBOUNCE_SECONDS = 0.5
VIRSH_EVENT_RETRY_SECONDS = 5
WATCHER_STOP_TIMEOUT_SECONDS = 3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stderr)],
)
log = logging.getLogger("libvirtbridge")

# prctl(PR_SET_PDEATHSIG, SIGTERM), run in the watcher's child process: the kernel
# then terminates it if this process dies without getting to stop it (e.g.
# SIGKILL). Looked up here rather than in the child, which runs it between fork
# and exec, where loading a library isn't safe.
_PR_SET_PDEATHSIG = 1
_prctl = ctypes.CDLL(None, use_errno=True).prctl


def _die_with_parent() -> None:
    _prctl(_PR_SET_PDEATHSIG, int(signal.SIGTERM))


def load_plugin_id() -> str:
    """The plugin's pairing id lives in entry.tp -- that's the only place Touch
    Portal itself reads it from, so it's the single source of truth."""
    entry = json.loads((REPO_ROOT / "entry.tp").read_text())
    return entry["id"]


def device_state_id(device_name: str) -> str:
    return f"libvirtbridge.device.{device_name}"


# The formats Touch Portal's "Background Color (RAW)" accepts: #RGB, #RRGGBB,
# #RGBA, #RRGGBBAA (alpha last).
COLOR_PATTERN = re.compile(r"#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})")
DEFAULT_ATTACHED_COLOR = "#2ECC71"
DEFAULT_DETACHED_COLOR = "#555555"
DEFAULT_OTHER_COLOR = "#F39C12"


def parse_color(value: str, default: str, setting_name: str) -> str:
    value = value.strip()
    if COLOR_PATTERN.fullmatch(value):
        return value
    if value:
        log.warning("%s %r is not a #RGB/#RRGGBB/#RGBA/#RRGGBBAA color -- using %s",
                    setting_name, value, default)
    return default


VM_STATE_ID = "libvirtbridge.vm.state"
VM_POWER_ACTION_ID = "libvirtbridge.action.vmpower"
VM_POWER_OPERATION_FIELD_ID = "libvirtbridge.action.vmpower.operation"
USB_DEVICE_ACTION_ID = "libvirtbridge.action.usbdevice"
USB_DEVICE_DEVICE_FIELD_ID = "libvirtbridge.action.usbdevice.device"
USB_DEVICE_OPERATION_FIELD_ID = "libvirtbridge.action.usbdevice.operation"

# Statically declared in entry.tp's "states" array -- unlike VM_STATE_ID and the
# per-device states below, these don't depend on settings, so they don't need
# runtime creation via create_state. Flipped by TouchPortalClient's
# on_hold_feedback while a hold-enabled button is held past the long-press
# threshold, purely so a button can be wired to show that visually (e.g. Touch
# Portal's Full Size Icon toggle) -- what to show is entirely the user's choice
# in the button editor, not this plugin's concern.
HOLD_FEEDBACK_STATE_IDS = {
    VM_POWER_ACTION_ID: "libvirtbridge.vm.holdactive",
    USB_DEVICE_ACTION_ID: "libvirtbridge.usbdevice.holdactive",
}

# Must match entry.tp's settings[].name exactly -- that field doubles as the id
# Touch Portal uses as the key in its "settings"/"info" messages.
SETTING_VM_NAME = "VM Name"
SETTING_XML_DIR = "XML Directory"
SETTING_ATTACHED_COLOR = "Attached Color"
SETTING_DETACHED_COLOR = "Detached Color"
SETTING_OTHER_COLOR = "Other Color"


@dataclass(frozen=True)
class Settings:
    """One consistent set of settings. Replaced as a whole on every change, so a
    rescan that grabs it once can't see half of an update."""

    vm_name: str = ""
    xml_dir: Path | None = None
    attached_color: str = DEFAULT_ATTACHED_COLOR
    detached_color: str = DEFAULT_DETACHED_COLOR
    other_color: str = DEFAULT_OTHER_COLOR

    def problem(self) -> str | None:
        """Why these settings can't be used, or None if they can. Checked on every
        use rather than once, since a directory can disappear at any time."""
        if not self.vm_name:
            return f"{SETTING_VM_NAME!r} is not set"
        if self.xml_dir is None:
            return f"{SETTING_XML_DIR!r} is not set"
        if not self.xml_dir.is_dir():
            return f"{SETTING_XML_DIR!r} {str(self.xml_dir)!r} is not a directory"
        return None


class Bridge:
    def __init__(self, tp_port: int) -> None:
        self.settings = Settings()
        self.tp = TouchPortalClient(
            plugin_id=load_plugin_id(),
            port=tp_port,
            on_close=self._on_tp_close,
            on_action=self._on_action,
            on_settings=self._on_settings,
            on_hold_feedback=self._on_hold_feedback,
        )
        # Only touched by rescan(), which only ever runs on the rescan worker.
        self._known_states: set[str] = set()
        self._power_lock = threading.Lock()
        self._usb_lock = threading.Lock()
        self._rescan_event = threading.Event()
        self._stop_event = threading.Event()
        self._watcher: subprocess.Popen[str] | None = None

    def stop(self) -> None:
        self._stop_event.set()
        self._rescan_event.set()

    def _on_tp_close(self) -> None:
        log.info("Touch Portal asked the plugin to close.")
        self.stop()

    def _on_settings(self, values: dict[str, str]) -> None:
        xml_dir = values.get(SETTING_XML_DIR, "").strip()
        new = Settings(
            vm_name=values.get(SETTING_VM_NAME, "").strip(),
            xml_dir=Path(xml_dir).expanduser() if xml_dir else None,
            attached_color=parse_color(
                values.get(SETTING_ATTACHED_COLOR, ""), DEFAULT_ATTACHED_COLOR,
                SETTING_ATTACHED_COLOR,
            ),
            detached_color=parse_color(
                values.get(SETTING_DETACHED_COLOR, ""), DEFAULT_DETACHED_COLOR,
                SETTING_DETACHED_COLOR,
            ),
            other_color=parse_color(
                values.get(SETTING_OTHER_COLOR, ""), DEFAULT_OTHER_COLOR,
                SETTING_OTHER_COLOR,
            ),
        )
        changed = new != self.settings
        self.settings = new

        log.info(
            "settings received: VM Name=%r XML Directory=%r Attached Color=%s "
            "Detached Color=%s Other Color=%s",
            new.vm_name, new.xml_dir, new.attached_color, new.detached_color,
            new.other_color,
        )

        problem = new.problem()
        if problem:
            log.warning(
                "%s -- the plugin is idle, leaving every state as it is, until that's "
                "fixed under Touch Portal Settings -> Plugins -> libvirt Bridge.",
                problem,
            )
            return
        if changed:
            self.request_rescan()

    def _on_action(self, action_id: str, data: dict[str, str]) -> None:
        if action_id == VM_POWER_ACTION_ID:
            self._on_power_action(data)
        elif action_id == USB_DEVICE_ACTION_ID:
            self._on_usb_action(data)
        else:
            log.warning("received unknown action id %r", action_id)

    def _on_hold_feedback(self, action_id: str, active: bool) -> None:
        state_id = HOLD_FEEDBACK_STATE_IDS.get(action_id)
        if state_id is not None:
            self.tp.update_state(state_id, "1" if active else "0")

    def _on_power_action(self, data: dict[str, str]) -> None:
        problem = self.settings.problem()
        if problem:
            log.warning("VM Power action ignored -- %s", problem)
            return
        vm_name = self.settings.vm_name

        operation = data.get(VM_POWER_OPERATION_FIELD_ID, "")
        # A shutdown can wait several seconds before resending (see vm_control),
        # and this callback runs on the thread that reads from Touch Portal -- so
        # do the work elsewhere to keep the plugin responsive meanwhile.
        threading.Thread(
            target=self._run_power_action, args=(operation, vm_name), daemon=True
        ).start()

    def _run_power_action(self, operation: str, vm_name: str) -> None:
        # Serialize presses, so a quick double press can't interleave two
        # shutdown/resend sequences.
        with self._power_lock:
            self._run_power_action_locked(operation, vm_name)

    def _run_power_action_locked(self, operation: str, vm_name: str) -> None:
        log.info("VM Power action -> %s %s", operation, vm_name)
        result = vm_control.run_operation(operation, vm_name)
        resolved = (
            f"{operation} -> {result.operation}" if result.operation != operation else operation
        )
        if result.ok:
            log.info("virsh %s %s succeeded: %s", resolved, vm_name, result.message)
        else:
            log.error("virsh %s %s failed: %s", resolved, vm_name, result.message)

        # The virsh event watcher will normally pick up the resulting lifecycle
        # event on its own, but request a rescan directly too so the state
        # reflects the command's outcome without waiting on that event to arrive.
        self.request_rescan()

    def _on_usb_action(self, data: dict[str, str]) -> None:
        problem = self.settings.problem()
        if problem:
            log.warning("USB Device action ignored -- %s", problem)
            return
        operation = data.get(USB_DEVICE_OPERATION_FIELD_ID, "")
        device_name = data.get(USB_DEVICE_DEVICE_FIELD_ID, "")
        # Run off the Touch Portal read thread, same reason as VM Power.
        threading.Thread(
            target=self._run_usb_action, args=(operation, device_name), daemon=True
        ).start()

    def _run_usb_action(self, operation: str, device_name: str) -> None:
        # Serialize presses, so two quick attach/detach presses on different
        # devices can't race each other's virsh calls against the same VM.
        with self._usb_lock:
            self._run_usb_action_locked(operation, device_name)

    def _run_usb_action_locked(self, operation: str, device_name: str) -> None:
        settings = self.settings
        problem = settings.problem()
        if problem:
            log.warning("USB Device action ignored -- %s", problem)
            return

        # Re-read the directory rather than trust a snapshot from the last
        # rescan -- the XML file (and thus the vendor/product ids and path
        # virsh needs) may have changed since the button was configured.
        devices = {device.name: device for device in discover_devices(settings.xml_dir)}
        device = devices.get(device_name)
        if device is None:
            log.error("USB Device action ignored -- unknown device %r", device_name)
            return

        log.info("USB Device action -> %s %s", operation, device.name)
        result = vm_control.run_usb_operation(operation, settings.vm_name, device)
        resolved = (
            f"{operation} -> {result.operation}" if result.operation != operation else operation
        )
        if result.ok:
            log.info("virsh %s %s succeeded: %s", resolved, device.name, result.message)
        else:
            log.error("virsh %s %s failed: %s", resolved, device.name, result.message)

        # The virsh event watcher will normally pick up the resulting hostdev
        # event on its own, but request a rescan directly too so the device's
        # color reflects the command's outcome without waiting on that event.
        self.request_rescan()

    def start(self) -> None:
        log.info("Connecting to Touch Portal at %s:%s", self.tp.host, self.tp.port)
        self.tp.connect_and_pair()

        # The first rescan is requested by _on_settings once Touch Portal has sent
        # the settings, and goes through the worker like every later one.
        threading.Thread(target=self._rescan_worker, daemon=True).start()
        threading.Thread(target=self._virsh_event_loop, daemon=True).start()

    def rescan(self) -> None:
        settings = self.settings
        problem = settings.problem()
        if problem:
            log.info("rescan skipped -- %s", problem)
            return

        domstate = state_detect.vm_domstate(settings.vm_name)
        vm_color = {
            state_detect.POWER_ON: settings.attached_color,
            state_detect.POWER_OFF: settings.detached_color,
        }.get(state_detect.power_of(domstate), settings.other_color)
        self._set_state(VM_STATE_ID, "VM power color", vm_color)
        log.info("VM %s -> %s (%s)", settings.vm_name, domstate, vm_color)

        attached_ids = state_detect.attached_usb_ids(settings.vm_name)
        current: set[str] = set()
        device_names: list[str] = []
        for device in discover_devices(settings.xml_dir):
            state_id = device_state_id(device.name)
            current.add(state_id)
            device_names.append(device.name)
            if attached_ids is None:
                status, color = "unknown", settings.other_color
            elif (device.vendor_id, device.product_id) in attached_ids:
                status, color = "attached", settings.attached_color
            else:
                status, color = "detached", settings.detached_color
            self._set_state(state_id, f"{device.name} color", color)
            log.info("device %s -> %s (%s)", device.name, status, color)

        # A device whose XML file is gone has nothing left to show -- remove its
        # state so Touch Portal's list of states matches the directory.
        for state_id in sorted(self._known_states - current - {VM_STATE_ID}):
            self.tp.remove_state(state_id)
            self._known_states.discard(state_id)
            log.info("removed state %s -- its XML file is gone", state_id)

        # Keeps the USB Device action's "Device" dropdown in sync with the XML
        # directory, the same way createState/removeState above keep the state
        # list in sync -- so a button's action list picks up new/removed devices
        # without needing entry.tp to list them statically.
        self.tp.update_choices(USB_DEVICE_DEVICE_FIELD_ID, sorted(device_names))

    def _set_state(self, state_id: str, description: str, value: str) -> None:
        if state_id not in self._known_states:
            # Created empty on purpose: buttons react to a *change* of this state,
            # so the first real color must always count as one -- otherwise a
            # freshly started Touch Portal would never color them.
            self.tp.create_state(state_id, description, "")
            self._known_states.add(state_id)
        self.tp.update_state(state_id, value)

    def request_rescan(self) -> None:
        self._rescan_event.set()

    def _rescan_worker(self) -> None:
        """Coalesces bursts of virsh events into a single rescan each."""
        while not self._stop_event.is_set():
            self._rescan_event.wait()
            if self._stop_event.is_set():
                return
            self._rescan_event.clear()
            time.sleep(DEBOUNCE_SECONDS)  # let a burst of events settle
            self._rescan_event.clear()
            try:
                self.rescan()
            except Exception:
                log.exception("rescan failed")

    def _virsh_event_loop(self) -> None:
        """Runs `virsh event --all --loop` for every domain and requests a rescan
        on each event for the configured VM.

        Restarts the subprocess with a short backoff if it ever exits (e.g.
        libvirtd restarting).
        """
        cmd = [*state_detect.VIRSH, "event", "--all", "--loop", "--timestamp"]
        while not self._stop_event.is_set():
            log.info("starting virsh event watcher: %s", " ".join(cmd))
            try:
                # PR_SET_PDEATHSIG (see _die_with_parent) fires when the *thread*
                # that started the child exits, not just the process -- fine here,
                # since this thread only moves on once the child has exited.
                self._watcher = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    preexec_fn=_die_with_parent,
                )
                assert self._watcher.stdout is not None
                for line in self._watcher.stdout:
                    self._handle_event_line(line.strip())
                self._watcher.wait()
            except FileNotFoundError:
                log.error("virsh not found -- cannot watch for libvirt events")
                return
            except Exception:
                log.exception("virsh event watcher failed")

            if self._stop_event.is_set():
                return
            log.warning("virsh event watcher exited, restarting in %ss",
                        VIRSH_EVENT_RETRY_SECONDS)
            self._stop_event.wait(VIRSH_EVENT_RETRY_SECONDS)

    def _handle_event_line(self, line: str) -> None:
        if not line:
            return
        vm_name = self.settings.vm_name
        # Every event line names its domain in quotes, followed by either
        # ": <details>" or nothing (e.g. "event 'reboot' for domain 'vm'").
        # Matching the whole quoted name keeps "vm" from matching "vm-2".
        tag = f" for domain '{vm_name}'"
        if vm_name and (line.endswith(tag) or f"{tag}: " in line):
            log.info("virsh event: %s", line)
            self.request_rescan()
        elif " for domain '" not in line:
            log.warning("virsh event watcher: %s", line)  # not an event, e.g. an error

    def _stop_watcher(self) -> None:
        proc = self._watcher
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=WATCHER_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        log.info("stopped virsh event watcher")

    def run_forever(self) -> None:
        self._stop_event.wait()
        self._stop_watcher()
        self.tp.close()


def main() -> None:
    tp_port = int(os.environ.get("TP_PORT", DEFAULT_PORT))
    bridge = Bridge(tp_port)

    def handle_signal(signum, frame):
        log.info("received signal %s, shutting down", signum)
        bridge.stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    bridge.start()
    bridge.run_forever()


if __name__ == "__main__":
    main()
