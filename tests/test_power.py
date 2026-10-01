"""CPU power (RAPL): probe parsing, power from the energy counter, log line, summary, baseline."""
import pytest

from stress_test.baseline import check_against_baseline
from stress_test.logparse import parse_log
from stress_test.models import ProbeData, Sample
from stress_test.monitor import format_reading
from stress_test.parsing import parse_probe_output, power_watts
from stress_test.summary import build_summary, run_stats
from test_compare import make_log
from test_integration import run_tool


def test_parse_probe_rapl_line():
    p = parse_probe_output("freq 3000\nrapl_uj 123456789 262143000000 25000000 51000000\n")
    assert p.energy_uj == 123456789 and p.energy_max_uj == 262143000000
    assert p.pl1_w == 25.0 and p.pl2_w == 51.0


def test_parse_probe_rapl_zero_limits_are_unknown():
    p = parse_probe_output("rapl_uj 5 0 0 0")
    assert p.energy_uj == 5 and p.energy_max_uj is None and p.pl1_w is None and p.pl2_w is None


def test_power_watts_basic_and_wraparound():
    assert power_watts((1_000_000, 10.0), 21_000_000, 12.0) == pytest.approx(10.0)
    assert power_watts((99_000_000, 10.0), 4_000_000, 12.0, max_uj=100_000_000) == pytest.approx(2.5)
    assert power_watts((99_000_000, 10.0), 4_000_000, 12.0) is None       # wrap without a maximum
    assert power_watts(None, 5, 1.0) is None and power_watts((1, 5.0), 9, 5.0) is None


def test_format_reading_power_suffix_and_parse_back():
    line = format_reading(50.0, ProbeData(temps={"CPU": 60}, freq_mhz=3000), now="10:00:00", power_w=17.34)
    assert line.endswith("Clock: 3000 MHz | Power: 17.3 W")
    assert "Power" not in format_reading(50.0, ProbeData(freq_mhz=3000), now="10:00:00")
    run = parse_log("=== KUBERNETES STRESS-NG LOG ===\nNode: n\nPower limits: PL1 25 W, PL2 51 W\n" + line + "\n")
    assert run.samples[0].power_w == 17.3 and run.power_limits == (25.0, 51.0)


def test_old_logs_without_power_still_parse():
    run = parse_log(make_log((50, 55), (52,), node="n"), "n.log")
    assert all(s.power_w is None for s in run.samples) and run.power_limits == (None, None)


def _samples(watts):
    return [Sample(t=float(i * 5), phase="test", cpu_temp=60, freq_mhz=3000, cpu_pct=99.0, power_w=w)
            for i, w in enumerate(watts)]


def test_summary_shows_power_and_limit_note():
    base = ProbeData(temps={"CPU": 40}, pl1_w=25.0, pl2_w=51.0)
    text = "\n".join(build_summary(_samples([24.5, 25.0, 24.8]), base, []))
    assert "CPU power (test)" in text and "limit PL1 25 W, PL2 51 W" in text
    assert "power limit" in text
    quiet = "\n".join(build_summary(_samples([10.0, 11.0]), base, []))
    assert "held down by the power limit" not in quiet


def test_run_stats_power():
    st = run_stats(_samples([10.0, 20.0, 30.0]), None)
    assert st.power_avg == 20.0 and st.power_max == 30.0
    assert run_stats(_samples([None, None]), None).power_avg is None


def test_baseline_flags_higher_power():
    def data(avg):
        return {"profile": "classic", "stage_targets": [], "source_log": "b.log", "baseline_temp": None,
                "metrics": {}, "stage_ops": {}, "stats": {"temp_max": 60, "freq_avg": 3000.0, "throttling": False,
                                                          "power_avg": avg}}
    run = parse_log(make_log((60,), (), node="n"), "n.log")
    import stress_test.baseline as b
    b.run_to_dict = lambda r, *a, **k: data(23.0)
    try:
        res = check_against_baseline(run, data(20.0))
    finally:
        import stress_test.export as e
        b.run_to_dict = e.run_to_dict
    assert res["status"] == "regression" and any("power draw" in w for w in res["warnings"])


def test_run_logs_power_and_exports_it(tmp_path):
    res = run_tool(tmp_path, "--time", "5", "--log", "--export", "both",
                   env_extra={"FAKE_RUN": "3", "FAKE_WATTS": "20"})
    assert res.returncode == 0, res.stdout + res.stderr
    log = next((tmp_path / "logs").rglob("fake-node-5s-*.log")).read_text(encoding="utf-8")
    assert "Power limits: PL1 25 W, PL2 51 W" in log and " W\n" in log and "CPU power (test)" in log
    csv_text = next((tmp_path / "logs").rglob("*.csv")).read_text()
    assert "power_w" in csv_text.splitlines()[0].split(",") and csv_text.splitlines()[0].endswith("gpu_fan_pct")
