"""Command line: arguments, interactive questions and building the test."""
from __future__ import annotations

import argparse
import re
import logging
import os
import shutil
import sys
import time
from typing import Callable, Optional

import secrets
from pathlib import Path

from . import background, baseline, compare, debuglog, parallel, series
from .kube import Kubectl, KubectlError
from .disk import (DISK_JOB_TIME_DEFAULT, DISK_SIZE_DEFAULT, MAX_DISK_JOB_TIME, MAX_DISK_SIZE,
                   MIN_DISK_JOB_TIME, MIN_DISK_SIZE, jobs as disk_jobs)
from . import menu, netmatrix, schedule, ui
from .gpu import GPU_MAX_TEMP_DEFAULT, GPU_MEM_PCT_DEFAULT, MAX_GPU_TIME, MIN_GPU_TIME
from .net import (MAX_NET_TIME, MIN_NET_TIME, NET_MODES, NET_TIME_DEFAULT, net_duration,
                  normalize_extras)
from .export import EXPORT_CHOICES, write_exports
from .logparse import read_log
from .models import (COOLDOWN_DEFAULT, COOLDOWN_MAX, DEFAULT_MAX_TEMP, MASTER_CPU_CAP,
                     MAX_MAX_TEMP, MAX_NODE_BUSY_PCT, MAX_SPIKE_PHASE_TIME, MIN_MAX_TEMP,
                     MIN_SPIKE_PHASE_TIME, MIN_STEP_TIME, PROFILE_CLASSIC, PROFILE_DISK, PROFILE_GPU, PROFILE_NET, PROFILE_SPIKE,
                     PROFILE_STEPPED, SPIKE_LOW_PCT, SPIKE_PHASE_TIME_DEFAULT, SPIKE_TARGETS,
                     STEP_TIME_DEFAULT, STEPS_DEFAULT, StressConfig)
from .parsing import (clean_text, describe_duration, describe_steps, format_workload, is_node_name,
                      parse_duration, parse_steps)
from .paths import make_private_dir, migrate_to_daily, open_private, user_log_dir, user_log_root
from .runner import EXIT_ERROR, EXIT_INTERRUPTED, GATE_TIMEOUT, RunnerError, StressRunner

YES = {"y", "yes"}
log = logging.getLogger(__name__)


def duration_arg(text: str) -> int:
    try:
        return parse_duration(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc))


def node_arg(text: str) -> str:
    """A node name as a Kubernetes node name (never something kubectl or a shell could take for more)."""
    if not is_node_name(text):
        raise argparse.ArgumentTypeError(f"'{text[:60]}' is not a valid node name (lowercase letters, digits, - and .)")
    return text


def node_list_arg(text: str) -> str:
    """--nodes a,b,c : every name validated, the comma separated text is kept."""
    for name in (n.strip(" ") for n in text.split(",")):
        if name:
            node_arg(name)
    return text


def notes_arg(text: str) -> str:
    return clean_text(text)


def interval_arg(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{text}' is not a number")
    if not 0.5 <= value <= 300:
        raise argparse.ArgumentTypeError("the interval must be 0.5-300 seconds")
    return value


def steps_arg(text: str) -> tuple[int, ...]:
    try:
        return parse_steps(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc))


def cooldown_arg(text: str) -> int:
    """Cooldown duration: 0 (off), seconds or 30s, 1m."""
    if text.strip().lower() in ("0", "0s", "0m"):
        return 0
    try:
        return parse_duration(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc))


def schedule_arg(text: str) -> float:
    """When to start the test: 'HH:MM' (the nearest occurrence, today/tomorrow), a duration from now (30m, 2h) or a date
    and time ('2026-10-03 02:00', '3.10. 02:00')."""
    try:
        return schedule.parse_start(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc))


QUICK_DURATION = 600       # s, duration of the quick test (--quick)

HELP_EPILOG = """\
examples:
  ./stress.sh --quick                          quick test: pick a node and it runs right away
  ./stress.sh --quick --workers                quick test of all workers one after another
  ./stress.sh                                  interactively: asks what and how to test
  ./stress.sh --node worker-1 --time 10m
                                               one node, 10 minutes, full CPU load
  ./stress.sh --workers --parallel --time 20m  all workers at once
  ./stress.sh --cluster --profile stepped      the whole cluster one after another, gradually 25-100 %
  ./stress.sh --node worker-3 --time 1h -b
                                               in the background (survives closing the terminal)
  ./stress.sh --status                         what is running right now
  ./stress.sh --stop worker-3             stop a test running in the background
  ./stress.sh --compare worker-1       compare the two latest tests of a node
  ./stress.sh --list-nodes                     list the cluster's nodes and exit
  ./stress.sh --list-gpus                      scan the nodes for GPUs: which have a usable NVIDIA card
  ./stress.sh --profile gpu --node worker-1 --time 2m
                                               GPU burn (gpu-burn) of one node; menu "2 GPU" scans and asks
  ./stress.sh --profile gpu --nodes a,b --gpu-prepull
                                               GPU test of several nodes one after another, image pulled first
  ./stress.sh --self-test --nodes worker-1
                                               FULL self-test limited to the given node(s)
  ./stress.sh --set-baseline worker-1  newest test of the node becomes its baseline
  ./stress.sh --export-log worker-1    JSON+CSV export of the node's newest log
  ./stress.sh --node worker-1 --time 10m --dry-run
                                               show the test settings, start nothing
  ./stress.sh --node worker-1 --time 20m --schedule 22:30 -b
                                               start today at 22:30, in the background
  ./stress.sh --node worker-1 --time 20m --schedule "2026-10-03 02:00" --persistent
                                               on a date, as a systemd timer (survives a restart)
  ./stress.sh --scheduled                      list the planned tests (cancel: --stop ID)
  ./stress.sh --dashboard                      live overview of every node (menu key D; 3 widths, read-only)
  ./stress.sh --node worker-1 --time 10m --allow-busy-node
                                               start even on a node the preflight finds busy

preflight (checks before EVERY test, including --quick):
  1) another test of this tool on the same node -> always refused (cannot be turned off)
  2) node usage (kubectl top, CPU/RAM) above --max-busy-pct -> --allow-busy-node / --no-capacity-check
  3) CPU temperature above --max-temp already BEFORE the load -> let the node cool down, or
     --allow-no-sensor only handles a missing sensor, not a high temperature

language (root ~/cluster-testing/stress.sh only):
  --cz / --eng as the FIRST switch, or the variable STRESS_LANG=CZ|ENG (default CZ)

environment variables:
  FORCE=1        same as --force
  KUBECTL=path   a kubectl other than the one in PATH

durations are given as seconds or 30s, 5m, 1h, 1h30m.
"""


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """--help that follows the terminal width (also STRESS_TEST_COLUMNS): the option list is wrapped by argparse, the
    hand-written description / epilog lines are wrapped at word boundaries; in a narrow window the help texts go under
    the option names. Without a terminal it is the classic help."""

    def __init__(self, prog, indent_increment=2, max_help_position=24, width=None):
        cols = ui.term_cols()
        if cols is not None:
            width = max(ui.MIN_COLS, min(cols, ui.WIDE_MAX)) - 2
            max_help_position = 24 if width >= 70 else 8
        super().__init__(prog, indent_increment, max_help_position, width)

    def add_argument(self, action):
        super().add_argument(action)
        if ui.term_cols() is not None:                    # a long option name must not push the help texts to the right
            self._action_max_length = min(self._action_max_length, self._max_help_position - 2)

    def _format_action_invocation(self, action):
        text = super()._format_action_invocation(action)
        cols = ui.term_cols()
        if cols is not None and ui.visible_len(re.sub(r"\x1b\[[0-9;]*m", "", text)) + 4 > cols - 1:
            text = re.sub(r"\{[^}]*\}", "{…}", text)       # a list of choices too long for the line
        return text

    def _fill_text(self, text, width, indent):
        if ui.term_cols() is None:
            return super()._fill_text(text, width, indent)
        out = []
        for line in text.splitlines():
            out += ui.adapt_line(indent + line, max(20, width), ui.mode(width + 2)) if line.strip() else [""]
        return "\n".join(out)


def _usage() -> str:
    cols = ui.term_cols()
    if cols is None:
        return "%(prog)s [options]      (no options = interactive)"
    return "%(prog)s [options]" + (" (no options = interactive)" if cols >= 64 else "")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="./stress.sh",
        usage=_usage(),
        description="Stress test of a node in a k3s/Kubernetes cluster (CPU, RAM, disk, network, GPU)\n"
                    "with temperature measurement and automatic stop on overheating.",
        epilog=HELP_EPILOG,
        formatter_class=_HelpFormatter,
        add_help=False)

    g = p.add_argument_group("quick test")
    g.add_argument("-q", "--quick", action="store_true",
                   help=f"asks only for the node and starts right away: {describe_duration(QUICK_DURATION)}, "
                        f"CPU 100 %%, no RAM or disk, limit {DEFAULT_MAX_TEMP} °C, cooldown "
                        f"{describe_duration(COOLDOWN_DEFAULT)}, logged. Can be combined with "
                        f"--node / --workers / --cluster / --nodes; other options override the values. "
                        f"A preflight check always runs before the start (even with --quick): another "
                        f"test on the node, node usage (--max-busy-pct, default {MAX_NODE_BUSY_PCT} %%) "
                        f"and CPU temperature (--max-temp) - see the 'safety' group")

    g = p.add_argument_group("what to test (asks if not given)")
    g.add_argument("--node", metavar="NODE", type=node_arg, help="one node")
    g.add_argument("--workers", action="store_true",
                   help="all workers one after another (without the master)")
    g.add_argument("--cluster", action="store_true",
                   help="the whole cluster one after another, master always last (CPU cap "
                        f"{MASTER_CPU_CAP} %%); always logged")
    g.add_argument("--nodes", metavar="A,B,C", type=node_list_arg,
                   help="selected nodes one after another (a master name in the list includes it)")
    g.add_argument("--include-master", action="store_true",
                   help="also include the master with --workers; in the stepped test without a question")
    g.add_argument("--parallel", action=argparse.BooleanOptionalAction, default=None,
                   help="test workers AT ONCE (each in a subprocess); the master after them, alone. "
                        "Without the flag it asks interactively, otherwise one after another")

    g = p.add_argument_group("what kind of test")
    g.add_argument("--time", type=duration_arg, dest="duration", metavar="DURATION",
                   help="test duration: seconds or 30s, 5m, 1h, 1h30m")
    g.add_argument("--profile", choices=[PROFILE_CLASSIC, PROFILE_STEPPED, PROFILE_SPIKE, PROFILE_DISK, PROFILE_NET, PROFILE_GPU],
                   default=None,
                   help="test type: classic = one load for the whole time (default), "
                        "stepped = gradually 25/50/75/100 %% (always logged), "
                        "spike = repeating on/off jump between a low and a target %% "
                        "(always logged), disk = fio disk benchmark: MB/s, IOPS and latency "
                        "(always logged, installs fio in the pod), net = network test: iperf3 "
                        "throughput, ping latency, MTU, link speed (always logged, installs iperf3), gpu = NVIDIA "
                        "GPU burn with gpu-burn: temperature, clock, VRAM, throttling (always logged, one GPU node "
                        "or several one after another with --nodes; see --list-gpus)")
    g.add_argument("--self-test", action="store_true",
                   help="FULL SELF-TEST of the whole cluster: network matrix, disks, CPU (stepped), GPU (nodes with "
                        "an NVIDIA card) and RAM one after another with cooling pauses, then one big report. "
                        "Loads everything at full power - asks for two confirmations. --nodes A,B limits it to "
                        "the given nodes")
    g.add_argument("--self-test-level", choices=["quick", "standard", "thorough"], default=None,
                   help="how long the self-test phases are (default: asks, non-interactive = standard)")
    g.add_argument("--self-test-ack", action="store_true",
                   help="second confirmation of --self-test given on the command line (for scripts)")
    g.add_argument("--net-matrix", action="store_true",
                   help="network matrix: ping + iperf3 between EVERY pair of nodes (all Ready nodes, "
                        "or --nodes a,b,c), shown as tables; finds the weak node or cable. "
                        "--net-time sets the length of one iperf3 test; the master is capped at 300 Mbit/s")
    g.add_argument("--net-watch", nargs="?", const="auto", default=None, metavar="NODE", type=node_arg,
                   help="during ANY test also ping another node (default: the master, or a worker when "
                        "the master is tested) once per reading: shows whether the load disturbs the "
                        "network (latency and lost pings in the log and summary)")
    g.add_argument("--net-peer", default=None, metavar="NODE", type=node_arg,
                   help="network test: the node on the other end (default: another Ready worker, "
                        "the master only when nothing else exists)")
    g.add_argument("--net-time", type=duration_arg, default=None, metavar="DURATION",
                   help=f"network test: length of one iperf3 test ({MIN_NET_TIME}-{MAX_NET_TIME} s, "
                        f"default {NET_TIME_DEFAULT} s); 4 tests: TCP up, TCP down, 4 streams, UDP")
    g.add_argument("--net-extra", default=None, metavar="LIST",
                   help="network test: extra jobs, comma separated: dns (CoreDNS + external lookups), "
                        "internet (ping + download speed), mtr (path to the peer), service "
                        "(TCP through a Kubernetes Service, needs --net-mode pod), no-udp (leave the UDP test out), or all "
                        "(dns, internet, mtr, service)")
    g.add_argument("--net-mode", choices=NET_MODES, default=None,
                   help="network test: host = the nodes' real network (default), "
                        "pod = the pod network (what workloads really use, flannel/CNI)")
    g.add_argument("--net-rate", type=int, default=None, metavar="MBIT",
                   help="network test: cap the TCP tests at this many Mbit/s (default unlimited; "
                        "300 when the master takes part)")
    g.add_argument("--disk-size", type=int, default=None, metavar="MiB",
                   help=f"disk test: size of the test file ({MIN_DISK_SIZE}-{MAX_DISK_SIZE} MiB, "
                        f"default {DISK_SIZE_DEFAULT}); it lives in an emptyDir on the node's disk")
    g.add_argument("--disk-job-time", type=duration_arg, default=None, metavar="DURATION",
                   help=f"disk test: length of one fio job ({MIN_DISK_JOB_TIME}-{MAX_DISK_JOB_TIME} s, "
                        f"default {DISK_JOB_TIME_DEFAULT} s); 4 jobs: seq read/write, random read/write")
    g.add_argument("--gpu-max-temp", type=int, default=None, metavar="C",
                   help=f"GPU test: GPU temperature that stops the test (default {GPU_MAX_TEMP_DEFAULT} °C; "
                        f"a warning appears 5 °C below it); asked in the interactive GPU dialog")
    g.add_argument("--gpu-mem-pct", type=int, default=None, metavar="%",
                   help=f"GPU test: share of the GPU memory gpu-burn uses (default {GPU_MEM_PCT_DEFAULT})")
    g.add_argument("--gpu-double", action="store_true",
                   help="GPU test: double precision (much slower on consumer cards)")
    g.add_argument("--gpu-prepull", action="store_true",
                   help="GPU test: pull the CUDA image on the chosen nodes first (all at once), so that the tests "
                        "do not wait for the 3 GB download one after another")
    g.add_argument("--gpu-image", default=None, metavar="IMAGE",
                   help="GPU test: image of the load pod (default a CUDA 12.x devel image; an image that "
                        "already contains gpu_burn skips the build)")
    g.add_argument("--steps", type=steps_arg, default=None, metavar="LIST",
                   help=f"stages of the stepped test in %%, e.g. 25,50,75,100 "
                        f"(default {describe_steps(STEPS_DEFAULT)})")
    g.add_argument("--step-time", type=duration_arg, default=None, metavar="DURATION",
                   help=f"length of one stage of the stepped test (default "
                        f"{describe_duration(STEP_TIME_DEFAULT)}, min {MIN_STEP_TIME} s); "
                        f"total time = stages x stage time")
    g.add_argument("--spike-target", type=int, choices=SPIKE_TARGETS, default=None, metavar="%",
                   help=f"spike test: high-phase target CPU load - 1/4, 2/4, 3/4 or 4/4 "
                        f"({'/'.join(str(s) for s in SPIKE_TARGETS)} %%; default "
                        f"{SPIKE_TARGETS[-1]}); the low phase is always {SPIKE_LOW_PCT} %%")
    g.add_argument("--spike-low-time", type=duration_arg, default=None, metavar="DURATION",
                   help=f"spike test: duration of the low phase ({SPIKE_LOW_PCT} %%) in each "
                        f"cycle (default {describe_duration(SPIKE_PHASE_TIME_DEFAULT)}, "
                        f"{MIN_SPIKE_PHASE_TIME}-{MAX_SPIKE_PHASE_TIME} s)")
    g.add_argument("--spike-high-time", type=duration_arg, default=None, metavar="DURATION",
                   help=f"spike test: duration of the high phase (--spike-target) in each "
                        f"cycle (default {describe_duration(SPIKE_PHASE_TIME_DEFAULT)}, "
                        f"{MIN_SPIKE_PHASE_TIME}-{MAX_SPIKE_PHASE_TIME} s); --time sets the "
                        f"total test time, rounded down to a whole number of cycles")
    g.add_argument("--cpu-load", type=int, metavar="%", help="CPU load in %% (default 100)")
    g.add_argument("--ram-pct", type=int, metavar="%",
                   help="enables the RAM test: how many %% of FREE memory to allocate")
    g.add_argument("--hdd", action=argparse.BooleanOptionalAction, default=None,
                   help="stress the disk")
    g.add_argument("--cooldown", type=cooldown_arg, default=None, metavar="DURATION",
                   help=f"after the load ends keep measuring CPU temperature and clock (cooldown): "
                        f"seconds or 30s, 1m; 0 = off (default {COOLDOWN_DEFAULT} s, "
                        f"max {COOLDOWN_MAX} s)")

    g = p.add_argument_group("safety")
    g.add_argument("--max-temp", type=int, metavar="°C",
                   help=f"CPU temperature to stop at ({MIN_MAX_TEMP}–{MAX_MAX_TEMP} °C, "
                        f"default {DEFAULT_MAX_TEMP}); also checked before the test "
                        f"(preflight) - if the node is already at this temperature, "
                        f"the test is refused until it cools down")
    g.add_argument("--api-limit", type=float, default=3.0, metavar="SEC",
                   help="when running in parallel: API response (kubectl) slower than this many seconds, or "
                        "a failure, twice in a row stops all tests (default 3)")
    g.add_argument("--start-mode", choices=["rolling", "sync"], default="rolling",
                   help="when running in parallel: rolling (default) = every node starts its load as soon as it is "
                        "ready and runs the full time from its own start; sync = all nodes wait until every one "
                        "is ready and start together")
    g.add_argument("--no-sync-start", action="store_true", help=argparse.SUPPRESS)   # old name of --start-mode rolling
    g.add_argument("--ready-timeout", type=int, default=GATE_TIMEOUT, metavar="SEC",
                   help=f"with --start-mode sync: how long to wait until all nodes are ready "
                        f"(default {GATE_TIMEOUT}); a node that is not ready in time is left out")
    g.add_argument("--force", action="store_true",
                   help="bypass the master protection (or the environment variable FORCE=1)")
    g.add_argument("--allow-no-sensor", action="store_true",
                   help="continue even without a CPU temperature sensor")
    g.add_argument("--max-busy-pct", type=int, default=MAX_NODE_BUSY_PCT, metavar="%",
                   help="preflight check: if the node is already using this many %% of CPU or "
                        f"RAM according to 'kubectl top', the test is refused (default "
                        f"{MAX_NODE_BUSY_PCT})")
    g.add_argument("--allow-busy-node", action="store_true",
                   help="continue even on a node that the check above finds already busy")
    g.add_argument("--no-capacity-check", action="store_true",
                   help="skip the node usage preflight check entirely")

    g = p.add_argument_group("background run and management")
    g.add_argument("-b", "--background", action=argparse.BooleanOptionalAction,
                   default=None,
                   help="run the test in the background (survives closing the terminal; logging "
                        "is turned on automatically). Default: no")
    g.add_argument("--status", action="store_true",
                   help="show tests running in the background and the tool's pods in the cluster")
    g.add_argument("--stop", metavar="ID|NODE|PID",
                   help="stop a test running in the background (gracefully, cleaning up pods)")

    g = p.add_argument_group("logs and results")
    g.add_argument("--log", action=argparse.BooleanOptionalAction, default=None,
                   help="save metrics to a file")
    g.add_argument("--compare", nargs="+", metavar="LOG|NODE",
                   help="compare two tests from logs: either two logs (older and newer; path or "
                        "name in the logs folder), or just a node name (compares its two "
                        "newest tests)")
    g.add_argument("--export", choices=EXPORT_CHOICES, default=None, metavar="FORMAT",
                   help="machine-readable export next to the log: json (default), csv "
                        "(measurements only), both or none")
    g.add_argument("--export-log", nargs="+", metavar="LOG|NODE",
                   help="create the JSON/CSV export of already finished tests from their logs "
                        "(a log, or a node name = its newest log) and exit; format from --export")
    g.add_argument("--set-baseline", metavar="LOG|NODE",
                   help="save a finished test as the baseline of its node (a log, or a node name "
                        "= its newest log); later tests of that node are compared with it")
    g.add_argument("--no-baseline-check", action="store_true",
                   help="do not compare the finished test with the node's baseline")
    g.add_argument("--migrate-logs", action="store_true",
                   help="one-off: move old flat result / debug / pytest logs into day folders (YYYY-MM-DD) by their date")
    g.add_argument("--log-dir", default="logs", metavar="DIR",
                   help="folder with test results (default 'logs' in the project directory, wherever you run it "
                        "from; a relative path is taken from the project, an absolute one unchanged). Results of a "
                        "run go into a day folder inside it: <DIR>/YYYY-MM-DD/ (created when it does not exist)")
    g.add_argument("--notes", metavar="TEXT", type=notes_arg, help="a note about the test (goes into the log)")

    g = p.add_argument_group("scheduling and information")
    g.add_argument("--schedule", type=schedule_arg, metavar="TIME",
                   help="start the test later: HH:MM (the nearest occurrence, today/tomorrow), a duration from now "
                        "(30m, 2h) or a date and time (2026-10-03 02:00, 3.10. 02:00); with -b/--background the "
                        "waiting process keeps running after closing the terminal; --persistent keeps the plan as "
                        "a systemd timer that survives a restart; planned tests: --scheduled, cancel: --stop ID")
    g.add_argument("--persistent", action="store_true",
                   help="with --schedule: keep the plan as a systemd USER timer (survives a restart of this computer; "
                        "while you are logged out it needs `sudo loginctl enable-linger $USER`)")
    g.add_argument("--scheduled", action="store_true",
                   help="list the planned tests (waiting processes and systemd timers) and exit")
    g.add_argument("--cancel-plan", metavar="ID", default=None, help=argparse.SUPPRESS)
    g.add_argument("--dry-run", action="store_true",
                   help="just show what would be started (node, test settings) and exit "
                        "without touching the cluster")
    g.add_argument("--list-nodes", action="store_true",
                   help="list the cluster's nodes (role, state) and exit")
    g.add_argument("--live", action="store_true",
                   help="with --status: the live screen (menu key W) - every running and planned test, refreshed every "
                        "second, with the details of its kind (GPU, CPU, disk, network, FULL self-test); x stops a test")
    g.add_argument("--dashboard", action="store_true",
                   help="live cluster DASHBOARD (menu key D): every node at a glance - CPU, temperature, clock, RAM, power, GPU, "
                        "network, pods, events; three widths (up to 90 / 91-160 / wider); read-only probes that exist only while "
                        "it is open; --nodes A,B limits it to some nodes")
    g.add_argument("--list-gpus", action="store_true",
                   help="scan every node for graphics cards (short read-only pods) and show which have a usable "
                        "dedicated NVIDIA GPU, then exit")

    g = p.add_argument_group("other")
    g.add_argument("-h", "--help", action="help", help="show this help and exit")
    g.add_argument("-y", "--yes", action="store_true",
                   help="confirm questions automatically (e.g. for the master)")
    g.add_argument("--non-interactive", action="store_true",
                   help="do not ask anything, take missing values from the defaults")
    g.add_argument("--no-hw", action="store_true",
                   help="do not detect node hardware (the hw-info pod is not started)")
    g.add_argument("--hw-privileged", action="store_true",
                   help="the hw-info pod runs privileged (needed to list RAM modules "
                        "via dmidecode; default is without privileged)")
    g.add_argument("--smart", action="store_true",
                   help="preflight: read the disk health (SMART) of the node with a PRIVILEGED pod; "
                        "a disk with a failed health check refuses the test")
    g.add_argument("--allow-bad-disk", action="store_true",
                   help="with --smart: test even if a disk reports a failed SMART health check")
    g.add_argument("--interval", type=interval_arg, default=5.0, metavar="SEC",
                   help="measurement interval in seconds, 0.5-300 (default 5)")
    g.add_argument("--remaining-every", type=int, default=3, metavar="N",
                   help="every Nth measurement print how much is left "
                        "(default 3, 0 = do not print)")

    p.add_argument("--log-file", default=None, help=argparse.SUPPRESS)      # internal (subprocess)
    p.add_argument("--concurrent", type=int, default=1, help=argparse.SUPPRESS)  # internal
    p.add_argument("--log-flat", action="store_true", help=argparse.SUPPRESS)    # internal: --log-dir exactly, no day folder
    p.add_argument("--start-gate", default="", help=argparse.SUPPRESS)           # internal (subprocess)
    p.add_argument("--no-gate-wait", action="store_true", help=argparse.SUPPRESS)  # internal: rolling start
    return p


# --- questions ------------------------------------------------------------------
def ask(prompt: str, default: Optional[str] = None) -> str:
    try:
        answer = input(ui.prompt(prompt, default)).strip()
    except EOFError:
        raise SystemExit("\nInput ended.")
    return answer or (default or "")


def ask_int(prompt: str, default: int) -> int:
    while True:
        raw = ask(prompt, str(default))
        try:
            return int(raw)
        except ValueError:
            ui.warn("Enter a whole number.")


def ask_duration(prompt: str, default: str) -> int:
    while True:
        raw = ask(prompt, default)
        try:
            seconds = parse_duration(raw)
        except ValueError as exc:
            ui.warn(str(exc))
            continue
        ui.info(describe_duration(seconds))
        return seconds


def ask_yes_no(prompt: str, default: bool = False) -> bool:
    raw = ask(f"{prompt} (y/n)", "y" if default else "n")
    return raw.lower() in YES


def ask_scope() -> str:
    """What to test: one node, all workers, or the whole cluster."""
    ui.show_choices("WHAT TO TEST", [
        ("1", "one node", ""),
        ("2", "all workers", "without the master"),
        ("3", "whole cluster", f"workers, the master last with a CPU cap of {MASTER_CPU_CAP} %")])
    while True:
        raw = ask("Choose", "1")
        if raw in ("1", "2", "3"):
            return {"1": "single", "2": "workers", "3": "cluster"}[raw]
        ui.warn("Invalid choice.")


def ask_profile() -> str:
    """Test type: classic, stepped, spike or disk."""
    ui.show_choices("TEST TYPE", [
        ("1", "classic", "one load for the given time"),
        ("2", "stepped", f"{describe_steps(STEPS_DEFAULT)} % in stages, always logged"),
        ("3", "spike", f"repeating jump {SPIKE_LOW_PCT} % <-> target %, always logged"),
        ("4", "disk", "fio benchmark: MB/s, IOPS, latency; always logged"),
        ("5", "network", "iperf3 + ping to another node: MB/s, latency, MTU; always logged"),
        ("6", "gpu", "NVIDIA GPU burn (gpu-burn): temperature, clocks, throttling; always logged")])
    while True:
        raw = ask("Choose", "1")
        if raw in ("1", "2", "3", "4", "5", "6"):
            return {"1": PROFILE_CLASSIC, "2": PROFILE_STEPPED, "3": PROFILE_SPIKE,
                    "4": PROFILE_DISK, "5": PROFILE_NET, "6": PROFILE_GPU}[raw]
        ui.warn("Invalid choice.")


SPIKE_TARGET_CHOICES = {"1": 25, "2": 50, "3": 75, "4": 100}


def ask_spike_target() -> int:
    """Spike test: target CPU load after the jump (1/4, 2/4, 3/4, 4/4)."""
    ui.show_choices("SPIKE TARGET", [
        (key, f"{key}/4", f"{pct} %" + (" (default)" if pct == SPIKE_TARGETS[-1] else ""))
        for key, pct in SPIKE_TARGET_CHOICES.items()])
    while True:
        raw = ask("Choose", "4")
        if raw in SPIKE_TARGET_CHOICES:
            return SPIKE_TARGET_CHOICES[raw]
        ui.warn("Invalid choice.")


COOLDOWN_CHOICES = {"1": 60, "2": 180, "3": 300, "4": COOLDOWN_MAX}


def ask_net_mode() -> str:
    """Network test: the nodes' real network, or the pod network."""
    ui.show_choices("NETWORK TO TEST", [
        ("1", "nodes' network", "the real NICs and cables (default)"),
        ("2", "pod network", "what workloads really use: flannel / CNI")])
    while True:
        raw = ask("Choose", "1")
        if raw in ("1", "2"):
            return "host" if raw == "1" else "pod"
        ui.warn("Invalid choice.")


def ask_net_extra() -> str:
    """Network test: optional extra jobs."""
    ui.show_choices("EXTRA NETWORK JOBS (comma separated, Enter = none)", [
        ("·", "dns", "CoreDNS + external name lookups"),
        ("·", "internet", "ping 1.1.1.1 + a bounded download"),
        ("·", "mtr", "path, loss and latency to the peer"),
        ("·", "service", "TCP through a Kubernetes Service (pod network only)"),
        ("·", "no-udp", "leave the UDP test out (when the firewall lets in only TCP)"),
        ("·", "all", "dns + internet + mtr + service")])
    while True:
        raw = ask("Extras", "").replace(" ", "")
        try:
            normalize_extras(x for x in raw.split(",") if x)
        except ValueError as exc:
            ui.warn(str(exc))
            continue
        return raw


def ask_cooldown() -> int:
    """Cooldown after the test: preset, custom duration, or off (0)."""
    ui.show_choices("COOLDOWN (measuring CPU temperature and clock)", [
        *((key, describe_duration(seconds),
           " (default)" if seconds == COOLDOWN_DEFAULT else " (max)" if seconds == COOLDOWN_MAX else "")
          for key, seconds in COOLDOWN_CHOICES.items()),
        ("5", "custom duration", "")], back="off")
    default = next((k for k, v in COOLDOWN_CHOICES.items() if v == COOLDOWN_DEFAULT), "1")
    while True:
        raw = ask("Choose", default)
        if raw == "0":
            return 0
        if raw in COOLDOWN_CHOICES:
            return COOLDOWN_CHOICES[raw]
        if raw == "5":
            break
        ui.warn("Invalid choice.")
    while True:
        seconds = ask_duration(f"Cooldown duration (e.g. 90, 2m; max "
                               f"{describe_duration(COOLDOWN_MAX)})", "2m")
        if seconds <= COOLDOWN_MAX:
            return seconds
        ui.warn(f"The maximum is {describe_duration(COOLDOWN_MAX)}.")


def choose_node(kube: Kubectl) -> str:
    names = kube.list_node_names()
    if not names:
        raise SystemExit("❌ No nodes were found in the cluster.")
    if ui.adaptive():
        for line in ui.grid([f"[{i}] {name}" for i, name in enumerate(names, 1)]):
            ui.emit(line)
    else:
        for i, name in enumerate(names, 1):
            ui.emit(f"[{i}] {name}")
    while True:
        raw = ask("Pick a node number or type the name")
        if raw.isdigit() and 1 <= int(raw) <= len(names):
            return names[int(raw) - 1]
        if raw in names:
            return raw
        ui.warn("Invalid choice.")


# --- building the configuration -----------------------------------------------------
def build_config(args: argparse.Namespace, node_name: str,
                 master_mode: bool, series: bool = False) -> StressConfig:
    interactive = not (args.non_interactive or args.quick)

    def pick(value, question: Callable[[], object], default):
        if value is not None:
            return value
        return question() if interactive else default

    profile = args.profile
    if profile is None:
        profile = ask_profile() if interactive else PROFILE_CLASSIC
    stepped = profile == PROFILE_STEPPED
    spike = profile == PROFILE_SPIKE
    disk = profile == PROFILE_DISK
    net = profile == PROFILE_NET
    gpu = profile == PROFILE_GPU
    if not gpu and (args.gpu_max_temp is not None or args.gpu_mem_pct is not None or args.gpu_double
                    or args.gpu_image or args.gpu_prepull):
        ui.emit("ℹ️  The --gpu-* options only apply to --profile gpu, they are ignored here.")
    gpu_max_temp = GPU_MAX_TEMP_DEFAULT if args.gpu_max_temp is None else args.gpu_max_temp
    net_time = NET_TIME_DEFAULT
    net_extra: tuple = ()
    disk_size = DISK_SIZE_DEFAULT
    disk_job_time = DISK_JOB_TIME_DEFAULT
    steps = args.steps or STEPS_DEFAULT
    step_time = STEP_TIME_DEFAULT
    spike_target = None
    spike_low_time = SPIKE_PHASE_TIME_DEFAULT
    spike_high_time = SPIKE_PHASE_TIME_DEFAULT
    spike_cycles = 1
    if stepped:
        step_time = pick(args.step_time,
                         lambda: ask_duration("Duration of one stage (e.g. 90, 3m)", "3m"),
                         STEP_TIME_DEFAULT)
        duration = len(steps) * step_time
        if args.duration is not None:
            ui.emit("ℹ️  For the stepped test the time is set with --step-time, --time is ignored.")
    elif net:
        net_mode = pick(args.net_mode, ask_net_mode, "host")
        args.net_mode = net_mode
        net_extra_text = pick(args.net_extra, ask_net_extra, "")
        args.net_extra = net_extra_text
        net_time = pick(args.net_time,
                        lambda: ask_duration(f"Length of one iperf3 test ({MIN_NET_TIME}-{MAX_NET_TIME} s, "
                                             f"e.g. 10, 30s)", str(NET_TIME_DEFAULT)), NET_TIME_DEFAULT)
        net_extra = normalize_extras(x for x in (args.net_extra or "").replace(" ", "").split(",") if x)
        duration = net_duration(net_time, net_extra, args.net_mode or "host")
        if args.duration is not None:
            ui.emit("ℹ️  For the network test the time is set with --net-time, --time is ignored.")
    elif gpu:
        gpu_max_temp = pick(args.gpu_max_temp, lambda: ask_int("GPU temperature for automatic stop (50-95 °C)", GPU_MAX_TEMP_DEFAULT),
                            GPU_MAX_TEMP_DEFAULT)
        duration = pick(args.duration,
                        lambda: ask_duration(f"GPU test duration ({MIN_GPU_TIME}-{MAX_GPU_TIME} s, e.g. 60, 5m)", "2m"), 120)
    elif disk:
        disk_size = pick(args.disk_size,
                         lambda: ask_int(f"Test file size in MiB ({MIN_DISK_SIZE}-{MAX_DISK_SIZE})",
                                         DISK_SIZE_DEFAULT), DISK_SIZE_DEFAULT)
        disk_job_time = pick(args.disk_job_time,
                             lambda: ask_duration(f"Length of one fio job ({MIN_DISK_JOB_TIME}-"
                                                  f"{MAX_DISK_JOB_TIME} s, e.g. 15, 30s)",
                                                  str(DISK_JOB_TIME_DEFAULT)), DISK_JOB_TIME_DEFAULT)
        duration = len(disk_jobs()) * disk_job_time
        if args.duration is not None:
            ui.emit("ℹ️  For the disk test the time is set with --disk-job-time, --time is ignored.")
    elif spike:
        spike_target = pick(args.spike_target, ask_spike_target, SPIKE_TARGETS[-1])
        spike_low_time = pick(
            args.spike_low_time,
            lambda: ask_duration(f"Low-phase duration ({SPIKE_LOW_PCT} %) each cycle "
                                 f"(e.g. 5, 10s)", str(SPIKE_PHASE_TIME_DEFAULT)),
            SPIKE_PHASE_TIME_DEFAULT)
        spike_high_time = pick(
            args.spike_high_time,
            lambda: ask_duration("High-phase duration (target %) each cycle (e.g. 5, 10s)",
                                 str(SPIKE_PHASE_TIME_DEFAULT)),
            SPIKE_PHASE_TIME_DEFAULT)
        wanted = pick(args.duration,
                     lambda: ask_duration(
                         "Total test duration (e.g. 5m, 10m, 1h)", "10m"), 600)
        phase = spike_low_time + spike_high_time
        spike_cycles = max(1, round(wanted / phase))
        duration = spike_cycles * phase
        if duration != wanted:
            ui.emit(f"ℹ️  Rounded to a whole number of cycles: {describe_duration(duration)} "
                 f"({spike_cycles}x {describe_duration(phase)}) instead of "
                 f"{describe_duration(wanted)}.")
    else:
        duration = pick(args.duration,
                        lambda: ask_duration(
                            "Test duration (e.g. 90, 30s, 5m, 1h, 1h30m)", "1m"), 60)
    if net or disk or gpu:        # these tests do not load the CPU: no question, the default CPU guard stays (--max-temp changes it)
        max_temp = DEFAULT_MAX_TEMP if args.max_temp is None else args.max_temp
    else:
        max_temp = pick(args.max_temp,
                        lambda: ask_int(
                            f"CPU temperature for automatic stop "
                            f"({MIN_MAX_TEMP}–{MAX_MAX_TEMP} °C)", DEFAULT_MAX_TEMP),
                        DEFAULT_MAX_TEMP)
    # the network / disk tests do not heat the CPU: no cooldown unless it was asked for explicitly
    cooldown = (args.cooldown or 0) if (net or disk) else pick(args.cooldown, ask_cooldown, COOLDOWN_DEFAULT)
    cpu_load = 100 if (stepped or spike or disk or net or gpu) else pick(args.cpu_load,
                                                     lambda: ask_int("CPU load in %", 100), 100)

    ram_pct = args.ram_pct
    hdd = args.hdd
    if stepped or spike or disk or net or gpu:   # these tests load only the CPU (or only the disk / network)
        if ram_pct is not None or hdd:
            kind = "stepped" if stepped else "spike" if spike else "disk" if disk else "GPU" if gpu else "network"
            only = "CPU" if (stepped or spike) else kind
            ui.emit(f"ℹ️  The {kind} test loads only the {only}, RAM and disk are ignored.")
        ram_pct, hdd = None, False
    elif not master_mode:   # on the master RAM and disk are not used anyway
        if ram_pct is None and interactive:
            if ask_yes_no("Stress RAM?"):
                ram_pct = ask_int("How many % of FREE RAM to allocate", 80)
        if hdd is None and interactive:
            hdd = ask_yes_no("Stress disk / IO?")

    run_bg = args.background
    if run_bg is None:
        run_bg = (ask_yes_no("Run in the background? (survives closing the terminal, "
                             "logging turns on by itself)") if interactive else False)

    why = [w for w, on in (("background test", run_bg), ("stepped test", stepped),
                           ("spike test", spike), ("disk test", disk), ("network test", net), ("GPU test", gpu), ("multi-node test", series)) if on]
    if why:
        reason = ", ".join(why)
        if args.log is False:
            ui.emit(f"ℹ️  --no-log is ignored: {reason} is always logged.")
        else:
            ui.emit(f"ℹ️  Logging turned on automatically ({reason}).")
        log = True
    else:
        log = pick(args.log, lambda: ask_yes_no("Save metrics to a file?"), False)
    notes = args.notes
    if notes is None:
        notes = clean_text(ask("Note about the test", "")) if (interactive and log) else ""

    return StressConfig(node=node_name, duration=duration, max_temp=max_temp,
                        cpu_load=cpu_load, ram_pct=ram_pct, hdd=bool(hdd),
                        log=bool(log), notes=notes, background=bool(run_bg),
                        cooldown=cooldown, export=args.export or "json",
                        baseline_check=not args.no_baseline_check,
                        smart=args.smart, allow_bad_disk=args.allow_bad_disk,
                        profile=profile, steps=tuple(steps),
                        step_time=step_time, spike_target=spike_target,
                        spike_low_time=spike_low_time, spike_high_time=spike_high_time,
                        spike_cycles=spike_cycles, disk_size=disk_size,
                        disk_job_time=disk_job_time, net_time=net_time, net_extra=net_extra,
                        net_watch=args.net_watch or "", net_peer=args.net_peer or "", net_mode=args.net_mode or "host",
                        net_rate=args.net_rate or 0,
                        gpu_max_temp=gpu_max_temp,
                        gpu_mem_pct=GPU_MEM_PCT_DEFAULT if args.gpu_mem_pct is None else args.gpu_mem_pct,
                        gpu_double=args.gpu_double, gpu_image=args.gpu_image or "")


def confirm_workload(kube: Kubectl, node, args: argparse.Namespace,
                     already_confirmed: bool) -> bool:
    """Prints what is running on the node and asks for confirmation for foreign services.

    Pods in kube-system are only counted. already_confirmed = the user already confirmed
    (on the master), the second question is not asked. -y confirms, --non-interactive without -y refuses.
    """
    try:
        work = kube.list_node_workload(node.name)
    except KubectlError as exc:
        ui.emit(f"⚠️  Could not find out what is running on the node ({exc}). Continuing.")
        log.warning("list_node_workload failed: %s", exc)
        return True
    log.info("Node workload: %s", work)
    if not work.user_pods:
        if work.system_count:
            ui.emit(f"ℹ️  Only system pods are running on the node ({work.system_count}× kube-system).")
        return True
    ui.emit(f"⚠️  Node {node.name} runs these services (the test loads them, "
          f"they may slow down or be interrupted):")
    for line in format_workload(work):
        ui.emit(f"    {line}")
    if work.system_count:
        ui.emit(f"    (+ {work.system_count} system pods in kube-system)")
    if already_confirmed or args.yes:
        return True
    if args.non_interactive:
        ui.emit("Test refused: services are running on the node, confirm with --yes.")
        return False
    return ask_yes_no("Continue with the test?")


def _out(line: str) -> None:
    """Prints to the screen and at the same time writes to the hidden debug log."""
    ui.emit(line)
    log.info("OUT %s", line)


def cmd_status() -> int:
    ui.emit(background.format_running(background.list_running()))
    if shutil.which(os.environ.get("KUBECTL", "kubectl")):
        try:
            pods = Kubectl().list_tool_pods()
        except KubectlError as exc:
            ui.emit(f"(could not find out the pods in the cluster: {exc})")
        else:
            ui.emit("\nThe tool's pods in the cluster:" if pods else "\nNo pods of the tool in the cluster.")
            if ui.adaptive() and pods:
                for line in ui.table([("pod", 0, "<"), ("node", 1, "<"), ("state", 0, "<")],
                                     [[pod.name, pod.node or "?", pod.phase] for pod in pods], lead="  "):
                    ui.emit(line)
            elif pods:
                for pod in pods:
                    ui.emit(f"  {pod.name:<28} node {pod.node or '?':<18} {pod.phase}")
    return 0


def _maybe_prepull(args: argparse.Namespace, kube: Kubectl, cfg: StressConfig, names: list) -> None:
    """--gpu-prepull: warms the CUDA image on the nodes of a GPU test before anything starts."""
    if cfg.gpu and args.gpu_prepull and names:
        from . import gpu as gpumod, gpuscan
        gpuscan.prepull(kube, names, cfg.gpu_image or gpumod.GPU_IMAGE_DEFAULT)


def cmd_list_gpus() -> int:
    from . import gpuscan
    try:
        scans = gpuscan.scan_nodes(Kubectl())
    except KubectlError as exc:
        ui.emit(f"❌ {exc}")
        return EXIT_ERROR
    print("\n".join(gpuscan.table_lines(scans, ui.color_enabled())))
    return 0 if gpuscan.candidates(scans) else EXIT_ERROR


def cmd_list_nodes() -> int:
    kube = Kubectl()
    try:
        names = kube.list_node_names()
    except KubectlError as exc:
        ui.emit(f"❌ {exc}")
        return EXIT_ERROR
    if not names:
        ui.emit("No nodes were found in the cluster.")
        return 0
    rows = []
    for name in names:
        try:
            node = kube.get_node(name)
        except KubectlError as exc:
            rows.append((name, "?", f"({exc})"))
            continue
        rows.append((name, "master" if node.is_control_plane else "worker", "Ready" if node.ready else "NotReady"))
    if ui.adaptive():
        for line in ui.table([("node", 0, "<"), ("role", 0, "<"), ("state", 0, "<")], [list(r) for r in rows], lead="  "):
            ui.emit(line)
    else:
        for name, role, state in rows:
            ui.emit(f"  {name:<38} ? {state}" if role == "?" else f"  {name:<38} {role:<7} {state}")
    return 0


def _wait_until(target: Optional[float]) -> None:
    """Waits until the scheduled start (--schedule). No effect if the time has already passed."""
    if target is None:
        return
    remaining = target - time.time()
    if remaining <= 0:
        return
    until = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(target))
    ui.emit(f"⏳ Waiting until {until} ({describe_duration(int(remaining))})…")
    log.info("Waiting for the scheduled start until %s (%.0f s)", until, remaining)
    time.sleep(remaining)


def cmd_compare(args: argparse.Namespace) -> int:
    log_dir = user_log_root(args.log_dir)
    try:
        first, second = compare.resolve_logs(args.compare, log_dir)
        lines = compare.compare_files(first, second, width=ui.avail(130) if ui.adaptive() else None)
    except compare.CompareError as exc:
        ui.emit(f"❌ {exc}")
        log.error("Comparison failed: %s", exc)
        return EXIT_ERROR
    for line in lines:
        ui.emit(line)
    log.info("Comparison of logs %s and %s done", first, second)
    return 0


def cmd_export_log(args: argparse.Namespace) -> int:
    log_dir = user_log_root(args.log_dir)
    formats = args.export or "both"
    if formats == "none":
        ui.emit("❌ --export none does not export anything.")
        return EXIT_ERROR
    code = 0
    for value in args.export_log:
        try:
            path = baseline.resolve_log(value, log_dir)
            written = write_exports(read_log(path), str(path), formats)
        except (compare.CompareError, OSError, ValueError) as exc:
            ui.emit(f"❌ {value}: {exc}")
            code = EXIT_ERROR
            continue
        for item in written:
            ui.emit(f"📄 {item}")
    return code


def cmd_set_baseline(args: argparse.Namespace) -> int:
    log_dir = user_log_root(args.log_dir)
    try:
        path = baseline.resolve_log(args.set_baseline, log_dir)
        run = read_log(path)
        saved = baseline.save_baseline(run, log_dir)
    except (compare.CompareError, OSError, ValueError) as exc:
        ui.emit(f"❌ {exc}")
        log.error("Setting the baseline failed: %s", exc)
        return EXIT_ERROR
    ui.emit(f"✅ Baseline of {run.node} set from {path.name}\n   saved to {saved}")
    log.info("Baseline of %s set from %s", run.node, path)
    return 0


def cmd_net_matrix(args: argparse.Namespace) -> int:
    """--net-matrix: every ordered pair of nodes; asks first (it loads the network of the cluster)."""
    kube = Kubectl()
    try:
        wanted = ([n.strip() for n in args.nodes.split(",") if n.strip()] if args.nodes
                  else kube.list_node_names())
        nodes = [kube.get_node(n) for n in wanted]
        busy = [p for p in kube.list_tool_pods() if p.phase not in ("Succeeded", "Failed")]
    except KubectlError as exc:
        ui.emit(f"❌ {exc}")
        return EXIT_ERROR
    nodes = [n for n in nodes if n.ready and n.internal_ip]
    if len(nodes) < 2:
        ui.emit("❌ The network matrix needs at least two Ready nodes with an InternalIP.")
        return EXIT_ERROR
    if busy:
        ui.emit(f"❌ A test of this tool is running right now ({', '.join(p.name for p in busy)}); "
              f"wait for it, the results would be skewed.")
        return EXIT_ERROR
    net_time = args.net_time or 5
    if not MIN_NET_TIME <= net_time <= MAX_NET_TIME:
        ui.emit(f"❌ --net-time must be {MIN_NET_TIME}-{MAX_NET_TIME} s.")
        return EXIT_ERROR
    pairs = len(nodes) * (len(nodes) - 1)
    estimate = pairs * (net_time + 8) + 90
    ui.emit(f"Network matrix: {len(nodes)} nodes, {pairs} pairs x ({net_time} s iperf3 + ping), "
          f"about {describe_duration(estimate)} + installing iperf3.")
    ui.emit("  " + ", ".join(n.name + (" (master, capped at 300 Mbit/s)" if n.is_control_plane else "") for n in nodes))
    if not args.yes:
        if args.non_interactive or not ask_yes_no("Load the network of these nodes now?"):
            ui.emit("Cancelled (--yes confirms without asking).")
            return EXIT_ERROR
    screen = ui.MatrixScreen([n.name for n in nodes], net_time) if ui.MatrixScreen.wanted() else None
    try:
        if screen:
            screen.draw(force=True)
            screen.start_ticker()
        result = netmatrix.run_matrix(kube, nodes, net_time, out=screen.event if screen else ui.emit,
                                      rate_mbit=args.net_rate or 0, screen=screen)
    except KeyboardInterrupt:
        if screen:
            screen.close()
        ui.emit("\n🛑 Interrupted, the helper pods were deleted.")
        return EXIT_INTERRUPTED
    except KubectlError as exc:
        if screen:
            screen.close()
        ui.emit(f"❌ {exc}")
        return EXIT_ERROR
    if screen:
        screen.close()
    ui.emit()
    for line in netmatrix.format_matrix(result, width=ui.avail(130) if ui.adaptive() else None):
        ui.emit(line)
    log_path, json_path = netmatrix.write_matrix(result, user_log_dir(args.log_dir, args.log_flat), open_private)
    ui.emit(f"\n📁 Saved: {log_path}\n📄 {json_path}")
    return 0


def cmd_scheduled() -> int:
    for line in schedule.format_plans(schedule.list_plans()):
        ui.emit(line)
    return 0


def cmd_stop(target: str) -> int:
    ok, message = schedule.cancel(target)
    ui.emit(("✅ " if ok else "❌ ") + message)
    return 0 if ok else EXIT_ERROR


def _plan_title(cfg: StressConfig, label: str) -> str:
    kind = ("GPU" if cfg.gpu else "disk" if cfg.disk else "network" if cfg.net else "stepped CPU" if cfg.stepped
            else "spike CPU" if cfg.spike else "CPU")
    return f"{kind} {describe_duration(cfg.total_duration)} · {label}"


def _profile_name(cfg: StressConfig) -> str:
    return ("gpu" if cfg.gpu else "disk" if cfg.disk else "net" if cfg.net else "stepped" if cfg.stepped
            else "spike" if cfg.spike else "classic")


def _run_extra(cfg: StressConfig, kind: str, nodes: list, log_dir: str = "") -> dict:
    """What the live status screen (menu W) needs to know about a registered test."""
    return {"kind": kind, "profile": _profile_name(cfg), "nodes": list(nodes), "log_dir": log_dir,
            "ram": bool(cfg.ram_pct), "hdd": bool(cfg.hdd), "max_temp": cfg.max_temp,
            "gpu_max_temp": cfg.gpu_max_temp if cfg.gpu else None, "cooldown": cfg.cooldown}


def _run_registered(runner, run_id: str, node: str, duration: int, log_path, title: str, extra: dict, enabled: bool = True) -> int:
    """Runs a test in the foreground and keeps its record in the registry, so that the status screen of another
    terminal sees it (a parallel subprocess does not register: its series does)."""
    if not enabled:
        return runner.run()
    background.register(run_id, node, duration, log_path, "", title=title, extra=extra)
    try:
        return runner.run()
    finally:
        background.unregister(run_id)


def _persist_plan(args: argparse.Namespace, argv: list, title: str, nodes: str) -> int:
    """--persistent: the plan becomes a systemd user timer (the test runs without questions at the start time)."""
    if not schedule.systemd_available():
        ui.emit("❌ systemd user timers are not available here - plan without --persistent (a waiting process, "
                "it does not survive a restart).")
        return EXIT_ERROR
    plan_id = secrets.token_hex(3)
    try:
        schedule.install_timer(plan_id, argv, args.schedule, title, nodes)
    except (OSError, RuntimeError) as exc:
        ui.emit(f"❌ The timer could not be set up: {exc}")
        return EXIT_ERROR
    ui.emit("=" * 52)
    ui.emit("🕒 TEST PLANNED as a systemd timer (survives a restart of this computer)")
    ui.emit("=" * 52)
    for line in ui.kv_block([("Test", title), ("Starts", schedule.describe_when(args.schedule)), ("Plan id", plan_id),
                             ("Cancel", f"./stress.sh --stop {plan_id}"), ("Planned tests", "./stress.sh --scheduled")],
                            label_w=15):
        ui.emit(line)
    if schedule.linger_enabled() is False:
        ui.emit("⚠️  Your user session stops when you log out - for the test to start while you are logged out run once:\n"
                "    sudo loginctl enable-linger $USER")
    return 0


def _single_replay(args: argparse.Namespace, cfg: StressConfig, log_path: str) -> list:
    """The full command line (no questions) of a single-node test, for a systemd timer."""
    options = series.SeriesOptions(
        log_dir=Path(log_path).parent, interval=args.interval, remaining_every=max(args.remaining_every, 0),
        allow_no_sensor=args.allow_no_sensor, hw_privileged=args.hw_privileged, skip_hw=args.no_hw,
        max_busy_pct=args.max_busy_pct, allow_busy_node=args.allow_busy_node, skip_capacity_check=args.no_capacity_check)
    return parallel.child_args(cfg, options, log_path, 1)


def _series_replay(args: argparse.Namespace, template: StressConfig, nodes: list, run_parallel: bool,
                   directory: Path) -> list:
    """The full command line (no questions) of a multi-node test, for a systemd timer."""
    import dataclasses
    options = series.SeriesOptions(
        log_dir=directory, interval=args.interval, remaining_every=max(args.remaining_every, 0),
        allow_no_sensor=args.allow_no_sensor, hw_privileged=args.hw_privileged, skip_hw=args.no_hw,
        max_busy_pct=args.max_busy_pct, allow_busy_node=args.allow_busy_node, skip_capacity_check=args.no_capacity_check)
    base = parallel.child_args(dataclasses.replace(template, node=nodes[0].name), options, "", 1)
    out, skip = [], 0
    for i, word in enumerate(base):
        if skip:
            skip -= 1
        elif word in ("--node", "--log-file", "--concurrent"):
            skip = 1
        else:
            out.append(word)
    out += ["--nodes", ",".join(n.name for n in nodes), "--log-dir", str(directory),
            *(["--parallel", "--start-mode", args.start_mode, "--ready-timeout", str(args.ready_timeout)]
              if run_parallel else ["--no-parallel"])]
    return out


def _print_background_info(node_label: str, duration_text: str, run_id: str, pid: int,
                           log_path: str, console_path: str, start_at: Optional[float] = None) -> None:
    ui.emit("=" * 52)
    ui.emit("🕒 TEST PLANNED (detached from the terminal, survives closing the window)" if start_at else
            "🚀 TEST RUNNING IN THE BACKGROUND (detached from the terminal, survives closing the window)")
    ui.emit("=" * 52)
    rows = [
        ("Node", node_label),
        *([("Starts", schedule.describe_when(start_at))] if start_at else []),
        ("Test duration", duration_text),
        ("Run id / PID", f"{run_id} / {pid}"),
        ("Results", log_path),
        ("Live output", f"tail -f {console_path}"),
        ("Status", "python3 -m stress_test --status   (or ./stress.sh --status)"),
        ("Stop / cancel", f"python3 -m stress_test --stop {run_id}"),
        *([("Planned tests", "python3 -m stress_test --scheduled   (menu 9 SCHEDULE)")] if start_at else []),
    ]
    for line in ui.kv_block(rows, label_w=15):
        ui.emit(line)
    ui.emit("\nIf the test does not start (missing sensor, cannot pull the image...), "
          "the cause will be in the live output.")


def _choose_scope(args: argparse.Namespace) -> str:
    """single / workers / cluster / nodes based on flags (or a question)."""
    chosen = [flag for flag, on in (("--workers", args.workers), ("--cluster", args.cluster),
                                     ("--nodes", bool(args.nodes))) if on]
    if len(chosen) > 1:
        raise ValueError("Give only one of the options --workers, --cluster, --nodes.")
    if chosen and args.node:
        raise ValueError("--node cannot be combined with --workers, --cluster or --nodes.")
    if args.cluster:
        return "cluster"
    if args.workers:
        return "workers"
    if args.nodes:
        return "nodes"
    if args.node or args.non_interactive or args.quick:
        return "single"
    return ask_scope()


def _keep_master(args: argparse.Namespace, template: StressConfig) -> bool:
    """In the stepped/spike test we ask whether to keep the master (CPU cap 70 %)."""
    if args.include_master or not template.multi_stage:
        return True                       # explicitly, or confirmed by the overview before the start
    if args.yes:
        return True
    if args.non_interactive:
        kind = "stepped" if template.stepped else "spike"
        ui.emit(f"ℹ️  Master left out of the {kind} test (no confirmation; add --include-master "
              "or --yes).")
        return False
    if template.stepped:
        capped = tuple(sorted({min(s, MASTER_CPU_CAP) for s in template.steps}))
        return ask_yes_no(f"Keep the master in the stepped test (stages capped at max "
                          f"{MASTER_CPU_CAP} %: {describe_steps(capped)})?")
    capped_target = min(template.spike_target, MASTER_CPU_CAP)
    return ask_yes_no(f"Keep the master in the spike test (target capped at max "
                      f"{MASTER_CPU_CAP} %: {capped_target} %)?")


def _choose_parallel(args: argparse.Namespace, nodes) -> bool:
    """Workers at once or one after another (--parallel/--no-parallel, otherwise a question)."""
    workers = [n for n in nodes if not n.is_control_plane]
    if args.parallel is False:
        return False
    if len(workers) < 2:
        if args.parallel:
            ui.emit("ℹ️  Running in parallel makes sense from two workers, testing one after another.")
        return False
    if args.parallel is True:
        return True
    if args.yes or args.non_interactive or args.quick:
        return False
    return ask_yes_no("Test the workers AT ONCE? (y = at once, n = one after another)")


def _run_series(args: argparse.Namespace, kube: Kubectl, scope: str) -> int:
    """Test of several nodes one after another (workers / whole cluster / list)."""
    names = ([n.strip() for n in args.nodes.split(",") if n.strip()]
             if scope == "nodes" else None)
    nodes, skipped = series.select_nodes(kube, scope, names, include_master=args.include_master)
    for name, reason in skipped:
        ui.emit(f"⚠️  Node {name} skipped: {reason}.")
    template = build_config(args, "*", master_mode=False, series=True)
    template.validate()
    if any(n.is_control_plane for n in nodes) and not _keep_master(args, template):
        nodes = [n for n in nodes if not n.is_control_plane]
        ui.emit("ℹ️  The master is left out of the test.")
    if not nodes:
        ui.emit("❌ Nothing to test (no node is Ready).")
        return EXIT_ERROR
    if args.api_limit <= 0:
        raise ValueError("--api-limit must be greater than 0.")
    if args.ready_timeout < 30:
        raise ValueError("--ready-timeout must be at least 30 s.")
    run_parallel = _choose_parallel(args, nodes)

    workloads = {}
    for node in nodes:
        try:
            workloads[node.name] = kube.list_node_workload(node.name)
        except KubectlError as exc:
            log.warning("Could not find out the workload of node %s: %s", node.name, exc)
    ui.emit("-" * 52)
    for line in series.plan_lines(template, nodes, workloads, parallel=run_parallel):
        ui.emit(line)
    if run_parallel:
        ui.emit(f"API guard: kubectl response slower than {args.api_limit:g} s (or a failure) "
              f"twice in a row stops all tests.")
        if args.start_mode == "sync":
            ui.emit(f"🚦 Synchronised start: the load starts on all nodes together, as soon as every one "
                    f"is ready (wait at most {describe_duration(args.ready_timeout)}).")
        else:
            ui.emit("🟢 Rolling start: every node starts its load as soon as it is ready and runs the full "
                    "time from its own start.")
    if args.schedule:
        ui.emit(f"🕒 Scheduled for {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(args.schedule))}.")
    ui.emit("-" * 52)
    if args.dry_run:
        ui.emit("🔎 Dry run, nothing is being started.")
        return 0
    if not (args.yes or args.quick):
        if args.non_interactive:
            ui.emit("Test refused: confirm with --yes.")
            ui.emit("Exiting.")
            return 0
        if not ask_yes_no("Start the test?"):
            ui.emit("Exiting.")
            return 0
    if template.background and not background.supported():
        ui.emit("❌ Running in the background is not supported on this system (no fork).")
        return EXIT_ERROR
    _maybe_prepull(args, kube, template, [n.name for n in nodes])
    series.preflight(kube, nodes)

    directory = make_private_dir(user_log_dir(args.log_dir, args.log_flat))
    log_path = series.new_series_log_path(directory)
    options = series.SeriesOptions(
        log_dir=directory, out=_out, interval=args.interval,
        remaining_every=max(args.remaining_every, 0), allow_no_sensor=args.allow_no_sensor,
        max_busy_pct=args.max_busy_pct, allow_busy_node=args.allow_busy_node,
        skip_capacity_check=args.no_capacity_check,
        hw_privileged=args.hw_privileged, skip_hw=args.no_hw)
    if run_parallel:
        runner = parallel.ParallelSeriesRunner(kube, template, nodes, skipped, options,
                                               log_path, api_limit=args.api_limit,
                                               sync_start=(args.start_mode == "sync"),
                                               ready_timeout=args.ready_timeout)
    else:
        runner = series.SeriesRunner(kube, template, nodes, skipped, options, log_path)
    if args.persistent and args.schedule:
        label = ", ".join(n.name for n in nodes)
        return _persist_plan(args, _series_replay(args, template, nodes, run_parallel, directory),
                             _plan_title(template, label), label)
    if not template.background:
        _wait_until(args.schedule)
        return _run_registered(runner, secrets.token_hex(3), "cluster", series.estimate_total(template, nodes, run_parallel), log_path,
                               _plan_title(template, f"{len(nodes)} nodes"),
                               _run_extra(template, "foreground", [n.name for n in nodes], str(directory)))

    # --- in the background: the whole series runs in a detached process
    console_path = log_path[:-4] + ".console.txt"
    run_id = secrets.token_hex(3)
    total = series.estimate_total(template, nodes, run_parallel)
    pid = background.detach(console_path)
    if pid:                                       # PARENT: prints the info and exits
        log.info("Series moved to the background: run_id=%s, PID=%s", run_id, pid)
        _print_background_info(f"cluster ({len(nodes)} nodes: " + ", ".join(n.name for n in nodes)
                               + ")", f"about {describe_duration(total)} in total", run_id, pid,
                               log_path, console_path, args.schedule)
        sys.stdout.flush()
        os._exit(0)
    log.info("Child detached from the terminal (PID %s)", os.getpid())
    schedule.wait_registered(args.schedule, run_id, "cluster", total, log_path, console_path,
                             _plan_title(template, f"{len(nodes)} nodes"),
                             extra=_run_extra(template, "background", [n.name for n in nodes], str(directory)))
    try:
        return runner.run()
    finally:
        background.unregister(run_id)


def apply_quick(args: argparse.Namespace) -> None:
    """--quick: fills in values the user did not give (explicitly given options take priority)."""
    if not args.quick:
        return
    if args.profile is None:
        args.profile = PROFILE_CLASSIC
    if args.duration is None:
        args.duration = QUICK_DURATION
    if args.cooldown is None:
        args.cooldown = COOLDOWN_DEFAULT
    if args.log is None:
        args.log = True
    if args.notes is None:
        args.notes = "quick"


def quick_summary(cfg: StressConfig) -> str:
    parts = [cfg.node, describe_duration(cfg.total_duration)]
    if cfg.stepped:
        parts.append("stepped " + describe_steps(cfg.steps) + " %")
    elif cfg.spike:
        parts.append(f"spike {SPIKE_LOW_PCT}<->{cfg.spike_target} % ({cfg.spike_cycles}x)")
    elif cfg.gpu:
        parts.append(f"GPU burn {cfg.gpu_mem_pct} %")
    elif cfg.disk:
        parts.append(f"disk fio {cfg.disk_size} MiB")
    elif cfg.net:
        parts.append(f"network {cfg.net_mode}")
    else:
        parts.append(f"CPU {cfg.cpu_load} %")
    if cfg.ram_pct:
        parts.append(f"RAM {cfg.ram_pct} %")
    if cfg.hdd:
        parts.append("disk")
    parts.append(f"limit {cfg.max_temp} °C")
    parts.append(f"cooldown {describe_duration(cfg.cooldown)}" if cfg.cooldown
                 else "no cooldown")
    parts.append("logged" if cfg.log else "not logged")
    if cfg.background:
        parts.append("in background")
    return " · ".join(parts)


def cmd_migrate_logs() -> int:
    counts = migrate_to_daily()
    ui.emit(f"✅ Moved into day folders: results {counts['logs']}, debug logs {counts['debug']}, pytest runs {counts['tests']}.")
    return 0


def _main(args: argparse.Namespace) -> int:
    apply_quick(args)
    if args.migrate_logs:
        return cmd_migrate_logs()
    if args.status:
        if args.live:
            from . import watch
            return watch.run()
        return cmd_status()
    if args.stop:
        return cmd_stop(args.stop)
    if args.cancel_plan:
        return cmd_stop(args.cancel_plan)
    if args.scheduled:
        return cmd_scheduled()
    if args.compare:
        return cmd_compare(args)
    if args.export_log:
        return cmd_export_log(args)
    if args.set_baseline:
        return cmd_set_baseline(args)
    if args.list_nodes:
        return cmd_list_nodes()
    if args.list_gpus:
        return cmd_list_gpus()
    if args.dashboard:
        from . import dashboard
        return dashboard.run([x.strip() for x in args.nodes.split(",") if x.strip()] if args.nodes else None)
    if args.self_test:
        if shutil.which(os.environ.get("KUBECTL", "kubectl")) is None:
            ui.emit("❌ Error: 'kubectl' was not found.")
            return EXIT_ERROR
        from . import selftest
        return selftest.run(args)
    if args.net_matrix:
        if shutil.which(os.environ.get("KUBECTL", "kubectl")) is None:
            ui.emit("❌ Error: 'kubectl' was not found.")
            return EXIT_ERROR
        return cmd_net_matrix(args)
    if shutil.which(os.environ.get("KUBECTL", "kubectl")) is None:
        ui.emit("❌ Error: 'kubectl' was not found.")
        log.error("kubectl was not found in PATH")
        return EXIT_ERROR

    kube = Kubectl()
    ui.emit("=" * 52)
    ui.emit("K3S STRESS TEST")
    ui.emit("=" * 52)
    try:
        scope = _choose_scope(args)
        if scope != "single":
            return _run_series(args, kube, scope)
        node_name = args.node
        if not node_name:
            if args.non_interactive:
                ui.emit("❌ In --non-interactive mode --node must be given.")
                return EXIT_ERROR
            node_name = choose_node(kube)
        node = kube.get_node(node_name)
        log.info("Node: %s", node)
        if not node.ready:
            ui.emit(f"❌ Node '{node.name}' is not Ready!")
            log.error("Node %s is not Ready", node.name)
            return EXIT_ERROR

        # master protection
        force = args.force or os.environ.get("FORCE") == "1"
        master_mode = node.is_control_plane and not force
        log.info("control-plane=%s force=%s -> master_mode=%s",
                 node.is_control_plane, force, master_mode)
        if node.is_control_plane and force:
            why = "--force" if args.force else "the FORCE=1 environment variable"
            ui.emit(f"⚠️  MASTER PROTECTION BYPASSED by {why}: no CPU cap, no lower temperature limit, "
                  f"no confirmation. The API of the cluster may slow down.")
        if master_mode:
            ui.emit(f"⚠️  '{node.name}' is the MASTER (control-plane). The load may slow down "
                  f"the API and services.\n    CPU will be limited to at most {MASTER_CPU_CAP} %, "
                  f"RAM and disk are not tested (bypass: --force or FORCE=1).")
            if not args.yes and (args.non_interactive or not ask_yes_no("Continue?")):
                ui.emit("Exiting.")
                log.info("The user did not confirm the test on the master")
                return 0

        if not confirm_workload(kube, node, args, already_confirmed=master_mode):
            ui.emit("Exiting.")
            log.info("The user did not confirm the test on a node with services")
            return 0

        cfg = build_config(args, node.name, master_mode)
        if master_mode:
            for msg in cfg.apply_master_limits():
                ui.emit(f"⚠️  {msg}")
                log.info("master limit: %s", msg)
        cfg.validate()
        if args.quick:
            ui.emit(f"⚡ Quick test: {quick_summary(cfg)}")
        if args.schedule:
            ui.emit(f"🕒 Scheduled for "
                  f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(args.schedule))}.")
        if args.dry_run:
            ui.emit(f"🔎 Dry run, nothing is being started: {quick_summary(cfg)}")
            return 0

        if args.log_file or (args.persistent and args.schedule):      # a parallel subprocess / a planned test is always logged
            cfg.log = True
        log_path = None
        if args.log_file:
            make_private_dir(Path(args.log_file).parent)
            log_path = args.log_file
        elif cfg.log:
            directory = make_private_dir(user_log_dir(args.log_dir, args.log_flat))
            stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
            log_path = str(directory / f"{node.name}-{cfg.duration}s-{stamp}.log")
        if log_path:                              # fail now, not after the pods are up and hardware is detected
            open_private(log_path, "a").close()

        if cfg.background and not background.supported():
            ui.emit("❌ Running in the background is not supported on this system (no fork).")
            return EXIT_ERROR

        _maybe_prepull(args, kube, cfg, [node.name])
        runner = StressRunner(
            kube, node, cfg, log_path=log_path,
            interval=args.interval, remaining_every=max(args.remaining_every, 0),
            hw_privileged=args.hw_privileged, skip_hw=args.no_hw, concurrent=args.concurrent,
            allow_no_sensor=args.allow_no_sensor,
            max_busy_pct=args.max_busy_pct, allow_busy_node=args.allow_busy_node,
            skip_capacity_check=args.no_capacity_check,
            start_gate=args.start_gate, gate_timeout=args.ready_timeout,
            gate_wait=not args.no_gate_wait,
            confirm_no_sensor=(None if (args.non_interactive or cfg.background) else
                               lambda: ask_yes_no("Continue without temperature protection?")),
            out=_out)

        if args.persistent and args.schedule:                    # a systemd timer instead of a waiting process
            return _persist_plan(args, _single_replay(args, cfg, log_path), _plan_title(cfg, node.name), node.name)
        if not cfg.background:
            _wait_until(args.schedule)
            return _run_registered(runner, runner.names.run_id, node.name, cfg.total_duration, log_path, _plan_title(cfg, node.name),
                                   _run_extra(cfg, "foreground", [node.name], str(Path(log_path).parent) if log_path else ""),
                                   enabled=args.concurrent <= 1)

        # --- in the background: quick checks first, we want to see errors right away in the terminal
        runner.preflight()
        console_path = log_path[:-4] + ".console.txt"
        pid = background.detach(console_path)
        if pid:                                   # PARENT: prints the info and exits
            log.info("Test moved to the background: run_id=%s, child PID=%s, output=%s",
                     runner.names.run_id, pid, console_path)
            _print_background_info(
                node.name, f"{describe_duration(cfg.total_duration)}  (+ 1–3 min preparation)",
                runner.names.run_id, pid, log_path, console_path, args.schedule)
            sys.stdout.flush()
            os._exit(0)                           # without cleanup: the child does that
        # CHILD: detached, stdout goes to console_path
        log.info("Child detached from the terminal (PID %s)", os.getpid())
        schedule.wait_registered(args.schedule, runner.names.run_id, node.name, cfg.total_duration,
                                 log_path, console_path, _plan_title(cfg, node.name),
                                 extra=_run_extra(cfg, "background", [node.name], str(Path(log_path).parent) if log_path else ""))
        try:
            return runner.run()
        finally:
            background.unregister(runner.names.run_id)
    except RunnerError as exc:
        ui.emit(f"❌ {exc}")
        log.error("Pre-start check failed: %s", exc)
        return EXIT_ERROR
    except ValueError as exc:
        ui.emit(f"❌ {exc}")
        log.error("Invalid settings: %s", exc)
        return EXIT_ERROR
    except KubectlError as exc:
        ui.emit(f"❌ {exc}")
        log.error("KubectlError: %s", exc)
        return EXIT_ERROR
    except OSError as exc:
        ui.emit(f"❌ Cannot create the log folder '{args.log_dir}': {exc}")
        log.exception("OSError while working with the log folder")
        return EXIT_ERROR


def main(argv: Optional[list[str]] = None) -> int:
    argv_list = list(sys.argv[1:] if argv is None else argv)
    debug_path = debuglog.setup(argv_list)      # hidden debug log of every run
    code = EXIT_ERROR
    try:
        if menu.enabled(argv_list):              # bare ./stress.sh in a terminal: the main menu
            code = menu.run_menu()
            return code
        code = _main(build_parser().parse_args(argv_list))
        return code
    except SystemExit as exc:                   # --help, argparse error, EOF on input
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else EXIT_ERROR)
        log.info("SystemExit(%r)", exc.code)
        raise
    except Exception as exc:                    # unexpected error -> traceback to the log
        log.exception("Unexpected error")
        ui.emit(f"❌ Unexpected error: {exc}")
        code = EXIT_ERROR
        return code
    finally:
        log.info("===== END, exit code %s =====", code)
        if code == EXIT_ERROR and debug_path is not None:
            ui.emit(f"ℹ️  Technical details for debugging: {debug_path}")
        debuglog.shutdown()


if __name__ == "__main__":   # pragma: no cover
    sys.exit(main())
