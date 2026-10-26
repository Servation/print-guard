"""In-memory stand-ins for the printer link and notifier, for fast deterministic tests.

The end-to-end tests (test_e2e_sim.py) use the real network code against the
simulator; these fakes are for checking decision logic in isolation.
"""

from __future__ import annotations

import time

from print_guard.printer.mqtt import CommandReply
from print_guard.printer.state import PrinterState


class FakeLink:
    def __init__(self, raw: dict, behaviour: str = "obey") -> None:
        self.raw = dict(raw)
        self.behaviour = behaviour  # obey | reject | ignore
        self.sent: list[str] = []
        self.connected = True
        self.last_error = ""
        self.pushalls = 0
        self._replies: dict[str, CommandReply] = {}
        self._seq = 0
        self.received_at: float | None = None  # None = "just now"

    def snapshot(self) -> PrinterState:
        at = time.time() if self.received_at is None else self.received_at
        return PrinterState(raw=dict(self.raw), received_at=at)

    def wait_for(self, predicate, timeout: float):
        deadline = time.monotonic() + timeout
        while True:
            state = self.snapshot()
            if predicate(state):
                return state
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.01)

    def pop_reply(self, seq: str):
        return self._replies.pop(seq, None)

    def request_pushall(self, force: bool = False) -> None:
        self.pushalls += 1

    def send_print_command(self, command: str, **fields):
        if not self.connected:
            return None
        self._seq += 1
        seq = str(self._seq)
        self.sent.append(command)
        if self.behaviour == "reject":
            self._replies[seq] = CommandReply(seq, command, "fail", "mqtt message verify failed", 84033543, {})
        elif self.behaviour == "obey":
            self._replies[seq] = CommandReply(seq, command, "success", "", 0, {})
            self.raw["gcode_state"] = {"pause": "PAUSE", "stop": "FAILED", "resume": "RUNNING"}[command]
        return seq


class CapturingNotifier:
    def __init__(self) -> None:
        self.channel_names = ["capture"]
        self.notices = []

    def send(self, notice, wait: bool = False) -> None:
        self.notices.append(notice)

    def titles(self) -> list[str]:
        return [n.title for n in self.notices]
