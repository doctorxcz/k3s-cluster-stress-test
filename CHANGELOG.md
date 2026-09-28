# Changelog

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
