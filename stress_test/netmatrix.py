"""Network matrix (--net-matrix): ping + iperf3 between every ordered pair of nodes, shown as a table."""
from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .kube import Kubectl, KubectlError
from .manifests import HW_LIMIT_MIB, IMAGE_UBUNTU, _base_pod
from .models import PodNames
from .net import PORT_BASE, PORT_SPAN, NET_RATE_CAP_MASTER, NetResult, parse_iperf, parse_kv, parse_ping

MX_READY = "MX-READY"
MX_PORT_BASE = 32100          # inside the NodePort range: the nodes' firewalls let in nothing else
MX_PING_COUNT = 10
MX_DEADLINE_MARGIN = 900
SLOW_PAIR_FRACTION = 0.7          # a pair below 70 % of the median throughput is reported
ASYMMETRY_FRACTION = 0.25         # A->B and B->A differing by more than 25 % are reported
POD_PREFIX = "net-mx"

MX_SETUP_SCRIPT = ("export DEBIAN_FRONTEND=noninteractive; "
                   "if apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq iperf3 iputils-ping >/dev/null 2>&1; "
                   f"then echo {MX_READY}; else echo MX-FAILED; fi; exec sleep infinity")
MX_LINK_SCRIPT = ("if=$(awk '$2==\"00000000\"{print $1; exit}' /proc/net/route); "
                  "echo \"if=$if speed=$(cat /sys/class/net/$if/speed 2>/dev/null) "
                  "duplex=$(cat /sys/class/net/$if/duplex 2>/dev/null)\"")


@dataclass
class Pair:
    client: str
    server: str
    mbps: Optional[float] = None
    retrans: Optional[int] = None
    ping_avg_ms: Optional[float] = None
    ping_loss_pct: Optional[float] = None
    error: str = ""
    capped: int = 0                    # Mbit/s cap of this pair (0 = none): its throughput is a limit, not a measurement


@dataclass
class MatrixResult:
    nodes: list[str]
    net_time: int
    started: str = ""
    links: dict = field(default_factory=dict)          # node -> {"if": .., "speed": .., "duplex": ..}
    pairs: list = field(default_factory=list)          # Pair, ordered (client -> server)
    skipped: dict = field(default_factory=dict)        # node -> reason

    def pair(self, client: str, server: str) -> Optional[Pair]:
        return next((p for p in self.pairs if p.client == client and p.server == server), None)


def matrix_pod(node: str, name: str, deadline: int, run_id: str) -> dict:
    """Long-lived helper pod of one node (hostNetwork, iperf3 + ping installed); commands run via exec."""
    pod = _base_pod(name, node, deadline, run_id, "netmx")
    pod["spec"]["hostNetwork"] = True
    pod["spec"]["dnsPolicy"] = "ClusterFirstWithHostNet"
    pod["spec"]["containers"] = [{
        "name": "net-mx",
        "image": IMAGE_UBUNTU,
        "securityContext": {"allowPrivilegeEscalation": False},
        "command": ["bash", "-c", MX_SETUP_SCRIPT],
        "resources": {"requests": {"memory": "64Mi"}, "limits": {"memory": f"{HW_LIMIT_MIB}Mi"}},
    }]
    return pod


def ping_script(target_ip: str) -> str:
    return f'echo "MX-PING $(ping -c {MX_PING_COUNT} -i 0.2 -q {target_ip} 2>&1 | tail -2 | tr \'\\n\' \' \')"'


def iperf_script(sender_ip: str, port: int, net_time: int, rate_mbit: int = 0) -> str:
    """iperf3 client run on the RECEIVING node with -R: the data flows from the sender (the iperf3 server) to it.
    A client that sends at full line rate is unreliable inside the pods (iperf3 3.16: 'unable to cancel thread:
    Permission denied' at the end of the test), a receiving client is not."""
    rate = f" -b {rate_mbit}M" if rate_mbit else ""
    return f'echo "MX-IPERF $(iperf3 -c {sender_ip} -p {port} -t {net_time} -R -J{rate} 2>&1 | tr -d \'\\n\')"'


def parse_pair_output(client: str, server: str, text: str) -> Pair:
    pair = Pair(client, server)
    for line in text.splitlines():
        if line.startswith("MX-PING "):
            ping = parse_ping(line[len("MX-PING "):])
            if ping:
                pair.ping_loss_pct = ping.values.get("loss_pct")
                pair.ping_avg_ms = ping.values.get("avg_ms")
        elif line.startswith("MX-IPERF "):
            res = parse_iperf("tcp", line[len("MX-IPERF "):])
            if res:
                pair.mbps, pair.retrans = res.values.get("mbps"), res.values.get("retrans")
            else:
                pair.error = "iperf3 failed (peer unreachable / port blocked?)"
    if pair.mbps is None and not pair.error:
        pair.error = "no result"
    return pair


def _wait_ready(kube: Kubectl, pod: str, timeout_s: int = 240) -> bool:
    if not kube.wait_ready(pod, 120):
        return False
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        text = kube.logs(pod)
        if MX_READY in text:
            return True
        if "MX-FAILED" in text:
            return False
        time.sleep(2)
    return False


def run_matrix(kube: Kubectl, nodes: list, net_time: int, out: Callable[[str], None] = print,
               rate_mbit: int = 0, run_id: Optional[str] = None,
               deadline_extra: int = 0) -> MatrixResult:
    """Runs the matrix on the given NodeInfo list (each needs internal_ip). Always deletes its pods."""
    names = PodNames.new(run_id)
    pods = {n.name: f"{POD_PREFIX}-{i}-{names.run_id}" for i, n in enumerate(nodes)}
    result = MatrixResult([n.name for n in nodes], net_time, time.strftime("%Y-%m-%d %H:%M:%S"))
    pair_count = len(nodes) * (len(nodes) - 1)
    deadline = pair_count * (net_time + 15) + MX_DEADLINE_MARGIN + deadline_extra
    try:
        out(f"⏳ Starting a helper pod on each of {len(nodes)} nodes (iperf3 is installed via apt)...")
        for n in nodes:
            kube.apply(matrix_pod(n.name, pods[n.name], deadline, names.run_id))
        ready = []
        for n in nodes:
            if _wait_ready(kube, pods[n.name]):
                ready.append(n)
            else:
                result.skipped[n.name] = "helper pod did not start (no internet for apt?)"
                out(f"⚠️  {n.name}: {result.skipped[n.name]}, left out.")
        for n in ready:
            try:
                result.links[n.name] = parse_kv("link", kube.exec(pods[n.name], MX_LINK_SCRIPT)).values
            except KubectlError:
                result.links[n.name] = {}
        index = 0
        taken = kube.used_node_ports()
        next_port = MX_PORT_BASE
        for server in ready:
            for client in ready:
                if client is server:
                    continue
                index += 1
                while next_port in taken:
                    next_port += 1
                port, next_port = next_port, next_port + 1
                capped = NET_RATE_CAP_MASTER if (server.is_control_plane or client.is_control_plane) and not rate_mbit \
                    else rate_mbit
                out(f"▶ [{index}/{len(ready) * (len(ready) - 1)}] {client.name} -> {server.name}"
                    + (f" (capped at {capped} Mbit/s)" if capped else ""))
                try:
                    # ping client -> server; the data flows client -> server: the iperf3 server runs on the CLIENT node
                    # and the iperf3 client (with -R) on the server node (see iperf_script)
                    text = kube.exec(pods[client.name], ping_script(server.internal_ip), timeout=MX_PING_COUNT + 40)
                    kube.exec(pods[client.name], f"iperf3 -s -1 -D -p {port}; sleep 1", timeout=30)
                    text += "\n" + kube.exec(pods[server.name], iperf_script(client.internal_ip, port, net_time, capped),
                                            timeout=net_time + 40)
                    pair = parse_pair_output(client.name, server.name, text)
                    pair.capped = capped
                except KubectlError as exc:
                    pair = Pair(client.name, server.name, error=f"exec failed: {exc}", capped=capped)
                result.pairs.append(pair)
                out("   " + describe_pair(pair))
    finally:
        kube.delete_pods(*pods.values())
    return result


def describe_pair(p: Pair) -> str:
    if p.error and p.mbps is None:
        return f"❌ {p.error}"
    bits = [f"{p.mbps:.0f} Mbit/s"]
    if p.ping_avg_ms is not None:
        bits.append(f"ping {p.ping_avg_ms:.2f} ms")
    if p.ping_loss_pct:
        bits.append(f"loss {p.ping_loss_pct:g} %")
    if p.retrans:
        bits.append(f"retransmits {p.retrans}")
    return ", ".join(bits)


def findings(result: MatrixResult) -> list[str]:
    notes = []
    for node, why in result.skipped.items():
        notes.append(f"{node} was not tested: {why}.")
    for node, link in result.links.items():
        speed = link.get("speed")
        if isinstance(speed, int) and 0 < speed < 1000:
            notes.append(f"{node}: the network card negotiated only {speed} Mb/s (cable / port / switch).")
        if link.get("duplex") == "half":
            notes.append(f"{node}: the link runs in half duplex.")
    good = [p for p in result.pairs if p.mbps and not p.capped]      # capped pairs (the master) say nothing about the link
    if len(good) >= 3:
        median = statistics.median(p.mbps for p in good)
        for p in good:
            if p.mbps < SLOW_PAIR_FRACTION * median:
                notes.append(f"{p.client} -> {p.server} is slow: {p.mbps:.0f} Mbit/s (median of all pairs {median:.0f}).")
        by_node: dict[str, list[float]] = {}
        for p in good:
            by_node.setdefault(p.client, []).append(p.mbps)
            by_node.setdefault(p.server, []).append(p.mbps)
        overall = statistics.mean(p.mbps for p in good)
        for node, values in by_node.items():
            if len(values) >= 2 and statistics.mean(values) < 0.8 * overall:
                notes.append(f"{node} is the weak spot: its pairs average {statistics.mean(values):.0f} Mbit/s "
                             f"(all pairs {overall:.0f}).")
    seen = set()
    for p in good:
        back = result.pair(p.server, p.client)
        key = frozenset((p.client, p.server))
        if back and back.mbps and key not in seen:
            seen.add(key)
            hi, lo = max(p.mbps, back.mbps), min(p.mbps, back.mbps)
            if lo < (1 - ASYMMETRY_FRACTION) * hi:
                notes.append(f"{p.client} <-> {p.server}: directions differ ({p.mbps:.0f} vs {back.mbps:.0f} Mbit/s).")
    for p in result.pairs:
        if p.ping_loss_pct:
            notes.append(f"{p.client} -> {p.server}: ping lost {p.ping_loss_pct:g} % of packets.")
        if p.error and p.mbps is None:
            notes.append(f"{p.client} -> {p.server}: {p.error}.")
    pings = [p.ping_avg_ms for p in result.pairs if p.ping_avg_ms]
    if len(pings) >= 3:
        median = statistics.median(pings)
        for p in result.pairs:
            if p.ping_avg_ms and p.ping_avg_ms > 3 * median and p.ping_avg_ms > 1.0:
                notes.append(f"{p.client} -> {p.server}: ping {p.ping_avg_ms:.2f} ms is high (median {median:.2f} ms).")
    return notes


def format_matrix(result: MatrixResult) -> list[str]:
    """Two tables (Mbit/s and ping ms): rows = client, columns = server; nodes are numbered to keep it narrow."""
    nodes = [n for n in result.nodes if n not in result.skipped]
    lines = ["NETWORK MATRIX (iperf3 TCP, ping) - row = client, column = server", ""]
    for i, name in enumerate(nodes, 1):
        link = result.links.get(name, {})
        speed = f"{link['speed']} Mb/s" if isinstance(link.get("speed"), int) and link["speed"] > 0 else "?"
        lines.append(f"  [{i}] {name}  ({link.get('if', '?')}, {speed})")
    for title, getter, fmt in (("Throughput, Mbit/s", lambda p: p.mbps, "{:.0f}"),
                               ("Ping, ms (average)", lambda p: p.ping_avg_ms, "{:.2f}")):
        lines += ["", title, "      " + "".join(f"{f'[{j}]':>9}" for j in range(1, len(nodes) + 1))]
        for i, client in enumerate(nodes, 1):
            cells = []
            for server in nodes:
                p = result.pair(client, server)
                value = getter(p) if p else None
                mark = "*" if (p and p.capped and title.startswith("Throughput")) else ""
                cells.append(f"{'-':>9}" if client == server else f"{(fmt.format(value) + mark) if value is not None else 'x':>9}")
            lines.append(f"  [{i}] " + "".join(cells))
    if any(p.capped for p in result.pairs):
        lines += ["", "* capped on purpose (the master keeps its network for the API): shows the limit, not the link."]
    notes = findings(result)
    lines += ["", *(f"⚠️  {n}" for n in notes)] if notes else ["", "✅ Nothing suspicious in the network matrix."]
    return lines


def matrix_to_dict(result: MatrixResult) -> dict:
    return {
        "schema_version": 1, "kind": "net-matrix", "started": result.started, "net_time": result.net_time,
        "nodes": result.nodes, "skipped": result.skipped, "links": result.links,
        "pairs": [{"client": p.client, "server": p.server, "mbps": p.mbps, "retrans": p.retrans,
                   "ping_avg_ms": p.ping_avg_ms, "ping_loss_pct": p.ping_loss_pct, "capped": p.capped,
                   "error": p.error}
                  for p in result.pairs],
        "findings": findings(result),
    }


def write_matrix(result: MatrixResult, log_dir, open_private) -> tuple:
    """Writes net-matrix-<stamp>.log (the tables) and .json next to the other results; returns both paths."""
    from pathlib import Path
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    base = Path(log_dir) / f"net-matrix-{stamp}"
    from .paths import make_private_dir
    make_private_dir(base.parent)
    log_path, json_path = base.with_suffix(".log"), base.with_suffix(".json")
    with open_private(log_path, "w") as fh:
        fh.write("\n".join(format_matrix(result)) + "\n")
    with open_private(json_path, "w") as fh:
        json.dump(matrix_to_dict(result), fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return log_path, json_path
