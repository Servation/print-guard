"""The guard's main loop: follow the print's lifecycle, run the checks, act on findings.

Every finding is logged and journaled. What happens next depends on its action
and the mode: notifications always go out; pause/stop are only sent when the
guard is armed, otherwise you get a "[DRY RUN] would pause" alert instead. Each
finding key is handled once per print (or again after its renotify interval),
so a persistent condition doesn't spam you.
"""

from __future__ import annotations

import collections
import json
import logging
import queue
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from print_guard.config import Config
from print_guard.credentials import redact
from print_guard.guard.actions import ActionExecutor, ActionResult
from print_guard.guard.events import Action, Finding
from print_guard.guard.rules import TelemetryRules
from print_guard.notify import Notice, Notifier
from print_guard.printer.camera import CameraStream
from print_guard.printer.mqtt import PrinterLink
from print_guard.printer.state import DONE_STATES, RUNOUT_STAGES, PrinterState, control_status

log = logging.getLogger(__name__)

TICK_SECONDS = 1.0
USER_PAUSE_STAGE = 16


@dataclass
class Entry:
    at: float
    level: str
    text: str


class Journal:
    """Machine-readable record of every finding and action (events.jsonl in the run folder)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def write(self, kind: str, **data) -> None:
        record = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "kind": kind, **data}
        line = redact(json.dumps(record, default=str, ensure_ascii=False))
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


class Guard:
    def __init__(self, cfg: Config, link: PrinterLink, notifier: Notifier, run_dir: Path,
                 camera: CameraStream | None = None, vision=None) -> None:
        self.cfg = cfg
        self.link = link
        self.camera = camera
        self.vision = vision
        self.notifier = notifier
        self.run_dir = run_dir
        self.rules = TelemetryRules(cfg)
        self.executor = ActionExecutor(link)
        self.journal = Journal(run_dir / "events.jsonl")
        self.findings: queue.Queue[Finding] = queue.Queue()
        self.recent: collections.deque[Entry] = collections.deque(maxlen=40)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="action")
        self._action_futures: list[Future] = []
        self._handled: dict[str, float] = {}
        self._prev: PrinterState | None = None
        self._job_active = False
        self._job_name = ""
        self._guard_paused_at: float | None = None
        self._expect_pause_until = 0.0
        self._offline_alerted = False
        self._dev_mode_checked = False
        self._paused_count = 0
        self._resumes = 0
        self.finished = threading.Event()
        self.started_at = time.time()
        self.last_tick = 0.0
        self.actions_taken: list[ActionResult] = []
        self.dry_run_actions = 0

    # Thread-safe entry point for other threads (vision) ------------------------------------------
    def emit(self, finding: Finding) -> None:
        self.findings.put(finding)

    @property
    def tripped(self) -> bool:
        return self._guard_paused_at is not None

    # Main loop ---------------------------------------------------------------------------------
    def run(self, stop: threading.Event, on_tick=None) -> None:
        mode = "ARMED (will pause/stop)" if self.cfg.armed else "DRY RUN (alerts only)"
        log.info("guard running in %s mode", mode)
        self.journal.write("start", mode=self.cfg.guard.mode)
        while not stop.is_set() and not self.finished.is_set():
            try:
                self.tick(time.time())
            except Exception:
                log.exception("guard tick failed; continuing")
            if on_tick:
                on_tick()
            stop.wait(TICK_SECONDS)
        self.journal.write("stop")

    def tick(self, now: float) -> None:
        self.last_tick = now
        state = self.link.snapshot()
        self._check_connection(state, now)
        if state.received_at:
            self._lifecycle(state, now)
            self._check_control(state)
            for finding in self.rules.evaluate(state, now):
                self.handle(finding, now)
            self._escalate(state, now)
            self._prev = state
        while True:
            try:
                finding = self.findings.get_nowait()
            except queue.Empty:
                break
            self.handle(finding, now)
        self._action_futures = [f for f in self._action_futures if not f.done()]

    # Lifecycle -----------------------------------------------------------------------------------
    def _lifecycle(self, state: PrinterState, now: float) -> None:
        if state.gcode_state == "UNKNOWN":
            # P1 printers send partial updates; until a report says what the printer is doing,
            # there is no lifecycle to follow (and no "UNKNOWN -> RUNNING" print start).
            return
        prev = self._prev if self._prev is not None and self._prev.gcode_state != "UNKNOWN" else None
        active = state.is_active_job
        if active and (not self._job_active or (state.job_name and state.job_name != self._job_name)):
            # A print we see start is at layer 0 or 1; anything later was already under way.
            self._start_print(state, attached=state.layer > 1)
        self._job_active = active
        # A resume after the guard's pause is recognised from any report newer than the confirmed
        # pause, not only from a PAUSE -> RUNNING pair: a quick resume can fall between two ticks.
        paused_at = self._guard_paused_at
        if paused_at is not None and state.is_running and state.received_at > paused_at:
            self._on_resumed(now)
        if prev is None:
            return
        old, new = prev.gcode_state, state.gcode_state
        if old == new:
            return
        stage = state.stage_name
        if new == "PAUSE" and state.stage == USER_PAUSE_STAGE and now <= self._expect_pause_until:
            stage = "paused by print-guard"  # the printer reports our pause as a user pause
        log.info("printer state %s -> %s (%s)", old, new, stage)
        self.journal.write("state", old=old, new=new, stage=state.stage_name, layer=state.layer)
        if new == "PAUSE" and old != "PAUSE":
            self._paused_count += 1
            # Runout has its own, louder alert; a pause we sent ourselves is reported by the action.
            if now > self._expect_pause_until and state.stage not in RUNOUT_STAGES:
                problems = "; ".join(p.text for p in state.hms[:2]) or state.stage_name
                self.handle(Finding(key=f"printer:paused:{self._paused_count}", source="printer",
                                    action=Action.NOTIFY, level="info" if state.stage == USER_PAUSE_STAGE else "",
                                    title="Printer paused", detail=problems), now)
        elif new == "FINISH":
            self.handle(Finding(key="printer:finished", source="printer", action=Action.NOTIFY, level="info",
                                title="Print finished", detail=self._summary()), now)
            self._done()
        elif new == "FAILED":
            pe = state.print_error
            detail = pe.text if pe else "stopped or failed"
            self.handle(Finding(key="printer:failed", source="printer", action=Action.NOTIFY,
                                title="Print ended without finishing", detail=detail), now)
            self._done()
        elif new == "IDLE" and old in ("RUNNING", "PAUSE", "PREPARE"):
            self._done()

    def _on_resumed(self, now: float) -> None:
        """You resumed a print the guard paused: re-arm, after a grace period for the cleanup."""
        self._guard_paused_at = None
        self._resumes += 1
        grace = self.cfg.guard.rearm_grace_minutes * 60
        self.handle(Finding(key=f"guard:rearmed:{self._resumes}", source="guard", action=Action.NOTIFY,
                            level="info", title="Print resumed: guard re-armed",
                            detail=f"camera checks resume after a {grace / 60:.0f} min grace period"), now)
        if self.vision:
            self.vision.rearm(grace)

    def _start_print(self, state: PrinterState, attached: bool) -> None:
        self._job_name = state.job_name
        self._handled.clear()
        self._guard_paused_at = None
        self.rules.reset()
        if self.vision:
            self.vision.new_print(attached=attached)
        how = f"attached mid-print at layer {state.layer}/{state.total_layers}" if attached else "print starting"
        log.info("watching job %r (%s)", state.job_name, how)
        self.journal.write("job", name=state.job_name, attached=attached, layer=state.layer)
        self._note("info", f"Watching {state.job_name or 'print'} ({how})")
        self.notifier.send(Notice("info", "Print Guard is watching",
                                  f"{state.job_name or 'Current print'}: {how}. Mode: {self.cfg.guard.mode}."))

    def _done(self) -> None:
        self._guard_paused_at = None
        if self.cfg.guard.exit_when_done:
            log.info("job over and exit_when_done is set; exiting")
            self.finished.set()

    def _summary(self) -> str:
        acted = len(self.actions_taken)
        return (f"{len(self._handled)} distinct findings, {acted} actions"
                + (f", {self.dry_run_actions} dry-run actions" if self.dry_run_actions else ""))

    # Health of our own view of the printer ------------------------------------------------------------
    def _check_connection(self, state: PrinterState, now: float) -> None:
        silent = now - (state.received_at or self.started_at)
        offline = (not self.link.connected or (self._job_active and silent > 30)) and \
            silent > self.cfg.connection.offline_alert_seconds
        if offline and not self._offline_alerted:
            self._offline_alerted = True
            why = self.link.last_error or "no status received"
            self.handle(Finding(key="connection:offline", source="connection", action=Action.NOTIFY,
                                title="No contact with the printer",
                                detail=f"no status for {silent:.0f}s ({why}). The guard cannot see or stop the "
                                       "print until it reconnects.", renotify_after=1800), now)
        elif not offline and self._offline_alerted and self.link.connected:
            self._offline_alerted = False
            self._handled.pop("connection:offline", None)
            self.handle(Finding(key=f"connection:back:{int(now)}", source="connection", action=Action.NOTIFY,
                                level="info", title="Printer connection restored"), now)
        if self.camera and self.cfg.vision.enabled and state.is_printing_layers:
            frame = self.camera.latest()
            age = now - (frame.at if frame else self.started_at)
            if age > self.cfg.connection.camera_alert_seconds:
                self.handle(Finding(key="connection:camera", source="connection", action=Action.NOTIFY,
                                    title="Camera feed lost",
                                    detail=f"no frame for {age:.0f}s ({self.camera.last_error or 'unknown'}); "
                                           "spaghetti detection is blind", renotify_after=3600), now)

    def _check_control(self, state: PrinterState) -> None:
        if self._dev_mode_checked:
            return
        accepted, reason = control_status(state, getattr(self.link, "announced_mode", None))
        if accepted is None:
            return
        self._dev_mode_checked = True
        if accepted:
            log.info("%s: pause/stop commands are accepted", reason)
        else:
            log.warning("%s: the printer will reject pause/stop from print-guard", reason)
            if self.cfg.armed:
                self.handle(Finding(key="control:unavailable", source="guard", action=Action.NOTIFY,
                                    level="critical", title="Armed, but the printer will refuse commands",
                                    detail=f"{reason}, so print-guard can only alert you. Enable LAN Only mode + "
                                           "Developer Mode on the printer to allow automatic pausing."),
                            time.time())

    def _escalate(self, state: PrinterState, now: float) -> None:
        limit = self.cfg.guard.stop_after_paused_minutes
        if limit and self._guard_paused_at and state.is_paused and now - self._guard_paused_at > limit * 60:
            self.handle(Finding(key="guard:escalate-stop", source="guard", action=Action.STOP,
                                title="Paused print left unattended",
                                detail=f"paused by print-guard {limit:.0f} min ago and not resumed; stopping it "
                                       "to turn the heaters off"), now)

    # Findings ------------------------------------------------------------------------------------------
    def handle(self, f: Finding, now: float | None = None) -> None:
        now = time.time() if now is None else now
        last = self._handled.get(f.key)
        if last is not None and (not f.renotify_after or now - last < f.renotify_after):
            return
        self._handled[f.key] = now
        log_level = logging.WARNING if f.action >= Action.PAUSE or f.level == "critical" else logging.INFO
        verb = f.action.name if f.action < Action.PAUSE or self.cfg.armed else f"DRY-RUN {f.action.name}"
        log.log(log_level, "%s [%s/%s] %s", verb, f.source, f.level, f.describe())
        self.journal.write("finding", key=f.key, source=f.source, action=f.action.name, level=f.level,
                           title=f.title, detail=f.detail, image=str(f.image) if f.image else None)
        if f.action == Action.LOG:
            self._note("info", f.describe())
            return
        image = f.image or (self._snapshot(f.key) if f.level != "info" else None)
        if f.action == Action.NOTIFY:
            self._note(f.level, f.describe())
            self.notifier.send(Notice(f.level, f.title, f.detail, image))
            return
        self._act(f, image)

    def _act(self, f: Finding, image: Path | None) -> None:
        command = "pause" if f.action == Action.PAUSE else "stop"
        state = self.link.snapshot()
        already = state.gcode_state in DONE_STATES or (command == "pause" and state.is_paused)
        if already:
            self._note("warning", f"{f.describe()} (printer already {state.gcode_state.lower()})")
            self.notifier.send(Notice("warning", f.title, f"{f.detail}\nPrinter is already "
                                      f"{state.gcode_state.lower()}; nothing to do.", image))
            return
        if not self.cfg.armed:
            self.dry_run_actions += 1
            self._note("critical", f"[DRY RUN] would {command}: {f.describe()}")
            self.journal.write("dry_run", command=command, key=f.key)
            self.notifier.send(Notice("critical", f"[DRY RUN] Would {command} the print: {f.title}",
                                      f"{f.detail}\nDry-run mode: nothing was sent to the printer.", image))
            return
        self._note("critical", f"{command.upper()} sent | {f.describe()}")
        self.notifier.send(Notice("critical", f"{f.title}: {'pausing' if command == 'pause' else 'stopping'} "
                                  "the print", f.detail, image))
        if command == "pause":
            self._expect_pause_until = time.time() + 120
        self._action_futures.append(self._pool.submit(self._run_action, command, f))

    def _run_action(self, command: str, f: Finding) -> ActionResult:
        result = self.executor.run(command)
        self.actions_taken.append(result)
        self.journal.write("action", command=command, ok=result.ok, message=result.message, key=f.key)
        if result.ok:
            if command == "pause":
                self._guard_paused_at = time.time()
            log.warning("%s confirmed: %s", command, result.message)
            self._note("critical", f"{command} confirmed by the printer")
            body = (f"{f.title}. Check the print, then resume or stop it on the printer."
                    if command == "pause" else f"{f.title}. The print was cancelled; heaters are off.")
            self.notifier.send(Notice("warning", f"Print {'paused' if command == 'pause' else 'stopped'}", body))
        else:
            log.error("%s FAILED: %s", command, result.message)
            self._note("critical", f"{command} FAILED: {result.message}")
            self.notifier.send(Notice("critical", f"Could not {command} the print!",
                                      f"{result.message}\nGo to the printer now. Reason: {f.describe()}"))
        return result

    # Helpers ---------------------------------------------------------------------------------------------
    def _note(self, level: str, text: str) -> None:
        self.recent.append(Entry(time.time(), level, redact(text)))

    def _snapshot(self, label: str) -> Path | None:
        if not self.camera:
            return None
        frame = self.camera.latest()
        if frame is None or time.time() - frame.at > 60:
            return None
        folder = self.run_dir / "snapshots"
        folder.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in label)[:60]
        path = folder / f"{time.strftime('%Y%m%d-%H%M%S')}_{safe}.jpg"
        path.write_bytes(frame.jpeg)
        return path

    def wait_for_actions(self, timeout: float = 120.0) -> None:
        deadline = time.time() + timeout
        for f in list(self._action_futures):
            f.result(timeout=max(0.1, deadline - time.time()))

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=False)
