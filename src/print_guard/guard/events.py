"""Findings: what a check concluded and what the guard should do about it."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path


class Action(IntEnum):
    LOG = 0
    NOTIFY = 1
    PAUSE = 2
    STOP = 3

    @classmethod
    def parse(cls, name: str) -> Action:
        return cls[name.upper()]


@dataclass
class Finding:
    key: str  # dedupe key; a key is acted on once per print unless renotify_after is set
    source: str  # hms, print_error, thermal, progress, filament, vision, judge, connection, printer
    action: Action
    title: str
    detail: str = ""
    level: str = ""  # info | warning | critical; derived from the action when empty
    image: Path | None = None
    renotify_after: float = 0.0  # seconds; 0 = once per print
    at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.level:
            self.level = {Action.LOG: "info", Action.NOTIFY: "warning"}.get(self.action, "critical")

    def describe(self) -> str:
        return f"{self.title}: {self.detail}" if self.detail else self.title
