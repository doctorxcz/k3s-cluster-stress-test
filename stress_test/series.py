"""Test of several nodes one after another (workers or the whole cluster) with a shared summary and log."""
from __future__ import annotations

import dataclasses
import logging
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

from .kube import Kubectl
from .models import MAX_NODE_BUSY_PCT, WARN_TEMP, NodeInfo, NodeWorkload, StressConfig
from .parsing import describe_duration, describe_steps, format_duration, format_workload
from .paths import make_private_dir, open_private
from .runner import (EXIT_ERROR, EXIT_INTERRUPTED, EXIT_OK, EXIT_OVERHEAT, EXIT_PREMATURE,
                     RunnerError, StressRunner)
from .summary import RunStats, StressMetric

log = logging.getLogger(__name__)

PREP_SECONDS = 90          # estimate of the pod preparation on one node (hw-info, probe, apt)
_STATUS = {EXIT_OK: "OK", EXIT_OVERHEAT: "OVERHEATED", EXIT_PREMATURE: "PREMATURE",
           EXIT_ERROR: "ERROR", EXIT_INTERRUPTED: "INTERRUPTED"}


@dataclass
class NodeOutcome:
    """Result of one node in the series."""

    name: str
    is_master: bool = False
    code: Optional[int] = None            # None = the node was not tested
    reason: str = ""                      # why it was not tested
    stats: Optional[RunStats] = None
    metrics: list[StressMetric] = field(default_factory=list)
    log_path: Optional[str] = None

    @property
    def status(self) -> str:
        if self.code is None:
            return "SKIPPED"
        return _STATUS.get(self.code, f"CODE {self.code}")


@dataclass
class SeriesOptions:
    """Run settings passed to every node unchanged."""

    log_dir: Path
    out: Callable[[str], None] = print
    interval: float = 5.0
    remaining_every: int = 3
    allow_no_sensor: bool = False
    hw_privileged: bool = False
    skip_hw: bool = False
    max_busy_pct: int = MAX_NODE_BUSY_PCT
    allow_busy_node: bool = False
    skip_capacity_check: bool = False


# --- node selection and plan -----------------------------------------------------------------

def select_nodes(kube: Kubectl, scope: str, names: Optional[Sequence[str]] = None,
                 include_master: bool = False) -> tuple[list[NodeInfo], list[tuple[str, str]]]:
    """Nodes to test (workers first, master last) and skipped ones (name, reason).

    scope: "workers" (workers only), "cluster" (master too), "nodes" (a list of names).
    The master is included for "cluster", for "nodes" when it is in the list, and with include_master.
    """
    available = kube.list_node_names()
    if scope == "nodes":
        unknown = [n for n in (names or []) if n not in available]
        if unknown:
            raise ValueError(f"Unknown nodes: {', '.join(unknown)}. "
                             f"Available: {', '.join(available)}.")
        wanted = list(dict.fromkeys(names or []))
    else:
        wanted = available
    infos = [kube.get_node(name) for name in wanted]
    workers = [n for n in infos if not n.is_control_plane]
    masters = [n for n in infos if n.is_control_plane]
    with_master = scope in ("cluster", "nodes") or include_master
    chosen = workers + (masters if with_master else [])
    ready = [n for n in chosen if n.ready]
    skipped = [(n.name, "node is not Ready") for n in chosen if not n.ready]
    return ready, skipped


def plan_config(template: StressConfig, node: NodeInfo) -> tuple[StressConfig, list[str]]:
    """Test settings for one node; for the master its limits are used (CPU cap 70 %)."""
    cfg = dataclasses.replace(template, node=node.name, background=False)
    messages = cfg.apply_master_limits() if node.is_control_plane else []
    cfg.validate()
    return cfg, messages


def estimate_seconds(cfg: StressConfig) -> int:
    """Rough estimate of the length of the test of one node (preparation + load + cooldown)."""
    return PREP_SECONDS + cfg.total_duration + cfg.cooldown


def estimate_total(template: StressConfig, nodes: Sequence[NodeInfo], parallel: bool = False) -> int:
    """Estimate of the whole series: one after another the sum, at once the longest worker and then the masters one after another."""
    est = {n.name: estimate_seconds(plan_config(template, n)[0]) for n in nodes}
    if not parallel:
        return sum(est.values())
    workers = [est[n.name] for n in nodes if not n.is_control_plane]
    masters = [est[n.name] for n in nodes if n.is_control_plane]
    return max(workers, default=0) + sum(masters)


def plan_lines(template: StressConfig, nodes: Sequence[NodeInfo],
               workloads: Optional[dict[str, NodeWorkload]] = None,
               parallel: bool = False) -> list[str]:
    """Overview before the start: order of nodes, time estimate, services on the nodes."""
    workers = [n for n in nodes if not n.is_control_plane]
    has_master = len(workers) != len(nodes)
    if parallel and len(workers) > 1:
        lines = [f"Test {len(nodes)} nodes: {len(workers)} workers AT ONCE"
                 + (", master alone afterwards (CPU cap 70 %)." if has_master else ".")]
    else:
        parallel = False
        lines = [f"Test {len(nodes)} nodes one after another (master always last)."
                 if len(nodes) > 1 else "Test of 1 node."]
    if template.stepped:
        lines.append(f"Profile: stepped {describe_steps(template.steps)} % for "
                     f"{describe_duration(template.step_time)}")
    elif template.spike:
        lines.append(f"Profile: spike - {template.spike_cycles}x "
                     f"({describe_duration(template.spike_low_time)} / "
                     f"{describe_duration(template.spike_high_time)} at {template.spike_target} %)")
    else:
        lines.append(f"Profile: classic, CPU load {template.cpu_load} % for "
                     f"{describe_duration(template.duration)}")
    for index, node in enumerate(nodes, 1):
        cfg, messages = plan_config(template, node)
        seconds = estimate_seconds(cfg)
        role = " (MASTER)" if node.is_control_plane else ""
        lines.append(f"  {index}. {node.name}{role}: about {format_duration(seconds)}")
        for message in messages:
            lines.append(f"       ⚠️  {message}")
        work = (workloads or {}).get(node.name)
        if work and work.user_pods:
            for text in format_workload(work):
                lines.append(f"       services: {text}")
        elif work and work.system_count:
            lines.append(f"       only system pods ({work.system_count}× kube-system)")
    total = estimate_total(template, nodes, parallel)
    lines.append(f"Estimate in total: about {format_duration(total)} "
                 f"(preparation, load and cooldown"
                 + (", workers at once)." if parallel else " of all nodes)."))
    if parallel:
        lines.append("⚠️  Running in parallel raises the power draw from the socket and warms up the room (check the circuit breaker "
                     "and the power strips). Results are not exactly comparable with a one-after-another test.")
    return lines


def preflight(kube: Kubectl, nodes: Sequence[NodeInfo]) -> None:
    """No other test of this tool may be running on the planned nodes.

    Node usage (kubectl top) and temperature are checked just before the start of
    each individual node (`StressRunner._check_capacity`/`_check_sensor`), not here
    in advance - in a series of nodes tested one after another they would be stale
    by the time each node's turn comes anyway.
    """
    busy = {p.node for p in kube.list_tool_pods() if p.phase in ("Running", "Pending")}
    clash = [n.name for n in nodes if n.name in busy]
    if clash:
        raise RunnerError(f"On nodes {', '.join(clash)} another test is running. "
                          f"Wait for it to finish, or stop it.")


# --- series summary ---------------------------------------------------------------------------

def aggregate_code(outcomes: Sequence[NodeOutcome]) -> int:
    """Worst result of the series: interruption > overheating > error/premature end > OK."""
    codes = [o.code for o in outcomes if o.code is not None]
    if EXIT_INTERRUPTED in codes:
        return EXIT_INTERRUPTED
    if EXIT_OVERHEAT in codes:
        return EXIT_OVERHEAT
    if any(c not in (EXIT_OK,) for c in codes):
        return EXIT_ERROR
    return EXIT_OK


def _cpu_ops(outcome: NodeOutcome) -> str:
    ops = [m.ops_per_s for m in outcome.metrics if m.name == "cpu"]
    return f"{ops[-1]:.1f}" if ops else "-"


def _recovery(stats: RunStats) -> str:
    if stats.recovery_reached is None:
        return "?"
    return f"{stats.recovery_s:.0f} s" if stats.recovery_reached else "not reached"


def format_cluster_summary(outcomes: Sequence[NodeOutcome], warn_temp: int = WARN_TEMP) -> list[str]:
    """Final table of the series (terminal and log)."""
    width = 104
    head = (f"{'Node':<26}{'State':<12}{'max °C':<8}{f'over {warn_temp} °C':<11}"
            f"{'throttling':<12}{'cpu perf':<11}{'return to idle'}")
    out = ["=" * width, "CLUSTER SUMMARY", "=" * width, head, "-" * width]
    for o in outcomes:
        name = o.name + (" (master)" if o.is_master else "")
        if o.stats is None:
            out.append(f"{name:<26}{o.status:<12}{o.reason}".rstrip())
            continue
        s = o.stats
        temp = f"{s.temp_max}" if s.temp_max is not None else "?"
        thr = "SUSPECTED" if s.throttling else "no"
        out.append(f"{name:<26}{o.status:<12}{temp:<8}"
                   f"{format_duration(round(s.above_s)):<11}{thr:<12}"
                   f"{_cpu_ops(o):<11}{_recovery(s)}")
    out.append("-" * width)

    staged = [o for o in outcomes if o.stats and o.stats.stages]
    if staged:
        out.append("Temperatures per stage (max):")
        for o in staged:
            parts = " | ".join(
                f"{st.target} % → {st.temp_max if st.temp_max is not None else '?'} °C"
                for st in o.stats.stages)
            out.append(f"  {o.name:<24}{parts}")
        out.append("-" * width)
    tested = [o for o in outcomes if o.stats and o.stats.temp_max is not None]
    if tested:
        hottest = max(tested, key=lambda o: o.stats.temp_max)
        out.append(f"Hottest: {hottest.name} ({hottest.stats.temp_max} °C).")
    logs = [o for o in outcomes if o.log_path]
    if logs:
        out.append("Node logs:")
        out.extend(f"  {o.name}: {o.log_path}" for o in logs)
    out.append("=" * width)
    return out


# --- series run ---------------------------------------------------------------------------------

class SeriesRunner:
    """Tests the nodes one by one, writes the series log and prints the summary at the end."""

    parallel = False               # ParallelSeriesRunner overrides it to True

    def __init__(self, kube: Kubectl, template: StressConfig, nodes: Sequence[NodeInfo],
                 skipped: Sequence[tuple[str, str]], options: SeriesOptions,
                 log_path: str) -> None:
        self.kube = kube
        self.template = template
        self.nodes = list(nodes)
        self.skipped = list(skipped)
        self.options = options
        self.log_path = log_path
        self.abort_reason = ""         # non-empty = the series was stopped from outside (e.g. API response)

    def _log(self, text: str) -> None:
        with open_private(self.log_path, "a") as fh:
            fh.write(text + "\n")

    def _say(self, text: str) -> None:
        self.options.out(text)
        self._log(text)

    def _header(self) -> None:
        t = self.template
        self._log("=== CLUSTER TEST ===")
        self._log(f"Started: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        if t.stepped:
            self._log(f"Profile: stepped {describe_steps(t.steps)} % for "
                      f"{describe_duration(t.step_time)}")
        elif t.spike:
            self._log(f"Profile: spike - {t.spike_cycles}x "
                      f"({describe_duration(t.spike_low_time)} / "
                      f"{describe_duration(t.spike_high_time)} at {t.spike_target} %)")
        else:
            self._log(f"Profile: classic, CPU {t.cpu_load} % for {describe_duration(t.duration)}")
        self._log("Nodes: " + ", ".join(n.name + (" (master)" if n.is_control_plane else "")
                                       for n in self.nodes))
        if self.parallel:
            self._log("Concurrency: workers at once, master alone afterwards")
        if t.notes:
            self._log(f"Notes: {t.notes}")
        for name, reason in self.skipped:
            self._log(f"Skipped: {name} ({reason})")
        self._log("----------------------------------------------------")

    def _test_node(self, index: int, node: NodeInfo) -> NodeOutcome:
        outcome = NodeOutcome(node.name, node.is_control_plane)
        cfg, messages = plan_config(self.template, node)
        self._say("")
        self._say("=" * 52)
        self._say(f"NODE {index}/{len(self.nodes)}: {node.name}"
                  + (" (master)" if node.is_control_plane else ""))
        self._say("=" * 52)
        for message in messages:
            self._say(f"⚠️  {message}")
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        log_path = str(self.options.log_dir / f"{node.name}-{cfg.duration}s-{stamp}.log")
        runner = StressRunner(
            self.kube, node, cfg, log_path=log_path, interval=self.options.interval,
            remaining_every=self.options.remaining_every,
            allow_no_sensor=self.options.allow_no_sensor, confirm_no_sensor=None,
            out=self.options.out, hw_privileged=self.options.hw_privileged,
            skip_hw=self.options.skip_hw, max_busy_pct=self.options.max_busy_pct,
            allow_busy_node=self.options.allow_busy_node,
            skip_capacity_check=self.options.skip_capacity_check)
        try:
            code = runner.run()
        except Exception as exc:                       # noqa: BLE001 - one node does not stop the series
            log.exception("Test of node %s failed unexpectedly", node.name)
            self._say(f"❌ Test of node {node.name} failed: {exc}")
            code = EXIT_ERROR
        outcome.code = code
        outcome.stats = runner.stats
        outcome.metrics = runner.metrics
        outcome.log_path = log_path
        self._log(f"[{time.strftime('%H:%M:%S')}] Node {node.name}: {outcome.status} "
                  f"(code {code}), log {log_path}")
        return outcome

    def _execute(self) -> tuple[list[NodeOutcome], bool]:
        """Tests the nodes one after another; returns the results and an interruption flag."""
        outcomes: list[NodeOutcome] = []
        try:
            for index, node in enumerate(self.nodes, 1):
                outcome = self._test_node(index, node)
                outcomes.append(outcome)
                if outcome.code == EXIT_INTERRUPTED:
                    return outcomes, True
        except KeyboardInterrupt:
            log.warning("Series interrupted between nodes")
            return outcomes, True
        return outcomes, False

    def run(self) -> int:
        def handler(signum, frame):                    # noqa: ARG001
            raise KeyboardInterrupt
        for sig in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, handler)
        self._header()
        outcomes, interrupted = self._execute()
        tested = {o.name for o in outcomes}
        if self.abort_reason:
            self._say(f"🛑 Series stopped: {self.abort_reason}.")
        elif interrupted:
            self._say("🛑 Series interrupted, the remaining nodes are not tested.")
        why = (f"not tested (stopped: {self.abort_reason})" if self.abort_reason
               else "not tested (series interrupted)")
        for node in self.nodes:
            if node.name not in tested:
                outcomes.append(NodeOutcome(node.name, node.is_control_plane, reason=why))
        for name, reason in self.skipped:
            outcomes.append(NodeOutcome(name, reason=reason))
        self._say("")
        for line in format_cluster_summary(outcomes):
            self._say(line)
        self._say(f"📁 Series log saved to: {self.log_path}")
        code = EXIT_ERROR if self.abort_reason else aggregate_code(outcomes)
        log.info("Series done, exit code %s", code)
        return code

def new_series_log_path(log_dir: Path) -> str:
    make_private_dir(log_dir)
    return str(log_dir / f"cluster-{time.strftime('%Y-%m-%d_%H-%M-%S')}.log")
