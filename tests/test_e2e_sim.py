"""End to end against the fake printer: real TLS sockets, real MQTT client, real camera
stream, real detector. Each test runs a short simulated print and checks that the
guard did the right thing to the "printer" (and nothing more)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from print_guard import runtime
from print_guard.cli import sim_config, simulate
from print_guard.config import from_dict
from print_guard.sim.printer import FakePrinter, SimSettings
from tests.fakes import CapturingNotifier

MODEL = Path(__file__).resolve().parents[1] / "models" / "obico-failure-detector.onnx"
FAST = {"total_layers": 40, "seconds_per_layer": 1.0, "failure_layer": 12, "prepare_seconds": 2.0}


@pytest.fixture
def base(tmp_path):
    cfg = from_dict({}, base_dir=Path(__file__).resolve().parents[1])
    cfg.guard.runs_dir = str(tmp_path / "runs")
    return cfg


def run(base, tmp_path, scenario, **kwargs):
    return simulate(scenario, base, plain=True, desktop=False, timeout=150, runs_dir=tmp_path / "runs",
                    sim_overrides=FAST, **kwargs)


@pytest.mark.skipif(not MODEL.exists(), reason="needs the detector model")
def test_spaghetti_is_paused(base, tmp_path):
    r = run(base, tmp_path, "spaghetti")
    assert r.passed, r.summary
    assert r.fake.commands == ["pause"]
    assert r.fake.state["layer_num"] < FAST["total_layers"]  # stopped wasting filament early
    snapshots = list((tmp_path / "runs").rglob("snapshots/*vision-failure*.jpg"))
    assert snapshots, "the failure frame should be saved for the notification"


@pytest.mark.skipif(not MODEL.exists(), reason="needs the detector model")
def test_spaghetti_dry_run_sends_nothing(base, tmp_path):
    r = run(base, tmp_path, "spaghetti", armed=False)
    assert r.passed, r.summary
    assert r.fake.commands == []
    assert r.guard.dry_run_actions >= 1


@pytest.mark.skipif(not MODEL.exists(), reason="needs the detector model")
def test_locked_printer_rejection_is_reported(base, tmp_path):
    r = run(base, tmp_path, "locked")
    assert r.passed, r.summary
    assert r.fake.rejected == ["pause"]
    assert r.guard.actions_taken and r.guard.actions_taken[0].rejected_for_auth


def _wait(condition, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return False


@pytest.mark.skipif(not MODEL.exists(), reason="needs the detector model")
def test_failure_after_resume_is_paused_again(base, tmp_path):
    # You clear the mess and resume; a few layers later the print fails again. The guard must
    # re-arm (calibrating on the cleaned-up look) and catch the second failure too.
    settings = SimSettings(scenario="spaghetti", access_code="simtest1", **{**FAST, "total_layers": 120})
    fake = FakePrinter(settings, tmp_path / "sim")
    fake.start()
    cfg = sim_config(base, fake, "spaghetti", armed=True, judge=False, desktop=False, ntfy=False,
                     runs_dir=tmp_path / "runs")
    cfg.guard.rearm_grace_minutes, cfg.guard.exit_when_done = 0.1, False
    rt = runtime.build(cfg, "simtest1", runtime.new_run_dir(cfg, "resume"), judge=False,
                       notifier=CapturingNotifier(), persist_baseline=False)  # type: ignore[arg-type]
    stop = threading.Event()
    rt.start()
    loop = threading.Thread(target=rt.run, args=(stop,), daemon=True)
    loop.start()
    try:
        assert _wait(lambda: fake.commands == ["pause"] and rt.guard.tripped, 90), fake.commands
        fake.user_resume(clear_mess=True, fail_again_after_layers=20)
        assert _wait(lambda: fake.commands == ["pause", "pause"], 90), fake.commands
        assert _wait(lambda: fake.state["gcode_state"] == "PAUSE", 10)
        assert "Print resumed: guard re-armed" in rt.notifier.titles()  # type: ignore[attr-defined]
    finally:
        stop.set()
        loop.join(10)
        rt.stop()
        fake.stop()


def test_runout_is_left_to_the_printer(base, tmp_path):
    r = run(base, tmp_path, "runout")
    assert r.passed, r.summary
    assert r.fake.commands == []


def test_overtemp_is_stopped(base, tmp_path):
    r = run(base, tmp_path, "overtemp")
    assert r.passed, r.summary
    assert r.fake.commands == ["stop"]


def test_wrong_access_code_is_rejected_and_reported(base, tmp_path, monkeypatch):
    # The guard must not "see" a printer it couldn't log in to.
    import print_guard.cli as cli

    real_watch = cli.watch

    def watch_with_bad_code(cfg, access_code, **kw):
        return real_watch(cfg, "wrongcode", **kw)

    monkeypatch.setattr(cli, "watch", watch_with_bad_code)
    r = simulate("clean", base, plain=True, desktop=False, timeout=8, runs_dir=tmp_path / "runs",
                 sim_overrides=FAST)
    assert not r.passed
    assert r.fake.logins_rejected >= 1
    assert r.fake.commands == []
