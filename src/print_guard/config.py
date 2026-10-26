"""Typed settings for print-guard, loaded from a local TOML file.

Every tunable threshold lives here with its default, so the example config, the
README and the code can't drift apart. Unknown keys are rejected (a typo in a
safety threshold should fail loudly, not silently fall back to a default).
Secrets are not resolved here; see `secrets.py`.
"""

from __future__ import annotations

import dataclasses
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ACTIONS = ("log", "notify", "pause", "stop")
MODES = ("dry-run", "armed")
LEVELS = ("info", "warning", "critical")


class ConfigError(ValueError):
    """The config file is missing, malformed or holds an invalid value."""


@dataclass
class PrinterConfig:
    host: str = ""
    serial: str = ""
    # Prefer Windows Credential Manager (`print-guard setup`) or PRINT_GUARD_ACCESS_CODE.
    access_code: str = ""
    name: str = "P1S"
    mqtt_port: int = 8883
    camera_port: int = 6000
    # Verify the printer's TLS certificate against Bambu's CA and its serial number.
    tls_verify: bool = True
    # Extra CA bundle to trust (the simulator uses this for its self-signed printer).
    ca_file: str = ""


@dataclass
class GuardConfig:
    mode: str = "dry-run"
    exit_when_done: bool = False
    # After you resume a print the guard paused, vision ignores this many minutes
    # so the detector does not re-trigger on the mess you just cleaned up.
    rearm_grace_minutes: float = 5.0
    # If the guard paused a print and nobody resumed or stopped it, stop it after
    # this many minutes (turns heaters off). 0 disables the escalation.
    stop_after_paused_minutes: float = 0.0
    runs_dir: str = "runs"


@dataclass
class VisionConfig:
    enabled: bool = True
    model_path: str = "models/obico-failure-detector.onnx"
    # The smoothing constants below were tuned by Obico for one frame every 10 s.
    interval_seconds: float = 10.0
    sensitivity: float = 1.0
    detection_threshold: float = 0.08
    nms_threshold: float = 0.45
    warmup_frames: int = 30
    ewm_span: int = 12
    rolling_win_short: int = 310
    rolling_win_long: int = 7200
    threshold_low: float = 0.38
    threshold_high: float = 0.78
    short_mean_multiple: float = 3.8
    escalating_factor: float = 1.75
    # Each print is judged against its own normal look, learned over about this long.
    baseline_minutes: float = 15.0
    # Extra margin, in standard deviations of the smoothed score's own noise.
    noise_margin_sigmas: float = 3.0
    # Region of interest as [x0, y0, x1, y1] fractions of the frame; detections whose
    # centre falls outside are ignored (use it to mask the purge chute, for example).
    roi: list[float] = field(default_factory=lambda: [0.0, 0.0, 1.0, 1.0])
    # Frames darker than this mean luma (0-255) are skipped: the chamber light is off.
    min_brightness: float = 20.0
    action: str = "pause"


@dataclass
class JudgeConfig:
    # "auto" turns the Claude judge on when Anthropic credentials are available.
    enabled: bool | str = "auto"
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    # Ask Claude to confirm before acting on a local-model failure.
    confirm_vision: bool = True
    # Periodic look at the print for failures the local model can't see
    # (detached part, nozzle printing air). 0 disables.
    periodic_minutes: float = 10.0
    first_layer_check_layers: list[int] = field(default_factory=lambda: [2, 5])
    # How far back the comparison frame is taken from.
    reference_minutes: float = 10.0
    warn_confidence: float = 0.6
    pause_confidence: float = 0.85
    stop_confidence: float = 0.95
    # A periodic verdict must repeat this many times in a row before the guard acts.
    confirmations_for_periodic: int = 2
    max_calls_per_hour: int = 12
    # When a confirmation call fails: "act" trusts the local model, "alert" only notifies.
    on_error: str = "act"
    timeout_seconds: float = 120.0


@dataclass
class ThermalConfig:
    enabled: bool = True
    nozzle_over_c: float = 15.0
    nozzle_over_seconds: float = 20.0
    bed_over_c: float = 10.0
    bed_over_seconds: float = 60.0
    nozzle_under_c: float = 25.0
    nozzle_under_seconds: float = 120.0
    over_action: str = "stop"
    under_action: str = "pause"


@dataclass
class ProgressConfig:
    stall_minutes: float = 30.0
    stall_action: str = "notify"


@dataclass
class FilamentConfig:
    low_remaining_percent: int = 10
    # The printer pauses itself on AMS runout; if it is still running this long
    # after reporting runout, the guard pauses it.
    runout_ensure_paused_seconds: float = 60.0


@dataclass
class HmsConfig:
    fatal_action: str = "pause"
    fatal_thermal_action: str = "stop"
    serious_action: str = "pause"
    common_action: str = "notify"
    info_action: str = "log"
    # Give the printer this long to react to its own error before the guard acts.
    act_after_seconds: float = 20.0
    ignore: list[str] = field(default_factory=list)
    overrides: dict[str, str] = field(default_factory=dict)


@dataclass
class ConnectionConfig:
    offline_alert_seconds: float = 90.0
    camera_alert_seconds: float = 180.0


@dataclass
class NotifyConfig:
    desktop: bool = True
    alarm_sound: bool = True
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_min_level: str = "warning"
    attach_snapshots: bool = True


@dataclass
class Config:
    printer: PrinterConfig = field(default_factory=PrinterConfig)
    guard: GuardConfig = field(default_factory=GuardConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    judge: JudgeConfig = field(default_factory=JudgeConfig)
    thermal: ThermalConfig = field(default_factory=ThermalConfig)
    progress: ProgressConfig = field(default_factory=ProgressConfig)
    filament: FilamentConfig = field(default_factory=FilamentConfig)
    hms: HmsConfig = field(default_factory=HmsConfig)
    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    # Directory relative paths (model, runs) resolve against; set by load().
    base_dir: Path = field(default_factory=Path.cwd)

    def resolve(self, path: str) -> Path:
        p = Path(path)
        return p if p.is_absolute() else (self.base_dir / p)

    @property
    def armed(self) -> bool:
        return self.guard.mode == "armed"


def project_root() -> Path:
    """Folder that holds pyproject.toml when running from the source checkout."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


def default_config_path() -> Path:
    env = os.environ.get("PRINT_GUARD_CONFIG")
    if env:
        return Path(env)
    cwd_cfg = Path.cwd() / "config.toml"
    if cwd_cfg.exists():
        return cwd_cfg
    return project_root() / "config.toml"


def _build(cls: type, data: dict[str, Any], where: str) -> Any:
    known = {f.name: f for f in dataclasses.fields(cls)}
    unknown = sorted(set(data) - set(known))
    if unknown:
        raise ConfigError(f"[{where}] unknown setting(s): {', '.join(unknown)}")
    defaults = cls()
    kwargs: dict[str, Any] = {}
    for name, value in data.items():
        default = getattr(defaults, name)
        if where == "judge" and name == "enabled":
            pass  # true, false or "auto"; checked in validate()
        elif isinstance(default, bool):
            if not isinstance(value, bool):
                raise ConfigError(f"[{where}] {name} must be true or false, got {value!r}")
        elif isinstance(default, int | float):
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ConfigError(f"[{where}] {name} must be a number, got {value!r}")
            # TOML has no float/int distinction for whole numbers; accept ints for floats.
            value = float(value) if isinstance(default, float) else value
            if isinstance(default, int) and not isinstance(value, int):
                raise ConfigError(f"[{where}] {name} must be a whole number, got {value!r}")
        elif isinstance(default, str):
            if not isinstance(value, str):
                raise ConfigError(f"[{where}] {name} must be text, got {value!r}")
        elif isinstance(default, list) and not isinstance(value, list):
            raise ConfigError(f"[{where}] {name} must be a list, got {value!r}")
        elif isinstance(default, dict) and not isinstance(value, dict):
            raise ConfigError(f"[{where}] {name} must be a table, got {value!r}")
        kwargs[name] = value
    return cls(**kwargs)


def from_dict(data: dict[str, Any], base_dir: Path | None = None) -> Config:
    sections = {f.name: f for f in dataclasses.fields(Config) if f.name != "base_dir"}
    unknown = sorted(set(data) - set(sections))
    if unknown:
        raise ConfigError(f"unknown section(s): {', '.join('[' + u + ']' for u in unknown)}")
    built = {}
    for name, f in sections.items():
        section = data.get(name, {})
        if not isinstance(section, dict):
            raise ConfigError(f"[{name}] must be a table")
        built[name] = _build(f.default_factory, section, name)  # type: ignore[misc]
    cfg = Config(**built, base_dir=base_dir or project_root())
    validate(cfg)
    return cfg


def load(path: Path | None = None) -> Config:
    path = path or default_config_path()
    if not path.exists():
        raise ConfigError(
            f"no config file at {path}. Run `print-guard setup` or copy config.example.toml to config.toml."
        )
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    return from_dict(data, base_dir=path.resolve().parent)


def _check_action(value: str, where: str) -> None:
    if value not in ACTIONS:
        raise ConfigError(f"{where} must be one of {', '.join(ACTIONS)}; got {value!r}")


def _check_positive(value: float, where: str, allow_zero: bool = False) -> None:
    if value < 0 or (value == 0 and not allow_zero):
        raise ConfigError(f"{where} must be {'>= 0' if allow_zero else '> 0'}; got {value!r}")


def validate(cfg: Config) -> None:
    if cfg.guard.mode not in MODES:
        raise ConfigError(f"guard.mode must be 'dry-run' or 'armed'; got {cfg.guard.mode!r}")
    for where, value in (
        ("vision.action", cfg.vision.action),
        ("thermal.over_action", cfg.thermal.over_action),
        ("thermal.under_action", cfg.thermal.under_action),
        ("progress.stall_action", cfg.progress.stall_action),
        ("hms.fatal_action", cfg.hms.fatal_action),
        ("hms.fatal_thermal_action", cfg.hms.fatal_thermal_action),
        ("hms.serious_action", cfg.hms.serious_action),
        ("hms.common_action", cfg.hms.common_action),
        ("hms.info_action", cfg.hms.info_action),
    ):
        _check_action(value, where)
    for code, action in cfg.hms.overrides.items():
        _check_action(action, f"hms.overrides[{code!r}]")
    if cfg.judge.enabled not in (True, False, "auto"):
        raise ConfigError("judge.enabled must be true, false or \"auto\"")
    if cfg.judge.on_error not in ("act", "alert"):
        raise ConfigError("judge.on_error must be \"act\" or \"alert\"")
    if cfg.judge.effort not in ("low", "medium", "high", "xhigh", "max"):
        raise ConfigError("judge.effort must be low, medium, high, xhigh or max")
    if cfg.notify.ntfy_min_level not in LEVELS:
        raise ConfigError(f"notify.ntfy_min_level must be one of {', '.join(LEVELS)}")
    roi = cfg.vision.roi
    if len(roi) != 4 or not all(0.0 <= v <= 1.0 for v in roi) or roi[0] >= roi[2] or roi[1] >= roi[3]:
        raise ConfigError("vision.roi must be [x0, y0, x1, y1] with 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1")
    _check_positive(cfg.vision.interval_seconds, "vision.interval_seconds")
    _check_positive(cfg.vision.sensitivity, "vision.sensitivity")
    _check_positive(cfg.vision.escalating_factor, "vision.escalating_factor")
    _check_positive(cfg.vision.baseline_minutes, "vision.baseline_minutes")
    _check_positive(cfg.vision.noise_margin_sigmas, "vision.noise_margin_sigmas", allow_zero=True)
    if not 0 < cfg.vision.detection_threshold < 1:
        raise ConfigError("vision.detection_threshold must be between 0 and 1")
    if cfg.vision.threshold_low >= cfg.vision.threshold_high:
        raise ConfigError("vision.threshold_low must be below vision.threshold_high")
    for name in ("warn_confidence", "pause_confidence", "stop_confidence"):
        v = getattr(cfg.judge, name)
        if not 0 < v <= 1:
            raise ConfigError(f"judge.{name} must be in (0, 1]")
    if not cfg.judge.warn_confidence <= cfg.judge.pause_confidence <= cfg.judge.stop_confidence:
        raise ConfigError("judge confidences must satisfy warn <= pause <= stop")
    if cfg.judge.confirmations_for_periodic < 1:
        raise ConfigError("judge.confirmations_for_periodic must be >= 1")
    _check_positive(cfg.judge.periodic_minutes, "judge.periodic_minutes", allow_zero=True)
    _check_positive(cfg.guard.stop_after_paused_minutes, "guard.stop_after_paused_minutes", allow_zero=True)
    _check_positive(cfg.hms.act_after_seconds, "hms.act_after_seconds", allow_zero=True)
    if not 0 <= cfg.filament.low_remaining_percent <= 100:
        raise ConfigError("filament.low_remaining_percent must be 0-100")
    if not (1 <= cfg.printer.mqtt_port <= 65535 and 1 <= cfg.printer.camera_port <= 65535):
        raise ConfigError("printer ports must be 1-65535")


def require_printer(cfg: Config) -> None:
    """Raise a helpful error when the printer address is still a placeholder."""
    placeholders = {"host": "XXX", "serial": "XXXXX"}
    missing = [k for k, mark in placeholders.items()
               if not getattr(cfg.printer, k) or mark in getattr(cfg.printer, k).upper()]
    if missing:
        raise ConfigError(
            f"printer {' and '.join(missing)} not set in the config. Run `print-guard setup` "
            "or `print-guard discover` to find them."
        )
