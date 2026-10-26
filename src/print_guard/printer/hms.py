"""Decode the printer's health (HMS) codes and print_error codes into readable,
classified problems.

The printer reports problems two ways in its MQTT status:
  * `hms`: a list of {"attr": int, "code": int}. Joined as hex they form the
    16-digit code Bambu documents, e.g. 0700_2000_0002_0001. The high half of
    `code` is the severity (1 fatal, 2 serious, 3 common, 4 info).
  * `print_error`: one int, shown as 8 hex digits, e.g. 0700_8011.

Descriptions come from the English error database that ha-bambulab (MIT) ships,
bundled as data/hms_en.json.gz. The category is derived from the description so
new codes classify sensibly without a hand-maintained table.
"""

from __future__ import annotations

import functools
import gzip
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from importlib import resources

SEVERITIES = {1: "fatal", 2: "serious", 3: "common", 4: "info"}

# print_error value the printer sets for a few seconds after a user cancels.
PRINT_ERROR_CANCELED = 0x0300400C

# Firmware rejects unsigned commands with these when Developer Mode is off
# ("MQTT command verification failed"). 84033545 means "need reset device pub key".
AUTH_ERROR_CODES = frozenset(range(84033543, 84033549))
HMS_AUTH_FAILED = "0500_0500_0001_0007"


class Category(StrEnum):
    RUNOUT = "runout"
    THERMAL = "thermal"
    EXTRUSION = "extrusion"
    AUTH = "auth"
    CANCELED = "canceled"
    OTHER = "other"


_RUNOUT_RE = re.compile(r"ran out|run out|runout|slot is empty|is empty; please insert|load new filament", re.I)
_AUTOSWITCH_RE = re.compile(r"automatically switched", re.I)
_THERMAL_RE = re.compile(
    r"(temperature|thermistor|heater|heating).*(abnormal|malfunction|too high|over temperature|"
    r"short circuit|open circuit|cannot reach|runaway|full load)",
    re.I,
)
_EXTRUSION_RE = re.compile(r"clog|extrusion|extruding|extruder.*(slip|stuck)|filament may be stuck", re.I)
_AUTH_RE = re.compile(r"verification failed", re.I)


@dataclass(frozen=True)
class Problem:
    """One problem the printer reported, decoded."""

    source: str  # "hms" or "print_error"
    code: str  # e.g. 0700_2000_0002_0001 or 0700_8011
    severity: str  # fatal | serious | common | info (print_error: serious)
    category: Category
    text: str
    auto_recovered: bool = False  # e.g. AMS switched to a backup spool on its own

    @property
    def wiki_url(self) -> str:
        if self.source == "hms":
            return f"https://wiki.bambulab.com/en/x1/troubleshooting/hmscode/{self.code}"
        return "https://e.bambulab.com/query.php?lang=en"

    def summary(self) -> str:
        return f"{self.source.upper()} {self.code} ({self.severity}): {self.text}"


@functools.lru_cache(maxsize=1)
def _database() -> dict:
    data = resources.files("print_guard.data").joinpath("hms_en.json.gz")
    with data.open("rb") as raw, gzip.open(raw, "rt", encoding="utf-8") as f:
        return json.load(f)


def describe(kind: str, code_hex: str, model: str = "P1S") -> str | None:
    """Look up a description. kind is "device_hms" or "device_error"."""
    entry = _database().get(kind, {}).get(code_hex.replace("_", "").upper())
    if not entry:
        return None
    fallback = None
    for text, models in entry.items():
        if model in models:
            return text
        if not models and fallback is None:
            fallback = text
    return fallback or next(iter(entry))


def classify(text: str, code: str) -> Category:
    if code == HMS_AUTH_FAILED or _AUTH_RE.search(text):
        return Category.AUTH
    if _RUNOUT_RE.search(text):
        return Category.RUNOUT
    if _THERMAL_RE.search(text):
        return Category.THERMAL
    if _EXTRUSION_RE.search(text):
        return Category.EXTRUSION
    if re.search(r"cancel", text, re.I) and len(text) < 40:
        return Category.CANCELED
    return Category.OTHER


def hms_code(attr: int, code: int) -> str:
    return f"{attr >> 16:04X}_{attr & 0xFFFF:04X}_{code >> 16:04X}_{code & 0xFFFF:04X}"


def decode_hms(entries: list[dict], model: str = "P1S") -> list[Problem]:
    problems = []
    for entry in entries or []:
        try:
            attr, code = int(entry["attr"]), int(entry["code"])
        except (KeyError, TypeError, ValueError):
            continue
        if attr <= 0 or code <= 0:
            continue
        code_str = hms_code(attr, code)
        severity = SEVERITIES.get(code >> 16, "unknown")
        text = describe("device_hms", code_str, model) or "Unknown HMS code (see the Bambu wiki)"
        category = classify(text, code_str)
        problems.append(
            Problem(
                source="hms",
                code=code_str,
                severity=severity,
                category=category,
                text=text,
                auto_recovered=bool(_AUTOSWITCH_RE.search(text)),
            )
        )
    return problems


def print_error_code(value: int) -> str:
    raw = f"{value & 0xFFFFFFFF:08X}"
    return f"{raw[:4]}_{raw[4:]}"


def decode_print_error(value: int | None, model: str = "P1S") -> Problem | None:
    if not value:
        return None
    code_str = print_error_code(int(value))
    text = describe("device_error", code_str, model) or "Unknown print error"
    category = Category.CANCELED if int(value) == PRINT_ERROR_CANCELED else classify(text, code_str)
    return Problem(source="print_error", code=code_str, severity="serious", category=category, text=text)
