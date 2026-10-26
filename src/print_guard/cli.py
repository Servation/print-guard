"""Command line entry point: `print-guard <command>`.

Commands that only read (check, discover, snapshot) never send anything to the
printer except status requests. Only `watch` in armed mode, and `simulate`
against the fake printer, ever send pause or stop.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import logging
import re
import secrets as pysecrets
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.table import Table

from print_guard import __version__, config, credentials, logging_setup
from print_guard.config import Config, ConfigError
from print_guard.credentials import mask_serial
from print_guard.keepawake import keep_awake

console = Console(highlight=False)
log = logging.getLogger("print_guard")


# Helpers -------------------------------------------------------------------------------------------
def _load(args) -> Config:
    path = Path(args.config) if args.config else None
    return config.load(path)


def _access_code(cfg: Config) -> str:
    code, source = credentials.resolve_access_code(cfg.printer.serial, cfg.printer.access_code)
    if source == "config file":
        console.print("[yellow]Note: the access code is read from config.toml. `print-guard setup` can move it "
                      "to Windows Credential Manager.[/]")
    return code


def _ok(flag: bool) -> str:
    return "[green]OK[/]" if flag else "[red]FAIL[/]"


# watch -------------------------------------------------------------------------------------------------
def cmd_watch(args) -> int:
    cfg = _load(args)
    if args.armed:
        cfg.guard.mode = "armed"
    if args.dry_run:
        cfg.guard.mode = "dry-run"
    if args.exit_when_done:
        cfg.guard.exit_when_done = True
    config.require_printer(cfg)
    code = _access_code(cfg)
    return watch(cfg, code, plain=args.plain, verbose=args.verbose, camera=not args.no_camera,
                 vision=not args.no_vision, judge=False if args.no_judge else None)


def watch(cfg: Config, access_code: str, *, plain: bool = False, verbose: bool = False, camera: bool = True,
          vision: bool = True, judge: bool | None = None, label: str = "", notifier=None,
          until=None, max_seconds: float | None = None, persist_baseline: bool = True) -> int:
    """Run the guard until Ctrl+C, the job ends (exit_when_done), `until()` is true or max_seconds pass."""
    from print_guard import runtime
    from print_guard.ui.dashboard import Dashboard

    run_dir = runtime.new_run_dir(cfg, label)
    tail = logging_setup.TailHandler()
    interactive = not plain and sys.stdout.isatty()
    logging_setup.setup(run_dir / "guard.log", console=not interactive, tail=tail, verbose=verbose)
    log.info("print-guard %s; run folder %s", __version__, run_dir)
    log.info("printer %s at %s; mode %s", mask_serial(cfg.printer.serial), cfg.printer.host, cfg.guard.mode)
    try:
        rt = runtime.build(cfg, access_code, run_dir, camera=camera, vision=vision, judge=judge,
                           notifier=notifier, persist_baseline=persist_baseline)
    except runtime.StartupError as e:
        log.error("%s", e)
        console.print(f"[red]Cannot start:[/] {e}")
        return 2
    log.info("notifications: %s", ", ".join(rt.notifier.channel_names) or "none")

    stop = threading.Event()
    started = time.time()

    def check_end() -> None:
        if until is not None and until(rt):
            stop.set()
        if max_seconds is not None and time.time() - started > max_seconds:
            log.warning("time limit reached")
            stop.set()

    # Ctrl+C should end the loop cleanly (finish any pause in flight) rather than kill it mid-command.
    main_thread = threading.current_thread() is threading.main_thread()
    previous = signal.getsignal(signal.SIGINT) if main_thread else None
    if main_thread:
        signal.signal(signal.SIGINT, lambda *_: stop.set())
    rt.start()
    try:
        with keep_awake():
            if interactive:
                dash = Dashboard(cfg, rt.link, rt.guard, tail, rt.camera, rt.worker)
                with Live(dash, console=console, refresh_per_second=2, screen=False, transient=False):
                    rt.run(stop, on_tick=check_end)
            else:
                rt.run(stop, on_tick=check_end)
    finally:
        if main_thread:
            signal.signal(signal.SIGINT, previous)
        log.info("shutting down")
        with contextlib.suppress(Exception):
            rt.guard.wait_for_actions(timeout=60)
        rt.stop()
    console.print(f"Run folder: {run_dir}")
    _last_runtime[0] = rt
    return 0


_last_runtime: list = [None]  # lets `simulate` inspect the finished run


# setup ---------------------------------------------------------------------------------------------------
def cmd_setup(args) -> int:
    from print_guard.printer.discovery import discover

    target = Path(args.config) if args.config else config.project_root() / "config.toml"
    console.print("[bold]print-guard setup[/]  (nothing is sent to the printer during setup)\n")
    console.print("Listening for printers on the network for 8 seconds...")
    found = []
    try:
        found = discover(8.0)
    except OSError as e:
        console.print(f"[yellow]Discovery unavailable ({e}). Close Bambu Studio and retry, or type the IP.[/]")
    host = serial = ""
    if found:
        for i, a in enumerate(found, 1):
            console.print(f"  {i}. {a.model} '{a.name}' at {a.ip}, serial {a.serial}, mode {a.connect_mode}")
        choice = input(f"Use which printer? [1-{len(found)}, Enter = 1]: ").strip() or "1"
        if choice.isdigit() and 1 <= int(choice) <= len(found):
            host, serial = found[int(choice) - 1].ip, found[int(choice) - 1].serial
    host = input(f"Printer IP [{host}]: ").strip() or host
    serial = (input(f"Printer serial [{serial}]: ").strip() or serial).upper()
    if not re.fullmatch(r"[0-9.]+", host) or not serial:
        console.print("[red]An IP address and serial are required.[/]")
        return 2
    console.print("\nThe LAN access code is on the printer screen: Settings > WLAN (8 characters).")
    code = getpass.getpass("Access code (hidden; Enter keeps the stored one): ").strip()
    if code:
        if not credentials.looks_like_access_code(code):
            console.print("[yellow]That doesn't look like an 8-character access code; storing it anyway.[/]")
        credentials.keyring_set(credentials.access_code_key(serial), code)
        console.print("[green]Access code stored in Windows Credential Manager.[/]")
    topic = ""
    if input("\nSet up phone alerts via the free ntfy app? [y/N]: ").strip().lower() == "y":
        topic = f"print-guard-{pysecrets.token_urlsafe(12).lower().replace('_', 'x').replace('-', 'x')}"
        console.print(f"Install ntfy on your phone and subscribe to this topic: [bold]{topic}[/]")
    key = getpass.getpass("\nOptional Anthropic API key for the Claude judge (hidden; Enter to skip): ").strip()
    if key:
        credentials.keyring_set("anthropic-api-key", key)
        console.print("[green]API key stored in Windows Credential Manager.[/]")

    example = config.project_root() / "config.example.toml"
    text = example.read_text(encoding="utf-8") if example.exists() else "[printer]\nhost = \"\"\nserial = \"\"\n"
    if target.exists():
        text = target.read_text(encoding="utf-8")
        console.print(f"Updating existing {target}")
    text = re.sub(r'(?m)^host = "[^"]*"', f'host = "{host}"', text, count=1)
    text = re.sub(r'(?m)^serial = "[^"]*"', f'serial = "{serial}"', text, count=1)
    if topic:
        text = re.sub(r'(?m)^ntfy_topic = "[^"]*"', f'ntfy_topic = "{topic}"', text, count=1)
    config.from_dict(__import__("tomllib").loads(text))  # validate before writing
    target.write_text(text, encoding="utf-8")
    console.print(f"\n[green]Wrote {target}[/]. Next: [bold]print-guard check[/] (read-only test).")
    return 0


# check ---------------------------------------------------------------------------------------------------------
def cmd_check(args) -> int:
    from print_guard.printer import camera as cam
    from print_guard.printer import tls
    from print_guard.printer.mqtt import PrinterLink
    from print_guard.printer.state import control_status
    from print_guard.vision.judge import credentials_available
    from print_guard.vision.model import MODEL_SHA256, FailureDetector, brightness, decode_jpeg, sha256_file

    logging_setup.setup(console=args.verbose, verbose=args.verbose)
    cfg = _load(args)
    config.require_printer(cfg)
    p = cfg.printer
    rows: list[tuple[str, str, str]] = []
    code, source = credentials.resolve_access_code(p.serial, p.access_code)
    rows.append(("Access code", _ok(True), f"from {source}"))

    extra = cfg.resolve(p.ca_file) if p.ca_file else None
    probe = tls.probe(p.host, p.mqtt_port, extra)
    if not probe.reachable:
        rows.append(("Network", _ok(False), f"{p.host}:{p.mqtt_port} unreachable ({probe.error})"))
        _print_rows(rows)
        console.print("Is the printer on, on the same network, and is the IP right? `print-guard discover` helps.")
        return 1
    rows.append(("Network", _ok(True), f"{p.host}:{p.mqtt_port} reachable"))
    same = probe.common_name == p.serial
    rows.append(("Printer identity", _ok(probe.verified and same),
                 (f"certificate for {mask_serial(probe.common_name)} issued by {probe.issuer!r}" if probe.verified
                  else probe.error)
                 + ("" if same or not probe.verified else " (different from config!)")))
    ctx = tls.make_context(p.serial, p.tls_verify, extra, strict=probe.strict_ok or not probe.verified)
    verify_after = probe.verified and not probe.strict_ok

    from print_guard.printer.discovery import discover

    heard: list = []
    listener = threading.Thread(target=lambda: heard.extend(_safe_discover(discover, 12.0)), daemon=True)
    listener.start()
    link = PrinterLink(p.host, p.serial, code, ctx, port=p.mqtt_port, verify_serial_after_connect=verify_after)
    link.start()
    state = link.wait_for(lambda s: bool(s.raw.get("gcode_state")) and bool(s.firmware), 20.0) or \
        link.wait_for(lambda s: bool(s.raw.get("gcode_state")), 5.0)
    link.stop()
    if state is None:
        rows.append(("MQTT status", _ok(False), link.last_error or "no status received in 25 s"))
        _print_rows(rows)
        console.print("If the error says 'Not authorized', re-check the access code (it changes when LAN mode "
                      "is toggled). Run `print-guard setup` to update it.")
        return 1
    rows.append(("MQTT status", _ok(True), f"{state.gcode_state}, {state.stage_name}"
                 + (f", job {state.job_name!r} {state.percent}%" if state.is_active_job else "")))
    rows.append(("Firmware", "[cyan]info[/]", state.firmware or "not reported"))
    listener.join(timeout=15)
    mode = next((a.connect_mode for a in heard if a.serial == p.serial), None)
    accepted, reason = control_status(state, mode)
    rows.append(("Pause/stop control", {True: _ok(True), False: "[yellow]NO[/]", None: "[cyan]?[/]"}[accepted],
                 reason + {True: ": print-guard can pause and stop",
                           False: ": the printer will refuse pause/stop (alerts still work)",
                           None: ""}[accepted]))
    if state.trays:
        rows.append(("AMS", "[cyan]info[/]", ", ".join(
            f"{t.label} {t.name} {t.remain if t.remain >= 0 else '?'}%" for t in state.trays)))
    for prob in state.hms[:3]:
        rows.append(("HMS", "[yellow]![/]", prob.summary()))

    frame_note = ""
    try:
        jpeg = cam.grab_one(p.host, p.camera_port, code, ctx, timeout=15,
                            verify_serial=p.serial if verify_after else None)
        image = decode_jpeg(jpeg)
        out = cfg.resolve(cfg.guard.runs_dir) / f"check-{time.strftime('%Y%m%d-%H%M%S')}.jpg"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(jpeg)
        frame_note = f"{image.shape[1]}x{image.shape[0]}, brightness {brightness(image):.0f}, saved {out}"
        rows.append(("Camera", _ok(True), frame_note))
    except Exception as e:
        image = None
        rows.append(("Camera", _ok(False), f"{type(e).__name__}: {e}. On the printer enable "
                     "Settings > WLAN > LAN Only Liveview, and close other camera viewers."))

    model_path = cfg.resolve(cfg.vision.model_path)
    if model_path.exists():
        good = sha256_file(model_path) == MODEL_SHA256
        detail = "checksum OK" if good else "checksum mismatch: re-run download-model"
        if good and image is not None:
            dets = FailureDetector(model_path).detect(image)
            detail += f"; this frame scores {sum(d.confidence for d in dets):.2f} ({len(dets)} boxes)"
        rows.append(("Spaghetti model", _ok(good), detail))
    else:
        rows.append(("Spaghetti model", _ok(False), "missing: run `print-guard download-model`"))
    rows.append(("Claude judge", "[cyan]info[/]", "credentials found" if credentials_available()
                 else "no Anthropic credentials (optional)"))
    rows.append(("Mode", "[cyan]info[/]", cfg.guard.mode))
    _print_rows(rows)
    console.print("No commands were sent to the printer.")
    return 0


def _safe_discover(discover, seconds: float) -> list:
    try:
        return discover(seconds)
    except OSError:
        return []


def _print_rows(rows) -> None:
    table = Table(show_header=False, box=None, padding=(0, 1))
    for name, status, detail in rows:
        table.add_row(f"[bold]{name}[/]", status, credentials.redact(detail))
    console.print(table)


# discover --------------------------------------------------------------------------------------------------
def cmd_discover(args) -> int:
    from print_guard.printer.discovery import discover

    console.print(f"Listening for Bambu printers for {args.seconds:.0f} s (passive; nothing is sent)...")
    try:
        found = discover(args.seconds)
    except OSError as e:
        console.print(f"[red]Cannot listen on UDP 2021: {e}[/] (close Bambu Studio and retry)")
        return 1
    if not found:
        console.print("No printers heard. Is the PC on the same network/VLAN as the printer?")
        return 1
    for a in found:
        lan = "LAN Only mode ON" if a.connect_mode == "lan" else f"mode {a.connect_mode or '?'} (LAN Only off)"
        console.print(f"{a.model} '{a.name}'  IP {a.ip}  serial {a.serial}  firmware {a.firmware}  {lan}")
    return 0


# snapshot ------------------------------------------------------------------------------------------------------
def cmd_snapshot(args) -> int:
    from print_guard.printer import camera as cam
    from print_guard.printer import tls
    from print_guard.vision.model import FailureDetector, annotate, brightness, decode_jpeg, encode_jpeg, in_roi

    logging_setup.setup(console=args.verbose, verbose=args.verbose)
    cfg = _load(args)
    if args.image:
        jpeg = Path(args.image).read_bytes()
    else:
        config.require_printer(cfg)
        code = _access_code(cfg)
        p = cfg.printer
        extra = cfg.resolve(p.ca_file) if p.ca_file else None
        ctx = tls.make_context(p.serial, p.tls_verify, extra)
        jpeg = cam.grab_one(p.host, p.camera_port, code, ctx)
    image = decode_jpeg(jpeg)
    detector = FailureDetector(cfg.resolve(cfg.vision.model_path), cfg.vision.detection_threshold,
                               cfg.vision.nms_threshold)
    dets = [d for d in detector.detect(image) if in_roi(d, cfg.vision.roi)]
    p_score = sum(d.confidence for d in dets)
    default_out = cfg.resolve(cfg.guard.runs_dir) / f"snapshot-{time.strftime('%Y%m%d-%H%M%S')}.jpg"
    out = Path(args.out) if args.out else default_out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(encode_jpeg(annotate(image, dets, cfg.vision.roi, f"p={p_score:.2f} boxes={len(dets)}")))
    console.print(f"Frame {image.shape[1]}x{image.shape[0]}, brightness {brightness(image):.0f}, "
                  f"failure score {p_score:.2f} from {len(dets)} boxes. Saved {out}")
    console.print("A healthy print on a clear plate scores under 0.3, but busy scenes (dense tree supports) can "
                  "score 2 to 4 while healthy. The guard therefore judges each print against its own normal "
                  "look; what matters is a jump of about 1.5 or more.")
    if args.judge:
        from print_guard.vision.judge import ClaudeJudge, JudgeRequest

        verdict = ClaudeJudge(cfg.judge).judge(JudgeRequest("periodic", jpeg))
        console.print(f"Claude ({verdict.model}, {verdict.seconds:.1f}s, ~${verdict.cost_usd:.3f}): {verdict.summary}")
    return 0


# judge-test -------------------------------------------------------------------------------------------------
def cmd_judge_test(args) -> int:
    from print_guard.vision.judge import ClaudeJudge, JudgeRequest, JudgeUnavailable

    logging_setup.setup(console=True, verbose=args.verbose)
    cfg = _load(args) if (args.config or config.default_config_path().exists()) else Config()
    current = Path(args.images[-1]).read_bytes()
    reference = Path(args.images[0]).read_bytes() if len(args.images) > 1 else None
    judge = ClaudeJudge(cfg.judge)
    req = JudgeRequest(args.reason, current, reference, 10.0 if reference else None,
                       context={"job": "judge test", "layer": args.layer, "total_layers": args.total_layers,
                                "percent": int(100 * args.layer / max(1, args.total_layers))})
    try:
        v = judge.judge(req)
    except JudgeUnavailable as e:
        console.print(f"[red]Judge unavailable:[/] {e}")
        return 1
    console.print(f"{v.model} in {v.seconds:.1f}s (~${v.cost_usd:.3f}): {v.summary}")
    console.print(f"unrecoverable={v.unrecoverable} image_usable={v.image_usable}")
    return 0


# test-notify ------------------------------------------------------------------------------------------------
def cmd_test_notify(args) -> int:
    from print_guard.notify import Notice, Notifier

    logging_setup.setup(console=True, verbose=args.verbose)
    cfg = _load(args) if (args.config or config.default_config_path().exists()) else Config()
    notifier = Notifier(cfg.notify)
    console.print(f"Sending a test notification ({args.level}) via: {', '.join(notifier.channel_names) or 'none'}")
    notifier.send(Notice(args.level, "Print Guard test", f"This is a {args.level} test notification."), wait=True)
    console.print("Done. (Critical alerts loop the alarm sound while they are on screen.)")
    return 0


# download-model -----------------------------------------------------------------------------------------------
def cmd_download_model(args) -> int:
    from print_guard.vision.model import MODEL_URL, download_model

    cfg = _load(args) if (args.config or config.default_config_path().exists()) else Config(
        base_dir=config.project_root())
    dest = cfg.resolve(cfg.vision.model_path)
    console.print(f"Downloading the Obico failure-detection model (~200 MB)\n  from {MODEL_URL}\n  to   {dest}")

    def progress(done: int, total: int) -> None:
        if total:
            sys.stdout.write(f"\r  {done / 1e6:6.1f} / {total / 1e6:.1f} MB")
            sys.stdout.flush()

    download_model(dest, progress)
    console.print("\n[green]Model downloaded and checksum verified.[/]")
    return 0


# simulate --------------------------------------------------------------------------------------------------------
def cmd_simulate(args) -> int:
    from print_guard.sim.printer import SCENARIOS

    if args.scenario not in SCENARIOS:
        console.print("Scenarios:\n" + "\n".join(f"  {k:10s} {v}" for k, v in SCENARIOS.items()))
        return 2
    base = _load(args) if (args.config or config.default_config_path().exists()) else Config(
        base_dir=config.project_root())
    result = simulate(args.scenario, base, armed=not args.dry_run, judge=args.judge, plain=args.plain,
                      desktop=not args.quiet, ntfy=args.ntfy, timeout=args.timeout)
    return 0 if result.passed else 1


class SimResult:
    def __init__(self, passed: bool, summary: str, guard=None, fake=None) -> None:
        self.passed, self.summary, self.guard, self.fake = passed, summary, guard, fake


def sim_config(base: Config, fake, scenario: str, armed: bool, judge: bool, desktop: bool, ntfy: bool,
               runs_dir: Path | None = None) -> Config:
    """Settings for a fast simulated print: same checks, compressed timescales."""
    cfg = config.from_dict({}, base_dir=base.base_dir)
    cfg.vision.model_path = str(base.resolve(base.vision.model_path))
    cfg.printer.host, cfg.printer.serial, cfg.printer.name = "127.0.0.1", fake.s.serial, "Simulated P1S"
    cfg.printer.mqtt_port, cfg.printer.camera_port = fake.mqtt_port, fake.camera_port
    cfg.printer.ca_file = str(fake.certs.ca_file)
    cfg.guard.mode = "armed" if armed else "dry-run"
    cfg.guard.exit_when_done = True
    cfg.guard.runs_dir = str(runs_dir or base.resolve(base.guard.runs_dir) / "simulations")
    # One frame per second and short averaging windows, so minutes of printing fit in seconds.
    cfg.vision.interval_seconds, cfg.vision.warmup_frames, cfg.vision.ewm_span = 1.0, 5, 4
    cfg.vision.rolling_win_short, cfg.vision.rolling_win_long = 31, 720
    cfg.progress.stall_minutes = 0.3
    cfg.hms.act_after_seconds = 3.0
    cfg.thermal.nozzle_over_seconds = 5.0
    cfg.connection.camera_alert_seconds = 60.0
    cfg.judge.enabled = judge
    cfg.judge.periodic_minutes = 1.0 if judge else 0.0
    cfg.notify.desktop = desktop
    cfg.notify.ntfy_topic = base.notify.ntfy_topic if ntfy else ""
    cfg.notify.ntfy_server = base.notify.ntfy_server
    return cfg


def _expectation(scenario: str, armed: bool):
    """Returns (done(guard, fake) -> bool, verdict(guard, fake) -> (passed, text))."""
    def acted(g, command):
        return [r for r in g.actions_taken if r.command == command]

    def finding(g, prefix):
        return any(k.startswith(prefix) for k in g._handled)

    if scenario == "clean":
        return (lambda g, f: g.finished.is_set(),
                lambda g, f: (g.finished.is_set() and not f.commands and g.dry_run_actions == 0,
                              f"print {'finished' if g.finished.is_set() else 'did not finish'}; "
                              f"commands sent: {f.commands or 'none'}"))
    if scenario in ("spaghetti", "overtemp", "hms-fatal"):
        command = "pause" if scenario == "spaghetti" else "stop"
        if not armed:
            return (lambda g, f: g.dry_run_actions > 0,
                    lambda g, f: (g.dry_run_actions > 0 and not f.commands,
                                  f"dry run: would have acted {g.dry_run_actions}x; sent {f.commands or 'nothing'}"))
        return (lambda g, f: bool(acted(g, command)),
                lambda g, f: (any(r.ok for r in acted(g, command)) and f.state["gcode_state"] in
                              (("PAUSE",) if command == "pause" else ("FAILED",)),
                              f"guard sent {command}; printer is now {f.state['gcode_state']} at layer "
                              f"{f.state['layer_num']}"))
    if scenario == "runout":
        return (lambda g, f: finding(g, "runout"),
                lambda g, f: (finding(g, "runout") and not f.commands,
                              "runout alert raised; guard left the self-paused printer alone"))
    if scenario == "stall":
        return (lambda g, f: finding(g, "progress:stall"),
                lambda g, f: (finding(g, "progress:stall"), "stall detected"))
    if scenario == "locked":
        return (lambda g, f: bool(g.actions_taken) or (not armed and g.dry_run_actions > 0),
                lambda g, f: ((any(r.rejected_for_auth for r in g.actions_taken) or not armed)
                              and f.state["gcode_state"] == "RUNNING",
                              f"printer rejected {f.rejected or 'nothing'}; guard reported it"))
    if scenario == "dark":
        return (lambda g, f: finding(g, "vision:dark"),
                lambda g, f: (finding(g, "vision:dark"), "dark camera reported"))
    raise ValueError(scenario)


def simulate(scenario: str, base: Config, *, armed: bool = True, judge: bool = False, plain: bool = False,
             desktop: bool = True, ntfy: bool = False, timeout: float = 360.0, runs_dir: Path | None = None,
             sim_overrides: dict | None = None) -> SimResult:
    from print_guard.notify import Notifier
    from print_guard.sim.printer import FakePrinter, SimSettings

    settings = SimSettings(scenario=scenario, access_code="sim" + pysecrets.token_hex(3))
    for k, v in (sim_overrides or {}).items():
        setattr(settings, k, v)
    with tempfile.TemporaryDirectory(prefix="print-guard-sim-") as tmp:
        fake = FakePrinter(settings, Path(tmp))
        fake.start()
        try:
            cfg = sim_config(base, fake, scenario, armed, judge, desktop, ntfy, runs_dir)
            done, verdict = _expectation(scenario, armed)
            console.print(f"[bold]Simulating '{scenario}'[/] on a fake P1S at 127.0.0.1 "
                          f"({'ARMED' if armed else 'DRY RUN'}); the real printer is not involved.")
            notifier = Notifier(cfg.notify, app_name="Print Guard (simulation)")
            settle: list[float] = []

            def until(rt) -> bool:
                if done(rt.guard, fake):
                    settle.append(time.time())
                    return time.time() - settle[0] > 4.0  # let the dashboard show the outcome
                return False

            _last_runtime[0] = None
            code = watch(cfg, settings.access_code, plain=plain, label=f"sim-{scenario}", notifier=notifier,
                         judge=judge, until=until, max_seconds=timeout, persist_baseline=False)
            rt = _last_runtime[0]
            if code != 0 or rt is None:
                return SimResult(False, f"guard failed to start (exit code {code})", None, fake)
            passed, text = verdict(rt.guard, fake)
        finally:
            fake.stop()
    style = "green" if passed else "red"
    console.print(f"[{style}]{'PASS' if passed else 'FAIL'}[/] {scenario}: {text}")
    return SimResult(passed, text, rt.guard, fake)


# Entry point -----------------------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="print-guard", description="Watch a Bambu Lab P1S and stop failing prints.")
    parser.add_argument("--version", action="version", version=f"print-guard {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="path to config.toml (default: ./config.toml)")
    common.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    w = sub.add_parser("watch", parents=[common], help="watch the printer (start this when a print begins)")
    mode = w.add_mutually_exclusive_group()
    mode.add_argument("--armed", action="store_true", help="really pause/stop (overrides guard.mode)")
    mode.add_argument("--dry-run", action="store_true", help="alerts only (overrides guard.mode)")
    w.add_argument("--exit-when-done", action="store_true", help="exit when the print finishes or fails")
    w.add_argument("--no-camera", action="store_true", help="telemetry checks only")
    w.add_argument("--no-vision", action="store_true", help="same as --no-camera")
    w.add_argument("--no-judge", action="store_true", help="never call Claude")
    w.add_argument("--plain", action="store_true", help="plain log output instead of the dashboard")
    w.set_defaults(func=cmd_watch)

    sub.add_parser("setup", parents=[common], help="interactive first-time setup").set_defaults(func=cmd_setup)
    sub.add_parser("check", parents=[common], help="read-only connection test").set_defaults(func=cmd_check)
    d = sub.add_parser("discover", parents=[common], help="find printers on the LAN (passive)")
    d.add_argument("--seconds", type=float, default=8.0)
    d.set_defaults(func=cmd_discover)
    s = sub.add_parser("snapshot", parents=[common], help="grab a camera frame and score it")
    s.add_argument("--out", help="where to save the annotated frame")
    s.add_argument("--image", help="score this JPEG instead of the live camera")
    s.add_argument("--judge", action="store_true", help="also ask Claude (costs a few cents)")
    s.set_defaults(func=cmd_snapshot)
    j = sub.add_parser("judge-test", parents=[common], help="ask Claude about one or two images")
    j.add_argument("images", nargs="+", help="current frame, or reference frame then current frame")
    j.add_argument("--reason", default="periodic", choices=["periodic", "confirm", "first_layer"])
    j.add_argument("--layer", type=int, default=50)
    j.add_argument("--total-layers", type=int, default=400)
    j.set_defaults(func=cmd_judge_test)
    n = sub.add_parser("test-notify", parents=[common], help="send a test notification")
    n.add_argument("--level", default="warning", choices=["info", "warning", "critical"])
    n.set_defaults(func=cmd_test_notify)
    sub.add_parser("download-model", parents=[common],
                   help="fetch the spaghetti-detection model").set_defaults(func=cmd_download_model)
    sm = sub.add_parser("simulate", parents=[common], help="run against a fake printer (safe end-to-end test)")
    sm.add_argument("scenario", nargs="?", default="list", help="spaghetti, runout, overtemp, hms-fatal, "
                                                                "stall, locked, dark, clean (or 'list')")
    sm.add_argument("--dry-run", action="store_true", help="alert only, even against the fake printer")
    sm.add_argument("--judge", action="store_true", help="include the Claude judge (real API calls)")
    sm.add_argument("--plain", action="store_true")
    sm.add_argument("--quiet", action="store_true", help="no desktop notifications")
    sm.add_argument("--ntfy", action="store_true", help="also push simulated alerts to your ntfy topic")
    sm.add_argument("--timeout", type=float, default=360.0)
    sm.set_defaults(func=cmd_simulate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "no_vision", False):
        args.no_camera = True
    try:
        return int(args.func(args) or 0)
    except ConfigError as e:
        console.print(f"[red]Config problem:[/] {e}")
        return 2
    except LookupError as e:
        console.print(f"[red]{e}[/]")
        return 2
    except KeyboardInterrupt:
        return 130
