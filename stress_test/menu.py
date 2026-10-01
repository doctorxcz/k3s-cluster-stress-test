"""Main menu shown by `./stress.sh` without any options: picks WHAT to do, the existing flow asks the rest.

Every entry only builds a list of command-line options (shown as the equivalent command) and runs the normal
program with it, so the menu adds no logic of its own and everything it does can also be typed as options.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import textwrap
import time
from pathlib import Path
from typing import Callable, Optional

from . import __version__, ui
from .paths import HIDDEN_LOGS, open_private, result_dirs, user_log_root

TITLE = "KUBERNETES STRESS TEST"
MENU_ITEMS = [
    ("1", "CPU", "classic · stepped · spike"),
    ("2", "GPU", "NVIDIA burn · temperature, clocks"),
    ("3", "DISK", "fio · SMART"),
    ("4", "NET", "one node · matrix of all nodes"),
    ("5", "QUICK", "one node, 10 min, few questions"),
    ("6", "FULL", "self-test of the whole cluster · one report"),
    ("7", "DATA", "compare · baseline · export"),
    ("8", "ADMIN", "what runs · stop · nodes"),
    ("9", "SCHEDULE", "planned tests · plan · cancel"),
]
ICONS = {"1": "⚡", "3": "💾", "4": "📡", "5": "🚀", "7": "📊", "8": "🔧", "6": "🧪", "2": "🎮", "9": "🕒"}
SECTIONS = (("TESTS", ("1", "2", "3", "4", "5")), ("CLUSTER", ("6", "7", "8", "9")))      # the order on the screen (keys of MENU_ITEMS)
# the keys row: (key, name, icon) - the icon tells at a glance what a key does (all of them are emoji of a safe width)
KEYS = [("R", "repeat", "🔁"), ("S", "settings", "🧰"), ("T", "start time", "⏰"), ("D", "dashboard", "📺"),
        ("W", "watch", "🔭"), ("?", "help", "❓"), ("Q", "quit", "🚪")]                                                                   # 0 quits as well
KEYS_COMPACT = KEYS
KEYS_WIDE = [("R", "repeat the last action", "🔁"), ("S", "settings", "🧰"), ("T", "start time", "⏰"),
             ("D", "dashboard", "📺"), ("W", "watch tests", "🔭"), ("?", "help and docs", "❓"), ("Q", "quit (0 works too)", "🚪")]
WIDE_HINTS = {
    "1": "classic · stepped 25/50/75/100 % · spike: temperatures, clock, throttling",
    "3": "fio benchmark (MB/s, IOPS, latency) · SMART health of the disks",
    "4": "iperf3 + ping to a peer · matrix of every node pair · MTU, DNS, internet",
    "5": "one node, 10 min, 100 % CPU, few questions: a fast health check",
    "7": "compare two tests · set a baseline · export a log to JSON / CSV",
    "8": "what is running · stop a test · list nodes (role, state)",
    "2": "NVIDIA GPU burn (gpu-burn): GPU temperature, clocks, throttling · stop limit selectable",
    "9": "planned tests: list what waits for its start, plan a test for later (in 2 h / at 22:30 / on a date), cancel a plan",
    "6": "whole cluster: network → disks → CPU → RAM, cooling pauses, one report (~35 min)",
}
COMPACT_BELOW, WIDE_FROM, WIDE_MAX = 60, 100, 200      # terminal columns: compact < 60 <= normal < 100 <= wide (menu up to 200)
_NODES_CACHE: list = []                                 # (name, is_master, ready) of the last cluster_line()
DOCS_URL = "https://github.com/doctorxcz/k3s-cluster-stress-test"
HELPDESK_URL = DOCS_URL + "/blob/HEAD/HELPDESK.md"
BACK = "0"
EXTRAS_HELP = "s = disk health (SMART, privileged pod), w = ping another node during the test (--net-watch)"


def enabled(argv: list, stdin_is_tty: Optional[bool] = None) -> bool:
    """The menu is for a bare `./stress.sh` typed in a terminal (STRESS_NO_MENU=1 turns it off)."""
    if argv or os.environ.get("STRESS_NO_MENU") == "1":
        return False
    return sys.stdin.isatty() if stdin_is_tty is None else stdin_is_tty


def cluster_line(kube=None) -> str:
    """One line about the cluster for the header ("4 nodes (3 workers + master), all Ready")."""
    try:
        from .kube import Kubectl
        kube = kube or Kubectl()
        nodes = [kube.get_node(n) for n in kube.list_node_names()]
    except Exception:                                    # noqa: BLE001 - the header must never stop the menu
        return "cluster: not reachable (kubectl?)"
    _NODES_CACHE[:] = [(n.name, n.is_control_plane, n.ready) for n in nodes]
    if not nodes:
        return "cluster: no nodes found"
    workers = sum(1 for n in nodes if not n.is_control_plane)
    masters = len(nodes) - workers
    not_ready = [n.name for n in nodes if not n.ready]
    state = "all Ready" if not not_ready else f"NOT Ready: {', '.join(not_ready)}"
    return f"cluster: {len(nodes)} nodes ({workers} workers + {masters} master), {state}"


def _choose(ask: Callable, title: str, entries: list, back_label: str = "Back") -> Optional[int]:
    """Prints a numbered list and returns the index of the choice, None for back/quit."""
    ui.show_choices(title.upper(), [(str(i), label, hint) for i, (label, hint) in enumerate(entries, 1)],
                    back=back_label)
    while True:
        raw = ask("Choice", BACK).strip()
        if raw == BACK or raw == "":
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(entries):
            return int(raw) - 1
        ui.warn("Invalid choice.")


def _extras(ask: Callable, allow_smart: bool = True) -> list:
    """Optional add-ons for a test: returns option strings."""
    raw = ask(f"Extras? ({EXTRAS_HELP}; Enter = none)", "").strip().lower()
    out = []
    if "s" in raw and allow_smart:
        out.append("--smart")
    if "w" in raw:
        out.append("--net-watch")
    return out


def _ask_number(ask: Callable, question: str, default: int, low: int, high: int) -> int:
    while True:
        raw = ask(f"{question} ({low}-{high})", str(default)).strip()
        if raw.isdigit() and low <= int(raw) <= high:
            return int(raw)
        ui.warn(f"Enter a whole number {low}-{high}.")


def _ask_seconds(ask: Callable, question: str, default: str, low: int, high: int) -> int:
    from .parsing import parse_duration
    while True:
        raw = ask(question, default).strip()
        try:
            seconds = parse_duration(raw)
        except ValueError as exc:
            ui.warn(str(exc))
            continue
        if low <= seconds <= high:
            return seconds
        ui.warn(f"The time must be {low}-{high} s.")


def default_gpu_scanner() -> list:
    from . import gpuscan
    return gpuscan.scan_nodes()


def gpu_dialog(ask: Callable, scanner: Optional[Callable] = None) -> Optional[list]:
    """GPU test: every setting a user may want, one by one with sensible defaults, then a summary to confirm.

    Returns the command-line options, None = cancelled.
    """
    from .gpu import (GPU_IMAGE_DEFAULT, GPU_MAX_TEMP_DEFAULT, GPU_MEM_PCT_DEFAULT, GPU_WARN_MARGIN,
                      MAX_GPU_TIME, MIN_GPU_TIME)
    from .parsing import describe_duration
    from . import gpuscan
    on = ui.color_enabled()
    try:
        scans = (scanner or default_gpu_scanner)()
    except Exception as exc:                                    # noqa: BLE001 - a broken scan must not kill the menu
        ui.emit(f"❌ The GPU scan failed: {exc}")
        return None
    print("\n" + "\n".join(gpuscan.table_lines(scans, on)))
    nodes = gpuscan.choose_nodes(scans, ask)
    if not nodes:
        return None
    intro = ui.wrap("NVIDIA GPU burn on one node (needs the NVIDIA driver, toolkit, runtime and device plugin). "
                    "While it runs you see CPU temperature, RAM, GPU temperature, clock, memory and load. "
                    "Enter keeps the default shown in the brackets; 0 at the first question goes back.", ui.panel_width() - 6)
    print("\n" + "\n".join(ui.box(f"{ui.ICON_GPU} GPU TEST SETTINGS", [intro], on, ui.panel_width())))
    i = _choose(ask, "Test length", [("1 min", "quick check of the cooling"), ("2 min", "default"),
                                     ("5 min", "heat soak - the temperature settles"), ("10 min", "long burn-in"),
                                     ("custom", f"{MIN_GPU_TIME}-{MAX_GPU_TIME} s, e.g. 90, 3m")])
    if i is None:
        return None
    seconds = (60, 120, 300, 600)[i] if i < 4 else _ask_seconds(ask, "Test length (e.g. 90, 3m)", "2m", MIN_GPU_TIME, MAX_GPU_TIME)
    max_temp = _ask_number(ask, f"GPU stop temperature in °C (warning {GPU_WARN_MARGIN} °C lower)", GPU_MAX_TEMP_DEFAULT, 50, 95)
    mem = _ask_number(ask, "Share of the GPU memory to fill in %", GPU_MEM_PCT_DEFAULT, 10, 95)
    double = ask("Precision: s = single (default, fast), d = double (slow on consumer cards)", "s").strip().lower().startswith("d")
    raw = ask("Cooldown: measure the cooling after the load, in seconds (0 = none, max 600)", "60").strip()
    cooldown = int(raw) if raw.isdigit() and int(raw) <= 600 else 60
    image = ask(f"Image of the load pod (Enter = {GPU_IMAGE_DEFAULT}; one with gpu_burn skips the build)", "").strip()
    prepull = ask("Pre-pull the CUDA image on the chosen nodes first? (y/n; seconds when it is already cached)", "y").strip().lower() \
        not in ("n", "no")
    extras = _extras(ask)
    options = [*gpuscan.options_for(nodes), "--profile", "gpu", "--time", str(seconds), "--gpu-max-temp", str(max_temp), "--gpu-mem-pct", str(mem),
               "--cooldown", str(cooldown)]
    if double:
        options.append("--gpu-double")
    if prepull:
        options.append("--gpu-prepull")
    if image:
        options += ["--gpu-image", image]
    options += extras
    rows = [("Nodes", ", ".join(nodes) + (" (one after another)" if len(nodes) > 1 else "")),
            ("Test length", describe_duration(seconds)), ("GPU stop at", f"{max_temp} °C (warning from {max_temp - GPU_WARN_MARGIN} °C)"),
            ("GPU memory", f"{mem} % · {'double' if double else 'single'} precision"),
            ("Cooldown", f"{cooldown} s" if cooldown else "off"), ("Image", (image or "default (CUDA 12.x)") + (", pre-pulled first" if prepull else "")),
            ("Extras", " ".join(extras) or "none")]
    print("\n" + "\n".join(ui.box(f"{ui.ICON_GPU} THIS TEST WILL RUN", [ui.kv_block(rows, label_w=14, lead="")], on, ui.panel_width())))
    return options


def start_time_dialog(ask: Callable, now: Optional[float] = None) -> Optional[dict]:
    """T: when does the next test start? Returns {"epoch", "persistent"}, {} = start immediately (plan cleared), None = back."""
    from . import schedule
    prompts = (("Start in (e.g. 30m, 2h, 1h30m)", "1h"), ("Start at a clock time (e.g. 22:30; today, or tomorrow when passed)", "22:30"),
               ("Start on a date and time (e.g. 2026-10-03 02:00, 3.10. 02:00)", ""))
    i = _choose(ask, "Start time", [("in…", "a time from now"), ("at…", "a clock time"), ("on a date…", "a day and a time"),
                                    ("now", "no plan - the next test starts at once")])
    if i is None:
        return None
    if i == 3:
        ui.emit("✅ The next test starts immediately.")
        return {}
    question, default = prompts[i]
    while True:
        raw = ask(question, default).strip()
        try:
            epoch = schedule.parse_start(raw, now)
            break
        except ValueError as exc:
            ui.warn(str(exc))
    persistent = False
    if schedule.systemd_available():
        persistent = ask("Should the plan survive a restart of this computer? (y/n - kept as a systemd timer; n = a waiting "
                         "process)", "n").strip().lower() in ("y", "yes")
    ui.emit(f"🕒 The next test starts {schedule.describe_when(epoch, now)}" + (" (systemd timer)" if persistent else "") + ".")
    return {"epoch": epoch, "persistent": persistent}


def schedule_options(pending: dict) -> list:
    """Options that turn an ordinary action into a planned one."""
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(pending["epoch"]))
    return ["--schedule", when, "--persistent" if pending.get("persistent") else "--background"]


PLANNABLE = ("1", "2", "3", "4", "5", "6")           # CPU, GPU, DISK, NET, QUICK, FULL


def planner(ask: Callable, run: Callable, pending_box: dict) -> None:
    """9 SCHEDULE: the list of planned tests with 'plan a test' and 'cancel a plan'."""
    from . import schedule
    while True:
        plans = schedule.list_plans()
        on = ui.color_enabled()
        rows = ([ui.paint("Nothing is scheduled.", ui.GREY, on)] if not plans else
                [f"{ui.paint(str(n).rjust(2), ui.BOLD + ';' + ui.YELLOW, on)}  {p['id']}  {schedule.describe_when(p['start_at'])}  {p['title']}"
                 + ("  🔁 systemd" if p["kind"] == "systemd" else "") + ("  ▶ running" if p["state"] == "running" else "")
                 for n, p in enumerate(plans, 1)])
        print("\n" + "\n".join(ui.box(f"{ICONS['9']} SCHEDULE", [rows], on, ui.panel_width())))
        entries = [("Plan a test", "set the start time, then choose the test")]
        if plans:
            entries.append(("Cancel a plan", "by its number or id"))
        i = _choose(ask, "Planner", entries)
        if i is None:
            return
        if i == 1:
            from . import catalog
            target = catalog.pick(ask("Plan number or id to cancel (0 = back)", ""), [p["id"] for p in plans])
            if target and target != BACK:
                ok, message = schedule.cancel(target)
                ui.emit(("✅ " if ok else "❌ ") + message)
            continue
        when = start_time_dialog(ask)
        if not when:
            continue
        j = _choose(ask, "Which test?", [("CPU", "classic · stepped · spike"), ("GPU", "NVIDIA burn"), ("DISK", "fio · SMART"),
                                         ("NET", "one node · matrix"), ("QUICK", "10 min"), ("FULL", "self-test of the cluster")])
        if j is None:
            continue
        options = build_action(PLANNABLE[j], ask)
        if options is None:
            continue
        options = apply_settings(options, {"node": "", "max_temp": 0}) + schedule_options(when)
        ui.emit(f"\n▶ {command_text(options)}   (the rest is asked below)\n")
        try:
            run(options)
        except SystemExit:
            pass
        except KeyboardInterrupt:
            print("\n🛑 Interrupted.")


def _words(text: str) -> list:
    return [w for w in text.replace(",", " ").split() if w]


def build_action(choice: str, ask: Callable) -> Optional[list]:
    """Options for a top-level choice (may open a submenu); None = nothing to run."""
    if choice == "1":
        i = _choose(ask, "CPU load", [
            ("classic", "one load for the given time"),
            ("stepped", "gradually 25 / 50 / 75 / 100 %"),
            ("spike", "repeating jump between a low and a target load")])
        if i is None:
            return None
        return ["--profile", ("classic", "stepped", "spike")[i], *_extras(ask)]
    if choice == "3":
        i = _choose(ask, "Disk", [
            ("fio benchmark", "MB/s, IOPS, latency"),
            ("fio benchmark + SMART", "also reads the disk health first")])
        if i is None:
            return None
        return ["--profile", "disk", *(["--smart"] if i == 1 else []), *_extras(ask, allow_smart=False)]
    if choice == "4":
        i = _choose(ask, "Network", [
            ("test one node", "iperf3 + ping to a peer, MTU, link speed"),
            ("matrix of all nodes", "every pair of nodes, tables (loads the network)")])
        if i is None:
            return None
        return ["--profile", "net"] if i == 0 else ["--net-matrix"]
    if choice == "5":
        return ["--quick", *_extras(ask)]
    if choice == "2":
        return gpu_dialog(ask)
    if choice == "7":
        i = _choose(ask, "Results", [
            ("compare two tests", "two logs, or a node = its two newest"),
            ("set a baseline", "a log, or a node = its newest test"),
            ("export a log to JSON/CSV", "a log, or a node = its newest test")])
        if i is None:
            return None
        from . import catalog
        entries = catalog.show(user_log_root())                  # nodes with logs + the numbered newest logs to choose from
        if i == 0:
            first = ask("First number (from the list), a log file, or a node name (= its two newest tests)", "").strip()
            if not first:
                return None
            if len(_words(first)) > 1:                                      # both typed at once (the old way): "n1 n2", "3 1"
                return ["--compare", *catalog.order_pair(_words(first), entries)]
            if not first.isdigit() and not catalog.looks_like_log(first):   # a node name: its two newest tests, no second question
                return ["--compare", first]
            second = ask("Second number (from the list) or a log file", "").strip()
            if not second:
                return None
            return ["--compare", *catalog.order_pair([first, second], entries)]
        word = ask("Number from the list, log file or node name", "").strip()
        if not word:
            return None
        word = catalog.resolve([word], entries)[0]
        return ["--set-baseline", word] if i == 1 else ["--export-log", word]
    if choice == "6":
        return ["--self-test"]
    if choice == "8":
        i = _choose(ask, "Management", [
            ("what is running", "tests in the background and the tool's pods"),
            ("stop a test", "by run id, node or PID"),
            ("list nodes", "role and state")])
        if i is None:
            return None
        if i == 0:
            return ["--status", "--live"]
        if i == 1:
            from . import catalog
            recs = catalog.running_entries()
            print("\n" + "\n".join(catalog.running_lines(recs, ui.color_enabled(), ui.panel_width())))
            target = catalog.pick(ask("Number from the list, run id, node or PID", ""), [r["run_id"] for r in recs])
            return ["--stop", target] if target else None
        return ["--list-nodes"]
    return None


def command_text(options: list) -> str:
    return "./stress.sh" + ("".join(" " + o for o in options))


# --- remembered state: the last action and the settings --------------------------------------

def state_path() -> Path:
    env = os.environ.get("STRESS_TEST_MENU_STATE")
    return Path(env) if env else HIDDEN_LOGS / "menu-state.json"


def load_state() -> dict:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    try:
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open_private(str(path), "w") as fh:
            json.dump(state, fh, indent=1)
    except OSError:
        pass                                              # the menu works without it


def settings_of(state: dict) -> dict:
    return {"node": "", "max_temp": 0, "live": True, **(state.get("settings") or {})}


def apply_settings(options: list, settings: dict) -> list:
    """Adds the saved default node / temperature limit to a single-node test that has none of its own."""
    options = list(options)
    if "--profile" in options or "--quick" in options:
        if settings.get("node") and "--node" not in options and "--nodes" not in options:
            options += ["--node", settings["node"]]
        if settings.get("max_temp") and "--max-temp" not in options:
            options += ["--max-temp", str(settings["max_temp"])]
    return options


def last_temperatures(log_dir: Optional[Path] = None, limit: int = 30) -> dict:
    """{node: max CPU temperature of its newest test log} from the newest result logs - what the tool saw last time."""
    from .logparse import read_log
    from .summary import run_stats
    root = log_dir or user_log_root()
    skip = ("net-matrix", "cluster-", "full-selftest")
    files = sorted((p for folder in result_dirs(root) if folder.is_dir() for p in folder.glob("*.log")
                    if not p.name.startswith(skip)), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    seen: dict = {}
    for path in files:
        try:
            run = read_log(str(path))
            top = run_stats(run.samples, run.baseline_temp).temp_max if run.samples else None
        except Exception:                                # noqa: BLE001 - a bad log must not stop the menu
            continue
        if top is not None and run.node not in seen:
            seen[run.node] = top
    return seen


def last_temperature(log_dir: Optional[Path] = None) -> Optional[tuple]:
    """(node, max CPU temperature) of the hottest of the newest test logs (the newest 6 logs)."""
    temps = last_temperatures(log_dir, 6)
    return max(temps.items(), key=lambda kv: kv[1]) if temps else None


def status_rows(state: dict, kube=None, on: bool = False) -> list:
    """Extra rows of the header: running tests, the last measured temperature, the last action."""
    rows = []
    try:
        from .kube import Kubectl
        busy = [p for p in (kube or Kubectl()).list_tool_pods() if p.phase not in ("Succeeded", "Failed")]
        if busy:
            rows.append(ui.paint(f"🟡 a test is running now ({len(busy)} pods of the tool) - 8 ▸ ADMIN shows it", ui.YELLOW, on))
    except Exception:                                    # noqa: BLE001
        pass
    try:
        temp = last_temperature()
        if temp:
            rows.append(f"🔥 {ui.paint('last measured', ui.GREY, on)} max {ui.paint(f'{temp[1]} °C', ui.temp_code(temp[1]), on)}"
                        f" {ui.paint('(' + temp[0] + ')', ui.GREY, on)}")
    except Exception:                                    # noqa: BLE001
        pass
    last = state.get("last")
    if last and last.get("options"):
        rows.append(f"🔁 {ui.paint('last:', ui.GREY, on)} {command_text(last['options'])} "
                    f"{ui.paint('· ' + last.get('when', ''), ui.GREY, on)}")
    return rows


# --- help ------------------------------------------------------------------------------------

# an entry is a text, or (label, text): the text is wrapped under the label
HELP_PAGES = [
    ("KEYS", [
        "1-9: open the entry. It only builds the usual command line (printed as ▶ ./stress.sh ...) and runs the "
        "normal program, which asks the rest.",
        ("R", "repeat the last action exactly as it ran."),
        ("S", "settings: default node, temperature limit, live frames."),
        ("T", "start time of the NEXT test (in 2 h, at 22:30, on a date); the test you choose afterwards is planned."),
        ("D", "the live cluster dashboard (every node at a glance, three widths)."),
        ("W", "watch the running tests: a live screen (every second) with the details of each kind - GPU, CPU, disk, network, FULL; x stops one."),
        ("?", "this help.     Q = quit (0 works too).     Ctrl+C stops a running test safely (its pods are deleted)."),
        "Everything the menu does can also be typed as options: ./stress.sh --help",
    ]),
    ("⚡ 1 CPU", [
        "Loads the CPU of ONE node and measures temperature, clock, throttling and the cooling afterwards.",
        ("classic", "one constant load for the time you give (RAM and disk load can be added)."),
        ("stepped", "25 / 50 / 75 / 100 % one after another - shows where the node starts to throttle."),
        ("spike", "repeated jumps between a low and a target load - shows how fast the node heats up."),
        "Stops itself at the temperature limit (default 85 °C).",
    ]),
    ("🎮 2 GPU", [
        "NVIDIA GPU burn (gpu-burn) on a node that offers nvidia.com/gpu: GPU temperature, clocks, power, "
        "throttling. No CPU load. You choose the GPU stop temperature (default 80 °C, a warning 5 °C below it).",
        ("needs", "NVIDIA driver, nvidia-container-toolkit, runtime nvidia and the device plugin on the node "
                  "(see HELPDESK.md). The first run pulls a ~3 GB image."),
    ]),
    ("💾 3 DISK", [
        "fio benchmark on one node: sequential and random read/write, MB/s, IOPS, latency. Uses a scratch file "
        "on the node's own disk (removed afterwards, ~1 GiB free needed).",
        ("+ SMART", "reads the disk health first (a privileged pod; a failing disk is refused)."),
        "No CPU load and no cooldown - only what the disk does.",
    ]),
    ("📡 4 NET", [
        ("one node", "iperf3 + ping to a peer: TCP up/down/4 streams, UDP, MTU, link speed, DNS, internet. "
                     "A blocked UDP is reported and skipped (--net-extra no-udp leaves it out)."),
        ("matrix", "every pair of nodes, tables of Mbit/s and ping - finds the weak cable or port."),
        "Needs TCP + UDP ports 30000-32767 open between the nodes (firewall). The master is capped at "
        "300 Mbit/s so that the API keeps its network.",
    ]),
    ("🚀 5 QUICK", [
        "One node, 10 minutes, almost no questions: CPU 100 %, 85 °C limit, 1 min cooldown. "
        "The fastest way to check that a node is healthy.",
    ]),
    ("🧪 6 FULL", [
        "Self-test of the WHOLE cluster in one go: network matrix, disks + SMART, CPU (stepped) and RAM with "
        "cooling pauses between the phases, then one report (log + JSON + a verdict per node).",
        ("levels", "quick ~15 min, standard ~35 min, thorough ~70 min (for a few nodes)."),
        ("warning", "everything runs at full power: the cluster is NOT usable meanwhile, the power draw and "
                    "the room temperature rise. Two confirmations are asked. The master is optional (default: no)."),
    ]),
    ("📺 D DASHBOARD", [
        "Key D: a live overview of every node (a small k9s): CPU, temperature, clock, RAM, power, GPU, network, ping between nodes, pods, "
        "events, the running and planned tests. Three widths - up to 90 columns a compact table, 91-160 more columns and a detail block "
        "of the selected node, wider the whole screen with history graphs and panels; resizing the window redraws it.",
        ("keys", "↑↓ select · Tab detail (overview / pods / events) · Enter detail page · +/- refresh interval · p pause · r refresh · q back."),
        ("read-only", "one tiny unprivileged probe pod per node (and one for nvidia-smi on a GPU node) exists only while the dashboard "
                      "is open and is removed when you leave; nothing is loaded."),
    ]),
    ("🕒 9 SCHEDULE", [
        "Planned tests. Press T first (or use 9 ▸ 1) to set WHEN the next test starts: in a given time (30m, 2h), at a clock time "
        "(22:30 = today, or tomorrow when it has passed) or on a date (2026-10-03 02:00, 3.10. 02:00). Then choose any test "
        "(1-6) and answer its questions as usual - it is planned instead of started.",
        ("how it waits", "by default a detached process waits for the start (closing the terminal does not matter, a restart of "
                         "this computer does). With systemd you can choose 'survive a restart': the plan becomes a systemd user timer."),
        ("cancel", "9 ▸ 2 and the plan id (or ./stress.sh --stop ID). ./stress.sh --scheduled lists the plans."),
    ]),
    ("📊 7 DATA", [
        ("compare", "two test logs side by side (or a node name = its two newest tests)."),
        ("baseline", "remember a test as the node's normal - later tests are checked against it."),
        ("export", "a log to JSON / CSV for a spreadsheet or your scripts."),
        "Results are in the folder logs/ (one .log and .json per node and test).",
    ]),
    ("🔧 8 ADMIN", [
        ("running", "background tests and the tool's pods in the cluster."),
        ("stop", "a test by run id, node or PID (its pods are deleted)."),
        ("nodes", "role and state of every node."),
    ]),
    ("MORE", [
        "Full README (every option) and HELPDESK (troubleshooting, firewall):",
        ("url", DOCS_URL),
        ("url", HELPDESK_URL),
        "Safety: a real load test needs your OK; the master is tested last, capped at 70 % CPU and 80 °C.",
    ]),
]


def help_lines(width: int) -> list:
    """The help as (kind, text) lines wrapped to the width: T = title, L = text; URLs are never split."""
    room = width - 4
    out = []
    for title, entries in HELP_PAGES:
        out.append(("T", title))
        label_w = max((len(e[0]) for e in entries if isinstance(e, tuple) and e[0] != "url"), default=0)
        for entry in entries:
            if isinstance(entry, tuple):
                label, text = entry
                if label == "url":
                    out.append(("L", text))
                    continue
                pad = label_w + 2
                wrapped = textwrap.wrap(text, max(20, room - pad)) or [""]
                out.append(("L", f"{label.ljust(label_w)}  {wrapped[0]}"))
                out += [("L", " " * pad + w) for w in wrapped[1:]]
            else:
                out += [("L", w) for w in textwrap.wrap(entry, room)]
        out.append(("L", ""))
    return out


def show_help(ask: Callable, size: Optional[tuple] = None) -> None:
    """The help in pages that fit the terminal; Enter = next page, 0 = back. A wide terminal (>= 100 columns) shows it in
    two columns, a narrow one (< 60) in a frame as wide as the window."""
    on = ui.color_enabled()
    fallback = shutil.get_terminal_size((80, 24))
    cols = size[0] if size else (ui.term_cols() or fallback.columns)
    rows = size[1] if size else fallback.lines
    md = menu_mode(cols)
    width = max(ui.MIN_COLS, min(WIDE_MAX if md == "wide" else ui.WIDTH, cols - 1))
    two = md == "wide"
    col_w = (width - 4 - 3) // 2 if two else width - 4
    content = help_lines(col_w + 4)
    per_page = max(6, rows - 6)
    cap = per_page * (2 if two else 1)
    pages, page = [], []
    for kind, text in content:
        if kind == "T" and len(page) >= cap - 4:          # do not leave a title at the bottom of a page
            pages.append(page)
            page = []
        page.append((kind, text))
        if len(page) >= cap:
            pages.append(page)
            page = []
    if page:
        pages.append(page)
    for number, lines in enumerate(pages, 1):
        body = [ui.paint(text, ui.BOLD + ";" + ui.YELLOW, on) if kind == "T" else text for kind, text in lines]
        if two:
            half = (len(body) + 1) // 2
            body = ui.columns2(body[:half], body[half:], col_w, ui.paint(" │ ", ui.GREY, on))
        print()
        print("\n".join(ui.box(f"HELP {number}/{len(pages)}", [body], on, width)))
        if number < len(pages):
            if ask("Enter = next page, 0 = back", "").strip() == BACK:
                return
        else:
            ask("Enter = back to the menu", "")


# --- settings ---------------------------------------------------------------------------------

def settings_screen(ask: Callable, state: dict) -> None:
    """S: default node, temperature limit, live frames on/off (kept between runs)."""
    while True:
        cfg = settings_of(state)
        on = ui.color_enabled()
        entries = [("1", f"default node", cfg["node"] or "ask every time"),
                   ("2", "temperature limit", f"{cfg['max_temp']} °C" if cfg["max_temp"] else "default (85 °C)"),
                   ("3", "live frames", "on" if cfg["live"] else "off (plain text output)"),
                   ("4", "reset", "back to the defaults")]
        ui.show_choices("SETTINGS", entries, back="Back")
        raw = ask("Choice", BACK).strip()
        if raw in (BACK, ""):
            return
        if raw == "1":
            from . import catalog
            names = [n for n, _m, _r in _NODES_CACHE]
            shown = catalog.nodes_lines(list(_NODES_CACHE), None, on, ui.panel_width())
            if shown:
                print("\n" + "\n".join(shown))
            cfg["node"] = catalog.pick(ask("Default node: number from the list or a name (Enter = ask every time)", cfg["node"]), names)
        elif raw == "2":
            value = ask("Temperature limit in °C (40-95, 0 = default)", str(cfg["max_temp"])).strip()
            cfg["max_temp"] = int(value) if value.isdigit() and (value == "0" or 40 <= int(value) <= 95) else cfg["max_temp"]
        elif raw == "3":
            cfg["live"] = not cfg["live"]
        elif raw == "4":
            cfg = {"node": "", "max_temp": 0, "live": True}
        else:
            ui.warn("Invalid choice.")
            continue
        state["settings"] = cfg
        save_state(state)
        _apply_live(cfg)


def _apply_live(cfg: dict) -> None:
    if cfg.get("live", True):
        if os.environ.get("STRESS_NO_LIVE") == "menu":
            os.environ.pop("STRESS_NO_LIVE", None)
    elif not os.environ.get("STRESS_NO_LIVE"):
        os.environ["STRESS_NO_LIVE"] = "menu"


# --- the screen and the loop -------------------------------------------------------------------

def menu_mode(cols: int) -> str:
    """'compact' (very narrow window), 'normal' or 'wide' from the number of terminal columns."""
    return "compact" if cols < COMPACT_BELOW else "wide" if cols >= WIDE_FROM else "normal"


def _wide_columns(names: dict, on: bool, temps: Optional[dict], col: int = 66) -> tuple:
    """The two columns of the wide menu: TESTS + tips | CLUSTER + nodes. Long hints are wrapped under the name."""
    name_w = max(6, max(len(v[0]) for v in names.values()))
    indent = 9 + name_w                                  # cells of " 7 ▸ 🧪 FULL   " (the emoji takes two)

    def items(keys) -> list:
        out = []
        for k in keys:
            hot = ui.RED if names[k][0] == "FULL" else ui.BOLD
            parts = textwrap.wrap(WIDE_HINTS[k], max(20, col - indent)) or [""]
            out.append(f"{ui.paint(k.rjust(2), ui.BOLD + ';' + ui.YELLOW, on)} {ui.paint('▸', ui.GREY, on)} {ICONS[k]} "
                       f"{ui.paint(names[k][0].ljust(name_w), hot, on)} {ui.paint(parts[0], ui.GREY, on)}")
            out += [" " * indent + ui.paint(part, ui.GREY, on) for part in parts[1:]]
        return out

    left = [ui.paint("TESTS", ui.GREY, on), *items(SECTIONS[0][1]), "", ui.paint("TIPS", ui.GREY, on)]
    for tip in ("R repeats the last action exactly as it ran (shown in the header as 🔁).",
                "S saves a default node and a temperature limit for the tests.",
                "? opens the help: every entry explained, links to README and HELPDESK.",
                "A test in a terminal is one live frame; FULL asks twice before it starts."):
        parts = textwrap.wrap(tip, max(20, col - 2))
        left += [ui.paint(("· " if i == 0 else "  ") + part, ui.GREY, on) for i, part in enumerate(parts)]
    right = [ui.paint("CLUSTER", ui.GREY, on), *items(SECTIONS[1][1]), "", ui.paint("NODES", ui.GREY, on)]
    temps = temps or {}
    for name, master, ready in _NODES_CACHE[:10]:
        temp = temps.get(name)
        tail = f"  🔥 {ui.paint(f'{temp} °C', ui.temp_code(temp), on)}" if temp is not None else ""
        state = ui.paint("Ready", ui.GREEN, on) if ready else ui.paint("NOT Ready", ui.RED, on)
        right.append(f"{ui.ICON_NODE} {name[:26]:<26} {ui.paint('master' if master else 'worker', ui.GREY, on):<14} {state}{tail}")
    if len(_NODES_CACHE) > 10:
        right.append(ui.paint(f"… and {len(_NODES_CACHE) - 10} more", ui.GREY, on))
    if temps:
        right.append(ui.paint("🔥 = hottest temperature in the newest test log of the node", ui.GREY, on))
    return left, right


def _tall_extra(on: bool, temps: Optional[dict], room: int, rows: int) -> list:
    """What a tall window (>= 40 lines) adds under the menu items: the tips and the nodes with their temperatures."""
    out = [ui.paint("TIPS", ui.GREY, on)]
    for tip in ("R repeats the last action exactly as it ran.", "S saves a default node and a temperature limit.",
                "T / 9 plan a test for later (a detached process or a systemd timer).",
                "D live cluster dashboard · W live status of the running tests.",
                "? opens the help: every entry explained, links to README and HELPDESK."):
        out += [ui.paint(("· " if i == 0 else "  ") + part, ui.GREY, on) for i, part in enumerate(textwrap.wrap(tip, max(16, room - 2)))]
    if _NODES_CACHE:
        out += ["", ui.paint("NODES", ui.GREY, on)]
        temps = temps or {}
        for name, master, ready in _NODES_CACHE[:max(3, rows - 48)]:
            temp = temps.get(name)
            tail = f"  🔥 {ui.paint(f'{temp} °C', ui.temp_code(temp), on)}" if temp is not None else ""
            out.append(f"{ui.ICON_NODE} {name[:max(10, room - 24)]}  {ui.paint('master' if master else 'worker', ui.GREY, on)}  "
                       + (ui.paint("Ready", ui.GREEN, on) if ready else ui.paint("NOT Ready", ui.RED, on)) + tail)
    return out


def menu_lines(cluster: str, status: list, on: bool, width: Optional[int] = None, cols: Optional[int] = None,
               temps: Optional[dict] = None, rows: Optional[int] = None) -> list:
    """The menu as lines. The look follows the width of the terminal: compact / normal / wide (two columns)."""
    names = {key: (name, hint) for key, name, hint in MENU_ITEMS}
    if not cols:                                        # without a terminal (pipe, tests) the normal width is used
        cols = ui.cols()
    mode = menu_mode(cols)
    status_rows_ = [ui.paint("› ", ui.GREEN, on) + ui.paint(cluster, ui.GREY, on), *status]
    if mode == "wide":
        width = width or min(cols - 1, WIDE_MAX)
        left, right = _wide_columns(names, on, temps, ((width - 4) - 3) // 2)
        return ui.menu_box_wide(__version__, status_rows_, left, right, KEYS_WIDE, on, width)
    sections = [(label, [(k, ICONS[k], names[k][0], names[k][1]) for k in keys]) for label, keys in SECTIONS]
    height = ui.term_rows() if rows is None else rows
    tall = height is not None and ui.height_mode(height) == "tall"
    if mode == "compact":
        w = width or max(cols - 1, 30)
        return ui.menu_box(__version__, status_rows_[:2], sections, KEYS_COMPACT, on, w, compact=True,
                           extra=_tall_extra(on, temps, w - 4, height) if tall else None)
    w = width or min(cols - 1, ui.WIDTH)
    return ui.menu_box(__version__, status_rows_, sections, KEYS, on, w, extra=_tall_extra(on, temps, w - 4, height) if tall else None)


def run_menu(ask: Optional[Callable] = None, run: Optional[Callable] = None,
             cluster: Optional[Callable[[], str]] = None, status: Optional[Callable] = None) -> int:
    """The menu loop: run the chosen action, come back to the menu, until 0. Returns 0."""
    from . import cli
    injected = cluster is not None
    persist = run is None                                 # tests that inject `run` must not write the state file
    ask = ask or cli.ask
    run = run or (lambda options: cli._main(cli.build_parser().parse_args(options)))
    cluster = cluster or cluster_line
    state = load_state()
    _apply_live(settings_of(state))
    pending: dict = {}                                    # {"epoch", "persistent"}: the start time of the NEXT test (key T)
    while True:
        on = ui.color_enabled()
        extra = status(state) if status else ([] if injected else status_rows(state, on=on))
        if pending:
            from . import schedule
            extra = [ui.paint(f"🕒 next test starts: {schedule.describe_when(pending['epoch'])}"
                              + (" · systemd timer" if pending.get("persistent") else "") + "  (T changes it)", ui.YELLOW, on),
                     *extra]
        print()
        cols = ui.cols()
        temps = None
        if menu_mode(cols) == "wide" and not injected:
            try:
                temps = last_temperatures()
            except Exception:                            # noqa: BLE001
                temps = None
        print("\n".join(menu_lines(cluster(), extra, on, cols=cols, temps=temps)))
        try:
            choice = ask("Choice", "q").strip()
            if choice in (BACK, "q", "Q"):
                return 0
            if choice in ("?", "h", "H"):
                show_help(ask)
                continue
            if choice in ("s", "S"):
                settings_screen(ask, state)
                continue
            if choice in ("t", "T"):
                answer = start_time_dialog(ask)
                if answer is not None:
                    pending = answer
                continue
            if choice == "9":
                planner(ask, run, pending)
                continue
            if choice in ("w", "W"):
                options = ["--status", "--live"]
                ui.emit(f"\n▶ {command_text(options)}\n")
                try:
                    run(options)
                except (SystemExit, KeyboardInterrupt):
                    pass
                continue
            if choice in ("d", "D"):
                options = ["--dashboard"]
                ui.emit(f"\n▶ {command_text(options)}\n")
                try:
                    run(options)
                except (SystemExit, KeyboardInterrupt):
                    pass
                continue
            if choice in ("r", "R"):
                last = (state.get("last") or {}).get("options")
                if not last:
                    ui.warn("Nothing to repeat yet.")
                    continue
                options = list(last)
            else:
                options = build_action(choice, ask)
                if options is not None:
                    options = apply_settings(options, settings_of(state))
        except (KeyboardInterrupt, EOFError):
            print()
            return 0
        if options is None:
            if choice not in {key for key, _, _ in MENU_ITEMS}:
                ui.warn("Invalid choice.")
            continue
        state["last"] = {"options": options, "when": time.strftime("%Y-%m-%d %H:%M")}
        if persist:
            save_state(state)
        if pending and (choice in PLANNABLE or choice in ("r", "R")):
            options = options + schedule_options(pending)    # the chosen test is planned for the time set with T
            pending = {}
        ui.emit(f"\n▶ {command_text(options)}   (the rest is asked below)\n")
        try:
            code = run(options)
        except SystemExit as exc:                        # argparse error / "Input ended." inside the flow
            if exc.code not in (None, 0) and not isinstance(exc.code, int):
                print(exc.code)
            code = exc.code if isinstance(exc.code, int) else 1
        except KeyboardInterrupt:
            print("\n🛑 Interrupted.")
            code = 130
        print(f"\n(exit code {code})" if code else "")
        try:
            ask("Enter = back to the menu", "")
        except (KeyboardInterrupt, EOFError):
            print()
            return 0
