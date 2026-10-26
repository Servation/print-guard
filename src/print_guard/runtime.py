"""Assembles the running guard from its parts, shared by `watch`, `simulate` and the tests.

Keeping construction in one place means the simulator and the end-to-end tests
exercise exactly the wiring a real print uses.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from print_guard.config import Config
from print_guard.credentials import mask_serial
from print_guard.guard.engine import Guard
from print_guard.logging_setup import TailHandler
from print_guard.notify import Notifier
from print_guard.printer import tls
from print_guard.printer.camera import CameraStream
from print_guard.printer.mqtt import PrinterLink
from print_guard.vision.judge import ClaudeJudge, credentials_available
from print_guard.vision.model import FailureDetector
from print_guard.vision.smoothing import FailureScore
from print_guard.vision.worker import VisionWorker

log = logging.getLogger(__name__)


class StartupError(RuntimeError):
    pass


@dataclass
class Runtime:
    cfg: Config
    link: PrinterLink
    guard: Guard
    notifier: Notifier
    run_dir: Path
    camera: CameraStream | None = None
    worker: VisionWorker | None = None
    judge: ClaudeJudge | None = None

    def start(self) -> None:
        threading.Thread(target=self._listen_for_announcement, name="ssdp", daemon=True).start()
        self.link.start()
        if self.camera:
            self.camera.start()
        if self.worker:
            self.worker.start()

    def _listen_for_announcement(self) -> None:
        """Learn LAN Only vs cloud mode from the printer's broadcast (passive, nothing is sent)."""
        from print_guard.printer.discovery import discover

        try:
            for ann in discover(15.0):
                if ann.serial == self.cfg.printer.serial:
                    self.link.announced_mode = ann.connect_mode
                    log.info("printer announces %s mode", "LAN Only" if ann.connect_mode == "lan" else "cloud")
        except OSError as e:
            log.debug("could not listen for printer announcements: %s", e)

    def stop(self) -> None:
        if self.worker:
            self.worker.stop()
        if self.judge:
            self.judge.shutdown()
        if self.camera:
            self.camera.stop()
        self.guard.shutdown()
        self.link.stop()

    def run(self, stop: threading.Event, on_tick=None) -> None:
        self.guard.run(stop, on_tick)


def new_run_dir(cfg: Config, label: str = "") -> Path:
    base = cfg.resolve(cfg.guard.runs_dir)
    name = time.strftime("%Y%m%d-%H%M%S") + (f"_{label}" if label else "")
    path = base / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def choose_tls(cfg: Config) -> tuple[object, bool]:
    """Probe the printer (no credentials sent) and build the TLS context.

    Returns (context, verify_after_connect)."""
    p = cfg.printer
    extra = cfg.resolve(p.ca_file) if p.ca_file else None
    if not p.tls_verify:
        log.warning("TLS certificate verification is OFF (printer.tls_verify = false)")
        return tls.make_context(p.serial, verify=False), False
    result = tls.probe(p.host, p.mqtt_port, extra)
    if not result.reachable:
        log.warning("printer not reachable yet at %s:%d (%s); will keep trying", p.host, p.mqtt_port, result.error)
        return tls.make_context(p.serial, True, extra, strict=True), False
    if not result.verified:
        raise StartupError(f"{p.host}:{p.mqtt_port}: {result.error}. Is this really the printer's IP? "
                           "(Set printer.tls_verify = false only if you are sure.)")
    if result.common_name != p.serial:
        raise StartupError(f"the device at {p.host} is a Bambu printer with a different serial "
                           f"({mask_serial(result.common_name)}) than the configured one "
                           f"({mask_serial(p.serial)}). Check printer.host and printer.serial.")
    log.info("printer identity verified: certificate for %s issued by %r", mask_serial(p.serial), result.issuer)
    if result.strict_ok:
        return tls.make_context(p.serial, True, extra, strict=True), False
    return tls.make_context(p.serial, True, extra, strict=False), True


def baseline_path(cfg: Config) -> Path:
    digest = hashlib.sha256(cfg.printer.serial.encode()).hexdigest()[:10]
    return cfg.resolve(cfg.guard.runs_dir) / f"vision_baseline_{digest}.json"


def build(cfg: Config, access_code: str, run_dir: Path, *, camera: bool = True, vision: bool = True,
          judge: bool | None = None, notifier: Notifier | None = None, persist_baseline: bool = True) -> Runtime:
    ctx, verify_after = choose_tls(cfg)
    p = cfg.printer
    link = PrinterLink(p.host, p.serial, access_code, ctx, port=p.mqtt_port, model="P1S",
                       verify_serial_after_connect=verify_after,
                       probe_ca=str(cfg.resolve(p.ca_file)) if p.ca_file else None)
    notifier = notifier or Notifier(cfg.notify)
    use_vision = vision and cfg.vision.enabled
    cam = None
    if camera and use_vision:
        cam = CameraStream(p.host, p.camera_port, access_code, ctx,
                           history_every=60.0 if cfg.vision.interval_seconds >= 5 else 5.0,
                           verify_serial=p.serial if verify_after else None)
    guard = Guard(cfg, link, notifier, run_dir, camera=cam)
    rt = Runtime(cfg, link, guard, notifier, run_dir, camera=cam)
    if not use_vision or cam is None:
        log.info("vision checks off")
        return rt

    detector = None
    model_path = cfg.resolve(cfg.vision.model_path)
    try:
        detector = FailureDetector(model_path, cfg.vision.detection_threshold, cfg.vision.nms_threshold)
        log.info("failure detector loaded (%s)", model_path.name)
    except FileNotFoundError as e:
        log.warning("%s Spaghetti detection is OFF until then.", e)

    want_judge = cfg.judge.enabled if judge is None else judge
    if want_judge == "auto":
        want_judge = credentials_available()
        if not want_judge:
            log.info("Claude judge off: no Anthropic credentials found (optional)")
    jd = ClaudeJudge(cfg.judge) if want_judge else None
    if jd:
        log.info("Claude judge on (%s, effort %s, at most %d calls/hour)", cfg.judge.model, cfg.judge.effort,
                 cfg.judge.max_calls_per_hour)
    if detector is None and jd is None:
        log.warning("no vision method available; camera frames will only be recorded")
    # Simulations must not teach the real camera's baseline what spaghetti looks like.
    score = FailureScore(cfg.vision, baseline_path(cfg) if persist_baseline else None)
    worker = VisionWorker(cfg, cam, detector, jd, link.snapshot, guard.emit, run_dir, score)
    guard.vision = worker
    rt.worker, rt.judge = worker, jd
    return rt


def tail_handler() -> TailHandler:
    return TailHandler()
