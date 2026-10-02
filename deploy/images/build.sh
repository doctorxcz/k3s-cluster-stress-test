#!/usr/bin/env bash
# Builds the three images locally (podman or docker) and checks that the tools are in them.
#   ./build.sh [TAG] [--only tools|probe|gpu] [--registry HOST/PATH] [--save DIR]
# TAG defaults to the version of the tool (stress_test/__init__.py). Nothing is pushed. --save DIR also writes <name>-<tag>.tar files (for an offline cluster:
# see push-to-registry.sh). The GPU image downloads the CUDA devel image (about 3.5 GB) - use --only to skip it.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
tag="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$root/stress_test/__init__.py")"
registry="ghcr.io/doctorxcz"; only="tools probe gpu"; save=""
while [ $# -gt 0 ]; do
  case "$1" in
    --only) only="$2"; shift 2 ;;
    --registry) registry="$2"; shift 2 ;;
    --save) save="$2"; shift 2 ;;
    -h|--help) sed -n '2,6p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) tag="$1"; shift ;;
  esac
done
engine=""; for e in podman docker; do command -v "$e" >/dev/null 2>&1 && { engine="$e"; break; }; done
[ -n "$engine" ] || { echo "build.sh: neither podman nor docker is installed" >&2; exit 1; }
name() { echo "$registry/k3s-stress-$1:$tag"; }

check() {  # image, command...   - runs a command in the image and fails when it does not work
  local img="$1"; shift
  "$engine" run --rm --entrypoint "" "$img" "$@" >/dev/null 2>&1 || { echo "  ✗ FAILED: $*" >&2; return 1; }
  echo "  ✓ $*"
}

for kind in $only; do
  img="$(name "$kind")"
  echo "== building $img ($engine)"
  "$engine" build --platform linux/amd64 -t "$img" -f "$here/$kind/Dockerfile" "$here/$kind"
  case "$kind" in
    tools)
      for c in "stress-ng --version" "fio --version" "iperf3 --version" "smartctl --version" "command -v ping" "mtr --version" "curl --version" "dmidecode --version" \
               "lspci --version" "sensors -v" "htop --version" "iostat -V" "ethtool --version" "dig -v" "nvme version" "jq --version"; do
        check "$img" sh -c "$c" || exit 1
      done ;;
    probe)
      for c in "sleep 0.1" "awk --version" "cat /proc/cpuinfo" "grep -m1 . /proc/meminfo"; do check "$img" sh -c "$c" || true; done
      check "$img" sh -c "sleep 0.1" || exit 1 ;;
    gpu)
      "$engine" run --rm --entrypoint "" "$img" sh -c 'ls -1 /opt/gpu-burn; echo ---; ldd /opt/gpu-burn/gpu_burn | grep "not found" || true' ;;
  esac
  size="$("$engine" image inspect "$img" --format '{{.Size}}' 2>/dev/null || echo 0)"
  echo "  size: $(( size / 1024 / 1024 )) MB"
  if [ -n "$save" ]; then mkdir -p "$save"; "$engine" save -o "$save/k3s-stress-$kind-$tag.tar" "$img"; echo "  saved: $save/k3s-stress-$kind-$tag.tar"; fi
done
echo "done. Nothing was pushed."
