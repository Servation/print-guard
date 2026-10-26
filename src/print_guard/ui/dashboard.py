"""Live terminal dashboard: printer status, check results and the latest log lines.

Rendering only reads snapshots, so a slow terminal can never stall the guard.
The log panel shrinks to fit the window, so the status panel and the help line
stay visible even in a short terminal.
"""

from __future__ import annotations

import logging
import time

from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from print_guard import __version__
from print_guard.config import Config
from print_guard.credentials import mask_serial
from print_guard.guard.engine import Guard
from print_guard.logging_setup import TailHandler
from print_guard.printer.camera import CameraStream
from print_guard.printer.mqtt import PrinterLink
from print_guard.printer.state import PrinterState, control_status

LEVEL_STYLE = {"info": "cyan", "warning": "yellow", "critical": "bold red"}
VISION_STYLE = {"ok": "green", "warmup": "cyan", "warning": "yellow", "failure": "bold red"}
USER_PAUSE_STAGE = 16
MAX_FINDINGS = 6


def _age(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds == float("inf") or seconds > 86400 * 30:
        return "never"
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    return f"{seconds / 60:.0f}m ago"


def _swatch(color: str) -> Text:
    hexcolor = (color or "")[:6]
    if len(hexcolor) == 6:
        return Text("■", style=f"#{hexcolor}")
    return Text("□")


def _gauge(value: float, width: int = 18) -> Text:
    """Bar where the first third is fine, the middle third warning, the last third failure."""
    filled = round(max(0.0, min(1.0, value)) * width)
    style = "green" if value < 1 / 3 else ("yellow" if value < 2 / 3 else "bold red")
    return Text("[") + Text("█" * filled, style=style) + Text("░" * (width - filled), style="grey37") + Text("]")


def _line(text: Text) -> Text:
    text.no_wrap = True
    text.overflow = "ellipsis"
    return text


class Dashboard:
    def __init__(self, cfg: Config, link: PrinterLink, guard: Guard, tail: TailHandler,
                 camera: CameraStream | None, vision) -> None:
        self.cfg, self.link, self.guard, self.tail = cfg, link, guard, tail
        self.camera, self.vision = camera, vision

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        yield self.render(console, options)

    # Sections -----------------------------------------------------------------------------------
    def _status_rows(self, state: PrinterState, now: float) -> list[tuple[str, Text]]:
        rows: list[tuple[str, Text]] = []
        if self.link.connected:
            link = Text("connected", style="green")
        elif not getattr(self.link, "ever_connected", True) and not self.link.last_error:
            link = Text("connecting...", style="yellow")
        else:
            link = Text("DISCONNECTED", style="bold red")
        link.append(f"  last status {_age(state.age(now))}", style="grey58")
        if self.link.last_error:
            link.append(f"  ({self.link.last_error})", style="red")
        rows.append(("Printer link", link))

        if self.camera is None:
            rows.append(("Camera", Text("off", style="grey58")))
        else:
            frame = self.camera.latest()
            ok = self.camera.connected and frame is not None and now - frame.at < 30
            cam = Text("streaming" if ok else "no frames", style="green" if ok else "yellow")
            if frame:
                cam.append(f"  last frame {_age(now - frame.at)}", style="grey58")
            if self.camera.last_error and not ok:
                cam.append(f"  ({self.camera.last_error})", style="red")
            rows.append(("Camera", cam))

        accepted, reason = control_status(state, self.link.announced_mode)
        if accepted is True:
            rows.append(("Control", Text(f"pause/stop available: {reason}", style="green")))
        elif accepted is False:
            rows.append(("Control", Text(f"pause/stop REFUSED, alerts only: {reason}", style="bold red")))
        else:
            rows.append(("Control", Text(f"unknown: {reason}", style="grey58")))

        if not state.received_at:
            return rows
        stage = state.stage_name
        if state.is_paused and self.guard.tripped and state.stage == USER_PAUSE_STAGE:
            stage = "paused by print-guard"
        job = Text(state.gcode_state, style="bold")
        job.append(f"  {stage}")
        if state.job_name:
            job.append(f"  {state.job_name}", style="cyan")
        rows.append(("Job", job))
        if state.is_active_job:
            rem = state.remaining_minutes
            rows.append(("Progress", Text(f"{state.percent}%  layer {state.layer}/{state.total_layers}  "
                                          f"{rem // 60}h{rem % 60:02d}m left")))
        rows.append(("Temps", Text(f"nozzle {state.nozzle_temp:.0f}/{state.nozzle_target:.0f}°C   "
                                   f"bed {state.bed_temp:.0f}/{state.bed_target:.0f}°C")))
        if state.trays:
            trays = Text()
            active = state.active_tray
            for t in state.trays:
                is_active = active is not None and t.label == active.label
                remain = f"{t.remain}%" if t.remain >= 0 else "?%"
                trays.append_text(_swatch(t.color))
                trays.append(f" {t.label} {t.name} {remain}" + (" ◀" if is_active else ""),
                             style="bold" if is_active else "")
                trays.append("   ")
            rows.append(("AMS", trays))
        for p in state.hms[:2]:
            rows.append(("HMS", Text(p.summary(), style="yellow")))
        return rows

    def _vision_rows(self) -> list[tuple[str, Text]]:
        if self.vision is None:
            return [("Vision", Text("off", style="grey58"))]
        v = self.vision.status
        vt = Text(v.level, style=VISION_STYLE.get(v.level, ""))
        vt.append("  ")
        vt.append_text(_gauge(v.gauge))
        vt.append(f"  frame {v.current_p:.2f}, normal {v.base:.2f}, excess {v.adjusted:+.2f}  {v.state}",
                  style="grey70")
        judge = Text(v.judge, style="green" if v.judge == "on" else "grey58")
        if v.judge == "on":
            judge.append(f"  spent ${v.judge_cost:.2f}", style="grey58")
            last = self.vision.judge.last_verdict if self.vision.judge else None
            if last:
                judge.append(f"  last: {last.summary}", style="grey70")
        return [("Vision", vt), ("Claude judge", judge)]

    # Layout --------------------------------------------------------------------------------------------
    def render(self, console: Console, options: ConsoleOptions) -> Group:
        """Lay out the panels; the log panel gets whatever height the others leave.

        Heights are measured, not assumed, because status lines wrap in a narrow window
        (the app's Terminal panel can be ~45 columns) instead of being cut off."""
        height = options.max_height or console.size.height
        measure = options.update(height=None)

        def rows_of(renderable) -> int:
            return len(console.render_lines(renderable, measure, pad=False))

        now = time.time()
        state = self.link.snapshot()
        if self.cfg.armed:
            mode = Text(" ARMED ", style="bold white on red")
        else:
            mode = Text(" DRY RUN ", style="bold black on yellow")
        # The mode badge comes first so it stays visible when a narrow window cuts the title.
        title = Text.assemble(mode, f" print-guard {__version__}  {self.cfg.printer.name} "
                                    f"{mask_serial(self.cfg.printer.serial)} @ {self.cfg.printer.host}")
        rows = self._status_rows(state, now) + self._vision_rows()
        if self.guard.tripped:
            guard_text = Text("TRIPPED: print-guard paused this print", style="bold red")
        else:
            guard_text = Text("watching", style="green")
        rows.append(("Guard", guard_text))

        info = Table.grid(padding=(0, 2))
        info.add_column(style="bold", no_wrap=True, min_width=max(len(name) for name, _ in rows))
        info.add_column(overflow="fold")
        for name, value in rows:
            info.add_row(name, value)
        parts: list = [Panel(info, title=title, title_align="left", border_style="blue")]

        recent = list(self.guard.recent)[-MAX_FINDINGS:]
        if recent:
            events = Table.grid()
            events.add_column(overflow="ellipsis", no_wrap=True)
            for entry in recent:
                events.add_row(_line(Text(time.strftime("%H:%M:%S ", time.localtime(entry.at)), style="grey58")
                                     + Text(entry.text, style=LEVEL_STYLE.get(entry.level, ""))))
            parts.append(Panel(events, title="Findings and actions", title_align="left", border_style="magenta"))

        footer = Text("Ctrl+C to stop watching. latest.jpg in the run folder shows what the camera sees.",
                      style="grey50")
        used = sum(rows_of(part) for part in parts) + rows_of(footer)
        # Whatever height is left (minus the log panel's two border rows and a spare) is for the log.
        log_rows = max(3, height - used - 3)
        log_lines = Table.grid()
        log_lines.add_column(overflow="ellipsis", no_wrap=True)
        for levelno, line in self.tail.tail(log_rows):
            style = "red" if levelno >= logging.ERROR else ("yellow" if levelno >= logging.WARNING else "grey70")
            log_lines.add_row(_line(Text(line, style=style)))
        parts.append(Panel(log_lines, title=f"Checks (full log in {self.guard.run_dir})", title_align="left",
                           border_style="grey37"))
        parts.append(footer)
        return Group(*parts)
