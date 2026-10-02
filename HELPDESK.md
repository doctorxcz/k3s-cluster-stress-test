# Helpdesk — Getting this running on your own cluster

This is a step-by-step guide for someone who just downloaded this repo (e.g. as a ZIP from GitHub)
and wants to run it against their own k3s/Kubernetes cluster. If you hit a problem not covered here,
check the "Common issues" section at the bottom before opening an issue.

## 0. What this tool actually needs

- A machine with **Python 3.9+** and a working **`kubectl`** pointed at your cluster.
- That's it — no config files, no API keys, no account. The tool only ever runs `kubectl` commands
  under the hood, using whatever context is already active on your machine.

## 1. Get the files onto your machine

**Option A — download the ZIP (simplest):**
1. On the GitHub repo page, click **Code → Download ZIP**.
2. Unzip it. You'll get a folder like `k3s-cluster-stress-test-master/`.
3. Move/rename it wherever you like, e.g. `~/k3s-cluster-stress-test/`.

**Option B — clone with git:**
```bash
git clone https://github.com/doctorxcz/k3s-cluster-stress-test.git
cd k3s-cluster-stress-test
```

## 2. Make the launcher executable (ZIP downloads sometimes lose this)

```bash
cd ~/k3s-cluster-stress-test
chmod +x stress.sh
```
If you skip this step, run the tool directly with `python3 -m stress_test` instead of `./stress.sh` —
that always works regardless of file permissions.

## 3. Check Python

```bash
python3 --version
```
Needs to be **3.9 or newer**. No extra packages are required to *run* the tool — it uses only the
standard library. (`requirements-dev.txt` is only needed if you want to run the test suite yourself.)

## 4. Check `kubectl` access

```bash
kubectl config current-context
kubectl get nodes
```
- `current-context` must point at the cluster you want to test.
- `get nodes` must list your nodes without errors. If this fails, fix your kubeconfig/cluster access
  **before** trying this tool — it has no way to work around a broken `kubectl`.

If you're running this directly on a k3s master, `kubectl` is usually already set up
(`/usr/local/bin/kubectl` is a symlink k3s creates for you).

## 5. (Optional but recommended) `metrics-server`

Needed for the RAM-usage fallback (`kubectl top nodes`). It's on by default in k3s. Check with:
```bash
kubectl top nodes
```
If this errors, RAM reporting during the test may be limited, but the CPU/temperature test itself
still works.

## 6. Find your node names

```bash
kubectl get nodes
```
Use the exact names shown here as the value for `--node NAME`. You don't have to remember them —
running the tool with no `--node` flag shows you an interactive list to pick from.

### 6b. Quick look without starting anything
```bash
./stress.sh --list-nodes                                   # nodes, roles, state
./stress.sh --node <your-worker-node> --time 10m --dry-run # shows what WOULD run, starts nothing
```

## 7. Run a small, safe first test

Don't jump straight to a long test on your master. Start small on a worker node:
```bash
./stress.sh --node <your-worker-node> --time 60 --cpu-load 50
```
or, if `stress.sh` isn't executable / you're on a system without bash:
```bash
python3 -m stress_test --node <your-worker-node> --time 60 --cpu-load 50
```
This runs a 60-second test at 50% CPU load — enough to confirm everything works without stressing
the machine.

## 8. Network tests need open ports (read this if you use `--profile net` or `--net-matrix`)

> ## ⚠️ FIREWALL: allow TCP + UDP ports 30000–32767 between your nodes
> The network tests (`--profile net`, `--net-matrix`) start an `iperf3` server on one node and connect to it
> from another. The tool uses a port from the top of the Kubernetes **NodePort range: 32000–32699**
> (`--net-matrix`: 32100 and up). **If a node's firewall (ufw, firewalld, iptables, a cloud security group)
> only lets in specific ports, these tests fail** — the connection hangs or is refused — while ping still works.
>
> **What to allow (incoming, from the other cluster nodes):**
> | Protocol | Ports | Used for |
> |---|---|---|
> | TCP | 30000–32767 | iperf3 TCP tests (upload, download, 4 streams) |
> | UDP | 30000–32767 | iperf3 UDP test (jitter, loss) |
> | ICMP | echo request/reply | ping, MTU probe, `--net-watch`, `mtr` |
>
> Example with `ufw` (run on each node that has a firewall, replace the subnet with yours):
> ```bash
> sudo ufw allow from 10.0.0.0/24 to any port 30000:32767 proto tcp
> sudo ufw allow from 10.0.0.0/24 to any port 30000:32767 proto udp
> sudo ufw status
> ```
> That range is normally already open on a k3s/Kubernetes node (NodePort Services need it), so on many
> clusters nothing has to be changed. Nodes without any firewall need nothing at all.

How to tell that a port is blocked: the network matrix shows `x` for a pair and the summary says
`iperf3 failed (peer unreachable / port blocked?)`, or the test stops with `The iperf3 server did not start
listening`. Check from another node:
```bash
timeout 3 bash -c '</dev/tcp/<node-ip>/32000' && echo open || echo blocked   # blocked is expected if no server runs
```
(Only a *running* server answers, so test with the tool itself: `./stress.sh --net-matrix --net-time 5`.)

The network tests also need the nodes to reach the **internet** once (the pods install `iperf3`,
`iputils-ping`, and optionally `curl`/`mtr-tiny` with `apt`), and `--net-extra internet` needs outgoing
access to `1.1.1.1` and `archive.ubuntu.com`.

`--smart` and `--profile disk` need no extra ports; `--smart` runs a *privileged* pod on the node
(read-only access to the disks) — your cluster must allow privileged pods (no restrictive Pod Security policy).

### Optional extras (nothing to install by hand)
The nodes need **no software installed by you**. The pods install what they need with `apt` at run time and it
disappears with the pod — this only requires that the node can reach the internet:
`stress-ng` (CPU/RAM), `fio` (`--profile disk`), `smartctl` (`--smart`), `iperf3`/`ping`/`mtr` (network tests).
Only `--smart` and `--hw-privileged` need a cluster that allows **privileged pods**.

## 9. Full usage

Running `./stress.sh` with no options opens the **main menu** (CPU load · Disk · Network · Quick test · Results ·
Management) and asks for everything. Skip it with any option, or with `STRESS_NO_MENU=1`. Handy examples:
```bash
./stress.sh                                            # menu
./stress.sh --node <worker> --time 10m --profile stepped
./stress.sh --node <worker> --profile disk --smart
./stress.sh --net-matrix --net-time 5                  # needs the open ports from step 8
./stress.sh --node <worker> --time 20m --schedule 22:30 -b   # start later, in the background
./stress.sh --status                                   # what runs now;  --stop <id|node|pid> stops it
```
Results (`logs/`) can be compared with `--compare`, exported with `--export`, and checked against a per-node
baseline (`--set-baseline`) — see `README.md`.

Once step 7 works, see `README.md` for the full option list (`--time`, `--max-temp`, `--ram-pct`,
`--hdd`, `--workers`/`--cluster`, `--profile stepped`, `--background`, `--compare`, etc.) and how
master/control-plane nodes are protected automatically.

---

## Common issues

### Cluster dashboard (key `D` in the menu, `--dashboard`)
- **"needs a terminal"**: the dashboard reads keys without Enter, run it in a real terminal (not in a pipe or a script).
- **A node shows `probe did not start`**: its probe pod did not become Ready in 90 s (image pull, node busy or down); the node is still listed from the
  Kubernetes API, only the live values (CPU, temperature, ...) are missing. The probes are `busybox` pods with `/sys` mounted read-only.
- **GPU column shows `?` or `GPU ?`**: the `nvidia-smi` probe needs the NVIDIA runtime (the same setup as for the GPU test, see the GPU section).
- **Probe pods remain after a crash**: they end by themselves after an hour; remove them with `kubectl delete pod -l app=stress-test-dashboard`.
- **Fast refresh (0.5 s / 1 s) feels slow or jumpy**: a round (one probe per node) takes a few tenths of a second, so the real rate is limited by it; the interval is
  start to start. The probes are read-only and light, but every round starts `kubectl exec` per node - use 2 s or more on a slow link.
- **`n/a (no metrics-server)` in the big dashboard**: the cluster has no metrics-server, only the measured values (CPU %, RAM) are shown, `kubectl top` ones are missing.
- **The table is cut / has few columns**: it adapts to the window width - widen the window (91-160 columns and over 160 show more). A tall window (40 lines or more)
  opens more blocks under the table instead of scrolling. `v` changes the view (temperatures, network, disks, GPU) with other columns, `?` lists every key.
- **I cannot find a node**: `/` filters by a part of the name, `f` shows only problems / workers / GPU nodes, `c` clears it all; `o` sorts by temperature, CPU, RAM, pods or GPU.
- **The log of a pod is empty (`l` ▸ Enter)**: the pod has not started or has no output yet; it is read with `kubectl logs --tail=200` every 2 s, read only.
- **The graph is empty or short**: the history is kept only while the dashboard runs (up to an hour at 1 s). `[` `]` change the window, `b` switches blocks / Braille dots.
  If the Braille dots show as boxes, stay with the blocks (the default). `x` draws all quantities over each other, each in the scale of its own range.
- **Where did `e`, `t` and `s` save?** In the `scr/` folder of the program (`scr/graphs/` for graphs): `e` = the screen as text, `t` = all the graphs as a text report,
  `s` = the data as CSV and JSON. Open them on a server: `less file.txt` or `batcat file.txt`; CSV: `column -s, -t < file.csv | less -S`, or `sudo apt install csvkit` and
  `csvlook file.csv | less -S`, or `sudo apt install visidata` and `vd file.csv`; JSON: `jq . file.json | less` (`sudo apt install jq`) or `python3 -m json.tool file.json`.
  The folder is created on the first save; `STRESS_TEST_SCREEN_DIR` moves it.

### Live status (key `W`, `--status --live`)
- **"needs a terminal"**: it reads keys without Enter - run it in a real terminal; in a pipe `--status --live` prints the plain list.
- **A test I started in another terminal is missing**: tests are listed from the registry `.logs/running/` of the project folder the test was started from; a test of a
  parallel subprocess is shown by its series. A finished test disappears at once.
- **A narrow window shows cards instead of a table**: under 60 columns every test is a card (scroll with `j` `k`); widen the window for the table, or make it taller
  to open all the cards.
- **`x` did not stop the test**: it asks first (`y`) and sends the same graceful stop as `--stop`; a test that is still cleaning its pod says so, check again with `--status`.

### Prebuilt images (`k3s-stress-tools`, `-gpu`, `-probe`)
- **"The prebuilt image … cannot be pulled (ErrImagePull)"**: the node could not download `ghcr.io/doctorxcz/k3s-stress-…:<version>` (the images are not published for
  this version yet, the package is not public, the node has no internet or cannot resolve `ghcr.io`). The tool carries on with plain Ubuntu / busybox and `apt`, so the
  test still runs, only slower. Check on the node: `kubectl run t --rm -it --image=ghcr.io/doctorxcz/k3s-stress-probe:<version> --restart=Never -- true` and
  `kubectl describe pod <the pod>`; `--no-prebuilt` skips the attempt.
- **The first test on a node is slow, later ones are fast**: the image is pulled once (about 170 MB for the tools, ~1.2 GB for the GPU image) and then cached on the node.
  Several nodes pull at the same time; on a big cluster k3s can share images between nodes (`--embedded-registry`, see `deploy/images/README.md`).
- **No internet / company network**: copy the images with `deploy/images/pull-to-folder.sh` and `push-to-registry.sh` into your registry and run with `--registry HOST/PATH`.
- **`ping` says "Operation not permitted" when I try the image with rootless podman**: `ping` needs the `NET_RAW` capability, which pods in k3s have by default; add
  `--cap-add=NET_RAW` for a local test.

### Planned tests (`9 SCHEDULE`, `--schedule`)
- **The plan is gone after a restart:** a plain plan is a waiting process and does not survive a restart of the computer. Plan with *"survive a restart"*
  (menu `T` asks when systemd is available) or `--schedule … --persistent`: it becomes a systemd **user** timer (`~/.config/systemd/user/stress-test-<id>.timer`).
- **A persistent test did not start while I was logged out:** run once `sudo loginctl enable-linger $USER` (the tool warns when linger is off).
  Check: `systemctl --user list-timers | grep stress-test`, the output of a run: `journalctl --user -u stress-test-<id>.service`.
- **Cancel a plan:** `9 SCHEDULE` ▸ cancel, or `./stress.sh --stop <id>` (works for both kinds); list: `./stress.sh --scheduled`.
- **The test was refused at the start:** the tool computer must reach the cluster, and a node with another test of the tool refuses a second one - see the
  log of the planned test (`logs/<day>/`, or `journalctl --user -u …` for a timer).

### GPU test (`--profile gpu`, menu `2 GPU`)

**One-time setup of a GPU node** (only NVIDIA cards; the other nodes need nothing). Do it on the GPU node unless it says otherwise:
1. Driver: install the proprietary NVIDIA driver for your card (`sudo ubuntu-drivers install`, or `sudo apt install nvidia-driver-580`) and reboot.
   `nvidia-smi` must print the card. Old cards (Pascal, e.g. Quadro P620) work with the 580 driver and **CUDA 12.x images** (the tool's default).
2. Container toolkit: install `nvidia-container-toolkit` (NVIDIA apt repository), then `sudo systemctl restart k3s-agent` (on the master: `k3s`).
   k3s notices the toolkit and adds the `nvidia` runtime: `sudo grep -n nvidia /var/lib/rancher/k3s/agent/etc/containerd/config.toml`.
3. RuntimeClass: `kubectl get runtimeclass nvidia` (k3s creates it; if missing: a `RuntimeClass` named `nvidia` with `handler: nvidia`).
4. Device plugin (from the machine with `kubectl`): copy `deploy/nvidia-device-plugin-k3s.yaml`, put your GPU node name instead of `GPU-NODE-NAME`
   and `kubectl apply -f` it. It needs `runtimeClassName: nvidia` (already in the file) - without it the plugin sees no GPU on k3s.
5. Check: `kubectl describe node <node> | grep nvidia.com/gpu` shows `nvidia.com/gpu: 1`, and `deploy/gpu-smoke-test.yaml` (node name replaced)
   prints `nvidia-smi` from inside a pod: `kubectl logs gpu-smoke-test`, then `kubectl delete pod gpu-smoke-test`.
Then `./stress.sh --list-gpus` should show the node as ✅ OK.

**Reading the GPU scan (`--list-gpus`, first step of menu `2 GPU`):**
- **✅ OK**: ready for the test.
- **⚠️ ERROR "found, but Kubernetes does not offer it"**: the card is in the machine, steps 1-4 above are not complete (most often the device plugin, or the
  runtime was not picked up: restart `k3s-agent`, check the `config.toml` line from step 2).
- **❌ none**: no dedicated NVIDIA GPU (an integrated Intel / AMD graphics cannot run the test).
- **❓ `? ERROR` "the scan failed"**: the short `lspci` pod did not finish (no internet for `apt`, node busy). It does not say there is no GPU - run the scan again.

**Other problems:**
- **"does not offer nvidia.com/gpu"** when starting: same as ⚠️ above. With `--no-hw` the message names the possible causes (driver, toolkit, runtime, plugin).
- **Pod stays Pending / timeout**: the GPU is used by another pod, or the first pull of the CUDA image (~3 GB) is still running
  (`kubectl describe pod <stress-test-...>`). Use `--gpu-prepull` (asked in the menu) to pull the image on all chosen nodes first.
- **The first run takes minutes**: image pull + building gpu-burn for your card. Later runs on the node are fast (the image is cached; the build repeats,
  or use `--gpu-image` with a ready `gpu_burn`).
- **Power shows `N/A`**: normal for some cards (Quadro P620); the test then judges temperature, clocks and throttling only. The GPU fan is in %, never RPM.
- **"Test stopped because of a GPU failure ... nvidia-smi did not respond"**: `nvidia-smi` failed 3 times in a row, the GPU temperature could not be watched.
- **"stopped because of overheating (GPU ...)"**: the card reached the GPU limit (default 80 °C, set with `--gpu-max-temp` or in the dialog). Check the cooling
  or lower the limit. A throttling note in the summary (`sw_thermal`, `hw_slowdown`) means the card slowed itself down.
- **No fan RPM**: the CPU fan is not read at all (Dell / HP desktops do not expose it to Linux); only the GPU fan % is shown.

**`stress.sh: package stress_test not found in ...`**
The script can't find the `stress_test/` folder next to itself. Make sure you didn't move `stress.sh`
out of the project folder on its own. Fix: run from inside the project folder, or use
`python3 -m stress_test` from that folder directly.

**`permission denied` when running `./stress.sh`**
The executable bit was lost (common with ZIP downloads on some OSes/archivers). Fix:
`chmod +x stress.sh`, or just use `python3 -m stress_test ...` instead.

**`kubectl: command not found`**
`kubectl` isn't installed or isn't on your `PATH`. Install it and make sure it can reach your cluster
before using this tool (see step 4).

**`error: You must be logged in to the server (Unauthorized)` or similar `kubectl` errors**
This is a `kubectl`/cluster access problem, not a bug in this tool. Fix your kubeconfig
(`~/.kube/config`) or context first — test with plain `kubectl get nodes`.

**Test on the master gets refused / asks for confirmation**
This is intentional. The tool auto-detects the control-plane node from Kubernetes itself (nothing is
hardcoded) and applies extra protection: lower CPU cap, lower temperature limit, and a confirmation
prompt. Use `-y`/`--yes` to confirm automatically, `--force` (or `FORCE=1`) to bypass entirely, or
just answer `y` when asked.

**Network test: `iperf3 failed (peer unreachable / port blocked?)`, `The iperf3 server did not start listening`, or the matrix shows `x`**
Almost always a node firewall. Allow TCP/UDP **30000–32767** from the other nodes (see step 8). Ping working
does not prove that TCP ports are open. Also check that the peer node can reach the internet (`apt` installs `iperf3`).

**Network test: `udp skipped: ... UDP is probably blocked by a firewall`**
Not an error: the TCP tests ran, only the UDP test (jitter / packet loss) got no answer - the node's firewall lets in TCP but not UDP
on 30000–32767. Allow UDP (step 8) or leave it out with `--net-extra no-udp`.

**Network test: many `x` in the matrix, but only for one node as the *server***
That node's firewall drops incoming connections on the tool's port. The matrix runs the `iperf3` server on the
sending node, so a node that cannot *receive* connections on 30000–32767 breaks every pair where it sends.

**`--net-matrix` refuses to start: "A test of this tool is running right now"**
Another run of this tool still has pods (`kubectl get pods -A | grep -E "stress-test|temp-probe|hw-info|net-mx"`).
Wait for it, or stop it with `--stop`. Leftover pods of a killed run can be removed with `kubectl delete pod <name>`.

**Pod stays `Pending` / test never starts**
Usually means the node doesn't have internet access to pull the container image, or the cluster
doesn't have enough free resources on that node. Check with `kubectl get pods -A` and
`kubectl describe pod <pod-name>` for the real reason.

**RAM % test doesn't show real numbers / `kubectl top nodes` fails**
`metrics-server` isn't installed or isn't ready yet in your cluster. The CPU/temperature test still
works; only the `--ram-pct` reporting is affected. Give `metrics-server` a minute to warm up after
cluster startup and try again.

**No CPU temperature reported / "no sensor" message**
Not every node exposes a temperature sensor to the pod (depends on hardware/drivers). Use
`--allow-no-sensor` to continue without it — you'll just lose the temperature-based auto-stop and the
temperature columns in the summary.

**Test seems to hang after `Ctrl+C`**
Give it a few seconds — the tool cleans up the pod on the node before exiting. If it's stuck for a
long time, check `kubectl get pods -A` for leftover pods from this tool and remove them manually with
`kubectl delete pod <name> -n <namespace>`.

**Background test (`-b`/`--background`) didn't survive closing the terminal**
Background mode needs a system with `fork()` — Linux and macOS are fine; this won't behave the same
on Windows outside WSL. Also note: it does **not** survive a full machine restart, only closing the
terminal/losing the SSH connection.

**Help text mentions `--cz` / `--eng` or `STRESS_LANG`**
Those belong to a launcher of the original author's private setup (a Czech and an English build side by
side). This repository is the English build only; ignore them.

**The live frame looks broken / lines are cut / I want plain text**
The frame needs a terminal that understands ANSI codes and shows emoji two cells wide (most do). Every screen adapts to the window width
(compact < 60 columns, normal 60-99, wide 100-200; it is re-measured for every screen, so resizing is fine). A window narrower than about
40 columns cannot be laid out properly - widen it (long names and paths are then cut with `…`). `STRESS_TEST_COLUMNS=80 ./stress.sh ...`
forces a width. Switch the frames off with `STRESS_NO_LIVE=1 ./stress.sh ...`, in the menu with `S` ▸ live frames, or by
redirecting the output (then the original text is printed).

**`--parallel`: one slow node - do the others wait for it?**
No (default `--start-mode rolling`): every node starts as soon as it is ready and runs the full time from its own start. If you want all
nodes to start at the same moment use `--start-mode sync` (a node that is not ready within `--ready-timeout` is left out).

**The FULL self-test (menu `6`) - what does it do to my cluster?** (`--nodes A,B` limits it to chosen nodes; a GPU node gets an extra GPU phase)
It loads everything at full power (network, disks, CPU, RAM) on all nodes for about 35 minutes in the `standard` level, so nothing else
should run meanwhile. It asks twice before it starts and the master is only tested when you say `y` (default `n`). Read the warning
box; start with `--self-test-level quick` the first time.

**A report says `slow disk ...` or `CPU throttling`**
`slow disk` names the disk and the usual reason (a mechanical hard disk does only about 100–300 random IOPS; SMART warnings like
reallocated sectors mean the disk is ageing). `CPU throttling` is reported when the clock dropped **and** the CPU got hot; a lower clock
on a cool CPU (under 65 °C) is power saving, not throttling.

**Want to run the tests of the tool itself (no cluster needed)?**
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

**Still stuck?**
Check `logs/<today's date>/` (test results) and `.logs/debug/<today's date>/` (technical debug log per run) in the project folder —
they usually have the actual error from `kubectl` or the pod. Everything a run writes goes into a folder of the day
(`YYYY-MM-DD`); old flat files can be moved there once with `./stress.sh --migrate-logs`.
