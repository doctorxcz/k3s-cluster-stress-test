#!/usr/bin/env bash
# Pushes the multi-arch images made by `./build.sh --platforms linux/amd64,linux/arm64 --registry ...` to the registry (a PUBLIC one for ghcr.io/<you>).
#   ./publish.sh TAG [--only "tools probe"] [--registry HOST/PATH] [--yes] [--overwrite]
# TAG is required (the new IMAGE_TAG, e.g. 1.19.0); it is the same TAG you gave build.sh. Without --yes it shows what it will push and asks.
# It REFUSES to replace a tag that already exists in the registry (a pinned digest, the nodes' image cache and the logs refer to it) unless --overwrite.
# podman must be logged in:  podman login ghcr.io -u <user>   (a token with write:packages). After the push the script prints the digests: run
# ./check_published.py TAG and put its snippet into stress_test/images.py (IMAGE_TAG, DIGESTS, PUBLISHED), then set new packages to public (GitHub > Packages).
set -euo pipefail
tag=""; only="tools probe"; registry="ghcr.io/doctorxcz"; yes=0; overwrite=0
while [ $# -gt 0 ]; do
  case "$1" in
    --only) only="$2"; shift 2 ;;
    --registry) registry="$2"; shift 2 ;;
    --yes) yes=1; shift ;;
    --overwrite) overwrite=1; shift ;;
    -h|--help) sed -n '2,8p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) tag="$1"; shift ;;
  esac
done
[ -n "$tag" ] || { echo "publish.sh: give the TAG to publish (e.g. 1.19.0)" >&2; exit 1; }
command -v podman >/dev/null 2>&1 || { echo "publish.sh: podman is required" >&2; exit 1; }
podman login --get-login "${registry%%/*}" >/dev/null 2>&1 || { echo "publish.sh: podman is not logged in to ${registry%%/*} (podman login ${registry%%/*} -u <user>)" >&2; exit 1; }

echo "About to push to ${registry} (tag ${tag}):"
for kind in $only; do
  img="$registry/k3s-stress-$kind:$tag"
  podman image exists "$img" || { echo "  ✗ $img does not exist locally - run ./build.sh $tag --platforms linux/amd64,linux/arm64 --registry $registry --only \"$only\" first" >&2; exit 1; }
  archs="$(podman manifest inspect "$img" 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); print(",".join(sorted({m["platform"]["architecture"] for m in d.get("manifests",[])})) or "single-platform")' 2>/dev/null || echo "?")"
  echo "  $img   [$archs]"
  case "$archs" in *arm64*amd64*|*amd64*arm64*) ;; *) echo "  ! not a multi-arch image (amd64+arm64): $archs" >&2 ;; esac
  if [ "$overwrite" = 0 ] && podman manifest inspect "docker://$img" >/dev/null 2>&1; then
    echo "  ✗ $img already exists in the registry - use a new TAG (or --overwrite if you really mean it)" >&2; exit 1
  fi
done
if [ "$yes" = 0 ]; then
  read -r -p "Push these now? Anything pushed to ghcr.io/<you> is public once the package is public. [y/N] " ans
  [ "$ans" = y ] || [ "$ans" = Y ] || { echo "cancelled, nothing was pushed"; exit 1; }
fi
for kind in $only; do
  img="$registry/k3s-stress-$kind:$tag"
  echo "== pushing $img"
  podman manifest push --all "$img" "docker://$img"
done
echo "pushed. Next: ./check_published.py $tag  (digests + the snippet for stress_test/images.py)"
