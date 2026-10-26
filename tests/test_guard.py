"""Decision logic of the guard: dry-run vs armed, verification, dedupe and lifecycle."""

from __future__ import annotations

import time

import pytest

from print_guard.guard.actions import ActionExecutor
from print_guard.guard.engine import Guard
from print_guard.guard.events import Action, Finding
from tests.fakes import CapturingNotifier, FakeLink

RUNNING = {"gcode_state": "RUNNING", "stg_cur": 0, "layer_num": 30, "total_layer_num": 200,
           "subtask_name": "mask", "nozzle_temper": 220, "nozzle_target_temper": 220,
           "bed_temper": 55, "bed_target_temper": 55}


def make_guard(cfg, tmp_path, behaviour="obey", armed=True, raw=None):
    cfg.guard.mode = "armed" if armed else "dry-run"
    link = FakeLink(raw or RUNNING, behaviour)
    notifier = CapturingNotifier()
    guard = Guard(cfg, link, notifier, tmp_path)  # type: ignore[arg-type]
    guard.executor = ActionExecutor(link, confirm_timeout=0.5)  # type: ignore[arg-type]
    return guard, link, notifier


def spaghetti() -> Finding:
    return Finding(key="vision:act:pause", source="vision", action=Action.PAUSE, title="Camera: failure",
                   detail="score 1.2")


def test_dry_run_never_sends_commands(cfg, tmp_path):
    guard, link, notifier = make_guard(cfg, tmp_path, armed=False)
    guard.tick(1000.0)
    guard.handle(spaghetti())
    assert link.sent == []
    assert any(t.startswith("[DRY RUN] Would pause") for t in notifier.titles())
    assert guard.dry_run_actions == 1


def test_armed_pause_is_verified(cfg, tmp_path):
    guard, link, notifier = make_guard(cfg, tmp_path)
    guard.tick(1000.0)
    guard.handle(spaghetti())
    guard.wait_for_actions(10)
    assert link.sent == ["pause"]
    assert guard.actions_taken[0].ok
    assert guard.tripped
    assert "Print paused" in notifier.titles()


def test_rejected_command_is_reported_loudly(cfg, tmp_path):
    guard, link, notifier = make_guard(cfg, tmp_path, behaviour="reject")
    guard.handle(spaghetti())
    guard.wait_for_actions(10)
    result = guard.actions_taken[0]
    assert not result.ok and result.rejected_for_auth
    assert link.sent == ["pause"]  # no point retrying a signature rejection
    assert any(n.level == "critical" and n.title.startswith("Could not pause") for n in notifier.notices)
    assert not guard.tripped


def test_unconfirmed_command_is_retried_then_reported(cfg, tmp_path):
    guard, link, _notifier = make_guard(cfg, tmp_path, behaviour="ignore")
    guard.handle(spaghetti())
    guard.wait_for_actions(60)
    assert link.sent == ["pause", "pause"]
    assert link.pushalls >= 1
    assert not guard.actions_taken[0].ok


def test_findings_are_deduplicated(cfg, tmp_path):
    guard, _link, notifier = make_guard(cfg, tmp_path, armed=False)
    f = Finding(key="x", source="t", action=Action.NOTIFY, title="once")
    guard.handle(f, 0)
    guard.handle(f, 10)
    again = Finding(key="y", source="t", action=Action.NOTIFY, title="again", renotify_after=60)
    guard.handle(again, 0)
    guard.handle(again, 30)
    guard.handle(again, 61)
    assert notifier.titles().count("once") == 1
    assert notifier.titles().count("again") == 2


def test_pause_when_already_paused_does_nothing(cfg, tmp_path):
    guard, link, _notifier = make_guard(cfg, tmp_path, raw={**RUNNING, "gcode_state": "PAUSE"})
    guard.handle(spaghetti())
    assert link.sent == []


def test_lifecycle_new_print_and_finish(cfg, tmp_path):
    cfg.guard.exit_when_done = True
    guard, link, notifier = make_guard(cfg, tmp_path, raw={"gcode_state": "IDLE"}, armed=False)
    guard.tick(1.0)
    link.raw.update(RUNNING)
    guard.tick(2.0)
    assert "Print Guard is watching" in notifier.titles()
    link.raw["gcode_state"] = "FINISH"
    guard.tick(3.0)
    assert "Print finished" in notifier.titles()
    assert guard.finished.is_set()


def test_unexpected_self_pause_is_reported(cfg, tmp_path):
    guard, link, notifier = make_guard(cfg, tmp_path, armed=False)
    guard.tick(1.0)
    link.raw.update(gcode_state="PAUSE", stg_cur=35)  # paused: nozzle clog
    guard.tick(2.0)
    assert "Printer paused" in notifier.titles()


def test_developer_mode_off_warns_when_armed(cfg, tmp_path):
    guard, _link, notifier = make_guard(cfg, tmp_path, raw={**RUNNING, "fun": "3EC1AFFF9CFF"})
    guard.tick(1.0)
    assert "Armed, but the printer will refuse commands" in notifier.titles()


def test_escalation_stops_unattended_guard_pause(cfg, tmp_path):
    cfg.guard.stop_after_paused_minutes = 1.0
    guard, link, _notifier = make_guard(cfg, tmp_path)
    guard.tick(1.0)
    guard.handle(spaghetti())
    guard.wait_for_actions(10)
    assert link.raw["gcode_state"] == "PAUSE"
    guard._guard_paused_at -= 61  # pretend a minute passed with nobody touching it
    guard.tick(time.time())
    guard.wait_for_actions(10)
    assert link.sent == ["pause", "stop"]


@pytest.mark.parametrize("behaviour", ["obey"])
def test_offline_alert_and_recovery(cfg, tmp_path, behaviour):
    guard, link, notifier = make_guard(cfg, tmp_path, behaviour)
    link.connected = False
    guard.started_at = 0.0
    link.received_at = 0.0  # nothing heard yet
    guard.tick(1000.0)
    assert "No contact with the printer" in notifier.titles()
    link.connected = True
    link.received_at = 1000.5
    guard.tick(1001.0)
    assert "Printer connection restored" in notifier.titles()


def test_partial_first_report_still_counts_as_attaching_mid_print(cfg, tmp_path):
    # P1 printers may send a delta without gcode_state before the full report arrives.
    guard, link, _notifier = make_guard(cfg, tmp_path, armed=False, raw={"nozzle_temper": 220})
    started = []
    guard._start_print = lambda state, attached: started.append(attached)  # type: ignore[method-assign]
    guard.tick(1.0)
    link.raw.update(RUNNING)  # layer 30 of 200: already under way
    guard.tick(2.0)
    assert started == [True]
