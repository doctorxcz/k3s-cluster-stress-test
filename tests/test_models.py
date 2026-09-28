import pytest

from stress_test.models import MASTER_CPU_CAP, StressConfig


def test_validate_ok():
    StressConfig(node="n").validate()


@pytest.mark.parametrize("kwargs", [
    {"duration": 0}, {"max_temp": 59}, {"max_temp": 96},
    {"cpu_load": 0}, {"cpu_load": 101}, {"ram_pct": 0}, {"ram_pct": 96},
])
def test_validate_rejects(kwargs):
    with pytest.raises(ValueError):
        StressConfig(node="n", **kwargs).validate()


def test_master_limits_applied():
    cfg = StressConfig(node="m", cpu_load=100, ram_pct=80, hdd=True)
    msgs = cfg.apply_master_limits()
    assert cfg.cpu_load == MASTER_CPU_CAP
    assert cfg.ram_pct is None and cfg.hdd is False
    assert cfg.max_temp == 80                                     # the master has a lower temperature limit
    assert len(msgs) == 4


def test_master_limits_noop_for_small_load():
    cfg = StressConfig(node="m", cpu_load=50, max_temp=80)
    assert cfg.apply_master_limits() == []
    assert cfg.cpu_load == 50


def test_master_temperature_limit_is_lowered_but_never_raised():
    hot = StressConfig(node="m", cpu_load=50)                     # default 85 °C
    assert hot.apply_master_limits() == ["Master: temperature limit lowered from 85 °C to 80 °C."]
    assert hot.max_temp == 80
    strict = StressConfig(node="m", cpu_load=50, max_temp=75)     # a stricter limit stays
    assert strict.apply_master_limits() == [] and strict.max_temp == 75


def test_cooldown_validation_and_default():
    assert StressConfig(node="n").cooldown == 60
    StressConfig(node="n", cooldown=0).validate()
    StressConfig(node="n", cooldown=600).validate()
    for bad in (-1, 601):
        with pytest.raises(ValueError):
            StressConfig(node="n", cooldown=bad).validate()


def test_cooldown_arg_accepts_seconds_units_and_zero():
    import argparse
    from stress_test.cli import cooldown_arg
    assert cooldown_arg("0") == 0 and cooldown_arg("90") == 90
    assert cooldown_arg("2m") == 120 and cooldown_arg("30s") == 30
    with pytest.raises(argparse.ArgumentTypeError):
        cooldown_arg("abc")


# ---------------- stepped test (profile stepped) ---------------------------------------------------

from stress_test.models import MASTER_CPU_CAP, STEP_TIME_DEFAULT, STEPS_DEFAULT


def _stepped(**kw):
    base = dict(node="n", profile="stepped", duration=len(STEPS_DEFAULT) * STEP_TIME_DEFAULT)
    base.update(kw)
    return StressConfig(**base)


def test_stepped_defaults_are_25_50_75_100_by_3_minutes_total_12():
    assert STEPS_DEFAULT == (25, 50, 75, 100) and STEP_TIME_DEFAULT == 180
    cfg = _stepped()
    cfg.validate()
    assert cfg.stepped and cfg.duration == 720 and StressConfig(node="n").stepped is False


def test_stepped_validation():
    for bad in (dict(steps=()), dict(steps=(50, 25)), dict(steps=(25, 25)), dict(steps=(0, 50)),
                dict(steps=(50, 101))):
        with pytest.raises(ValueError, match="Stages"):
            _stepped(**bad).validate()
    with pytest.raises(ValueError, match="stage time"):
        _stepped(step_time=10, duration=40).validate()
    with pytest.raises(ValueError, match="only the CPU"):
        _stepped(ram_pct=50).validate()
    with pytest.raises(ValueError, match="only the CPU"):
        _stepped(hdd=True).validate()
    with pytest.raises(ValueError, match="total time"):
        _stepped(duration=100).validate()
    with pytest.raises(ValueError, match="profile"):
        StressConfig(node="n", profile="other").validate()


def test_stepped_master_steps_are_capped_at_70_percent():
    cfg = _stepped()
    messages = cfg.apply_master_limits()
    assert cfg.steps == (25, 50, MASTER_CPU_CAP)                         # 75 and 100 -> 70, without a duplicate
    assert cfg.duration == 3 * STEP_TIME_DEFAULT == 540
    assert "Master: stages limited to at most 70 %: 25/50/70." in messages and cfg.max_temp == 80
    cfg.validate()


def test_stepped_master_steps_below_cap_are_unchanged():
    cfg = _stepped(steps=(20, 40, 60), duration=3 * STEP_TIME_DEFAULT, max_temp=75)
    assert cfg.apply_master_limits() == [] and cfg.steps == (20, 40, 60)
