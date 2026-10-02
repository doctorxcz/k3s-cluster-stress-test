#!/usr/bin/env bash
# Downloads the three published images into tar files (on a computer WITH internet), to carry them to a cluster without it.
#   ./pull-to-folder.sh [DIR] [TAG] [--registry HOST/PATH]     then on the other side: ./push-to-registry.sh YOUR-REGISTRY/PATH DIR
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dir="${1:-images-offline}"; tag="${2:-$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$here/../../stress_test/__init__.py")}"; registry="ghcr.io/doctorxcz"
[ "${3:-}" = "--registry" ] && registry="${4:?--registry needs HOST/PATH}"
engine=""; for e in podman docker; do command -v "$e" >/dev/null 2>&1 && { engine="$e"; break; }; done
[ -n "$engine" ] || { echo "neither podman nor docker is installed" >&2; exit 1; }
mkdir -p "$dir"
for kind in tools probe gpu; do
  img="$registry/k3s-stress-$kind:$tag"
  echo "== $img"
  "$engine" pull "$img"
  "$engine" save -o "$dir/k3s-stress-$kind-$tag.tar" "$img"
done
echo "saved in $dir"
