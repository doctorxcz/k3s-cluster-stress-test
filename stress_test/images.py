"""The container images of the tool: three ready-made images instead of `apt-get install` in every pod.

* `tools`  - Ubuntu 24.04 with stress-ng, fio, smartmontools, iperf3, ping, mtr, curl, dmidecode, pciutils, lm-sensors, htop ... (CPU / RAM / disk /
             network tests, hardware and SMART detection, GPU scan);
* `gpu`    - the CUDA runtime with gpu-burn built for several GPU generations (the GPU test);
* `probe`  - busybox for the temperature probe and the dashboard probes.

Why: a pod that starts from a clean `ubuntu:24.04` must download the package lists and install its tools first - 25 s on a fast node, 130 s on a slow
Celeron, every single test. With the prebuilt image the tools are already there (the image is pulled once per node and stays in the cache).

The pods work with BOTH: every script starts with `command -v tool || apt-get install ...` (see `ensure_tools`), so when a prebuilt image cannot
be pulled (not published yet, no access, no internet) the tool says so, switches that kind of pod to the plain Ubuntu / busybox image and goes on with
apt, exactly as before. `--no-prebuilt` forces that path, `--registry HOST/PATH` points the three images at your own registry (an offline or
company cluster).
"""
from __future__ import annotations

import re
from typing import Optional

from . import __version__

REGISTRY_DEFAULT = "ghcr.io/doctorxcz"
NAMES = {"tools": "k3s-stress-tools", "gpu": "k3s-stress-gpu", "probe": "k3s-stress-probe"}
# the fallback of every kind (what the tool used before the prebuilt images): started from scratch, tools installed by apt
FALLBACK = {"tools": "ubuntu:24.04", "probe": "busybox:1.36", "gpu": "nvidia/cuda:12.9.1-devel-ubuntu24.04"}
# The content digests (as the REGISTRY reports them: `Docker-Content-Digest` of the manifest, not what `podman inspect` shows) of the published images, per tool
# version: `name:tag@sha256:...` always pulls exactly the bytes that were tested, even if the tag were moved. A version without an entry, or a custom
# `--registry` (a copy may have another manifest), uses the plain tag. Update after every publication (amd64 only; a multi-arch publication has an index digest).
DIGESTS: dict = {
    "1.18.0": {
        "probe": "sha256:d7accbf2d2dd2318a0d7083d92140a9c971367ed261ce29170a084a9dc61236a",
        "tools": "sha256:08308640ee6fa04141c609ace7ee5b072f842a5d57641a1d5251efd3b78850ee",
        "gpu": "sha256:7a23baa1227071df9b08ddf767dce3f7f8a9da8cc7c5e42b5d78f6427d27e4b5",
    },
}

# Pull problems of a pod (the container waiting reason) that make the tool switch to the fallback image
PULL_ERRORS = ("ErrImagePull", "ImagePullBackOff", "InvalidImageName", "ErrImageNeverPull", "RegistryUnavailable")

_REGISTRY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,200}$")

_state = {"registry": REGISTRY_DEFAULT, "prebuilt": True, "unavailable": set()}


def tag() -> str:
    """The image tag = the version of the tool (the images are built from the same release)."""
    return __version__


def configure(registry: str = "", prebuilt: bool = True) -> None:
    """Sets the registry (empty = the default) and whether the prebuilt images are used at all. Also forgets earlier pull failures."""
    registry = (registry or REGISTRY_DEFAULT).strip().rstrip("/")
    if not _REGISTRY_RE.match(registry) or ".." in registry or "//" in registry:
        raise ValueError(f"'{registry[:60]}' is not a valid registry (HOST[:PORT]/PATH, letters, digits, . _ - : /)")
    _state["registry"], _state["prebuilt"] = registry, bool(prebuilt)
    _state["unavailable"] = set()


def cli_args() -> list:
    """The command-line flags that carry the current choice to a child process (parallel workers, a systemd timer, a FULL self-test phase)."""
    out = []
    if registry() != REGISTRY_DEFAULT:
        out += ["--registry", registry()]
    if not prebuilt_enabled():
        out.append("--no-prebuilt")
    return out


def registry() -> str:
    return _state["registry"]


def prebuilt_enabled() -> bool:
    return bool(_state["prebuilt"])


def prebuilt_ref(kind: str) -> str:
    """`ghcr.io/doctorxcz/k3s-stress-tools:1.18.0` (with `@sha256:...` when the digest is known)."""
    name = f"{registry()}/{NAMES[kind]}:{tag()}"
    digest = DIGESTS.get(tag(), {}).get(kind) if registry() == REGISTRY_DEFAULT else None
    return f"{name}@{digest}" if digest else name


def active(kind: str) -> bool:
    """True while pods of this kind use the prebuilt image (not switched off, and no earlier pull of it failed)."""
    return prebuilt_enabled() and kind not in _state["unavailable"]


def image(kind: str) -> str:
    """The image to put into a pod of this kind now."""
    return prebuilt_ref(kind) if active(kind) else FALLBACK[kind]


def mark_unavailable(kind: str) -> None:
    """A prebuilt image could not be pulled: from now on this kind starts from the fallback image (apt)."""
    _state["unavailable"].add(kind)


def is_prebuilt(image_ref: str) -> bool:
    return any(image_ref.startswith(f"{registry()}/{name}:") for name in NAMES.values())


# --- shell: install only what is missing ----------------------------------------------------------------------------
# package -> the command that proves it is installed
BINARIES = {"stress-ng": "stress-ng", "fio": "fio", "iperf3": "iperf3", "iputils-ping": "ping", "mtr-tiny": "mtr", "curl": "curl",
            "smartmontools": "smartctl", "dmidecode": "dmidecode", "pciutils": "lspci", "git": "git", "make": "make", "g++": "g++",
            "lm-sensors": "sensors", "htop": "htop"}


def ensure_tools(packages: str, quiet: bool = True) -> str:
    """A shell condition that is true when all `packages` are usable: they are already there (the prebuilt image), or `apt` installs them.

    `{ command -v a && command -v b; } || { apt-get update && apt-get install -y a b; }` - exit status 0 = ready.
    """
    names = packages.split()
    checks = " && ".join(f"command -v {BINARIES.get(p, p)} >/dev/null 2>&1" for p in names)
    sink = ">/dev/null 2>&1" if quiet else ""
    install = f"{{ apt-get update -qq {sink} && apt-get install -y -qq {' '.join(names)} {sink}; }}"
    return f"{{ {checks} || {install}; }}"
