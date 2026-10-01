"""Network test (iperf3 + ping): job definitions, shell command, parsing of results, formatting and log lines."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

NET_TIME_DEFAULT = 10                    # s, one iperf3 test
MIN_NET_TIME, MAX_NET_TIME = 5, 60
NET_MODES = ("host", "pod")              # host = the nodes' real network, pod = the pod network (CNI/overlay)
NET_RATE_CAP_MASTER = 300                # Mbit/s: the master (as client or as peer) is not saturated
NET_UDP_MBIT = 100                       # Mbit/s of the UDP test (jitter / loss)
PING_COUNT, PING_INTERVAL = 50, 0.2      # 10 s
MTU_JOB_SECONDS = 2
# The nodes' firewalls (ufw) let in only the Kubernetes NodePort range from other machines - on the live cluster
# some nodes drop every other port - so iperf3 uses a port from the top of 30000-32767.
PORT_BASE = 32000
PORT_SPAN = 700
NET_DIR_NOTE = "iperf3 server listening"

DROP_WARN = 20                          # new dropped packets during the test from which a warning is printed
WARN_LINK_MBIT = 1000                    # a slower negotiated link is highlighted


EXTRAS = ("dns", "internet", "mtr", "service", "no-udp")     # optional jobs (--net-extra); "no-udp" leaves the UDP test out
EXTRA_SECONDS = {"dns": 3, "internet": 12, "mtr": 5}   # "service" takes one iperf3 test
INTERNET_HOST = "1.1.1.1"
INTERNET_URL = "http://archive.ubuntu.com/ubuntu/dists/noble/Contents-$(dpkg --print-architecture).gz"
DOWNLOAD_SECONDS = 8
WARN_WIFI_DBM = -70


def normalize_extras(extras) -> tuple[str, ...]:
    """Valid extras in the canonical order (ValueError for an unknown one)."""
    wanted = set(extras or ())
    if "all" in wanted:
        wanted = set(EXTRAS) - {"no-udp"}
    unknown = wanted - set(EXTRAS)
    if unknown:
        raise ValueError(f"Unknown network extra: {', '.join(sorted(unknown))} (choose from {', '.join(EXTRAS)}, all).")
    return tuple(e for e in EXTRAS if e in wanted)


def job_names(mode: str = "host", extras=()) -> tuple[str, ...]:
    """Names of the jobs in the order they run (the link checks are only meaningful on the host network)."""
    extras = normalize_extras(extras)
    core = ("ping", "mtu", "tcp-up", "tcp-down", "tcp-x4") + (() if "no-udp" in extras else ("udp",))
    extra_jobs = tuple(e for e in extras if e != "no-udp" and (e != "service" or mode == "pod"))
    extra_jobs = tuple("tcp-svc" if e == "service" else e for e in extra_jobs)
    return ("link", *core, *extra_jobs, "link-end") if mode == "host" else (*core, *extra_jobs)


def net_duration(net_time: int, extras=(), mode: str = "host") -> int:
    """Seconds of the load: ping + MTU probe + 4 iperf3 tests + the extras (the link reads take no time)."""
    total = int(PING_COUNT * PING_INTERVAL) + MTU_JOB_SECONDS + 4 * net_time
    for e in normalize_extras(extras):
        if e == "service":
            total += net_time if mode == "pod" else 0
        elif e == "no-udp":
            total -= net_time                       # the UDP test is left out
        else:
            total += EXTRA_SECONDS[e]
    return total


def packages(extras=()) -> str:
    """apt packages the client pod needs."""
    extras = normalize_extras(extras)
    return " ".join(["iperf3", "iputils-ping", *(["curl"] if "internet" in extras else []),
                     *(["mtr-tiny"] if "mtr" in extras else [])])


def net_port(run_id: str, avoid=()) -> int:
    """iperf3 port of a run: inside the NodePort range (open on firewalled nodes), different for concurrent runs,
    never one that a NodePort Service already uses (kube-proxy would swallow the traffic)."""
    try:
        port = PORT_BASE + int(run_id, 16) % PORT_SPAN
    except ValueError:
        port = PORT_BASE
    avoid = set(avoid)
    while port in avoid:
        port = PORT_BASE + (port - PORT_BASE + 1) % PORT_SPAN
    return port


@dataclass
class NetResult:
    name: str
    values: dict = field(default_factory=dict)

    def log_line(self) -> str:
        """Machine-readable line of the log (read back by parse_result_line)."""
        return f"Net result: {self.name} | " + " ".join(f"{k}={v}" for k, v in self.values.items())

    def line(self) -> str:
        return f"{self.name:<13} {describe(self)}"


_RESULT_RE = re.compile(r"^Net result:\s*(\S+)\s*\|\s*(.*)$")


def _number(text: str):
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def parse_result_line(line: str) -> Optional[NetResult]:
    m = _RESULT_RE.match(line.strip())
    if not m:
        return None
    values = {}
    for part in m.group(2).split():
        key, _, val = part.partition("=")
        values[key] = _number(val)
    return NetResult(m.group(1), values)


_PING_RTT = re.compile(r"=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)\s*ms")
_PING_LOSS = re.compile(r"([\d.]+)% packet loss")


def parse_ping(text: str) -> Optional[NetResult]:
    loss = _PING_LOSS.search(text)
    if not loss:
        return None
    values = {"loss_pct": float(loss.group(1))}
    rtt = _PING_RTT.search(text)
    if rtt:
        values.update(min_ms=float(rtt.group(1)), avg_ms=float(rtt.group(2)),
                      max_ms=float(rtt.group(3)), jitter_ms=float(rtt.group(4)))
    return NetResult("ping", values)


def parse_kv(name: str, text: str) -> NetResult:
    """`key=value key=value` payload (link, mtu)."""
    values = {}
    for part in text.split():
        key, _, val = part.partition("=")
        if key and val != "":
            values[key] = _number(val)
    return NetResult(name, values)


def parse_iperf(name: str, text: str) -> Optional[NetResult]:
    """NetResult from `iperf3 -J` (TCP: goodput at the receiver + retransmits; UDP: rate, jitter, loss)."""
    try:
        end = json.loads(text)["end"]
    except (ValueError, KeyError, TypeError):
        return None
    if "sum_received" in end:
        values = {"mbps": round(end["sum_received"]["bits_per_second"] / 1e6, 1)}
        if "retransmits" in end.get("sum_sent", {}):
            values["retrans"] = end["sum_sent"]["retransmits"]
        return NetResult(name, values)
    if "sum" in end:
        s = end["sum"]
        return NetResult(name, {"mbps": round(s.get("bits_per_second", 0) / 1e6, 1),
                                "jitter_ms": round(s.get("jitter_ms", 0.0), 3),
                                "loss_pct": round(s.get("lost_percent", 0.0), 2)})
    return None


def parse_payload(name: str, payload: str) -> Optional[NetResult]:
    """Dispatch by job name."""
    if name == "ping":
        return parse_ping(payload)
    if name in ("link", "link-end", "mtu", "dns"):
        return parse_kv(name, payload)
    if name == "internet-ping":
        res = parse_ping(payload)
        return NetResult(name, res.values) if res else None
    if name == "internet-down":
        kv = parse_kv(name, payload).values
        speed = kv.get("speed")
        if not isinstance(speed, (int, float)):
            return None
        return NetResult(name, {"mbps": round(speed * 8 / 1e6, 1), "ttfb_ms": round(float(kv.get("ttfb", 0)) * 1000, 1),
                                "code": kv.get("code", 0)})
    if name in ("mtr", "mtr-internet"):
        return parse_mtr(name, payload)
    return parse_iperf(name, payload)


_MTR_HOP = re.compile(r"\d+\.\|--\s+(\S+)\s+([\d.]+)%\s+\d+\s+[\d.]+\s+([\d.]+)")


def parse_mtr(name: str, text: str) -> Optional[NetResult]:
    """`mtr -r -w -n` report (lines joined with ';'): number of hops, loss and average of the last hop, worst loss on the way."""
    hops = _MTR_HOP.findall(text)
    if not hops:
        return None
    return NetResult(name, {"hops": len(hops), "loss_pct": float(hops[-1][1]), "avg_ms": float(hops[-1][2]),
                            "worst_loss": max(float(h[1]) for h in hops)})


def link_errors(start: NetResult, end: NetResult) -> NetResult:
    """Growth of the error/drop counters of the NIC during the test."""
    values = {k: end.values[k] - start.values[k] for k in ("rx_errors", "rx_dropped", "tx_errors", "tx_dropped")
              if isinstance(end.values.get(k), (int, float)) and isinstance(start.values.get(k), (int, float))}
    return NetResult("link-errors", values)


def describe(r: NetResult) -> str:
    v = r.values
    if r.name in ("link", "link-end"):
        speed = v.get("speed")
        s = f"{speed} Mb/s" if isinstance(speed, int) and speed > 0 else "speed unknown"
        wifi = f", Wi-Fi signal {v['wifi_dbm']} dBm" if "wifi_dbm" in v else ""
        return f"{v.get('if', '?')}: {s}, {v.get('duplex', '?')} duplex{wifi}"
    if r.name == "link-errors":
        total = sum(v.values())
        return "no new errors or drops" if total == 0 else ", ".join(f"{k} +{n}" for k, n in v.items() if n)
    if r.name == "ping":
        if "avg_ms" not in v:
            return f"no reply, loss {v.get('loss_pct', 100):g} %"
        return (f"avg {v['avg_ms']:.2f} ms (min {v['min_ms']:.2f}, max {v['max_ms']:.2f}, "
                f"jitter {v['jitter_ms']:.2f}) loss {v['loss_pct']:g} %")
    if r.name == "dns":
        fails = f", {v['fails']} failed" if v.get("fails") else ""
        return f"cluster name {v.get('cluster_ms', '?')} ms, external name {v.get('external_ms', '?')} ms (average of 10){fails}"
    if r.name == "internet-ping":
        return describe(NetResult("ping", v)).replace("no reply", "no reply from the internet")
    if r.name == "internet-down":
        if not v.get("mbps"):
            return f"download failed (HTTP {v.get('code', '?')})"
        return f"download {v['mbps']:.1f} Mbit/s, first byte after {v.get('ttfb_ms', 0):.0f} ms"
    if r.name.startswith("mtr"):
        return (f"{v['hops']} hop(s), last hop: loss {v['loss_pct']:g} %, avg {v['avg_ms']:.2f} ms"
                + (f" (loss on the way up to {v['worst_loss']:g} %)" if v["worst_loss"] > v["loss_pct"] else ""))
    if r.name == "mtu":
        return f"path MTU {v['mtu']} B" if v.get("mtu") else "path MTU below 1300 B or ICMP blocked"
    if r.name == "udp":
        return f"{v.get('mbps', 0):.1f} Mbit/s, jitter {v.get('jitter_ms', 0):.3f} ms, loss {v.get('loss_pct', 0):g} %"
    text = f"{v.get('mbps', 0):.1f} Mbit/s" + (" (via the Service / kube-proxy)" if r.name == "tcp-svc" else "")
    if "retrans" in v:
        text += f", retransmits {v['retrans']}"
    return text


def findings(results: list[NetResult]) -> list[str]:
    """Plain-language notes about problems found in the results."""
    by = {r.name: r for r in results}
    notes = []
    link = by.get("link")
    if link and isinstance(link.values.get("speed"), int) and 0 < link.values["speed"] < WARN_LINK_MBIT:
        notes.append(f"The network card negotiated only {link.values['speed']} Mb/s: check the cable, port and switch.")
    if link and link.values.get("duplex") == "half":
        notes.append("The link runs in half duplex: a cable/switch problem.")
    errors = by.get("link-errors")
    if errors:
        bad = {k: n for k, n in errors.values.items() if n and (k.endswith("_errors") or n >= DROP_WARN)}
        if bad:
            notes.append("The NIC counted new errors/drops during the test: "
                         + ", ".join(f"{k} +{n}" for k, n in bad.items()) + ".")
    ping = by.get("ping")
    if ping and ping.values.get("loss_pct", 0) > 0:
        notes.append(f"Ping lost {ping.values['loss_pct']:g} % of packets.")
    if ping and ping.values.get("jitter_ms", 0) > 1.0:
        notes.append(f"Ping jitter is high ({ping.values['jitter_ms']:.2f} ms).")
    up, down = by.get("tcp-up"), by.get("tcp-down")
    if up and down and up.values.get("mbps") and down.values.get("mbps"):
        hi, lo = max(up.values["mbps"], down.values["mbps"]), min(up.values["mbps"], down.values["mbps"])
        if lo < 0.75 * hi:
            notes.append(f"The directions differ a lot (up {up.values['mbps']:.0f}, down {down.values['mbps']:.0f} "
                         f"Mbit/s): a duplex/cable/driver problem is possible.")
    x4, one = by.get("tcp-x4"), by.get("tcp-up")
    if x4 and one and one.values.get("mbps") and x4.values.get("mbps", 0) > 1.3 * one.values["mbps"]:
        notes.append("Four streams are much faster than one: a single connection is limited (window, CPU, driver).")
    for r in results:
        if r.name.startswith("tcp") and r.values.get("retrans", 0) > 100:
            notes.append(f"{r.name}: many TCP retransmits ({r.values['retrans']}): packets are being lost on the way.")
            break
    if link and isinstance(link.values.get("wifi_dbm"), (int, float)) and link.values["wifi_dbm"] <= WARN_WIFI_DBM:
        notes.append(f"The Wi-Fi signal is weak ({link.values['wifi_dbm']} dBm): a cable would be better for a cluster node.")
    dns = by.get("dns")
    if dns and dns.values.get("fails"):
        notes.append(f"{dns.values['fails']} DNS lookups failed.")
    if dns and isinstance(dns.values.get("cluster_ms"), (int, float)) and dns.values["cluster_ms"] > 50:
        notes.append(f"Cluster DNS (CoreDNS) is slow: {dns.values['cluster_ms']} ms per lookup.")
    if dns and isinstance(dns.values.get("external_ms"), (int, float)) and dns.values["external_ms"] > 200:
        notes.append(f"External DNS is slow: {dns.values['external_ms']} ms per lookup.")
    inet = by.get("internet-ping")
    if inet and inet.values.get("loss_pct", 0) > 0:
        notes.append(f"The internet ping lost {inet.values['loss_pct']:g} % of packets.")
    down = by.get("internet-down")
    if down and not down.values.get("mbps"):
        notes.append("The internet download test failed (no route, DNS or a proxy?).")
    for r in results:
        if r.name.startswith("mtr") and r.values.get("loss_pct", 0) > 0:
            notes.append(f"{r.name}: the last hop lost {r.values['loss_pct']:g} % of packets.")
    svc, up = by.get("tcp-svc"), by.get("tcp-up")
    if svc and up and svc.values.get("mbps") and up.values.get("mbps") and svc.values["mbps"] < 0.75 * up.values["mbps"]:
        notes.append(f"Through the Service (kube-proxy) it is slower: {svc.values['mbps']:.0f} vs "
                     f"{up.values['mbps']:.0f} Mbit/s directly.")
    udp = by.get("udp")
    if udp and udp.values.get("loss_pct", 0) > 1:
        notes.append(f"UDP lost {udp.values['loss_pct']:g} % of packets at {NET_UDP_MBIT} Mbit/s.")
    return notes


_LINK_FN = r"""linkinfo() { d=/sys/class/net/$if; w=""; \
if [ -d $d/wireless ]; then w=" wifi_dbm=$(awk -v i="$if:" '$1==i{gsub(/\./,"",$4); print $4}' /proc/net/wireless)"; fi; \
echo "if=$if speed=$(cat $d/speed 2>/dev/null) duplex=$(cat $d/duplex 2>/dev/null) \
rx_errors=$(cat $d/statistics/rx_errors 2>/dev/null) rx_dropped=$(cat $d/statistics/rx_dropped 2>/dev/null) \
tx_errors=$(cat $d/statistics/tx_errors 2>/dev/null) tx_dropped=$(cat $d/statistics/tx_dropped 2>/dev/null)$w"; }"""


def build_net_command(target: str, port: int, mode: str, net_time: int, rate_mbit: int = 0,
                      extras=(), service_ip: str = "") -> str:
    """One shell script running the jobs in order. Markers: NET-JOB i/n name, NET-RESULT name payload, NET-DONE / NET-FAILED name."""
    extras = normalize_extras(extras)
    names = job_names(mode, extras)
    if "tcp-svc" in names and not service_ip:
        names = tuple(x for x in names if x != "tcp-svc")
    n = len(names)
    rate = f" -b {rate_mbit}M" if rate_mbit else ""
    udp_rate = min(NET_UDP_MBIT, rate_mbit) if rate_mbit else NET_UDP_MBIT

    def iperf(name: str, opts: str, host: str = target, soft: bool = False) -> str:
        # iperf3 3.16 sometimes dies at the end of a line-rate test with "unable to cancel thread: Permission denied"
        # (no result in the JSON): one retry, and only then the test counts as failed
        run = f"iperf3 -c {host} -p {port} -t {net_time} -J{opts}"
        # a soft job (UDP: the firewall often lets in only TCP) does not stop the test, it is reported as skipped with the reason
        fail = (f"echo \"NET-SKIPPED {name} $(echo \"$out\" | tr -d '\\n' | cut -c1-160)\"; " if soft
                else f"echo \"NET-FAILED {name}\"; exit 1; ")
        ok = f"echo \"NET-RESULT {name} $(echo \"$out\" | tr -d '\\n')\"; "
        run = f"timeout {net_time + 10} {run}" if soft else run
        return (f"out=$({run} 2>&1); echo \"$out\" | grep -q 'sum_received\\|lost_percent' || "
                f"{{ sleep 2; out=$({run} 2>&1); }}; "
                f"if echo \"$out\" | grep -q 'sum_received\\|lost_percent'; then {ok}else {fail}fi; ")

    def job(name: str) -> str:
        return f'i=$((i+1)); echo "NET-JOB $i/{n} {name}"; '

    parts = ["i=0; "]
    if mode == "host":
        parts += [r"if=$(awk '$2==\"00000000\"{print $1; exit}' /proc/net/route); ".replace('\\"', '"'), _LINK_FN + "; ",
                  job("link"), 'echo "NET-RESULT link $(linkinfo)"; ']
    parts += [job("ping"),
              f'echo "NET-RESULT ping $(ping -c {PING_COUNT} -i {PING_INTERVAL} -q {target} 2>&1 | tail -2 | tr \'\\n\' \' \')"; ',
              job("mtu"),
              f"mtu=0; for s in 1472 1422 1372 1272; do if ping -c 2 -W 1 -M do -s $s -q {target} >/dev/null 2>&1; "
              f'then mtu=$((s+28)); break; fi; done; echo "NET-RESULT mtu mtu=$mtu"; ',
              job("tcp-up"), iperf("tcp-up", rate),
              job("tcp-down"), iperf("tcp-down", f" -R{rate}"),
              job("tcp-x4"), iperf("tcp-x4", f" -P 4{rate}")]
    if "udp" in names:
        parts += [job("udp"), iperf("udp", f" -u -b {udp_rate}M", soft=True)]
    if "dns" in names:
        parts += [job("dns"),
                  'f=0; t0=$(date +%s%N); for k in 1 2 3 4 5 6 7 8 9 10; do '
                  'getent hosts kubernetes.default.svc.cluster.local >/dev/null 2>&1 || f=$((f+1)); done; t1=$(date +%s%N); '
                  'for k in 1 2 3 4 5 6 7 8 9 10; do getent hosts archive.ubuntu.com >/dev/null 2>&1 || f=$((f+1)); done; t2=$(date +%s%N); '
                  'echo "NET-RESULT dns cluster_ms=$(awk -v a=$t0 -v b=$t1 \'BEGIN{printf "%.2f",(b-a)/10/1e6}\') '
                  'external_ms=$(awk -v a=$t1 -v b=$t2 \'BEGIN{printf "%.2f",(b-a)/10/1e6}\') fails=$f"; ']
    if "internet" in names:
        parts += [job("internet"),
                  f'echo "NET-RESULT internet-ping $(ping -c 5 -i 0.2 -q {INTERNET_HOST} 2>&1 | tail -2 | tr \'\\n\' \' \')"; ',
                  f'echo "NET-RESULT internet-down $(curl -s -o /dev/null --range 0-49999999 --max-time {DOWNLOAD_SECONDS} '
                  f'-w \'speed=%{{speed_download}} ttfb=%{{time_starttransfer}} code=%{{http_code}}\' {INTERNET_URL} 2>/dev/null)"; ']
    if "mtr" in names:
        parts += [job("mtr"),
                  f'echo "NET-RESULT mtr $(mtr -r -w -n -c 5 {target} 2>&1 | tr \'\\n\' \';\')"; ']
    if "tcp-svc" in names:
        parts += [job("tcp-svc"), iperf("tcp-svc", rate, service_ip)]
    if mode == "host":
        parts += [job("link-end"), 'echo "NET-RESULT link-end $(linkinfo)"; ']
    parts.append("echo NET-DONE")
    return "".join(parts)


NET_SERVER_SCRIPT = ("export DEBIAN_FRONTEND=noninteractive; "
                     "apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq iperf3 >/dev/null 2>&1 "
                     "|| { echo NET-SERVER-FAILED; exit 1; }; exec iperf3 -s --forceflush -p PORT")
