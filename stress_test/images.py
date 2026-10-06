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

REGISTRY_DEFAULT = "ghcr.io/doctorxcz"
IMAGE_TAG = "1.19.0"                         # the tag of the images in the registry (see tag())
KIND_TAGS = {"gpu": "1.18.0"}                # an image that was not rebuilt for IMAGE_TAG keeps the tag it was published under (the GPU image is amd64 only, 1.18.0)
NAMES = {"tools": "k3s-stress-tools", "gpu": "k3s-stress-gpu", "probe": "k3s-stress-probe"}
# the fallback of every kind (what the tool used before the prebuilt images): started from scratch, tools installed by apt
FALLBACK = {"tools": "ubuntu:24.04", "probe": "busybox:1.36", "gpu": "nvidia/cuda:12.9.1-devel-ubuntu24.04"}
# The content digests (as the REGISTRY reports them: `Docker-Content-Digest` of the manifest, not what `podman inspect` shows) of the published images, per tool
# version: `name:tag@sha256:...` always pulls exactly the bytes that were tested, even if the tag were moved. A version without an entry, or a custom
# `--registry` (a copy may have another manifest), uses the plain tag. Update after every publication (amd64 only; a multi-arch publication has an index digest).
DIGESTS: dict = {
    "1.19.0": {                                  # multi-arch INDEXES (amd64 + arm64), published 2026-10-06
        "probe": "sha256:e7c2b461f942a35c99ad36ba3475c7bcbd000d39a2d795f73ed94750f478ebd2",
        "tools": "sha256:c0aa3a771579c280a0cb9b38ecf4a6998f29f9bd68ed9e101d48b034b3f805c8",
    },
    "1.18.0": {                                  # single-platform amd64 manifests
        "probe": "sha256:d7accbf2d2dd2318a0d7083d92140a9c971367ed261ce29170a084a9dc61236a",
        "tools": "sha256:08308640ee6fa04141c609ace7ee5b072f842a5d57641a1d5251efd3b78850ee",
        "gpu": "sha256:7a23baa1227071df9b08ddf767dce3f7f8a9da8cc7c5e42b5d78f6427d27e4b5",
    },
}

# Pull problems of a pod (the container waiting reason) that make the tool switch to the fallback image
PULL_ERRORS = ("ErrImagePull", "ImagePullBackOff", "InvalidImageName", "ErrImageNeverPull", "RegistryUnavailable")

_REGISTRY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,200}$")

# The CPU architectures the prebuilt images are published for: tools and probe 1.19.0 are multi-arch (amd64 + arm64), gpu 1.18.0 is amd64 only (gpu-burn and
# the NVIDIA runtime are built for x86 cards). A node of an architecture without a prebuilt image (e.g. armv7) starts from the multi-arch fallback images and
# installs the tools with apt. After a new publication (deploy/images/publish.sh) update IMAGE_TAG, DIGESTS (the digests of the INDEXES) and this table:
# `deploy/images/check_published.py` prints all three.
PUBLISHED = {"tools": {"amd64", "arm64"}, "probe": {"amd64", "arm64"}, "gpu": {"amd64"}}

_state = {"registry": REGISTRY_DEFAULT, "prebuilt": True, "unavailable": set()}      # unavailable: {(kind, architecture)}
_NODE_ARCH: dict = {}                                                                # node name -> normalised architecture (filled by parsing.node_from_json)

_ARCH_ALIASES = {"amd64": "amd64", "x86_64": "amd64", "x86-64": "amd64", "arm64": "arm64", "aarch64": "arm64", "arm64/v8": "arm64",
                 "arm": "armv7", "armv7": "armv7", "armv7l": "armv7", "armhf": "armv7", "armv6l": "armv6", "riscv64": "riscv64", "ppc64le": "ppc64le",
                 "s390x": "s390x", "386": "386", "i386": "386", "i686": "386"}


def normalize_arch(raw: str) -> str:
    """'amd64' / 'x86_64' -> 'amd64', 'arm64' / 'aarch64' -> 'arm64', 'arm' / 'armv7l' -> 'armv7'; anything unknown (or '?') -> 'unknown'."""
    return _ARCH_ALIASES.get((raw or "").strip().lower(), "unknown")


def note_node(name: str, architecture: str) -> None:
    """Remembers the architecture of a node, so that its pods get an image that can run there."""
    if name:
        _NODE_ARCH[name] = normalize_arch(architecture)


def node_arch(node: str = "") -> str:
    """The architecture of a node ('' / a node that was never read = amd64: what this tool ran on before the multi-arch support)."""
    return _NODE_ARCH.get(node) or "amd64"


def forget_nodes() -> None:
    _NODE_ARCH.clear()


def tag(kind: str = "") -> str:
    """The tag of the published image of this kind. It is NOT the version of the tool: the tool can be released (1.19.1, 1.20.0 ...) without publishing new
    images; change IMAGE_TAG (with DIGESTS and PUBLISHED) only when images are published under a new tag. KIND_TAGS lists the kinds that keep an older tag."""
    return KIND_TAGS.get(kind, IMAGE_TAG)


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
    name = f"{registry()}/{NAMES[kind]}:{tag(kind)}"
    digest = DIGESTS.get(tag(kind), {}).get(kind) if registry() == REGISTRY_DEFAULT else None
    return f"{name}@{digest}" if digest else name


def active(kind: str, node: str = "") -> bool:
    """True while pods of this kind on this node use the prebuilt image: not switched off, published for the node's architecture, and no earlier
    pull of it failed on that architecture (a failure on a Raspberry Pi must not switch the prebuilt image off for the amd64 nodes)."""
    arch = node_arch(node)
    return prebuilt_enabled() and arch in PUBLISHED.get(kind, set()) and (kind, arch) not in _state["unavailable"]


def image(kind: str, node: str = "") -> str:
    """The image to put into a pod of this kind, running on `node`, now."""
    return prebuilt_ref(kind) if active(kind, node) else FALLBACK[kind]


def mark_unavailable(kind: str, node: str = "") -> None:
    """A prebuilt image could not be pulled: from now on this kind starts from the fallback image (apt) on nodes of the same architecture."""
    _state["unavailable"].add((kind, node_arch(node)))


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
