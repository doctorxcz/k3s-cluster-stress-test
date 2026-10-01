"""GPU profile (NVIDIA, gpu-burn): units - nvidia-smi parsing, guard, log field, summary, commands, manifest, config."""
import pytest

from stress_test import cli, gpu
from stress_test.gpu import (GpuGuard, GpuReading, build_gpu_command, build_gpu_setup, format_gpu, gpu_state,
                             parse_gpu_field, parse_smi_csv, summarize, summary_lines, throttle_names)
from stress_test.manifests import build_stress_command, stress_pod
from stress_test.models import PROFILE_GPU, PodNames, Sample, StressConfig
from stress_test.parallel import child_args
from stress_test import series


def _cfg(**kw):
    base = dict(node="n", profile=PROFILE_GPU, duration=60)
    base.update(kw)
    return StressConfig(**base)


def sample(temp=60, util=100, sm=1300, power=50.0, thr=0, phase="test", mem=1800):
    return Sample(t=0.0, phase=phase, cpu_temp=40, freq_mhz=3000, cpu_pct=5.0, mem_used_mib=1000, mem_used_pct=10.0,
                  gpu_temp=temp, gpu_power_w=power, gpu_sm_mhz=sm, gpu_util_pct=util, gpu_mem_mib=mem, gpu_throttle=thr)


# ---- nvidia-smi parsing ------------------------------------------------------------------------

def test_parse_smi_csv_full_line():
    r = parse_smi_csv("62, 55.20, 120.00, 1300, 3504, 99, 1800, 2048, P0, 0x0000000000000004\n")
    assert (r.temp, r.power_w, r.power_limit_w, r.sm_mhz, r.mem_mhz) == (62, 55.2, 120.0, 1300, 3504)
    assert (r.util_pct, r.mem_used_mib, r.mem_total_mib, r.pstate, r.throttle) == (99, 1800, 2048, "P0", 4)


def test_parse_smi_csv_na_values_like_quadro_p620():
    r = parse_smi_csv("55, [N/A], [N/A], 1100, 3004, 100, 1700, 2048, P0, 0x0")
    assert r.temp == 55 and r.power_w is None and r.power_limit_w is None and r.throttle == 0
    r = parse_smi_csv("55, [Not Supported], [N/A], [N/A], 3004, 100, 1700, 2048, [N/A], [N/A]")
    assert r.sm_mhz is None and r.pstate == "" and r.throttle is None


def test_parse_smi_csv_garbage_and_decimal_mask():
    assert parse_smi_csv("") is None and parse_smi_csv("NVIDIA-SMI has failed") is None
    assert parse_smi_csv("a, b, c") is None
    assert parse_smi_csv("x, 1, 2, 3, 4, 5, 6, 7, P0, 12").throttle == 12      # decimal mask is accepted
    assert parse_smi_csv("x, 1, 2, 3, 4, 5, 6, 7, P0, zz").throttle is None
    r = parse_smi_csv("noise line\n60, 1, 2, 3, 4, 5, 6, 7, P0, 0x0\nsecond gpu, 1,2,3,4,5,6,7,P0,0x0")
    assert r.temp == 60                                                          # first valid line wins


def test_throttle_names_and_bad_filter():
    assert throttle_names(None) == [] and throttle_names(0) == []
    assert throttle_names(0x1) == ["gpu_idle"] and throttle_names(0x1, only_bad=True) == []
    assert throttle_names(0x24) == ["sw_power_cap", "sw_thermal"]
    assert throttle_names(0x1 | 0x40, only_bad=True) == ["hw_thermal"]


def test_smi_query_has_both_throttle_field_names():
    assert "clocks_throttle_reasons.active" in gpu.SMI_QUERY and "clocks_event_reasons.active" in gpu.SMI_QUERY
    assert "||" in gpu.SMI_QUERY                      # the new name is a fallback for new drivers


# ---- guard -------------------------------------------------------------------------------------

def test_guard_needs_two_hot_readings_in_a_row():
    g = GpuGuard(85)
    assert g.update(90) is False and g.update(84) is False        # reset
    assert g.update(85) is False and g.update(86) is True
    g2 = GpuGuard(85)
    assert g2.update(None) is False and g2.update(95) is False and g2.update(None) is False and g2.update(95) is True


def test_guard_limit_is_inclusive():
    g = GpuGuard(80, consecutive=1)
    assert g.update(79) is False and g.update(80) is True


# ---- log field ---------------------------------------------------------------------------------

def test_format_parse_roundtrip():
    r = GpuReading(temp=61, power_w=48.5, sm_mhz=1250, util_pct=99, mem_used_mib=1700, throttle=0x20)
    text = format_gpu(r)
    assert text.startswith(" | GPU: 61°C 48.5W 1250MHz 99% 1700MiB thr=0x20") and "HOT" not in text
    back = parse_gpu_field(text.split("GPU:", 1)[1])
    assert (back.temp, back.power_w, back.sm_mhz, back.util_pct, back.mem_used_mib, back.throttle) == \
           (61, 48.5, 1250, 99, 1700, 0x20)


def test_format_parse_roundtrip_with_none_and_hot():
    r = GpuReading(temp=83, power_w=None, sm_mhz=None, util_pct=100, mem_used_mib=None, throttle=None)
    text = format_gpu(r)
    assert "83°C ? ? 100% ? thr" in text and "thr=0x0" in text and text.endswith("⚠️ GPU HOT!")
    back = parse_gpu_field(text.split("GPU:", 1)[1])
    assert back.temp == 83 and back.power_w is None and back.sm_mhz is None and back.mem_used_mib is None
    unknown = parse_gpu_field(format_gpu(GpuReading(temp=None)).split("GPU:", 1)[1])
    assert unknown.temp is None
    assert format_gpu(None) == "" and parse_gpu_field("garbage") is None


def test_hot_warning_threshold():
    assert "HOT" not in format_gpu(GpuReading(temp=74)) and "HOT" in format_gpu(GpuReading(temp=75))


# ---- summary -----------------------------------------------------------------------------------

def test_summarize_none_without_gpu_data():
    assert summarize([]) is None
    plain = Sample(t=0.0, phase="test", cpu_temp=40, freq_mhz=3000, cpu_pct=5.0, mem_used_mib=1, mem_used_pct=1.0)
    assert summarize([plain]) is None
    assert "no readings" in summary_lines([plain])[0]
    assert summarize([sample(phase="cooldown")]) is None


def test_summarize_ok_run():
    s = summarize([sample(60), sample(64, power=52.0), sample(70, util=0, sm=139, thr=1)])
    assert s.max_temp == 70 and s.verdict == "ok"
    assert s.clock_min == 1300 and s.clock_max == 1300        # idle reading (util 0) ignored
    assert s.throttle_pct == 0                                  # gpu_idle is harmless


def test_summarize_warnings():
    hot = summarize([sample(82), sample(83)])
    assert hot.verdict == "warn" and any("83" in f for f in hot.findings)
    thermal = summarize([sample(70, thr=0x20)] * 3)
    assert thermal.thermal_pct == 100 and any("Thermal" in f for f in thermal.findings) and "sw_thermal" in thermal.reasons
    power = summarize([sample(60, thr=0x4)] * 4)
    assert any("limited" in f for f in power.findings)
    lazy = summarize([sample(60, util=10)] * 3)
    assert any("utilization" in f for f in lazy.findings)


def test_summary_lines_power_na_and_no_crash_on_none():
    lines = "\n".join(summary_lines([sample(60, power=None), sample(61, power=None)]))
    assert "N/A" in lines and "GPU temperature:          max 61" in lines
    odd = Sample(t=0.0, phase="test", cpu_temp=None, freq_mhz=None, cpu_pct=None, mem_used_mib=None,
                 mem_used_pct=None, gpu_temp=None, gpu_sm_mhz=1000)
    assert summary_lines([odd])                                  # temp None, util None, throttle None


# ---- state / commands ----------------------------------------------------------------------------

def test_gpu_state():
    assert gpu_state("CPU: x", 1) == ("ok", "")
    assert gpu_state("GPU: NVIDIA Corporation GP107GL", 0)[0] == "no-plugin"
    assert gpu_state("GPU: Intel UHD", 0)[0] == "no-gpu"
    assert "nvidia.com/gpu" in gpu_state("", 0)[1]


def test_gpu_command_and_setup():
    cmd = build_gpu_command(90, 80, True)
    assert '"$GPU_BIN" -m 80% -d 90' in cmd and "GPU-DONE" in cmd and "GPU-FAILED" in cmd and "FAULTY" in cmd
    assert "-d" not in build_gpu_command(90, 80).split("> /tmp/gb.out")[0].replace("-m 80%", "")
    assert build_stress_command(_cfg(duration=90, gpu_mem_pct=80, gpu_double=True), None) == cmd
    setup = build_gpu_setup(80)
    assert "git fetch" in setup and "3ead140434da" in setup and "COMPUTE=" in setup and setup.rstrip().endswith('-x "$GPU_BIN" ]')


# ---- manifest ------------------------------------------------------------------------------------

def test_stress_pod_gpu_manifest_is_unprivileged():
    pod = stress_pod("n", PodNames.new(), 900, "echo hi", package="git make g++",
                     image="nvidia/cuda:12.9.1-devel-ubuntu24.04", gpu=True, setup=build_gpu_setup())
    spec = pod["spec"]
    c = spec["containers"][0]
    assert spec["runtimeClassName"] == "nvidia" and c["resources"]["limits"]["nvidia.com/gpu"] == 1
    assert c["image"].startswith("nvidia/cuda") and "git make g++" in c["command"][-1]
    assert "hostPath" not in str(pod) and "privileged" not in str(pod).lower().replace("allowprivilegeescalation", "")
    assert c["securityContext"]["allowPrivilegeEscalation"] is False
    assert not spec.get("hostNetwork") and spec["nodeName"] == "n"
    plain = stress_pod("n", PodNames.new(), 900, "echo hi")
    assert "runtimeClassName" not in plain["spec"] and "nvidia.com/gpu" not in plain["spec"]["containers"][0]["resources"]["limits"]


def test_gpu_image_stays_one_json_field_not_shell():
    evil = "x; rm -rf /"
    pod = stress_pod("n", PodNames.new(), 900, "echo hi", image=evil, gpu=True)
    assert pod["spec"]["containers"][0]["image"] == evil and evil not in pod["spec"]["containers"][0]["command"][-1]
    with pytest.raises(ValueError):                  # ... and validate() refuses such a name anyway
        _cfg(gpu_image=evil).validate()


# ---- config validation ----------------------------------------------------------------------------

def test_validate_ranges():
    _cfg().validate()
    _cfg(duration=20, gpu_max_temp=50, gpu_mem_pct=10).validate()
    _cfg(duration=3600, gpu_max_temp=95, gpu_mem_pct=95, gpu_image="reg.local:5000/gpu/burn:1.0").validate()
    for kw in ({"duration": 19}, {"duration": 3601}, {"gpu_max_temp": 49}, {"gpu_max_temp": 96},
               {"gpu_mem_pct": 9}, {"gpu_mem_pct": 96}, {"ram_pct": 10}, {"hdd": True}, {"gpu_image": "a b"},
               {"gpu_image": "$(id)"}, {"gpu_image": "-flag"}):
        with pytest.raises(ValueError):
            _cfg(**kw).validate()


def test_gpu_master_limits_no_cpu_cap():
    cfg = _cfg(cpu_load=100)
    assert all("CPU load" not in m for m in cfg.apply_master_limits()) and cfg.cpu_load == 100


def test_cli_parses_gpu_flags_and_menu_item(monkeypatch):
    a = cli.build_parser().parse_args(["--node", "n", "--profile", "gpu", "--gpu-max-temp", "80", "--gpu-mem-pct", "50",
                                       "--gpu-double", "--gpu-image", "img:1", "--time", "30", "--no-background",
                                       "--notes", "", "--non-interactive"])
    cfg = cli.build_config(a, "n", master_mode=False)
    assert (cfg.gpu, cfg.gpu_max_temp, cfg.gpu_mem_pct, cfg.gpu_double, cfg.gpu_image, cfg.duration) == \
           (True, 80, 50, True, "img:1", 30)
    cfg.validate()
    monkeypatch.setattr("builtins.input", lambda _p="": "6")
    assert cli.ask_profile() == PROFILE_GPU


def test_child_args_gpu_roundtrip():
    """--parallel/--workers start a subprocess per node: it must keep the GPU profile and its parameters."""
    opts = series.SeriesOptions(log_dir=None, interval=5.0, remaining_every=3)
    cfg = _cfg(duration=45, gpu_max_temp=80, gpu_mem_pct=70, gpu_double=True, gpu_image="img:2")
    a = child_args(cfg, opts, "/l/w.log", 1)
    child = cli.build_config(cli.build_parser().parse_args(a), "w", master_mode=False)
    assert child.gpu and (child.duration, child.gpu_max_temp, child.gpu_mem_pct, child.gpu_double, child.gpu_image) == \
           (45, 80, 70, True, "img:2")


def test_zero_gpu_values_are_refused_not_replaced_by_defaults():
    for flag in ("--gpu-max-temp", "--gpu-mem-pct"):
        a = cli.build_parser().parse_args(["--node", "n", "--profile", "gpu", flag, "0", "--no-background",
                                           "--notes", "", "--non-interactive"])
        with pytest.raises(ValueError):
            cli.build_config(a, "n", master_mode=False).validate()


def test_mem_total_roundtrip_and_old_format():
    from stress_test.gpu import GpuReading, format_gpu, parse_gpu_field
    r = GpuReading(temp=60, sm_mhz=1300, util_pct=99, mem_used_mib=1800, mem_total_mib=2048, throttle=0)
    back = parse_gpu_field(format_gpu(r).split("GPU:", 1)[1])
    assert back.mem_used_mib == 1800 and back.mem_total_mib == 2048
    assert parse_gpu_field("60°C 55.0W 1300MHz 100% 1800MiB thr=0x0").mem_used_mib == 1800      # logs written before


def test_parse_gpu_info_handles_na():
    from stress_test.gpu import parse_gpu_info
    lines = parse_gpu_info("Quadro P620, 580.178.04, 2048, [N/A], 1island, 3504, 3, 16, 6.1, 86.07.3C.00.0B")
    assert lines[0] == "GPU: Quadro P620" and "Driver: 580.178.04" in lines and "Power limit: N/A" in lines
    assert "Memory: 2048 MiB" in lines and "PCIe (max): gen 3" in lines and parse_gpu_info("garbage") == []
