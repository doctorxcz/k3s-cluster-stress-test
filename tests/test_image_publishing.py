"""Publishing multi-arch images: the tag is independent of the tool version, the scripts are valid, check_published reads a registry and compares it with the code."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from stress_test import images

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy" / "images"


def load_checker():
    spec = importlib.util.spec_from_file_location("check_published", DEPLOY / "check_published.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_image_tag_is_not_the_tool_version():
    assert images.tag() == images.IMAGE_TAG and images.IMAGE_TAG in images.DIGESTS and images.tag("gpu") == images.KIND_TAGS["gpu"]
    assert "__version__" not in (ROOT / "stress_test" / "images.py").read_text(encoding="utf-8").replace("not the version", "")      # no coupling left


def test_build_script_reads_the_image_tag_from_images_py():
    out = subprocess.run(["bash", "-c", f"sed -n 's/^IMAGE_TAG = \"\\([^\"]*\\)\".*/\\1/p' {ROOT}/stress_test/images.py"], capture_output=True, text=True).stdout.strip()
    assert out == images.IMAGE_TAG


@pytest.mark.parametrize("script", ["build.sh", "publish.sh", "push-to-registry.sh", "pull-to-folder.sh"])
def test_scripts_are_valid_bash(script):
    assert subprocess.run(["bash", "-n", str(DEPLOY / script)], capture_output=True).returncode == 0


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck not installed")
@pytest.mark.parametrize("script", ["build.sh", "publish.sh"])
def test_scripts_pass_shellcheck(script):
    res = subprocess.run(["shellcheck", "-S", "warning", str(DEPLOY / script)], capture_output=True, text=True)
    assert res.returncode == 0, res.stdout


def test_publish_needs_a_tag_and_does_not_push_by_itself():
    res = subprocess.run([str(DEPLOY / "publish.sh")], capture_output=True, text=True)
    assert res.returncode != 0 and "give the TAG" in res.stderr
    assert "podman manifest push" in (DEPLOY / "publish.sh").read_text(encoding="utf-8")           # the push exists, but only after the checks and the question
    text = (DEPLOY / "publish.sh").read_text(encoding="utf-8")
    assert text.index("Push these now?") < text.index("podman manifest push --all") and "already exists in the registry" in text


@pytest.mark.skipif(os.path.exists("/proc/sys/fs/binfmt_misc/qemu-aarch64") or os.uname().machine != "x86_64", reason="QEMU present / not an x86_64 host")
def test_multi_arch_build_without_qemu_stops_with_a_clear_message():
    if not shutil.which("podman"):
        pytest.skip("podman not installed")
    res = subprocess.run([str(DEPLOY / "build.sh"), "--platforms", "linux/amd64,linux/arm64", "--only", "probe"], capture_output=True, text=True)
    assert res.returncode != 0 and "QEMU" in res.stderr


def test_the_gpu_image_is_amd64_only_everywhere():
    assert images.PUBLISHED["gpu"] == {"amd64"}
    assert 'platforms_of() { [ "$1" = gpu ]' in (DEPLOY / "build.sh").read_text(encoding="utf-8")
    workflow = ROOT / ".github" / "workflows" / "images.yml"                       # lives only in the development repository, not in the release
    if not workflow.exists():
        return
    wf = workflow.read_text(encoding="utf-8")                                      # text checks: PyYAML is not a dependency of the tool
    assert '{image: gpu, platforms: "linux/amd64"}' in wf
    assert '{image: tools, platforms: "linux/amd64,linux/arm64"}' in wf and '{image: probe, platforms: "linux/amd64,linux/arm64"}' in wf
    assert "platforms: ${{ matrix.platforms }}" in wf


# ---------------- check_published with a fake registry ----------------------------------------------------------------------

def fake_registry(monkeypatch, mod, platforms_by_kind, digest="sha256:" + "b" * 64, fail=None):
    def fetch(url, headers=None):
        if "/token" in url:
            return json.dumps({"token": "t"}).encode(), {}
        kind = next(k for k, n in images.NAMES.items() if n in url)
        if fail == kind:
            import urllib.error
            raise urllib.error.HTTPError(url, 404, "nf", {}, None)
        if "/manifests/" in url:
            plats = platforms_by_kind[kind]
            if len(plats) == 1:
                return json.dumps({"config": {"digest": "sha256:cfg"}}).encode(), {"Docker-Content-Digest": digest}
            return json.dumps({"manifests": [{"platform": {"os": "linux", "architecture": a}} for a in plats]
                               + [{"platform": {"os": "unknown", "architecture": "unknown"}}]}).encode(), {"Docker-Content-Digest": digest}
        return json.dumps({"os": "linux", "architecture": platforms_by_kind[kind][0]}).encode(), {}
    monkeypatch.setattr(mod, "fetch", fetch)


def test_check_published_reads_index_and_single_manifests(monkeypatch):
    mod = load_checker()
    fake_registry(monkeypatch, mod, {"tools": ["amd64", "arm64"], "probe": ["amd64", "arm64"], "gpu": ["amd64"]})
    tools = mod.inspect("ghcr.io/doctorxcz", "tools", "1.19.0")
    gpu = mod.inspect("ghcr.io/doctorxcz", "gpu", "1.19.0")
    assert tools == {"platforms": ["linux/amd64", "linux/arm64"], "digest": "sha256:" + "b" * 64, "index": True}      # the attestation entry (unknown) is skipped
    assert gpu["index"] is False and gpu["platforms"] == ["linux/amd64"]


def test_check_published_snippet_and_errors(monkeypatch):
    mod = load_checker()
    fake_registry(monkeypatch, mod, {"tools": ["amd64", "arm64"], "probe": ["amd64", "arm64"], "gpu": ["amd64"]}, fail="gpu")
    found = {k: mod.inspect("ghcr.io/doctorxcz", k, "1.19.0") for k in ("tools", "probe", "gpu")}
    assert "HTTP 404" in found["gpu"]["error"]
    text = mod.snippet("1.19.0", found)
    assert 'IMAGE_TAG = "1.19.0"' in text and '"tools": {\'amd64\', \'arm64\'}' in text.replace("{'arm64', 'amd64'}", "{'amd64', 'arm64'}") and '"gpu": set()' in text


def test_verify_finds_claims_the_registry_does_not_back(monkeypatch):
    mod = load_checker()
    fake_registry(monkeypatch, mod, {"tools": ["amd64"], "probe": ["amd64"], "gpu": ["amd64"]})
    found = {k: mod.inspect("ghcr.io/doctorxcz", k, images.IMAGE_TAG) for k in ("tools", "probe", "gpu")}
    problems = mod.verify("", found)
    assert any("DIGESTS has" in p for p in problems)                                                          # the fake digest differs
    assert any("tools: the code claims ['arm64']" in p for p in problems) and not any("gpu: the code claims" in p for p in problems)   # tools/probe claim arm64, the fake has amd64 only
    monkeypatch.setitem(images.PUBLISHED, "tools", {"amd64"})
    assert not any("tools: the code claims" in p for p in mod.verify("", found))
    assert any("IMAGE_TAG in the code" in p for p in mod.verify("9.9.9", found))
