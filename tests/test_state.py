"""Merging P1-style delta reports and reading typed state from them."""

from __future__ import annotations

from print_guard.printer.state import PrinterState, deep_merge
from tests.conftest import make_state


def test_deep_merge_merges_ams_trays_by_id():
    base = {"ams": {"ams": [{"id": "0", "tray": [{"id": "0", "remain": 50, "tray_type": "PLA"},
                                                {"id": "1", "remain": 80, "tray_type": "PLA"}]}],
                    "tray_now": "0"}, "layer_num": 3}
    deep_merge(base, {"ams": {"ams": [{"id": "0", "tray": [{"id": "1", "remain": 79}]}]}, "layer_num": 4})
    trays = base["ams"]["ams"][0]["tray"]
    assert trays[0]["remain"] == 50 and trays[1]["remain"] == 79 and trays[1]["tray_type"] == "PLA"
    assert base["layer_num"] == 4
    assert base["ams"]["tray_now"] == "0"


def test_deep_merge_replaces_plain_lists():
    base = {"hms": [{"attr": 1, "code": 2}]}
    deep_merge(base, {"hms": []})
    assert base["hms"] == []


def test_developer_mode_bit():
    assert make_state(fun="3EC18FFF9CFF").developer_mode is True
    assert make_state(fun="3EC1AFFF9CFF").developer_mode is False
    assert make_state().developer_mode is None
    assert make_state(fun="zz").developer_mode is None


def test_active_tray_and_labels():
    st = make_state(ams={"ams": [{"id": "0", "tray": [
        {"id": "0", "tray_type": "PLA", "tray_color": "FFFFFFFF", "remain": 7, "tray_sub_brands": "PLA Basic"},
        {"id": "1"}, {"id": "2", "tray_type": "PETG", "remain": -1}]}], "tray_now": "2"})
    assert [t.label for t in st.trays] == ["A1", "A3"]
    assert st.active_tray is not None and st.active_tray.material == "PETG"
    assert make_state(ams={"tray_now": "254"}).active_tray is None


def test_printing_layers_excludes_prepare_phases():
    assert make_state().is_printing_layers
    assert not make_state(stg_cur=1).is_printing_layers  # bed leveling
    assert not make_state(gcode_state="PAUSE").is_printing_layers
    assert not make_state(layer_num=0).is_printing_layers


def test_defaults_on_empty_report():
    st = PrinterState(raw={})
    assert st.gcode_state == "UNKNOWN" and not st.is_active_job and st.hms == [] and st.print_error is None


def test_control_status_uses_the_announced_mode_when_the_bit_is_missing():
    from print_guard.printer.state import control_status

    assert control_status(make_state(), "cloud")[0] is False  # Developer Mode needs LAN Only mode
    assert control_status(make_state(), "lan")[0] is None
    assert control_status(make_state(), None)[0] is None
    assert control_status(make_state(fun="3EC18FFF9CFF"), "cloud")[0] is True  # the printer's own bit wins
