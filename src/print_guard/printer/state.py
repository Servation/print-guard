"""The printer's status, merged from its MQTT reports into one immutable snapshot.

P1-series printers send a full report only when asked (`pushall`) and small
deltas otherwise, so the raw dictionary is merged message by message and the
guard reads typed snapshots built from it.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field
from typing import Any

from print_guard.printer.hms import Problem, decode_hms, decode_print_error

# Stage ids reported in `stg_cur` (subset that matters on a P1S; ids from ha-bambulab, MIT).
STAGES = {
    -1: "idle",
    0: "printing",
    1: "auto bed leveling",
    2: "heatbed preheating",
    3: "vibration compensation",
    4: "changing filament",
    5: "M400 pause",
    6: "paused: filament runout",
    7: "heating hotend",
    8: "calibrating extrusion",
    9: "scanning bed surface",
    10: "inspecting first layer",
    11: "identifying build plate",
    13: "homing toolhead",
    14: "cleaning nozzle tip",
    15: "checking extruder temperature",
    16: "paused by user",
    17: "paused: front cover fell",
    19: "calibrating extrusion flow",
    20: "paused: nozzle temperature malfunction",
    21: "paused: heatbed temperature malfunction",
    22: "unloading filament",
    23: "paused: skipped step",
    24: "loading filament",
    25: "calibrating motor noise",
    26: "paused: AMS lost",
    27: "paused: heatbreak fan speed low",
    28: "paused: chamber temperature control error",
    29: "cooling chamber",
    30: "paused by G-code",
    31: "motor noise showoff",
    32: "paused: nozzle covered with filament",
    33: "paused: cutter error",
    34: "paused: first layer error",
    35: "paused: nozzle clog",
    255: "idle",
}
# Stages in which the printer has paused itself because something went wrong.
SELF_PAUSE_STAGES = {6, 17, 20, 21, 23, 26, 27, 28, 32, 33, 34, 35}
RUNOUT_STAGES = {6}
ACTIVE_STATES = {"PREPARE", "RUNNING", "PAUSE", "SLICING", "INIT"}
DONE_STATES = {"FINISH", "FAILED", "IDLE"}


def deep_merge(base: dict, delta: dict) -> dict:
    """Merge a report delta into the accumulated state, in place.

    Lists of objects with an "id" (AMS units and trays) are merged by id, because
    deltas may carry only the trays that changed. Other lists are replaced.
    """
    for key, value in delta.items():
        current = base.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            deep_merge(current, value)
        elif (
            isinstance(value, list)
            and isinstance(current, list)
            and value
            and all(isinstance(v, dict) and "id" in v for v in value)
            and all(isinstance(c, dict) and "id" in c for c in current)
        ):
            by_id = {c["id"]: c for c in current}
            for item in value:
                if item["id"] in by_id:
                    deep_merge(by_id[item["id"]], item)
                else:
                    current.append(copy.deepcopy(item))
        else:
            base[key] = copy.deepcopy(value)
    return base


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return default


@dataclass(frozen=True)
class Tray:
    ams: int
    slot: int
    material: str
    color: str  # RRGGBBAA
    remain: int  # percent, -1 when the AMS doesn't know (non-RFID spool)
    name: str

    @property
    def label(self) -> str:
        return f"{chr(ord('A') + self.ams)}{self.slot + 1}"

    @property
    def global_index(self) -> int:
        return self.ams * 4 + self.slot


@dataclass(frozen=True)
class PrinterState:
    raw: dict = field(repr=False)
    received_at: float = 0.0
    model: str = "P1S"

    # Basic job info -------------------------------------------------------
    @property
    def gcode_state(self) -> str:
        return str(self.raw.get("gcode_state") or "UNKNOWN").upper()

    @property
    def stage(self) -> int:
        return _int(self.raw.get("stg_cur"), -1)

    @property
    def stage_name(self) -> str:
        return STAGES.get(self.stage, f"stage {self.stage}")

    @property
    def job_name(self) -> str:
        return str(self.raw.get("subtask_name") or self.raw.get("gcode_file") or "")

    @property
    def percent(self) -> int:
        return _int(self.raw.get("mc_percent"))

    @property
    def remaining_minutes(self) -> int:
        return _int(self.raw.get("mc_remaining_time"))

    @property
    def layer(self) -> int:
        return _int(self.raw.get("layer_num"))

    @property
    def total_layers(self) -> int:
        return _int(self.raw.get("total_layer_num"))

    @property
    def is_running(self) -> bool:
        return self.gcode_state == "RUNNING"

    @property
    def is_paused(self) -> bool:
        return self.gcode_state == "PAUSE"

    @property
    def is_active_job(self) -> bool:
        return self.gcode_state in ACTIVE_STATES

    @property
    def is_printing_layers(self) -> bool:
        """Actually laying down the model (not homing, leveling or purging)."""
        return self.is_running and self.stage in (0, -1, 255) and self.layer > 0

    # Temperatures ---------------------------------------------------------
    @property
    def nozzle_temp(self) -> float:
        return _num(self.raw.get("nozzle_temper"))

    @property
    def nozzle_target(self) -> float:
        return _num(self.raw.get("nozzle_target_temper"))

    @property
    def bed_temp(self) -> float:
        return _num(self.raw.get("bed_temper"))

    @property
    def bed_target(self) -> float:
        return _num(self.raw.get("bed_target_temper"))

    @property
    def chamber_temp(self) -> float:
        return _num(self.raw.get("chamber_temper"))

    # Problems -------------------------------------------------------------
    @property
    def hms(self) -> list[Problem]:
        return decode_hms(self.raw.get("hms") or [], self.model)

    @property
    def print_error(self) -> Problem | None:
        return decode_print_error(_int(self.raw.get("print_error")), self.model)

    # AMS --------------------------------------------------------------------
    @property
    def trays(self) -> list[Tray]:
        trays = []
        ams = self.raw.get("ams") or {}
        for unit in ams.get("ams") or []:
            ams_id = _int(unit.get("id"))
            for t in unit.get("tray") or []:
                if not t.get("tray_type"):
                    continue  # empty slot
                trays.append(
                    Tray(
                        ams=ams_id,
                        slot=_int(t.get("id")),
                        material=str(t.get("tray_type") or "?"),
                        color=str(t.get("tray_color") or ""),
                        remain=_int(t.get("remain"), -1),
                        name=str(t.get("tray_sub_brands") or t.get("tray_type") or ""),
                    )
                )
        return trays

    @property
    def active_tray(self) -> Tray | None:
        """The AMS tray feeding the extruder (None for the external spool or unknown)."""
        ams = self.raw.get("ams") or {}
        now = _int(ams.get("tray_now"), 255)
        if now >= 254:  # 254 external spool, 255 none
            return None
        for t in self.trays:
            if t.global_index == now:
                return t
        return None

    # Capabilities -----------------------------------------------------------
    @property
    def developer_mode(self) -> bool | None:
        """True if the printer accepts unsigned control commands (Developer Mode on).

        Firmware with Bambu's authorization control reports it in `fun`: bit 29
        set means commands must be signed, i.e. third-party pause/stop is refused.
        None when the printer doesn't report the field.
        """
        fun = self.raw.get("fun")
        if not fun:
            return None
        try:
            bits = int(str(fun), 16)
        except ValueError:
            return None
        return not bits & (1 << 29)

    @property
    def firmware(self) -> str:
        return str(self.raw.get("_firmware") or "")

    @property
    def chamber_light_on(self) -> bool | None:
        for light in self.raw.get("lights_report") or []:
            if light.get("node") == "chamber_light":
                return light.get("mode") == "on"
        return None

    def age(self, now: float | None = None) -> float:
        return (now or time.time()) - self.received_at if self.received_at else float("inf")


def control_status(state: PrinterState, announced_mode: str | None) -> tuple[bool | None, str]:
    """Will the printer accept pause/stop from print-guard? (answer, reason)

    P1-series status reports may not include the Developer Mode bit, but the printer's
    network announcement says whether LAN Only mode is on, and Developer Mode can only
    be enabled in LAN Only mode, so cloud mode means commands will be refused."""
    if state.developer_mode is True:
        return True, "Developer Mode on"
    if state.developer_mode is False:
        return False, "Developer Mode off"
    if announced_mode == "cloud":
        return False, "printer is in cloud mode (LAN Only off), so Developer Mode is off"
    if announced_mode == "lan":
        return None, "LAN Only mode is on; Developer Mode is not reported, the first command will tell"
    return None, "not reported; the first real command will tell"
