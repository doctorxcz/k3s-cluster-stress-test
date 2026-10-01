"""GPU fan speed in % (nvidia-smi gives no RPM): smi parsing, log field, live frame, export. No CPU fan is read."""
import io

from stress_test import ui
from stress_test.gpu import format_gpu, parse_gpu_field, parse_smi_csv
from stress_test.logparse import _parse_sample_line


def test_gpu_fan_in_smi_and_log_field():
    r = parse_smi_csv("60, [N/A], [N/A], 1300, 3504, 99, 1800, 2048, P0, 0x0000000000000000, 45")
    assert r.fan_pct == 45
    assert parse_smi_csv("60, [N/A], [N/A], 1300, 3504, 99, 1800, 2048, P0, 0x0, [N/A]").fan_pct is None
    assert parse_smi_csv("60, [N/A], [N/A], 1300, 3504, 99, 1800, 2048, P0, 0x0").fan_pct is None     # older output
    back = parse_gpu_field(format_gpu(r).split("GPU:", 1)[1])
    assert back.fan_pct == 45 and "fan=45%" in format_gpu(r)


def test_log_line_roundtrip_with_gpu_fan_and_old_lines():
    line = ("[10:00:00] CPU: 50%, RAM: 100 MiB (10%) | Temp: CPU: 50°C | Clock: 3000 MHz | Power: 12.5 W"
            " | Ping: 0.40 ms | GPU: 67°C ? 1354MHz 100% 1593/2048MiB thr=0x0 fan=45%")
    sample = _parse_sample_line(line)[1]
    assert sample.gpu_fan_pct == 45 and sample.power_w == 12.5 and sample.ping_ms == 0.4 and sample.gpu_temp == 67
    old = "[10:00:00] CPU: 50%, RAM: 100 MiB (10%) | Temp: CPU: 50°C | Clock: 3000 MHz"
    assert _parse_sample_line(old)[1].gpu_fan_pct is None


def test_gpu_frame_shows_gpu_fan_and_no_cpu_fan(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("STRESS_TEST_COLUMNS", "110")
    g = ui.GpuScreen("n", total=10, stream=io.StringIO())
    g.reading("[1] CPU: 5% | RAM: 1 MiB (1%) | Temp: CPU: 46°C | Clock: 3614 MHz | "
              "GPU: 60°C ? 1300MHz 99% 1800/2048MiB thr=0x0 fan=45%")
    text = "\n".join(g._lines())
    assert "GPU FAN 45 %" in text and "RPM" not in text


def test_no_cpu_fan_anywhere(tmp_path):
    from test_integration import run_tool
    res = run_tool(tmp_path, "--time", "5", "--log", "--export", "both", env_extra={"FAKE_RUN": "3"})
    assert res.returncode == 0 and "Fan" not in res.stdout
    assert "fan_rpm" not in next((tmp_path / "logs").rglob("*.csv")).read_text()
