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
git clone https://github.com/<owner>/k3s-cluster-stress-test.git
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

## 8. Full usage

Once step 7 works, see `README.md` for the full option list (`--time`, `--max-temp`, `--ram-pct`,
`--hdd`, `--workers`/`--cluster`, `--profile stepped`, `--background`, `--compare`, etc.) and how
master/control-plane nodes are protected automatically.

---

## Common issues

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

**Still stuck?**
Check `logs/` (test results) and `.logs/debug/` (technical debug log per run) in the project folder —
they usually have the actual error from `kubectl` or the pod.
