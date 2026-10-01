<p align="center">
  <img src="icons/k3s-cluster-stress-test-icon-256.png" width="128" height="128" alt="Kubernetes stress test icon">
</p>

# Kubernetes stress test (Python)

> **Version 1.17.0 (English)** (cluster dashboard with graphs, live status of the running tests, scheduling, height-aware screens, GPU test, FULL self-test - see
> [`CHANGELOG.md`](CHANGELOG.md)). New here? Start with [`HELPDESK.md`](HELPDESK.md) — a step-by-step
> installation and troubleshooting guide. **What does it look like?** See the [Gallery](#gallery---what-every-screen-looks-like). Quick start: `./stress.sh --list-nodes`, then
> `./stress.sh --node <worker> --time 60 --cpu-load 50` (a safe first test) or just `./stress.sh` for the menu.

Load test of a node in a k3s/Kubernetes cluster. It starts `stress-ng` in a pod directly on the chosen
node, measures load, temperatures and CPU clock during the test and **stops the test by itself when the node
overheats**. It is a rewrite of the original script `stress-node-v5.sh` in Python (no external
dependencies, only the standard library and `kubectl`).

## Main menu
Run `./stress.sh` with no options in a terminal:
```
┏━━━━━━━━━━━━━━━━━━━━━━━━━ github.com/doctorxcz/k3s-cluster-stress-test ━┓
┃ ◖●▲●◗ K3S·STRESS                                               v1.17.0 ┃
┃ › cluster: 5 nodes (4 workers + 1 master), all Ready                   ┃
┃ 🔥 last measured max 71 °C (worker-1)                                  ┃
┃ 🔁 last: ./stress.sh --profile stepped · 2026-10-01 10:40              ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ TESTS                                                                  ┃
┃ 1 ▸ ⚡ CPU       classic · stepped · spike                             ┃
┃ 2 ▸ 🎮 GPU       NVIDIA burn · temperature, clocks                     ┃
┃ 3 ▸ 💾 DISK      fio · SMART                                           ┃
┃ 4 ▸ 📡 NET       one node · matrix of all nodes                        ┃
┃ 5 ▸ 🚀 QUICK     one node, 10 min, few questions                       ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ CLUSTER                                                                ┃
┃ 6 ▸ 🧪 FULL      self-test of the whole cluster · one report           ┃
┃ 7 ▸ 📊 DATA      compare · baseline · export                           ┃
┃ 8 ▸ 🔧 ADMIN     what runs · stop · nodes                              ┃
┃ 9 ▸ 🕒 SCHEDULE  planned tests · plan · cancel                         ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ 🔁 R ▸ repeat   🧰 S ▸ settings   ⏰ T ▸ start time   📺 D ▸ dashboard ┃
┃ 🔭 W ▸ watch   ❓ ? ▸ help   🚪 Q ▸ quit                               ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```
Each entry (some open a submenu) only picks WHAT to do; the program then asks the usual questions (node, duration, ...). The chosen
options are printed as the equivalent command (`▶ ./stress.sh --profile stepped ...`), so you learn the flags, and after the test you
are back in the menu. The menu is skipped when you give any option, when the input is not a terminal, or with `STRESS_NO_MENU=1`.

| Key | What it does |
|---|---|
| `1`-`9` | open the entry (CPU, GPU, DISK, NET, QUICK, FULL, DATA, ADMIN, SCHEDULE) |
| 📺 **D** | the live cluster dashboard (every node at a glance, graphs, pods, logs) |
| 🔭 **W** | the live status of the running and planned tests (refreshed every second, details by the kind of the test) |
| **T** | start time of the NEXT test: in a time (`2h`), at a clock time (`22:30`) or on a date (`2026-10-03 02:00`); the test you pick afterwards is planned |
| `R` | repeat the last action exactly as it ran (shown in the header line `last:`) |
| `S` | settings: default node, temperature limit, live frames on/off (kept in `.logs/menu-state.json`) |
| `?` | help: what every entry does, in pages that fit a small terminal, with the links to this README and `HELPDESK.md` |
| `Q` (or `0`) | quit |

**The layout follows the height too:** a window of 40 lines or more (even a narrow one, e.g. 60 columns by 300 lines) opens everything that fits instead of scrolling - all test cards in the live status, the node, pods, cluster totals and the other nodes in the dashboard, tips and nodes in the menu, up to 60 events in a running test, a block per node under the parallel table. `STRESS_TEST_LINES` fixes the height (tests).

**The whole program follows the width of your terminal** (measured every time something is printed, so a resized window is fine). There are
three modes: **compact** up to 59 columns, **normal** 60-99 columns (the layout above) and **wide** from 100 columns up to 200 columns.
- *Menu:* compact = icons and names only; wide = two columns - TESTS with longer descriptions and tips on the left, CLUSTER and a NODES list
  (role, state, the last measured temperature of every node) on the right.
- *Submenus and questions* (CPU, DISK, NET, DATA, ADMIN, SETTINGS, every question with its choices): compact puts the hint under the name,
  normal puts it beside, wide shows two columns of choices. A long question is printed wrapped above the prompt, so the input never wraps.
- *Help `?`*: pages that fit the window; two columns in wide mode.
- *Test settings, summaries, `--status`, `--list-nodes`, `--compare`, the cluster summary, the network matrix and the FULL self-test
  screens:* long sentences are wrapped at word boundaries with a hanging indent (URLs and paths are never split, they are cut with `…`),
  `Label:  value` blocks become two columns in wide mode and a label-above-value layout in compact mode, tables drop or shorten the least
  important columns when the window is narrow (and show extra columns such as average temperature, clock and power when it is wide), the
  matrix splits its columns into blocks. `--help` is wrapped the same way.
- The saved files (`.log`, `.json`, exports) always keep their fixed format and never contain colours; only what is shown on the screen adapts.
  Without a terminal (pipe, file) the plain 76-column layout is printed. For tests and screen recordings `STRESS_TEST_COLUMNS=NN` forces a
  width (it also works without a terminal).

The header also shows a yellow line while a test of the tool is running in the cluster, and the hottest temperature seen in the newest
result logs (from the logs, not measured live).

## Live screens (terminal only)
In a terminal a running test is **one framed block that is redrawn in place** - nothing scrolls:
- one node: a progress bar (yellow `▰▱` while the node is prepared, green `█░` during the load, snowflakes `❄️` during the cooldown),
  the stage, the time left, CPU, RAM, temperature, clock, power and ping, and the last events; under the title the CPU model with
  cores / threads (`Intel Core i7-4790S · 4c/8t @ 3.2 GHz`);
- GPU test: its own frame with the card, bars, VRAM, throttling and (for several nodes) the table of all nodes - see the GPU section;
- `--parallel`: one table with a line per node (its own bar, state, CPU, temperature, clock, RAM);
- `--net-matrix`: the node x node grid fills in as the pairs finish.

The preparation of a node (checks, probe pod, hardware, pod start, installing the tool) is shown as a bar with the current step.
The frames adapt to the window (same three width modes as everywhere: compact < 60, normal 60-99, wide 100-200 columns): a wide window gives wider frames and more columns, a narrow one fewer columns (a node line keeps the
name, state and temperature even in a 45-column window) and fewer event rows. **Resizing the window while a test runs is handled**: the
frame is cleared and drawn again from the top at the new size, so no pieces of the old one stay on the screen. Without a terminal
(pipe, file, `NO_COLOR`) or with `STRESS_NO_LIVE=1` (or **S** ▸ live frames off) the original line-by-line text is printed; the log
files never contain colours.

## Cluster dashboard (key `D` in the menu, `--dashboard`)
A live overview of every node in the retro GUI (a small k9s): `./stress.sh --dashboard` or the key `D` in the menu; `--nodes A,B` limits it to some nodes. It adapts to the window
(and is redrawn when you resize it) in **three widths**:

| Width | Shows |
|---|---|
| **up to 90 columns** | a compact table: node, CPU, temperature, RAM, GPU, pods (Enter = detail page of the node) |
| **91 - 160** | more columns (role, clock, power, load, network ↓↑, ping to the other nodes, GPU model / °C / load / VRAM, state, last test) + a **detail block of the selected node** (Tab: overview / pods / events) |
| **over 160** | the whole screen with **everything the tool can find**: the table gains history graphs (CPU, temperature), IP, metrics-server use, kernel, k3s, image count, requests, restarts, uptime and the last test; below it big panels (2 columns, **3 from 230 columns**): **hardware** of the selected node (vendor / model / BIOS, CPU governor and turbo, clock and temperature of every core, thermal zones, throttle events, load, tasks, open files, pressure stall, RAM breakdown, swap, disks with read / write speed, NICs with MTU / duplex / errors / drops, ping, full GPU), **Kubernetes** (all conditions, cordon, taints, labels, OS / kernel / runtime / version, addresses, node age, cached images, CPU and RAM allocatable vs. requested vs. limits vs. used, pods x of max, last test and baseline), **pods** of the node (readiness, reason such as CrashLoopBackOff, restarts, age, QoS), **cluster** (nodes, CPU / RAM / GPU / pods capacity, requested vs. allocatable, namespaces by pods, services by type, ingress, configmaps, PVC / PV and GiB, jobs, cronjobs, workloads, API latency, warnings by reason), **problems** (pods that are not fine, most restarts, latest warnings) and **tests** (running, planned, last result of every node) |

Keys (capital letters work too): `↑↓` `PgUp` `PgDn` `Home` `End` select · `Tab` next tab of the detail (overview / pods / events - in every width) ·
`Enter` detail page (narrow window) · `o` **sort** (temperature, CPU, RAM, pods, GPU) · `/` **filter** by a part of the node name (Enter keeps it, Esc clears it) ·
`f` show **all / problems / workers / gpu** · `v` **view** of the table (overview, temperatures, network, disks, GPU - each with its own columns) · `c` clear all of it ·
`g` **graphs** (below) · `l` **pods** of the node, `Enter` = the end of the log of the pod (read only) · `e` **save the screen** (below) · `?` all keys ·
`+` `-` refresh interval (**0.5** / **1** / 2 / 5 / 10 / 30 s) · `p` pause · `r` refresh · `q` back (closes a page first).

### Graphs (key `g` in the dashboard)
The history of the selected node, one graph per quantity - CPU, temperature (with the 80 °C limit, red above it), RAM, clock, power, GPU temperature and load, network.
Above every graph the name and `now / min / max / avg` in the colour of its line. The default is **blocks** (easy to read), `b` switches to **Braille dots**
(four times finer). `[` `]` change the window (1 min · 5 min · 20 min · 1 h · all), `←` `→` move back and forward in time (`End` = now), `,` `.` move a **cursor** that
shows the values of every quantity at one moment, `a` fills the area, `x` draws **all lines over each other in one graph** (each in the scale of its own range -
for seeing what rises or falls at the same time; the numbers are in the legend), `↑↓` change the node. A short peak is never lost when old data are averaged
(the min-max band is kept). The dashboard keeps up to an hour of samples at 1 s (more at a slower refresh).

### Saved screens and graph data (`scr/`)
`e` (dashboard and live status) saves the screen exactly as it is drawn as a **text file** `scr/dashboard-<date>_<time>-<columns>x<rows>.txt` (no colour codes).
In the graphs `t` saves **all the graphs as a text report** (fixed width 100, with a table of the numbers) and `s` saves **the data as CSV and JSON**, both in
`scr/graphs/`. Files are private (owner only). Read them on a server with `less`, `cat` or `batcat` (text); `column -s, -t < file.csv | less -S`, `csvlook` (`sudo apt install csvkit`)
or `vd file.csv` (visidata) for CSV; `jq . file.json | less` (`sudo apt install jq`) or `python3 -m json.tool file.json` for JSON.

**Read-only and light:** the data come from the Kubernetes API (nodes, pods, events) and from one tiny unprivileged probe pod per node (`/sys` read-only, label
`app=stress-test-dashboard`, so it never blocks a test); a GPU node gets a second probe that only runs `nvidia-smi` (it does **not** request `nvidia.com/gpu`, so
it never takes the GPU away from a test). The probes exist only while the dashboard is open and are removed when you leave (they also end by themselves after
an hour). Needs a terminal.

## Live status of the running tests (key `W`, `--status --live`)
One screen for everything that runs or waits - tests started in the background **and in the foreground** (also from another terminal), the FULL self-test and the
planned ones - refreshed **every second** (`+` `-`: 0.5 - 30 s). It reads only local files (the registry `.logs/running/` and the result logs), so it asks the cluster
for nothing. What it shows follows the kind of the test: a **GPU** test only the GPU (temperature with the limit, clock, VRAM, load, fan, throttling, gflop/s, and the host
in one line), a **CPU** test the CPU, RAM, temperature, clock, power, stage and history, **disk** and **network** tests their jobs and results, a **multi-node** or **FULL**
test a table of the nodes with the detail of the selected one and the phase. Narrow windows (under 60 columns) get **cards stacked downwards** with scrolling (`j` `k`)
instead of a squeezed table; tall windows open all of them. Keys: `↑↓` test · `Tab` node · `Enter` end of the log · `x` stop the test (asks first) · `+` `-` interval ·
`p` pause · `e` save the screen · `q` back.

## Scheduling tests for later (`9 SCHEDULE`, key `T`)
Any test - CPU, GPU, disk, network, quick and the FULL self-test - can be planned:
- **Key `T`** in the main menu sets WHEN the next test starts: `in…` a time from now (`30m`, `2h`), `at…` a clock time (`22:30` = today, or tomorrow when it
  has passed) or `on a date…` (`2026-10-03 02:00`, `3.10. 02:00`); `now` clears it. The header shows `🕒 next test starts: …`. Then choose a test as usual
  (1-6), answer its questions - it is **planned instead of started**. The time applies to one test only.
- **`9 SCHEDULE`** opens the planner: the list of planned tests (id, time, test, node), `Plan a test` (the same as T + a test) and `Cancel a plan` (by its number or id).
- **Command line:** `./stress.sh --node X --time 20m --schedule 22:30 -b` (a waiting process), add `--persistent` for a systemd timer, `./stress.sh --scheduled`
  lists the plans, `./stress.sh --stop ID` cancels one (`--status` shows waiting processes as PLANNED).

| | Waiting process (default) | systemd timer (`--persistent`, "survive a restart") |
|---|---|---|
| closing the terminal | survives | survives |
| restart of this computer | the plan is lost | survives, a missed start is caught up at the next boot |
| while you are logged out | survives (Ubuntu keeps the process) | needs once `sudo loginctl enable-linger $USER` (the tool tells you) |
| needs | nothing | systemd (`systemctl --user`), asked only when available |
| the computer is suspended at the start | starts after waking up | starts after waking up |

The computer with the tool must be on and reach the cluster at the start time, otherwise the test is refused and the reason is in its log. At the start the node
is checked again (another test of the tool on it refuses the new one). The FULL self-test asks its two confirmations when it is planned, not at night.

## FULL self-test of the whole cluster (`6 FULL`, or `--self-test`)
One click checks everything the tool can test and ends with **one report**:

| Phase | What runs |
|---|---|
| 1. Network | the node x node matrix (TCP, ping, UDP is optional) |
| 2. Disks | fio benchmark + SMART health on every node |
| 3. CPU | the stepped test 25 / 50 / 75 / 100 %: temperatures, clock, throttling; hardware is read with a read-only privileged pod (RAM modules, GPU, disks) |
| 4. GPU | gpu-burn on every node that offers `nvidia.com/gpu` (alone, never together with the CPU load; the phase is left out when no node has a GPU) |
| 5. RAM | memory allocation under a moderate CPU load (workers only) |

Between the phases it waits until the nodes cool down: the target is each node's **own idle temperature + 8 °C** (at least 45 °C), it
stops waiting when the target is reached, when the hottest node has stopped falling for 2 minutes (a room / case limit) or after
10 minutes. Workers run at once; the **master is optional** (asked, default `n`) and runs last, capped at 70 % CPU.

`--nodes A,B` limits the self-test to the chosen nodes (a master only when named; with a single node the network matrix is skipped).
Levels: `quick` (about 15-20 min for a few nodes), `standard` (about 35 min, default), `thorough` (about 70 min) - `--self-test-level`.
**It loads everything at full power: the cluster is not usable meanwhile, the power draw and the room temperature rise.** It shows a
warning box and needs two confirmations (`y`, then the word `START`; for scripts `--self-test-ack`). The result:
`logs/<day>/selftest-<time>/full-selftest-<time>.log` + `.json` (phases, a table per node, findings such as a slow disk with its model,
SMART warnings, throttling, the hardware of every node, the network matrix, and a verdict), and a summary frame on the screen.

## GPU test (NVIDIA) - `--profile gpu`, main menu `2 GPU`
```bash
./stress.sh --list-gpus                                                    # which nodes have a usable GPU?
./stress.sh --profile gpu --node worker-1 --time 2m --gpu-max-temp 80
./stress.sh --profile gpu --nodes worker-1,gpu-node-2 --time 2m   # several nodes, one after another
```
Burns the node's NVIDIA GPU with gpu-burn (a pod with `runtimeClassName: nvidia` and `nvidia.com/gpu: 1`, unprivileged, no hostPath) and reads
`nvidia-smi` every interval: GPU temperature, utilization, clock, VRAM, power (`N/A` on cards that do not report it, e.g. Quadro P620), throttle
reasons and the GPU fan speed in % (`nvidia-smi` gives no RPM; the CPU fan is not read). No CPU / RAM / disk load is combined with it.

**Menu `2 GPU`** starts with a **scan of every node** (also `--list-gpus`): the Kubernetes API (`nvidia.com/gpu`) plus `lspci` in a short read-only
pod are combined into one table:

| Mark | Meaning |
|---|---|
| ✅ OK | a usable dedicated NVIDIA GPU (model and `nvidia.com/gpu 1` shown) |
| ⚠️ ERROR | an NVIDIA card is there, but Kubernetes does not offer it (driver / toolkit / runtime / device plugin missing) |
| ❌ none | no dedicated GPU (only an integrated Intel / AMD one, or no card) |
| ❓ `? ERROR` | the scan failed, it cannot be said whether a GPU is there |
| 💤 not Ready | the node is down |

Only ✅ nodes can be chosen: with a single one it is picked automatically, with several you type numbers (`1,3`) or `a` for all - they are tested
**one after another**. Then a settings dialog asks for the length (1 / 2 / 5 / 10 min or custom), the **GPU stop temperature** (default 80 °C, a
warning 5 °C lower), the share of the GPU memory (default 90 %), single / double precision, the cooldown, the image, pre-pulling the image and the
extras, and shows a summary. The CPU temperature limit is not asked (the default 85 °C guard stays; `--max-temp` changes it).

**Live frame** (its own `GpuScreen`): the card, VRAM, driver, CUDA capability and PCIe in the title; bars for the GPU temperature (with the stop and
warning marks and a small history graph), clock (against the card maximum), VRAM used / total and load; power, throttle reason and fan; the host
(CPU load, RAM, CPU temperature and clock) on one row; the last events. With several nodes the frame starts with **one table of all nodes**
(✅ done with max temperature, clock and throttling · ▶ running with progress and live temperature · ⏳ waiting · ❌ failed), and the same table is
printed at the end instead of the CPU cluster summary. Before the load the card's static data (`GPU INFO`: model, driver, memory, power limit,
maximum clocks, PCIe, VBIOS) is read and written to the log. CPU tests show the model and cores / threads under their title
(`Intel Core i7-4790S · 4c/8t @ 3.2 GHz`).

**Safety:** the test stops at `--gpu-max-temp` (2 readings in a row), at the CPU limit, and also when `nvidia-smi` stops answering (3 failed readings
- the GPU could not be watched). The cooldown measures the GPU cooling (the pod idles after the load). A GPU used by another pod is refused.

| Option | Meaning |
|---|---|
| `--gpu-max-temp °C` | GPU stop temperature (50-95, default 80; asked in the dialog) |
| `--gpu-mem-pct %` | share of the GPU memory gpu-burn fills (default 90) |
| `--gpu-double` | double precision (very slow on consumer cards) |
| `--gpu-image IMAGE` | image of the load pod (default a CUDA 12.x devel image; one that contains `gpu_burn` skips the build) |
| `--gpu-prepull` | pull the CUDA image on the chosen nodes first, all at once (asked in the menu dialog) |
| `--dashboard` | the live cluster dashboard (see above); `--nodes A,B` limits it |
| `--list-gpus` | scan all nodes for graphics cards and show the table above, then exit |

Needs on each GPU node: the NVIDIA driver, `nvidia-container-toolkit`, runtime `nvidia` in containerd and the NVIDIA device plugin - a step-by-step
guide and the manifests (`deploy/nvidia-device-plugin-k3s.yaml`, `deploy/gpu-smoke-test.yaml`) are in [`HELPDESK.md`](HELPDESK.md). The first run
pulls a ~3 GB image and builds gpu-burn (a few minutes); `--gpu-prepull` or a `--gpu-image` with a ready `gpu_burn` avoids waiting later.
The `--gpu-*` options without `--profile gpu` are ignored (a note is printed). The FULL self-test (below; `--nodes A,B` limits it to chosen nodes)
adds a GPU phase for every node that offers `nvidia.com/gpu`.

## Gallery - what every screen looks like
All screens below are drawn by the program itself from a simulated cluster (generic node names such as `worker-1`, no real data); the colours are left out of the text. Every screen
adapts to the width **and the height** of the window.

### Main menu
```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━ github.com/doctorxcz/k3s-cluster-stress-test ━┓
┃ ◖●▲●◗ K3S·STRESS                                               v1.17.0 ┃
┃ › cluster: 5 nodes (4 workers + 1 master), all Ready                   ┃
┃ 🔥 last measured max 71 °C (worker-1)                                  ┃
┃ 🔁 last: ./stress.sh --profile stepped · 2026-10-01 10:40              ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ TESTS                                                                  ┃
┃ 1 ▸ ⚡ CPU       classic · stepped · spike                             ┃
┃ 2 ▸ 🎮 GPU       NVIDIA burn · temperature, clocks                     ┃
┃ 3 ▸ 💾 DISK      fio · SMART                                           ┃
┃ 4 ▸ 📡 NET       one node · matrix of all nodes                        ┃
┃ 5 ▸ 🚀 QUICK     one node, 10 min, few questions                       ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ CLUSTER                                                                ┃
┃ 6 ▸ 🧪 FULL      self-test of the whole cluster · one report           ┃
┃ 7 ▸ 📊 DATA      compare · baseline · export                           ┃
┃ 8 ▸ 🔧 ADMIN     what runs · stop · nodes                              ┃
┃ 9 ▸ 🕒 SCHEDULE  planned tests · plan · cancel                         ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ 🔁 R ▸ repeat   🧰 S ▸ settings   ⏰ T ▸ start time   📺 D ▸ dashboard ┃
┃ 🔭 W ▸ watch   ❓ ? ▸ help   🚪 Q ▸ quit                               ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```
<details>
<summary>Wide window (two columns, nodes with their last temperatures)</summary>

```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ github.com/doctorxcz/k3s-cluster-stress-test ━┓
┃ ◖●▲●◗ K3S·STRESS                                                                                                        v1.17.0 ┃
┃ › cluster: 5 nodes (4 workers + 1 master), all Ready                                                                            ┃
┃ 🔥 last measured max 71 °C (worker-1)                                                                                           ┃
┃ 🔁 last: ./stress.sh --profile stepped · 2026-10-01 10:40                                                                       ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ TESTS                                                          │ CLUSTER                                                        ┃
┃  1 ▸ ⚡ CPU      classic · stepped 25/50/75/100 % · spike:     │  6 ▸ 🧪 FULL     whole cluster: network → disks → CPU → RAM,   ┃
┃                  temperatures, clock, throttling               │                  cooling pauses, one report (~35 min)          ┃
┃  2 ▸ 🎮 GPU      NVIDIA GPU burn (gpu-burn): GPU temperature,  │  7 ▸ 📊 DATA     compare two tests · set a baseline · export a ┃
┃                  clocks, throttling · stop limit selectable    │                  log to JSON / CSV                             ┃
┃  3 ▸ 💾 DISK     fio benchmark (MB/s, IOPS, latency) · SMART   │  8 ▸ 🔧 ADMIN    what is running · stop a test · list nodes    ┃
┃                  health of the disks                           │                  (role, state)                                 ┃
┃  4 ▸ 📡 NET      iperf3 + ping to a peer · matrix of every     │  9 ▸ 🕒 SCHEDULE planned tests: list what waits for its start, ┃
┃                  node pair · MTU, DNS, internet                │                  plan a test for later (in 2 h / at 22:30 / on ┃
┃  5 ▸ 🚀 QUICK    one node, 10 min, 100 % CPU, few questions: a │                  a date), cancel a plan                        ┃
┃                  fast health check                             │                                                                ┃
┃                                                                │ NODES                                                          ┃
┃ TIPS                                                           │ 💻 control-1                  master         Ready  🔥 55 °C   ┃
┃ · R repeats the last action exactly as it ran (shown in the    │ 💻 worker-1                   worker         Ready  🔥 71 °C   ┃
┃   header as 🔁).                                               │ 💻 worker-2                   worker         Ready  🔥 64 °C   ┃
┃ · S saves a default node and a temperature limit for the       │ 💻 worker-3                   worker         Ready  🔥 52 °C   ┃
┃   tests.                                                       │ 💻 gpu-worker-1               worker         Ready  🔥 68 °C   ┃
┃ · ? opens the help: every entry explained, links to README and │ 🔥 = hottest temperature in the newest test log of the node    ┃
┃   HELPDESK.                                                    │                                                                ┃
┃ · A test in a terminal is one live frame; FULL asks twice      │                                                                ┃
┃   before it starts.                                            │                                                                ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ 🔁 R ▸ repeat the last action   🧰 S ▸ settings   ⏰ T ▸ start time   📺 D ▸ dashboard   🔭 W ▸ watch tests                     ┃
┃ ❓ ? ▸ help and docs   🚪 Q ▸ quit (0 works too)                                                                                ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

</details>

<details>
<summary>Narrow window (under 60 columns) and a tall window (tips and nodes are added)</summary>

```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ ◖●▲●◗ K3S·STRESS                        v1.17.0 ┃
┃ › cluster: 5 nodes (4 workers + 1 master), all… ┃
┃ 🔥 last measured max 71 °C (worker-1)           ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ TESTS                                           ┃
┃ 1 ▸ ⚡ CPU                                      ┃
┃ 2 ▸ 🎮 GPU                                      ┃
┃ 3 ▸ 💾 DISK                                     ┃
┃ 4 ▸ 📡 NET                                      ┃
┃ 5 ▸ 🚀 QUICK                                    ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ CLUSTER                                         ┃
┃ 6 ▸ 🧪 FULL                                     ┃
┃ 7 ▸ 📊 DATA                                     ┃
┃ 8 ▸ 🔧 ADMIN                                    ┃
┃ 9 ▸ 🕒 SCHEDULE                                 ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ 🔁 R  🧰 S  ⏰ T  📺 D  🔭 W  ❓ ?  🚪 Q        ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━ github.com/doctorxcz/k3s-cluster-stress-test ━┓
┃ ◖●▲●◗ K3S·STRESS                                               v1.17.0 ┃
┃ › cluster: 5 nodes (4 workers + 1 master), all Ready                   ┃
┃ 🔥 last measured max 71 °C (worker-1)                                  ┃
┃ 🔁 last: ./stress.sh --profile stepped · 2026-10-01 10:40              ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ TESTS                                                                  ┃
┃ 1 ▸ ⚡ CPU       classic · stepped · spike                             ┃
┃ 2 ▸ 🎮 GPU       NVIDIA burn · temperature, clocks                     ┃
┃ 3 ▸ 💾 DISK      fio · SMART                                           ┃
┃ 4 ▸ 📡 NET       one node · matrix of all nodes                        ┃
┃ 5 ▸ 🚀 QUICK     one node, 10 min, few questions                       ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ CLUSTER                                                                ┃
┃ 6 ▸ 🧪 FULL      self-test of the whole cluster · one report           ┃
┃ 7 ▸ 📊 DATA      compare · baseline · export                           ┃
┃ 8 ▸ 🔧 ADMIN     what runs · stop · nodes                              ┃
┃ 9 ▸ 🕒 SCHEDULE  planned tests · plan · cancel                         ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ TIPS                                                                   ┃
┃ · R repeats the last action exactly as it ran.                         ┃
┃ · S saves a default node and a temperature limit.                      ┃
┃ · T / 9 plan a test for later (a detached process or a systemd timer). ┃
┃ · D live cluster dashboard · W live status of the running tests.       ┃
┃ · ? opens the help: every entry explained, links to README and         ┃
┃   HELPDESK.                                                            ┃
┃                                                                        ┃
┃ NODES                                                                  ┃
┃ 💻 control-1  master  Ready                                            ┃
┃ 💻 worker-1  worker  Ready                                             ┃
┃ 💻 worker-2  worker  Ready                                             ┃
┃ 💻 worker-3  worker  Ready                                             ┃
┃ 💻 gpu-worker-1  worker  Ready                                         ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ 🔁 R ▸ repeat   🧰 S ▸ settings   ⏰ T ▸ start time   📺 D ▸ dashboard ┃
┃ 🔭 W ▸ watch   ❓ ? ▸ help   🚪 Q ▸ quit                               ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

</details>

### Starting a test
Every entry only builds the command line; then the usual questions follow.
<details>
<summary>CPU submenu, settings, schedule and the help</summary>

```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ ◖●▲●◗ CPU LOAD                                                                                  ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ 1 ▸ classic  one load for the given time                                                        ┃
┃ 2 ▸ stepped  gradually 25 / 50 / 75 / 100 %                                                     ┃
┃ 3 ▸ spike    repeating jump between a low and a target load                                     ┃
┃ 0 ▸ Back                                                                                        ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ ◖●▲●◗ SETTINGS                                                                                  ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ 1 ▸ default node       ask every time                                                           ┃
┃ 2 ▸ temperature limit  default (85 °C)                                                          ┃
┃ 3 ▸ live frames        on                                                                       ┃
┃ 4 ▸ reset              back to the defaults                                                     ┃
┃ 0 ▸ Back                                                                                        ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ github.com/doctorxcz/k3s-cluster-stress-test ━┓
┃ ◖●▲●◗ 🕒 SCHEDULE                                                                               ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ Nothing is scheduled.                                                                           ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛

┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ ◖●▲●◗ PLANNER                                                                                   ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ 1 ▸ Plan a test  set the start time, then choose the test                                       ┃
┃ 0 ▸ Back                                                                                        ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ github.com/doctorxcz/k3s-cluster-stress-test ━┓
┃ ◖●▲●◗ HELP 1/2                                                                                  ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ KEYS                                           │ °C, a warning 5 °C below it).                  ┃
┃ 1-9: open the entry. It only builds the usual  │ needs  NVIDIA driver, nvidia-container-        ┃
┃ command line (printed as ▶ ./stress.sh ...)    │        toolkit, runtime nvidia and the device  ┃
┃ and runs the normal program, which asks the    │        plugin on the node (see HELPDESK.md).   ┃
┃ rest.                                          │        The first run pulls a ~3 GB image.      ┃
┃ R  repeat the last action exactly as it ran.   │                                                ┃
┃ S  settings: default node, temperature limit,  │ 💾 3 DISK                                      ┃
┃    live frames.                                │ fio benchmark on one node: sequential and      ┃
┃ T  start time of the NEXT test (in 2 h, at     │ random read/write, MB/s, IOPS, latency. Uses a ┃
┃    22:30, on a date); the test you choose      │ scratch file on the node's own disk (removed   ┃
┃    afterwards is planned.                      │ afterwards, ~1 GiB free needed).               ┃
┃ D  the live cluster dashboard (every node at a │ + SMART  reads the disk health first (a        ┃
┃    glance, three widths).                      │          privileged pod; a failing disk is     ┃
┃ W  watch the running tests: a live screen      │          refused).                             ┃
┃    (every second) with the details of each     │ No CPU load and no cooldown - only what the    ┃
┃    kind - GPU, CPU, disk, network, FULL; x     │ disk does.                                     ┃
┃    stops one.                                  │                                                ┃
┃ ?  this help.     Q = quit (0 works too).      │ 📡 4 NET                                       ┃
┃    Ctrl+C stops a running test safely (its     │ one node  iperf3 + ping to a peer: TCP         ┃
┃    pods are deleted).                          │           up/down/4 streams, UDP, MTU, link    ┃
┃ Everything the menu does can also be typed as  │           speed, DNS, internet. A blocked UDP  ┃
┃ options: ./stress.sh --help                    │           is reported and skipped (--net-extra ┃
┃                                                │           no-udp leaves it out).               ┃
┃ ⚡ 1 CPU                                       │ matrix    every pair of nodes, tables of       ┃
┃ Loads the CPU of ONE node and measures         │           Mbit/s and ping - finds the weak     ┃
┃ temperature, clock, throttling and the cooling │           cable or port.                       ┃
┃ afterwards.                                    │ Needs TCP + UDP ports 30000-32767 open between ┃
┃ classic  one constant load for the time you    │ the nodes (firewall). The master is capped at  ┃
┃          give (RAM and disk load can be        │ 300 Mbit/s so that the API keeps its network.  ┃
┃          added).                               │                                                ┃
┃ stepped  25 / 50 / 75 / 100 % one after        │ 🚀 5 QUICK                                     ┃
┃          another - shows where the node starts │ One node, 10 minutes, almost no questions: CPU ┃
┃          to throttle.                          │ 100 %, 85 °C limit, 1 min cooldown. The        ┃
┃ spike    repeated jumps between a low and a    │ fastest way to check that a node is healthy.   ┃
┃          target load - shows how fast the node │                                                ┃
┃          heats up.                             │ 🧪 6 FULL                                      ┃
┃ Stops itself at the temperature limit (default │ Self-test of the WHOLE cluster in one go:      ┃
┃ 85 °C).                                        │ network matrix, disks + SMART, CPU (stepped)   ┃
┃                                                │ and RAM with cooling pauses between the        ┃
┃ 🎮 2 GPU                                       │ phases, then one report (log + JSON + a        ┃
┃ NVIDIA GPU burn (gpu-burn) on a node that      │ verdict per node).                             ┃
┃ offers nvidia.com/gpu: GPU temperature,        │ levels   quick ~15 min, standard ~35 min,      ┃
┃ clocks, power, throttling. No CPU load. You    │          thorough ~70 min (for a few nodes).   ┃
┃ choose the GPU stop temperature (default 80    │ warning  everything runs at full power: the    ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

</details>

<details>
<summary>GPU scan (`--list-gpus`, also the first step of the GPU test): which nodes have a usable NVIDIA card</summary>

```text
🎮 Scanning 3 nodes for graphics cards (short read-only pods, up to ~1-2 min)...
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ github.com/doctorxcz/k3s-cluster-stress-test ━┓
┃ ◖●▲●◗ 🎮 GPU SCAN                                                                               ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ 💻 worker-1                   worker  ✅ OK                                                     ┃
┃       NVIDIA Corporation GP107GL [Quadro P620] (rev a1) · nvidia.com/gpu 1                      ┃
┃ 💻 worker-2                   worker  ❌ none                                                   ┃
┃       no dedicated NVIDIA GPU (only: Intel Intel Corporation HD Graphics 4600 (rev 06))         ┃
┃ 💻 control-1                  master  ❗ ERROR                                                  ┃
┃       NVIDIA Corporation GP107GL [Quadro P620] (rev a1) found, but Kubernetes does not offer it ┃
┃       (driver / toolkit / runtime / device plugin)                                              ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ 1 of 3 nodes have a usable dedicated NVIDIA GPU                                                 ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

</details>

### While a test runs - one live frame
A test in a terminal is one framed block redrawn in place: the progress bar, the stage, the latest measurement and the last events.
```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ github.com/doctorxcz/k3s-cluster-stress-test ━┓
┃ ◖●▲●◗ 💻 worker-1 · 1 min 20 s (80 s) test                                                      ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ⚡ Intel Core i7-4790S · 4c/8t                                                                  ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ 🎯 starting                                                                                     ┃
┃ ███████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  18 %  ⏳ test · 1:05 left                             ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ⚡ CPU 100%   🧠 RAM 1953 MiB (25%)                                                             ┃
┃ 🔥 TEMP CPU: 71°C, NVMe: 41°C   📈 CLOCK 3591 MHz                                               ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ 🔥  CPU sensor found (44°C). Automatic stop at 85°C is active.                                  ┃
┃ ⏳ Detecting node hardware...                                                                   ┃
┃ 💻  NODE HARDWARE worker-1:                                                                     ┃
┃ CPU: Intel(R) Core(TM) i7-4790S @ 3.20GHz                                                       ┃
┃ Threads: 8                                                                                      ┃
┃ ⏳ Waiting for the pod to start...                                                              ┃
┃ ⏳ Waiting for stress-ng to be installed and started...                                         ┃
┃ 🔥 Test running 1 min 20 s (80 s), ends around 19:28:12 (automatic stop at 85°C)                ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```
<details>
<summary>Stepped test (25 / 50 / 75 / 100 %)</summary>

```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ github.com/doctorxcz/k3s-cluster-stress-test ━┓
┃ ◖●▲●◗ 💻 worker-1 · 12 min (720 s) test                                                         ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ⚡ Intel Core i7-4790S · 4c/8t                                                                  ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ 🎯 Stage 2/4: 50 % (3 min (180 s))                                                              ┃
┃ █░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░   3 %  ⏳ test · 11:37 left                            ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ⚡ CPU 50%   🧠 RAM 1953 MiB (25%)                                                              ┃
┃ 🔥 TEMP CPU: 57°C, NVMe: 41°C   📈 CLOCK 3591 MHz                                               ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ CPU: Intel(R) Core(TM) i7-4790S @ 3.20GHz                                                       ┃
┃ Threads: 8                                                                                      ┃
┃ 📊 Metrics are being saved to: logs/2026-10-01/worker-1-720s-2026-10-01_19-27…                  ┃
┃ ⏳ Waiting for the pod to start...                                                              ┃
┃ ⏳ Waiting for stress-ng to be installed and started...                                         ┃
┃ 🔥 Test running 12 min (720 s), ends around 19:39:08 (automatic stop at 85°C)                   ┃
┃ [19:27:08] ▶ Stage 1/4: 25 % (3 min (180 s))                                                    ┃
┃ [19:27:28] ▶ Stage 2/4: 50 % (3 min (180 s))                                                    ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

</details>

<details>
<summary>GPU test (NVIDIA, gpu-burn): the card, temperature with the stop limit, clock, VRAM, load, power, throttling, fan and the host</summary>

```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ github.com/doctorxcz/k3s-cluster-stress-test ━┓
┃ ◖●▲●◗ 🎮 Quadro P620 · 💻 worker-1                                                               ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ 2 GB · driver 580.178.04 · CC 6.1 · PCIe gen 3 16x                                               ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ 🎯 gpu-burn started (90 % of the GPU memory, single precision)                                   ┃
┃ ███████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  27 %  ⏳ test · 0:58 left                              ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ 🔥 GPU TEMP  68 °C            ▕████████████████████████░░░░▏ stop 80 · warn 75  ▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇… ┃
┃ 📈 GPU CLOCK 1300 MHz         ▕█████████████████████░░░░░░░▏ max 1721                            ┃
┃ 🧠 VRAM      1800 / 2048 MiB  ▕█████████████████████████░░░▏ 88 %                                ┃
┃ ⚡ GPU LOAD  100 %            ▕████████████████████████████▏                                     ┃
┃ 🔌 POWER N/A (not reported)   🧊 THROTTLE none   🌀 GPU FAN 40 %                                 ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ 💻 HOST   ⚡ CPU 100%   🧠 RAM 25%   🔥 CPU 71°C, NVMe: 41°C   📈 3591 MHz                       ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ⏳ Pulling the CUDA image can take several minutes the first time (about 3 GB)...                ┃
┃ ⏳ Waiting for gpu-burn to be built and started...                                               ┃
┃ 🎮 GPU INFO: (full text below after the test)                                                    ┃
┃ 🔥 Test running 1 min 20 s (80 s), ends around 19:28:52 (automatic stop at 85°C, GPU 80°C)       ┃
┃ [19:27:32] ▶ gpu-burn started (90 % of the GPU memory, single precision)                         ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

</details>

<details>
<summary>Network test (iperf3 + ping to a peer: link, ping, MTU, TCP up / down / 4 streams, UDP)</summary>

```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ github.com/doctorxcz/k3s-cluster-stress-test ━┓
┃ ◖●▲●◗ 💻 worker-1 · 36 s test                                                                   ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ⚡ Intel Core i7-4790S · 4c/8t                                                                  ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ 🎯 Network job 7/8 udp                                                                          ┃
┃ ███████████████████████████████████░░░░░  87 %  ⏳ test · 0:05 left                             ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ⚡ CPU 100%   🧠 RAM 1953 MiB (25%)                                                             ┃
┃ 🔥 TEMP CPU: 40°C, NVMe: 41°C   📈 CLOCK 3000 MHz                                               ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ [19:34:01] 🌐 mtu           path MTU 1500 B                                                     ┃
┃ [19:34:01] ▶ Network job 4/8 tcp-up                                                             ┃
┃ [19:34:06] 🌐 tcp-up        940.0 Mbit/s, retransmits 2                                         ┃
┃ [19:34:06] ▶ Network job 5/8 tcp-down                                                           ┃
┃ [19:34:11] 🌐 tcp-down      940.0 Mbit/s, retransmits 2                                         ┃
┃ [19:34:11] ▶ Network job 6/8 tcp-x4                                                             ┃
┃ [19:34:16] 🌐 tcp-x4        940.0 Mbit/s, retransmits 2                                         ┃
┃ [19:34:16] ▶ Network job 7/8 udp                                                                ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

</details>

<details>
<summary>Network matrix (every pair of nodes, Mbit/s)</summary>

```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ github.com/doctorxcz/k3s-cluster-stress-test ━┓
┃ ◖●▲●◗ 📡 NETWORK MATRIX · 3 nodes · 5 s per pair                                                ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ 🎯 [6/6] worker-2 → control-1  (capped at 300 Mbit/s)                                           ┃
┃ ████████████████████████████████████████ 100 %  ⏳ ~0:00 left  pairs 6/6                        ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃                    [1]    [2]    [3]                                                            ┃
┃ 💻 [1] worker-1       -    940   940*                                                           ┃
┃ 💻 [2] worker-2     940      -   940*                                                           ┃
┃ 💻 [3] control-1   940*   940*      -                                                           ┃
┃ Mbit/s · row = client, column = server · * = capped on purpose                                  ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ control-1 → worker-1: 940 Mbit/s, ping 0.40 ms, retransmits 1                                   ┃
┃ worker-1 → worker-2: 940 Mbit/s, ping 0.40 ms, retransmits 1                                    ┃
┃ control-1 → worker-2: 940 Mbit/s, ping 0.40 ms, retransmits 1                                   ┃
┃ worker-1 → control-1: 940 Mbit/s, ping 0.40 ms, retransmits 1                                   ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

</details>

<details>
<summary>Several nodes at once (`--parallel`)</summary>

```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ github.com/doctorxcz/k3s-cluster-stress-test ━┓
┃ ◖●▲●◗ PARALLEL TEST · 2 nodes                                                                               ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ 🟢 running · all 2 nodes started                                                                            ┃
┃ ███████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  28 %  ⏳ ~0:50 left  (all nodes)                                  ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ 💻 worker-1           🎯 load         ████░░░░░░░░  32%  ⏳    0:40 ⚡ 100% 🔥  64°C 📈 3200 MHz 🧠  25%    ┃
┃ 💻 worker-2           🎯 load         ████░░░░░░░░  32%  ⏳    0:40 ⚡ 100% 🔥  64°C 📈 3200 MHz 🧠  25%    ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

</details>

### The result
```text
====================================================
TEST SUMMARY
====================================================
Samples:                  7 during the test, 10 during cooldown
CPU temp (test):          min 71 | avg 71 | max 71 °C   (idle before test 44 °C)
Time above 80 °C:         0 s
CPU clock (test):         avg 3591 | min 3591 | max 3591 MHz
Throttling:               no signs of throttling (clock stable, change 0.0 %)
CPU load (test):          avg 100 % | max 100 %
RAM usage (test):         max 1953 MiB (25 %)
stress-ng performance:    cpu 658.9 | matrix 1458.5 bogo ops/s
Cooldown (11 s):          71 → 44 °C (-27 °C, 152.5 °C/min)
Drop after load stops:    71 → 65 °C in 1 s (-6 °C)
Slow cooldown:            short measurement (10 s), rate cannot be estimated
Clock after test:         3591 MHz
Return to idle (≤ 49 °C): after 4 s (idle before test 44 °C + 5)
====================================================
📄 Export saved to: logs/2026-10-01/worker-1-7s-2026-10-01_19-32-21.json
📁 Log saved to: logs/2026-10-01/worker-1-7s-2026-10-01_19-32-21.log
```
<details>
<summary>Comparing two tests of a node (`--compare`)</summary>

```text
==================================================================================================
TEST COMPARISON
==================================================================================================
A: logs/2026-10-01/worker-1-8s-2026-10-01_19-33-12.log
B: logs/2026-10-01/worker-1-8s-2026-10-01_19-33-28.log

                        A (older)             B (newer)             change B vs A
--------------------------------------------------------------------------------------------------
Node:                   worker-1              worker-1
Started:                2026-10-01 19:33:13   2026-10-01 19:33:29
Test duration:          8 s                   8 s
Profile:                classic               classic
Parallel nodes:         no                    no
Idle before test:       44 °C                 44 °C                 0 °C

CPU temp max:           66 °C                 73 °C                 +7 °C (worse)
CPU temp avg:           66 °C                 73 °C                 +7 °C (worse)
Time above 80 °C:       0 s                   0 s                   0 s
CPU clock avg:          3591 MHz (min 3591)   3401 MHz (min 3401)   -190 MHz
Throttling:             no                    no
CPU load avg:           100 %                 100 %                 0 %
Performance cpu (bogo   658.9                 658.9                 +0.0 %
ops/s):
Performance matrix      1458.5                1458.5                +0.0 %
(bogo ops/s):

Drop after load off:    66 → 60 °C in 1 s     73 → 67 °C in 1 s     0 °C
                        (-6)                  (-6)
Slow cooldown:          short measurement     short measurement
Return to idle (+5 °C): after 3 s             after 4 s             +1 s
--------------------------------------------------------------------------------------------------
Total: 0× better, 2× worse (the rest without a significant change).

How to read: 'Drop after load off' is the fast drop right after the load ends (the difference
between chip and heatsink),
'Slow cooldown' is the rest of the whole heatsink cooling down. Changes smaller than 2 °C, 2 % of
performance or 10 s are not rated.
==================================================================================================
```

</details>

### Live status of the running tests (key `W`)
One screen for everything that runs or waits; what is shown follows the kind of the test. The selected test is open, the other ones are one line.
```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ github.com/doctorxcz/k3s-cluster-stress-test ━┓
┃ ◖●▲●◗ 🔭 STATUS · 3 tests running · 1 s · 19:25:41                                                         ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ▸ 🎮 GPU           gpu-worker-1             a4f2c1  █████████░░░░░░░  54%  55 s left                       ┃
┃   ⚡ CPU stepped   worker-3                 b81e09 (fg)  █████████░░░░░░░  54%  55 s left                  ┃
┃   ⚡ CPU           cluster                  c77d12 (fg)  █████░░░░░░░░░░░  33%  0/3 nodes done             ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ▸ gpu test · gpu-worker-1                                                                                  ┃
┃ 🔥 GPU  67°  stop 80  ▅▅▅▅▅▆▆▆▆▆▆▆▆▆                                                                       ┃
┃ 📈 1354 MHz   ⚡ load 99 %   🧠 VRAM 1593 MiB   🔌 power N/A   🌀 fan 40 %                                 ┃
┃ 🧊 throttle none   🚀 1086 Gflop/s · errors 0                                                              ┃
┃ 💻 host  CPU 82% · RAM 20% · 61 °C · 2870 MHz                                                              ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ▸ events                                                                                                   ┃
┃ [10:01:00] 🚀 gpu-burn performance: 1086 Gflop/s · errors 0                                                ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ↑↓ test  Tab node  Enter log  x stop  e screen  +/- interval  p pause  q back                              ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```
<details>
<summary>A CPU test and a test on several nodes</summary>

```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ github.com/doctorxcz/k3s-cluster-stress-test ━┓
┃ ◖●▲●◗ 🔭 STATUS · 3 tests running · 1 s · 19:25:41                                                         ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃   🎮 GPU           gpu-worker-1             a4f2c1  █████████░░░░░░░  54%  55 s left                       ┃
┃ ▸ ⚡ CPU stepped   worker-3                 b81e09 (fg)  █████████░░░░░░░  54%  55 s left                  ┃
┃   ⚡ CPU           cluster                  c77d12 (fg)  █████░░░░░░░░░░░  33%  0/3 nodes done             ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ▸ stepped test · worker-3                                                                                  ┃
┃ ⚡ CPU ███████░  82%   🧠 RAM ██░░░░░░  20% (1200 MiB)                                                     ┃
┃ 🔥 TEMP  61°  stop 85   📈 2870 MHz   🔌 20.5 W                                                            ┃
┃ history CPU ▃▃▄▄▄▅▅▅▅▆▆▆▆▇               temp ▄▄▄▄▄▄▄▅▅▅▅▅▅▅               clock ██████████████          … ┃
┃ clock 2870 MHz now · 3000 MHz max seen · steady                                                            ┃
┃ 🎯 stage 3/4 · 75 %                                                                                        ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ▸ events                                                                                                   ┃
┃ [10:01:00] ▶ Stage 3/4: 75 % (3 min)                                                                       ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ↑↓ test  Tab node  Enter log  x stop  e screen  +/- interval  p pause  q back                              ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ github.com/doctorxcz/k3s-cluster-stress-test ━┓
┃ ◖●▲●◗ 🔭 STATUS · 3 tests running · 1 s · 19:25:41                                                           ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃   🎮 GPU           gpu-worker-1             a4f2c1  █████████░░░░░░░  54%  55 s left                         ┃
┃   ⚡ CPU stepped   worker-3                 b81e09 (fg)  █████████░░░░░░░  54%  55 s left                    ┃
┃ ▸ ⚡ CPU           cluster                  c77d12 (fg)  █████░░░░░░░░░░░  33%  0/3 nodes done               ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ▸ classic test · worker-1, worker-2, worker-4                                                                ┃
┃ NODE                        STATE       CPU          TEMP   RAM          GPU           CLOCK                 ┃
┃ ▸ worker-1                    test        ████░  82%    61°   █░░░░  20%   –             2870MHz             ┃
┃   worker-2                    test        ████░  82%    61°   █░░░░  20%   –             2870MHz             ┃
┃   worker-4                    waiting                                                                        ┃
┃                                                                                                              ┃
┃ ▸ worker-1                                                                                                   ┃
┃ ⚡ CPU ███████░  82%   🧠 RAM ██░░░░░░  20% (1200 MiB)                                                       ┃
┃ 🔥 TEMP  61°  stop 85   📈 2870 MHz   🔌 20.5 W                                                              ┃
┃ history CPU ▃▃▄▄▄▅▅▅▅▆▆▆▆▇                temp ▄▄▄▄▄▄▄▅▅▅▅▅▅▅                clock ██████████████          … ┃
┃ clock 2870 MHz now · 3000 MHz max seen · steady                                                              ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ↑↓ test  Tab node  Enter log  x stop  e screen  +/- interval  p pause  q back                                ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

</details>

<details>
<summary>A narrow window: cards stacked downwards (scroll with j / k) - and the same window made tall</summary>

```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ ◖●▲●◗ 🔭 STATUS · 3                        ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ────────────────────────────────────────── ┃
┃ ▸ 🎮 GPU  a4f2c1                           ┃
┃   gpu-worker-1                             ┃
┃   ███████████░░░░░░░░░ 54 %                ┃
┃   55 s left                                ┃
┃   🔥 GPU        67°  (stop 80)             ┃
┃   📈 clock     1354 MHz                    ┃
┃   ⚡ load      99 %                        ┃
┃   🧠 VRAM      1593 MiB                    ┃
┃   🔌 power     N/A                         ┃
┃   🌀 fan       40 %                        ┃
┃   🧊 throttle  none                        ┃
┃   🚀 perf      1086 Gflop/s · errors 0     ┃
┃   💻 host      CPU 82% · RAM 20%           ┃
┃ ────────────────────────────────────────── ┃
┃   ⚡ CPU stepped  54% worker-3             ┃
┃   ⚡ CPU  33% cluster                      ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ↑↓ test  j/k scroll  x stop  e screen  q   ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ ◖●▲●◗ 🔭 STATUS · 3                        ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ────────────────────────────────────────── ┃
┃ ▸ 🎮 GPU  a4f2c1                           ┃
┃   gpu-worker-1                             ┃
┃   ███████████░░░░░░░░░ 54 %                ┃
┃   55 s left                                ┃
┃   🔥 GPU        67°  (stop 80)             ┃
┃   📈 clock     1354 MHz                    ┃
┃   ⚡ load      99 %                        ┃
┃   🧠 VRAM      1593 MiB                    ┃
┃   🔌 power     N/A                         ┃
┃   🌀 fan       40 %                        ┃
┃   🧊 throttle  none                        ┃
┃   🚀 perf      1086 Gflop/s · errors 0     ┃
┃   💻 host      CPU 82% · RAM 20%           ┃
┃ ────────────────────────────────────────── ┃
┃ ▸ ⚡ CPU stepped  b81e09                   ┃
┃   worker-3                                 ┃
┃   ███████████░░░░░░░░░ 54 %                ┃
┃   55 s left                                ┃
┃   ⚡ CPU       █████░  82%                 ┃
┃   🧠 RAM       █░░░░░  20%                 ┃
┃   🔥 temp       61°  (stop 85)             ┃
┃   📈 clock     2870 MHz                    ┃
┃   🔌 power     20.5 W                      ┃
┃   🎯 stage     3/4 · 75 %                  ┃
┃ ────────────────────────────────────────── ┃
┃ ▸ ⚡ CPU  c77d12                           ┃
┃   cluster                                  ┃
┃   ███████░░░░░░░░░░░░░ 33 %                ┃
┃   0/3 nodes done                           ┃
┃ ────────────────────────────────────────── ┃
┃ ▸ worker-1                                 ┃
┃   ⚡ CPU       █████░  82%                 ┃
┃   🧠 RAM       █░░░░░  20%                 ┃
┃   🔥 temp       61°  (stop 85)             ┃
┃   📈 clock     2870 MHz                    ┃
┃   🔌 power     20.5 W                      ┃
┃ ────────────────────────────────────────── ┃
┃ ▸ worker-2                                 ┃
┃   ⚡ CPU       █████░  82%                 ┃
┃   🧠 RAM       █░░░░░  20%                 ┃
┃   🔥 temp       61°  (stop 85)             ┃
┃   📈 clock     2870 MHz                    ┃
┃   🔌 power     20.5 W                      ┃
┃ ────────────────────────────────────────── ┃
┃ ▸ worker-4                                 ┃
┃   waiting                                  ┃
┃ ────────────────────────────────────────── ┃
┃ ▸ history                                  ┃
┃   CPU  ▃▃▄▄▄▅▅▅▅▆▆▆▆▇                      ┃
┃   temp ▄▄▄▄▄▄▄▅▅▅▅▅▅▅                      ┃
┃   GPU  ▅▅▅▅▅▅▅▆▆▆▆▆▆▆                      ┃
┃ ▸ events                                   ┃
┃ [10:01:00] 🚀 gpu-burn performance: 1086   ┃
┃ Gflop/s · errors 0                         ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ↑↓ test  j/k scroll  x stop  e screen  q   ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

</details>

### Cluster dashboard (key `D`)
Three layouts by the width. Up to 90 columns:
```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ github.com/doctorxcz/k3s-cluster-stress-test ━┓
┃ ◖●▲●◗ 📊 CLUSTER DASHBOARD · 5 nodes · 5 Ready · 5 s · 19:25:41                  ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ❗ 1 pod(s) not running on 1 node(s)                                             ┃
┃ no test is running or planned                                                    ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃   NODE                        CPU          TEMP  RAM          GPU       PODS     ┃
┃ ▸ control-1 (M)               █░░░░░  22%   48°  ██░░░░  40%  –         2        ┃
┃   worker-1                    █████░  91%   71°  ████░░  63%   59° 0%   2+1!     ┃
┃   worker-2                    ███░░░  47%   58°  ████░░  60%  –         2        ┃
┃   worker-3                    ░░░░░░   8%   44°  ████░░  63%  –         3        ┃
┃   gpu-worker-1                ████░░  63%   66°  ██░░░░  38%   59° 0%   2        ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ↑↓ Enter o / f v g l e ? q                                                       ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```
<details>
<summary>91 - 160 columns: more columns and the detail of the selected node</summary>

```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ github.com/doctorxcz/k3s-cluster-stress-test ━┓
┃ ◖●▲●◗ 📊 CLUSTER DASHBOARD · 5 nodes · 5 Ready · 5 s · 19:25:41                                                                  ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ❗ 1 pod(s) not running on 1 node(s)                                                                                             ┃
┃ no test is running or planned                                                                                                    ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃   NODE                         ROLE    CPU            TEMP  RAM            NET ↓↑       GPU                       PODS  STATE    ┃
┃ ▸ control-1 (M)                master  ██░░░░░░  22%   48°  ███░░░░░  40%  210k 150k    –                         2     ok       ┃
┃   worker-1                     worker  ███████░  91%   71°  █████░░░  63%  3.4M 1.2M    P620  59° 0% 3/2048M      2+1!  ok       ┃
┃   worker-2                     worker  ████░░░░  47%   58°  █████░░░  60%  800k 600k    –                         2     ok       ┃
┃   worker-3                     worker  █░░░░░░░   8%   44°  █████░░░  63%  110k 90k     –                         3     ok       ┃
┃   gpu-worker-1                 worker  █████░░░  63%   66°  ███░░░░░  38%  1.9M 750k    P620  59° 0% 3/2048M      2     ok       ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ▸ control-1                                                                                                                      ┃
┃ CPU Intel Core i5-10500T @ 2.3 GHz · 6 cores / 12 threads (hyper-threading)   load 0.50 0.40 0.30                                ┃
┃ RAM 3100/7800 MiB (40%)                                                                                                          ┃
┃ OS Ubuntu 24.04 · kernel 6.8.0 · amd64 · containerd://1.7 · v1.30.4+k3s1                                                         ┃
┃ IP 10.0.0.11   up 168 h                                                                                                          ┃
┃ temps CPU 48°C  NVMe 41°C   thermal throttle events 0                                                                            ┃
┃ power 14.0 W (CPU package)                                                                                                       ┃
┃ NIC eno1 1000 Mbit/s up  ↓ 210k/s  ↑ 150k/s                                                                                      ┃
┃ disk sda 238 GB SSD/NVMe Patriot P210                                                                                            ┃
┃ ping worker-1 0.4ms  worker-2 0.5ms                                                                                              ┃
┃ pods 2 running · 0 restarts · requests 350 m CPU / 384 MiB                                                                       ┃
┃ state ok   last test CPU 10m OK 55°                                                                                              ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ↑↓ select  Tab detail  o sort  / filter  f show  v view  g graph  l logs  e screen  ? help  q back                               ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

</details>

<details>
<summary>Over 160 columns: everything the tool can find (hardware, Kubernetes, pods, cluster, problems, tests)</summary>

```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ github.com/doctorxcz/k3s-cluster-stress-test ━┓
┃ ◖●▲●◗ 📊 CLUSTER DASHBOARD · 5 nodes · 5 Ready · 5 s · 19:25:41                                                                                                          ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ❗ 1 pod(s) not running on 1 node(s)                                                                                                                                     ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃   NODE                             ROLE    CPU              CPU ▸ 60 s      TEMP  TEMP ▸ 60 s     RAM              NET ↓↑       GPU                       PODS  STATE    ┃
┃ ▸ control-1 (M)                    master  ██░░░░░░░░  22%  ▁▁▁▁▁▁▁▁▁▁▁▁▁▁   48°  ▄▄▄▄▄▄▄▄▄▄▄▄▄▄  ████░░░░░░  40%  210k 150k    –                         2     ok       ┃
┃   worker-1                         worker  █████████░  91%  ▇▇▇▇▇▇▇▇▇▇▇▇▇▇   71°  ▄▄▄▄▄▄▄▄▄▄▄▄▄▄  ██████░░░░  63%  3.4M 1.2M    P620  59° 0% 3/2048M      2+1!  ok       ┃
┃   worker-2                         worker  █████░░░░░  47%  ▄▄▄▄▄▄▄▄▄▄▄▄▄▄   58°  ▃▃▃▃▃▃▃▃▃▃▃▃▃▃  ██████░░░░  60%  800k 600k    –                         2     ok       ┃
┃   worker-3                         worker  █░░░░░░░░░   8%  ▁▁▁▁▁▁▁▁▁▁▁▁▁▁   44°  ▂▂▂▂▂▂▂▂▂▂▂▂▂▂  ██████░░░░  63%  110k 90k     –                         3     ok       ┃
┃   gpu-worker-1                     worker  ██████░░░░  63%  ▅▅▅▅▅▅▅▅▅▅▅▅▅▄   66°  ▄▄▄▄▄▄▄▄▄▄▄▄▄▄  ████░░░░░░  38%  1.9M 750k    P620  59° 0% 3/2048M      2     ok       ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ▸ control-1 · hardware                                                              │ ▸ control-1 · pods (2)                                                             ┃
┃ machine HP ProDesk 400 G6 DM                                                        │ default        web-7d9f0                      1/1  Running            ↻0   3 d 0 … ┃
┃ BIOS American Megatrends 02.18.00 (2021-11-03)                                      │ monitoring     metrics-agent-0                1/1  Running            ↻0   20 d 0… ┃
┃ CPU Intel Core i5-10500T @ 2.3 GHz · 6 cores / 12 threads (hyper-threading)         │                                                                                    ┃
┃ governor powersave · 800-4600 MHz (hardware max 4600 MHz) · turbo on                │ ▸ cluster                                                                          ┃
┃ core clocks 2.3 2.3 2.4 2.4 2.5 2.5 2.5 2.6 GHz                                     │ nodes 5/5 Ready · 1 master · 0 cordoned · k3s v1.30.4+k3s1                         ┃
┃ core temps C0 45°  C1 46°  C2 47°  C3 48°                                           │ capacity 38 logical CPUs · 78.7 GiB RAM · 2 GPU · 12/550 pods                      ┃
┃ sensors NVMe 41°  acpitz 42°                                                        │ requested CPU 2350 m of 38000 m (6%) · RAM 3072 Mi of 80600 Mi (4%)                ┃
┃ thermal throttle events 0                                                           │ namespaces 5 · kube-system(11) default(6) monitoring(5) ingress(1) batch(1)        ┃
┃ power 14.0 W (CPU package)                                                          │ services 11 · ClusterIP 8 NodePort 2 LoadBalancer 1 · ingress 2 · configmaps 19    ┃
┃ load 0.50 0.40 0.30 · tasks 210 · open files 3200                                   │ storage PVC Bound 3 · 60 GiB requested · PV 3                                      ┃
┃ pressure (stall avg10) cpu some 0.1%  memory some 0.0%  io some 0.3%                │ jobs active 0 · failed 1 · done 12 · cronjobs 2                                    ┃
┃ RAM 3100/7800 MiB (40%) · free 900 · buffers 120 · cached 2400 · dirty 3 · slab 31… │ workloads deployments 5/5 · statefulsets 1/1 · daemonsets 5/5                      ┃
┃ disk sda 238 GB SSD/NVMe Patriot P210 · read 1.2M/s write 800k/s                    │ API server 18 ms (/readyz)                                                         ┃
┃ NIC eno1 1000 Mbit/s ? up mtu ? · ↓ 210k/s ↑ 150k/s · no errors                     │ warnings BackOff ×4 · FailedMount ×1                                               ┃
┃ ping worker-1 0.4ms  worker-2 0.5ms                                                 │                                                                                    ┃
┃                                                                                     │ ▸ problems                                                                         ┃
┃ ▸ control-1 · Kubernetes                                                            │ ✗ batch/report-job-5kq                 CrashLoopBackOff     ↻7 worker-1            ┃
┃ conditions Ready True  Memory False  Disk False  PID False                          │ most restarts report-job-5kq ↻7  db-0 ↻1                                           ┃
┃ schedulable yes   taints node-role.kubernetes.io/control-plane:NoSchedule           │ 10:41:07 BackOff Pod/report-job-5kq Back-off restarting failed container           ┃
┃ labels arch=amd64 os=linux control-plane                                            │ 10:12:40 FailedMount Pod/db-0 MountVolume.SetUp failed (retrying)                  ┃
┃ system Ubuntu 24.04 · kernel 6.8.0 · amd64 · containerd://1.7 · v1.30.4+k3s1        │                                                                                    ┃
┃ addresses InternalIP 10.0.0.11 Hostname control-1 · node age 90 d 0 h               │ ▸ tests                                                                            ┃
┃ images 14 cached, 2.1 GB                                                            │ no test is running or planned                                                      ┃
┃ CPU  alloc 12000 m   req 350 m (3%)     limits 700 m     use 22% (2640 m)           │ control-1                CPU 10m OK 55°                                            ┃
┃ RAM  alloc 7600 Mi   req 384 Mi (5%)    limits 768 Mi    use 40% (3100 Mi)          │ worker-1                 stepped OK 71°  ✓ baseline                                ┃
┃ pods 2 of 110 · running 2 · not running 0 · restarts 0 · ephemeral 196 GiB          │ worker-2                 CPU 5m OK 62°                                             ┃
┃ last test CPU 10m OK 55°   no baseline                                              │ worker-3                 CPU 5m OK 49°                                             ┃
┃                                                                                     │ gpu-worker-1             GPU 2m OK 68°  ✓ baseline                                 ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ↑↓ select  Tab detail  o sort  / filter  f show  v view  g graph  l logs  e screen  ? help  q back                                                                       ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

</details>

<details>
<summary>Views (`v`), sorting (`o`) and filters (`/`, `f`): temperatures sorted, the network view, only the nodes with problems</summary>

```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ github.com/doctorxcz/k3s-cluster-stress-test ━┓
┃ ◖●▲●◗ 📊 CLUSTER DASHBOARD · 5 nodes · 5 Ready · 5 s · 19:25:41                                                                  ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ❗ 1 pod(s) not running on 1 node(s)                                                                                             ┃
┃ no test is running or planned                                                                                                    ┃
┃ sort: temp  view: temps   (c = clear)                                                                                            ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃   NODE                          CPU            TEMP  CORES max  NVMe  CLOCK   PWR    THROT  GPU °  TEMP ▸ history                ┃
┃ ▸ worker-1                      ███████░  91%   71°   71°        41°  3.6GHz  42 W   2       59°   ▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄              ┃
┃   gpu-worker-1                  █████░░░  63%   66°   66°        41°  3.4GHz  38 W   0       59°   ▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄              ┃
┃   worker-2                      ████░░░░  47%   58°   58°        41°  3.2GHz  22 W   0      –      ▃▃▃▃▃▃▃▃▃▃▃▃▃▃▃▃              ┃
┃   control-1 (M)                 ██░░░░░░  22%   48°   48°        41°  2.3GHz  14 W   0      –      ▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄              ┃
┃   worker-3                      █░░░░░░░   8%   44°   42°        41°  2.6GHz  10 W   0      –      ▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂              ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ▸ worker-1                                                                                                                       ┃
┃ CPU Intel Core i7-4790S @ 3.2 GHz · 4 cores / 8 threads (hyper-threading)   load 0.50 0.40 0.30                                  ┃
┃ RAM 14800/23400 MiB (63%)                                                                                                        ┃
┃ OS Ubuntu 24.04 · kernel 6.8.0 · amd64 · containerd://1.7 · v1.30.4+k3s1                                                         ┃
┃ IP 10.0.0.12   up 288 h                                                                                                          ┃
┃ temps CPU 71°C  NVMe 41°C   thermal throttle events 2                                                                            ┃
┃ power 41.5 W (CPU package)                                                                                                       ┃
┃ GPU Quadro P620 · driver 580.178.04 · 59°C · 0% · 1354 MHz · VRAM 3/2048 MiB · power N/A · fan 40%                               ┃
┃ NIC eno1 1000 Mbit/s up  ↓ 3.4M/s  ↑ 1.2M/s                                                                                      ┃
┃ disk sda 238 GB SSD/NVMe Patriot P210                                                                                            ┃
┃ ping control-1 0.3ms  worker-2 0.4ms                                                                                             ┃
┃ pods 2 running, 1 not running · 7 restarts · requests 450 m CPU / 512 MiB                                                        ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ↑↓ select  Tab detail  o sort  / filter  f show  v view  g graph  l logs  e screen  ? help  q back                               ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ github.com/doctorxcz/k3s-cluster-stress-test ━┓
┃ ◖●▲●◗ 📊 CLUSTER DASHBOARD · 5 nodes · 5 Ready · 5 s · 19:25:41                                                                  ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ❗ 1 pod(s) not running on 1 node(s)                                                                                             ┃
┃ no test is running or planned                                                                                                    ┃
┃ view: net   (c = clear)                                                                                                          ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃   NODE                          IP               LINK     NET ↓↑       PING    LOST  NET ▸ history                               ┃
┃ ▸ control-1 (M)                 10.0.0.11        1000M    210k 150k    0.5ms   0     ▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂▂                            ┃
┃   worker-1                      10.0.0.12        1000M    3.4M 1.2M    0.3ms   0     ▃▃▃▃▃▃▃▃▃▃▃▃▃▃▃▄                            ┃
┃   worker-2                      10.0.0.13        1000M    800k 600k    0.5ms   0     ▅▆▆▆▆▆▆▆▆▆▆▆▆▆▆▇                            ┃
┃   1-3 of 5  (↑↓ PgUp PgDn)                                                                                                       ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ▸ control-1                                                                                                                      ┃
┃ CPU Intel Core i5-10500T @ 2.3 GHz · 6 cores / 12 threads (hyper-threading)   load 0.50 0.40 0.30                                ┃
┃ RAM 3100/7800 MiB (40%)                                                                                                          ┃
┃ OS Ubuntu 24.04 · kernel 6.8.0 · amd64 · containerd://1.7 · v1.30.4+k3s1                                                         ┃
┃ IP 10.0.0.11   up 168 h                                                                                                          ┃
┃ temps CPU 48°C  NVMe 41°C   thermal throttle events 0                                                                            ┃
┃ power 14.0 W (CPU package)                                                                                                       ┃
┃ NIC eno1 1000 Mbit/s up  ↓ 210k/s  ↑ 150k/s                                                                                      ┃
┃ disk sda 238 GB SSD/NVMe Patriot P210                                                                                            ┃
┃ ping worker-1 0.4ms  worker-2 0.5ms                                                                                              ┃
┃ pods 2 running · 0 restarts · requests 350 m CPU / 384 MiB                                                                       ┃
┃ state ok   last test CPU 10m OK 55°                                                                                              ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ↑↓ select  Tab detail  o sort  / filter  f show  v view  g graph  l logs  e screen  ? help  q back                               ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ github.com/doctorxcz/k3s-cluster-stress-test ━┓
┃ ◖●▲●◗ 📊 CLUSTER DASHBOARD · 1/5 nodes · 5 Ready · 5 s · 19:25:41                                                    ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ❗ 1 pod(s) not running on 1 node(s)                                                                                 ┃
┃ no test is running or planned                                                                                        ┃
┃ show: problems   (c = clear)                                                                                         ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃   NODE                          ROLE    CPU            TEMP  RAM            GPU                       PODS  STATE    ┃
┃ ▸ worker-1                      worker  ███████░  91%   71°  █████░░░  63%  P620  59° 0% 3/2048M      2+1!  ok       ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ▸ worker-1                                                                                                           ┃
┃ CPU Intel Core i7-4790S @ 3.2 GHz · 4 cores / 8 threads (hyper-threading)   load 0.50 0.40 0.30                      ┃
┃ RAM 14800/23400 MiB (63%)                                                                                            ┃
┃ OS Ubuntu 24.04 · kernel 6.8.0 · amd64 · containerd://1.7 · v1.30.4+k3s1                                             ┃
┃ IP 10.0.0.12   up 288 h                                                                                              ┃
┃ temps CPU 71°C  NVMe 41°C   thermal throttle events 2                                                                ┃
┃ power 41.5 W (CPU package)                                                                                           ┃
┃ GPU Quadro P620 · driver 580.178.04 · 59°C · 0% · 1354 MHz · VRAM 3/2048 MiB · power N/A · fan 40%                   ┃
┃ NIC eno1 1000 Mbit/s up  ↓ 3.4M/s  ↑ 1.2M/s                                                                          ┃
┃ disk sda 238 GB SSD/NVMe Patriot P210                                                                                ┃
┃ ping control-1 0.3ms  worker-2 0.4ms                                                                                 ┃
┃ pods 2 running, 1 not running · 7 restarts · requests 450 m CPU / 512 MiB                                            ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ↑↓ select  Tab detail  o sort  / filter  f show  v view  g graph  l logs  e screen  ? help  q back                   ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

</details>

<details>
<summary>Pods of a node (`l`) and the keys (`?`)</summary>

```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ github.com/doctorxcz/k3s-cluster-stress-test ━┓
┃ ◖●▲●◗ 📜 control-1 · pods (2)                                                                              ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃   NAMESPACE       POD                                     PHASE             RESTARTS                       ┃
┃ ▸ default         web-7d9f0                               Running           0                              ┃
┃   monitoring      metrics-agent-0                         Running           0                              ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ↑↓ pod   Enter = log   q back                                                                              ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ github.com/doctorxcz/k3s-cluster-stress-test ━┓
┃ ◖●▲●◗ 📊 DASHBOARD · keys                                                                                  ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ↑ ↓  PgUp PgDn  Home End select a node                                                                     ┃
┃ Tab                      next tab of the detail (overview · pods · events); in a wide window the pods /    ┃
┃                          events panel                                                                      ┃
┃ Enter                    narrow window: the detail page of the node                                        ┃
┃ o                        sort: default · temp · cpu · ram · pods · gpu                                     ┃
┃ /                        filter by a part of the node name (Enter keeps it, Esc clears it)                 ┃
┃ f                        show: all · problems · workers · gpu                                              ┃
┃ v                        view of the table: overview · temps · net · disks · gpu                           ┃
┃ c                        clear sort, filter, show and view                                                 ┃
┃ g                        graphs: the history of the selected node. Inside: [ ] window · ← → time · , .     ┃
┃                          cursor · b Braille dots · a area · t save as text · s save CSV+JSON (in           ┃
┃                          scr/graphs/) · x combined                                                         ┃
┃   x (in graphs)          all lines in ONE chart, each scaled 0-100 % of its own range - to see what rises  ┃
┃                          or falls at the same time (the numbers are in the legend)                         ┃
┃ l                        pods of the selected node, Enter = the end of the log of the pod (read only)      ┃
┃ e                        SCREEN: saves what you see as a text file to the folder scr/                      ┃
┃ + -                      refresh interval 0.5 · 1 · 2 · 5 · 10 · 30 s                                      ┃
┃ p                        pause                                                                             ┃
┃ r                        refresh now                                                                       ┃
┃ ?                        this help                                                                         ┃
┃ q / Esc                  back (closes a page first)                                                        ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ any key = back                                                                                             ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

</details>

<details>
<summary>A narrow but tall window opens the cluster, problems and the other nodes under the table</summary>

```text
┏━━━━━━━━━━━━━ github.com/doctorxcz/k3s-cluster-stress-test ━┓
┃ ◖●▲●◗ 📊 DASHBOARD · 5 nodes · 5 Ready · 5 s · 19:25:41    ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ❗ 1 pod(s) not running on 1 node(s)                       ┃
┃ no test is running or planned                              ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃   NODE                      CPU       TEMP  RAM       PODS ┃
┃ ▸ control-1 (M)             █░░  22%   48°  █░░  40%  2    ┃
┃   worker-1                  ███  91%   71°  ██░  63%  2+1! ┃
┃   worker-2                  █░░  47%   58°  ██░  60%  2    ┃
┃   worker-3                  ░░░   8%   44°  ██░  63%  3    ┃
┃   gpu-worker-1              ██░  63%   66°  █░░  38%  2    ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ▸ control-1                                                ┃
┃ CPU Intel Core i5-10500T @ 2.3 GHz · 6 cores / 12 threads  ┃
┃   (hyper-threading) load 0.50 0.40 0.30                    ┃
┃ RAM 3100/7800 MiB (40%)                                    ┃
┃ OS Ubuntu 24.04 · kernel 6.8.0 · amd64 · containerd://1.7  ┃
┃   · v1.30.4+k3s1                                           ┃
┃ IP 10.0.0.11   up 168 h                                    ┃
┃ temps CPU 48°C  NVMe 41°C   thermal throttle events 0      ┃
┃ power 14.0 W (CPU package)                                 ┃
┃ NIC eno1 1000 Mbit/s up  ↓ 210k/s  ↑ 150k/s                ┃
┃ disk sda 238 GB SSD/NVMe Patriot P210                      ┃
┃ ping worker-1 0.4ms  worker-2 0.5ms                        ┃
┃ pods 2 running · 0 restarts · requests 350 m CPU / 384 MiB ┃
┃ state ok   last test CPU 10m OK 55°                        ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ▸ pods · control-1                                         ┃
┃ default web-7d9f0 Running ↻0                               ┃
┃ monitoring metrics-agent-0 Running ↻0                      ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ▸ cluster                                                  ┃
┃ nodes 5/5 Ready · 1 master · 0 cordoned · k3s v1.30.4+k3s1 ┃
┃ capacity 38 logical CPUs · 78.7 GiB RAM · 2 GPU · 12/550   ┃
┃   pods                                                     ┃
┃ requested CPU 2350 m of 38000 m (6%) · RAM 3072 Mi of      ┃
┃   80600 Mi (4%)                                            ┃
┃ namespaces 5 · kube-system(11) default(6) monitoring(5)    ┃
┃   ingress(1) batch(1)                                      ┃
┃ services 11 · ClusterIP 8 NodePort 2 LoadBalancer 1 ·      ┃
┃   ingress 2 · configmaps 19                                ┃
┃ storage PVC Bound 3 · 60 GiB requested · PV 3              ┃
┃ jobs active 0 · failed 1 · done 12 · cronjobs 2            ┃
┃ workloads deployments 5/5 · statefulsets 1/1 · daemonsets  ┃
┃   5/5                                                      ┃
┃ API server 18 ms (/readyz)                                 ┃
┃ warnings BackOff ×4 · FailedMount ×1                       ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ▸ problems                                                 ┃
┃ ✗ batch/report-job-5kq CrashLoopBackOff ↻7 worker-1        ┃
┃ most restarts report-job-5kq ↻7  db-0 ↻1                   ┃
┃ 10:41:07 BackOff Pod/report-job-5kq Back-off restarting    ┃
┃   failed container                                         ┃
┃ 10:12:40 FailedMount Pod/db-0 MountVolume.SetUp failed     ┃
┃   (retrying)                                               ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ▸ tests                                                    ┃
┃ no test is running or planned                              ┃
┃ control-1                CPU 10m OK 55°                    ┃
┃ worker-1                 stepped OK 71°  ✓ baseline        ┃
┃ worker-2                 CPU 5m OK 62°                     ┃
┃ worker-3                 CPU 5m OK 49°                     ┃
┃ gpu-worker-1             GPU 2m OK 68°  ✓ baseline         ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ↑↓ Enter o / f v g l e ? q                                 ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

</details>

### Dashboard graphs (key `g`)
```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ github.com/doctorxcz/k3s-cluster-stress-test ━┓
┃ ◖●▲●◗ 📈 control-1 · history · 20 min · line · 5 s                                                   ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ■ CPU %  now 2   min 2   max 52   avg 25                                                             ┃
┃   100┤                                                                      │                        ┃
┃      │      ▁                       ▁▁                       ▁              │         ▁              ┃
┃      │ ▂▄▆▇███▇▆▄▂              ▃▅▇████▇▅▃▁             ▁▄▆▇████▆▄▂         │    ▂▄▆████▇▆▄▁         ┃
┃     0┤████████████▇▅▃▁▁▁▁▁▁▁▁▄▆████████████▆▄▂▁▁▁▁▁▁▁▂▅▇████████████▅▃▁▁▁▁▁▁▁▁▃▅████████████▇▄▂▁▁▁▁▁ ┃
┃ ■ TEMP °C  now 53   min 40   max 56   avg 48                                                         ┃
┃    90┤┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄ ┃
┃      │                                                                      │                        ┃
┃      │▃▄▅▅▆▆▆▇▇▇▇▇▇▇▇▇▆▆▅▅▄▄▃▃▂▂▁▁           ▁▁▂▂▃▃▄▄▅▅▆▆▇▇▇▇▇▇▇▇▇▆▆▆▅▅▄▃▃▂▂▁▁           ▁▁▁▂▃▃▄▄▅▅▆ ┃
┃    25┤████████████████████████████████▇▇▇█████████████████████████████████████████▇▇▇▇██████████████ ┃
┃ ■ RAM %  now 38   min 34   max 46   avg 40                                                           ┃
┃   100┤┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄ ┃
┃      │                                                                      │                        ┃
┃      │▅▅▅▅▅▅▆▆▆▆▆▆▆▆▆▆▆▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▆▆▆▆▆▆▆▆▆▆▆▅▅▅▅▅▅▅▅▄▄▄▄▄▄▄▄▄▃▃▃▃▃▃▃▃▃▃▃▃▃▃▃▃▃▃▃▃▃▃▃▃▃▃▄▄▄▄▄▄▄ ┃
┃     0┤██████████████████████████████████████████████████████████████████████████████████████████████ ┃
┃ ■ CLOCK MHz  now 2261   min 2261   max 2300   avg 2280                                               ┃
┃  2530┤▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅▅ ┃
┃      │██████████████████████████████████████████████████████████████████████████████████████████████ ┃
┃      │██████████████████████████████████████████████████████████████████████████████████████████████ ┃
┃     0┤██████████████████████████████████████████████████████████████████████████████████████████████ ┃
┃ ■ POWER W  now 10   min 10   max 18   avg 14                                                         ┃
┃    20┤  ▁▂▃▄▄▅▅▅▅▄▄▃▂▁                    ▁▂▃▄▄▅▅▅▅▅▄▃▂▁                    │▁▂▃▄▅▅▅▅▅▄▄▃▂▁          ┃
┃      │▇████████████████▇▆▄▃▂▂▁    ▁▁▂▃▄▅▆▇███████████████▇▆▅▄▃▂▁▁   ▁▁▂▃▄▅▆▇████████████████▇▅▄▃▂▁▁  ┃
┃      │██████████████████████████████████████████████████████████████████████████████████████████████ ┃
┃     0┤██████████████████████████████████████████████████████████████████████████████████████████████ ┃
┃ ■ NET ↓+↑ B/s  now 207k   min 200k   max 1.8M   avg 1.0M                                             ┃
┃  2.0M┤   ▃▅▅▄▂              ▁▄▅▅▄▁              ▂▄▅▅▃               ▃▅▅▄▂   │          ▁▄▅▅▄▁        ┃
┃      │▁▅██████▇▃          ▂▆██████▆▂          ▃▇██████▄          ▁▅██████▇▃ │        ▂▆██████▆▂      ┃
┃      │██████████▇▃      ▂▆██████████▆▂      ▃▇██████████▄▁     ▁▅██████████▇▃      ▂▆██████████▆▂    ┃
┃     0┤████████████▇▅▃▃▅▇██████████████▇▄▃▄▅███████████████▆▄▃▄▆███████████████▅▄▃▄▇██████████████▇▄▃ ┃
┃      └────────────────────────────────────────────────────────────────────────────────────────────── ┃
┃       -20 min                                     -10 min                                        now ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ⏱ 19:20:40  CPU 2%  TEMP 44°C  RAM 34%  CLOCK 2281MHz  POWER 15W  NET ↓+↑ 628kB/s                    ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ↑↓ node  [ ] window  ← → time  , . cursor  b dots  x combined  a area  t text  s data  e screen  q   ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```
<details>
<summary>Braille dots (`b`) and the combined graph (`x`)</summary>

```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ github.com/doctorxcz/k3s-cluster-stress-test ━┓
┃ ◖●▲●◗ 📈 control-1 · history · 20 min · line · 5 s                                                   ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ■ CPU %  now 2   min 2   max 52   avg 25                                                             ┃
┃   100┤    ⢀⣀⣀⣀                     ⣀⣀⣀⣀                     ⣀⣀⣀⡀                    ⢀⣀⣀⣀             ┃
┃     0┤⠒⠚⠋⠉⠉⠁ ⠈⠉⠉⠙⠒⠲⠤⣤⣀⣀⣀⣀⣀⣀⣀⣠⠤⠴⠒⠚⠉⠉⠉  ⠉⠉⠉⠓⠒⠦⠤⣄⣀⣀⣀⣀⣀⣀⣀⣤⠤⠖⠒⠋⠉⠉⠁ ⠈⠉⠉⠙⠓⠲⠤⢤⣀⣀⣀⣀⣀⣀⣀⣠⡤⠴⠖⠚⠋⠉⠉  ⠈⠉⠉⠛⠒⠶⠤⣄⣀⣀⣀⣀⣀ ┃
┃ ■ TEMP °C  now 53   min 40   max 56   avg 48                                                         ┃
┃    90┤⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂ ┃
┃    25┤⠋⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠓⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠚⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠛⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠚⠋⠉⠉⠉⠉ ┃
┃ ■ RAM %  now 38   min 34   max 46   avg 40                                                           ┃
┃   100┤⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂⠂ ┃
┃     0┤⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠉⠙⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠋⠉⠉⠉⠉⠉⠉⠉ ┃
┃ ■ CLOCK MHz  now 2261   min 2261   max 2300   avg 2280                                               ┃
┃  2530┤⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒ ┃
┃     0┤                                                                                               ┃
┃ ■ POWER W  now 10   min 10   max 18   avg 14                                                         ┃
┃    20┤⠤⠤⠶⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠦⠤⠤⠤⣄⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣠⡤⠤⠤⠴⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠲⠤⠤⠤⢤⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⡤⠤⠤⠤⠖⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠒⠦⠤⠤⠤⣄⣀⣀⣀⣀⣀ ┃
┃     0┤                                                                                               ┃
┃ ■ NET ↓+↑ B/s  now 207k   min 200k   max 1.8M   avg 1.0M                                             ┃
┃  2.0M┤⣀⡤⠴⠒⠒⠒⠒⠲⢤⣄⡀        ⢀⣠⡤⠖⠒⠒⠒⠒⠦⢤⣀         ⣀⣠⠴⠖⠒⠒⠒⠲⠦⣄⣀         ⣀⡤⠴⠒⠒⠒⠒⠲⢤⣄⡀        ⢀⣠⡤⠖⠒⠒⠒⠒⠶⢤⣀      ┃
┃     0┤          ⠉⠓⠶⠤⠤⠤⠤⠖⠚⠉         ⠈⠙⠓⠦⠤⠤⠤⠴⠖⠛⠉         ⠉⠙⠲⠦⠤⠤⠤⠴⠚⠋⠁         ⠉⠓⠲⠤⠤⠤⠤⠶⠚⠉⠁        ⠈⠙⠓⠦⠤⠤ ┃
┃      └────────────────────────────────────────────────────────────────────────────────────────────── ┃
┃       -20 min                                     -10 min                                        now ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ cursor: , and . move it, c hides it                                                                  ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ↑↓ node  [ ] window  ← → time  , . cursor  b dots  x combined  a area  t text  s data  e screen  q   ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ github.com/doctorxcz/k3s-cluster-stress-test ━┓
┃ ◖●▲●◗ 📈 control-1 · history · 20 min · line · 5 s                                                   ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ each line has its own scale (0-100 % of its range), the numbers are in the legend                    ┃
┃ ■ CPU %  now 2   min 2   max 52   avg 25                                                             ┃
┃ ■ TEMP °C  now 53   min 40   max 56   avg 48                                                         ┃
┃ ■ CLOCK MHz  now 2261   min 2261   max 2300   avg 2280                                               ┃
┃ ■ POWER W  now 10   min 10   max 18   avg 14                                                         ┃
┃   max┤                                                                                               ┃
┃      │⠒⠲⠦⠖⠒⣦⡶⠖⠶⠶⠶⢶⣴⠒⠶⠴⠒⠲⠴⠖⠲⠤⠖⠲⠦⠖⠒⠦⠖⠒⠦⠶⠒⠦⠴⠒⠶⠴⠒⣲⣴⠶⠲⠶⠶⠶⣦⡖⠲⠦⠖⠒⠦⠶⠒⠦⠶⠒⠶⠴⠒⠶⠴⠒⠲⠴⠖⠲⠤⠖⠲⠦⠖⢒⣦⠶⠖⠶⠶⠶⢦⣴⠒⠶⠴⠒⠲⠴⠒⠲⠴⠖⠲⠦ ┃
┃      │  ⣀⡴⠋⠁      ⠉⠳⣦⡀                    ⢀⣤⠞⠉      ⠈⠙⢶⣄                     ⣠⡴⠋⠁      ⠉⠳⣤⡀          ┃
┃      │⣠⡾⠋           ⠈⠙⢦⡀                ⢀⣴⠟⠁           ⠙⢳⣄                 ⣠⡾⠋           ⠈⠻⣦⡀        ┃
┃      │⠉                ⠙⢦⡀            ⢀⣴⠟⠁               ⠉⢷⣄             ⣠⡾⠋               ⠈⠻⣦⡀      ┃
┃      │                   ⠙⢦⣀        ⢀⡴⠛⠁                   ⠈⠳⣄⡀        ⣠⠞⠋                   ⠈⠛⢦⡀    ┃
┃      │    ⢀⣠⣤⣀             ⠉⠳⢦⣄⣀⣀⣠⡤⣞⣯⣄⣀                     ⣀⣬⣟⡶⣤⣀⣀⣀⣤⠴⠛⠁            ⢀⣠⢤⣀        ⠙⠳⢤⣄ ┃
┃      │   ⣴⢏⣁⡤⠼⠷⣖⠒⠒⠒⠒⠶⠤⣤⣀         ⢀⡾⠋  ⠙⢷⡀             ⢀⣀⣤⠤⣶⠞⠓⠒⠚⠻⣶⠦⢤⣀⡀             ⢀⣴⠋  ⠈⢳⡄         ⣀ ┃
┃      │⣀⣤⡾⠛⠉    ⠙⣦      ⠈⠙⠓⠦⣄⡀   ⢠⡟      ⢻⡄        ⣀⣠⠴⠚⠉⠁ ⣴⠋     ⠘⢧⡀ ⠉⠓⠶⣤⣀        ⢀⡾⠁     ⠹⣆   ⢀⣠⡴⠞⠋⠁ ┃
┃      │⢁⡾⠁       ⠘⣧          ⠉⠛⠶⣤⣟        ⢻⡄   ⣀⡤⠶⠛⠉     ⣼⠃       ⠈⢷⡀    ⠈⠙⠲⢤⣀⡀  ⢠⡞        ⣹⣦⠴⠚⠉      ┃
┃      │⡾⠁         ⠘⣧           ⢠⡏⠈⠉⠓⠲⠦⠤⠤⠤⠤⠤⢿⡖⠚⠉⠁        ⣼⠃         ⠈⢷        ⠈⠉⠛⢒⡿⠤⠤⠤⠤⠤⠤⠖⠚⠋⠉⠹⣆        ┃
┃      │            ⠘⣧         ⢠⡟            ⢻⡄         ⣼⠃           ⠈⢧         ⢀⡾⠁           ⠹⣆       ┃
┃      │             ⠘⣦       ⢠⡟              ⢻⡄       ⣴⠃             ⠘⢧       ⢀⡾⠁             ⠹⣆      ┃
┃   min┤              ⠘⠧⠤⠤⠤⠤⠤⠤⠟                ⠻⠤⠤⠤⠤⠤⠤⠼⠃               ⠈⠷⠤⠤⠤⠤⠤⠤⠞⠁               ⠹⠦⠤⠤⠤⠤ ┃
┃      └────────────────────────────────────────────────────────────────────────────────────────────── ┃
┃       -20 min                                     -10 min                                        now ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ cursor: , and . move it, c hides it                                                                  ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ ↑↓ node  [ ] window  ← → time  , . cursor  b dots  x combined  a area  t text  s data  e screen  q   ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

</details>

<details>
<summary>`t` saves all the graphs as a plain text report (`scr/graphs/`), readable with `cat`, `less` or `batcat`</summary>

```text
GRAPH worker-1
saved 2026-10-01 19:25:41 · window 20 min (19:05:40 - 19:25:40) · interval 5 s · 1200 samples kept

■ CPU %  now 85   min 30   max 90   avg 65
  100┤     ▂▃▃▃▂                       ▂▃▃▃▂                       ▂▃▃▃▂                       ▂▃▃▃▂
     │ ▁▃▆███████▅▃                 ▃▆███████▆▃          ▁▂▅█   ▃▆███████▆▃                 ▃▆██████
   50┤▅█████████████▄▁           ▂▅█████████████▄▁   ▄█▇█████▂▅█████████████▅▂           ▂▅█████████
     │████████████████▆▃▁     ▂▄▇█████████████████▆▄▁█████████████████████████▆▄▁     ▂▄▇███████████
     │████████████████████▇▆▇█████████████████████████████████████████████████████▇▆▇███████████████
    0┤██████████████████████████████████████████████████████████████████████████████████████████████
     └──────────────────────────────────────────────────────────────────────────────────────────────
      -20 min                                     -10 min                                        now

  (… one graph for every quantity: TEMP, RAM, CLOCK, POWER, GPU, NET …)

TABLE (the average of every 57 s)
time           cpu %    temp °C      ram %  clock MHz    power W    net B/s
19:06:09          74         60         45       3575       43.0       1.7M
19:07:06          89         64         46       3570       45.1       1.0M
19:08:03          77         66         46       3573       45.0       276k
19:09:00          50         65         45       3572       42.6       777k
19:09:57          32         62         45       3570       39.6       1.7M
19:10:54          40         58         43       3573       37.7       1.4M
19:11:52          66         54         42       3568       38.2       461k
  …
```

</details>

### Lists to choose from (menu `7 DATA`, stop a test)
```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ github.com/doctorxcz/k3s-cluster-stress-test ━┓
┃ ◖●▲●◗ 📚 NODES AND LOGS · ★ = has a baseline                                           ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃ NODE          LOGS  NEWEST TEST               BASE  MAX                                ┃
┃ worker-1         2  2026-10-01 12:00 classic  ★      55°                               ┃
┃ worker-2         2  2026-10-01 11:00 classic         55°                               ┃
┃ worker-3         2  2026-10-01 10:00 stepped         61°                               ┃
┃ gpu-worker-1     2  2026-10-01 10:00 gpu             61°                               ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃  #  WHEN              NODE          TEST    MAX  RESULT                                ┃
┃  1  2026-10-01 12:00  worker-1      classic  55°  done                                 ┃
┃  2  2026-10-01 11:00  worker-2      classic  55°  stopped                              ┃
┃  3  2026-10-01 10:00  worker-1      classic  55°  done                                 ┃
┃  4  2026-10-01 10:00  worker-2      classic  61°  ?                                    ┃
┃  5  2026-10-01 10:00  worker-3      stepped  61°  ?                                    ┃
┃  6  2026-10-01 10:00  gpu-worker-1  gpu      61°  ?                                    ┃
┃  7  2026-09-30 09:00  gpu-worker-1  classic  55°  done                                 ┃
┃  8  2026-09-30 08:00  worker-3      classic  55°  done                                 ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```
<details>
<summary>Running tests to stop, and the nodes of the cluster</summary>

```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ github.com/doctorxcz/k3s-cluster-stress-test ━┓
┃ ◖●▲●◗ 🛑 RUNNING TESTS · pick one to stop                                              ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃  #  ID      WHAT                      LEFT                                             ┃
┃  1  aa11    CPU 2 min · worker-3      1:10 (fg)  PID 359559                            ┃
┃  2  bb22    GPU 2 min · gpu-worke…    1:50  PID 359559                                 ┃
┃  3  a4f2c1  gpu test · gpu-worker…    2:00  PID 359559                                 ┃
┃  4  b81e09  stepped test · worker…    2:00 (fg)  PID 359559                            ┃
┃  5  c77d12  classic test · worker…    2:00 (fg)  PID 359559                            ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

```text
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ github.com/doctorxcz/k3s-cluster-stress-test ━┓
┃ ◖●▲●◗ 🖥 NODES · pick a number or type a name                                           ┃
┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫
┃  #  NODE                      ROLE    STATE                                            ┃
┃  1  control-1                 master  Ready                                            ┃
┃  2  worker-1                  worker  Ready  🔥 71 °C                                  ┃
┃  3  worker-2                  worker  NOT Ready                                        ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛
```

</details>


## Requirements
- Python 3.9+ (tested on 3.12)
- `kubectl` with access to the cluster (k3s ships a symlink `/usr/local/bin/kubectl`)
- `metrics-server` (for the `kubectl top` fallback of the RAM test; default in k3s)
- a node with internet access (the pods pull the image and install `stress-ng` via `apt`)
- nothing to install on the nodes themselves: the tools the tests need (`stress-ng`, `fio`, `smartctl`, `iperf3`,
  `ping`, optionally `mtr`/`curl`) are installed by the pods with `apt` at run time and removed with the pod
- `--smart` and `--hw-privileged` start a **privileged** pod — the cluster must allow privileged pods
- network tests need open TCP/UDP ports 30000–32767 between the nodes (see the warning below)
- installation step by step: [`HELPDESK.md`](HELPDESK.md)

## Usage
From the project folder (e.g. `~/cluster-testing/python-stress-test-en`):

```bash
python3 -m stress_test                     # interactive, asks for everything
python3 -m stress_test --node worker-3 --time 300 --max-temp 80 --log
python3 -m stress_test --node worker-3 --non-interactive --time 120
python3 -m stress_test --help
```

| Option | Meaning |
|---|---|
| `--node NAME` | the node to test (otherwise it offers a list) |
| `--time DURATION` | test duration: seconds or `30s`, `5m`, `1h`, `1h30m` (no unit = seconds) |
| `--max-temp °C` | the test stops at this CPU temperature (2 readings in a row), 60–95, default 85 (not asked for the disk, network and GPU tests; the default guard stays) |
| `--cpu-load %` | CPU load, default 100 |
| `--ram-pct %` | enables the RAM test: how many % of **free** memory to allocate |
| `--hdd` / `--no-hdd` | stress the disk |
| `--log` / `--no-log` | save the test results to `logs/<node>-<duration>s-<date>.log` |
| `--log-dir DIR` | folder with the results (default `logs` in the project directory; a relative path is taken from the project, an absolute one is used unchanged). A run writes into a **day folder** inside it: `DIR/YYYY-MM-DD/` (created when it does not exist) |
| `--migrate-logs` | one-off: move old flat result / debug / pytest logs into day folders by their date |
| `-b`, `--background` | run the test **in the background** (see below), logging turns on by itself. Default: no |
| `--status` | shows tests running in the background and the tool's pods in the cluster |
| `--status --live` | the live screen of all running and planned tests (the same as key `W` in the menu), see below |
| `--stop ID\|NODE\|PID` | gracefully stops a test running in the background (pod cleanup) |
| `--compare LOG [LOG]` | compare two tests from logs (or just a node name = its two newest), see below |
| `--profile classic\|stepped\|spike\|disk\|net\|gpu` | classic test (default), stepped 25/50/75/100 % (always logged), spike (repeating jump between a low and a high load), disk benchmark (fio), network test (iperf3) or GPU test (gpu-burn, see the GPU section) |
| `--spike-target %` | spike test: target CPU load of the high phase (see `--help` for the allowed values) |
| `--spike-low-time DURATION` / `--spike-high-time DURATION` | spike test: length of the low (10 %) and the high phase in every cycle |
| `--net-time DURATION` | network test: length of one iperf3 test (5–60 s) |
| `--schedule TIME` | start the test later: `HH:MM` (nearest occurrence), a duration from now (`30m`, `2h`) or a date and time (`2026-10-03 02:00`, `3.10. 02:00`), typically with `-b` (see Scheduling) |
| `--persistent` | with `--schedule`: keep the plan as a systemd user timer that survives a restart of this computer |
| `--scheduled` | list the planned tests (waiting processes and systemd timers) and exit; cancel with `--stop ID` |
| `--dry-run` | only show what would be started (node, settings) and exit; nothing runs on the cluster |
| `--list-nodes` | list the cluster's nodes (role, state) and exit |
| `--list-gpus` | scan every node for graphics cards (read-only pods) and show which have a usable dedicated NVIDIA GPU, then exit |
| `--no-background` | run in the foreground (the opposite of `-b`) |
| `--steps 25,50,75,100` | stages of the stepped test in % |
| `--step-time DURATION` | length of one stage (default 3 min) |
| `--workers` / `--cluster` / `--nodes a,b` | test several nodes one after another (workers / the whole cluster / a list) |
| `--parallel` / `--no-parallel` | workers at once (master alone afterwards) / one after another; without a flag a question is asked |
| `--start-mode rolling\|sync` | with `--parallel`: `rolling` (default) every node starts its load as soon as it is ready and runs the full time from its own start; `sync` all nodes wait until every one is ready and start together |
| `--ready-timeout SEC` | with `--start-mode sync`: how long to wait until all nodes are ready (default 900); a node that is late is left out |
| `--self-test` | the FULL self-test of the whole cluster (see above); `--self-test-level quick\|standard\|thorough`, `--self-test-ack` (the second confirmation for scripts) |
| `--net-extra LIST` | network test extras: `dns`, `internet`, `mtr`, `service`, `all`, and `no-udp` (leave the UDP test out) |
| `--api-limit SEC` | when running in parallel: an API response slower than this many seconds (2× in a row) stops all tests |
| `--include-master` | also include the master with `--workers`; in the stepped test without a question |
| `--cooldown DURATION` | after the test keep measuring temperature and clock (default 60 s, `0` = off, max 600 s; the network and disk tests have none unless you ask) |
| `--notes TEXT` | a note for the log |
| `--force` | bypass the master protection (`FORCE=1 python3 -m stress_test` works too) |
| `-y`, `--yes` | confirm questions automatically (e.g. for the master) |
| `--allow-no-sensor` | continue even without a CPU temperature sensor |
| `--no-hw` | do not detect node hardware (the `hw-info` pod is not started) |
| `--hw-privileged` | the `hw-info` pod runs privileged (to list RAM modules via `dmidecode`); the default is without `privileged`. The hardware list also shows the GPU (`lspci`) and the disks (`lsblk`) |
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
  Node:          worker-1
  Test duration: 1 h (3600 s)  (+ 1–3 min preparation)
  Run id / PID:  728ade / 1025
  Results:       .../logs/worker-1-3600s-<date>.log
  Live output:   tail -f .../logs/worker-1-3600s-<date>.console.txt
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
- **Throttling** (only when the CPU also got warm: a lower clock while the CPU stays below 65 °C is reported as "not thermal - power saving / governor") is judged from the clock under full load (utilisation above 80 %): the average of the first
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
./stress.sh --compare worker-1            # the two newest tests of the node (older = A, newer = B)
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

## Export and baseline (since 1.11.0)
Every logged test also produces a machine-readable `<log>.json` next to the log (`--export json|csv|both|none`,
csv holds only the measurements: `t, phase, stage, cpu_temp, freq_mhz, cpu_pct, mem_used_mib, mem_used_pct`).
Older logs: `./stress.sh --export-log worker-1` (a log or a node = its newest log).

Baseline = a saved reference result of a node:
```bash
./stress.sh --set-baseline worker-1     # the newest test of the node becomes its baseline
```
At the end of every later test of that node a `BASELINE CHECK` is printed: max temperature, average clock,
performance and throttling against the baseline, with the verdict `OK` or `REGRESSION` (limits: +5 °C,
-5 % clock, -5 % performance). Different profile or stages = "not comparable". `--no-baseline-check` skips it.
The baselines are in `logs/baselines/<node>.json`.

## CPU power, disk health and disk benchmark (since 1.12.0)
- **Power:** on Intel machines the measurement line ends with `| Power: 17.3 W` (RAPL, no privileged mode).
  The log header and summary show the PL1/PL2 limits; if the average power sits at PL1, the summary says the
  clock is limited by power. Not available on AMD/ARM (nothing is printed).
- **`--smart`** (preflight, PRIVILEGED pod): reads SMART of all disks. A failed health check refuses the test,
  `--allow-bad-disk` tests anyway. Reallocated sectors, NVMe wear >= 90 %, media errors and a hot disk are warnings.
- **`--profile disk`** (menu item 2): an fio benchmark instead of a CPU load - sequential and random read/write.
  `--disk-size MiB`, `--disk-job-time`. The file lives in an emptyDir on the node's disk and is removed with the pod.
  On the master only read jobs with a 256 MiB file run. Results (MB/s, IOPS, latency) are in the log, the JSON
  and in the baseline check.

## ⚠️ Network tests: open TCP/UDP ports 30000–32767 between the nodes
`--profile net` and `--net-matrix` run an `iperf3` server on one node and a client on another. The port comes from the
Kubernetes **NodePort range** (32000–32699; the matrix uses 32100 and up). **A node firewall that lets in only chosen ports
makes these tests fail** (the pair shows `x`, "iperf3 failed (peer unreachable / port blocked?)") - even though ping works.
If only **UDP** is blocked, the test does not stop: the UDP job is reported as `skipped` (with the reason) and the rest goes on; use
`--net-extra no-udp` to leave it out for good.

| Protocol | Ports | For |
|---|---|---|
| TCP | 30000-32767 | iperf3 TCP tests |
| UDP | 30000-32767 | iperf3 UDP test (jitter, loss) |
| ICMP | echo | ping, MTU probe, `--net-watch`, `mtr` |

```bash
sudo ufw allow from 10.0.0.0/24 to any port 30000:32767 proto tcp   # replace with your subnet
sudo ufw allow from 10.0.0.0/24 to any port 30000:32767 proto udp
```
That range is usually open on a Kubernetes node already (NodePort Services use it). The pods also need internet access once
(`apt` installs `iperf3`, `iputils-ping`, optionally `curl`/`mtr-tiny`). Details and troubleshooting: `HELPDESK.md` (step 8).

## Network test (since 1.13.0)
```bash
./stress.sh --node worker-1 --profile net                       # against an automatically chosen peer
./stress.sh --node worker-3 --profile net --net-peer worker-4 --net-mode pod
```
An iperf3 server pod starts on the peer, the tested node runs the client (both pods `hostNetwork` in `host` mode, so the
real NIC is measured; `pod` mode goes through the pod network). Jobs: link speed/duplex, ping (latency, jitter, loss), path MTU,
TCP up/down, TCP with 4 streams, UDP (jitter, loss; optional and never fatal), NIC error counters. There is no CPU load and no
cooldown in this test, and its summary only shows the node's CPU, RAM and temperature as information. Typical findings the summary points out: a link
negotiated at 100 Mb/s (bad cable/port), half duplex, packet loss, big difference between up and down, retransmits.
`--net-rate` caps the TCP tests; when the master is on either end they are capped at 300 Mbit/s so the API keeps its network.
Needs internet on both nodes (apt installs iperf3 and iputils-ping) and a free TCP/UDP port (5201 + a per-run offset).

### More network tools (since 1.14.0)
```bash
./stress.sh --net-matrix --yes                          # every pair of nodes, tables + findings
./stress.sh --node worker-3 --time 5m --net-watch  # any test + ping to the master during the load
./stress.sh --node worker-1 --profile net --net-mode pod --net-extra all
```
- `--net-matrix` needs at least two Ready nodes, refuses while another test of the tool runs and asks before loading the network
  (`--yes`); the master is capped at 300 Mbit/s. Results: `logs/net-matrix-<date>.log` + `.json`.
- `--net-watch` adds `| Ping: X ms` (or `lost`) to every measurement line and a "Network latency" row to the summary.
- `--net-extra dns,internet,mtr,service|all` adds jobs to the network test (the `service` job goes through a temporary Kubernetes
  Service that is deleted at the end).

## Stepped test and testing several nodes (since 1.5.0)
**What to test** and **which test** are chosen at startup (interactively by questions, or by options):

| Choice | Options | Flag |
|---|---|---|
| Scope | one node, all workers, the whole cluster, a list of nodes | `--node`, `--workers`, `--cluster`, `--nodes a,b` |
| Test type | classic (one load), stepped 25 → 50 → 75 → 100 % | `--profile classic\|stepped` |

```bash
./stress.sh --workers --profile stepped                 # workers one after another, stepped test (12 min per node)
./stress.sh --cluster --profile classic --time 5m       # the whole cluster, master last
./stress.sh --nodes worker-1 --profile stepped --steps 25,50,100 --step-time 2m
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
controls them and shows one framed table in the terminal (redrawn in place; without a terminal a text table is printed every
30 s). In a terminal every node has its own bar; a text line looks like this:
```
worker-1          ▶ Stage 3/4 (75 %)      CPU   74 %    71 °C   3591 MHz  left ~6 min
worker-3             ▶ Stage 3/4 (75 %)      CPU   76 %    52 °C   1600 MHz  left ~6 min
```
- **Start mode:** by default (`--start-mode rolling`) every node starts its load **as soon as it is ready** and runs the full time from
  its **own** start, so a slow node (slow CPU, slow network, `apt`) never holds the others back; the text output says
  `🟢 <node> started its load (k/N)`. With `--start-mode sync` every node prepares, says READY (`🚦 <node> READY (k/N)`), and the load
  starts on all nodes at the same moment (`🟢 GO`); a node that is not ready within `--ready-timeout` is left out.
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
worker-1          OK          84      1 min      no          2646.7     not reached
worker-3             OK          49      0 s        no          676.3      42 s
control-1 (master) OK         61      0 s        no          410.2      35 s
```


## How a test runs
1. Leftovers of earlier tests are removed (finished pods of this tool; running ones are never touched).
2. The `hw-info` pod runs briefly (unprivileged by default): CPU, threads, system, motherboard, RAM total, GPU, disks (model, size,
   HDD/SSD); RAM modules need `--hw-privileged`.
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
start the tool from). **Everything is written into a folder of the day** (`YYYY-MM-DD`): a run looks whether today's folder
exists, creates it when it does not, and writes only into it - so 20 tests in one day stay together and tomorrow starts a new folder.

| Where | For whom | What is in it |
|---|---|---|
| `logs/<day>/` (visible) | the user | test results: hardware, ongoing measurements, remaining time, the reason for ending. Created only with `--log`. File `<node>-<duration>s-<date_time>.log` (+ `.json`/`.csv`, series logs `cluster-<date_time>.log`, `net-matrix-...`, and `selftest-<time>/` folders of the FULL self-test) |
| `logs/baselines/` | the user | the baseline of every node (outside the day folders, so it is found from any day) |
| `.logs/debug/<day>/` (hidden) | debugging | **a technical log of every run** (even without `--log`): all called `kubectl` commands with the return code and time, every measurement and decision of the temperature guard, all user-facing output and the full traceback on an error. One file per run: `<date>_<time>_<pid>.log` |
| `.logs/tests/<day>/<date>_<time>_<pid>/` (hidden) | debugging | history of pytest runs: `pytest.log` (results, errors, summary), `inprocess-debug.log`, `tool-runs/` (debug logs of the tool started from the integration tests) |
| `.logs/running/`, `.logs/menu-state.json` | the tool | registry of background tests, the menu's last action and settings (not in day folders) |

With `--log-dir DIR` the day folder is made inside `DIR`. `--compare`, `--export-log` and `--set-baseline` search **all day folders**
(a node name = its newest logs from any day). Older flat files can be moved into day folders once with `python3 -m stress_test --migrate-logs`.

The history of the debug logs is complete, nothing is deleted automatically. The hidden folders have a dot
in their name (`ls -a`). On an error (exit code 1) the tool prints the path to the debug log. If the debug
log cannot be created (e.g. permissions), the tool keeps running without it. New folders are private (`0700`).

Quick looks into the history:
```bash
ls -lt .logs/debug/$(date +%F) | head                    # the latest runs of today
grep -l "ERROR" .logs/debug/*/*.log                      # which run went wrong (any day)
tail -n 60 "$(ls -t .logs/debug/*/*.log | head -1)"      # the end of the last run
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
  paths.py      where logs are stored: day folders (logs/<day>/, .logs/debug/<day>/, .logs/tests/<day>/), log migration
  debuglog.py   the hidden technical log of every run
  menu.py       the main menu (sections, R repeat, S settings, ? help, status rows)
  ui.py         the retro look: frames, bars, icons, live screens (single node, matrix), width-aware drawing
  selftest.py   the FULL self-test: phases, cooling pauses, hardware / disk findings, one report
  export.py     export of results (--export, --export-log)
  baseline.py   baseline of a node and the comparison with it (--set-baseline)
  smart.py      SMART disk health (--smart)
  disk.py       fio disk benchmark (--profile disk)
  gpu.py        GPU test: gpu-burn command, nvidia-smi parsing, throttle reasons, guard, summary, log field
  dashboard.py  the cluster dashboard (menu key D): collector (read-only probes), three width tiers, keys, sort / filter / views, pods and logs
  graph.py      the fine graphs of the dashboard (key g): blocks / Braille, windows, cursor, text report and CSV / JSON
  watch.py      the live status of the running tests (menu key W, --status --live)
  catalog.py    the numbered lists of nodes, logs, running tests (menu 7 DATA, stop, settings, planner)
  schedule.py   planned tests: start times, waiting processes, systemd timers, list and cancel
  gpuscan.py    GPU scan of all nodes (--list-gpus, menu 2), node choice, the multi-node table (GpuBoard), image pre-pull
  net.py        network test of one node (--profile net, --net-extra, --net-watch)
  netmatrix.py  network matrix of all nodes (--net-matrix)
conftest.py     writes the history of pytest runs to .logs/tests/<day>/
tests/
  test_parsing.py, test_models.py, test_monitor.py, test_summary.py, test_compare.py,
  test_series.py, test_parallel.py, test_background.py,
  test_paths_and_debuglog.py, test_spike.py, test_export_baseline.py, test_smart.py, test_disk.py,
  test_net.py, test_net_extras.py, test_net_watch.py, test_netmatrix.py, test_power.py,
  test_menu.py, test_menu_v2.py, test_security.py, test_live_ui.py, test_sync_start.py,
  test_net_udp_light.py, test_selftest.py, test_daily_logs.py, test_gpu.py, test_gpu_profile.py,
  test_gpuscan.py, test_gpu_board.py, test_schedule.py, test_dashboard.py, test_gpu_fan.py, test_width_modes.py, test_responsive.py   unit tests
deploy/
  nvidia-device-plugin-k3s.yaml, gpu-smoke-test.yaml   manifests for the GPU node setup (see HELPDESK.md)
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
