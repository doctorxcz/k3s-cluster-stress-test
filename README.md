# Kubernetes stress test (Python)

> **English build 1.6.1a** — functionally identical to 1.6.0, with every message, log line, help text,
> document, comment and test translated to English. Result logs written by this build are in English.

Load test of a node in a k3s/Kubernetes cluster. It starts `stress-ng` in a pod directly on the chosen
node, measures load, temperatures and CPU clock during the test and **stops the test by itself when the node
overheats**. It is a rewrite of the original script `stress-node-v5.sh` in Python (no external
dependencies, only the standard library and `kubectl`).

## Requirements
- Python 3.9+ (tested on 3.12)
- `kubectl` with access to the cluster (k3s ships a symlink `/usr/local/bin/kubectl`)
- `metrics-server` (for the `kubectl top` fallback of the RAM test; default in k3s)
- a node with internet access (the pods pull the image and install `stress-ng` via `apt`)

## Getting started on your own cluster
1. **Clone/download** this repo onto a machine that can reach your cluster (typically the master, but
   any machine with a working `kubectl` context works).
2. **Point `kubectl` at your cluster** — this tool has no config of its own, it just runs `kubectl` with
   whatever context/kubeconfig is already active (`kubectl config current-context` to check,
   `kubectl get nodes` to confirm access).
3. **List your node names** with `kubectl get nodes` — that's exactly what you pass to `--node NAME`
   (or just run the tool without `--node` and pick from the interactive list).
4. **Master/control-plane detection is automatic** — the tool reads the node's role from Kubernetes
   itself (no node names are hardcoded anywhere), so master protection (lower CPU cap, lower temperature
   limit, confirmation prompt) applies to whichever node actually has the control-plane role in *your*
   cluster.
5. **Run it**: `python3 -m stress_test` (interactive) or `./stress.sh` — see options below. Start with a
   short, low-impact run (e.g. `--time 60 --cpu-load 50`) on one worker node before testing the whole
   cluster or the master.
6. **Requirements on the node being tested**: internet access (the pod installs `stress-ng` via `apt`)
   and, for the RAM test fallback, `metrics-server` in the cluster (`kubectl top nodes` should work).

No cloud credentials, tokens or external accounts are needed — everything runs through your own
`kubectl` access.

## Usage
From the project folder (e.g. `~/cluster-testing/python-stress-test-en`):

```bash
python3 -m stress_test                     # interactive, asks for everything
python3 -m stress_test --node hp-g2-celeron --time 300 --max-temp 80 --log
python3 -m stress_test --node hp-g2-celeron --non-interactive --time 120
python3 -m stress_test --help
```

| Option | Meaning |
|---|---|
| `--node NAME` | the node to test (otherwise it offers a list) |
| `--time DURATION` | test duration: seconds or `30s`, `5m`, `1h`, `1h30m` (no unit = seconds) |
| `--max-temp °C` | the test stops at this CPU temperature (2 readings in a row), 60–95, default 85 |
| `--cpu-load %` | CPU load, default 100 |
| `--ram-pct %` | enables the RAM test: how many % of **free** memory to allocate |
| `--hdd` / `--no-hdd` | stress the disk |
| `--log` / `--no-log` | save the test results to `logs/<node>-<duration>s-<date>.log` |
| `--log-dir DIR` | folder with the results (default `logs` in the project directory; a relative path is taken from the project, an absolute one is used unchanged) |
| `-b`, `--background` | run the test **in the background** (see below), logging turns on by itself. Default: no |
| `--status` | shows tests running in the background and the tool's pods in the cluster |
| `--stop ID\|NODE\|PID` | gracefully stops a test running in the background (pod cleanup) |
| `--compare LOG [LOG]` | compare two tests from logs (or just a node name = its two newest), see below |
| `--profile classic\|stepped` | classic test (default), or stepped 25/50/75/100 % (always logged) |
| `--steps 25,50,75,100` | stages of the stepped test in % |
| `--step-time DURATION` | length of one stage (default 3 min) |
| `--workers` / `--cluster` / `--nodes a,b` | test several nodes one after another (workers / the whole cluster / a list) |
| `--parallel` / `--no-parallel` | workers at once (master alone afterwards) / one after another; without a flag a question is asked |
| `--api-limit SEC` | when running in parallel: an API response slower than this many seconds (2× in a row) stops all tests |
| `--include-master` | also include the master with `--workers`; in the stepped test without a question |
| `--cooldown DURATION` | after the test keep measuring temperature and clock (default 60 s, `0` = off, max 600 s) |
| `--notes TEXT` | a note for the log |
| `--force` | bypass the master protection (`FORCE=1 python3 -m stress_test` works too) |
| `-y`, `--yes` | confirm questions automatically (e.g. for the master) |
| `--allow-no-sensor` | continue even without a CPU temperature sensor |
| `--no-hw` | do not detect node hardware (the `hw-info` pod is not started) |
| `--hw-privileged` | the `hw-info` pod runs privileged (to list RAM modules via `dmidecode`); the default is without `privileged` |
| `--non-interactive` | do not ask anything, take missing values from the defaults (`--node` required) |
| `--interval S` | measurement interval, default 5 s |
| `--remaining-every N` | every Nth measurement also prints how much is left (default 3, `0` = off) |

Anything not given as an option is asked in interactive mode (answers are `y`/`n`).

## Running in the background
Interactively, at the end of the options it asks `Run in the background? (survives closing the terminal, logging turns on by itself) (y/n)`
(Enter = no). Without questions use `--background`. With `y` **logging turns on automatically** (even if
`--no-log` was given) and the test detaches from the terminal: it survives closing the window and losing the
SSH connection. Only a short message stays on the screen:

```
TEST RUNNING IN THE BACKGROUND (detached from the terminal, survives closing the window)
  Node:          dell-9020-sff-i7
  Test duration: 1 h (3600 s)  (+ 1–3 min preparation)
  Run id / PID:  728ade / 1025
  Results:       .../logs/dell-9020-sff-i7-3600s-<date>.log
  Live output:   tail -f .../logs/dell-9020-sff-i7-3600s-<date>.console.txt
  Stop:          python3 -m stress_test --stop 728ade
```
- **Results** (`logs/*.log`) are the same as for a regular test. **Live output** (`*.console.txt`)
  is everything that would otherwise run on the screen, including errors.
- **Checks before detaching** (e.g. that no other test runs on the node) happen in the terminal, you see an
  error right away. Later problems (missing sensor, image cannot be pulled) are in the live output.
- A background test **does not ask questions** (e.g. about a test without a temperature sensor), such a
  test is refused unless `--allow-no-sensor` is given.
- `--status` and `--stop` read the registry in `.logs/running/`. `--stop` sends a graceful signal, the tool
  deletes its pods and `Test interrupted` stays in the log. As with other tests, the pods are also guarded
  by `activeDeadlineSeconds` in case the process disappears.
- A background test **does not survive a machine restart**. It needs a system with `fork` (Linux, macOS).

## Summary and cooldown after the test
After `stress-ng` ends the tool **keeps measuring CPU temperature and clock for another 60 s**
(`--cooldown DURATION`, `0` = off, max 600 s; `Ctrl+C` skips the cooldown). Then it prints a **summary**
that is also saved to the log:

```
TEST SUMMARY
Samples:                  23 during the test, 12 during cooldown
CPU temp (test):          min 43 | avg 47 | max 49 °C   (idle before test 39 °C)
Time above 80 °C:         0 s
CPU clock (test):         avg 1599 | min 1599 | max 1600 MHz
Throttling:               no signs of throttling (clock stable, change 0.0 %)
CPU load (test):          avg 96 % | max 100 %
RAM usage (test):         max 4883 MiB (63 %)
stress-ng performance:    cpu 658.9 | matrix 1458.5 | vm 100.6 bogo ops/s
Cooldown (60 s):          49 → 42 °C (-7 °C, 7.0 °C/min)
Drop after load stops:    49 → 45 °C in 6 s (-4 °C)
Slow cooldown:            45 → 42 °C in 54 s (-3 °C, 3.3 °C/min)
Clock after test:         1600 MHz
Return to idle (≤ 44 °C): after 35 s (idle before test 39 °C + 5)
```
- **CPU % and RAM are read directly from the node** (`/proc/stat` counters and `MemTotal`/`MemAvailable`
  from the probe), not from `kubectl top`. The line is therefore always current. The metrics server only
  refreshes its data now and then and the line used to lag by tens of seconds.
- **Throttling** is judged from the clock under full load (utilisation above 80 %): the average of the first
  and the last third of the measurements is compared. A drop of 10 % or more is flagged as a *suspicion*.
  It is an estimate from measured values, not proof.
- **Two cooldown phases:** the *drop after the load stops* is the fast fall right after the load ends (the
  difference between chip and heatsink, it mostly reflects the contact and the thermal paste) and the *slow
  cooldown* is the rest, when the whole heatsink with the fan cools down. Measurements are taken every 5 s,
  so times are accurate to about ±5 s.
- **Cooldown** shows how fast the CPU cools after the load ends and how long it takes to return to the idle
  temperature before the test (+5 °C). It is handy for comparing two runs of the same machine (e.g. before and
  after replacing the paste). Absolute numbers depend on the ambient temperature.
- Cooldown is measured also after a stop because of overheating and after a premature end. The summary is
  printed even after an interruption (`Ctrl+C`, `--stop`), just without the cooldown part.

## Comparing two tests (`--compare`)
Compares two result logs side by side, for example a test of the dell before and after cleaning the heatsink:

```bash
./stress.sh --compare dell-9020-sff-i7            # the two newest tests of the node (older = A, newer = B)
./stress.sh --compare log_before.log log_after.log     # two specific logs (name in the logs folder, or a path)
```
It shows the idle and the maximum/average temperature, the time above 80 °C, clock, throttling, `stress-ng`
performance, both cooldown steps and the return to the idle temperature. For every change that is big
enough (temperature 2 °C, performance 2 %, time above 80 °C 10 s, return to idle 5 s) it adds `(better)` or
`(worse)` and totals them at the end.
- It reads **logs written by this English build** (with cooldown and summary). Logs without cooldown data
  are read too, just without the cooldown and the idle temperature, and the tool warns about it. Logs of the
  Czech builds are not readable by this build.
- **Warnings when comparing:** a different node, a different idle temperature (surroundings), a different
  test length, a different load (other stressors), missing cooldown. Compare runs with the same settings and
  at a similar ambient temperature.
- It does not need `kubectl`, it only reads files. Change the logs folder with `--log-dir`.

## Stepped test and testing several nodes (since 1.5.0)
**What to test** and **which test** are chosen at startup (interactively by questions, or by options):

| Choice | Options | Flag |
|---|---|---|
| Scope | one node, all workers, the whole cluster, a list of nodes | `--node`, `--workers`, `--cluster`, `--nodes a,b` |
| Test type | classic (one load), stepped 25 → 50 → 75 → 100 % | `--profile classic\|stepped` |

```bash
./stress.sh --workers --profile stepped                 # workers one after another, stepped test (12 min per node)
./stress.sh --cluster --profile classic --time 5m       # the whole cluster, master last
./stress.sh --nodes dell-9020-sff-i7 --profile stepped --steps 25,50,100 --step-time 2m
./stress.sh --workers --include-master --profile stepped --yes
```

**Stepped test** (`--profile stepped`): every stage is a separate run of `stress-ng` with `--cpu-load`
according to the stage, all in one pod one after another. The default stages are 25/50/75/100 % for 3 minutes
each (`--step-time`), i.e. 12 minutes in total (`--steps` changes the stages, `--time` is ignored). It loads
only the CPU (no RAM and disk) and it is **always logged**. All stages use the same method (`matrixprod`) so
they are comparable. **The classic test is unchanged.** The summary at the end shows every stage separately:
the target and the actually measured utilisation, temperature, clock and performance, and throttling is judged
per stage:
```
Stages (target → measured):
  1/4   25 % → 26 %  max 47 °C | avg 45 °C | clock 3300 MHz | 700.1 bogo ops/s
  4/4  100 % → 99 %  max 84 °C | avg 82 °C | clock 3591 MHz | 2646.7 bogo ops/s
```
From this you can tell at which load the temperature or the clock start to misbehave.

**Several nodes** (`--workers`, `--cluster`, `--nodes`): the nodes are tested **one after another**, the master
always last.
- **The master has a CPU cap of 70 %** (in the stepped test the stages are 25/50/70, no RAM and disk). In the
  stepped test it **asks whether to keep it**; `--include-master` or `--yes` keeps it without a question,
  `--non-interactive` without them leaves the master out. The master is also the NFS server for the game
  servers, so test it outside of gaming hours.
- Before the start a plan is printed: the order, the time estimate and the services running on every node, and
  one confirmation is asked (`--yes` skips it, `--non-interactive` without `--yes` refuses the test).
- **The failure of one node does not stop the series** (overheating, premature end). Nodes that are not Ready
  are skipped. Only `Ctrl+C` or `--stop cluster` stops it (the remaining nodes are not tested, the summary is
  printed anyway).
- Exit code: 0 all good, 3 someone overheated, 1 an error or a premature end, 130 interrupted.
- **Logs:** every node has its own log (`<node>-<duration>s-<date>.log`) and the series has
  `logs/cluster-<date>.log` with the state of every node and a final table. `--compare <node>` keeps reading
  the node logs and also recognises stepped tests (it compares the temperature per stage; a classic test
  against a stepped one only with a warning).
- It can also run in the background (`--background`, one detached process for the whole series; `--status`,
  `--stop cluster`).

**Workers at once** (`--parallel`, since 1.6.0): every worker runs in its own subprocess, the tool only
controls them and shows one table in the terminal (redrawn in place in a terminal, otherwise printed once per
30 s):
```
dell-9020-sff-i7          ▶ Stage 3/4 (75 %)      CPU   74 %    71 °C   3591 MHz  left ~6 min
hp-g2-celeron             ▶ Stage 3/4 (75 %)      CPU   76 %    52 °C   1600 MHz  left ~6 min
```
- The total time is the longest worker, not the sum. Without `--parallel` it asks interactively (**at once /
  one after another**), otherwise it tests one after another (`--no-parallel` forces that). Running in parallel
  makes sense from two workers.
- **The master is never tested at the same time as the workers**: `--cluster --parallel` starts the workers at
  once and **the master alone after them** (CPU cap 70 %, in the stepped test 25/50/70, temperature limit
  **80 °C** instead of 85). The master is also the API server and the NFS server.
- **API guard:** every 5 s the response time of `kubectl get --raw /readyz` is measured. When it is slower than
  `--api-limit` (default 3 s) or fails **twice in a row**, the tool **stops all tests** (the subprocesses delete
  their pods) and exits with code 1. Without the API the temperature guard would not work.
- The summary is assembled from the node logs. The log of every node has a line `Concurrency: yes (N nodes at
  once)`, `--compare` warns about a different concurrency. The output of the subprocesses is in
  `logs/<node>-...console.txt`, the series log in `logs/cluster-<date>.log`.
- **Running in parallel raises the power draw from the socket and warms up the room**, so the results are not
  exactly comparable with a one-after-another test. A warning is printed before the start (check the circuit
  breaker and the power strips).
- `Ctrl+C` and `--stop cluster` gracefully end all subprocesses (pod cleanup) and the summary is printed anyway.

```
CLUSTER SUMMARY
Node                      State       max °C  over 80 °C throttling  cpu perf   return to idle
dell-9020-sff-i7          OK          84      1 min      no          2646.7     not reached
hp-g2-celeron             OK          49      0 s        no          676.3      42 s
hp-prodesk-400-g6 (master) OK         61      0 s        no          410.2      35 s
```


## How a test runs
1. Leftovers of earlier tests are removed (finished pods of this tool; running ones are never touched).
2. The `hw-info` pod runs briefly (unprivileged by default): CPU, threads, system, motherboard, RAM modules.
3. The `temp-probe` pod is started (busybox, `/sys` **read-only**) and it is verified that the node has a CPU
   temperature sensor. Without it the overheating protection would not work, so the tool asks (or refuses,
   with `--non-interactive` without `--allow-no-sensor`).
4. If the node is already **above the limit before** the test, the test does not start.
5. The `stress-test` pod and `stress-ng` are started.
6. A background thread every `--interval` seconds prints (and optionally saves to the log): the CPU and RAM
   utilisation from the probe, temperatures (CPU/GPU/NVMe/disks) and the average CPU clock in MHz. A clock
   drop at a high temperature means throttling. Every 3rd measurement (configurable) a line is added:
   `⏱️  Remaining 3 min 40 s (elapsed 1 min 20 s of 5 min, 27 %)`.
7. At the end (or on interruption) the pods are deleted.

## Logs: where to find what
Three different things in two places, always inside the project directory (it does not matter where you
start the tool from):

| Where | For whom | What is in it |
|---|---|---|
| `logs/` (visible) | the user | test results: hardware, ongoing measurements, remaining time, the reason for ending. Created only with `--log`. File `<node>-<duration>s-<date_time>.log` |
| `.logs/debug/` (hidden) | debugging | **a technical log of every run** (even without `--log`): all called `kubectl` commands with the return code and time, every measurement and decision of the temperature guard, all user-facing output and the full traceback on an error. One file per run: `<date>_<time>_<pid>.log` |
| `.logs/tests/<date>_<time>_<pid>/` (hidden) | debugging | history of pytest runs: `pytest.log` (results, errors, summary), `inprocess-debug.log`, `tool-runs/` (debug logs of the tool started from the integration tests) |

The history of the debug logs is complete, nothing is deleted automatically. The hidden folders have a dot
in their name (`ls -a`). On an error (exit code 1) the tool prints the path to the debug log. If the debug
log cannot be created (e.g. permissions), the tool keeps running without it.

Quick looks into the history:
```bash
ls -lt .logs/debug | head                       # the latest runs
grep -l "ERROR" .logs/debug/*.log               # which run went wrong
tail -n 60 "$(ls -t .logs/debug/*.log | head -1)"   # the end of the last run
```
The path to the hidden folder with the debug logs can be overridden with the `STRESS_TEST_DEBUG_DIR` variable.

## Safety features
- **Preflight (check before EVERY test, including `--quick`):**
  1. **A concurrent test of this tool on the same node** - always refused, cannot be turned off
     (another node at the same time is fine, the tool just announces it).
  2. **Node usage** (`kubectl top`, both CPU and RAM) - above `--max-busy-pct` (default 80 %) the test
     is refused. Bypass: `--allow-busy-node` (continues with a warning) or `--no-capacity-check`
     (turns the check off entirely). If `kubectl top` fails, it is just reported and things continue.
  3. **CPU temperature already BEFORE the load** - if the node is at or above `--max-temp` before the
     start, the test is refused, so it does not begin on a node that needs to cool down. Does not apply
     to a missing sensor (that is handled by `--allow-no-sensor`).
- **Stop on overheating:** the limit must be exceeded in 2 readings in a row. From 80 °C a warning appears in
  the output.
- **Master protection** (a node with the control-plane role): confirmation, CPU load at most 70 %, RAM and
  disk are not tested. Bypass: `--force` or `FORCE=1`.
- **Before the test it prints what is running on the node** (pods by namespace, `kube-system` is only
  counted) and asks for confirmation for foreign services (default no). `-y` confirms, `--non-interactive`
  without `-y` refuses the test. On the master it asks only once. Finished pods and pods of this tool are not
  counted.
- **The RAM test is computed from the node's `MemAvailable`** (read by the probe) and keeps a 512 MiB
  reserve. When that value is missing, the fallback from `kubectl top` is used. With almost no free memory the
  RAM test is turned off.
- **The load pod has a memory limit** (RAM test target + 1 GiB, 1.5 GiB without the RAM test) and only a small
  `request` (64 MiB). When memory runs short, the kernel kills this pod first, not foreign services.
- **`hw-info` runs without `privileged` and without `hostPath`** (default). It reads the system, the board and
  the CPU anyway, the RAM modules usually not (`dmidecode` needs privileged mode), and the output says so.
  `--hw-privileged` turns it on explicitly (and the tool announces it), `--no-hw` skips the pod entirely.
- **Pinned images** (`ubuntu:24.04`, `busybox:1.36`), pods without an API token
  (`automountServiceAccountToken: false`) and without `allowPrivilegeEscalation`.
- **File permissions:** debug logs, results, background output and the registry have `0600`, newly created
  folders `0700` (existing folders are not changed).
- **`activeDeadlineSeconds`** on all pods (test duration + 10 min): Kubernetes ends them by itself even if the
  tool disappears (dropped SSH, kill -9).
- **Signals:** Ctrl+C, `SIGTERM` and `SIGHUP` (closed terminal) start the pod cleanup.
- It creates only 3 temporary pods in the current namespace and changes nothing else. **Every run has its own
  names with a random suffix** (`stress-test-a1b2c3`, `temp-probe-a1b2c3`, `hw-info-a1b2c3`) and the label
  `app=stress-test-tool`, so concurrent tests do not delete each other.
- **Concurrent tests:** on different nodes at once is fine (the tool only announces it), on the **same** node
  the second test is refused (the results would be distorted).
- **A premature end is not reported as a success.** A regular end is recognised by the final message of
  stress-ng (`successful run completed`). When the log stream ends earlier and the message is missing, the tool
  writes after how long and in what state the pod ended, and returns code 4.
- The debug log must never crash the tool (on a write error it is simply skipped).

If something is left over after an unexpected crash:
```bash
kubectl delete pod stress-test temp-probe hw-info --now
```

## Exit codes
| Code | Meaning |
|---|---|
| 0 | the test finished fine (or was declined on the master) |
| 1 | an error (node not Ready, pod did not start, kubectl failed...) |
| 3 | the test was stopped because of overheating |
| 4 | the test ended **prematurely** (the load pod disappeared or was terminated from outside) |
| 130 | interrupted by the user / a signal |

## Project structure
```
stress_test/
  models.py     data classes and constants (StressConfig, NodeInfo, ...)
  parsing.py    pure functions for kubectl and probe output (easy to test)
  manifests.py  pod definitions as dictionaries, probe and hardware scripts
  kube.py       thin wrapper over kubectl (class Kubectl)
  monitor.py    OverheatGuard, line format and the Monitor thread
  runner.py     class StressRunner: the whole course of a test and the cleanup
  cli.py        argparse, questions, node workload check, main() with the debug log
  background.py running in the background (process detaching), registry, --status and --stop
  summary.py    summary at the end of a test (temperatures, clock, throttling, cooldown, stress-ng performance)
  logparse.py   reading result logs back into data
  compare.py    comparing two tests (--compare)
  series.py     testing several nodes one after another, plan, summary and series log
  parallel.py   workers at once (subprocesses), live table, API response guard
  paths.py      where logs are stored (logs/, .logs/debug/, .logs/tests/)
  debuglog.py   the hidden technical log of every run
conftest.py     writes the history of pytest runs to .logs/tests/
tests/
  test_parsing.py, test_models.py, test_monitor.py, test_summary.py, test_compare.py,
  test_series.py, test_parallel.py, test_background.py,
  test_paths_and_debuglog.py                          unit tests
  fake_kubectl.py + test_integration.py               the whole flow against a fake kubectl
```

## Tests
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```
Every pytest run is written to `.logs/tests/<date>_<time>_<pid>/` (see above), so you can find out afterwards
what failed and what the tool did during the tests. The integration tests do not need a cluster: instead of
`kubectl` they use `tests/fake_kubectl.py` (through the `KUBECTL` variable) and verify a normal finish, a stop
on overheating, a missing sensor, the master protection, several nodes, running in parallel and the refusal to
start on an already hot node.

## Possible improvements
- output to CSV/JSON and a graph of temperature and clock over time (matplotlib)
- the official Kubernetes client instead of calling `kubectl`
- a dedicated namespace for the pods (`--namespace`)
