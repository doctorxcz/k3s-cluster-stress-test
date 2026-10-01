# Changelog

## 1.16.0 - 2026-10-01
- **GPU test (NVIDIA, 2026-10-01):** `--profile gpu` (main menu `2 GPU`) burns one NVIDIA GPU with gpu-burn in a pod with `runtimeClassName: nvidia`
  and `nvidia.com/gpu: 1` (not privileged, no hostPath) and reads `nvidia-smi` every interval: temperature, utilization, SM clock, power
  (`N/A` on cards that do not report it, e.g. Quadro P620), throttle reasons. GPU stop temperature selectable (`--gpu-max-temp`, asked in
  the dialog, default 80 °C; a warning 5 °C below it); the CPU limit stays active too. The GPU pod stays idle after the load, so the
  cooldown measures the GPU cooling. If `nvidia-smi` stops answering 3 times in a row the test stops (the GPU cannot be watched).
  Missing driver / toolkit / device plugin is reported with a hint. `--gpu-mem-pct`, `--gpu-double`, `--gpu-image` (an image with a ready
  `gpu_burn` skips the build). FULL self-test gets a GPU phase for nodes that offer `nvidia.com/gpu` (never together with the CPU load).
  Menu `2 GPU` opens a settings dialog (length, stop temperature, GPU memory share, single/double precision, cooldown, image, extras) and a
  summary before the start. The live frame shows CPU temperature, RAM, GPU temperature, GPU clock, VRAM used/total, GPU load, power and
  throttling; GPU model, driver, memory, limits and PCIe are read once before the load (`GPU INFO`, also in the log). The self-test
  accepts `--nodes A,B` (only these nodes; a single node skips the network matrix).
  **GPU scan:** menu `2 GPU` (and `--list-gpus`) first scans every node (Kubernetes `nvidia.com/gpu` + `lspci` in a short unprivileged pod) and shows
  a table: ✅ OK = usable dedicated NVIDIA GPU, ⚠️ ERROR = card found but not offered to Kubernetes (driver / toolkit / plugin), ❌ none = no dedicated
  GPU, ❓ `? ERROR` = the scan failed (cannot tell). Only ✅ nodes can be chosen: one node is picked automatically, several = numbers / `a` (all);
  several nodes are tested one after another (`--nodes a,b --profile gpu`).
  **GPU fan:** the GPU fan speed is shown in % (`nvidia-smi fan.speed`, `GPU FAN` in the GPU frame, log field `fan=45%`, CSV `gpu_fan_pct`); the CPU fan
  is not read (the nodes do not expose it through hwmon). CPU tests show the model and cores / threads under the title (`Intel Core i7-4790S · 4c/8t @ 3.2 GHz`);
  the GPU test has its own live frame (card, bars, VRAM, throttling).
  **Several GPU nodes:** one table of all nodes (✅ done · ▶ running with progress and live temperature · ⏳ waiting · ❌ failed) on top of the live frame, printed
  between the nodes in plain output and as the final table (replaces the CPU cluster summary). `--gpu-prepull` (asked in the menu) pulls the CUDA image on all chosen
  nodes first. The settings show both stop temperatures; `--gpu-*` without `--profile gpu` is reported as ignored. No CPU temperature question for the GPU, disk and
  network tests. Docs: setup guide for a GPU node in HELPDESK.md, `deploy/*.yaml`. The menu was renumbered: 1 CPU, 2 GPU, 3 DISK, 4 NET, 5 QUICK, 6 FULL, 7 DATA, 8 ADMIN.
  The log has ` | GPU: ...` fields, CSV/JSON have `gpu_*` columns; `--compare` and the baseline do not show GPU data yet.
- **Every screen adapts to the terminal width (2026-09-30 23:40):** three modes - compact (< 60 columns), normal (60-99), wide (100-200).
  Submenus and questions (panels), long prompts, the help, settings, TEST SETTINGS, test / cluster / network / disk summaries, `--status`,
  `--list-nodes`, `--compare`, the network matrix, the background box, the FULL self-test screens and `--help` are laid out for the
  window: long sentences wrapped with a hanging indent (URLs / paths cut with `…`, never split), key/value blocks in two columns (wide) or
  label-above-value (compact), tables drop or shorten columns by priority. The menu goes up to 200 columns. Saved logs / JSON / exports keep
  their fixed format. `STRESS_TEST_COLUMNS=NN` forces a width (also without a terminal).
- **Live screens:** in a terminal a running test is one framed block redrawn in place (no scrolling): node, stage, progress bar
  (preparation / load / cooldown with snowflakes), CPU, RAM, temperature, clock, power, ping and the last events. `--parallel`
  shows one table with every node (its own bar per node). `--net-matrix` fills its grid live. Plain text is kept for pipes,
  `NO_COLOR` and `STRESS_NO_LIVE=1`.
- **Start mode (`--parallel`):** `--start-mode rolling` (default) - every node starts its load as soon as it is ready and runs the
  full time from its own start (a slow node does not hold the others back). `--start-mode sync` - every node prepares, says READY and
  the load starts on all nodes at the same moment (`--ready-timeout SEC`, default 900, leaves out a node that is late).
- **Network test:** the UDP job no longer stops the test when a firewall blocks UDP (it is reported as skipped with the reason);
  `--net-extra no-udp` leaves it out. Network and disk tests have no cooldown by default and a short own summary.
- **FULL self-test** (main menu `6`, or `--self-test`): network matrix, disks + SMART, CPU (stepped) and RAM for the whole cluster
  with cooling pauses between the phases and one report (log + JSON + a framed verdict). Two confirmations; the master is optional.
  `--self-test-level quick|standard|thorough`, `--self-test-ack`.
- **Day folders:** results, debug logs and pytest runs are written into a folder per day (`logs/2026-09-30/`, `.logs/debug/2026-09-30/`,
  `.logs/tests/2026-09-30/`); a run uses today's folder if it exists and creates it when it does not. With `--log-dir DIR` the day folder
  is created inside DIR. `--compare`, `--export-log` and `--set-baseline` search all day folders; baselines stay in `logs/baselines/`.
  `--migrate-logs` moves old flat files into day folders by their date.
- The repo address is shown in the top border of the menu and the frames.

## 1.15.0
- **Main menu:** `./stress.sh` typed without any option in a terminal shows a menu (CPU load, Disk, Network, Quick test,
  Results, Management) with the cluster state in the header. Every entry only builds the usual options (printed as
  `▶ ./stress.sh --profile disk --smart`) and runs the normal program, which asks the rest; afterwards you are back in the menu
  (`0` = quit). With any option, on a pipe/cron, or with `STRESS_NO_MENU=1` nothing changes.
- The network test now asks for the network (nodes' / pod) and the extra jobs when run interactively.

## 1.14.0
More network tools.
- **`--net-matrix`:** ping + iperf3 between EVERY ordered pair of nodes (all Ready nodes, or `--nodes a,b,c`), printed as two tables
  (Mbit/s and ping ms; row = client, column = server) with plain-language findings (slow pair, weak node, asymmetric directions,
  loss, NIC below 1 Gb/s, half duplex). One helper pod per node (iperf3 installed once, commands via exec), `--net-time` per test
  (default 5 s), the master capped at 300 Mbit/s, refuses while another test runs, asks before it loads the network (`--yes`).
  Saves `logs/net-matrix-<date>.log` and `.json`.
- **`--net-watch [NODE]` (any test):** the probe also pings another node (default the master; a worker when the master is tested)
  once per reading. The log line ends with `| Ping: 0.42 ms` / `| Ping: lost`, the summary shows latency under load against idle and
  says when the load disturbs the network.
- **`--net-extra LIST` (network test):** optional jobs: `dns` (CoreDNS and external lookups), `internet` (ping 1.1.1.1 + a bounded
  download from the Ubuntu mirror), `mtr` (hops, loss and latency to the peer), `service` (TCP through a Kubernetes Service =
  kube-proxy path, needs `--net-mode pod`), or `all`. Wi-Fi interfaces report their signal (weak below -70 dBm).

## 1.13.0
- **Network test (`--profile net`, menu item 5):** the tested node runs an iperf3 client and ping against a peer node
  (an iperf3 server pod on the peer; `--net-peer NODE`, default: another Ready worker, the master only as the last option).
  Jobs: NIC link speed and duplex, ping (50 x 0.2 s: avg/min/max/jitter/loss), path MTU probe, TCP upload, TCP download,
  TCP with 4 streams, UDP at 100 Mbit/s (jitter, loss), and the NIC error/drop counters before vs after.
  `--net-time` (5-60 s per iperf3 test, default 10), `--net-mode host|pod` (the nodes' real network, or the pod network
  = what workloads really use through flannel/CNI), `--net-rate MBIT` (TCP cap; 300 Mbit/s automatically when the master
  is on either end). The summary explains problems in plain words (link negotiated 100 Mb/s, half duplex, packet loss,
  asymmetric up/down, single-stream limit, retransmits, UDP loss). Results go to the log, the JSON and the baseline check
  (TCP -10 %, ping +50 %, new loss).

## 1.12.0
- **CPU power (RAPL):** the probe reads the Intel RAPL energy counter (no privileged mode needed) and every
  measurement line ends with `| Power: 17.3 W`. The log header has `Power limits: PL1 25 W, PL2 51 W`, the summary
  shows average/max power next to the limits and says when the average sits at PL1 (the clock is then held
  down by the power limit, not by heat). The baseline check warns when power draw is 10 % higher. CSV has `power_w`.
  Machines without RAPL (AMD, ARM) simply show nothing.
- **Disk health (`--smart`):** preflight step with a PRIVILEGED pod (`smartctl -j`): health verdict, temperature,
  power-on hours, reallocated/pending sectors, NVMe wear and errors. A failed health check refuses the test
  (`--allow-bad-disk` overrides); warnings only print. Results go to the log ("DISK HEALTH (SMART)") and the JSON.
- **Disk benchmark (`--profile disk`, menu item 4):** fio in the pod (file in an emptyDir on the node's disk,
  direct I/O): sequential read/write (1M) and random read/write (4k, QD32), MB/s, IOPS, average and p99 latency.
  `--disk-size MiB` (128-8192, default 1024), `--disk-job-time` (5-120 s, default 15 s). Always logged, exported,
  and compared with the baseline (throughput -10 %, p99 latency +25 %). The master runs read jobs only, 256 MiB.

## 1.11.0
- **Spike test** (`--profile spike`, menu item 3): repeating jump between a low load (10 %) and a target
  (`--spike-target 25/50/75/100`), phases set with `--spike-low-time` / `--spike-high-time` (default 5 s each,
  1-300 s); `--time` is rounded to whole cycles. CPU only, the master is capped at 70 %.
  `--compare` and the log reader understand spike logs (stage targets are read from `▶ Low` / `▶ Spike!`).
- **Export:** every logged test also writes `<log>.json` (metadata, computed statistics, all measurements,
  stage data) next to the log. `--export json|csv|both|none` picks the format (csv = measurements only).
  `--export-log LOG|NODE` creates the export of older logs.
- **Baseline and regression:** `--set-baseline LOG|NODE` saves a finished test as the "golden" result of its
  node (`logs/baselines/<node>.json`). Every later test of that node is checked against it at the end:
  max temperature (+5 °C), average clock (-5 %), performance (-5 %), new throttling -> verdict OK or REGRESSION
  (also stored in the JSON). Tests with a different profile or stages are reported as not comparable.
  `--no-baseline-check` turns the check off.

## 1.10.0
Preflight check of node usage before the test (adds to the already existing check for a concurrent test and temperature).
- **Node usage:** before EVERY test (including `--quick`) `kubectl top` checks the node's CPU and RAM.
  Above `--max-busy-pct` (default 80 %) the test is refused. `--allow-busy-node` continues anyway
  (with a warning), `--no-capacity-check` turns the check off completely. If `kubectl top` fails, it is
  just reported and things continue.
- **Temperature before the test** (the check already existed, now it is documented as a preflight step and
  moved before hardware detection, so that an overheated node does not needlessly wait on apt/dmidecode):
  if the node is already at or above `--max-temp` before the load starts, the test is refused.
- `--help` has a new "preflight" section with an overview of all three checks and how to bypass them.

## 1.9.0
Scheduling a start and informational switches.
- **`--schedule TIME`:** start the test later - the time `HH:MM` (the nearest occurrence, today/tomorrow) or
  a duration from now (`30m`, `2h`). Can be combined with `-b`/`--background` (keeps running even after
  closing the terminal).
- **`--dry-run`:** just show the test settings (node, parameters) and exit without touching the cluster.
- **`--list-nodes`:** list the cluster's nodes (role master/worker, state Ready/NotReady) and exit.

## 1.8.0
Quick test with a single switch.
- **`--quick` / `-q`:** asks only for the node and starts the test right away without further questions - 10
  minutes, CPU 100 %, no RAM or disk, limit 85 °C, cooldown 1 min, the result is logged with the note
  `quick`. Combinable with `--node` (no question), `--workers`/`--cluster`/`--nodes` (once for all nodes,
  asks only about scope if not given), and other switches, which always take priority
  (`--quick --time 2m --ram-pct 50` etc.). Prints a summary of what is being started before it begins.

## 1.7.0
Clearer help and a cooldown choice in the questions. The tests themselves are unchanged.
- **Cooldown question:** interactively offers 1 min (default) / 3 min / 5 min / 10 min (max), a custom
  duration, or off. For a multi-node test asked once for all of them. A given `--cooldown` skips the
  question, `--non-interactive` takes 1 min.
- **`--help` grouped:** what to test, what kind of test, safety, background run and management, logs,
  other; examples, language choice and environment variables at the end.
- The root `stress.sh` runs the Czech version by default (`--eng` or `STRESS_LANG=ENG` for English).

## 1.6.1a (English build)
An extra build, not part of the normal version line. **Identical in function to 1.6.0** — no new features,
no behaviour changes apart from the language.
- **Everything is in English:** all messages, questions, `--help` texts, the content of result logs and the
  debug log, README, this changelog (including the older entries), code comments and docstrings, and the
  whole test suite.
- **Questions are answered `y`/`n`** (`yes`/`no`), no longer `a`/`n`.
- **Result logs are in English** (`Node:`, `Started:`, `Test duration:`, `Temp:`, `Clock:`, `[cooldown]`,
  `TEST SUMMARY`, ...). The log reader of this build reads logs written by this build; logs of the Czech
  builds (1.6.0 and older) are not readable by it.
- Status names in the cluster table: `OK`, `OVERHEATED`, `PREMATURE`, `ERROR`, `INTERRUPTED`, `SKIPPED`.
- Options, exit codes and log/file naming (`<node>-<duration>s-<date>.log`, `cluster-<date>.log`) are
  unchanged.

## 1.6.0
Workers at once with master protection. Testing one after another and the classic test are unchanged.
- **`--parallel`:** the workers are tested at once, each in a subprocess (own pods, log and cleanup). One
  live table instead of intermixed output, the total time is the longest worker. Without the flag it asks
  interactively (at once / one after another), `--no-parallel` forces testing one after another.
- **The master never at the same time as the workers:** always after them and alone, with a CPU cap of 70 %
  (stepped test 25/50/70) and a **lower temperature limit of 80 °C** (also applies to testing the master one
  after another and alone).
- **API response guard:** `kubectl get --raw /readyz` every 5 s; slower than `--api-limit` (3 s) or an outage
  twice in a row stops all tests, the pods are deleted, code 1.
- A node log has a line `Concurrency: yes (N nodes at once)`, `--compare` and the log reader know it and warn.
- Node results in parallel mode are assembled from their logs (the reader learned the performance per stage).
- The fake kubectl in the tests keeps its state separately for every run.

## 1.5.0
Stepped test and testing several nodes one after another. The classic test is unchanged.
- **Stepped test** (`--profile stepped`): stages 25/50/75/100 % of 3 minutes each (12 min in total),
  `--steps`, `--step-time`. One pod, stages one after another, a uniform method (`matrixprod`). CPU only,
  always logged. Summary per stage (target and measured utilisation, temperature, clock, performance,
  throttling per stage). The stepped test is "completed" only when all stages have finished (an interrupted
  third stage is a premature end).
- **Several nodes one after another:** `--workers`, `--cluster`, `--nodes`, the master always last with a CPU
  cap of 70 % (stepped test 25/50/70, a question whether to keep the master; `--include-master`, `--yes`).
  An overview and one confirmation before the start, the failure of one node does not stop the series, nodes
  without Ready are skipped, a check that no other test runs on the nodes. The series log
  `cluster-<date>.log` and a final table. Also in the background with `--stop`.
- When started without `--node` it asks **what to test** (one node / workers / the whole cluster) and **which
  test**.
- `--compare` and the log reader know the stepped test (temperature per stage, a warning about different
  profiles).
- A measurement is assigned to a stage by the state at the start of the measurement (before, a sample on the
  boundary of stages could be assigned wrongly).
- Width of the labels in the test settings (`Cooldown after test:` used to run into the value).

## 1.4.1
- **Two cooldown phases in the summary:** the *drop after the load stops* (from the last measurement under load
  to the first one after it) and the *slow cooldown* (the rest, °C/min). The drop shows the chip/heatsink
  temperature difference, the slow cooldown the heatsink with the fan.
- **`--compare`:** compares two tests from logs (two logs, or a node name = the two newest). An A vs. B table,
  changes with a rating (better/worse), warnings about incomparable tests. Reads logs from version 1.4.0,
  older ones only partly.
- New modules `logparse.py` (reading logs) and `compare.py`.

## 1.4.0
Summary at the end of a test and a live CPU/RAM line.
- **The measurement line** takes the CPU utilisation from the `/proc/stat` counters and RAM from
  `MemTotal`/`MemAvailable` (probe), not from `kubectl top`. It used to lag by tens of seconds because the
  metrics server refreshes its data only now and then.
- **Cooldown after the test:** after `stress-ng` ends temperature and clock are measured for another 60 s
  (`--cooldown`, `0` = off). Cooldown is measured also after overheating and a premature end, `Ctrl+C` and
  `--stop` skip it.
- **Test summary** in the terminal and in the log: temperatures (min, avg, max, idle before the test), time
  above 80 °C, clock, **suspected throttling**, CPU utilisation, RAM, `stress-ng` performance (bogo ops/s),
  cooldown and the return to the idle temperature. The summary is printed even after an interruption.
- After the temperature limit is exceeded the measurement continues (before, the monitor thread ended).

## 1.3.0
Safety and a more precise RAM test.
- `hw-info` runs **without `privileged` and without `hostPath`** by default. Options `--hw-privileged`
  (explicit turning on, with a warning) and `--no-hw` (no hardware detection). Listing RAM modules without
  privileged mode may not work, the tool says so.
- Before the test it prints what is running on the node and asks for confirmation for foreign services (`-y`
  skips it).
- The RAM test target is taken from the node's `MemAvailable` (probe), a 512 MiB reserve, a fallback from
  `kubectl top`.
- The load pod has a **memory limit** and a small `request`, so that the OOM killer kills it first.
- Pinned images `ubuntu:24.04` and `busybox:1.36`, pods without an API token and without privilege escalation.
- Logs, background output and the registry run with permissions `0600`, new folders `0700`.

## 1.2.0
New: running in the background.
- The question `Run in the background? (y/n)` (default no) and the option `-b/--background`. The test detaches
  from the terminal (survives closing the window/SSH), **logging turns on automatically**.
- Results in `logs/*.log`, the whole output in `logs/*.console.txt` (live via `tail -f`).
- `--status` (running tests and the tool's pods) and `--stop ID|NODE|PID` (graceful end with pod cleanup).
- Checks before detaching (a concurrent test on the same node) are done in the terminal.

## 1.1.1
Fix: concurrent runs disturbed each other and a premature end looked like a success.
- Pods have unique names per run (`stress-test-<id>`), a `run-id` label; the cleanup deletes only its own pods.
  Before, all tests had the same names and the start of one test deleted the pods of another.
- Leftovers are cleaned only for finished/failed pods of the tool, running ones are never deleted.
- A second test on the same node is refused, on another node only a notice.
- A test whose `stress-ng` did not end with a final message before the time was up is reported as
  **ended prematurely** (exit code 4) instead of "Test completed". `stress-ng` with an error = code 1.

## 1.1.0
- a hidden debug log of every run (`.logs/debug/`), pytest history (`.logs/tests/`)
- test results into `logs/` in the project directory
- duration as `30s`, `5m`, `1h30m`, a summary of the settings, time to the end

## 1.0.0
- rewrite of the script `stress-node-v5.sh` to Python
