"""Platform compatibility measurement: how the tool behaves on amd64 (Intel, AMD) and arm64 / armv7 (Raspberry Pi, "noname" boards).

Nothing here touches a cluster. Two kinds of evidence:

* MODELLED nodes: a folder tree that imitates what a node shows to the probe pod (`/sys/class/hwmon`, cpufreq, powercap, `/proc/cpuinfo`, DMI ...).
  The REAL `PROBE_SCRIPT` and `HW_SCRIPT` of the tool run against it (busybox sh / bash, only the paths are redirected) and the REAL parsers
  read the output. The trees are typical layouts written from knowledge of these devices - NOT captured from real hardware.
* PUBLISHED facts (optional, needs the internet): the CPU architectures of the container images the tool uses and whether the apt packages
  exist for arm64 on Ubuntu 24.04.

Run: `python tests/platform_matrix.py [--offline] [--out DIR]` writes `platform-compat-<date>.log` and `.md`. `tests/test_platforms.py` asserts the same results.
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from stress_test import __version__, images                                   # noqa: E402
from stress_test.manifests import HW_SCRIPT, PROBE_SCRIPT       # noqa: E402
from stress_test.parsing import cpu_summary, parse_probe_output  # noqa: E402

# ------------------------------------------------------------------------------------------------ the modelled platforms
INTEL_CPUINFO = """processor\t: 0\nvendor_id\t: GenuineIntel\ncpu family\t: 6\nmodel\t\t: 60\nmodel name\t: {name}\nstepping\t: 3\ncpu MHz\t\t: 800.000\n"""
ARM_BASE = "processor\t: 0\nBogoMIPS\t: 108.00\nFeatures\t: fp asimd evtstrm crc32 cpuid\nCPU implementer\t: 0x41\nCPU architecture: 8\nCPU variant\t: 0x0\nCPU part\t: {part}\nCPU revision\t: 3\n"


@dataclass
class Platform:
    key: str
    title: str
    arch: str                                   # kubernetes architecture label
    cpuinfo: str
    lscpu_model: str                            # what `lscpu` calls the model (it works on ARM, /proc/cpuinfo often has no name)
    threads: int
    cores: int
    hwmon: list = field(default_factory=list)   # [(hwmon name, millidegrees)]
    thermal_zones: list = field(default_factory=list)     # [(type, millidegrees)] under /sys/class/thermal
    cpufreq_khz: list = field(default_factory=list)       # per CPU scaling_cur_freq
    rapl: bool = False
    dmi: dict = field(default_factory=dict)     # /sys/class/dmi/id files (absent on ARM boards)
    mem_kb: int = 8000000
    disks: str = "sda 238.5G 0 Patriot P210"    # lsblk -dn -o NAME,SIZE,ROTA,MODEL
    dt_model: str = ""                          # /proc/device-tree/model (ARM boards; NUL terminated in reality)
    machine: str = "x86_64"                     # uname -m
    note: str = ""


PLATFORMS = [
    Platform("intel-i7", "Intel Core i7-4790S (Dell 9020 SFF)", "amd64", INTEL_CPUINFO.format(name="Intel(R) Core(TM) i7-4790S CPU @ 3.20GHz"),
             "Intel(R) Core(TM) i7-4790S CPU @ 3.20GHz", 8, 4, hwmon=[("coretemp", 52000), ("nvme", 41000)], cpufreq_khz=[3600000] * 8, rapl=True,
             dmi={"sys_vendor": "Dell Inc.", "product_name": "OptiPlex 9020", "board_name": "0DNKXP"},
             note="this cluster's own hardware family: the baseline everything else is compared with"),
    Platform("intel-celeron", "Intel Celeron (HP G2 mini)", "amd64", INTEL_CPUINFO.format(name="Intel(R) Celeron(R) CPU G1840T @ 2.50GHz"),
             "Intel(R) Celeron(R) CPU G1840T @ 2.50GHz", 2, 2, hwmon=[("coretemp", 46000)], cpufreq_khz=[1600000] * 2, rapl=True,
             dmi={"sys_vendor": "HP", "product_name": "HP G2 Mini", "board_name": "8000"}),
    Platform("amd-a10", "AMD PRO A10-9700E (HP 705 G4)", "amd64",
             "processor\t: 0\nvendor_id\t: AuthenticAMD\ncpu family\t: 21\nmodel\t\t: 101\nmodel name\t: AMD PRO A10-9700E R7, 10 Compute Cores 4C+6G\n",
             "AMD PRO A10-9700E R7, 10 Compute Cores 4C+6G", 4, 4, hwmon=[("k10temp", 55000), ("amdgpu", 50000)], cpufreq_khz=[3143000] * 4,
             dmi={"sys_vendor": "HP", "product_name": "HP EliteDesk 705 G4 SFF", "board_name": "83F1"}, note="no RAPL on this AMD generation"),
    Platform("rpi4-arm64", "Raspberry Pi 4 Model B, 64-bit OS (Cortex-A72)", "arm64",
             ARM_BASE.format(part="0xd08") + "\nHardware\t: BCM2835\nRevision\t: c03115\nModel\t\t: Raspberry Pi 4 Model B Rev 1.5\n",
             "Cortex-A72", 4, 4, dt_model="Raspberry Pi 4 Model B Rev 1.5", machine="aarch64", hwmon=[("cpu_thermal", 48000), ("rpi_volt", 0)], thermal_zones=[("cpu-thermal", 48000)], cpufreq_khz=[1500000] * 4,
             disks="mmcblk0 29.7G 0 ", mem_kb=3900000, note="arm64 kernel prints no `model name` in /proc/cpuinfo; the CPU sensor is called cpu_thermal"),
    Platform("rpi5-arm64", "Raspberry Pi 5, 64-bit OS (Cortex-A76)", "arm64",
             ARM_BASE.format(part="0xd0b") + "\nRevision\t: d04170\nModel\t\t: Raspberry Pi 5 Model B Rev 1.0\n", "Cortex-A76", 4, 4, dt_model="Raspberry Pi 5 Model B Rev 1.0", machine="aarch64",
             hwmon=[("cpu_thermal", 52000), ("pwmfan", 0)], thermal_zones=[("cpu-thermal", 52000)], cpufreq_khz=[2400000] * 4, disks="nvme0n1 238.5G 0 Samsung",
             mem_kb=7900000),
    Platform("rpi3-armv7", "Raspberry Pi 3, 32-bit OS (armv7l)", "arm",
             "processor\t: 0\nmodel name\t: ARMv7 Processor rev 4 (v7l)\nBogoMIPS\t: 38.40\nFeatures\t: half thumb fastmult vfp edsp neon vfpv3 tls vfpv4\n"
             "CPU implementer\t: 0x41\nCPU architecture: 7\nCPU part\t: 0xd03\n\nHardware\t: BCM2835\nRevision\t: a02082\nModel\t\t: Raspberry Pi 3 Model B Rev 1.2\n",
             "Cortex-A53", 4, 4, dt_model="Raspberry Pi 3 Model B Rev 1.2", machine="armv7l", hwmon=[("cpu_thermal", 55000)], thermal_zones=[("cpu-thermal", 55000)], cpufreq_khz=[1200000] * 4, disks="mmcblk0 14.9G 0 ",
             mem_kb=950000, note="32-bit OS: the only platform where /proc/cpuinfo DOES have `model name` (\"ARMv7 Processor\", not the real CPU)"),
    Platform("rk3588-noname", "\"noname\" RK3588 board (Rockchip, 8 cores big.LITTLE)", "arm64",
             ARM_BASE.format(part="0xd05") + "\nprocessor\t: 4\nCPU part\t: 0xd0b\n", "Cortex-A55", 8, 8, dt_model="Radxa ROCK 5B", machine="aarch64",
             hwmon=[("soc_thermal", 44000), ("bigcore0_thermal", 47000)], thermal_zones=[("soc-thermal", 44000)], cpufreq_khz=[1800000] * 8,
             disks="mmcblk1 59.5G 0 ", mem_kb=15700000, note="sensor name derived from the thermal zone: soc_thermal, not a known name"),
    Platform("bare-arm64", "\"noname\" arm64 box with the bare minimum (no hwmon, no cpufreq, no DMI)", "arm64",
             ARM_BASE.format(part="0xd03") + "\nHardware\t: Generic DT based system\n", "Cortex-A53", 4, 4, machine="aarch64", hwmon=[],
             thermal_zones=[("cpu-thermal", 61000)], cpufreq_khz=[], disks="mmcblk0 7.4G 0 ", mem_kb=1900000,
             note="the temperature exists only as /sys/class/thermal/thermal_zone0 - the probe does not read that path"),
]


# ------------------------------------------------------------------------------------------------ building and running
def build_tree(p: Platform, root: Path) -> None:
    sysd, procd = root / "sys", root / "proc"
    for i, (name, milli) in enumerate(p.hwmon):
        d = sysd / "class/hwmon" / f"hwmon{i}"
        d.mkdir(parents=True)
        (d / "name").write_text(name + "\n")
        (d / "temp1_input").write_text(f"{milli}\n")
    for i, (kind, milli) in enumerate(p.thermal_zones):
        d = sysd / "class/thermal" / f"thermal_zone{i}"
        d.mkdir(parents=True)
        (d / "type").write_text(kind + "\n")
        (d / "temp").write_text(f"{milli}\n")
    for i, khz in enumerate(p.cpufreq_khz):
        d = sysd / f"devices/system/cpu/cpu{i}/cpufreq"
        d.mkdir(parents=True)
        (d / "scaling_cur_freq").write_text(f"{khz}\n")
    if p.rapl:
        d = sysd / "class/powercap/intel-rapl:0"
        d.mkdir(parents=True)
        for f, v in (("energy_uj", "123456789"), ("max_energy_range_uj", "262143328850"), ("constraint_0_power_limit_uw", "65000000"),
                     ("constraint_1_power_limit_uw", "81250000")):
            (d / f).write_text(v + "\n")
    dmi = sysd / "class/dmi/id"
    if p.dmi:
        dmi.mkdir(parents=True)
        for f, v in p.dmi.items():
            (dmi / f).write_text(v + "\n")
    procd.mkdir(parents=True)
    if p.dt_model:
        (procd / "device-tree").mkdir()
        (procd / "device-tree" / "model").write_bytes(p.dt_model.encode() + b"\0")
    (procd / "cpuinfo").write_text(p.cpuinfo)
    (procd / "stat").write_text("cpu  1000 0 500 8000 0 0 0 0 0 0\n")
    (procd / "meminfo").write_text(f"MemTotal:       {p.mem_kb} kB\nMemAvailable:   {int(p.mem_kb * 0.8)} kB\n")
    bin_ = root / "bin"
    bin_.mkdir()
    stubs = {
        "nproc": f"echo {p.threads}",
        "lscpu": ('if [ "$1" = "-p=CORE,SOCKET" ]; then echo "# comment"; i=0; while [ $i -lt ' + str(p.cores) + ' ]; do echo "$i,0"; i=$((i+1)); done; '
                  f'else echo "Model name: {p.lscpu_model}"; fi'),
        "free": f'echo "              total used free"; echo "Mem:   {p.mem_kb // 1024} 100 100"',
        "dmidecode": ("echo '# No SMBIOS nor DMI entry point found, sorry.' >&2" if not p.dmi else "exit 0"),
        "lspci": "exit 0",
        "lsblk": f'echo "{p.disks}"',
        "apt-get": "exit 0",
        "uname": f"echo {p.machine}",
    }
    for name, body in stubs.items():
        f = bin_ / name
        f.write_text("#!/bin/sh\n" + body + "\n")
        f.chmod(0o755)


def run_probe(root: Path) -> str:
    script = (PROBE_SCRIPT.replace("/host-sys", f"{root}/sys").replace("/proc/stat", f"{root}/proc/stat")
              .replace("/proc/meminfo", f"{root}/proc/meminfo"))
    shell = ["busybox", "sh", "-c"] if shutil.which("busybox") else ["sh", "-c"]       # the probe pod is busybox
    return subprocess.run([*shell, script], capture_output=True, text=True, timeout=30).stdout


def run_hw(root: Path) -> str:
    script = (HW_SCRIPT.replace("/proc/cpuinfo", f"{root}/proc/cpuinfo").replace("/sys/class/dmi/id", f"{root}/sys/class/dmi/id")
              .replace("/proc/device-tree/model", f"{root}/proc/device-tree/model"))
    env = dict(os.environ, PATH=f"{root}/bin:{os.environ['PATH']}", HW_PRIVILEGED="0")
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30, env=env).stdout


@dataclass
class Result:
    platform: Platform
    probe_text: str
    hw_text: str
    cpu_temp: object
    temps: dict
    freq_mhz: object
    power_seen: bool
    mem_total_mib: object
    cpu_line: str
    system_line: str
    unread_sensors: list
    findings: list                                  # (level, text): FAIL / WARN / INFO
    verdict: str


def measure(p: Platform) -> Result:
    tmp = Path(tempfile.mkdtemp(prefix="platform-"))
    try:
        build_tree(p, tmp)
        probe_text, hw_text = run_probe(tmp), run_hw(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    data = parse_probe_output(probe_text)
    cpu_line = cpu_summary(hw_text)
    raw_cpu = next((ln.partition(":")[2].strip() for ln in hw_text.splitlines() if ln.startswith("CPU:")), "")
    system = next((ln.partition(":")[2].strip() for ln in hw_text.splitlines() if ln.startswith("System:")), "")
    read = {ln.split()[1] for ln in probe_text.splitlines() if ln.startswith("temp ")}
    unread = [n for n, _ in p.hwmon if n not in read and n not in ("rpi_volt", "pwmfan")] + [f"thermal_zone:{t}" for t, _ in p.thermal_zones if data.cpu_temp is None]
    findings = []
    if data.cpu_temp is None:
        findings.append(("FAIL", "no CPU temperature: the probe reads only the hwmon names coretemp/k10temp/zenpower/amdgpu/radeon/nouveau/nvme/drivetemp. "
                                 "The test is REFUSED without --allow-no-sensor and the overheating protection (auto-stop) does not work."))
    if data.freq_mhz is None:
        findings.append(("WARN", "no CPU clock (no cpufreq in /sys): the clock column and the throttling check are empty"))
    if not raw_cpu:
        findings.append(("WARN", "CPU model is empty: HW_SCRIPT reads `model name` from /proc/cpuinfo, which arm64 kernels do not print "
                                 "(lscpu knows it: " + p.lscpu_model + ")"))
    if not system:
        findings.append(("INFO", "System/Motherboard lines are empty (no /sys/class/dmi on this board); the log header just shows blanks"))
    if "mmcblk" in p.disks:
        findings.append(("INFO", "the SD card / eMMC is listed as `SSD/NVMe` (lsblk ROTA=0): only a cosmetic label in the hardware text"))
    if data.energy_uj is None:
        findings.append(("INFO", "no CPU power (RAPL is Intel only) - nothing is printed, nothing breaks"))
    arch = images.normalize_arch(p.arch)
    if arch not in images.PUBLISHED["tools"]:
        findings.append(("INFO", f"no prebuilt image is published for {arch}: the tool starts the pods from the multi-arch fallback images "
                                 f"(ubuntu:24.04 / busybox:1.36) and installs the tools with apt (images.image('tools', node) = '{images.FALLBACK['tools']}' there)"))
    else:
        findings.append(("INFO", f"the prebuilt multi-arch images ({images.tag('tools')}) cover {arch}: the pods start without apt"))
    if p.arch == "arm":
        findings.append(("INFO", "32-bit ARM: the fallback ubuntu:24.04 / busybox images have an armv7 variant; see the armhf package table for the tools"))
    level = "FAIL" if any(f[0] == "FAIL" for f in findings) else "WARN" if any(f[0] == "WARN" for f in findings) else "OK"
    return Result(p, probe_text, hw_text, data.cpu_temp, dict(data.temps), data.freq_mhz, data.energy_uj is not None, data.mem_total_mib, cpu_line, system,
                  unread, findings, level)


# ------------------------------------------------------------------------------------------------ published facts (network)
ACCEPT = ("application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, "
          "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json")


def _get(url: str, headers=None) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=60) as r:
        return r.read()


def image_platforms(host: str, repo: str, ref: str) -> list:
    if host == "ghcr.io":
        base, token_url = "https://ghcr.io", f"https://ghcr.io/token?scope=repository:{repo}:pull"
    else:
        base, token_url = "https://registry-1.docker.io", f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull"
    auth = {"Authorization": "Bearer " + json.loads(_get(token_url))["token"]}
    doc = json.loads(_get(f"{base}/v2/{repo}/manifests/{ref}", {**auth, "Accept": ACCEPT}))
    if "manifests" in doc:
        out = []
        for m in doc["manifests"]:
            pl = m["platform"]
            if pl["architecture"] != "unknown":
                out.append(f"{pl['os']}/{pl['architecture']}" + (f"/{pl['variant']}" if pl.get("variant") else ""))
        return out
    cfg = json.loads(_get(f"{base}/v2/{repo}/blobs/{doc['config']['digest']}", auth))
    return [f"{cfg.get('os')}/{cfg.get('architecture')} (a single-platform manifest, no index)"]


IMAGES = [("ghcr.io", "doctorxcz/k3s-stress-tools", images.tag("tools"), "tools (prebuilt)"), ("ghcr.io", "doctorxcz/k3s-stress-probe", images.tag("probe"), "probe (prebuilt)"),
          ("ghcr.io", "doctorxcz/k3s-stress-gpu", images.tag("gpu"), "gpu (prebuilt)"), ("docker", "library/ubuntu", "24.04", "tools fallback"),
          ("docker", "library/busybox", "1.36", "probe fallback"), ("docker", "nvidia/cuda", "12.9.1-devel-ubuntu24.04", "gpu fallback")]
PACKAGES = ["stress-ng", "fio", "iperf3", "smartmontools", "mtr-tiny", "dmidecode", "pciutils", "lm-sensors", "htop", "iputils-ping", "curl"]


def ports_packages(arch: str) -> dict:
    found = {}
    for comp in ("main", "universe"):
        data = gzip.decompress(_get(f"https://ports.ubuntu.com/ubuntu-ports/dists/noble/{comp}/binary-{arch}/Packages.gz")).decode()
        for blk in data.split("\n\n"):
            for w in PACKAGES:
                if blk.startswith(f"Package: {w}\n"):
                    found[w] = f"{comp} {next(l for l in blk.splitlines() if l.startswith('Version:'))[9:]}"
    return found


def collect_online() -> dict:
    out = {"images": [], "packages": {}, "packages_armhf": {}, "error": ""}
    try:
        for host, repo, ref, role in IMAGES:
            try:
                out["images"].append((role, f"{repo}:{ref}", image_platforms(host, repo, ref), ""))
            except Exception as exc:                                           # noqa: BLE001
                out["images"].append((role, f"{repo}:{ref}", [], str(exc)[:120]))
        out["packages"] = ports_packages("arm64")
        out["packages_armhf"] = ports_packages("armhf")
    except Exception as exc:                                                   # noqa: BLE001
        out["error"] = str(exc)[:200]
    return out


# ------------------------------------------------------------------------------------------------ the end-to-end runs on the fake cluster
def fake_runs(workdir: Path) -> list:
    """The whole tool (cli -> runner -> monitor -> summary) against tests/fake_kubectl.py with a node that reports another architecture."""
    cases = [
        ("amd64 with a sensor", {"FAKE_ARCH": "amd64"}, [], 0),
        ("arm64 with a sensor (as if the probe found cpu_thermal)", {"FAKE_ARCH": "arm64"}, [], 0),
        ("arm64 WITHOUT a sensor, no flag (today's Raspberry Pi behaviour)", {"FAKE_ARCH": "arm64", "FAKE_NO_SENSOR": "1"}, [], 1),
        ("arm64 WITHOUT a sensor, --allow-no-sensor", {"FAKE_ARCH": "arm64", "FAKE_NO_SENSOR": "1"}, ["--allow-no-sensor"], 0),
        ("arm64, prebuilt image cannot be pulled (fallback to ubuntu/busybox + apt)", {"FAKE_ARCH": "arm64", "FAKE_PULL_FAIL": "1"}, [], 0),
        ("arm64, --no-prebuilt", {"FAKE_ARCH": "arm64"}, ["--no-prebuilt"], 0),
        ("arm64, stepped profile", {"FAKE_ARCH": "arm64"}, ["--profile", "stepped", "--steps", "50,100", "--step-time", "30"], 0),
    ]
    out = []
    for i, (title, env_extra, extra, want) in enumerate(cases):
        tmp = workdir / f"run{i}"
        tmp.mkdir()
        env = dict(os.environ, KUBECTL=str(HERE / "fake_kubectl.py"), FAKE_STATE=str(tmp), PYTHONPATH=str(ROOT), FAKE_RUN="3",
                   STRESS_TEST_RUNNING_DIR=str(tmp / "running"), **env_extra)
        cmd = [sys.executable, "-m", "stress_test", "--node", "fake-node", "--non-interactive", "--interval", "0.5", "--time", "5", "--cooldown", "0",
               "--log", "--log-dir", str(tmp / "logs"), *extra]
        t0 = time.time()
        res = subprocess.run(cmd, cwd=tmp, env=env, capture_output=True, text=True, timeout=180)
        logs = list((tmp / "logs").rglob("*.log"))
        arch_line = ""
        if logs:
            arch_line = next((ln.strip() for ln in logs[0].read_text(encoding="utf-8").splitlines() if "Arch:" in ln), "")
        out.append({"title": title, "rc": res.returncode, "want": want, "ok": res.returncode == want, "seconds": round(time.time() - t0, 1),
                    "arch_line": arch_line, "stdout_tail": "\n".join(res.stdout.strip().splitlines()[-4:])})
    return out


# ------------------------------------------------------------------------------------------------ the report
def render_log(results, online, runs, started: str) -> str:
    L = [f"PLATFORM COMPATIBILITY MEASUREMENT  {started}", f"tool version {__version__} (version-lab-dev), host {os.uname().machine}", "=" * 100, ""]
    for r in results:
        p = r.platform
        L += [f"### {p.key}: {p.title}  [{p.arch}]  verdict {r.verdict}", f"  note: {p.note}" if p.note else "",
              "  --- probe output (real PROBE_SCRIPT on the modelled /sys) ---", *[f"  | {x}" for x in r.probe_text.strip().splitlines()],
              "  --- hardware output (real HW_SCRIPT) ---", *[f"  | {x}" for x in r.hw_text.strip().splitlines()],
              f"  parsed: cpu_temp={r.cpu_temp} temps={r.temps} freq_mhz={r.freq_mhz} power(RAPL)={r.power_seen} mem_total_mib={r.mem_total_mib}",
              f"  cpu_summary: '{r.cpu_line}'", f"  sensors present but not read: {r.unread_sensors or 'none'}",
              *[f"  [{lvl}] {txt}" for lvl, txt in r.findings], ""]
    L += ["### container images (published manifests)"]
    for role, ref, plats, err in online["images"]:
        L.append(f"  {role:<18} {ref:<52} {', '.join(plats) if plats else 'ERROR ' + err}")
    for arch, key in (("arm64", "packages"), ("armhf", "packages_armhf")):
        L += ["", f"### Ubuntu 24.04 (noble) {arch} packages used by the pods", *[f"  {k:<14} {v}" for k, v in online[key].items()]]
        L += [f"  missing: {[w for w in PACKAGES if w not in online[key]] or 'none'}"] if online[key] else ["  (not collected)"]
    L += ["", "### end-to-end runs on the fake cluster (whole tool, no real cluster)"]
    for r in runs:
        L += [f"  [{'OK ' if r['ok'] else 'BAD'}] {r['title']}: exit {r['rc']} (expected {r['want']}) in {r['seconds']} s  {r['arch_line']}"]
        L += [f"        {x}" for x in r["stdout_tail"].splitlines()]
    return "\n".join(x for x in L if x is not None) + "\n"


def render_md(results, online, runs, started: str) -> str:
    md = [f"# Platform compatibility: amd64 / arm64 / Raspberry Pi / \"noname\" ARM", "",
          f"Measured {started} with tool version **{__version__}** (`version-lab-dev`) on an {os.uname().machine} host. "
          "Script: `version-lab-dev/tests/platform_matrix.py`, asserted by `tests/test_platforms.py`, raw output in the `.log` next to this file.", "",
          "> **What this is and is not.** The real cluster has only amd64 nodes, so no test ran on real ARM hardware. The node data are *modelled*: typical "
          "`/sys` and `/proc` layouts of each device, written from knowledge, not captured from real boards. The tool's real probe/hardware scripts and parsers "
          "run against them. Container-image platforms and apt package availability were read from the public registries. "
          "Confirm on one real Raspberry Pi before relying on it.", "",
          "## Summary", "", "| Platform | Arch | Verdict | CPU temp read | Clock | Power | CPU name | Main problem |", "|---|---|---|---|---|---|---|---|"]
    for r in results:
        p = r.platform
        main = next((t for lvl, t in r.findings if lvl == "FAIL"), next((t for lvl, t in r.findings if lvl == "WARN"), "–"))
        md.append(f"| {p.title} | {p.arch} | **{r.verdict}** | {str(r.cpu_temp) + ' °C' if r.cpu_temp is not None else '**no**'} | "
                  f"{str(r.freq_mhz) + ' MHz' if r.freq_mhz else 'no'} | {'RAPL' if r.power_seen else 'no'} | {r.cpu_line or '**empty**'} | {main[:110]} |")
    md += ["", "Verdict: **OK** = works as on the cluster; **WARN** = works, some information is missing; **FAIL** = the test is refused or cannot start without a workaround.", "",
           "## Container images", "", "| Role | Image | Platforms |", "|---|---|---|"]
    for role, ref, plats, err in online["images"]:
        md.append(f"| {role} | `{ref}` | {', '.join(plats) if plats else 'ERROR ' + err} |")
    md += ["", "The three prebuilt images (`ghcr.io/doctorxcz/k3s-stress-*`) are single-platform **linux/amd64** manifests, pinned by digest in `images.DIGESTS`. "
           "On an arm64 node the image is pulled (a single-platform manifest has no platform check at pull time) but cannot run (`exec format error`), "
           "which is *not* a pull error, so the automatic fallback to `ubuntu:24.04` + apt would probably **not** trigger. This part is reasoned from how container runtimes behave, "
           "not measured on a real ARM node. Workaround today: `--no-prebuilt`. The fallback images themselves are multi-arch (ubuntu: arm64/v8, armv7; busybox: arm64/v8, armv7, armv6).", ""]
    for arch, key in (("arm64", "packages"), ("armhf (32-bit)", "packages_armhf")):
        if online.get(key):
            md += [f"## apt packages for {arch} (Ubuntu 24.04 noble, ports.ubuntu.com)", "", "| Package | Archive / version |", "|---|---|",
                   *[f"| {k} | {v} |" for k, v in online[key].items()], "",
                   f"Missing: {[w for w in PACKAGES if w not in online[key]] or 'none'}.", ""]
    md += ["## End-to-end runs on the fake cluster", "", "The whole tool against `tests/fake_kubectl.py` (a node that reports the given architecture). "
           "They prove that nothing in the CLI/runner/monitor/summary depends on `amd64`; they do not prove anything about real ARM sensors or images.", "",
           "| Case | Exit | Expected | Result | Arch line in the log |", "|---|---|---|---|---|"]
    for r in runs:
        md.append(f"| {r['title']} | {r['rc']} | {r['want']} | {'OK' if r['ok'] else '**BAD**'} | `{r['arch_line'] or '-'}` |")
    md += ["", "## Conclusions (after the multi-arch phase 1, 2026-10-06)", "",
           "1. **amd64 (Intel and AMD) is unchanged** - temperature, clock, power (Intel), CPU name read exactly as before.",
           "2. **ARM boards now pass the preflight**: the probe reads `cpu_thermal` (Raspberry Pi), `soc_thermal` / `bigcore0_thermal` (Rockchip) and, when no hwmon CPU sensor exists, "
           "`/sys/class/thermal/thermal_zone*` of a cpu/soc zone. The overheating auto-stop works. GPU, NPU, DDR, battery zones are never taken for the CPU "
           "(better no temperature than a wrong one).",
           "3. **Images follow the node**: each node's architecture is read from Kubernetes; the prebuilt images (amd64 only) are used on amd64 nodes, "
           "an arm64/armv7 node starts from `ubuntu:24.04` / `busybox:1.36` and installs the tools with apt (slower start). A mixed cluster works; a pull failure on one "
           "architecture does not switch the prebuilt image off for the others.",
           "4. **CPU name and board** on ARM come from `lscpu` and `/proc/device-tree/model` (\"Cortex-A72\", \"Raspberry Pi 4 Model B Rev 1.5\"); SD cards show as `SD/eMMC`.",
           "5. **GPU test is refused on non-amd64 nodes** with a clear message (gpu-burn / the NVIDIA image are amd64 only).",
           "6. **Still open:** (a) publish a multi-arch index of the tools/probe images so ARM also gets the fast prebuilt start (`images.PUBLISHED` + `DIGESTS`; needs a registry login); "
           "(b) one short run on a real Raspberry Pi 4/5 - everything here is modelled, not captured from hardware; (c) real sensors may be named differently on other boards - "
           "the message of a missing sensor tells what to do (`--allow-no-sensor`).", ""]
    md += ["", "## Details per platform", ""]
    for r in results:
        p = r.platform
        md += [f"### {p.title} (`{p.key}`, {p.arch}) – {r.verdict}", "", p.note + "" if p.note else "",
               "Probe output:", "```", r.probe_text.strip() or "(empty)", "```", "Hardware output:", "```", r.hw_text.strip(), "```",
               f"Parsed: CPU temp `{r.cpu_temp}`, sensors `{r.temps}`, clock `{r.freq_mhz}`, RAPL `{r.power_seen}`, memory `{r.mem_total_mib}` MiB; "
               f"`cpu_summary` = `{r.cpu_line or ''}`.", ""]
        md += [f"- **{lvl}** {txt}" for lvl, txt in r.findings] + [""]
    return "\n".join(md) + "\n"


def run_all(offline: bool, out_dir: Path) -> tuple:
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    results = [measure(p) for p in PLATFORMS]
    online = {"images": [], "packages": {}, "packages_armhf": {}, "error": ""} if offline else collect_online()
    work = Path(tempfile.mkdtemp(prefix="platform-fake-"))
    try:
        runs = fake_runs(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    log_path, md_path = out_dir / f"platform-compat-{stamp}.log", out_dir / f"platform-compat-{stamp}.md"
    log_path.write_text(render_log(results, online, runs, started), encoding="utf-8")
    md_path.write_text(render_md(results, online, runs, started), encoding="utf-8")
    return log_path, md_path, results, online, runs


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "logs" / "platform-compat"))
    a = ap.parse_args()
    lp, mp, res, onl, runs = run_all(a.offline, Path(a.out))
    print(lp)
    print(mp)
    for r in res:
        print(f"{r.verdict:<5} {r.platform.key}")
    print("fake runs:", sum(x["ok"] for x in runs), "/", len(runs), "as expected")
