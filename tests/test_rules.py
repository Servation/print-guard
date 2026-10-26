"""Telemetry rules: thresholds must persist before acting, and runout is never 'fixed' by stopping."""

from __future__ import annotations

from print_guard.guard.events import Action
from print_guard.guard.rules import TelemetryRules
from tests.conftest import make_state


def keys(findings):
    return {f.key: f.action for f in findings}


def test_nothing_when_idle_or_healthy(cfg):
    rules = TelemetryRules(cfg)
    assert rules.evaluate(make_state(gcode_state="IDLE", nozzle_temper=300.0), 0) == []
    assert rules.evaluate(make_state(), 0) == []


def test_nozzle_overtemp_must_persist_then_stops(cfg):
    rules = TelemetryRules(cfg)
    hot = make_state(nozzle_temper=240.0)
    assert "thermal:nozzle-over" not in keys(rules.evaluate(hot, 0))
    assert "thermal:nozzle-over" not in keys(rules.evaluate(hot, 19))
    assert keys(rules.evaluate(hot, 21))["thermal:nozzle-over"] == Action.STOP


def test_target_change_resets_thermal_timer(cfg):
    rules = TelemetryRules(cfg)
    rules.evaluate(make_state(nozzle_temper=240.0), 0)
    # Target raised mid-way (e.g. a filament change): the timer starts over.
    assert "thermal:nozzle-over" not in keys(rules.evaluate(make_state(nozzle_temper=240.0,
                                                                       nozzle_target_temper=222.0), 25))


def test_cold_nozzle_only_counts_while_printing_layers(cfg):
    rules = TelemetryRules(cfg)
    cold = dict(nozzle_temper=180.0)
    rules.evaluate(make_state(stg_cur=7, **cold), 0)  # heating hotend: expected to be cold
    assert "thermal:nozzle-under" not in keys(rules.evaluate(make_state(stg_cur=7, **cold), 500))
    rules.evaluate(make_state(**cold), 600)
    assert keys(rules.evaluate(make_state(**cold), 721))["thermal:nozzle-under"] == Action.PAUSE


def test_runout_alerts_but_never_stops(cfg):
    rules = TelemetryRules(cfg)
    paused = make_state(gcode_state="PAUSE", stg_cur=6, print_error=0x07008011,
                        hms=[{"attr": 0x07002000, "code": 0x00020001}])
    found = keys(rules.evaluate(paused, 0)) | keys(rules.evaluate(paused, 1000))
    assert found["runout:1"] == Action.NOTIFY
    assert not any(a >= Action.PAUSE for a in found.values())


def test_runout_safety_net_pauses_if_printer_keeps_running(cfg):
    rules = TelemetryRules(cfg)
    running = make_state(hms=[{"attr": 0x07002000, "code": 0x00020001}])
    assert "runout:1:ensure-paused" not in keys(rules.evaluate(running, 0))
    assert keys(rules.evaluate(running, 61))["runout:1:ensure-paused"] == Action.PAUSE


def test_second_runout_in_same_print_alerts_again(cfg):
    rules = TelemetryRules(cfg)
    out = make_state(gcode_state="PAUSE", stg_cur=6)
    assert "runout:1" in keys(rules.evaluate(out, 0))
    rules.evaluate(make_state(), 100)  # reloaded and resumed
    assert "runout:2" in keys(rules.evaluate(out, 200))


def test_serious_hms_acts_only_if_printer_keeps_running(cfg):
    rules = TelemetryRules(cfg)
    hms = [{"attr": 0x03000900, "code": 0x00020001}]  # extruder overloaded / clog
    first = keys(rules.evaluate(make_state(hms=hms), 0))
    assert first["hms:0300_0900_0002_0001"] == Action.NOTIFY
    assert "hms:0300_0900_0002_0001:act" not in first
    assert keys(rules.evaluate(make_state(hms=hms), 21))["hms:0300_0900_0002_0001:act"] == Action.PAUSE
    rules.reset()
    rules.evaluate(make_state(gcode_state="PAUSE", hms=hms), 0)
    assert "hms:0300_0900_0002_0001:act" not in keys(rules.evaluate(make_state(gcode_state="PAUSE", hms=hms), 30))


def test_fatal_thermal_hms_stops_even_when_paused(cfg):
    rules = TelemetryRules(cfg)
    hms = [{"attr": 0x03000100, "code": 0x00010001}]
    rules.evaluate(make_state(gcode_state="PAUSE", hms=hms), 0)
    assert keys(rules.evaluate(make_state(gcode_state="PAUSE", hms=hms), 21))[
        "hms:0300_0100_0001_0001:act"] == Action.STOP


def test_ignore_and_override(cfg):
    cfg.hms.ignore = ["0300_0900_0002_0001"]
    cfg.hms.overrides = {"0300_0100_0001_0001": "notify"}
    rules = TelemetryRules(cfg)
    state = make_state(hms=[{"attr": 0x03000900, "code": 0x00020001}, {"attr": 0x03000100, "code": 0x00010001}])
    found = keys(rules.evaluate(state, 0)) | keys(rules.evaluate(state, 100))
    assert not any(k.startswith("hms:0300_0900") for k in found)
    assert "hms:0300_0100_0001_0001:act" not in found


def test_stall(cfg):
    rules = TelemetryRules(cfg)
    rules.evaluate(make_state(layer_num=40), 0)
    assert not [f for f in rules.evaluate(make_state(layer_num=40), 1700) if f.source == "progress"]
    stalled = [f for f in rules.evaluate(make_state(layer_num=40), 1801) if f.source == "progress"]
    assert stalled and stalled[0].action == Action.NOTIFY


def test_low_filament_warning(cfg):
    rules = TelemetryRules(cfg)
    ams = {"ams": [{"id": "0", "tray": [{"id": "0", "tray_type": "PLA", "remain": 4}]}], "tray_now": "0"}
    assert "filament:low:A1" in keys(rules.evaluate(make_state(ams=ams), 0))
    ams["ams"][0]["tray"][0]["remain"] = -1  # unknown (non-RFID spool): no guess
    assert "filament:low:A1" not in keys(rules.evaluate(make_state(ams=ams), 0))


def test_auth_rejection_is_explained(cfg):
    rules = TelemetryRules(cfg)
    found = rules.evaluate(make_state(hms=[{"attr": 0x05000500, "code": 0x00010007}]), 0)
    auth = [f for f in found if f.key == "auth-rejected"]
    assert auth and "Developer Mode" in auth[0].detail
