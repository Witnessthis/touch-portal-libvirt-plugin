"""Minimal Touch Portal plugin socket client.

Touch Portal's plugin API is a plain TCP socket carrying newline-delimited JSON
(no websocket framing). This client implements the handful of message types this
plugin actually needs: pairing, dynamically creating and removing states at runtime
(createState/removeState -- no entry.tp "states" array needed), pushing values
(stateUpdate), keeping an action's "choice" data field in sync with the same
dynamic device list (choiceUpdate), receiving button-triggered actions declared in
entry.tp's "actions" array -- both a plain press ("action") and, for an action whose
entry.tp definition includes "hasHoldFunctionality", a button being held ("down")
and released again ("up") -- receiving the user-configured values of entry.tp's
"settings" fields (both the initial "info" message sent right after pairing, and
the "settings" message sent whenever they're changed and saved in the app), and
reacting to Touch Portal telling us to shut down (closePlugin).

Touch Portal sends "down" the instant a hold-enabled button is physically pressed
and "up" the instant it's released -- confirmed against the official API docs
(https://www.touch-portal.com/api/index.php?section=communication_listen_action_hold_info):
"When the user presses the Touch Portal button down, Touch Portal will send the
'down' event. When the user releases the button, Touch Portal will send the 'up'
event." There's no built-in minimum hold duration -- a plain tap sends "down"
immediately followed by "up", same as a genuine hold.

This client stays deliberately agnostic about what "held long enough" means for any
given action: "up" just reports how long the button was down (on_hold_release) --
whether that counts as significant, and what to do about it, is entirely up to the
caller (see bin/daemon.py's Before/After Threshold actions, which compare it against
a per-action, user-configured threshold rather than a fixed constant here).

Separately, on_hold_feedback exists purely so a button can *show* the user a
threshold was reached while still held -- e.g. Touch Portal's Full Size Icon
toggle, wired in the button editor off a state the plugin exposes. Unlike
on_hold_release (reported after the fact, on "up"), this needs a real timer, since
there's no other way to notice "still down after N ms" without polling: "down"
schedules one for any data field whose id ends in THRESHOLD_FIELD_SUFFIX, which
calls on_hold_feedback(action_id, data, True) if it fires; "up" always cancels it
and calls on_hold_feedback(action_id, data, False) to reset. This is cosmetic only
-- a tiny race between an "up" arriving right as the timer fires can in principle
leave a stray True after a False, self-correcting on the next press. Whether a
given action's feedback signal is actually wired to anything is up to the caller
(bin/daemon.py only acts on it for its After Threshold actions).

Pending presses are keyed by (actionId, data) rather than actionId alone --
several buttons can share one hold-enabled action (e.g. this plugin's VM Power
across several VMs, or USB Device across several devices), distinguished only
by their configured data values, which "down"/"up" echo back unchanged. Keying
on actionId alone would let one button's "down" clobber another's pending
state if both were held at once.

Message shapes are as sent by Touch Portal 4.6.1. The port can be overridden with
the TP_PORT environment variable (see entry.tp's plugin_start_cmd).
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from collections.abc import Callable

log = logging.getLogger("libvirtbridge")

DEFAULT_PORT = 12136
DEFAULT_HOST = "127.0.0.1"

# Convention shared with bin/daemon.py's entry.tp field ids: any data field whose
# id ends with this is treated as this action instance's feedback threshold, in
# milliseconds -- see the module docstring's on_hold_feedback paragraph.
THRESHOLD_FIELD_SUFFIX = ".threshold"


class TouchPortalClient:
    def __init__(
        self,
        plugin_id: str,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        on_close: Callable[[], None] | None = None,
        on_action: Callable[[str, dict[str, str]], None] | None = None,
        on_settings: Callable[[dict[str, str]], None] | None = None,
        on_hold_release: Callable[[str, dict[str, str], float], None] | None = None,
        on_hold_feedback: Callable[[str, dict[str, str], bool], None] | None = None,
    ) -> None:
        self.plugin_id = plugin_id
        self.host = host
        self.port = port
        self._on_close = on_close
        self._on_action = on_action
        self._on_settings = on_settings
        self._on_hold_release = on_hold_release
        self._on_hold_feedback = on_hold_feedback
        self._sock: socket.socket | None = None
        self._file = None
        self._lock = threading.Lock()
        self._reader_thread: threading.Thread | None = None
        # Pending presses, keyed by (actionId, data) -- see the module docstring
        # for why data has to be part of the key. Value is (press time, feedback
        # timer or None if this data had no ...threshold field).
        self._hold_lock = threading.Lock()
        self._pending_holds: dict[tuple, tuple[float, threading.Timer | None]] = {}

    def connect_and_pair(self) -> None:
        self._sock = socket.create_connection((self.host, self.port), timeout=10)
        self._sock.settimeout(None)
        self._file = self._sock.makefile("r", encoding="utf-8")
        self._send({"type": "pair", "id": self.plugin_id})

        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def _send(self, message: dict) -> None:
        assert self._sock is not None
        line = json.dumps(message) + "\n"
        with self._lock:
            self._sock.sendall(line.encode("utf-8"))

    def _read_loop(self) -> None:
        assert self._file is not None
        try:
            for line in self._file:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._handle_message(message)
        except OSError:
            pass  # socket closed
        finally:
            if self._on_close is not None:
                self._on_close()

    def _handle_message(self, message: dict) -> None:
        msg_type = message.get("type")
        # Debug only: Touch Portal broadcasts carry device IPs, device names and
        # page names, which shouldn't end up in a log users share.
        log.debug("received from Touch Portal: %s", json.dumps(message))
        if msg_type == "closePlugin":
            if self._on_close is not None:
                self._on_close()
        elif msg_type == "action":
            self._dispatch_action(message)
        elif msg_type == "down":
            self._mark_press_down(message)
        elif msg_type == "up":
            self._report_hold_release(message)
        elif msg_type in ("settings", "info"):
            if self._on_settings is not None:
                settings = self._extract_settings(message)
                if settings:
                    self._on_settings(settings)

    def _mark_press_down(self, message: dict) -> None:
        """A hold-enabled button was pressed down: records when this press
        started (for on_hold_release, reported on "up"), and, if its data
        includes a "...threshold" field, schedules the feedback timer that
        fires if it's still down once that many milliseconds have passed."""
        action_id = message.get("actionId", "")
        data = self._flatten_data(message)
        key = self._hold_key(action_id, data)
        press_time = time.monotonic()

        feedback_timer = None
        threshold_ms = self._extract_threshold_ms(data)
        if threshold_ms is not None:
            feedback_timer = threading.Timer(
                threshold_ms / 1000.0, self._fire_hold_feedback, args=(key, action_id, data)
            )
            feedback_timer.daemon = True

        with self._hold_lock:
            # Replace rather than stack, in case a stray "down" ever arrives
            # without a matching "up" first.
            stale = self._pending_holds.pop(key, None)
            self._pending_holds[key] = (press_time, feedback_timer)
        if stale is not None and stale[1] is not None:
            stale[1].cancel()
        if feedback_timer is not None:
            feedback_timer.start()

    def _fire_hold_feedback(self, key: tuple, action_id: str, data: dict[str, str]) -> None:
        """Runs on the timer's own thread, once this data's threshold has passed
        after "down" -- only signals feedback if the button's still down
        (nothing popped it first)."""
        with self._hold_lock:
            if key not in self._pending_holds:
                return  # already released -- "up" got there first
        if self._on_hold_feedback is not None:
            self._on_hold_feedback(action_id, data, True)

    def _report_hold_release(self, message: dict) -> None:
        """The button was released: cancels the feedback timer and resets its
        signal, then reports how long it was held -- deciding what that means
        is entirely up to on_hold_release's caller."""
        action_id = message.get("actionId", "")
        data = self._flatten_data(message)
        key = self._hold_key(action_id, data)
        with self._hold_lock:
            pending = self._pending_holds.pop(key, None)
        if pending is None:
            return
        press_time, feedback_timer = pending
        if feedback_timer is not None:
            feedback_timer.cancel()
        if self._on_hold_feedback is not None:
            self._on_hold_feedback(action_id, data, False)
        if self._on_hold_release is not None:
            self._on_hold_release(action_id, data, time.monotonic() - press_time)

    @staticmethod
    def _extract_threshold_ms(data: dict[str, str]) -> float | None:
        for field_id, value in data.items():
            if field_id.endswith(THRESHOLD_FIELD_SUFFIX):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return None
        return None

    @staticmethod
    def _flatten_data(message: dict) -> dict[str, str]:
        # Touch Portal sends action data as a list of {"id": ..., "value":
        # ...} pairs (one per entry.tp "data" field) -- flatten to a plain
        # dict keyed by that field's id for convenience.
        return {
            item["id"]: item.get("value", "")
            for item in message.get("data", [])
            if "id" in item
        }

    @staticmethod
    def _hold_key(action_id: str, data: dict[str, str]) -> tuple:
        return (action_id, tuple(sorted(data.items())))

    def _dispatch_action(self, message: dict) -> None:
        if self._on_action is None:
            return
        action_id = message.get("actionId", "")
        self._on_action(action_id, self._flatten_data(message))

    @staticmethod
    def _extract_settings(message: dict) -> dict[str, str]:
        """Flattens Touch Portal's settings representation into a plain dict.

        Both messages use a list of single-key dicts, one per setting,
        `[{"<name>": "<value>"}, ...]` -- under "values" in the "settings"
        message sent on save, and under "settings" in the "info" message sent
        right after pairing (confirmed against Touch Portal 4.6.1).
        """
        for key in ("values", "settings"):
            items = message.get(key)
            if isinstance(items, list):
                result: dict[str, str] = {}
                for item in items:
                    if isinstance(item, dict):
                        result.update(item)
                return result
        return {}

    def create_state(self, state_id: str, description: str, default_value: str = "") -> None:
        """Create a state at runtime -- no static entry.tp declaration required."""
        self._send(
            {
                "type": "createState",
                "id": state_id,
                "desc": description,
                "defaultValue": default_value,
            }
        )

    def remove_state(self, state_id: str) -> None:
        self._send({"type": "removeState", "id": state_id})

    def update_state(self, state_id: str, value: str) -> None:
        self._send({"type": "stateUpdate", "id": state_id, "value": value})

    def update_choices(self, field_id: str, choices: list[str]) -> None:
        """Replaces the available values of an action's "choice" data field
        declared in entry.tp -- the action-data equivalent of create_state/
        remove_state for dynamic state ids."""
        self._send({"type": "choiceUpdate", "id": field_id, "value": choices})

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
