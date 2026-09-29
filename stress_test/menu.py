"""Main menu shown by `./stress.sh` without any options: picks WHAT to do, the existing flow asks the rest.

Every entry only builds a list of command-line options (shown as the equivalent command) and runs the normal
program with it, so the menu adds no logic of its own and everything it does can also be typed as options.
"""
from __future__ import annotations

import os
import sys
from typing import Callable, Optional

from . import __version__

TITLE = "KUBERNETES STRESS TEST"
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
    if not nodes:
        return "cluster: no nodes found"
    workers = sum(1 for n in nodes if not n.is_control_plane)
    masters = len(nodes) - workers
    not_ready = [n.name for n in nodes if not n.ready]
    state = "all Ready" if not not_ready else f"NOT Ready: {', '.join(not_ready)}"
    return f"cluster: {len(nodes)} nodes ({workers} workers + {masters} master), {state}"


def _choose(ask: Callable, title: str, entries: list, back_label: str = "Back") -> Optional[int]:
    """Prints a numbered list and returns the index of the choice, None for back/quit."""
    print()
    print(title)
    for i, (label, hint) in enumerate(entries, 1):
        print(f"  [{i}] {label:<26}{hint}")
    print(f"  [{BACK}] {back_label}")
    while True:
        raw = ask("Choice", BACK).strip()
        if raw == BACK or raw == "":
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(entries):
            return int(raw) - 1
        print("  Invalid choice.")


def _extras(ask: Callable, allow_smart: bool = True) -> list:
    """Optional add-ons for a test: returns option strings."""
    raw = ask(f"Extras? ({EXTRAS_HELP}; Enter = none)", "").strip().lower()
    out = []
    if "s" in raw and allow_smart:
        out.append("--smart")
    if "w" in raw:
        out.append("--net-watch")
    return out


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
    if choice == "2":
        i = _choose(ask, "Disk", [
            ("fio benchmark", "MB/s, IOPS, latency"),
            ("fio benchmark + SMART", "also reads the disk health first")])
        if i is None:
            return None
        return ["--profile", "disk", *(["--smart"] if i == 1 else []), *_extras(ask, allow_smart=False)]
    if choice == "3":
        i = _choose(ask, "Network", [
            ("test one node", "iperf3 + ping to a peer, MTU, link speed"),
            ("matrix of all nodes", "every pair of nodes, tables (loads the network)")])
        if i is None:
            return None
        return ["--profile", "net"] if i == 0 else ["--net-matrix"]
    if choice == "4":
        return ["--quick", *_extras(ask)]
    if choice == "5":
        i = _choose(ask, "Results", [
            ("compare two tests", "two logs, or a node = its two newest"),
            ("set a baseline", "a log, or a node = its newest test"),
            ("export a log to JSON/CSV", "a log, or a node = its newest test")])
        if i is None:
            return None
        if i == 0:
            words = _words(ask("Two logs (older newer), or one node name", ""))
            return ["--compare", *words] if words else None
        word = ask("Log file or node name", "").strip()
        if not word:
            return None
        return ["--set-baseline", word] if i == 1 else ["--export-log", word]
    if choice == "6":
        i = _choose(ask, "Management", [
            ("what is running", "tests in the background and the tool's pods"),
            ("stop a test", "by run id, node or PID"),
            ("list nodes", "role and state")])
        if i is None:
            return None
        if i == 0:
            return ["--status"]
        if i == 1:
            target = ask("Run id, node or PID", "").strip()
            return ["--stop", target] if target else None
        return ["--list-nodes"]
    return None


def command_text(options: list) -> str:
    return "./stress.sh" + ("".join(" " + o for o in options))


def run_menu(ask: Optional[Callable] = None, run: Optional[Callable] = None,
             cluster: Optional[Callable[[], str]] = None) -> int:
    """The menu loop: run the chosen action, come back to the menu, until 0. Returns 0."""
    from . import cli
    ask = ask or cli.ask
    run = run or (lambda options: cli._main(cli.build_parser().parse_args(options)))
    cluster = cluster or cluster_line
    while True:
        print()
        print("=" * 46)
        print(f"   {TITLE}          v{__version__}")
        print(f"   {cluster()}")
        print("=" * 46)
        print("  [1] CPU load             classic · stepped · spike")
        print("  [2] Disk                 fio: MB/s, IOPS, latency · SMART")
        print("  [3] Network              one node · matrix of all nodes")
        print("  [4] Quick test           one node, 10 min, few questions")
        print("  [5] Results              compare · baseline · export")
        print("  [6] Management           what runs · stop · nodes")
        print("  [0] Quit")
        try:
            choice = ask("Choice", BACK).strip()
            if choice in (BACK, "q", "Q"):
                return 0
            options = build_action(choice, ask)
        except (KeyboardInterrupt, EOFError):
            print()
            return 0
        if options is None:
            if choice not in "123456":
                print("  Invalid choice.")
            continue
        print(f"\n▶ {command_text(options)}   (the rest is asked below)\n")
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
