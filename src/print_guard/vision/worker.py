"""The camera check loop: score a frame every few seconds and decide what it means.

Runs on its own thread so slow model inference or a slow Claude call never
delays the telemetry checks. Results leave as Findings through `emit`.
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path

import cv2

from print_guard.config import Config
from print_guard.guard.events import Action, Finding
from print_guard.printer.camera import CameraStream
from print_guard.printer.state import PrinterState
from print_guard.vision.judge import ClaudeJudge, JudgeRequest, JudgeUnavailable, Verdict
from print_guard.vision.model import (
    Detection,
    FailureDetector,
    annotate,
    brightness,
    decode_jpeg,
    in_roi,
)
from print_guard.vision.smoothing import FailureScore, Level

log = logging.getLogger(__name__)

DARK_FRAMES_BEFORE_ALERT = 6
VETO_COOLDOWN = 600.0
UNSURE_COOLDOWN = 300.0
RECHECK_DELAY = 90.0
WARNING_RENOTIFY = 1800.0
WARNING_CONFIRM_INTERVAL = 600.0
NOT_PRINTING = {"PAUSE": "paused", "FINISH": "finished", "FAILED": "stopped", "PREPARE": "preparing",
                "IDLE": "idle", "UNKNOWN": "waiting for status"}


@dataclass
class VisionStatus:
    state: str = "starting"
    level: str = Level.WARMUP.value
    current_p: float = 0.0
    base: float = 0.0
    adjusted: float = 0.0
    gauge: float = 0.0
    detections: int = 0
    brightness: float = 0.0
    frames_scored: int = 0
    last_check_at: float = 0.0
    judge: str = "off"
    judge_cost: float = 0.0
    notes: list[str] = field(default_factory=list)


@dataclass
class _Pending:
    future: Future
    reason: str
    snapshot: Path | None
    adjusted: float = 0.0


class VisionWorker:
    def __init__(
        self,
        cfg: Config,
        camera: CameraStream,
        detector: FailureDetector | None,
        judge: ClaudeJudge | None,
        get_state: Callable[[], PrinterState],
        emit: Callable[[Finding], None],
        run_dir: Path,
        score: FailureScore,
    ) -> None:
        self.cfg = cfg
        self.camera = camera
        self.detector = detector
        self.judge = judge
        self.get_state = get_state
        self.emit = emit
        self.run_dir = run_dir
        self.score = score
        self.status = VisionStatus(judge="on" if judge and judge.enabled else "off")
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="vision", daemon=True)
        self._lock = threading.Lock()
        self._last_index = -1
        self._dark_frames = 0
        self._grace_until = 0.0
        self._pending: list[_Pending] = []
        self._cooldown_until = 0.0
        self._veto_score = 0.0
        self._next_periodic = time.time() + cfg.judge.periodic_minutes * 60
        self._first_layer_done: set[int] = set()
        self._periodic_hits = 0
        self._layers: collections.deque[tuple[float, int]] = collections.deque(maxlen=720)
        self._acted = False
        self._last_warning_notice = 0.0
        self._last_warning_confirm = 0.0
        self._warning_snapshot: Path | None = None
        # Finding keys carry the episode so a failure after a resume is acted on again
        # instead of being deduplicated as "already handled this print".
        self._episode = 0

    # Lifecycle --------------------------------------------------------------------------
    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)
        self.score.save_baseline()

    def new_print(self, attached: bool = False) -> None:
        with self._lock:
            self.score.new_print(attached)
            self._first_layer_done.clear()
            self._periodic_hits = 0
            self._pending.clear()
            self._cooldown_until = 0.0
            self._acted = False
            self._last_warning_notice = self._last_warning_confirm = 0.0
            self._episode += 1
            self._next_periodic = time.time() + self.cfg.judge.periodic_minutes * 60
        log.info("vision: %s", "joined a print in progress; calibrating on its current look for "
                 f"{self.cfg.vision.warmup_frames} frames (a failure already under way would look normal)"
                 if attached else "new print; history reset")

    def rearm(self, grace_seconds: float) -> None:
        with self._lock:
            self.score.rearm()
            self._grace_until = time.time() + grace_seconds
            self._periodic_hits = 0
            self._acted = False
            self._episode += 1
            self._last_warning_notice = self._last_warning_confirm = 0.0
            self._next_periodic = time.time() + max(grace_seconds, self.cfg.judge.periodic_minutes * 60)
        log.info("vision: re-armed with %.0f min grace", grace_seconds / 60)

    # Main loop -----------------------------------------------------------------------------
    def _run(self) -> None:
        last_baseline_save = time.time()
        while not self._stop.wait(self.cfg.vision.interval_seconds):
            try:
                self._tick()
                self._collect_judge_results()
            except Exception:
                log.exception("vision check failed; continuing")
            if time.time() - last_baseline_save > 300:
                self.score.save_baseline()
                last_baseline_save = time.time()

    def _tick(self) -> None:
        now = time.time()
        state = self.get_state()
        self._layers.append((now, state.layer))
        self.status.last_check_at = now
        if self.judge:
            self.status.judge = "on" if self.judge.enabled else f"off ({self.judge.disabled_reason})"
            self.status.judge_cost = self.judge.total_cost
        if not state.is_printing_layers:
            what = state.stage_name if state.is_running else NOT_PRINTING.get(state.gcode_state,
                                                                               state.gcode_state.lower())
            self.status.state = f"not scoring while {what}"
            return
        if now < self._grace_until:
            self.status.state = f"grace period after resume ({(self._grace_until - now) / 60:.1f} min left)"
            return
        frame = self.camera.latest()
        if frame is None or now - frame.at > max(30.0, 3 * self.cfg.vision.interval_seconds):
            self.status.state = "waiting for a fresh camera frame"
            return
        if frame.index == self._last_index:
            self.status.state = "camera frame unchanged"
            return
        self._last_index = frame.index
        image = decode_jpeg(frame.jpeg)
        light = brightness(image)
        self.status.brightness = light
        if light < self.cfg.vision.min_brightness:
            self._dark_frames += 1
            self.status.state = f"frame too dark ({light:.0f}); is the chamber light off?"
            log.info("vision: skipped dark frame (brightness %.0f < %.0f)", light, self.cfg.vision.min_brightness)
            if self._dark_frames == DARK_FRAMES_BEFORE_ALERT:
                self.emit(Finding(key="vision:dark", source="vision", action=Action.NOTIFY,
                                  title="Camera too dark to watch the print",
                                  detail="Turn the chamber light on so failures can be seen.",
                                  renotify_after=3600))
            return
        self._dark_frames = 0

        detections = self._detect(image)
        p = sum(d.confidence for d in detections)
        with self._lock:
            level = self.score.update(p)
        s = self.score.state
        self.status.state = "watching"
        self.status.level = level.value
        self.status.current_p = p
        self.status.base = s.base
        self.status.adjusted = self.score.adjusted
        self.status.gauge = self.score.normalized()
        self.status.detections = len(detections)
        self.status.frames_scored = s.frame
        log.info("vision: frame %d p=%.2f ewm=%.2f base=%.2f margin=%.2f adj=%.2f boxes=%d -> %s", frame.index,
                 p, s.ewm_mean, s.base, self.score.margin, self.score.adjusted, len(detections), level.value)

        caption = (f"print-guard {time.strftime('%H:%M:%S')} | {level.value.upper()} | score {self.score.adjusted:.2f}"
                   f" (frame p={p:.2f}) | layer {state.layer}/{state.total_layers}")
        marked = annotate(image, detections, self.cfg.vision.roi, caption)
        self._write_latest(marked)

        if level == Level.WARNING:
            self._on_local_warning(marked, frame.jpeg, state)
        elif level == Level.FAILURE:
            self._on_local_failure(marked, frame.jpeg, state)

        self._maybe_ask_judge(frame.jpeg, state, now)

    def _detect(self, image) -> list[Detection]:
        if self.detector is None:
            return []
        return [d for d in self.detector.detect(image) if in_roi(d, self.cfg.vision.roi)]

    # Acting on the local model --------------------------------------------------------------
    def _on_local_warning(self, marked, jpeg: bytes, state: PrinterState) -> None:
        """Warning level: tell the user, and let Claude (if enabled) decide whether it's real.

        Weak but persistent detections never reach the failure level on their own, so
        this is where a second opinion can catch slower failures."""
        now = time.time()
        if now - self._last_warning_notice >= WARNING_RENOTIFY:
            self._last_warning_notice = now
            snapshot = self._save_snapshot(marked, "vision-warning")
            self._warning_snapshot = snapshot
            self.emit(Finding(key=f"vision:warning:{self._episode}", source="vision", action=Action.NOTIFY,
                              title="Possible print failure on camera",
                              detail=f"detector score {self.score.adjusted:.2f} is rising "
                                     "(warning level, not acting on the detector alone)",
                              image=snapshot, renotify_after=WARNING_RENOTIFY))
        j = self.judge
        if (not j or not j.enabled or not self.cfg.judge.confirm_vision or self._acted
                or any(p.reason == "confirm" for p in self._pending) or now < self._cooldown_until
                or now - self._last_warning_confirm < WARNING_CONFIRM_INTERVAL):
            return
        self._last_warning_confirm = now
        log.info("vision: warning level; asking Claude whether the print is failing")
        req = JudgeRequest("confirm", jpeg, *self._reference(now),
                           context=self._context(state, score=round(self.score.adjusted, 2)))
        self._pending.append(_Pending(j.submit(req), "confirm", self._warning_snapshot, self.score.adjusted))

    def _on_local_failure(self, marked, jpeg: bytes, state: PrinterState) -> None:
        if self._acted or any(p.reason == "confirm" for p in self._pending):
            return
        now = time.time()
        if now < self._cooldown_until and self.score.adjusted < self._veto_score * 1.5:
            return
        snapshot = self._save_snapshot(marked, "vision-failure")
        detail = f"detector score {self.score.adjusted:.2f} (failure level)"
        if self.judge and self.judge.enabled and self.cfg.judge.confirm_vision:
            log.info("vision: failure level reached; asking Claude to confirm before acting")
            req = JudgeRequest("confirm", jpeg, *self._reference(now),
                               context=self._context(state, score=round(self.score.adjusted, 2)))
            self._pending.append(_Pending(self.judge.submit(req), "confirm", snapshot, self.score.adjusted))
            self.status.state = "asking Claude to confirm"
            return
        self._act(Action.parse(self.cfg.vision.action), "Print failure seen by the camera", detail, snapshot)

    def _act(self, action: Action, title: str, detail: str, snapshot: Path | None) -> None:
        self._acted = action >= Action.PAUSE
        self.emit(Finding(key=f"vision:act:{action.name.lower()}:{self._episode}", source="vision", action=action,
                          title=title, detail=detail, image=snapshot))

    # Claude ---------------------------------------------------------------------------------------
    def _context(self, state: PrinterState, **extra) -> dict:
        return {"job": state.job_name, "layer": state.layer, "total_layers": state.total_layers,
                "percent": state.percent, **extra}

    def _reference(self, now: float) -> tuple[bytes | None, float | None]:
        minutes = self.cfg.judge.reference_minutes
        ref = self.camera.frame_near(minutes * 60)
        if ref is None or now - ref.at < 120:
            return None, None
        return ref.jpeg, (now - ref.at) / 60

    def _layer_at(self, when: float) -> int | None:
        if not self._layers:
            return None
        return min(self._layers, key=lambda tl: abs(tl[0] - when))[1]

    def _maybe_ask_judge(self, jpeg: bytes, state: PrinterState, now: float) -> None:
        j = self.judge
        if not j or not j.enabled or any(p.reason != "confirm" for p in self._pending) or self._acted:
            return
        for layer in self.cfg.judge.first_layer_check_layers:
            if state.layer >= layer and layer not in self._first_layer_done:
                self._first_layer_done.add(layer)
                if state.layer > layer + 2:
                    continue  # started watching mid-print; this check no longer applies
                log.info("judge: first-layer check at layer %d", state.layer)
                req = JudgeRequest("first_layer", jpeg, context=self._context(state))
                self._pending.append(_Pending(j.submit(req), "first_layer", None))
                return
        if self.cfg.judge.periodic_minutes > 0 and now >= self._next_periodic:
            self._next_periodic = now + self.cfg.judge.periodic_minutes * 60
            ref_jpeg, ref_minutes = self._reference(now)
            ctx = self._context(state)
            if ref_minutes is not None:
                ctx["reference_layer"] = self._layer_at(now - ref_minutes * 60)
            log.info("judge: periodic check (reference %s)",
                     f"{ref_minutes:.0f} min ago" if ref_minutes else "none yet")
            req = JudgeRequest("periodic", jpeg, ref_jpeg, ref_minutes, context=ctx)
            self._pending.append(_Pending(j.submit(req), "periodic", None))

    def _collect_judge_results(self) -> None:
        for pending in [p for p in self._pending if p.future.done()]:
            self._pending.remove(pending)
            try:
                verdict = pending.future.result()
            except JudgeUnavailable as e:
                self._judge_failed(pending, str(e))
                continue
            except Exception as e:
                log.exception("judge call crashed")
                self._judge_failed(pending, f"{type(e).__name__}")
                continue
            log.info("judge (%s, %.1fs, $%.3f): %s", pending.reason, verdict.seconds, verdict.cost_usd,
                     verdict.summary)
            if pending.reason == "confirm":
                self._handle_confirm(pending, verdict)
            else:
                self._handle_observation(pending, verdict)

    def _judge_failed(self, pending: _Pending, why: str) -> None:
        log.warning("judge unavailable for %s check: %s", pending.reason, why)
        if pending.reason != "confirm":
            return
        detail = f"detector score {pending.adjusted:.2f}; Claude could not confirm ({why})"
        if self.cfg.judge.on_error == "act":
            self._act(Action.parse(self.cfg.vision.action), "Print failure seen by the camera", detail,
                      pending.snapshot)
        else:
            self.emit(Finding(key=f"vision:unconfirmed:{self._episode}", source="vision", action=Action.NOTIFY,
                              level="critical",
                              title="Possible print failure (unconfirmed)", detail=detail,
                              image=pending.snapshot, renotify_after=900))

    def _verdict_action(self, v: Verdict) -> Action:
        c = self.cfg.judge
        if v.unrecoverable and v.confidence >= c.stop_confidence:
            return Action.STOP
        return Action.parse(self.cfg.vision.action)

    def _handle_confirm(self, pending: _Pending, v: Verdict) -> None:
        c = self.cfg.judge
        if v.failing and v.confidence >= c.pause_confidence:
            self._act(self._verdict_action(v), f"Claude confirms {v.failure_type.replace('_', ' ')}",
                      f"{v.evidence} (Claude {v.confidence:.0%}, detector {pending.adjusted:.2f})", pending.snapshot)
        elif v.failing and v.confidence >= c.warn_confidence:
            self._cooldown_until, self._veto_score = time.time() + UNSURE_COOLDOWN, pending.adjusted
            self.emit(Finding(key=f"vision:unsure:{self._episode}", source="judge", action=Action.NOTIFY,
                              level="critical",
                              title="Possible failure: please look at the printer",
                              detail=f"{v.evidence} (Claude {v.confidence:.0%}, below the pause threshold)",
                              image=pending.snapshot, renotify_after=900))
        else:
            self._cooldown_until, self._veto_score = time.time() + VETO_COOLDOWN, pending.adjusted
            if v.image_usable:
                with self._lock:
                    self.score.absorb()  # Claude says this look is normal; learn it
            self.emit(Finding(key="vision:veto", source="judge", action=Action.LOG,
                              title="Claude overruled the local detector",
                              detail=f"{v.summary}; not acting for {VETO_COOLDOWN / 60:.0f} min unless the "
                                     "score keeps rising"))

    def _handle_observation(self, pending: _Pending, v: Verdict) -> None:
        c = self.cfg.judge
        if v.failing and v.confidence >= c.pause_confidence:
            self._periodic_hits += 1
            if self._periodic_hits >= c.confirmations_for_periodic:
                frame = self.camera.latest()
                snapshot = None
                if frame is not None:
                    snapshot = self._save_snapshot(decode_jpeg(frame.jpeg), f"judge-{v.failure_type}")
                self._act(self._verdict_action(v), f"Claude sees {v.failure_type.replace('_', ' ')}",
                          f"{v.evidence} ({v.confidence:.0%}, seen {self._periodic_hits}x in a row)", snapshot)
            else:
                log.info("judge: failure seen (%d/%d); re-checking in %.0fs", self._periodic_hits,
                         c.confirmations_for_periodic, RECHECK_DELAY)
                self._next_periodic = time.time() + RECHECK_DELAY
        else:
            self._periodic_hits = 0
            if v.failing and v.confidence >= c.warn_confidence:
                self.emit(Finding(key=f"judge:warn:{v.failure_type}", source="judge", action=Action.NOTIFY,
                                  title=f"Claude suspects {v.failure_type.replace('_', ' ')}",
                                  detail=f"{v.evidence} ({v.confidence:.0%})", renotify_after=1800))

    # Files ----------------------------------------------------------------------------------------
    def _write_latest(self, image) -> None:
        target = self.run_dir / "latest.jpg"
        tmp = self.run_dir / "latest.tmp.jpg"
        if cv2.imwrite(str(tmp), image):
            tmp.replace(target)

    def _save_snapshot(self, image, label: str) -> Path | None:
        folder = self.run_dir / "snapshots"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{time.strftime('%Y%m%d-%H%M%S')}_{label}.jpg"
        return path if cv2.imwrite(str(path), image) else None
