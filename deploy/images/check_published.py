#!/usr/bin/env python3
"""What is really in the registry: the platforms and the digest of each published image, and what to put into stress_test/images.py.

    ./check_published.py [TAG] [--registry ghcr.io/doctorxcz] [--verify-code]

TAG defaults to the tag of each image in stress_test/images.py (IMAGE_TAG, or the older tag of a kind in KIND_TAGS, e.g. the amd64-only GPU image). Read-only (anonymous pull token); needs the internet and public packages.
Prints the platforms, the digest of the manifest (for a multi-arch image: of the INDEX) and a snippet for images.py (IMAGE_TAG, DIGESTS, PUBLISHED).
--verify-code compares the registry with the code: every architecture the code claims in PUBLISHED must be there, and the digest in DIGESTS must be the
registry's one; exit status 1 when they differ (use it before a release).
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from stress_test import images  # noqa: E402

ACCEPT = ("application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, "
          "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json")


def fetch(url: str, headers: dict | None = None) -> tuple:
    """(body bytes, response headers). The one place that touches the network (tests replace it). Only https."""
    if not url.startswith("https://"):
        raise ValueError("only https URLs are fetched")
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=60) as resp:
        return resp.read(), resp.headers


def inspect(registry: str, kind: str, tag: str) -> dict:
    """{"platforms": [...], "digest": "sha256:...", "index": bool} or {"error": "..."}; registry like ghcr.io/doctorxcz."""
    if not images._REGISTRY_RE.match(registry) or ".." in registry or "//" in registry or "/" not in registry:
        return {"error": f"'{registry[:60]}' is not a valid registry (HOST/OWNER)"}
    host, _, owner = registry.partition("/")
    repo = f"{owner}/{images.NAMES[kind]}"
    try:
        token = json.loads(fetch(f"https://{host}/token?scope=repository:{repo}:pull")[0])["token"]
        auth = {"Authorization": "Bearer " + token}
        body, headers = fetch(f"https://{host}/v2/{repo}/manifests/{tag}", {**auth, "Accept": ACCEPT})
        doc = json.loads(body)
        digest = headers.get("Docker-Content-Digest", "")
        if "manifests" in doc:
            plats = sorted({m["platform"]["os"] + "/" + m["platform"]["architecture"] for m in doc["manifests"] if m["platform"]["architecture"] != "unknown"})
            return {"platforms": plats, "digest": digest, "index": True}
        cfg = json.loads(fetch(f"https://{host}/v2/{repo}/blobs/{doc['config']['digest']}", auth)[0])
        return {"platforms": [f"{cfg.get('os')}/{cfg.get('architecture')}"], "digest": digest, "index": False}
    except urllib.error.HTTPError as exc:
        return {"error": f"HTTP {exc.code} (not published, or the package is private)"}
    except (OSError, ValueError, KeyError) as exc:
        return {"error": str(exc)[:120]}


def archs(platforms: list) -> set:
    return {p.split("/", 1)[1] for p in platforms}


def snippet(tag: str, found: dict) -> str:
    lines = [f'IMAGE_TAG = "{tag}"', "DIGESTS: dict = {", f'    "{tag}": {{']
    lines += [f'        "{kind}": "{found[kind]["digest"]}",' for kind in ("probe", "tools", "gpu") if found.get(kind, {}).get("digest")]
    lines += ["    },", "}", "PUBLISHED = {"]
    lines += [f'    "{kind}": {sorted(archs(found[kind]["platforms"])) if "platforms" in found.get(kind, {}) else "set()"},'.replace("[", "{").replace("]", "}")
              for kind in ("tools", "probe", "gpu")]
    lines.append("}")
    return "\n".join(lines)


def verify(tag: str, found: dict) -> list:
    """Problems between the registry and images.py (empty = consistent). `tag` = the tag that was read for every kind, or "" = the code's own tag per kind."""
    problems = []
    if tag and tag != images.IMAGE_TAG:
        problems.append(f"IMAGE_TAG in the code is {images.IMAGE_TAG}, not {tag}")
    for kind in ("tools", "probe", "gpu"):
        info = found.get(kind, {})
        if "error" in info:
            problems.append(f"{kind}: {info['error']}")
            continue
        missing = images.PUBLISHED.get(kind, set()) - archs(info["platforms"])
        if missing:
            problems.append(f"{kind}: the code claims {sorted(missing)} in PUBLISHED, the registry has only {info['platforms']}")
        pinned = images.DIGESTS.get(tag or images.tag(kind), {}).get(kind)
        if pinned and pinned != info["digest"]:
            problems.append(f"{kind}: DIGESTS has {pinned[:19]}..., the registry has {info['digest'][:19]}...")
    return problems


def main(argv: list) -> int:
    tag, registry, check = "", images.REGISTRY_DEFAULT, False
    it = iter(argv)
    for a in it:
        if a == "--registry":
            registry = next(it)
        elif a == "--verify-code":
            check = True
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            tag = a
    found = {kind: inspect(registry, kind, tag or images.tag(kind)) for kind in ("tools", "probe", "gpu")}
    for kind, info in found.items():
        print(f"{registry}/{images.NAMES[kind]}:{tag or images.tag(kind)}")
        print("   " + (info["error"] if "error" in info else f"{', '.join(info['platforms'])}  {'(index)' if info['index'] else '(single-platform manifest)'}  {info['digest']}"))
    print("\n# for stress_test/images.py:\n" + snippet(tag or images.IMAGE_TAG, found))
    if check:
        problems = verify(tag, found)
        print("\n" + ("code and registry agree" if not problems else "MISMATCH:\n  " + "\n  ".join(problems)))
        return 1 if problems else 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
