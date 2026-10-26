"""Temporal smoothing: single blips don't trip it, sustained spaghetti does."""

from __future__ import annotations

from print_guard.vision.smoothing import FailureScore, Level


def test_warmup_then_ok(cfg):
    s = FailureScore(cfg.vision)
    for _ in range(cfg.vision.warmup_frames - 1):
        assert s.update(0.0) == Level.WARMUP
    assert s.update(0.0) == Level.OK


def test_single_blip_is_ignored(cfg):
    s = FailureScore(cfg.vision)
    for _ in range(60):
        s.update(0.02)
    assert s.update(2.0) == Level.OK  # one frame of a purge blob
    for _ in range(5):
        s.update(0.02)
    assert s.level() == Level.OK


def test_sustained_spaghetti_warns_then_fails(cfg):
    s = FailureScore(cfg.vision)
    for _ in range(60):
        s.update(0.02)
    levels = [s.update(2.5) for _ in range(20)]
    assert Level.WARNING in levels
    assert levels[-1] == Level.FAILURE
    assert levels.index(Level.WARNING) < levels.index(Level.FAILURE)
    # Never on a single frame: at the real 10 s interval this is at least 20 s of evidence.
    assert levels.index(Level.FAILURE) >= 1


def test_weak_detections_only_warn(cfg):
    # Faint, persistent texture is worth a look (and a Claude check), not an automatic pause.
    s = FailureScore(cfg.vision)
    for _ in range(60):
        s.update(0.02)
    levels = [s.update(0.9) for _ in range(30)]
    assert levels[0] == Level.OK
    assert Level.WARNING in levels
    assert Level.FAILURE not in levels


def test_sensitivity_scales_reaction(cfg):
    def frames_to_fail(sensitivity: float) -> int:
        cfg.vision.sensitivity = sensitivity
        s = FailureScore(cfg.vision)
        for _ in range(60):
            s.update(0.02)
        for i in range(100):
            if s.update(1.6) == Level.FAILURE:
                return i
        return 100

    assert frames_to_fail(1.5) < frames_to_fail(1.0) < frames_to_fail(0.7)


def test_noisy_camera_baseline_is_learned(cfg, tmp_path):
    # A view that always shows some texture (p ~ 0.6) must not be treated as failing forever.
    baseline = tmp_path / "baseline.json"
    s = FailureScore(cfg.vision, baseline)
    for _ in range(3000):
        s.update(0.6)
    assert s.level() == Level.OK
    s.save_baseline()
    again = FailureScore(cfg.vision, baseline)
    assert abs(again.state.long_mean - s.state.long_mean) < 1e-9
    assert again.state.lifetime_frames == 3000


def test_rearm_requires_new_warmup(cfg):
    s = FailureScore(cfg.vision)
    for _ in range(40):
        s.update(3.0)
    assert s.level() in (Level.WARNING, Level.FAILURE)
    s.rearm()
    assert s.level() == Level.WARMUP
    assert 0.0 <= s.normalized() <= 1.0


def test_corrupt_baseline_file_is_ignored(cfg, tmp_path):
    bad = tmp_path / "b.json"
    bad.write_text("{not json", encoding="utf-8")
    s = FailureScore(cfg.vision, bad)
    assert s.state.lifetime_frames == 0


# Detector scores of 36 consecutive frames of a healthy print with dense tree supports,
# measured on a real P1S (2026-10-25). The stock Obico scheme would call this a failure.
TREE_SUPPORTS = [2.901, 2.564, 2.15, 3.122, 3.305, 3.322, 3.009, 4.044, 3.421, 3.522, 1.353, 2.569, 2.398, 2.184,
                 1.054, 1.31, 2.596, 4.372, 3.443, 2.499, 3.646, 2.992, 2.891, 3.131, 3.473, 3.46, 3.471, 2.949,
                 3.002, 2.885, 2.308, 3.887, 2.896, 2.971, 3.495, 3.221]


def _busy_print(cfg, seed: int, frames: int):
    import random

    rng = random.Random(seed)
    s = FailureScore(cfg.vision)
    s.new_print(attached=True)
    levels = [s.update(rng.choice(TREE_SUPPORTS)) for _ in range(frames)]
    return s, rng, levels


def test_busy_healthy_print_does_not_alarm_once_calibrated(cfg):
    for seed in range(5):
        _, _, levels = _busy_print(cfg, seed, 5400)  # 15 hours at one frame per 10 s
        assert Level.FAILURE not in levels and Level.WARNING not in levels


def test_spaghetti_on_top_of_supports_is_caught(cfg):
    # Synthetic strands drawn over the real frames raised their score by about +1.5 to +3.5.
    for seed in range(5):
        s, rng, _ = _busy_print(cfg, seed, 600)
        levels = [s.update(rng.choice(TREE_SUPPORTS) + 2.5) for _ in range(40)]
        assert Level.FAILURE in levels
        assert levels.index(Level.FAILURE) < 20  # under ~3.5 minutes at the real cadence


def test_fresh_print_starts_from_the_camera_level_capped(cfg):
    s = FailureScore(cfg.vision)
    s.state.long_mean = 2.5  # the previous print had dense supports
    s.new_print(attached=False)
    assert s.state.base == 1.0 and not s.state.calibrating
    s.new_print(attached=True)
    assert s.state.calibrating


def test_failure_freezes_the_baseline_and_a_veto_absorbs_it(cfg):
    s, rng, _ = _busy_print(cfg, 1, 300)
    for _ in range(30):
        s.update(rng.choice(TREE_SUPPORTS) + 3.0)
    assert s.level() == Level.FAILURE
    frozen = s.state.base
    s.update(6.0)
    assert s.state.base == frozen  # a failure must not teach itself to look normal
    s.absorb()  # Claude looked and said it's fine
    assert s.state.base > frozen + 2.0
    assert s.level() != Level.FAILURE
