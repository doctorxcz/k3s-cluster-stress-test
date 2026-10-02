#!/usr/bin/env bash
# Loads the image tar files of a folder (made with `build.sh --save DIR`, or `podman save`) into YOUR registry, for a cluster that cannot reach ghcr.io.
#   ./push-to-registry.sh HOST[:PORT]/PATH [DIR]
# Then run the tool with  --registry HOST[:PORT]/PATH  (the tags stay the same). A plain-HTTP registry needs --tls-verify=false in the line below.
set -euo pipefail
target="${1:?usage: push-to-registry.sh HOST[:PORT]/PATH [DIR]}"; dir="${2:-.}"
engine=""; for e in podman docker; do command -v "$e" >/dev/null 2>&1 && { engine="$e"; break; }; done
[ -n "$engine" ] || { echo "neither podman nor docker is installed" >&2; exit 1; }
shopt -s nullglob
files=("$dir"/k3s-stress-*.tar)
[ ${#files[@]} -gt 0 ] || { echo "no k3s-stress-*.tar in $dir" >&2; exit 1; }
for f in "${files[@]}"; do
  loaded="$("$engine" load -i "$f" | sed -n 's/^Loaded image[s]*: //p' | tail -n1)"
  loaded="${loaded#localhost/}"
  base="${loaded##*/}"                                        # k3s-stress-tools:1.18.0
  echo "$f -> $target/$base"
  "$engine" tag "$loaded" "$target/$base"
  "$engine" push "$target/$base"
done
