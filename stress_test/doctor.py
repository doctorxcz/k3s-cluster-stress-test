"""`--doctor`: a thorough, read-only health check of the whole cluster, with advice what to fix or improve.

Layers (each one only adds to the previous):
  1. the Kubernetes API (nodes, conditions, versions, pods, events, API speed) - kubectl reads only;
  2. short read-only probe pods, the same as the dashboard's (temperatures, clock, throttling, NIC, disks, swap, PSI, time);
  3. light active checks from the probe pods (ping between nodes, DNS in the cluster, clock difference);
  4. optional: a mini load test (every worker at once at 50 % for a minute, the master never) with the responsiveness of the
     nodes measured before and during it - only after an explicit OK, never by itself.

The findings are pure functions of the collected data (easy to test); the result is a report (screen, Markdown, JSON) that is
compared with the previous report and with the older load-test logs of the same node.
"""
from __future__ import annotations

import json
import os
import logging
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import ui
from .parsing import format_duration

log = logging.getLogger(__name__)

LEVELS = ("OK", "INFO", "WARN", "FAIL")
PENALTY = {"OK": 0, "INFO": 0, "WARN": 4, "FAIL": 15}
ICONS = {"OK": "✅", "INFO": "💡", "WARN": "⚠️ ", "FAIL": "❌"}
SCHEMA = 1

# thresholds (one place, so the advice and the tests agree)
IDLE_TEMP_WARN = 70            # °C in a node that does nothing: the cooling is poor
TEMP_NEAR_LIMIT = 80           # °C
API_WARN_MS, API_FAIL_MS = 500.0, 2000.0
SKEW_WARN_S, SKEW_FAIL_S = 2.0, 30.0
RAM_FREE_WARN_PCT = 10
PSI_WARN = 10.0                # % of time stalled (avg10) at rest
RESTARTS_WARN = 5
REQUEST_WARN_PCT = 90          # requested CPU / RAM of a node in % of what it can allocate
PEER_WARN_MS = 5.0
LOAD_PCT = 50                  # the mini load test
LOAD_SECONDS = 60
FULL_LOAD_PCT = 90             # a log counts as a full-load reference when its CPU reached this
SLOWDOWN_WARN = 3.0            # responsiveness under load more than 3x slower than at rest
SLOWDOWN_FAIL = 10.0


@dataclass
class Finding:
    level: str                 # OK / INFO / WARN / FAIL
    area: str                  # api / node / pods / network / clock / performance / load / consistency
    title: str
    detail: str = ""
    advice: str = ""
    node: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Report:
    when: float
    version: str = ""
    nodes: list = field(default_factory=list)
    findings: list = field(default_factory=list)          # Finding
    scores: dict = field(default_factory=dict)            # node (and "cluster") -> 0-100
    restarts: dict = field(default_factory=dict)          # node -> container restarts at that time (the next check counts the new ones)
    load: dict = field(default_factory=dict)              # the mini load test: responsiveness at rest / under load per node
    schema: int = SCHEMA

    @property
    def worst(self) -> str:
        return max((f.level for f in self.findings), key=LEVELS.index, default="OK")

    @property
    def verdict(self) -> str:
        score = self.scores.get("cluster", 100)
        return "healthy" if score >= 90 else "mostly fine" if score >= 75 else "needs attention" if score >= 50 else "in trouble"

    def count(self, level: str) -> int:
        return sum(1 for f in self.findings if f.level == level)

    def to_dict(self) -> dict:
        return {"schema": self.schema, "when": self.when, "version": self.version, "nodes": self.nodes, "scores": self.scores,
                "verdict": self.verdict, "findings": [f.to_dict() for f in self.findings], "load": self.load, "restarts": self.restarts}


def _f(level: str, area: str, title: str, detail: str = "", advice: str = "", node: str = "") -> Finding:
    return Finding(level, area, title, detail, advice, node)


# --- 1. the Kubernetes API ----------------------------------------------------------------------------------------------

def api_findings(view) -> list:
    out = []
    if view.error:
        out.append(_f("FAIL", "api", "kubectl returned an error", view.error, "Check the connection to the cluster (kubectl get nodes)."))
    if view.api_ms is None:
        out.append(_f("INFO", "api", "API response time not measured"))
    elif view.api_ms >= API_FAIL_MS:
        out.append(_f("FAIL", "api", f"The API server answers very slowly ({view.api_ms:.0f} ms)", "",
                      "Look at the load of the master (etcd on a slow disk is the usual cause) and at the network to it."))
    elif view.api_ms >= API_WARN_MS:
        out.append(_f("WARN", "api", f"The API server is slow ({view.api_ms:.0f} ms)", "", "Check the master: CPU, disk (etcd) and the network."))
    else:
        out.append(_f("OK", "api", f"API server answers in {view.api_ms:.0f} ms"))
    bad = [p for p in view.problems]
    if bad:
        out.append(_f("WARN", "pods", f"{len(bad)} pod(s) are not fine", "; ".join(f"{p[1]} ({p[2]}) on {p[0]}" for p in bad[:6]),
                      "kubectl describe pod <name> and kubectl logs <name> show why."))
    else:
        out.append(_f("OK", "pods", "All pods are running"))
    warnings = sum(view.ev_counts.values()) if view.ev_counts else len(view.events)
    if warnings:
        top = ", ".join(f"{r} ×{n}" for r, n in sorted(view.ev_counts.items(), key=lambda kv: -kv[1])[:4])
        out.append(_f("WARN" if warnings >= 10 else "INFO", "pods", f"{warnings} warning event(s) in the cluster", top,
                      "kubectl get events -A --field-selector type=Warning"))
    pending = (view.pvcs or {}).get("Pending", 0)
    if pending:
        out.append(_f("WARN", "pods", f"{pending} PersistentVolumeClaim(s) are Pending", "", "kubectl describe pvc shows the missing volume or storage class."))
    return out


def node_findings(n, prev_restarts: Optional[int] = None) -> list:
    """Everything that can be said about ONE node from the API data and its probe. prev_restarts: the container restarts of the node
    at the previous check (None = there was no previous check, the number is then only cumulative)."""
    name, out = n.name, []

    def add(level, area, title, detail="", advice=""):
        out.append(_f(level, area, title, detail, advice, name))

    if not n.ready:
        add("FAIL", "node", "The node is NOT Ready", str(n.conditions.get("Ready", ("?", ""))), "Check kubelet and the network of the node (journalctl -u k3s-agent).")
        return out
    for kind in n.pressure:
        add("WARN", "node", f"{kind} pressure", "", "The node is short of that resource: move pods away or free it.")
    if n.unschedulable:
        add("INFO", "node", "The node is cordoned (no new pods)", "", "kubectl uncordon " + name + " when the maintenance is over.")
    if n.taints and not n.master:
        add("INFO", "node", "The node has taints", ", ".join(n.taints[:4]))
    if not n.probe_ok:
        add("WARN", "node", "The probe pod did not start - no live data", "", "Is the image pullable on this node? kubectl get pods -l app=stress-test-dashboard -o wide")
        return out
    # temperature and throttling
    temp = n.temp
    if temp is not None:
        if temp >= TEMP_NEAR_LIMIT:
            add("FAIL", "performance", f"CPU at {temp} °C while idle-ish", "", "Clean the cooler / replace the paste; check the airflow.")
        elif temp >= IDLE_TEMP_WARN and (n.cpu_pct or 0) < 30:
            add("WARN", "performance", f"CPU at {temp} °C with little load", "", "The cooling is weak - clean the fan and heat sink, check the paste.")
        else:
            add("OK", "performance", f"CPU temperature {temp} °C")
    if n.throttle_count:
        add("WARN", "performance", f"CPU thermal throttling happened {n.throttle_count}× since boot", "", "It overheated under load: improve the cooling, or cap the load.")
    gov = n.gov[0] if n.gov else ""
    if gov and gov not in ("performance",):
        add("INFO", "performance", f"CPU governor is '{gov}'", "", "For the maximum speed: cpupower frequency-set -g performance (uses more power).")
    if n.turbo_off:
        add("INFO", "performance", "CPU turbo boost is off", "", "Enable it in the BIOS / intel_pstate/no_turbo=0 for the maximum speed.")
    # memory, swap, pressure
    pct = n.mem_pct
    if pct is not None and 100 - pct < RAM_FREE_WARN_PCT:
        add("WARN", "performance", f"Only {100 - pct:.0f} % of RAM is free", "", "Add RAM, or limit what runs here.")
    if n.swap_total_mib and n.swap_used_mib and n.swap_used_mib > 64:
        add("WARN", "performance", f"Swap is used ({n.swap_used_mib} MiB)", "", "The node is short of RAM; Kubernetes prefers swap off.")
    elif n.swap_total_mib:
        add("INFO", "performance", f"Swap is enabled ({n.swap_total_mib} MiB)", "", "Kubernetes recommends swap off (swapoff -a, and remove it from /etc/fstab).")
    for key, value in (n.psi or {}).items():
        if key.endswith("some") and value is not None and value >= PSI_WARN:
            add("WARN", "performance", f"Pressure stall: {key} {value:.1f} %", "", "Processes wait for that resource at rest.")
    # network cards
    for nic in n.nics or []:
        label = nic.get("name", "?")
        if nic.get("state") not in (None, "up", "unknown"):
            continue
        if nic.get("duplex") == "half":
            add("WARN", "network", f"{label}: half duplex", "", "Check the cable and the switch port (auto-negotiation failed).")
        errs = (nic.get("rx_err", 0) or 0) + (nic.get("tx_err", 0) or 0)
        drops = (nic.get("rx_drop", 0) or 0) + (nic.get("tx_drop", 0) or 0)
        if errs:
            add("WARN", "network", f"{label}: {errs} transmit/receive errors", "", "Replace the cable, try another switch port.")
        if drops > 1000:
            add("INFO", "network", f"{label}: {drops} dropped packets", "", "Can be harmless (a busy interface); watch whether it grows.")
    # peers
    lost = [p for p, ms in (n.peers or {}).items() if ms is None]
    if n.peers and not lost:
        add("OK", "network", f"Ping to the other nodes works ({len(n.peers)})")
    if lost:
        add("WARN", "network", f"No ping to: {', '.join(lost)}", "", "A firewall, a cable or a node that is off.")
    slow = [f"{p} {ms:.1f} ms" for p, ms in (n.peers or {}).items() if ms is not None and ms > PEER_WARN_MS]
    if slow:
        add("INFO", "network", "Slow ping inside the LAN", ", ".join(slow), "A LAN ping should be under 1 ms; check Wi-Fi, switches and the load of the network.")
    # requests
    cpu_alloc = _cpu_m(n.alloc.get("cpu"))
    if cpu_alloc and n.req_cpu_m * 100 / cpu_alloc >= REQUEST_WARN_PCT:
        add("WARN", "pods", f"CPU requests are {n.req_cpu_m * 100 / cpu_alloc:.0f} % of the node", "", "New pods will not fit; add a node or lower the requests.")
    if n.alloc.get("memory") and n.mem_alloc_mib and n.req_mem_mib * 100 / n.mem_alloc_mib >= REQUEST_WARN_PCT:
        add("WARN", "pods", f"RAM requests are {n.req_mem_mib * 100 / n.mem_alloc_mib:.0f} % of the node", "", "New pods will not fit; add RAM or lower the requests.")
    if prev_restarts is None:
        if n.restarts >= RESTARTS_WARN:
            add("INFO", "pods", f"{n.restarts} container restarts on this node since its pods started (cumulative)",
                "", "The next check shows only the NEW ones. kubectl get pods -A -o wide | grep " + name)
    elif n.restarts - prev_restarts >= RESTARTS_WARN:
        add("WARN", "pods", f"{n.restarts - prev_restarts} new container restarts on this node since the last check", "",
            "kubectl get pods -A -o wide | grep " + name)
    # the master's disk (etcd)
    if n.master:
        rot = [d for d in n.disks or [] if d.get("kind") == "HDD"]
        if rot and not any(d.get("kind") == "SSD/NVMe" for d in n.disks):
            add("WARN", "performance", "The control plane runs on a spinning disk", "", "etcd is slow on an HDD: move the master to an SSD.")
    if not n.has_baseline:
        add("INFO", "node", "No baseline stored for this node", "", "Run a test and store it (--set-baseline) so later tests are compared with it.")
    return out


def _cpu_m(value) -> int:
    text = str(value or "0")
    try:
        return int(text[:-1]) if text.endswith("m") else int(float(text) * 1000)
    except ValueError:
        return 0


def consistency_findings(nodes: list) -> list:
    out = []
    live = [n for n in nodes if n.ready]
    for attr, label, level in (("kubelet", "Kubernetes (k3s) version", "WARN"), ("runtime", "container runtime", "INFO"),
                               ("kernel", "kernel", "INFO"), ("os_image", "operating system", "INFO")):
        values = {getattr(n, attr) for n in live if getattr(n, attr)}
        if len(values) > 1:
            who = ", ".join(f"{n.name}: {getattr(n, attr)}" for n in live if getattr(n, attr))
            out.append(_f(level, "consistency", f"The nodes differ in {label}", who,
                          "Upgrade the odd ones out." if level == "WARN" else "Not wrong, but a uniform cluster is easier to run."))
    speeds = {}
    for n in live:
        for nic in n.nics or []:
            if nic.get("state") == "up" and nic.get("speed"):
                speeds[n.name] = max(speeds.get(n.name, 0), int(nic["speed"]))
    if len(set(speeds.values())) > 1:
        slow = min(speeds, key=speeds.get)
        out.append(_f("WARN", "network", f"{slow} has a slower link than the others ({speeds[slow]} Mbit/s)",
                      ", ".join(f"{k}: {v}" for k, v in sorted(speeds.items())), "A 100 Mbit link on a gigabit LAN is usually a bad cable or port."))
    mtus = {n.name: nic.get("mtu") for n in live for nic in (n.nics or []) if nic.get("state") == "up" and nic.get("mtu")}
    if len(set(mtus.values())) > 1:
        out.append(_f("WARN", "network", "The nodes use different MTUs", ", ".join(f"{k}: {v}" for k, v in sorted(mtus.items())),
                      "Different MTUs on one LAN cause slow or stalled transfers."))
    return out


# --- 3. light active checks (from the probe pods) ---------------------------------------------------------------------

EXTRA_SCRIPT = r"""
echo "now $(date +%s)"
if nslookup kubernetes.default.svc.cluster.local >/dev/null 2>&1; then echo "dns ok"; else echo "dns fail"; fi
exit 0
"""


def parse_extra(text: str, local_before: float, local_after: float) -> dict:
    """{"skew_s": seconds the node's clock is ahead of this computer (None = unknown), "dns_ok": bool | None}."""
    out = {"skew_s": None, "dns_ok": None}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "now" and parts[1].isdigit():
            out["skew_s"] = float(parts[1]) - (local_before + local_after) / 2
        elif parts[:1] == ["dns"] and len(parts) == 2:
            out["dns_ok"] = parts[1] == "ok"
    return out


def extras_findings(extras: dict) -> list:
    out = []
    for name, data in sorted(extras.items()):
        skew = data.get("skew_s")
        if skew is not None:
            if abs(skew) >= SKEW_FAIL_S:
                out.append(_f("FAIL", "clock", f"The clock is off by {skew:+.0f} s", "", "Fix the time (chrony / systemd-timesyncd): certificates and etcd need correct time.", name))
            elif abs(skew) >= SKEW_WARN_S:
                out.append(_f("WARN", "clock", f"The clock is off by {skew:+.1f} s", "", "Enable time synchronisation (chrony / systemd-timesyncd).", name))
        if data.get("dns_ok") is False:
            out.append(_f("WARN", "network", "DNS inside the cluster does not answer", "kubernetes.default.svc.cluster.local",
                          "Check CoreDNS (kubectl -n kube-system get pods -l k8s-app=kube-dns).", name))
    if extras and all(d.get("skew_s") is not None and abs(d["skew_s"]) < SKEW_WARN_S for d in extras.values()):
        out.append(_f("OK", "clock", "The clocks of all nodes agree"))
    if extras and all(d.get("dns_ok") for d in extras.values()):
        out.append(_f("OK", "network", "DNS works inside the cluster"))
    return out


# --- 4. the mini load test: responsiveness at rest and under load ------------------------------------------------------

def median_ms(values: list) -> Optional[float]:
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def load_findings(rest: dict, loaded: dict, previous: Optional[dict] = None, logs: Optional[dict] = None) -> list:
    """rest / loaded: {"api": ms, "<node>": ms of an exec into the node's probe}. previous: the `load` part of the previous report.
    logs: {node: {"peak_temp": °C, "avg_freq": MHz}} of the newest older load test of the node."""
    out = []
    for name in sorted(k for k in loaded if k != "api"):
        a, b = rest.get(name), loaded.get(name)
        if not a or not b:
            continue
        ratio = b / a
        if ratio >= SLOWDOWN_FAIL:
            out.append(_f("FAIL", "load", f"Under {LOAD_PCT} % load the node answers {ratio:.0f}× slower ({a:.0f} → {b:.0f} ms)", "",
                          "A node that stalls at half load is overcommitted or has a failing disk / CPU.", name))
        elif ratio >= SLOWDOWN_WARN:
            out.append(_f("WARN", "load", f"Responsiveness drops {ratio:.1f}× under {LOAD_PCT} % load ({a:.0f} → {b:.0f} ms)", "",
                          "Reserve CPU for the system (kubelet / system-reserved) or lower the number of pods.", name))
        else:
            out.append(_f("OK", "load", f"Responsive under load ({a:.0f} → {b:.0f} ms)", "", "", name))
        old = ((previous or {}).get(name) or {}).get("loaded")
        if old and b > old * 1.5 and b - old > 20:
            out.append(_f("WARN", "load", f"Slower under load than the last check ({old:.0f} → {b:.0f} ms)", "", "Something changed on this node since then.", name))
        ref = (logs or {}).get(name)
        if ref and ref.get("peak_temp") and ref.get("measured_temp") and ref["measured_temp"] >= ref["peak_temp"] - 3:
            out.append(_f("WARN", "load", f"At {LOAD_PCT} % load it is already as hot ({ref['measured_temp']} °C) as in an older full-load test ({ref['peak_temp']} °C)",
                          "", "The cooling got worse since that test.", name))
        if ref and ref.get("avg_freq") and ref.get("measured_freq") and ref["measured_freq"] < ref["avg_freq"] * 0.85:
            out.append(_f("WARN", "load", f"Clock {ref['measured_freq']} MHz at {LOAD_PCT} % load, an older full-load test averaged {ref['avg_freq']} MHz", "",
                          "A power limit or thermal throttling holds the CPU back.", name))
    a, b = rest.get("api"), loaded.get("api")
    if a and b:
        out.append(_f("WARN" if b > API_WARN_MS else "OK", "load", f"API server under load: {a:.0f} → {b:.0f} ms",
                      "", "The master struggles while the workers work." if b > API_WARN_MS else ""))
    return out


def older_load_reference(name: str, root: Optional[Path] = None) -> Optional[dict]:
    """The newest CPU test log of the node that ran at (nearly) full load: its peak temperature and average clock.
    A log of a lighter test (the doctor's own 50 % mini test, a network test ...) is not a reference."""
    from .logparse import read_log
    from .paths import result_dirs, user_log_root
    root = root or user_log_root()
    candidates = []
    for folder in result_dirs(root)[:30]:
        try:
            files = list(folder.glob(name + "-*.log"))
        except OSError:
            continue
        for path in files:
            try:
                candidates.append((path.stat().st_mtime, path))
            except OSError:
                continue
    for _mtime, path in sorted(candidates, reverse=True)[:12]:
        try:
            run = read_log(path)
        except Exception:                                              # noqa: BLE001 - an odd old log must not stop the check
            continue
        test = [s for s in run.samples if s.phase == "test"]
        if max((s.cpu_pct or 0 for s in test), default=0) < FULL_LOAD_PCT:
            continue
        temps = [s.cpu_temp for s in test if s.cpu_temp is not None]
        freqs = [s.freq_mhz for s in test if s.freq_mhz]
        if temps or freqs:
            return {"peak_temp": max(temps) if temps else None, "avg_freq": int(sum(freqs) / len(freqs)) if freqs else None, "log": str(path)}
    return None


# --- scoring -----------------------------------------------------------------------------------------------------------

def score(findings: list, nodes: list) -> dict:
    scores = {}
    for name in nodes:
        scores[name] = max(0, 100 - sum(PENALTY[f.level] for f in findings if f.node == name))
    general = sum(PENALTY[f.level] for f in findings if not f.node)
    scores["cluster"] = max(0, min([100] + [v for k, v in scores.items()]) - general) if scores else max(0, 100 - general)
    return scores


def build_report(view, extras: Optional[dict] = None, load: Optional[dict] = None, version: str = "", when: Optional[float] = None,
                 previous: Optional[dict] = None, logs: Optional[dict] = None) -> Report:
    nodes = [n.name for n in view.nodes]
    findings = [*api_findings(view)]
    old_restarts = (previous or {}).get("restarts") or {}                      # node -> restarts at the previous check
    for n in view.nodes:
        findings += node_findings(n, old_restarts.get(n.name))
    findings += consistency_findings(view.nodes)
    findings += extras_findings(extras or {})
    if load and load.get("error"):
        findings.append(_f("FAIL", "load", "The mini load test did not run", load["error"],
                           "Run it by hand to see why: ./stress.sh --nodes <workers> --parallel --cpu-load 50 --time 60"))
    elif load:
        findings += load_findings(load.get("rest", {}), load.get("loaded", {}), (previous or {}).get("load", {}).get("nodes"), logs)
    report = Report(when=when or time.time(), version=version, nodes=nodes, findings=findings, load=load or {})
    report.scores = score(findings, nodes)
    report.restarts = {n.name: n.restarts for n in view.nodes if n.probe_ok or n.pods}
    return report


# --- the previous report: what changed ---------------------------------------------------------------------------------

def key_of(f) -> tuple:
    d = f if isinstance(f, dict) else f.to_dict()
    return (d["node"], d["area"], d["title"].split(" (")[0].split(" at ")[0])


def changes(previous: Optional[dict], report: Report) -> dict:
    """{"new": [...], "fixed": [...]} - findings of WARN / FAIL that appeared or disappeared since the previous report."""
    if not previous:
        return {"new": [], "fixed": [], "first": True}
    common = set(previous.get("nodes", [])) & set(report.nodes)
    bad = lambda d: d["level"] in ("WARN", "FAIL") and (not d["node"] or d["node"] in common)       # noqa: E731
    before = {key_of(f): f for f in previous.get("findings", []) if bad(f)}              # a node missing from one of the reports is no "fixed"
    now = {key_of(f): f for f in (x.to_dict() for x in report.findings) if bad(f)}
    return {"new": [now[k] for k in now if k not in before], "fixed": [before[k] for k in before if k not in now], "first": False}


# --- files --------------------------------------------------------------------------------------------------------------

def report_dir(root: Optional[Path] = None) -> Path:
    from .paths import user_log_root
    return (root or user_log_root()) / "doctor"


def save(report: Report, root: Optional[Path] = None) -> tuple:
    """Writes <logs>/doctor/RRRR-MM-DD_HH-MM-SS.json and .md; returns (json path, md path)."""
    folder = report_dir(root)
    from .paths import make_private_dir                      # the report names nodes, addresses and versions: owner only (like the other logs)
    make_private_dir(folder)
    stem = time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime(report.when))
    jpath, mpath = folder / f"{stem}.json", folder / f"{stem}.md"
    for path, text in ((jpath, json.dumps(report.to_dict(), indent=1, ensure_ascii=False)), (mpath, to_markdown(report))):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    return jpath, mpath


def latest_report(root: Optional[Path] = None, before: Optional[float] = None) -> Optional[dict]:
    folder = report_dir(root)
    if not folder.is_dir():
        return None
    for path in sorted(folder.glob("*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("schema") == SCHEMA and (before is None or data.get("when", 0) < before):
            return data
    return None


def seen_path(root: Optional[Path] = None) -> Path:
    return report_dir(root) / ".seen"


def mark_seen(when: float, root: Optional[Path] = None) -> None:
    try:
        report_dir(root).mkdir(parents=True, exist_ok=True)
        seen_path(root).write_text(str(when), encoding="utf-8")
    except OSError:
        pass


def unseen_report(root: Optional[Path] = None) -> Optional[dict]:
    """The newest saved report when the user has not looked at it yet (the scheduled check runs unattended), else None."""
    data = latest_report(root)
    if not data:
        return None
    try:
        seen = float(seen_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        seen = 0.0
    return data if data.get("when", 0) > seen else None


def banner(root: Optional[Path] = None, now: Optional[float] = None) -> str:
    """One line for the menu: the last check (how long ago, the verdict) - '' when there has never been one."""
    data = latest_report(root)
    if not data:
        return ""
    ago = format_duration(int((now or time.time()) - data["when"]))
    fresh = unseen_report(root) is not None
    counts = {lv: sum(1 for f in data.get("findings", []) if f["level"] == lv) for lv in ("FAIL", "WARN")}
    return (f"🩺 cluster check {ago} ago: {data.get('verdict', '?')} ({data.get('scores', {}).get('cluster', 100)}/100, "
            f"{counts['FAIL']} fail, {counts['WARN']} warn)" + (" · NEW - 8 ▸ 5 or --doctor-last shows it" if fresh else ""))


def report_from_dict(data: dict) -> Report:
    findings = [Finding(**{k: f.get(k, "") for k in ("level", "area", "title", "detail", "advice", "node")}) for f in data.get("findings", [])]
    return Report(when=data.get("when", 0.0), version=data.get("version", ""), nodes=data.get("nodes", []), findings=findings,
                  scores=data.get("scores", {}), load=data.get("load", {}), restarts=data.get("restarts", {}))


def show_last(args=None, root: Optional[Path] = None) -> int:
    """`--doctor-last`: the newest saved report on the screen (also what the window of the scheduled check shows)."""
    if root is None and args is not None and getattr(args, "log_dir", None):
        from .paths import user_log_root
        root = user_log_root(args.log_dir)
    data = latest_report(root)
    if not data:
        ui.emit("No cluster check yet - run ./stress.sh --doctor first.")
        return 1
    report = report_from_dict(data)
    older = latest_report(root, before=report.when)
    print("\n" + "\n".join(render(report, ui.color_enabled(), diff=changes(older, report), show_ok=False)))
    ui.emit(f"{ui.ICON_STAGE} {time.strftime('%Y-%m-%d %H:%M', time.localtime(report.when))} · {report_dir(root)}")
    mark_seen(report.when, root)
    return 0


def to_markdown(report: Report) -> str:
    lines = [f"# Cluster doctor – {time.strftime('%Y-%m-%d %H:%M', time.localtime(report.when))}", "",
             f"Verdict: **{report.verdict}** · score {report.scores.get('cluster', 100)}/100 · "
             f"{report.count('FAIL')} fail · {report.count('WARN')} warn · {report.count('INFO')} tips", "",
             "| node | score |", "|---|---|", *[f"| {n} | {report.scores.get(n, 100)} |" for n in report.nodes], ""]
    for level in ("FAIL", "WARN", "INFO", "OK"):
        items = [f for f in report.findings if f.level == level]
        if not items:
            continue
        lines += [f"## {level} ({len(items)})", ""]
        for f in items:
            who = f"**{f.node}** – " if f.node else ""
            lines.append(f"- {who}{f.title}" + (f" — {f.detail}" if f.detail else ""))
            if f.advice:
                lines.append(f"  - what to do: {f.advice}")
        lines.append("")
    return "\n".join(lines)


# --- the screen ---------------------------------------------------------------------------------------------------------

def render(report: Report, on: bool, width: Optional[int] = None, diff: Optional[dict] = None, show_ok: bool = False) -> list:
    width = width or ui.panel_width()
    room = width - 4
    code = {"healthy": ui.GREEN, "mostly fine": ui.GREEN, "needs attention": ui.YELLOW, "in trouble": ui.RED}[report.verdict]
    head = [f"{ui.paint(report.verdict.upper(), ui.BOLD + ';' + code, on)}  score {report.scores.get('cluster', 100)}/100  ·  "
            f"{report.count('FAIL')} fail · {report.count('WARN')} warn · {report.count('INFO')} tips · {report.count('OK')} ok"]
    head += [ui.paint("  ".join(f"{n} {report.scores.get(n, 100)}" for n in report.nodes), ui.GREY, on)] if report.nodes else []
    sections = [head]
    for level in ("FAIL", "WARN", "INFO") + (("OK",) if show_ok else ()):
        rows = []
        for f in (x for x in report.findings if x.level == level):
            who = ui.paint(f"{f.node}: ", ui.BOLD, on) if f.node else ""
            lead = f"{ICONS[level]} {who}"
            for i, part in enumerate(ui.wrap(f"{f.title}" + (f" - {f.detail}" if f.detail else ""), room - 3 - ui.visible_len(who))):
                rows.append((lead if i == 0 else "   ") + part)
            for part in ui.wrap(f.advice, room - 6) if f.advice else []:
                rows.append("   " + ui.paint("→ " + part, ui.GREY, on))
        if rows:
            sections.append(rows)
    if diff and not diff.get("first"):
        rows = [f"{ui.paint('since the last check', ui.BOLD, on)}: {len(diff['new'])} new, {len(diff['fixed'])} fixed"]
        rows += [ui.paint("  + " + f["title"] + (f" ({f['node']})" if f["node"] else ""), ui.YELLOW, on) for f in diff["new"][:6]]
        rows += [ui.paint("  - " + f["title"] + (f" ({f['node']})" if f["node"] else ""), ui.GREEN, on) for f in diff["fixed"][:6]]
        sections.append(rows)
    elif diff:
        sections.append([ui.paint("first check - there is nothing to compare with yet", ui.GREY, on)])
    return ui.box("🩺 CLUSTER DOCTOR", sections, on, width)


# --- collecting (touches the cluster: read-only) ----------------------------------------------------------------------

def responsiveness(kube, probes: dict, samples: int = 3, clock: Callable[[], float] = time.monotonic) -> dict:
    """{"api": ms of /readyz, "<node>": median ms of a trivial exec into the node's probe pod}."""
    out = {}
    api = []
    for _ in range(samples):
        t = clock()
        try:
            kube.run("get", "--raw", "/readyz", timeout=10)
            api.append((clock() - t) * 1000)
        except Exception:                                              # noqa: BLE001
            api.append(None)
    out["api"] = median_ms(api)
    for node, probe in probes.items():
        times = []
        for _ in range(samples):
            t = clock()
            try:
                kube.exec(probe, "true", timeout=15)
                times.append((clock() - t) * 1000)
            except Exception:                                          # noqa: BLE001
                times.append(None)
        out[node] = median_ms(times)
    return out


def collect(kube, only: Optional[list] = None, say: Callable[[str], None] = lambda _t: None, rounds: int = 3, interval: float = 1.0,
            sleep: Callable[[float], None] = time.sleep, collector=None) -> tuple:
    """Starts the dashboard's probe pods (temporary, read-only), reads them `rounds` times and returns (view, extras, probes).
    The probe pods are removed before this returns unless `keep` is wanted by the caller (see run)."""
    from .dashboard import Collector
    c = collector or Collector(kube, only, keep_hours=0)
    say("starting the read-only probes …")
    c.start(say, True)
    try:
        for i in range(rounds):
            c.refresh()
            if i < rounds - 1:
                sleep(interval)
        extras = {}
        for node, (probe, _gpu) in list(c.pods.items()):
            before = time.time()
            try:
                text = kube.exec(probe, EXTRA_SCRIPT, timeout=25)
            except Exception:                                          # noqa: BLE001 - a node that cannot be asked is simply not judged
                continue
            extras[node] = parse_extra(text, before, time.time())
        probes = {node: pair[0] for node, pair in c.pods.items()}
        view = c.view
        view.nodes = [c.nodes[n] for n in c.nodes]
        return view, extras, probes, c
    except Exception:
        c.stop()
        raise


# --- the mini load test and the whole run -----------------------------------------------------------------------------

def load_command(workers: list, log_dir: Optional[str] = None) -> list:
    """The ordinary program, asked for the doctor's mini test: every worker at once, CPU and RAM at 50 %, a minute, no master."""
    import sys
    # the program takes ONE way to name the nodes: --nodes A,B (the workers we found) or, without a list, --workers
    argv = [sys.executable, "-m", "stress_test", "--nodes", ",".join(workers)] if workers else [sys.executable, "-m", "stress_test", "--workers"]
    argv += ["--parallel", "--profile", "classic", "--cpu-load", str(LOAD_PCT), "--ram-pct", str(LOAD_PCT), "--time", str(LOAD_SECONDS),
             "--cooldown", "0", "--log", "--non-interactive", "--yes"]
    if log_dir:
        argv += ["--log-dir", log_dir]
    return argv


def newest_log_stats(name: str, since: float, root: Optional[Path] = None) -> Optional[dict]:
    """Peak temperature and average clock of the log the mini test wrote for the node (a log not older than `since`)."""
    from .logparse import read_log
    from .paths import result_dirs, user_log_root
    best = None
    for folder in result_dirs(root or user_log_root())[:3]:
        try:
            files = list(folder.glob(name + "-*.log"))
        except OSError:
            continue
        for path in files:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime >= since and (best is None or mtime > best[0]):
                best = (mtime, path)
    if best is None:
        return None
    try:
        run = read_log(best[1])
    except Exception:                                                  # noqa: BLE001
        return None
    test = [s for s in run.samples if s.phase == "test"]
    temps = [s.cpu_temp for s in test if s.cpu_temp is not None]
    freqs = [s.freq_mhz for s in test if s.freq_mhz]
    return {"measured_temp": max(temps) if temps else None, "measured_freq": int(sum(freqs) / len(freqs)) if freqs else None}


def run_load(kube, probes: dict, workers: list, log_dir: Optional[str] = None, say: Callable[[str], None] = lambda _t: None,
             sleep: Callable[[float], None] = time.sleep, popen=None, output_dir: Optional[Path] = None) -> dict:
    """Starts the mini test as a child process and measures the responsiveness every few seconds while it runs.
    Returns {node/api: median ms under load}; if the child ended with an error before any measuring, "_error" holds the end of its output
    (a mini test that never ran must not look like a test that found nothing). The safety of the test itself is the program's own."""
    import subprocess
    popen = popen or subprocess.Popen
    say(f"mini load test: {len(workers)} worker(s) at {LOAD_PCT} % for {LOAD_SECONDS} s (the master is not loaded) …")
    from .paths import HIDDEN_LOGS, make_private_dir, open_private
    folder = make_private_dir(output_dir or HIDDEN_LOGS)
    out_path = Path(folder) / f"doctor-load-{time.strftime('%Y-%m-%d_%H-%M-%S')}.txt"
    with open_private(str(out_path), "w") as sink:
        child = popen(load_command(workers, log_dir), stdout=sink, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        samples: dict = {}
        started = time.monotonic()
        try:
            # the test needs about a minute to prepare its pods and then runs LOAD_SECONDS: measure while it runs
            while child.poll() is None and time.monotonic() - started < LOAD_SECONDS + 420:
                sleep(5)
                if time.monotonic() - started < 45:
                    continue                                             # still preparing: the load is not on yet
                for key, ms in responsiveness(kube, probes, samples=1).items():
                    samples.setdefault(key, []).append(ms)
        finally:
            if child.poll() is None:
                child.terminate()
    result = {key: median_ms(values) for key, values in samples.items()}
    code = child.poll()
    if code not in (0, None) and not samples:
        try:
            tail = " ".join(out_path.read_text(encoding="utf-8", errors="replace").split())[-300:]
        except OSError:
            tail = ""
        result["_error"] = f"the test ended with code {code}: {tail}"
    elif code not in (0, None):
        result["_note"] = f"the test ended with code {code}"
    return result


def run(args, kube=None, ask: Optional[Callable] = None, say: Callable[[str], None] = None, root: Optional[Path] = None,
        load_runner: Callable = run_load, collect_fn: Callable = collect) -> int:
    """`--doctor`: checks, optional mini load test (only after an explicit OK), report on the screen and in files. Exit code 0 = no
    failure, 1 = at least one FAIL (a script can react)."""
    from . import __version__, cli
    from .kube import Kubectl
    say = say or ui.emit
    kube = kube or Kubectl()
    ask = ask or cli.ask
    on = ui.color_enabled()
    if root is None and getattr(args, "log_dir", None):
        from .paths import user_log_root
        root = user_log_root(args.log_dir)
    only = [x.strip() for x in (getattr(args, "nodes", "") or "").split(",") if x.strip()] or None
    say(f"{ui.ICON_STAGE} Cluster doctor: read-only checks first (probe pods are removed at the end).")
    view, extras, probes, collector = collect_fn(kube, only, say)
    try:
        workers = [n.name for n in view.nodes if n.ready and not n.master]
        want_load = bool(getattr(args, "doctor_load", False))
        allowed = bool(getattr(args, "doctor_load_ok", False))
        load = {}
        if want_load and workers:
            if not allowed:
                if getattr(args, "non_interactive", False) and not getattr(args, "yes", False):
                    say("The mini load test needs your OK; it is skipped in a non-interactive run without --yes.")
                    want_load = False
                elif not getattr(args, "yes", False):
                    ui.show_question(f"Run the mini load test? (every worker at {LOAD_PCT} % CPU and RAM for {LOAD_SECONDS} s; the master is not loaded)",
                                     False, warning=True)
                    want_load = ask("y/n", "n").strip().lower() in ("y", "yes")
        elif want_load:
            say("There is no Ready worker to load.")
            want_load = False
        if want_load:
            started = time.time()
            rest = responsiveness(kube, probes)
            loaded = dict(load_runner(kube, probes, workers, getattr(args, "log_dir", None), say))
            error = loaded.pop("_error", "")
            loaded.pop("_note", None)
            load = {"rest": rest, "loaded": loaded, "started": started, "error": error,
                    "nodes": {n: {"rest": rest.get(n), "loaded": loaded.get(n)} for n in workers}}
        previous = latest_report(root)
        logs = {}
        if load:
            for name in workers:
                ref, new = older_load_reference(name, root), newest_log_stats(name, load["started"], root)
                if ref and new:
                    logs[name] = {**ref, **new}
        report = build_report(view, extras, load or None, __version__, previous=previous, logs=logs)
        diff = changes(previous, report)
        jpath, mpath = save(report, root)
        mark_seen(report.when, root)
        print("\n" + "\n".join(render(report, on, diff=diff)))
        say(f"{ui.ICON_STAGE} Report: {mpath}")
        return 1 if report.count("FAIL") else 0
    finally:
        collector.stop()
