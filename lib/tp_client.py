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

What this plugin wants is a long press: run the action once, on release, but only
if the button was held past LONG_PRESS_SECONDS -- not a hold-to-repeat that fires
while still held. So "down" just records a deadline (now + LONG_PRESS_SECONDS);
"up" fires the action only if that deadline has already passed. No timer/thread
is scheduled for the action itself, and it never fires while the button is still
down.

Separately, on_hold_feedback exists purely so the button can *show* the user the
threshold was reached, while it's still held -- e.g. Touch Portal's Full Size
Icon toggle, wired in the button editor off a state this plugin exposes. That
does need a real timer (there's no other way to notice "still down after
LONG_PRESS_SECONDS" without polling): "down" schedules one, which calls
on_hold_feedback(action_id, data, True) if it fires; "up" always cancels it and
calls on_hold_feedback(action_id, data, False) to reset. This is cosmetic only
-- a tiny race between an "up" arriving right as the timer fires can in
principle leave a stray True after a False, self-correcting on the next press
-- so it's kept separate from the action-dispatch deadline above, which has no
such tolerance.

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

# How long a hold-enabled button must stay down before release counts as a long
# press rather than a tap -- Touch Portal itself enforces no minimum (see the
# module docstring).
LONG_PRESS_SECONDS = 1.0


class TouchPortalClient:
    def __init__(
        self,
        plugin_id: str,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        on_close: Callable[[], None] | None = None,
        on_action: Callable[[str, dict[str, str]], None] | None = None,
        on_settings: Callable[[dict[str, str]], None] | None = None,
        on_hold_feedback: Callable[[str, dict[str, str], bool], None] | None = None,
    ) -> None:
        self.plugin_id = plugin_id
        self.host = host
        self.port = port
        self._on_close = on_close
        self._on_action = on_action
        self._on_settings = on_settings
        self._on_hold_feedback = on_hold_feedback
        self._sock: socket.socket | None = None
        self._file = None
        self._lock = threading.Lock()
        self._reader_thread: threading.Thread | None = None
        # Pending presses, keyed by (actionId, data) -- see the module docstring
        # for why data has to be part of the key.
        self._hold_lock = threading.Lock()
        self._pending_holds: dict[tuple, tuple[float, threading.Timer]] = {}

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
            self._maybe_dispatch_long_press(message)
        elif msg_type in ("settings", "info"):
            if self._on_settings is not None:
                settings = self._extract_settings(message)
                if settings:
                    self._on_settings(settings)

    def _mark_press_down(self, message: dict) -> None:
        """A hold-enabled button was pressed down: records when it'll have been
        held long enough to count as a long press on release, and schedules the
        feedback timer that fires if it's still down at that point."""
        action_id = message.get("actionId", "")
        data = self._flatten_data(message)
        key = self._hold_key(action_id, data)
        deadline = time.monotonic() + LONG_PRESS_SECONDS
        timer = threading.Timer(
            LONG_PRESS_SECONDS, self._fire_hold_feedback, args=(key, action_id, data)
        )
        timer.daemon = True
        with self._hold_lock:
            # Replace rather than stack, in case a stray "down" ever arrives
            # without a matching "up" first.
            stale = self._pending_holds.pop(key, None)
            self._pending_holds[key] = (deadline, timer)
        if stale is not None:
            stale[1].cancel()
        timer.start()

    def _fire_hold_feedback(self, key: tuple, action_id: str, data: dict[str, str]) -> None:
        """Runs on the timer's own thread, LONG_PRESS_SECONDS after "down" -- only
        signals feedback if the button's still down (nothing popped it first)."""
        with self._hold_lock:
            if key not in self._pending_holds:
                return  # already released -- "up" got there first
        if self._on_hold_feedback is not None:
            self._on_hold_feedback(action_id, data, True)

    def _maybe_dispatch_long_press(self, message: dict) -> None:
        """The button was released: cancels the feedback timer and resets its
        signal, then fires the action only if it was still down past its
        deadline -- a quick tap (deadline not yet reached) does nothing."""
        action_id = message.get("actionId", "")
        data = self._flatten_data(message)
        key = self._hold_key(action_id, data)
        with self._hold_lock:
            pending = self._pending_holds.pop(key, None)
        if pending is None:
            return
        deadline, timer = pending
        timer.cancel()
        if self._on_hold_feedback is not None:
            self._on_hold_feedback(action_id, data, False)
        if time.monotonic() >= deadline:
            self._dispatch_action(message)

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
