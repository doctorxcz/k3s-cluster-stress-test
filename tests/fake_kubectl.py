#!/usr/bin/env python3
"""Fake kubectl for integration tests (no cluster needed).

State is kept separately for every run (by the suffix of the pod names, e.g. stress-test-a1b2c3),
so several tests at once do not influence each other.

Controlled by environment variables:
  FAKE_STATE       directory for state files (required)
  FAKE_NODES       JSON list of nodes, e.g. [{"name":"w1"},{"name":"m1","master":true},
                   {"name":"w2","ready":false}] (default one node "fake-node")
  FAKE_TEMP        CPU temperature in °C while stress-ng runs at full load (default 40)
  FAKE_TEMP_IDLE   CPU temperature when idle (before the test and after cooldown; default 40)
  FAKE_COOL_STEP   by how many °C the temperature drops at every reading after the load ends (default 2)
  FAKE_FREQ        CPU clock in MHz (default 3000)
  FAKE_FREQ_DROP   "1" = the clock under load drops by 100 MHz at every reading (throttling simulation)
  FAKE_CPU_BUSY    CPU utilisation in % under the load of a classic test (default 100; idle 10, cooldown 5)
  FAKE_MASTER      "1" = the only default node has the control-plane role
  FAKE_NO_SENSOR   "1" = the probe does not return the CPU temperature
  FAKE_RUN         how long (s) stress-ng "runs" in total (in the stepped test it is split between the stages);
                   at the end it prints "successful run completed" (for every stage separately)
  FAKE_KILL_AFTER  after N seconds the pod "disappears" (as if someone deleted it from outside)
  FAKE_API_DELAY   `get --raw /readyz` answers after N seconds (overloaded API simulation)
  FAKE_API_FAIL    "1" = `get --raw /readyz` fails
  FAKE_NODE_PODS   JSON list of pods running on the node (workload)
  FAKE_NODE_PODS_BY_NODE  JSON {"node name": [list of pods]}, overrides FAKE_NODE_PODS for the node
  FAKE_MEM_AVAILABLE_KB  MemAvailable from the probe in kB (default 6000000)
  FAKE_NO_MEM      "1" = the probe does not return MemAvailable (test of the fallback computation)
  FAKE_OTHER_PODS  JSON list of other pods of the tool
The file events.txt in FAKE_STATE receives lines "<time> start|end <node>" (when the load was running).
"""
import json
import os
import re
import sys
import time

state = os.environ["FAKE_STATE"]
args = sys.argv[1:]


def flag(name):
    return os.path.join(state, name)


def rid_of(pod):
    """Run suffix: stress-test-a1b2c3 -> a1b2c3."""
    return pod.rsplit("-", 1)[-1]


def sflag(name, rid):
    return flag(f"{name}-{rid}")


def deleted(pod):
    return os.path.exists(flag(f"deleted-{pod}"))


def read_int(path, default=0):
    try:
        return int(open(path).read())
    except (OSError, ValueError):
        return default


def write_int(path, value):
    with open(path, "w") as fh:
        fh.write(str(value))


def event(kind, node):
    with open(flag("events.txt"), "a") as fh:
        fh.write(f"{time.time():.3f} {kind} {node}\n")


def node_object(spec):
    labels = {"node-role.kubernetes.io/control-plane": "true"} if spec.get("master") else {}
    return {
        "metadata": {"name": spec["name"], "labels": labels},
        "status": {
            "conditions": [{"type": "Ready", "status": "True" if spec.get("ready", True) else "False"}],
            "allocatable": {"memory": "8000000Ki"},
            "capacity": {"cpu": "8"},
            "nodeInfo": {"osImage": "FakeOS", "kernelVersion": "1.0",
                         "architecture": "amd64", "containerRuntimeVersion": "fake://1"},
        },
    }


NODE_SPECS = json.loads(os.environ.get("FAKE_NODES") or "null") or [
    {"name": "fake-node", "master": os.environ.get("FAKE_MASTER") == "1"}]

cmd = args[0]
if cmd == "get":
    if args[1] == "--raw":                        # API response check (/readyz)
        time.sleep(float(os.environ.get("FAKE_API_DELAY", "0")))
        if os.environ.get("FAKE_API_FAIL") == "1":
            print("Error from server (ServiceUnavailable)", file=sys.stderr)
            sys.exit(1)
        print("ok")
    elif args[1] == "nodes":
        print(json.dumps({"items": [node_object(s) for s in NODE_SPECS]}))
    elif args[1] == "node":
        found = [s for s in NODE_SPECS if s["name"] == args[2]]
        if not found:
            print(f'Error from server (NotFound): nodes "{args[2]}" not found', file=sys.stderr)
            sys.exit(1)
        print(json.dumps(node_object(found[0])))
    elif args[1] == "pods" and "--field-selector" in args:   # node workload
        selector = args[args.index("--field-selector") + 1]
        node_name = selector.split("=", 1)[1] if "=" in selector else ""
        by_node = json.loads(os.environ.get("FAKE_NODE_PODS_BY_NODE", "{}"))
        pods = by_node.get(node_name, json.loads(os.environ.get("FAKE_NODE_PODS", "[]")))
        items = []
        for p in pods:
            items.append({"metadata": {"name": p["name"], "namespace": p["ns"],
                                       "labels": p.get("labels", {})},
                          "status": {"phase": p.get("phase", "Running")}})
        print(json.dumps({"items": items}))
    elif args[1] == "pods":                       # list of the tool's pods (-l app=...)
        items = []
        for p in json.loads(os.environ.get("FAKE_OTHER_PODS", "[]")):
            items.append({"metadata": {"name": p["name"], "labels": {"run-id": p["run"]}},
                          "spec": {"nodeName": p["node"]},
                          "status": {"phase": p["phase"]}})
        print(json.dumps({"items": items}))
    elif args[1] == "pod":                        # pod phase (jsonpath)
        pod = args[2]
        if pod.startswith("stress-test") and deleted(pod):
            print("Error from server (NotFound): pods not found", file=sys.stderr)
            sys.exit(1)
        print("Succeeded")
elif cmd == "top":
    print("fake-node 1000m 12% 2000Mi 25%")
elif cmd == "apply":
    manifest = json.loads(sys.stdin.read())
    name = manifest["metadata"]["name"]
    rid = rid_of(name)
    with open(flag(f"manifest-{name}.json"), "w") as fh:     # for checking in tests
        json.dump(manifest, fh)
    if name.startswith("temp-probe"):                       # start of a run: clean state
        for stale in ("stress-started", "stress-ended", "cool-calls", "load-calls", "stage-pct"):
            if os.path.exists(sflag(stale, rid)):
                os.remove(sflag(stale, rid))
    if name.startswith("stress-test"):
        open(sflag("stress-started", rid), "w").close()
        if os.path.exists(flag(f"deleted-{name}")):
            os.remove(flag(f"deleted-{name}"))
elif cmd == "delete":
    for a in args[2:]:
        if a.startswith("stress-test"):
            open(flag(f"deleted-{a}"), "w").close()
            open(sflag("stress-ended", rid_of(a)), "w").close()   # the load ended
elif cmd == "wait":
    pass
elif cmd == "logs":
    pod = args[-1]
    rid = rid_of(pod)
    if "-f" in args:
        run_total = float(os.environ.get("FAKE_RUN", "4"))
        kill = os.environ.get("FAKE_KILL_AFTER")
        start = time.time()
        kill_at = start + float(kill) if kill else None
        # stepped test: the stages are found in the pod command (a loop `for p in 25 50 ...`)
        script, node = "", "?"
        try:
            manifest = json.load(open(flag(f"manifest-{pod}.json")))
            script = manifest["spec"]["containers"][0]["command"][2]
            node = manifest["spec"].get("nodeName", "?")
        except (OSError, KeyError, ValueError):
            pass
        stages = []
        match = re.search(r"for p in ([\d ]+);", script)
        if "STRESS-STAGE" in script and match:
            stages = [int(x) for x in match.group(1).split()]
        finished = True
        event("start", node)

        def run_for(seconds):
            """Prints progress; returns False when the pod disappeared or was killed."""
            end = time.time() + seconds
            while time.time() < end:
                if deleted(pod):
                    return False
                if kill_at and time.time() >= kill_at:
                    open(flag(f"deleted-{pod}"), "w").close()   # the pod disappeared from outside
                    return False
                print("stress-ng: running...", flush=True)
                time.sleep(0.5)
            return True

        def metrics(percent):
            print("stress-ng: metrc: [1] stressor       bogo ops real time  usr time  sys time   bogo ops/s     bogo ops/s", flush=True)
            print("stress-ng: metrc: [1]                           (secs)    (secs)    (secs)   (real time) (usr+sys time)", flush=True)
            print(f"stress-ng: metrc: [1] cpu               {percent * 800}    180.01    112.28      0.21       {percent * 6.5:.2f}         702.96", flush=True)
            print("stress-ng: info:  [1] successful run completed in 3 mins, 0.01 secs", flush=True)

        if stages:
            for i, percent in enumerate(stages, 1):
                print(f"STRESS-STAGE {i}/{len(stages)} {percent}%", flush=True)
                write_int(sflag("stage-pct", rid), percent)
                if not run_for(run_total / len(stages)):
                    finished = False
                    break
                metrics(percent)
        else:
            finished = run_for(run_total)
            if finished:
                print("stress-ng: metrc: [1] stressor       bogo ops real time  usr time  sys time   bogo ops/s     bogo ops/s", flush=True)
                print("stress-ng: metrc: [1]                           (secs)    (secs)    (secs)   (real time) (usr+sys time)", flush=True)
                print("stress-ng: metrc: [1] cpu               79080    120.01    112.28      0.21       658.93         702.96", flush=True)
                print("stress-ng: metrc: [1] matrix           175024    120.00    111.74      0.15      1458.51        1564.25", flush=True)
                print("stress-ng: info:  [1] successful run completed in 1 min 5.95 secs", flush=True)
        open(sflag("stress-ended", rid), "w").close()             # the load ended
        event("end", node)
    elif pod.startswith("hw-info"):
        print("CPU: Fake CPU\nThreads: 8")
    else:
        print("STRESS-NG STARTED")
elif cmd == "exec":
    rid = rid_of(args[1])
    started = os.path.exists(sflag("stress-started", rid))
    ended = os.path.exists(sflag("stress-ended", rid))
    phase = "cool" if (started and ended) else ("load" if started else "idle")
    idle_t = int(os.environ.get("FAKE_TEMP_IDLE", "40"))
    load_t = int(os.environ.get("FAKE_TEMP", "40"))
    stage_pct = read_int(sflag("stage-pct", rid), 0)         # stepped test: the current stage
    if phase == "idle":
        temp = idle_t
    elif phase == "load":
        temp = idle_t + (load_t - idle_t) * stage_pct // 100 if stage_pct else load_t
    else:                                                   # cooldown: the temperature gradually drops
        n = read_int(sflag("cool-calls", rid)) + 1
        write_int(sflag("cool-calls", rid), n)
        temp = max(idle_t, load_t - n * int(os.environ.get("FAKE_COOL_STEP", "2")))
    freq = int(os.environ.get("FAKE_FREQ", "3000"))
    if phase == "load" and os.environ.get("FAKE_FREQ_DROP") == "1":
        n = read_int(sflag("load-calls", rid)) + 1
        write_int(sflag("load-calls", rid), n)
        freq = max(800, freq - n * 100)
    busy_load = stage_pct if stage_pct else int(os.environ.get("FAKE_CPU_BUSY", "100"))
    busy = {"idle": 10, "load": busy_load, "cool": 5}[phase]
    total = read_int(sflag("cpu-total", rid)) + 1000        # the /proc/stat counters grow
    idle = read_int(sflag("cpu-idle", rid)) + 1000 * (100 - busy) // 100
    write_int(sflag("cpu-total", rid), total)
    write_int(sflag("cpu-idle", rid), idle)
    lines = []
    if os.environ.get("FAKE_NO_SENSOR") != "1":
        lines.append(f"temp coretemp {temp * 1000}")
    lines.append("temp nvme 41000")
    lines.append(f"freq {freq}")
    lines.append(f"cpu_stat {total} {idle}")
    if os.environ.get("FAKE_NO_MEM") != "1":
        lines.append("mem_total_kb 8000000")
        lines.append(f"mem_available_kb {os.environ.get('FAKE_MEM_AVAILABLE_KB', '6000000')}")
    print("\n".join(lines))
