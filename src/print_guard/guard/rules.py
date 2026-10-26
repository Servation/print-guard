"""Checks on the printer's own telemetry: health codes, errors, temperatures,
progress, filament and connectivity.

Each check is evaluated once per tick against a fresh status snapshot and returns
findings. Conditions that must persist (temperature drift, a stall) keep their own
timers so one odd reading never triggers an action. Time is passed in explicitly
so tests can drive the rules deterministically.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from print_guard.config import Config
from print_guard.guard.events import Action, Finding
from print_guard.printer.hms import Category, Problem
from print_guard.printer.state import RUNOUT_STAGES, PrinterState

log = logging.getLogger(__name__)


@dataclass
class _Timer:
    """Tracks how long a condition has held continuously."""

    since: float | None = None
    marker: object = None  # resets the timer when it changes (e.g. a new target temperature)

    def update(self, condition: bool, now: float, marker: object = None) -> float:
        if not condition or (self.since is not None and marker != self.marker):
            self.since = None
        if condition and self.since is None:
            self.since, self.marker = now, marker
        return 0.0 if self.since is None else now - self.since


class TelemetryRules:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.reset()

    def reset(self) -> None:
        """Called at the start of every print."""
        self._problem_first_seen: dict[str, float] = {}
        self._nozzle_over = _Timer()
        self._bed_over = _Timer()
        self._nozzle_under = _Timer()
        self._progress_marker: tuple | None = None
        self._progress_since: float | None = None
        self._runout_since: float | None = None
        self._runouts = 0  # numbers each runout, so a second one in the same print alerts again

    # ---------------------------------------------------------------------------------
    def evaluate(self, state: PrinterState, now: float) -> list[Finding]:
        findings: list[Finding] = []
        if not state.is_active_job:
            return findings
        findings += self._problems(state, now)
        findings += self._runout(state, now)
        if self.cfg.thermal.enabled:
            findings += self._thermal(state, now)
        findings += self._progress(state, now)
        findings += self._filament(state)
        return findings

    # Health codes and print errors ---------------------------------------------------------
    def _action_for(self, p: Problem) -> Action:
        h = self.cfg.hms
        if p.code in h.overrides:
            return Action.parse(h.overrides[p.code])
        if p.category in (Category.RUNOUT, Category.AUTH, Category.CANCELED) or p.auto_recovered:
            return Action.NOTIFY  # handled by dedicated logic below / informational
        if p.severity == "fatal":
            return Action.parse(h.fatal_thermal_action if p.category == Category.THERMAL else h.fatal_action)
        return Action.parse({"serious": h.serious_action, "common": h.common_action,
                             "info": h.info_action}.get(p.severity, h.common_action))

    def _problems(self, state: PrinterState, now: float) -> list[Finding]:
        findings = []
        problems = list(state.hms)
        if (pe := state.print_error) is not None:
            problems.append(pe)
        present = set()
        for p in problems:
            if p.code in self.cfg.hms.ignore or p.category == Category.CANCELED:
                continue
            present.add(p.code)
            first = self._problem_first_seen.setdefault(p.code, now)
            action = self._action_for(p)
            if p.category == Category.AUTH:
                findings.append(Finding(
                    key="auth-rejected", source=p.source, action=Action.NOTIFY, level="critical",
                    title="Printer rejected a control command",
                    detail="Developer Mode is off, so the printer refuses pause/stop from print-guard. "
                           "Enable LAN Only mode + Developer Mode on the printer to let it act.",
                ))
                continue
            if p.category == Category.RUNOUT:
                # Logged here for the record; _runout() owns the notification and the safety net.
                findings.append(Finding(key=f"{p.source}:{p.code}", source=p.source, action=Action.LOG,
                                        title="Printer reports runout", detail=f"{p.code}: {p.text}"))
                continue
            level = "info" if p.auto_recovered or p.severity == "info" else "warning"
            # Always tell the user straight away...
            findings.append(Finding(
                key=f"{p.source}:{p.code}", source=p.source,
                action=Action.LOG if action == Action.LOG else Action.NOTIFY, level=level,
                title=f"Printer reports {p.severity} problem", detail=f"{p.code}: {p.text}",
            ))
            # ...but only act after giving the printer a moment to handle its own error,
            # and only if it is still printing (most errors pause the printer by themselves).
            waited = now - first >= self.cfg.hms.act_after_seconds
            if action >= Action.PAUSE and waited and (action == Action.STOP or state.is_running):
                findings.append(Finding(
                    key=f"{p.source}:{p.code}:act", source=p.source, action=action,
                    title=f"Printer {p.severity} error ({p.category})", detail=f"{p.code}: {p.text}",
                ))
        for code in list(self._problem_first_seen):
            if code not in present:
                del self._problem_first_seen[code]
        return findings

    def _runout(self, state: PrinterState, now: float) -> list[Finding]:
        """The printer pauses itself on runout; make sure it really did."""
        runout = state.stage in RUNOUT_STAGES or any(p.category == Category.RUNOUT for p in state.hms) or (
            (pe := state.print_error) is not None and pe.category == Category.RUNOUT)
        if not runout:
            self._runout_since = None
            return []
        if self._runout_since is None:
            self._runout_since = now
            self._runouts += 1
        findings = [Finding(
            key=f"runout:{self._runouts}", source="filament", action=Action.NOTIFY, level="critical",
            title="Filament ran out: printer is waiting for you",
            detail="Load filament into the same AMS slot (or swap the spool) and resume on the printer. "
                   "print-guard never stops a print for runout.",
            renotify_after=1800,
        )]
        waited = now - self._runout_since
        if state.is_running and waited >= self.cfg.filament.runout_ensure_paused_seconds:
            findings.append(Finding(
                key=f"runout:{self._runouts}:ensure-paused", source="filament", action=Action.PAUSE,
                title="Runout reported but the printer kept printing",
                detail=f"still RUNNING {waited:.0f}s after the runout report; pausing to save the print",
            ))
        return findings

    # Temperatures -------------------------------------------------------------------------
    def _thermal(self, state: PrinterState, now: float) -> list[Finding]:
        t = self.cfg.thermal
        findings = []
        nozzle_over = state.nozzle_target > 0 and state.nozzle_temp > state.nozzle_target + t.nozzle_over_c
        held = self._nozzle_over.update(nozzle_over, now, state.nozzle_target)
        if held >= t.nozzle_over_seconds:
            findings.append(Finding(
                key="thermal:nozzle-over", source="thermal", action=Action.parse(t.over_action),
                title="Nozzle overheating",
                detail=f"{state.nozzle_temp:.0f}°C vs target {state.nozzle_target:.0f}°C for {held:.0f}s",
            ))
        bed_over = state.bed_target > 0 and state.bed_temp > state.bed_target + t.bed_over_c
        held = self._bed_over.update(bed_over, now, state.bed_target)
        if held >= t.bed_over_seconds:
            findings.append(Finding(
                key="thermal:bed-over", source="thermal", action=Action.parse(t.over_action),
                title="Heatbed overheating",
                detail=f"{state.bed_temp:.0f}°C vs target {state.bed_target:.0f}°C for {held:.0f}s",
            ))
        # Under-temperature only matters while laying down plastic: printing too cold
        # starves extrusion. Re-heating after a target change is expected, hence the marker.
        nozzle_under = (state.is_printing_layers and state.nozzle_target >= 150
                        and state.nozzle_temp < state.nozzle_target - t.nozzle_under_c)
        held = self._nozzle_under.update(nozzle_under, now, state.nozzle_target)
        if held >= t.nozzle_under_seconds:
            findings.append(Finding(
                key="thermal:nozzle-under", source="thermal", action=Action.parse(t.under_action),
                title="Nozzle too cold while printing",
                detail=f"{state.nozzle_temp:.0f}°C vs target {state.nozzle_target:.0f}°C for {held:.0f}s "
                       "(heater fault or a blob insulating the nozzle?)",
            ))
        return findings

    # Progress -------------------------------------------------------------------------------
    def _progress(self, state: PrinterState, now: float) -> list[Finding]:
        if not state.is_printing_layers:
            self._progress_marker, self._progress_since = None, None
            return []
        marker = (state.layer, state.percent)
        if marker != self._progress_marker:
            self._progress_marker, self._progress_since = marker, now
            return []
        stalled = 0.0 if self._progress_since is None else now - self._progress_since
        if stalled >= self.cfg.progress.stall_minutes * 60:
            return [Finding(
                key=f"progress:stall:{state.layer}", source="progress",
                action=Action.parse(self.cfg.progress.stall_action),
                title="Print looks stalled",
                detail=f"no layer or percent change for {stalled / 60:.0f} min (layer {state.layer}, "
                       f"{state.percent}%) while the printer says RUNNING",
            )]
        return []

    # Filament ---------------------------------------------------------------------------------
    def _filament(self, state: PrinterState) -> list[Finding]:
        tray = state.active_tray
        low = self.cfg.filament.low_remaining_percent
        if not state.is_running or tray is None or tray.remain < 0 or tray.remain > low:
            return []
        return [Finding(
            key=f"filament:low:{tray.label}", source="filament", action=Action.NOTIFY,
            title=f"Filament low in AMS slot {tray.label}",
            detail=f"{tray.name} about {tray.remain}% left. The printer will pause on runout; "
                   "have the next spool ready.",
        )]
