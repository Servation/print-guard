"""Config loading: the example file matches the code defaults, and bad values fail loudly."""

from __future__ import annotations

import dataclasses
import tomllib
from pathlib import Path

import pytest

from print_guard.config import Config, ConfigError, from_dict, load, require_printer

ROOT = Path(__file__).resolve().parents[1]


def test_example_config_matches_defaults():
    example = from_dict(tomllib.loads((ROOT / "config.example.toml").read_text(encoding="utf-8")))
    defaults = Config()
    for section in dataclasses.fields(Config):
        if section.name == "base_dir":
            continue
        got, want = getattr(example, section.name), getattr(defaults, section.name)
        for f in dataclasses.fields(want):
            if section.name == "printer" and f.name in ("host", "serial"):
                continue  # placeholders
            assert getattr(got, f.name) == getattr(want, f.name), f"{section.name}.{f.name}"


def test_every_setting_is_documented_in_the_example():
    text = (ROOT / "config.example.toml").read_text(encoding="utf-8")
    for section in dataclasses.fields(Config):
        if section.name == "base_dir":
            continue
        assert f"[{section.name}]" in text
        for f in dataclasses.fields(section.default_factory()):  # type: ignore[misc]
            if f.name == "access_code":
                continue  # deliberately not offered in the example
            assert f"\n{f.name} =" in text, f"{section.name}.{f.name} missing from config.example.toml"


@pytest.mark.parametrize("data, message", [
    ({"vision": {"sensitivty": 1.0}}, "unknown setting"),
    ({"visoin": {}}, "unknown section"),
    ({"guard": {"mode": "armed!"}}, "guard.mode"),
    ({"vision": {"action": "explode"}}, "vision.action"),
    ({"vision": {"interval_seconds": "10"}}, "must be a number"),
    ({"thermal": {"enabled": "yes"}}, "true or false"),
    ({"vision": {"roi": [0.5, 0.5, 0.4, 0.9]}}, "roi"),
    ({"hms": {"overrides": {"0700_2000_0002_0001": "nuke"}}}, "overrides"),
    ({"judge": {"pause_confidence": 0.5, "warn_confidence": 0.6}}, "warn <= pause"),
    ({"judge": {"enabled": "sometimes"}}, "judge.enabled"),
    ({"filament": {"low_remaining_percent": 10.5}}, "whole number"),
])
def test_invalid_values_are_rejected(data, message):
    with pytest.raises(ConfigError, match=message):
        from_dict(data)


def test_ints_accepted_for_floats_and_paths_resolve(tmp_path):
    (tmp_path / "config.toml").write_text('[vision]\ninterval_seconds = 5\n[printer]\nhost = "1.2.3.4"\n'
                                          'serial = "01P09ABC0000001"\n', encoding="utf-8")
    cfg = load(tmp_path / "config.toml")
    assert cfg.vision.interval_seconds == 5.0 and isinstance(cfg.vision.interval_seconds, float)
    assert cfg.resolve("models/x.onnx") == tmp_path / "models" / "x.onnx"
    require_printer(cfg)


def test_placeholders_are_caught():
    example = from_dict(tomllib.loads((ROOT / "config.example.toml").read_text(encoding="utf-8")))
    with pytest.raises(ConfigError, match="host and serial"):
        require_printer(example)


def test_missing_file_explains_what_to_do(tmp_path):
    with pytest.raises(ConfigError, match="print-guard setup"):
        load(tmp_path / "nope.toml")
