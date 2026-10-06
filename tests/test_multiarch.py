"""Multi-architecture support (phase 1): the tool finds out which node is amd64 and which arm64 and picks images, sensors and names accordingly."""
import json
import os
import subprocess
import sys

import pytest

from stress_test import images, parsing
from stress_test.manifests import PROBE_SCRIPT, hw_pod, probe_pod, smart_pod, stress_pod
from stress_test.models import PodNames
from test_integration import FAKE, ROOT, run_tool


@pytest.fixture(autouse=True)
def clean():
    images.configure()
    images.forget_nodes()
    yield
    images.configure()
    images.forget_nodes()


@pytest.mark.parametrize("raw, want", [("amd64", "amd64"), ("x86_64", "amd64"), ("AMD64", "amd64"), ("arm64", "arm64"), ("aarch64", "arm64"),
                                       ("arm", "armv7"), ("armv7l", "armv7"), ("riscv64", "riscv64"), ("?", "unknown"), ("", "unknown"), ("weird", "unknown")])
def test_architecture_names_are_normalised(raw, want):
    assert images.normalize_arch(raw) == want


def test_reading_a_node_remembers_its_architecture():
    parsing.node_from_json({"metadata": {"name": "pi4"}, "status": {"nodeInfo": {"architecture": "arm64"}, "conditions": []}})
    parsing.node_from_json({"metadata": {"name": "dell"}, "status": {"nodeInfo": {"architecture": "amd64"}, "conditions": []}})
    assert images.node_arch("pi4") == "arm64" and images.node_arch("dell") == "amd64"
    assert images.node_arch("never-read") == "amd64" and images.node_arch() == "amd64"          # unknown node: what the tool did before


def test_arm64_and_amd64_nodes_get_the_multi_arch_prebuilt_images_and_armv7_the_fallback():
    images.note_node("pi4", "aarch64")
    images.note_node("dell", "x86_64")
    images.note_node("pi3", "arm")                                   # 32-bit: no prebuilt image for it
    names = PodNames.new()
    tools = lambda node: hw_pod(node, names)["spec"]["containers"][0]["image"]                 # noqa: E731
    assert tools("dell").startswith("ghcr.io/doctorxcz/k3s-stress-tools:") and tools("pi4") == tools("dell")             # the same index: the runtime picks arm64
    assert "@sha256:" in tools("pi4")                                                              # pinned by the digest of the index
    assert tools("pi3") == images.FALLBACK["tools"] and smart_pod("pi3", names)["spec"]["containers"][0]["image"] == "ubuntu:24.04"
    assert probe_pod("pi4", names, 100)["spec"]["containers"][0]["image"].startswith("ghcr.io/doctorxcz/k3s-stress-probe:")
    assert probe_pod("pi3", names, 100)["spec"]["containers"][0]["image"] == "busybox:1.36"
    assert stress_pod("pi3", names, 100, "stress-ng --cpu 0", 512, package="stress-ng")["spec"]["containers"][0]["image"] == "ubuntu:24.04"


def test_the_gpu_image_keeps_its_own_tag_and_is_amd64_only():
    images.note_node("dell", "amd64")
    images.note_node("pi4", "arm64")
    assert images.tag("gpu") == "1.18.0" and images.tag("tools") == images.IMAGE_TAG and ":1.18.0@sha256:" in images.image("gpu", "dell")
    assert images.image("gpu", "pi4") == images.FALLBACK["gpu"]


def test_an_unknown_architecture_is_never_given_the_amd64_image():
    images.note_node("odd", "mips64")
    assert images.image("tools", "odd") == images.FALLBACK["tools"] and not images.active("tools", "odd")


def test_a_pull_failure_on_one_architecture_does_not_switch_the_prebuilt_image_off_for_the_other():
    images.note_node("dell", "amd64")
    images.note_node("pi4", "arm64")
    assert images.active("tools", "pi4") and images.active("tools", "dell")
    images.mark_unavailable("tools", "pi4")
    assert not images.active("tools", "pi4") and images.active("tools", "dell")
    images.mark_unavailable("tools", "dell")
    assert not images.active("tools", "dell")


def test_no_prebuilt_still_wins_everywhere():
    images.note_node("dell", "amd64")
    images.configure(prebuilt=False)
    assert images.image("tools", "dell") == images.FALLBACK["tools"]


# ---------------- sensors ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["cpu_thermal", "soc_thermal", "bigcore0_thermal", "littlecore_thermal", "tz:cpu-thermal", "tz:soc-thermal", "x86_pkg_temp",
                                  "coretemp", "k10temp", "zenpower"])
def test_cpu_like_sensor_names_are_the_cpu(name):
    assert parsing.label_for_sensor(name) == "CPU"


@pytest.mark.parametrize("name, label", [("amdgpu", "GPU"), ("nvme", "NVMe"), ("gpu_thermal", "HDD"), ("tz:gpu-thermal", "HDD"), ("battery", "HDD"),
                                         ("ddr_thermal", "HDD"), ("npu_thermal", "HDD"), ("rpi_volt", "HDD"), ("acpitz", "HDD")])
def test_other_sensors_are_not_taken_for_the_cpu(name, label):
    assert parsing.label_for_sensor(name) == label


def test_probe_script_knows_the_arm_sensor_names_and_the_thermal_zone_fallback():
    for word in ("*cpu*", "*soc*", "*core*", "thermal_zone", "tz:"):
        assert word in PROBE_SCRIPT


# ---------------- the whole tool with nodes of different architectures --------------------------------------------------------

def test_mixed_cluster_every_node_pod_gets_an_image_for_its_architecture(tmp_path):
    nodes = json.dumps([{"name": "x86-node", "arch": "amd64"}, {"name": "pi-node", "arch": "arm"}])
    env = dict(os.environ, KUBECTL=str(FAKE), FAKE_STATE=str(tmp_path), PYTHONPATH=str(ROOT), STRESS_TEST_RUNNING_DIR=str(tmp_path / "running"),
               FAKE_NODES=nodes, FAKE_RUN="2")
    cmd = [sys.executable, "-m", "stress_test", "--nodes", "x86-node,pi-node", "--no-parallel", "--non-interactive", "--time", "5", "--yes",
           "--allow-no-sensor", "--interval", "0.5", "--cooldown", "0", "--log-dir", str(tmp_path / "logs")]
    res = subprocess.run(cmd, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180)
    assert res.returncode == 0, res.stdout + res.stderr
    seen = {}
    for path in tmp_path.glob("manifest-*.json"):
        spec = json.loads(path.read_text())
        seen.setdefault(spec["spec"]["nodeName"], set()).add(spec["spec"]["containers"][0]["image"])
    assert seen.get("x86-node") and seen.get("pi-node"), seen
    assert all("/k3s-stress-" in i for i in seen["x86-node"]), seen
    assert all("/k3s-stress-" not in i for i in seen["pi-node"]), seen


def test_gpu_test_on_an_arm_node_is_refused_clearly(tmp_path):
    res = run_tool(tmp_path, "--profile", "gpu", "--time", "30", "--allow-no-sensor", env_extra={"FAKE_ARCH": "arm64", "FAKE_GPU": "1"})
    assert res.returncode != 0 and "amd64 nodes only" in res.stdout


def test_list_nodes_shows_the_architecture(tmp_path):
    nodes = json.dumps([{"name": "x86-node", "arch": "amd64"}, {"name": "pi-node", "arch": "arm64"}])
    res = run_tool(tmp_path, "--list-nodes", env_extra={"FAKE_NODES": nodes})
    assert "amd64" in res.stdout and "arm64" in res.stdout
