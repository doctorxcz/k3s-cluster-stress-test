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
  FAKE_PULL_FAIL  "1" = the prebuilt images (names with /k3s-stress-) cannot be pulled: the pod's waiting reason is ErrImagePull
  GPU simulation (profile gpu):
  FAKE_GPU         "1" = the node has allocatable nvidia.com/gpu: 1 and the hw script prints an NVIDIA GPU line;
                   "plugin-missing" = the hw script prints NVIDIA, but allocatable has no nvidia.com/gpu;
                   anything else = no GPU at all
  FAKE_GPU_TEMP    GPU temperature in °C under the gpu-burn load (default 60)
  FAKE_GPU_IDLE_TEMP  GPU temperature when idle / after the load (default 40)
  FAKE_GPU_POWER   "na" = power.draw/power.limit are [N/A] (Quadro P620), otherwise watts (default 55)
  FAKE_GPU_THROTTLE  clocks_throttle_reasons mask under load, e.g. 0x20 (default 0x0)
  FAKE_GPU_FAIL    "faulty" = gpu_burn reports FAULTY, "nofinish" = it prints no result, "build" = gpu-burn
                   cannot be built (the pod prints the FAILED marker)
  FAKE_GPU_BUSY    "1" = another pod on the node already requests nvidia.com/gpu: 1
  FAKE_GPU_FAN     GPU fan.speed as nvidia-smi prints it ("45", or "[N/A]"); unset = the field is not printed
  FAKE_GPU_SMI_FAIL  "1" = `exec ... nvidia-smi` fails
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


def gpu_mode(node):
    """FAKE_GPU_BY_NODE {"node": mode} overrides FAKE_GPU per node (modes: 1, plugin-missing, intel, none, scan-fail)."""
    by_node = json.loads(os.environ.get("FAKE_GPU_BY_NODE", "{}"))
    return by_node.get(node, os.environ.get("FAKE_GPU", ""))


def node_object(spec):
    labels = {"node-role.kubernetes.io/control-plane": "true"} if spec.get("master") else {}
    return {
        "metadata": {"name": spec["name"], "labels": labels, "creationTimestamp": "2026-09-01T10:00:00Z"},
        "spec": {"taints": spec.get("taints", []), "unschedulable": bool(spec.get("cordoned"))},
        "status": {
            "conditions": [{"type": "Ready", "status": "True" if spec.get("ready", True) else "False"}],
            "allocatable": dict({"memory": "8000000Ki", "cpu": "8", "pods": "110", "ephemeral-storage": "90000Mi"},
                                **({"nvidia.com/gpu": "1"} if gpu_mode(spec["name"]) == "1" else {})),
            "capacity": {"cpu": "8", "memory": "8000000Ki", "pods": "110", "ephemeral-storage": "100000Mi"},
            "images": [{"sizeBytes": 500_000_000}, {"sizeBytes": 250_000_000}],
            "addresses": [{"type": "InternalIP", "address": spec.get("ip", "10.0.0.1")},
                          {"type": "Hostname", "address": spec["name"]}],
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
    elif args[1] == "pods" and "-A" in args and "--field-selector" not in args:       # dashboard: every pod of the cluster
        default = [{"name": f"app-{s['name']}", "ns": "default", "node": s["name"], "phase": "Running", "restarts": 0}
                   for s in NODE_SPECS]
        pods = json.loads(os.environ.get("FAKE_DASH_PODS", "null")) or default
        print(json.dumps({"items": [{"metadata": {"name": p["name"], "namespace": p.get("ns", "default")},
                                     "spec": {"nodeName": p["node"], "containers": [{"resources": {"requests": {
                                         "cpu": p.get("cpu", "100m"), "memory": p.get("mem", "128Mi")}}}]},
                                     "status": {"phase": p.get("phase", "Running"),
                                                "containerStatuses": [{"restartCount": p.get("restarts", 0)}]}} for p in pods]}))
    elif args[1] == "events":
        events = json.loads(os.environ.get("FAKE_EVENTS", "null"))
        events = [{"reason": "BackOff", "kind": "Pod", "name": "app-x", "message": "Back-off restarting failed container", "time": "2026-10-01T14:00:00Z"}] if events is None else events
        print(json.dumps({"items": [{"lastTimestamp": e.get("time", ""), "reason": e["reason"], "message": e["message"],
                                     "involvedObject": {"kind": e.get("kind", "Pod"), "name": e.get("name", "x")}} for e in events]}))
    elif args[1] in ("ingress", "pvc", "jobs", "cronjobs") and "-o" in args and args[args.index("-o") + 1] == "json":
        items = {"pvc": [{"status": {"phase": "Bound"}, "spec": {"resources": {"requests": {"storage": "10Gi"}}}}],
                 "jobs": [{"status": {"succeeded": 1}}, {"status": {"active": 1, "failed": 2}}], "ingress": [{}, {}], "cronjobs": [{}]}[args[1]]
        print(json.dumps({"items": items}))
    elif args[1] in ("pv", "configmaps") and "name" in args:
        print("\n".join(f"{args[1]}/x{i}" for i in range(3)))
    elif args[1] in ("deployments", "statefulsets", "daemonsets"):
        print(json.dumps({"items": [{"spec": {"replicas": 2}, "status": {"readyReplicas": 2, "numberReady": 2, "desiredNumberScheduled": 2}}]}))
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
        if os.environ.get("FAKE_GPU_BUSY") == "1":              # another pod holds the GPU
            items.append({"metadata": {"name": "cuda-job", "namespace": "default", "labels": {}},
                          "spec": {"containers": [{"resources": {"limits": {"nvidia.com/gpu": "1"}}}]},
                          "status": {"phase": "Running"}})
        print(json.dumps({"items": items}))
    elif args[1] == "pods":                       # list of the tool's pods (-l app=...)
        items = []
        for p in json.loads(os.environ.get("FAKE_OTHER_PODS", "[]")):
            items.append({"metadata": {"name": p["name"], "labels": {"run-id": p["run"]}},
                          "spec": {"nodeName": p["node"]},
                          "status": {"phase": p["phase"]}})
        print(json.dumps({"items": items}))
    elif args[1] == "services":                   # all Services (NodePorts in use)
        ports = json.loads(os.environ.get("FAKE_NODEPORTS", "[]"))
        print(json.dumps({"items": [{"spec": {"ports": [{"nodePort": p} for p in ports]}}]}))
    elif args[1] == "service":                    # Service address (jsonpath)
        print("" if os.environ.get("FAKE_NO_SERVICE_IP") == "1" else "10.43.0.7")
    elif args[1] == "pod":                        # pod phase (jsonpath)
        pod = args[2]
        if pod.startswith("stress-test") and deleted(pod):
            print("Error from server (NotFound): pods not found", file=sys.stderr)
            sys.exit(1)
        if "jsonpath={.status.containerStatuses[*].state.waiting.reason}" in args:      # why a pod does not start (image pull problems)
            reason = ""
            if os.environ.get("FAKE_PULL_FAIL") == "1":                                  # the prebuilt images (k3s-stress-*) cannot be pulled
                try:
                    image = json.load(open(flag(f"manifest-{pod}.json")))["spec"]["containers"][0]["image"]
                    reason = "ErrImagePull" if "/k3s-stress-" in image else ""
                except (OSError, KeyError, ValueError):
                    pass
            print(reason)
            sys.exit(0)
        print("10.42.0.9" if "jsonpath={.status.podIP}" in args else "Succeeded")
elif cmd == "top" and args[1] == "nodes":
    for s in NODE_SPECS:
        print(f"{s['name']} 400m 5% 2000Mi 25%")
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
        if os.path.exists(sflag("stress-ended", rid)):                 # the pod is applied again (image fallback): the load starts anew
            os.remove(sflag("stress-ended", rid))
        if os.path.exists(flag(f"deleted-{name}")):
            os.remove(flag(f"deleted-{name}"))
elif cmd == "delete":
    if "-l" in args:                                  # `delete pod -l app=...` (dashboard leftovers)
        with open(flag("deleted-by-label"), "a") as fh:
            fh.write(args[args.index("-l") + 1] + "\n")
    for a in args[2:]:
        if a.startswith("stress-test"):
            open(flag(f"deleted-{a}"), "w").close()
            open(sflag("stress-ended", rid_of(a)), "w").close()   # the load ended
elif cmd == "wait":
    pass
elif cmd == "logs":
    pod = args[-1]
    rid = rid_of(pod)
    if os.environ.get("FAKE_BAD_UTF8") == "1":                 # a pod that prints bytes which are not valid UTF-8
        sys.stdout.flush()
        sys.stdout.buffer.write(b"caf\xe9 \xff\xfe not utf-8\n")
        sys.stdout.buffer.flush()
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

        gpu_run = "GPU-DONE" in script
        net_jobs = re.findall(r'NET-JOB \$i/\d+ (\S+)"', script)
        if net_jobs:                                                # network test: one result per job
            print("STRESS-NG STARTED", flush=True)
            mbps = float(os.environ.get("FAKE_NET_MBPS", "940"))
            errs = int(os.environ.get("FAKE_NET_ERR", "0"))
            n_link = 0
            for i, name in enumerate(net_jobs, 1):
                print(f"NET-JOB {i}/{len(net_jobs)} {name}", flush=True)
                if not run_for(run_total / len(net_jobs)):
                    finished = False
                    break
                if name in ("link", "link-end"):
                    n_link += 1
                    e = errs if name == "link-end" else 0
                    payload = (f"if=eth0 speed={os.environ.get('FAKE_NET_SPEED', '1000')} duplex=full "
                               f"rx_errors={e} rx_dropped=0 tx_errors=0 tx_dropped=0")
                elif name == "ping":
                    loss = os.environ.get("FAKE_NET_LOSS", "0")
                    payload = (f"50 packets transmitted, 50 received, {loss}% packet loss, time 9800ms "
                               f"rtt min/avg/max/mdev = 0.210/0.350/0.900/0.080 ms ")
                elif name == "mtu":
                    payload = "mtu=1500"
                elif name == "dns":
                    payload = (f"cluster_ms={os.environ.get('FAKE_DNS_MS', '1.20')} external_ms=4.50 "
                               f"fails={os.environ.get('FAKE_DNS_FAILS', '0')}")
                elif name == "internet":
                    payload = ""
                elif name == "mtr":
                    payload = ("Start: 2026-09-29T14:00:00+0200;HOST: x  Loss%   Snt   Last   Avg  Best  Wrst StDev;"
                               "  1.|-- 10.0.0.2   0.0%     5    0.3   0.3   0.2   0.5   0.1;")
                elif name == "udp":
                    payload = json.dumps({"end": {"sum": {"bits_per_second": 100e6, "jitter_ms": 0.04,
                                                          "lost_percent": 0.0}}})
                else:
                    rate_mbps = float(os.environ.get("FAKE_NET_SVC_MBPS", mbps)) if name == "tcp-svc" else mbps
                    payload = json.dumps({"end": {"sum_sent": {"retransmits": 2},
                                                  "sum_received": {"bits_per_second": rate_mbps * 1e6}}})
                if os.environ.get("FAKE_NET_FAIL") == name:
                    print(f"NET-FAILED {name}", flush=True)
                    finished = False
                    break
                if name == "internet":
                    print("NET-RESULT internet-ping 5 packets transmitted, 5 received, 0% packet loss, time 800ms "
                          "rtt min/avg/max/mdev = 5.0/6.0/7.0/0.8 ms ", flush=True)
                    print("NET-RESULT internet-down speed=25000000 ttfb=0.056 code=206", flush=True)
                else:
                    print(f"NET-RESULT {name} {payload}", flush=True)
            else:
                print("NET-DONE", flush=True)
        disk_jobs = re.findall(r'"(\S+) (?:read|write|randread|randwrite) ', script) if "DISK-JOB" in script else []
        if net_jobs:
            pass
        elif gpu_run:                                               # gpu-burn: ends with GPU-INFO lines + GPU-DONE / GPU-FAILED
            print("STRESS-NG STARTED", flush=True)
            finished = run_for(run_total)
            if finished:
                fail = os.environ.get("FAKE_GPU_FAIL", "")
                print("GPU-INFO GPU 0: OK" if fail != "faulty" else "GPU-INFO GPU 0: FAULTY", flush=True)
                print("GPU-PERF 100.0%  proc'd: 56 (1086 Gflop/s)   errors: 0   temps: 65 C", flush=True)
                print("GPU-INFO Tested 1 GPUs:", flush=True)
                if fail == "faulty":
                    print("GPU-FAILED faulty (compute errors)", flush=True)
                elif fail == "nofinish":
                    print("GPU-FAILED gpu_burn did not finish correctly", flush=True)
                else:
                    print("GPU-DONE", flush=True)
        elif disk_jobs:                                             # disk benchmark (fio): one result per job
            mbs = float(os.environ.get("FAKE_DISK_MBS", "400"))
            print("STRESS-NG STARTED", flush=True)
            for i, name in enumerate(disk_jobs, 1):
                print(f"DISK-JOB {i}/{len(disk_jobs)} {name}", flush=True)
                if not run_for(run_total / len(disk_jobs)):
                    finished = False
                    break
                side = "write" if "write" in name else "read"
                job = {"jobs": [{side: {"io_bytes": 1000, "bw_bytes": int(mbs * 1e6), "iops": mbs * 10,
                                        "lat_ns": {"mean": 2_000_000.0},
                                        "clat_ns": {"percentile": {"99.000000": 5_000_000}}}}]}
                print(f"DISK-RESULT {name} " + json.dumps(job), flush=True)
                if os.environ.get("FAKE_DISK_FAIL") == "1" and i == 2:
                    print(f"DISK-FAILED {name}", flush=True)
                    finished = False
                    break
            else:
                print("DISK-DONE", flush=True)
        elif stages:
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
    elif pod.startswith("net-mx"):                              # network matrix helper pod: installed, waiting
        bad = os.environ.get("FAKE_MX_NOT_READY", "")
        try:
            node = json.load(open(flag(f"manifest-{pod}.json")))["spec"]["nodeName"]
        except (OSError, KeyError, ValueError):
            node = "?"
        print("MX-FAILED" if bad and bad == node else "MX-READY")
    elif pod.startswith("hw-info"):
        try:
            with open(flag(f"manifest-{pod}.json")) as fh:
                kind = json.load(fh)["spec"]["containers"][0]["name"]
        except (OSError, ValueError, KeyError):
            kind = "hw-info"
        if kind == "net-server":                                # network test: the iperf3 server on the peer
            print("apt noise\n-----------------------------------------------------------\n"
                  "Server listening on 5201 (test #1)" if os.environ.get("FAKE_NET_SERVER_FAIL") != "1"
                  else "NET-SERVER-FAILED")
        elif kind == "smart":                                     # --smart: SMART data of one fake disk
            if os.environ.get("FAKE_SMART_RAW") is not None:
                print(os.environ["FAKE_SMART_RAW"])
            else:
                passed = "false" if os.environ.get("FAKE_SMART_FAIL") == "1" else "true"
                realloc = os.environ.get("FAKE_SMART_REALLOC", "0")
                print("SMART-BEGIN /dev/sda")
                print(json.dumps({"model_name": "Fake SSD 256GB", "smart_status": {"passed": passed == "true"},
                                  "temperature": {"current": 35}, "power_on_time": {"hours": 1234},
                                  "ata_smart_attributes": {"table": [
                                      {"id": 5, "raw": {"value": int(realloc)}},
                                      {"id": 197, "raw": {"value": 0}}]}}))
                print("SMART-END")
        elif kind == "gpu-scan":                                  # GPU scan: lspci of the node (FAKE_GPU_BY_NODE)
            try:
                node = json.load(open(flag(f"manifest-{pod}.json")))["spec"]["nodeName"]
            except (OSError, KeyError, ValueError):
                node = "?"
            mode = gpu_mode(node)
            if mode == "scan-fail":
                sys.exit(1)
            print("GPU-CARD Intel Corporation HD Graphics 4600 [8086:0412] (rev 06)")
            if mode in ("1", "plugin-missing"):
                print("GPU-CARD NVIDIA Corporation GP107GL [Quadro P620] [10de:1cb6] (rev a1)")
            print("SCAN-DONE")
        else:
            print("CPU: Fake CPU\nThreads: 8")
            if os.environ.get("FAKE_GPU") in ("1", "plugin-missing"):
                print("GPU: NVIDIA Corporation GP107GL [Quadro P620]")
    else:
        gated = False
        try:
            gated = "STRESS-NG READY" in json.load(open(flag(f"manifest-{pod}.json")))["spec"]["containers"][0]["command"][2]
        except (OSError, KeyError, ValueError):
            pass
        if gated:
            print("STRESS-NG READY")
        script_txt = ""
        try:
            script_txt = json.load(open(flag(f"manifest-{pod}.json")))["spec"]["containers"][0]["command"][2]
        except (OSError, KeyError, ValueError):
            pass
        if "GPU-DONE" in script_txt and os.environ.get("FAKE_GPU_FAIL") == "build":
            print("STRESS-NG FAILED")
            sys.exit(0)
        if not gated or os.path.exists(sflag("gate-go", rid)):
            print("STRESS-NG STARTED")
elif cmd == "exec" and "touch /tmp/go" in args[-1]:              # synchronised start: the GO for the stress pod
    open(sflag("gate-go", rid_of(args[1])), "w").close()
elif cmd == "exec" and "nvidia-smi" in args[-1]:               # GPU readings from the stress pod
    if os.environ.get("FAKE_GPU_SMI_FAIL") == "1":
        print("NVIDIA-SMI has failed", file=sys.stderr)
        sys.exit(9)
    if "driver_version" in args[-1]:                              # static card info (read once before the load)
        print("Quadro P620, 580.178.04, 2048, [N/A], 1island, 3504, 3, 16, 6.1, 86.07.3C.00.0B".replace("1island", "1721"))
        sys.exit(0)
    rid = rid_of(args[1])
    started = os.path.exists(sflag("stress-started", rid))
    ended = os.path.exists(sflag("stress-ended", rid))
    idle_t = int(os.environ.get("FAKE_GPU_IDLE_TEMP", "40"))
    load_t = int(os.environ.get("FAKE_GPU_TEMP", "60"))
    if started and not ended:
        temp, util, sm, used, mask = load_t, 100, 1300, 1800, os.environ.get("FAKE_GPU_THROTTLE", "0x0")
    else:
        temp, util, sm, used, mask = idle_t, 0, 139, 0, "0x0000000000000001"
        if started:
            n = read_int(sflag("gpu-cool", rid)) + 1
            write_int(sflag("gpu-cool", rid), n)
            temp = max(idle_t, load_t - n * int(os.environ.get("FAKE_COOL_STEP", "2")))
    if os.environ.get("FAKE_GPU_POWER", "55") == "na":
        power, limit = "[N/A]", "[N/A]"
    else:
        power = os.environ.get("FAKE_GPU_POWER", "55") if util else "10"
        limit = "120"
    fan_field = ", " + os.environ["FAKE_GPU_FAN"] if "FAKE_GPU_FAN" in os.environ else ""
    print(f"{temp}, {power}, {limit}, {sm}, 3504, {util}, {used}, 2048, {'P0' if util else 'P8'}, {mask}{fan_field}")
elif cmd == "exec" and args[1].startswith("net-mx"):           # network matrix helper pods
    script = args[-1]
    try:
        me = json.load(open(flag(f"manifest-{args[1]}.json")))["spec"]["nodeName"]
    except (OSError, KeyError, ValueError):
        me = "?"
    if "/proc/net/route" in script:
        print(f"if=eth0 speed={json.loads(os.environ.get('FAKE_MX_SPEED', '{}')).get(me, 1000)} duplex=full")
    elif "MX-PING" in script:                                   # ping from the client node to the server node
        target = re.search(r"ping -c \d+ -i [\d.]+ -q (\S+)", script).group(1)
        peer = next((s["name"] for s in NODE_SPECS if s.get("ip") == target), "?")
        loss = json.loads(os.environ.get("FAKE_MX_LOSS", "{}")).get(f"{me}>{peer}", 0)
        print(f"MX-PING 10 packets transmitted, 10 received, {loss}% packet loss, time 1800ms "
              f"rtt min/avg/max/mdev = 0.200/0.400/0.800/0.100 ms ")
    elif "MX-IPERF" in script:                                  # iperf3 -R run on the server node: data flows peer -> me
        target = re.search(r"iperf3 -c (\S+) ", script).group(1)
        sender = next((s["name"] for s in NODE_SPECS if s.get("ip") == target), "?")
        mbps = json.loads(os.environ.get("FAKE_MX_MBPS", "{}")).get(f"{sender}>{me}", 940.0)
        if os.environ.get("FAKE_MX_FAIL") == f"{sender}>{me}":
            print("MX-IPERF iperf3: error - unable to connect")
        else:
            print("MX-IPERF " + json.dumps({"end": {"sum_sent": {"retransmits": 1},
                                                    "sum_received": {"bits_per_second": mbps * 1e6}}}))
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
    if "PING_TARGET=" in args[-1]:                          # --net-watch: the probe pings another node
        if phase == "load" and os.environ.get("FAKE_PING_LOST_UNDER_LOAD") == "1":
            lines.append("ping_ms lost")
        else:
            lines.append(f"ping_ms {os.environ.get('FAKE_PING_LOAD', '3.0') if phase == 'load' else os.environ.get('FAKE_PING_IDLE', '0.4')}")
    if "cut -d' ' -f1 /proc/uptime" in args[-1]:           # dashboard: the extra values of EXTRA_SCRIPT
        n = args[1]
        lines += ["uptime 93784", "load 0.52 0.40 0.31", "swap_total_kb 0", "swap_free_kb 0", "threads 8", "cores 4",
                  "cpu_model Intel(R) Core(TM) i7-4790S CPU @ 3.20GHz", "thr 0",
                  f"nic eno1 1000 up {read_int(sflag('rx', rid)) + 1_500_000} {read_int(sflag('tx', rid)) + 400_000} 1500 full 0 0 2 0",
                  "disk sda 238 0 Patriot P210 256",
                  "dmi sys_vendor Dell Inc.", "dmi product_name OptiPlex 9020", "dmi bios_version A18", "dmi bios_date 07/04/2018",
                  "gov powersave 800 3600 3600", "turbo 0", "cpufreqs 3600 3500 3600 3590", "ctemp Core_0 52000", "ctemp Package_id_0 53000",
                  "tz acpitz 27000", "psi cpu some 0.40", "psi memory some 0.00", "psi io full 0.30",
                  "mem MemFree 8192000", "mem Cached 5120000", "mem Dirty 12288",
                  f"dio sda {read_int(sflag('dio-r', rid)) + 2000} {read_int(sflag('dio-w', rid)) + 1000}",
                  "files 4200", "tasks 2/534"]
        write_int(sflag("dio-r", rid), read_int(sflag("dio-r", rid)) + 2000)
        write_int(sflag("dio-w", rid), read_int(sflag("dio-w", rid)) + 1000)
        write_int(sflag("rx", rid), read_int(sflag("rx", rid)) + 1_500_000)
        write_int(sflag("tx", rid), read_int(sflag("tx", rid)) + 400_000)
        for p in args[-1].split('PEERS="', 1)[-1].split('"', 1)[0].split():
            lines.append(f"peer {p.split('=')[0]} 0.4")
    watts = os.environ.get("FAKE_WATTS")
    if watts:                                               # RAPL: the energy counter grows by watts * 0.5 s per call (interval of run_tool)
        energy = read_int(sflag("rapl-uj", rid)) + int(float(watts) * 500_000)
        write_int(sflag("rapl-uj", rid), energy)
        lines.append(f"rapl_uj {energy} 262143000000 {os.environ.get('FAKE_PL1_UW', '25000000')} 51000000")
    print("\n".join(lines))
