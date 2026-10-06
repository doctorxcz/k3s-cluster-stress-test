#!/usr/bin/env bash
# Builds the three images locally (podman or docker) and checks that the tools are in them.
#   ./build.sh [TAG] [--only "tools probe gpu"] [--registry HOST/PATH] [--save DIR] [--platforms linux/amd64,linux/arm64]
# TAG defaults to IMAGE_TAG of stress_test/images.py (the tag of the published images, not the version of the tool). Nothing is pushed (publish.sh does that).
# --save DIR also writes <name>-<tag>.tar files (for an offline cluster: see push-to-registry.sh). The GPU image downloads the CUDA devel image (about 3.5 GB) - use
# --only to skip it; it is always built for linux/amd64 only (gpu-burn / the NVIDIA runtime are x86).
# --platforms with more than one platform builds a MULTI-ARCH image (a manifest list, podman only) and tests every platform; the foreign one runs under QEMU:
# install QEMU first (Debian / Ubuntu up to 24.04: sudo apt install qemu-user-static; Ubuntu 25.10+/26.04: sudo apt install qemu-user-binfmt qemu-user), or build on a machine of the other architecture / with the GitHub workflow.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
tag="$(sed -n 's/^IMAGE_TAG = "\([^"]*\)".*/\1/p' "$root/stress_test/images.py")"
registry="ghcr.io/doctorxcz"; only="tools probe gpu"; save=""; platforms="linux/amd64"
while [ $# -gt 0 ]; do
  case "$1" in
    --only) only="$2"; shift 2 ;;
    --registry) registry="$2"; shift 2 ;;
    --save) save="$2"; shift 2 ;;
    --platforms) platforms="$2"; shift 2 ;;
    -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) tag="$1"; shift ;;
  esac
done
engine=""; for e in podman docker; do command -v "$e" >/dev/null 2>&1 && { engine="$e"; break; }; done
[ -n "$engine" ] || { echo "build.sh: neither podman nor docker is installed" >&2; exit 1; }
[ -n "$tag" ] || { echo "build.sh: cannot read IMAGE_TAG from stress_test/images.py" >&2; exit 1; }
name() { echo "$registry/k3s-stress-$1:$tag"; }
platforms_of() { [ "$1" = gpu ] && echo "linux/amd64" || echo "$platforms"; }       # the GPU image is amd64 only

case "$platforms" in *,*) multi=1 ;; *) multi=0 ;; esac
if [ "$multi" = 1 ]; then
  [ "$engine" = podman ] || { echo "build.sh: a multi-arch build needs podman (docker: use the GitHub workflow, or 'docker buildx build --push')" >&2; exit 1; }
  native="$(uname -m)"; case "$native" in x86_64) native=amd64 ;; aarch64) native=arm64 ;; esac
  for p in ${platforms//,/ }; do
    arch="${p#linux/}"; [ "$arch" = "$native" ] && continue
    case "$arch" in arm64) bf=qemu-aarch64 ;; arm) bf=qemu-arm ;; amd64) bf=qemu-x86_64 ;; *) bf="qemu-$arch" ;; esac
    [ -e "/proc/sys/fs/binfmt_misc/$bf" ] || { echo "build.sh: $p cannot run on this $native machine: no QEMU (/proc/sys/fs/binfmt_misc/$bf). Install QEMU (Debian/older Ubuntu: qemu-user-static; Ubuntu 26.04: qemu-user-binfmt + qemu-user) and run again." >&2; exit 1; }
  done
fi

check() {  # platform, image, command...   - runs a command in the image (on that platform) and fails when it does not work
  local plat="$1" img="$2"; shift 2
  "$engine" run --rm --platform "$plat" --entrypoint "" "$img" "$@" >/dev/null 2>&1 || { echo "  ✗ FAILED on $plat: $*" >&2; return 1; }
  echo "  ✓ [$plat] $*"
}

for kind in $only; do
  img="$(name "$kind")"
  plats="$(platforms_of "$kind")"
  echo "== building $img ($engine) for $plats"
  case "$plats" in
    *,*) "$engine" rmi -f "$img" >/dev/null 2>&1 || true; "$engine" manifest rm "$img" >/dev/null 2>&1 || true
         "$engine" build --platform "$plats" --manifest "$img" -f "$here/$kind/Dockerfile" "$here/$kind" ;;
    *)   "$engine" build --platform "$plats" -t "$img" -f "$here/$kind/Dockerfile" "$here/$kind" ;;
  esac
  for plat in ${plats//,/ }; do
  case "$kind" in
    tools)
      for c in "stress-ng --version" "fio --version" "iperf3 --version" "smartctl --version" "command -v ping" "mtr --version" "curl --version" "dmidecode --version" \
               "lspci --version" "sensors -v" "htop --version" "iostat -V" "ethtool --version" "dig -v" "nvme version" "jq --version"; do
        check "$plat" "$img" sh -c "$c" || exit 1
      done ;;
    probe)
      for c in "sleep 0.1" "awk --version" "cat /proc/cpuinfo" "grep -m1 . /proc/meminfo"; do check "$plat" "$img" sh -c "$c" || true; done
      check "$plat" "$img" sh -c "sleep 0.1" || exit 1 ;;
    gpu)
      "$engine" run --rm --platform "$plat" --entrypoint "" "$img" sh -c 'ls -1 /opt/gpu-burn; echo ---; ldd /opt/gpu-burn/gpu_burn | grep "not found" || true' ;;
  esac
  done
  if [ "$multi" = 0 ] || [ "$kind" = gpu ]; then
    size="$("$engine" image inspect "$img" --format '{{.Size}}' 2>/dev/null || echo 0)"
    echo "  size: $(( size / 1024 / 1024 )) MB"
  fi
  if [ -n "$save" ]; then mkdir -p "$save"; "$engine" save -o "$save/k3s-stress-$kind-$tag.tar" "$img"; echo "  saved: $save/k3s-stress-$kind-$tag.tar"; fi
done
echo "done. Nothing was pushed (publish.sh pushes)."
