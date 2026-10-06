"""Platform compatibility: amd64 (Intel, AMD) works; arm64 / armv7 / "noname" boards - what works today and what is a known gap.

The nodes are MODELLED (see platform_matrix.py): the real probe and hardware scripts run against typical /sys and /proc layouts.
Tests of a known gap are marked `xfail(strict=True)`: when the gap is fixed the test XPASSes and strict mode forces removing the mark.
"""
import pytest

from stress_test import images
import platform_matrix as pm

BY_KEY = {p.key: p for p in pm.PLATFORMS}
ARM = [k for k, p in BY_KEY.items() if p.arch != "amd64"]
X86 = [k for k, p in BY_KEY.items() if p.arch == "amd64"]


@pytest.fixture(scope="module")
def measured():
    return {p.key: pm.measure(p) for p in pm.PLATFORMS}


@pytest.mark.parametrize("key", X86)
def test_x86_platforms_read_temperature_clock_and_cpu_name(measured, key):
    r = measured[key]
    assert r.cpu_temp is not None and r.freq_mhz and r.cpu_line and r.verdict == "OK"


def test_intel_reads_rapl_power_and_amd_does_not_break_without_it(measured):
    assert measured["intel-i7"].power_seen and not measured["amd-a10"].power_seen
    assert measured["amd-a10"].cpu_temp == 55                                # k10temp


@pytest.mark.parametrize("key", ARM)
def test_arm_boards_still_report_clock_memory_and_threads(measured, key):
    r = measured[key]
    p = BY_KEY[key]
    assert r.mem_total_mib and "Threads: %d" % p.threads in r.hw_text
    if p.cpufreq_khz:
        assert r.freq_mhz == p.cpufreq_khz[0] // 1000
    else:
        assert r.freq_mhz is None                                            # bare board: no cpufreq, the column is simply empty


@pytest.mark.parametrize("key", ARM)
def test_arm_boards_do_not_crash_the_scripts_and_power_is_just_absent(measured, key):
    r = measured[key]
    assert r.probe_text.strip() and r.hw_text.startswith("CPU:") and not r.power_seen


def test_every_arm_cpu_gets_a_real_name_from_lscpu(measured):                       # arm64 has no `model name`, armv7 only "ARMv7 Processor"
    for key in ARM:
        assert measured[key].cpu_line.startswith("Cortex-A"), (key, measured[key].cpu_line)


@pytest.mark.parametrize("key", ARM)
def test_arm_findings_say_which_images_are_used(measured, key):
    want = "prebuilt multi-arch images" if images.normalize_arch(BY_KEY[key].arch) in images.PUBLISHED["tools"] else "fallback images"
    assert any(lvl == "INFO" and want in text for lvl, text in measured[key].findings)


@pytest.mark.parametrize("key", ARM)
def test_arm_boards_read_the_cpu_temperature(measured, key):                       # was a known gap before the multi-arch phase 1
    r = measured[key]
    assert r.cpu_temp is not None and r.temps.get("CPU") == r.cpu_temp and r.verdict != "FAIL"


def test_arm_sensor_sources_hwmon_names_and_the_thermal_zone_fallback(measured):
    assert measured["rpi4-arm64"].cpu_temp == 48 and measured["rk3588-noname"].cpu_temp == 47       # cpu_thermal; soc_thermal / bigcore0_thermal (hottest)
    assert measured["bare-arm64"].cpu_temp == 61 and "tz:cpu-thermal" in measured["bare-arm64"].probe_text     # no hwmon at all: thermal_zone0


def test_arm_hardware_text_names_the_cpu_and_the_board(measured):
    assert measured["rpi4-arm64"].cpu_line.startswith("Cortex-A72") and "Raspberry Pi 4 Model B" in measured["rpi4-arm64"].system_line
    assert "Radxa ROCK 5B" in measured["rk3588-noname"].system_line and measured["rpi3-armv7"].cpu_line.startswith("Cortex-A53")      # lscpu, not "ARMv7 Processor"
    assert "Arch: aarch64" in measured["rpi4-arm64"].hw_text and "Arch: x86_64" in measured["intel-i7"].hw_text


def test_sd_cards_are_labelled_and_x86_output_did_not_change(measured):
    assert "| SD/eMMC |" in measured["rpi4-arm64"].hw_text and "| SSD/NVMe |" in measured["intel-i7"].hw_text
    assert measured["intel-i7"].temps == {"CPU": 52, "NVMe": 41} and measured["amd-a10"].temps == {"CPU": 55, "GPU": 50}


def test_a_gpu_or_battery_zone_is_not_taken_for_the_cpu(tmp_path):
    p = pm.Platform("odd", "odd board", "arm64", "processor\t: 0\n", "Cortex-A53", 4, 4, hwmon=[("gpu_thermal", 80000), ("battery", 90000)],
                    thermal_zones=[("gpu-thermal", 85000), ("battery", 88000)], cpufreq_khz=[1000000])
    r = pm.measure(p)
    assert r.cpu_temp is None and r.verdict == "FAIL"                                  # better no temperature than the GPU's one


def test_end_to_end_runs_on_the_fake_cluster_behave_as_expected(tmp_path):
    runs = pm.fake_runs(tmp_path)
    bad = [(r["title"], r["rc"], r["want"]) for r in runs if not r["ok"]]
    assert not bad, bad
    assert all("Arch: arm64" in r["arch_line"] for r in runs if "arm64" in r["title"] and r["rc"] == 0)


def test_report_files_are_written(tmp_path):
    log_path, md_path, results, _online, runs = pm.run_all(True, tmp_path)
    assert log_path.read_text(encoding="utf-8").startswith("PLATFORM COMPATIBILITY") and "| Platform | Arch |" in md_path.read_text(encoding="utf-8")
    assert len(results) == len(pm.PLATFORMS) and len(runs) >= 6
