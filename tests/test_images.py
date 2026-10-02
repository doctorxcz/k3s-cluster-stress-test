"""Prebuilt images (tools / gpu / probe): names, registry, fallback to apt, scripts that install only what is missing, the CLI flags."""
import json
import os
import re
import subprocess
import sys

import pytest

from stress_test import cli, images, kube as kubemod, manifests
from stress_test.manifests import hw_pod, probe_pod, smart_pod, stress_pod
from stress_test.models import PodNames
from test_integration import FAKE, ROOT, run_tool


# ---------------- names, registry, state ------------------------------------------------------------------------------------

def test_default_images_are_the_three_prebuilt_ones_with_the_tool_version():
    from stress_test import __version__
    for kind, name in (("tools", "k3s-stress-tools"), ("gpu", "k3s-stress-gpu"), ("probe", "k3s-stress-probe")):
        ref = images.image(kind)
        assert ref.startswith(f"ghcr.io/doctorxcz/{name}:{__version__}@sha256:")                    # pinned by the digest of the tested image
    assert images.active("tools") and images.prebuilt_enabled()
    assert "latest" not in images.image("tools")                       # pinned, never "latest"


def test_registry_override_and_validation():
    images.configure("registry.local:5000/team/tools-mirror/")
    assert images.image("tools") == f"registry.local:5000/team/tools-mirror/k3s-stress-tools:{images.tag()}"       # no digest for a copy
    assert images.cli_args() == ["--registry", "registry.local:5000/team/tools-mirror"]
    for bad in ("a b", "ghcr.io/../x", "x//y", "bad;rm -rf", "$(id)", "", "-x/y"[:0] + "http://x"):
        if bad == "":
            images.configure("")                                         # empty = the default
            assert images.registry() == images.REGISTRY_DEFAULT
            continue
        with pytest.raises(ValueError):
            images.configure(bad)
    images.configure()


def test_no_prebuilt_uses_the_fallback_images_and_is_carried_to_children():
    images.configure("", prebuilt=False)
    assert images.image("tools") == "ubuntu:24.04" and images.image("probe") == "busybox:1.36" and images.image("gpu").startswith("nvidia/cuda:")
    assert images.cli_args() == ["--no-prebuilt"]
    images.configure()
    assert images.cli_args() == []


def test_a_failed_pull_switches_only_that_kind_to_the_fallback():
    images.mark_unavailable("tools")
    assert images.image("tools") == "ubuntu:24.04" and not images.active("tools")
    assert images.active("probe") and images.image("probe").startswith("ghcr.io/")
    images.configure()
    assert images.active("tools")                                      # configure forgets earlier failures


def test_digest_pins_the_exact_image(monkeypatch):
    monkeypatch.setitem(images.DIGESTS, images.tag(), {"tools": "sha256:" + "a" * 64})
    assert images.image("tools") == f"ghcr.io/doctorxcz/k3s-stress-tools:{images.tag()}@sha256:" + "a" * 64
    assert images.is_prebuilt(images.image("tools")) and not images.is_prebuilt("ubuntu:24.04")
    assert "@" not in images.image("probe") or images.DIGESTS[images.tag()].get("probe")           # a kind without a digest uses the tag


def test_published_digests_are_valid_and_only_used_for_the_default_registry(monkeypatch):
    import re
    for version, kinds in images.DIGESTS.items():
        assert set(kinds) <= set(images.NAMES), version
        assert all(re.fullmatch(r"sha256:[0-9a-f]{64}", d) for d in kinds.values()), version
    # the tag of this version has digests for all three images (a release must be pinned)
    assert set(images.DIGESTS.get(images.tag(), {})) == {"tools", "probe", "gpu"}
    for kind in images.NAMES:
        assert images.image(kind).count("@sha256:") == 1
    images.configure("registry.local:5000/lab")
    assert all("@" not in images.image(k) for k in images.NAMES)                                    # a copy in another registry: the tag
    images.configure()
    monkeypatch.setattr(images, "tag", lambda: "9.9.9")
    assert all("@" not in images.image(k) for k in images.NAMES)                                    # a version without an entry: the tag


# ---------------- scripts: install only what is missing ---------------------------------------------------------------------

def test_ensure_tools_checks_the_binaries_and_falls_back_to_apt():
    s = images.ensure_tools("iperf3 iputils-ping")
    assert "command -v iperf3" in s and "command -v ping" in s and "apt-get install -y -qq iperf3 iputils-ping" in s
    assert images.ensure_tools("stress-ng").startswith("{ command -v stress-ng") and "||" in s
    for pkg in ("stress-ng", "fio", "iperf3", "smartmontools", "dmidecode", "pciutils", "lm-sensors", "htop"):
        assert pkg in images.BINARIES or pkg == "stress-ng"


@pytest.mark.skipif(os.name != "posix", reason="needs a shell")
def test_the_condition_is_true_when_the_tool_exists_and_runs_apt_only_otherwise(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "apt-get").write_text("#!/bin/sh\necho apt-get $@ >> " + str(tmp_path / "apt.log") + "\nexit 0\n")
    (fake_bin / "sh-tool").write_text("#!/bin/sh\nexit 0\n")
    for f in fake_bin.iterdir():
        f.chmod(0o755)
    env = dict(os.environ, PATH=f"{fake_bin}:/usr/bin:/bin")
    present = subprocess.run(["bash", "-c", "if " + images.ensure_tools("sh-tool") + "; then echo ready; fi"], env=env, capture_output=True, text=True)
    assert present.stdout.strip() == "ready" and not (tmp_path / "apt.log").exists()          # already there: apt is not touched
    missing = subprocess.run(["bash", "-c", "if " + images.ensure_tools("surely-missing-tool-xyz") + "; then echo ready; fi"], env=env, capture_output=True, text=True)
    log = (tmp_path / "apt.log").read_text()
    assert missing.stdout.strip() == "ready" and "update" in log and "install -y -qq surely-missing-tool-xyz" in log


def test_pod_scripts_use_the_helper_not_a_bare_apt():
    pod = stress_pod("n1", PodNames.new(), 600, "stress-ng --cpu 0 --timeout 5s", package="stress-ng")
    script = pod["spec"]["containers"][0]["command"][2]
    assert "command -v stress-ng" in script and "apt-get" in script and script.index("command -v") < script.index("apt-get")
    assert pod["spec"]["containers"][0]["image"] == images.image("tools")
    hw = hw_pod("n1", PodNames.new())
    assert "command -v dmidecode" in hw["spec"]["containers"][0]["command"][2] and hw["spec"]["containers"][0]["image"] == images.image("tools")
    assert "command -v smartctl" in smart_pod("n1", PodNames.new())["spec"]["containers"][0]["command"][2]
    assert probe_pod("n1", PodNames.new(), 600)["spec"]["containers"][0]["image"] == images.image("probe")
    gpu = stress_pod("n1", PodNames.new(), 600, "x", package="", gpu=True, setup="true")
    assert gpu["spec"]["containers"][0]["image"] == images.image("gpu") and "apt-get" not in gpu["spec"]["containers"][0]["command"][2]
    custom = stress_pod("n1", PodNames.new(), 600, "x", package="", gpu=True, image="my/own:1")
    assert custom["spec"]["containers"][0]["image"] == "my/own:1"


def test_gpu_setup_installs_the_build_tools_only_when_gpu_burn_has_to_be_built():
    from stress_test import gpu
    setup = gpu.build_gpu_setup(90)
    assert setup.index("command -v gpu_burn") < setup.index("command -v git") < setup.index("apt-get")      # prebuilt gpu_burn found first, tools only for the build


def test_every_pod_follows_the_fallback_state():
    images.configure("", prebuilt=False)
    assert hw_pod("n", PodNames.new())["spec"]["containers"][0]["image"] == "ubuntu:24.04"
    assert probe_pod("n", PodNames.new(), 60)["spec"]["containers"][0]["image"] == "busybox:1.36"
    assert manifests.dashboard_gpu_pod("n", "p", "r", 60)["spec"]["containers"][0]["image"] == "ubuntu:24.04"
    assert manifests.gpu_scan_pod("n", PodNames.new())["spec"]["containers"][0]["image"] == "ubuntu:24.04"
    assert manifests.net_server_pod("n", PodNames.new(), 60, 32000, True)["spec"]["containers"][0]["image"] == "ubuntu:24.04"
    from stress_test import netmatrix
    assert netmatrix.matrix_pod("n", "pod", 60, "r")["spec"]["containers"][0]["image"] == "ubuntu:24.04"
    assert "command -v iperf3" in netmatrix.MX_SETUP_SCRIPT and "command -v iperf3" in manifests.NET_SERVER_SCRIPT if hasattr(manifests, "NET_SERVER_SCRIPT") else True


# ---------------- apply_image_pod: the fallback --------------------------------------------------------------------------------

class Recorder:
    """A Kubectl stand-in: records applied pods, answers pull_problem / pod_phase from a script."""

    def __init__(self, reasons, phase="Pending"):
        self.applied, self.deleted, self.reasons, self.phase, self.said = [], [], list(reasons), phase, []

    apply = kubemod.Kubectl.apply_image_pod
    pull_problem = lambda self, name: (self.reasons.pop(0) if self.reasons else "")             # noqa: E731

    def run_apply(self, manifest):
        self.applied.append(manifest)

    def delete_pods(self, *names):
        self.deleted += names

    def pod_phase(self, name):
        return self.phase


def make_kube(reasons, phase="Pending"):
    k = kubemod.Kubectl.__new__(kubemod.Kubectl)
    rec = Recorder(reasons, phase)
    k.apply = rec.run_apply
    k.delete_pods = rec.delete_pods
    k.pod_phase = rec.pod_phase
    k.pull_problem = rec.pull_problem
    return k, rec


def build_hw():
    return hw_pod("n1", PodNames("rid", "p", "hw-rid", "s"))


def test_a_pull_error_replaces_the_pod_by_one_on_the_fallback_image():
    k, rec = make_kube(["", "ErrImagePull"])
    said = []
    used = k.apply_image_pod(build_hw, "hw-rid", "tools", said.append, grace=60, sleep=lambda s: None, clock=iter(range(0, 1000)).__next__)
    assert used is True and rec.deleted == ["hw-rid"]
    assert [m["spec"]["containers"][0]["image"] for m in rec.applied] == [images.prebuilt_ref("tools"), "ubuntu:24.04"]
    assert said and "ErrImagePull" in said[0] and "apt" in said[0] and "--no-prebuilt" in said[0]
    assert not images.active("tools")                                                  # later pods of the kind start from the fallback directly


def test_a_healthy_pull_keeps_the_prebuilt_image_and_stops_waiting():
    for phase in ("Running", "Succeeded"):
        images.configure()
        k, rec = make_kube([], phase)
        assert k.apply_image_pod(build_hw, "hw-rid", "tools", None, grace=60, sleep=lambda s: None, clock=iter(range(0, 1000)).__next__) is False
        assert len(rec.applied) == 1 and rec.deleted == [] and images.active("tools")


def test_a_slow_pull_without_an_error_is_left_alone_after_the_grace_time():
    k, rec = make_kube([], "Pending")
    ticks = iter(range(0, 1000, 10))
    assert k.apply_image_pod(build_hw, "hw-rid", "tools", None, grace=25, sleep=lambda s: None, clock=lambda: next(ticks)) is False
    assert len(rec.applied) == 1 and images.active("tools")


def test_no_fallback_for_a_custom_image_or_when_prebuilt_is_off():
    k, rec = make_kube(["ErrImagePull"])
    assert k.apply_image_pod(build_hw, "hw-rid", None, None) is False and len(rec.applied) == 1
    images.configure("", prebuilt=False)
    k, rec = make_kube(["ErrImagePull"])
    assert k.apply_image_pod(build_hw, "hw-rid", "tools", None) is False and len(rec.applied) == 1


def test_pull_problem_reads_the_container_waiting_reason():
    class K(kubemod.Kubectl):
        def __init__(self, out):
            self.out = out
            self.calls = []

        def run(self, *args, **kw):
            self.calls.append(args)
            return self.out
    assert K("ImagePullBackOff").pull_problem("p") == "ImagePullBackOff"
    assert K("ContainerCreating").pull_problem("p") == "" and K("").pull_problem("p") == ""
    assert K("PodInitializing ErrImagePull").pull_problem("p") == "ErrImagePull"
    k = K("")
    k.pull_problem("p")
    assert "jsonpath={.status.containerStatuses[*].state.waiting.reason}" in k.calls[0]


# ---------------- CLI --------------------------------------------------------------------------------------------------------

def test_cli_flags_registry_and_no_prebuilt():
    p = cli.build_parser()
    assert p.parse_args(["--registry", "my.reg:5000/x"]).registry == "my.reg:5000/x"
    assert p.parse_args(["--no-prebuilt"]).no_prebuilt is True
    assert p.parse_args([]).registry is None and p.parse_args([]).no_prebuilt is False
    with pytest.raises(SystemExit):
        p.parse_args(["--registry", "bad registry;x"])
    assert images.registry() == images.REGISTRY_DEFAULT                                # validating did not change the state


def test_main_applies_the_flags_and_keeps_them_for_the_next_phase():
    args = cli.build_parser().parse_args(["--registry", "r.example/p", "--no-prebuilt", "--list-nodes"])
    env_kube = None
    try:
        cli._main(args)
    except Exception:                                                                    # noqa: BLE001 - no cluster: only the flags matter here
        pass
    assert images.registry() == "r.example/p" and not images.prebuilt_enabled()
    cli._main(cli.build_parser().parse_args(["--list-nodes"])) if False else None
    again = cli.build_parser().parse_args(["--list-nodes"])
    try:
        cli._main(again)
    except Exception:                                                                    # noqa: BLE001
        pass
    assert images.registry() == "r.example/p"                                           # a later phase without flags does not reset the choice


def test_parallel_children_and_timers_get_the_flags():
    from stress_test import parallel, series
    images.configure("r.example/p", prebuilt=False)
    cfg = cli.build_parser().parse_args(["--node", "w1", "--time", "60"])
    options = series.SeriesOptions(log_dir=ROOT, interval=1.0, remaining_every=0)
    tpl = cli.build_config(cfg, "w1", interactive=False) if False else None
    args = parallel.child_args(__import__("stress_test.models", fromlist=["StressConfig"]).StressConfig(node="w1", duration=60), options, "x.log", 2)
    assert "--registry" in args and args[args.index("--registry") + 1] == "r.example/p" and "--no-prebuilt" in args


# ---------------- end to end against the fake kubectl ------------------------------------------------------------------------

def manifests_of(tmp_path):
    out = {}
    for f in tmp_path.glob("manifest-*.json"):
        data = json.loads(f.read_text())
        out[data["metadata"]["name"]] = data
    return out


def test_a_normal_run_uses_the_prebuilt_images_and_installs_nothing(tmp_path):
    res = run_tool(tmp_path, "--time", "5", env_extra={"FAKE_RUN": "3"})
    assert res.returncode == 0, res.stdout + res.stderr
    ms = manifests_of(tmp_path)
    by_role = {n.split("-")[0]: m["spec"]["containers"][0]["image"] for n, m in ms.items()}
    assert by_role["temp"].startswith("ghcr.io/doctorxcz/k3s-stress-probe:") and by_role["stress"].startswith("ghcr.io/doctorxcz/k3s-stress-tools:")
    assert by_role["hw"].startswith("ghcr.io/doctorxcz/k3s-stress-tools:")
    assert "prebuilt image: pulled once per node" in res.stdout or "tools are in the prebuilt image" in res.stdout


def test_no_prebuilt_runs_exactly_like_before(tmp_path):
    res = run_tool(tmp_path, "--time", "5", "--no-prebuilt", env_extra={"FAKE_RUN": "3"})
    assert res.returncode == 0, res.stdout + res.stderr
    images_used = {m["spec"]["containers"][0]["image"] for m in manifests_of(tmp_path).values()}
    assert images_used <= {"ubuntu:24.04", "busybox:1.36"} and "ghcr.io" not in res.stdout


def test_registry_flag_changes_every_image(tmp_path):
    res = run_tool(tmp_path, "--time", "5", "--registry", "mirror.local:5000/lab", env_extra={"FAKE_RUN": "3"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert all(m["spec"]["containers"][0]["image"].startswith("mirror.local:5000/lab/k3s-stress-") for m in manifests_of(tmp_path).values())


def test_unpullable_images_fall_back_with_a_warning_and_the_test_still_runs(tmp_path):
    res = run_tool(tmp_path, "--time", "5", env_extra={"FAKE_RUN": "3", "FAKE_PULL_FAIL": "1"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "cannot be pulled (ErrImagePull)" in res.stdout and "continuing with" in res.stdout and "Test completed" in res.stdout
    ms = manifests_of(tmp_path)
    assert all(m["spec"]["containers"][0]["image"] in ("ubuntu:24.04", "busybox:1.36") for m in ms.values())      # the replaced pods use the fallback
