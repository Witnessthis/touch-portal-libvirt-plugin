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
is scheduled, and nothing fires while the button is still down.

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
    ) -> None:
        self.plugin_id = plugin_id
        self.host = host
        self.port = port
        self._on_close = on_close
        self._on_action = on_action
        self._on_settings = on_settings
        self._sock: socket.socket | None = None
        self._file = None
        self._lock = threading.Lock()
        self._reader_thread: threading.Thread | None = None
        # Long-press deadlines (time.monotonic() seconds), keyed by actionId --
        # Touch Portal's down/up messages carry no per-press instance id, so two
        # buttons sharing the same hold-enabled action held down at once would
        # collide here; not a concern for this plugin's small, fixed action set.
        self._hold_lock = threading.Lock()
        self._hold_deadlines: dict[str, float] = {}

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
        """A hold-enabled button was pressed down -- just records when it'll have
        been held long enough to count as a long press on release."""
        action_id = message.get("actionId", "")
        with self._hold_lock:
            self._hold_deadlines[action_id] = time.monotonic() + LONG_PRESS_SECONDS

    def _maybe_dispatch_long_press(self, message: dict) -> None:
        """The button was released. Fires the action only if it was still down
        past its deadline -- a quick tap (deadline not yet reached) does nothing."""
        action_id = message.get("actionId", "")
        with self._hold_lock:
            deadline = self._hold_deadlines.pop(action_id, None)
        if deadline is not None and time.monotonic() >= deadline:
            self._dispatch_action(message)

    def _dispatch_action(self, message: dict) -> None:
        if self._on_action is None:
            return
        action_id = message.get("actionId", "")
        # Touch Portal sends action data as a list of {"id": ..., "value":
        # ...} pairs (one per entry.tp "data" field) -- flatten to a plain
        # dict keyed by that field's id for convenience.
        data = {
            item["id"]: item.get("value", "")
            for item in message.get("data", [])
            if "id" in item
        }
        self._on_action(action_id, data)

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
