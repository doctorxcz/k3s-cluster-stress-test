"""GPU scan of the cluster: which nodes have a dedicated NVIDIA GPU that a test can really use.

Three independent sources are combined per node:
  1. the Kubernetes API: allocatable `nvidia.com/gpu` (driver + toolkit + runtime + device plugin all work),
  2. `lspci` in a short unprivileged pod (every graphics card the hardware has, also integrated ones),
  3. whether the scan itself worked (a failed scan is `?`, never "no GPU").
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import ui
from .kube import Kubectl, KubectlError
from .models import PodNames
from .parsing import clean_text

SCAN_SCRIPT = r"""
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq pciutils >/dev/null 2>&1 || echo "SCAN-NOLSPCI"
lspci -nn 2>/dev/null | grep -iE 'vga compatible|3d controller|display controller' | sed 's/^[^ ]* [^:]*: /GPU-CARD /'
echo SCAN-DONE
"""
VENDORS = {"10de": "NVIDIA", "8086": "Intel", "1002": "AMD", "1022": "AMD"}
_ID_RE = re.compile(r"\[([0-9a-f]{4}):([0-9a-f]{4})\]")

OK, ERROR, NONE, UNKNOWN, NOT_READY = "ok", "error", "none", "unknown", "not-ready"


@dataclass
class NodeScan:
    name: str
    master: bool = False
    ready: bool = True
    gpu_count: int = 0                       # allocatable nvidia.com/gpu
    cards: list = field(default_factory=list)   # (vendor, description) of every graphics card lspci saw
    scan_ok: bool = False
    status: str = UNKNOWN
    note: str = ""

    @property
    def nvidia_cards(self) -> list:
        return [c for c in self.cards if c[0] == "NVIDIA"]


def parse_cards(text: str) -> tuple[list, bool]:
    """([(vendor, description)], scan finished) from the output of SCAN_SCRIPT."""
    cards = []
    for line in text.splitlines():
        if not line.startswith("GPU-CARD "):
            continue
        body = line[len("GPU-CARD "):].strip()
        found = _ID_RE.search(body)
        vendor = VENDORS.get(found.group(1), "other") if found else "other"
        body = _ID_RE.sub("", body).strip()
        cards.append((vendor, clean_text(re.sub(r"\s+", " ", body), 100)))
    return cards, "SCAN-DONE" in text


def classify(scan: NodeScan) -> NodeScan:
    """Sets status and note from everything known about the node."""
    if not scan.ready:
        scan.status, scan.note = NOT_READY, "node is not Ready"
    elif scan.gpu_count > 0:
        card = scan.nvidia_cards[0][1] if scan.nvidia_cards else "NVIDIA GPU"
        scan.status, scan.note = OK, f"{card} · nvidia.com/gpu {scan.gpu_count}"
    elif not scan.scan_ok:
        scan.status, scan.note = UNKNOWN, "the scan failed (pod did not finish) - cannot tell"
    elif scan.nvidia_cards:
        scan.status = ERROR
        scan.note = f"{scan.nvidia_cards[0][1]} found, but Kubernetes does not offer it (driver / toolkit / runtime / device plugin)"
    elif scan.cards:
        kind = ", ".join(f"{v} {d}" for v, d in scan.cards)
        scan.status, scan.note = NONE, f"no dedicated NVIDIA GPU (only: {kind[:70]})"
    else:
        scan.status, scan.note = NONE, "no graphics card found"
    return scan


def _scan_one(kube: Kubectl, node, timeout: int) -> NodeScan:
    scan = NodeScan(node.name, node.is_control_plane, node.ready, node.gpu_count)
    if node.ready:
        from .manifests import gpu_scan_pod
        names = PodNames.new()
        try:
            kube.apply(gpu_scan_pod(node.name, names))
            end = time.monotonic() + timeout
            phase = ""
            while time.monotonic() < end:
                phase = kube.pod_phase(names.hw)
                if phase in ("Succeeded", "Failed"):
                    break
                time.sleep(2)
            text = kube.logs(names.hw) if phase == "Succeeded" else ""
            scan.cards, scan.scan_ok = parse_cards(text)
        except KubectlError:
            scan.scan_ok = False
        finally:
            kube.delete_pods(names.hw)
    return classify(scan)


def scan_nodes(kube: Optional[Kubectl] = None, emit: Callable[[str], None] = ui.emit, timeout: int = 150) -> list:
    """Scans every node of the cluster (in parallel, read-only pods) and returns the NodeScan list in cluster order."""
    kube = kube or Kubectl()
    nodes = [kube.get_node(n) for n in kube.list_node_names()]
    emit(f"{ui.ICON_GPU} Scanning {len(nodes)} nodes for graphics cards (short read-only pods, up to ~1-2 min)...")
    results: dict = {}

    def work(node) -> None:
        results[node.name] = _scan_one(kube, node, timeout)

    threads = [threading.Thread(target=work, args=(n,), daemon=True) for n in nodes]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return [results[n.name] for n in nodes]


def prepull(kube: Kubectl, names: list, image: str, emit: Callable[[str], None] = ui.emit, timeout: int = 1500) -> dict:
    """Pulls the CUDA image on the nodes (all at once) so that the tests start fast; returns {node: ok}.
    Failures are reported but do not stop anything - the load pod pulls the image itself later."""
    from .gpu import valid_image
    from .manifests import image_pull_pod
    if not valid_image(image):
        emit(f"⚠️  Not a valid image name, the pre-pull is skipped: {clean_text(image, 60)}")
        return {n: False for n in names}
    emit(f"{ui.ICON_GPU} Pre-pulling {image} on {len(names)} node(s) (about 3 GB the first time, seconds when cached)...")
    results: dict = {}

    def work(name: str) -> None:
        pod = PodNames.new()
        ok = False
        try:
            kube.apply(image_pull_pod(name, pod, image))
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                phase = kube.pod_phase(pod.hw)
                if phase in ("Succeeded", "Failed"):
                    ok = phase == "Succeeded"
                    break
                time.sleep(3)
        except KubectlError:
            ok = False
        finally:
            kube.delete_pods(pod.hw)
        results[name] = ok
        emit(f"  {'✅' if ok else '⚠️ '} {name}: image {'ready' if ok else 'could not be pulled (the test will try again)'}")

    threads = [threading.Thread(target=work, args=(n,), daemon=True) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


ICON_BY_STATUS = {OK: "✅", ERROR: "⚠️", NONE: "❌", UNKNOWN: "❓", NOT_READY: "💤"}
LABEL_BY_STATUS = {OK: "OK", ERROR: "ERROR", NONE: "none", UNKNOWN: "? ERROR", NOT_READY: "not Ready"}


def table_lines(scans: list, on: bool = False, width: Optional[int] = None) -> list:
    """The framed table of the scan: node, role, status icon + what was found."""
    width = width or ui.panel_width()
    room = width - 4
    rows = []
    for s in scans:
        head = f"{ui.ICON_NODE} {s.name[:26]:<26} {'master' if s.master else 'worker':<7}"
        icon = ICON_BY_STATUS[s.status].replace("⚠️", "❗")           # a one-cell-safe warning inside frames
        label = LABEL_BY_STATUS[s.status]
        colour = {OK: ui.GREEN, ERROR: ui.RED, UNKNOWN: ui.YELLOW}.get(s.status, ui.GREY)
        first = f"{head} {icon} {ui.paint(label, colour, on)}"
        rows.append(first)
        rows += [("      " + part) for part in ui.wrap(s.note, max(20, room - 6))]
    ok = sum(1 for s in scans if s.status == OK)
    foot = f"{ok} of {len(scans)} nodes have a usable dedicated NVIDIA GPU"
    return ui.box(f"{ui.ICON_GPU} GPU SCAN", [rows, [ui.paint(foot, ui.GREEN if ok else ui.YELLOW, on)]], on, width)


def candidates(scans: list) -> list:
    return [s for s in scans if s.status == OK]


def choose_nodes(scans: list, ask: Callable, emit: Callable[[str], None] = ui.emit) -> Optional[list]:
    """Node names to test: one candidate = chosen automatically ('one node'), several = the user picks (numbers, names or a = all)."""
    ok = candidates(scans)
    if not ok:
        emit("❌ No node with a usable dedicated NVIDIA GPU - nothing to test (see HELPDESK.md, section GPU test).")
        return None
    if len(ok) == 1:
        emit(f"{ui.ICON_GPU} One GPU node found, using it: {ok[0].name}")
        return [ok[0].name]
    entries = [(str(i), s.name, s.note) for i, s in enumerate(ok, 1)]
    entries.append(("a", "all", "every node above, one after another"))
    ui.show_choices("WHICH GPU NODES?", entries, back="Back")
    while True:
        raw = ask("Nodes (numbers like 1,3 / a = all / 0 = back)", "a").strip().lower()
        if raw in ("0", "q"):
            return None
        if raw in ("a", "all", ""):
            return [s.name for s in ok]
        picked, bad = [], False
        for part in re.split(r"[,\s]+", raw):
            if part.isdigit() and 1 <= int(part) <= len(ok):
                name = ok[int(part) - 1].name
            elif part in {s.name for s in ok}:
                name = part
            else:
                bad = True
                break
            if name not in picked:
                picked.append(name)
        if picked and not bad:
            return picked
        ui.warn("Invalid choice.")


def options_for(names: list) -> list:
    return ["--node", names[0]] if len(names) == 1 else ["--nodes", ",".join(names)]


# --- one table for a GPU test of several nodes (one after another) --------------------------------------
WAITING, RUNNING, DONE, WARN, FAILED, SKIPPED = "waiting", "running", "done", "warn", "failed", "skipped"


@dataclass
class BoardRow:
    name: str
    state: str = WAITING
    card: str = ""
    progress: float = 0.0                # 0..1 of the load phase while running
    temp: Optional[float] = None         # newest GPU temperature (running)
    max_temp: Optional[float] = None
    clock: Optional[float] = None        # MHz: newest while running, the loaded average at the end
    vram: str = ""
    throttle_pct: Optional[float] = None
    note: str = ""


class GpuBoard:
    """The state of a multi-node GPU test: shown inside the live frame of the running node, printed between the nodes
    in plain output and as the final table. Written by the series runner and the live screen, read when drawing."""

    def __init__(self, names: list, cards: Optional[dict] = None) -> None:
        self.rows = {n: BoardRow(n, card=(cards or {}).get(n, "")) for n in names}
        self.order = list(names)

    def start(self, name: str) -> None:
        self.rows[name].state = RUNNING

    def live(self, name: str, temp=None, clock=None, progress=None, card: str = "", vram: str = "") -> None:
        row = self.rows.get(name)
        if row is None or row.state != RUNNING:
            return
        if temp is not None:
            row.temp = temp
            row.max_temp = temp if row.max_temp is None else max(row.max_temp, temp)
        if clock is not None:
            row.clock = clock
        if progress is not None:
            row.progress = progress
        if card:
            row.card = card
        if vram:
            row.vram = vram

    def finish(self, name: str, code: Optional[int], summary=None) -> None:
        row = self.rows[name]
        if summary is not None:
            row.max_temp = summary.max_temp if summary.max_temp is not None else row.max_temp
            row.clock = summary.clock_max or row.clock
            row.throttle_pct = summary.throttle_pct
            row.note = "; ".join(summary.findings)
        row.progress = 1.0
        row.temp = None
        if code == 0:
            row.state = WARN if row.note else DONE
        else:
            row.state = FAILED
            row.note = row.note or {3: "overheated, stopped", 1: "error"}.get(code, f"exit code {code}")

    def skip_rest(self, why: str) -> None:
        for row in self.rows.values():
            if row.state == WAITING:
                row.state, row.note = SKIPPED, why

    @property
    def position(self) -> str:
        done = sum(1 for r in self.rows.values() if r.state not in (WAITING, RUNNING))
        running = any(r.state == RUNNING for r in self.rows.values())
        return f"{min(done + (1 if running else 0), len(self.order))}/{len(self.order)}"

    def lines(self, on: bool = False, width: int = 100) -> list:
        """Rows for a frame section: a header and one row per node (states: ✅ done, ⚠️ done with a note, ▶ running,
        ⏳ waiting, ❌ failed, ➖ skipped)."""
        compact = width < 60
        name_w = 18 if compact else 24
        out = [f"{ui.ICON_GPU} {ui.paint('GPU TEST', ui.BOLD, on)} · {len(self.order)} nodes, one after another · {self.position}"]
        for name in self.order:
            r = self.rows[name]
            head = f"{ui.ICON_NODE} {name[:name_w]:<{name_w}}"
            if not compact:
                head += f" {(r.card or '?')[:18]:<18}"
            if r.state == RUNNING:
                temp = f"{r.temp:.0f} °C" if r.temp is not None else "…"
                tail = (f"{ui.paint('▶', ui.YELLOW, on)} {r.progress * 100:>3.0f} %  {ui.ICON_TEMP} {temp}"
                        + (f"  {ui.ICON_CLOCK} {r.clock:.0f} MHz" if r.clock is not None and not compact else ""))
            elif r.state in (DONE, WARN):
                icon = "✅" if r.state == DONE else "❗"
                tail = (f"{icon} done   {ui.ICON_TEMP} {r.max_temp:.0f} °C max" if r.max_temp is not None else f"{icon} done")
                if not compact:
                    tail += (f"  {ui.ICON_CLOCK} {r.clock:.0f} MHz" if r.clock else "")
                    tail += f"  throttle {r.throttle_pct:.0f} %" if r.throttle_pct is not None else ""
            elif r.state == FAILED:
                tail = f"❌ {ui.paint(r.note or 'failed', ui.RED, on)}"
            elif r.state == SKIPPED:
                tail = f"➖ {ui.paint(r.note or 'not tested', ui.GREY, on)}"
            else:
                tail = f"⏳ {ui.paint('waiting', ui.GREY, on)}"
            out.append(f"{head} {tail}")
        return out

    def table(self, on: bool = False, width: Optional[int] = None) -> list:
        """The same as a framed block (plain output between the nodes and the final table)."""
        width = width or ui.panel_width()
        notes = [f"{ui.ICON_NODE} {r.name}: {r.note}" for r in self.rows.values() if r.note and r.state == WARN]
        return ui.box(f"{ui.ICON_GPU} GPU TEST SUMMARY", [self.lines(on, width), *([notes] if notes else [])], on, width)
