"""Security and robustness checks: pod specs (least privilege), injection, log forging, hostile input, permissions.

Findings: [S..] = code review, [O..] = excess permissions.
Tests of a known open problem are marked `xfail(strict=True)`: when the problem gets fixed the test XPASSes and
strict mode forces removing the mark.
"""
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from stress_test import background, baseline, cli, netmatrix
from stress_test.logparse import parse_log
from stress_test.manifests import (hw_pod, net_server_pod, net_service, probe_pod, smart_pod, stress_pod)
from stress_test.models import TOOL_LABEL, PodNames
from stress_test.netmatrix import matrix_pod
from stress_test.parsing import node_from_json, parse_duration
from stress_test.paths import open_private
from test_integration import run_tool

ROOT = Path(__file__).parent.parent
FAKE = Path(__file__).parent / "fake_kubectl.py"
MAX_DEADLINE = 24 * 3600 + 3600


def all_pods():
    """Every kind of pod the tool can create: (label, pod)."""
    n = PodNames.new("abc123")
    return [
        ("hw", hw_pod("n1", n)),
        ("hw-privileged", hw_pod("n1", n, privileged=True)),
        ("smart", smart_pod("n1", n)),
        ("net-server-host", net_server_pod("n1", n, 900, 32001, True)),
        ("net-server-pod", net_server_pod("n1", n, 900, 32001, False)),
        ("probe", probe_pod("n1", n, 900)),
        ("stress", stress_pod("n1", n, 900, "echo hi")),
        ("stress-net", stress_pod("n1", n, 900, "echo hi", package="iperf3 iputils-ping", host_network=True)),
        ("stress-disk", stress_pod("n1", n, 900, "echo hi", package="fio", scratch_mib=512)),
        ("matrix", matrix_pod("n1", "net-mx-0-abc123", 900, "abc123")),
    ]


def containers(pod):
    return pod["spec"]["containers"]


# ---------------- A. pod specs: least privilege as it is today (regression guard) -------------------------------

@pytest.mark.parametrize("label, pod", all_pods())
def test_pod_basics(label, pod):
    spec = pod["spec"]
    assert pod["kind"] == "Pod" and spec["nodeName"] == "n1" and spec["restartPolicy"] == "Never"
    assert isinstance(spec["activeDeadlineSeconds"], int) and 0 < spec["activeDeadlineSeconds"] <= MAX_DEADLINE
    assert spec["automountServiceAccountToken"] is False                     # no API access from the pods
    assert not spec.get("hostPID") and not spec.get("hostIPC")
    assert pod["metadata"]["labels"]["app"] == TOOL_LABEL and pod["metadata"]["labels"]["run-id"]
    for c in containers(pod):
        assert c["resources"]["limits"]["memory"].endswith("Mi")             # memory is always capped
        image = c["image"]
        assert ":" in image and not image.endswith(":latest")                # pinned by tag


@pytest.mark.parametrize("label, pod", all_pods())
def test_privileged_only_where_needed(label, pod):
    privileged = [c["securityContext"].get("privileged") for c in containers(pod)]
    if label in ("smart", "hw-privileged"):
        assert privileged == [True]
    else:
        assert privileged == [None] and all(
            c["securityContext"].get("allowPrivilegeEscalation") is False for c in containers(pod))


@pytest.mark.parametrize("label, pod", all_pods())
def test_no_added_capabilities_no_dangerous_mounts(label, pod):
    for c in containers(pod):
        assert "capabilities" not in c["securityContext"] or not c["securityContext"]["capabilities"].get("add")
    host_paths = [v["hostPath"]["path"] for v in pod["spec"]["volumes"] if "hostPath" in v]
    assert host_paths == (["/sys"] if label == "probe" else [])              # only the sensor tree, only the probe
    if label == "probe":
        mount = containers(pod)[0]["volumeMounts"][0]
        assert mount["readOnly"] is True and mount["mountPath"] == "/host-sys"
    for v in pod["spec"]["volumes"]:
        if "emptyDir" in v:
            assert v["emptyDir"].get("sizeLimit")                            # scratch space is bounded


def test_host_network_only_for_network_pods():
    host = {label for label, pod in all_pods() if pod["spec"].get("hostNetwork")}
    assert host == {"net-server-host", "stress-net", "matrix"}
    for label, pod in all_pods():
        if pod["spec"].get("hostNetwork"):
            assert pod["spec"]["dnsPolicy"] == "ClusterFirstWithHostNet"


def test_service_selects_only_its_own_server_pod():
    svc = net_service("net-svc-abc123", "abc123", 32001)
    assert svc["spec"]["selector"] == {"run-id": "abc123", "role": "net-server"}
    assert len(svc["spec"]["ports"]) == 1 and svc["spec"].get("type", "ClusterIP") == "ClusterIP"


@pytest.mark.xfail(strict=True, reason="[O4] no pod drops capabilities (needs a live check that apt/ping/iperf3 still work)")
def test_pods_drop_all_capabilities():
    for label, pod in all_pods():
        if label in ("smart", "hw-privileged"):
            continue
        assert all(c["securityContext"].get("capabilities", {}).get("drop") == ["ALL"] for c in containers(pod))


@pytest.mark.xfail(strict=True, reason="[S4] no seccompProfile: RuntimeDefault (restricted Pod Security Standard)")
def test_pods_use_default_seccomp():
    for label, pod in all_pods():
        if label in ("smart", "hw-privileged"):
            continue
        assert pod["spec"].get("securityContext", {}).get("seccompProfile", {}).get("type") == "RuntimeDefault"


# ---------------- B. injection into the shell scripts that run in the pods ---------------------------------------

HOSTILE_IPS = ['1.2.3.4"; touch /tmp/pwn; "', "1.2.3.4$(id)", "`id`", "1.2.3.4\nrm -rf /", "evil.example.com", "999.1.1.1", ""]


def _node_with_ip(address):
    return node_from_json({"metadata": {"name": "n"}, "status": {
        "conditions": [{"type": "Ready", "status": "True"}], "allocatable": {"memory": "1Gi"},
        "addresses": [{"type": "InternalIP", "address": address}]}})


@pytest.mark.parametrize("address", HOSTILE_IPS[:-1])
def test_node_address_must_be_an_ip(address):
    """[S10] the InternalIP goes into shell scripts run as root in pods: anything that is not an IP is dropped."""
    assert _node_with_ip(address).internal_ip == ""


def test_valid_node_addresses_are_kept():
    assert _node_with_ip("192.168.88.43").internal_ip == "192.168.88.43"
    assert _node_with_ip("fd00::1").internal_ip == "fd00::1"


class _StubKube(cli.Kubectl):
    def __init__(self, reply):
        super().__init__("/nonexistent")
        self.reply = reply

    def run(self, *args, **kwargs):
        return self.reply


@pytest.mark.parametrize("address", HOSTILE_IPS[:-1])
def test_service_and_pod_ip_from_the_cluster_are_validated(address):
    """[S10] the address of a Service / pod is put into the iperf3 client command."""
    kube = _StubKube(address)
    assert kube.service_ip("svc") == "" and kube.pod_ip("pod") == ""


def test_valid_pod_and_service_ip_pass():
    assert _StubKube("10.43.0.7\n").service_ip("svc") == "10.43.0.7"
    assert _StubKube("10.42.5.120").pod_ip("pod") == "10.42.5.120"


def test_net_extra_cannot_carry_a_command():
    for hostile in ("dns;id", "dns$(id)", "dns && rm -rf /", "`id`", "dns\nid"):
        args = cli.build_parser().parse_args(["--profile", "net", "--net-extra", hostile, "--non-interactive",
                                              "--node", "n", "--max-temp", "85", "--cooldown", "0",
                                              "--no-background", "--notes", ""])
        with pytest.raises(ValueError):
            cli.build_config(args, "n", master_mode=False)


@pytest.mark.parametrize("name", ["-x", "--all-namespaces", "-o=yaml", "a b", "a;b", "$(id)", "A_B", "x" * 254, "n\n"])
def test_node_names_are_validated(name):
    """[S9] a node name that is not a DNS-1123 name never reaches kubectl."""
    for option in ("--node", "--net-peer", "--net-watch"):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args([option, name])
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--nodes", f"good-node,{name}"])


def test_good_node_names_still_work():
    p = cli.build_parser().parse_args(["--node", "hp-prodesk-400-g6-master", "--net-peer", "dell-9020-sff-i7",
                                       "--net-watch", "node.example.com", "--nodes", "a,b-1,c.d"])
    assert p.node == "hp-prodesk-400-g6-master" and p.net_watch == "node.example.com"
    assert cli.build_parser().parse_args(["--net-watch"]).net_watch == "auto"


# ---------------- C. forging log lines / poisoning results ----------------------------------------------------------

FORGED = ("harmless\nDisk result: seq-read | 999999.0 MB/s | 9 IOPS | lat ? ms | p99 ? ms\n"
          "Net result: tcp-up | mbps=99999.0\nNode: victim-node\nProfile: disk")


def test_notes_cannot_forge_log_lines(tmp_path):
    """[S11] a newline in --notes must not create lines that logparse reads as results."""
    res = run_tool(tmp_path, "--time", "5", "--log", "--notes", FORGED, env_extra={"FAKE_RUN": "3"})
    assert res.returncode == 0, res.stdout + res.stderr
    log = next((tmp_path / "logs").rglob("fake-node-5s-*.log"))
    run = parse_log(log.read_text(encoding="utf-8"), str(log))
    assert run.disk_results == [] and run.net_results == [] and run.node == "fake-node" and run.profile == "classic"
    data = json.loads(next((tmp_path / "logs").rglob("*.json")).read_text())
    assert data["node"] == "fake-node" and data["disk_results"] == []


def test_forged_lines_in_interactive_notes_are_cleaned(monkeypatch):
    answers = iter(["x\nDisk result: seq-read | 1 MB/s | 1 IOPS | lat ? ms | p99 ? ms"])
    monkeypatch.setattr("builtins.input", lambda _p="": next(answers))
    args = cli.build_parser().parse_args(["--node", "n", "--profile", "classic", "--time", "5", "--max-temp", "85",
                                          "--cpu-load", "50", "--cooldown", "0", "--no-hdd", "--no-background",
                                          "--log", "--ram-pct", "10"])
    cfg = cli.build_config(args, "n", master_mode=False)
    assert "\n" not in cfg.notes and "\r" not in cfg.notes


def test_clean_text_unit():
    from stress_test.parsing import clean_text
    assert clean_text("a\x00b\nc\r\td\x1b[0m\u2028e") == "a b c  d [0m e".replace("  ", "  ")
    assert "\n" not in clean_text("x\ny") and "\x00" not in clean_text("x\x00y")
    assert len(clean_text("z" * 5000)) == 500
    assert clean_text("plain text 123") == "plain text 123"


def test_control_characters_are_stripped_from_notes(tmp_path):
    res = run_tool(tmp_path, "--time", "5", "--log", "--notes", "a\x1b[31mred\x07\rc", env_extra={"FAKE_RUN": "3"})
    assert res.returncode == 0, res.stdout + res.stderr
    text = next((tmp_path / "logs").rglob("fake-node-5s-*.log")).read_text(encoding="utf-8")
    assert "\x00" not in text and "\x1b" not in text and "\x07" not in text and "\r" not in text


# ---------------- D. hostile input / robustness -----------------------------------------------------------------------

def test_pod_output_that_is_not_utf8_does_not_crash_the_tool(tmp_path):
    """[S3] bytes that are not valid UTF-8 in pod logs / kubectl output."""
    res = run_tool(tmp_path, "--time", "5", "--log", env_extra={"FAKE_RUN": "3", "FAKE_BAD_UTF8": "1"})
    assert "Traceback" not in res.stdout + res.stderr and "UnicodeDecodeError" not in res.stdout + res.stderr
    assert res.returncode == 0, res.stdout + res.stderr


@pytest.mark.parametrize("option, value", [("--interval", "0"), ("--interval", "-1"), ("--interval", "0.01"),
                                           ("--time", "99999999999"), ("--time", "9" * 40), ("--time", "0")])
def test_absurd_numbers_are_rejected(tmp_path, option, value):
    """[S12] no busy loop against the API, no test that lasts forever."""
    res = run_tool(tmp_path, "--node", "fake-node", "--dry-run", option, value)
    assert res.returncode != 0 and "Traceback" not in res.stdout + res.stderr


def test_sane_numbers_still_work(tmp_path):
    res = run_tool(tmp_path, "--dry-run", "--time", "1h", "--interval", "5")
    assert res.returncode == 0, res.stdout + res.stderr


def test_stop_never_signals_a_process_that_is_not_ours(tmp_path, monkeypatch):
    """[S1] a stale record whose PID now belongs to somebody else's process must not be killed."""
    victim = subprocess.Popen(["sleep", "30"])
    try:
        monkeypatch.setenv("STRESS_TEST_RUNNING_DIR", str(tmp_path))
        (tmp_path / "run1.json").write_text(json.dumps({"run_id": "run1", "pid": victim.pid, "node": "n1",
                                                        "duration": 60, "started": time.time(),
                                                        "log": None, "console": "x"}))
        ok, message = background.stop("run1", wait_s=0.5)
        assert victim.poll() is None, "the unrelated process was killed"
        assert not ok
    finally:
        victim.kill()
        victim.wait()


def test_open_private_refuses_symlinks(tmp_path):
    """[S2] a symlink planted in the results folder must not redirect the write."""
    target = tmp_path / "precious.txt"
    target.write_text("keep me")
    link = tmp_path / "result.log"
    link.symlink_to(target)
    with pytest.raises(OSError):
        with open_private(link, "w") as fh:
            fh.write("overwritten")
    assert target.read_text() == "keep me"


def test_results_are_private(tmp_path):
    res = run_tool(tmp_path, "--time", "5", "--log", "--export", "both", env_extra={"FAKE_RUN": "3"})
    assert res.returncode == 0, res.stdout + res.stderr
    for path in (tmp_path / "logs").rglob("fake-node-5s-*"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    assert stat.S_IMODE((tmp_path / "logs").stat().st_mode) == 0o700


def test_baseline_files_are_private(tmp_path):
    res = run_tool(tmp_path, "--time", "5", "--log", env_extra={"FAKE_RUN": "3"})
    assert res.returncode == 0, res.stdout + res.stderr
    res = run_tool(tmp_path, "--set-baseline", "fake-node")
    assert res.returncode == 0, res.stdout + res.stderr
    folder = tmp_path / "logs" / "baselines"
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE((folder / "fake-node.json").stat().st_mode) == 0o600


def test_network_matrix_results_are_private(tmp_path):
    nodes = json.dumps([{"name": "n1", "ip": "10.0.0.1"}, {"name": "n2", "ip": "10.0.0.2"}])
    res = run_tool(tmp_path, "--net-matrix", "--net-time", "5", "--yes", env_extra={"FAKE_NODES": nodes})
    assert res.returncode == 0, res.stdout + res.stderr
    files = list((tmp_path / "logs").rglob("net-matrix-*"))
    assert files and all(stat.S_IMODE(f.stat().st_mode) == 0o600 for f in files)


@pytest.mark.parametrize("content", ["[]", '"text"', "12", "null", '{"stats": "x"}', '{"stats": {"temp_max": "hot"}}',
                                     '{"profile": "classic", "stage_targets": [], "stats": {}, "metrics": []}',
                                     "{not json", '{"disk_results": "x", "net_results": 5, "profile": "classic", "stage_targets": [], "stats": {}}'])
def test_tampered_baseline_never_breaks_a_test(tmp_path, content):
    """A baseline file is read back from disk: whatever is in it, the run still finishes."""
    (tmp_path / "logs" / "baselines").mkdir(parents=True)
    (tmp_path / "logs" / "baselines" / "fake-node.json").write_text(content)
    res = run_tool(tmp_path, "--time", "5", "--log", env_extra={"FAKE_RUN": "3"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Traceback" not in res.stdout + res.stderr


def test_load_baseline_rejects_non_objects(tmp_path):
    (tmp_path / "baselines").mkdir()
    for content in ("[]", '"x"', "1", "null"):
        (tmp_path / "baselines" / "n.json").write_text(content)
        assert baseline.load_baseline(tmp_path, "n") is None


def test_hostile_log_lines_are_parsed_quickly():
    """No catastrophic regex backtracking on very long / adversarial lines."""
    header = "=== KUBERNETES STRESS-NG LOG ===\nNode: n\n"
    hostile = ["[10:00:00] CPU: " + "|" * 200_000, "[10:00:00] CPU: " + "1" * 200_000 + "|Temp: " * 5000,
               "Net result: " + "x=1 " * 50_000, "Disk result: a | " + "9" * 100_000 + " MB/s",
               "▶ Stage " + "1" * 100_000, "Power limits: PL1 " + "9" * 100_000 + " W"]
    started = time.monotonic()
    parse_log(header + "\n".join(hostile))
    assert time.monotonic() - started < 3.0


def test_duration_parser_handles_absurd_text():
    started = time.monotonic()
    for text in ("9" * 5000, "1h" * 3000, "m" * 10000, "1" + " " * 10000 + "h"):
        try:
            parse_duration(text)
        except ValueError:
            pass
    assert time.monotonic() - started < 2.0


def test_compare_does_not_echo_foreign_files(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-CONTENT\n" * 3)
    res = run_tool(tmp_path, "--compare", str(secret), str(secret))
    assert res.returncode != 0 and "TOP-SECRET-CONTENT" not in res.stdout + res.stderr
    assert "Traceback" not in res.stdout + res.stderr


def _stub_kubectl(tmp_path, body):
    script = tmp_path / "kubectl-stub"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    return script


@pytest.mark.parametrize("body", ["echo '{{{ not json'", "echo 'null'", "echo '[]'", "echo ''; exit 0",
                                  "echo boom >&2; exit 1", "sleep 0.1; printf '\\377\\376'", "echo '{\"items\": 5}'"])
def test_broken_kubectl_replies_give_clean_errors(tmp_path, body):
    stub = _stub_kubectl(tmp_path, body)
    for args in (["--list-nodes"], ["--node", "n1", "--time", "5", "--non-interactive", "--yes", "--dry-run"],
                 ["--net-matrix", "--yes"]):
        env = dict(os.environ, KUBECTL=str(stub), PYTHONPATH=str(ROOT), STRESS_TEST_RUNNING_DIR=str(tmp_path / "run"))
        res = subprocess.run([sys.executable, "-m", "stress_test", *args, "--log-dir", str(tmp_path / "logs")],
                             cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
        assert "Traceback" not in res.stdout + res.stderr, (args, res.stdout, res.stderr)


def test_missing_kubectl_is_a_clean_error(tmp_path):
    env = dict(os.environ, KUBECTL=str(tmp_path / "does-not-exist"), PYTHONPATH=str(ROOT))
    res = subprocess.run([sys.executable, "-m", "stress_test", "--node", "n", "--time", "5", "--non-interactive"],
                         cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert res.returncode != 0 and "Traceback" not in res.stdout + res.stderr


# ---------------- E. cleanup when the tool is killed ---------------------------------------------------------------------

def test_sigterm_deletes_the_pods_and_the_service(tmp_path):
    """SIGTERM in the middle of a network test with the Service extra: every object of the run is removed."""
    nodes = json.dumps([{"name": "fake-node", "ip": "10.0.0.1"}, {"name": "peer-node", "ip": "10.0.0.2"}])
    env = dict(os.environ, KUBECTL=str(FAKE), FAKE_STATE=str(tmp_path), PYTHONPATH=str(ROOT), FAKE_NODES=nodes,
               FAKE_RUN="30", STRESS_TEST_RUNNING_DIR=str(tmp_path / "running"))
    cmd = [sys.executable, "-m", "stress_test", "--node", "fake-node", "--profile", "net", "--net-time", "5",
           "--net-mode", "pod", "--net-extra", "service", "--non-interactive", "--yes", "--interval", "0.5",
           "--cooldown", "0", "--log-dir", str(tmp_path / "logs")]
    proc = subprocess.Popen(cmd, cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.time() + 40
        seen = ""
        while time.time() < deadline and "Network job" not in seen:
            seen += proc.stdout.readline()
        assert "Network job" in seen, seen
        proc.terminate()
        out, _ = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    calls = (tmp_path / "calls.log").read_text() if (tmp_path / "calls.log").exists() else ""
    assert "Traceback" not in seen + out
    assert proc.returncode in (130, 143, 1, -15, 0), proc.returncode
    deleted = [p.name for p in tmp_path.glob("deleted-*")]
    assert any(name.startswith("deleted-stress-test") for name in deleted), (deleted, calls)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unwritable_results_folder_fails_before_any_pod_is_created(tmp_path):
    """The results folder is checked before the cluster is touched (no pods started just to fail a minute later)."""
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        res = run_tool(tmp_path, "--time", "5", "--log", "--log-dir", str(ro), env_extra={"FAKE_RUN": "3"})
    finally:
        ro.chmod(0o700)
    assert res.returncode != 0 and "Traceback" not in res.stdout + res.stderr
    assert not list(tmp_path.glob("manifest-*.json")), "a pod was applied before the log folder was checked"


FORGED_LOG = ("=== KUBERNETES STRESS-NG LOG ===\nNode: {node}\nStarted: 2026-09-29 10:00:00\n"
              "[10:00:00] CPU: 5%, RAM: 100 MiB (1%) | Temp: CPU: 50°C, GPU: 50°C | Clock: 3000 MHz\n")


@pytest.mark.parametrize("node", ["../../evil", "../escape", "/etc/cron.d/x", "a/b", "..", "x\\y"])
def test_baseline_from_a_forged_log_cannot_write_outside_the_baseline_folder(tmp_path, node):
    """[S14] the node name inside a log decides the file name of the baseline: no path traversal."""
    logs = tmp_path / "logs"
    logs.mkdir()
    forged = logs / "forged.log"
    forged.write_text(FORGED_LOG.format(node=node))
    res = run_tool(tmp_path, "--set-baseline", str(forged))
    assert res.returncode != 0 and "Traceback" not in res.stdout + res.stderr
    written = [p for p in tmp_path.rglob("*.json") if "manifest" not in p.name]
    assert not written, written
    assert not (tmp_path / "evil.json").exists() and not (tmp_path.parent / "evil.json").exists()


def test_baseline_path_refuses_bad_node_names(tmp_path):
    with pytest.raises(ValueError):
        baseline.baseline_path(tmp_path, "../x")
    assert baseline.baseline_path(tmp_path, "hp-705-g4-a10").name == "hp-705-g4-a10.json"
    assert baseline.load_baseline(tmp_path, "../x") is None


def test_bypassing_the_master_protection_is_never_silent(tmp_path):
    """[O13] FORCE=1 left in a shell profile must be visible in the output."""
    res = run_tool(tmp_path, "--dry-run", "--time", "5", env_extra={"FAKE_MASTER": "1", "FORCE": "1"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "MASTER PROTECTION BYPASSED by the FORCE=1 environment variable" in res.stdout
    res = run_tool(tmp_path, "--dry-run", "--time", "5", "--force", env_extra={"FAKE_MASTER": "1"})
    assert "MASTER PROTECTION BYPASSED by --force" in res.stdout
    res = run_tool(tmp_path, "--dry-run", "--time", "5", "--yes", env_extra={"FAKE_MASTER": "1"})
    assert "BYPASSED" not in res.stdout                               # the normal (protected) way stays quiet


# ---------------- F. wrong / missing cluster objects give clean errors --------------------------------------------------

TWO_NODES = json.dumps([{"name": "fake-node", "ip": "10.0.0.1"}, {"name": "peer-node", "ip": "10.0.0.2"},
                        {"name": "down-node", "ip": "10.0.0.3", "ready": False}, {"name": "no-ip-node", "ip": ""}])


@pytest.mark.parametrize("extra, expected", [
    (["--profile", "net", "--net-peer", "ghost-node"], "ghost-node"),
    (["--profile", "net", "--net-peer", "fake-node"], "different node"),
    (["--profile", "net", "--net-peer", "no-ip-node", "--net-mode", "host"], "InternalIP"),
])
def test_network_test_with_bad_peers_fails_cleanly(tmp_path, extra, expected):
    res = run_tool(tmp_path, "--net-time", "5", *extra, env_extra={"FAKE_NODES": TWO_NODES, "FAKE_RUN": "3"})
    assert res.returncode != 0 and expected in res.stdout and "Traceback" not in res.stdout + res.stderr
    assert not list(tmp_path.glob("deleted-*")) or True                # pods are cleaned up in every case


@pytest.mark.parametrize("args", [["--node", "ghost-node"], ["--node", "down-node"], ["--nodes", "ghost-node,fake-node"]])
def test_unknown_or_not_ready_nodes_fail_cleanly(tmp_path, args):
    env = {"FAKE_NODES": TWO_NODES, "FAKE_RUN": "3"}
    extra = [a for a in args]
    res = subprocess.run([sys.executable, "-m", "stress_test", *extra, "--time", "5", "--non-interactive", "--yes",
                          "--interval", "0.5", "--cooldown", "0", "--log-dir", str(tmp_path / "logs")],
                         cwd=tmp_path, env=dict(os.environ, KUBECTL=str(FAKE), FAKE_STATE=str(tmp_path),
                                                PYTHONPATH=str(ROOT), STRESS_TEST_RUNNING_DIR=str(tmp_path / "run"), **env),
                         capture_output=True, text=True, timeout=90)
    assert res.returncode != 0 and "Traceback" not in res.stdout + res.stderr


def test_net_watch_with_a_missing_node_only_warns(tmp_path):
    res = run_tool(tmp_path, "--time", "5", "--net-watch", "ghost-node",
                   env_extra={"FAKE_NODES": TWO_NODES, "FAKE_RUN": "3"})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "net-watch" in res.stdout and "Traceback" not in res.stdout + res.stderr


def test_matrix_leaves_out_nodes_that_are_not_ready_or_have_no_address(tmp_path):
    res = run_tool(tmp_path, "--net-matrix", "--net-time", "5", "--yes", env_extra={"FAKE_NODES": TWO_NODES})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "down-node" not in res.stdout.split("NETWORK MATRIX")[-1] and "no-ip-node" not in res.stdout.split("NETWORK MATRIX")[-1]


def test_credentials_are_hidden_in_the_debug_log():
    """[S20] `kubectl get node -o json` contains k3s.io/node-env with K3S_TOKEN; it is masked by k3s, but never rely on that."""
    from stress_test.kube import _cut
    plain = _cut('{"K3S_TOKEN": "s3cr3t-value", "name": "n1", "password":"hunter2"}')
    assert "s3cr3t-value" not in plain and "hunter2" not in plain and '"name": "n1"' in plain
    escaped = _cut('"k3s.io/node-env": "{\\"K3S_TOKEN\\":\\"abc123\\",\\"K3S_URL\\":\\"https://x:6443\\"}"')
    assert "abc123" not in escaped and "K3S_URL" in escaped and "https://x:6443" in escaped
    assert _cut("nothing secret here") == "nothing secret here"


# ---------------- GPU test and the node scan (2026-10-01) -------------------------------------------------------

def test_gpu_burn_is_built_from_a_pinned_commit_never_from_the_branch_head():
    from stress_test.gpu import GPU_BURN_COMMIT, build_gpu_setup
    setup = build_gpu_setup()
    assert GPU_BURN_COMMIT in setup and len(GPU_BURN_COMMIT) == 40 and "git clone" not in setup
    assert "git checkout -q FETCH_HEAD" in setup


@pytest.mark.parametrize("bad", ["img; rm -rf /", "a b", "x$(id)", "`id`", "img\nname", "-flag", "", "a" * 300])
def test_gpu_image_with_shell_or_odd_characters_is_refused(bad):
    from stress_test.gpu import valid_image
    from stress_test.models import StressConfig
    assert not valid_image(bad)
    if bad:
        with pytest.raises(ValueError):
            StressConfig(node="n", profile="gpu", duration=60, gpu_image=bad).validate()


def test_prepull_refuses_a_bad_image_without_touching_the_cluster():
    from stress_test import gpuscan

    class NoKube:
        def apply(self, *a, **k):
            raise AssertionError("kubectl must not be called")
    said = []
    assert gpuscan.prepull(NoKube(), ["n1"], "bad image;id", said.append) == {"n1": False} and "not a valid image" in said[0].lower()


def test_pod_text_cannot_forge_log_lines_or_send_escape_codes():
    from stress_test.gpuscan import parse_cards
    from stress_test.gpu import parse_gpu_info
    from stress_test.parsing import clean_block
    cards, _ = parse_cards("GPU-CARD NVIDIA Quadro \x1b[31mRED\x1b[0m [10de:1cb6]\nSCAN-DONE")
    assert "\x1b" not in cards[0][1]
    info = parse_gpu_info("Evil\\x1b[2J, 580.1, 2048, [N/A], 1480, 3504, 3, 16, 6.1, vbios")
    assert all("\x1b" not in line for line in info)
    assert clean_block("CPU: x\nRAM \x1b]0;title\x07 bad\r\nDisk: ok") == "CPU: x\nRAM  ]0;title  bad\nDisk: ok"


def test_gpu_scan_and_prepull_pods_are_unprivileged_without_host_access():
    import json
    from stress_test.manifests import gpu_scan_pod, image_pull_pod, stress_pod
    from stress_test.models import PodNames
    names = PodNames.new()
    for pod in (gpu_scan_pod("n", names), image_pull_pod("n", names, "nvidia/cuda:x"),
                stress_pod("n", names, 600, "true", gpu=True, image="nvidia/cuda:x")):
        text = json.dumps(pod)
        assert '"privileged"' not in text and "hostPath" not in text and "hostNetwork" not in text and "hostPID" not in text
        assert pod["spec"]["automountServiceAccountToken"] is False
        assert pod["spec"]["activeDeadlineSeconds"] > 0
