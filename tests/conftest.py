"""Shared test helpers: a config factory and printer-state builders."""

from __future__ import annotations

import time

import pytest

from print_guard.config import Config, from_dict
from print_guard.printer.state import PrinterState


@pytest.fixture
def cfg(tmp_path) -> Config:
    c = from_dict({}, base_dir=tmp_path)
    c.printer.host, c.printer.serial = "192.0.2.10", "01P09TEST000001"
    return c


def make_state(**fields) -> PrinterState:
    raw = {
        "gcode_state": "RUNNING", "stg_cur": 0, "layer_num": 10, "total_layer_num": 100, "mc_percent": 10,
        "nozzle_temper": 220.0, "nozzle_target_temper": 220.0, "bed_temper": 55.0, "bed_target_temper": 55.0,
        "subtask_name": "test_job", "hms": [], "print_error": 0,
    }
    raw.update(fields)
    return PrinterState(raw=raw, received_at=time.time())
