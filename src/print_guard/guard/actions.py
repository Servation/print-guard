"""Pausing and stopping the printer, with proof that it worked.

A published command is not proof of anything: the printer may be offline, may
reject unsigned commands (Developer Mode off), or may miss the message. So each
action waits for the printer's own status to change, retries once, and reports
a clear failure otherwise so the user can be told to act by hand.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from print_guard.printer.hms import AUTH_ERROR_CODES, HMS_AUTH_FAILED
from print_guard.printer.mqtt import PrinterLink
from print_guard.printer.state import DONE_STATES, PrinterState

log = logging.getLogger(__name__)


@dataclass
class ActionResult:
    command: str
    ok: bool
    message: str
    rejected_for_auth: bool = False


def _goal(command: str):
    if command == "pause":
        return lambda s: s.is_paused or s.gcode_state in DONE_STATES
    if command == "stop":
        return lambda s: s.gcode_state in DONE_STATES
    if command == "resume":
        return lambda s: s.is_running
    raise ValueError(command)


class ActionExecutor:
    def __init__(self, link: PrinterLink, confirm_timeout: float = 30.0, attempts: int = 2) -> None:
        self.link = link
        self.confirm_timeout = confirm_timeout
        self.attempts = attempts

    def run(self, command: str) -> ActionResult:
        goal = _goal(command)
        if goal(self.link.snapshot()):
            return ActionResult(command, True, f"printer already in the target state for {command}")
        last_problem = "printer did not confirm"
        for attempt in range(1, self.attempts + 1):
            seq = self.link.send_print_command(command)
            if seq is None:
                last_problem = "not connected to the printer"
                time.sleep(3)
                continue
            log.info("sent %s (attempt %d, seq %s); waiting for the printer to confirm", command, attempt, seq)
            outcome = self._await(command, seq, goal, self.confirm_timeout)
            if isinstance(outcome, ActionResult):
                return outcome
            if outcome is not None:
                return ActionResult(command, True, f"printer confirmed: {outcome.gcode_state}")
            # No reply and no state change: ask for a full report in case a delta was missed.
            self.link.request_pushall(force=True)
            state = self.link.wait_for(goal, min(10.0, self.confirm_timeout))
            if state is not None:
                return ActionResult(command, True, f"printer confirmed: {state.gcode_state}")
        return ActionResult(command, False, f"{last_problem} after {self.attempts} attempts")

    def _await(self, command: str, seq: str, goal, timeout: float) -> PrinterState | ActionResult | None:
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            state = self.link.wait_for(goal, min(2.0, remaining))
            if state is not None:
                return state
            reply = self.link.pop_reply(seq)
            if reply is not None and not reply.ok:
                auth = reply.err_code in AUTH_ERROR_CODES or "verif" in reply.reason.lower()
                why = ("the printer requires signed commands: enable LAN Only mode + Developer Mode"
                       if auth else f"result={reply.result!r} reason={reply.reason!r}")
                return ActionResult(command, False, f"printer rejected {command}: {why} "
                                    f"(err_code {reply.err_code})", rejected_for_auth=auth)
            if any(p.code == HMS_AUTH_FAILED for p in self.link.snapshot().hms):
                return ActionResult(command, False, "printer rejected the command (HMS 0500_0500_0001_0007: "
                                    "MQTT command verification failed). Enable Developer Mode.",
                                    rejected_for_auth=True)
        return None
