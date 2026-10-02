# Prebuilt images of the tool

Since 1.18.0 the pods of the tool start from three ready-made images instead of installing their tools with `apt-get` in every single test
(on a slow node that was 130 s per test - mostly downloading and processing package lists):

| image | for | contains |
|---|---|---|
| `k3s-stress-tools` | CPU / RAM / disk / network tests, hardware and SMART detection, GPU scan | Ubuntu 24.04 + `stress-ng`, `fio`, `smartmontools`, `iperf3`, `iputils-ping`, `mtr`, `curl`, `dmidecode`, `pciutils`, `lshw`, `usbutils`, `lm-sensors`, `nvme-cli`, `htop`, `sysstat`, `iotop`, `ethtool`, `dig`, `traceroute`, `netcat`, `iproute2`, `numactl`, `jq`, `bc` (the same Ubuntu packages as before, so results stay comparable with older logs and baselines) |
| `k3s-stress-gpu` | the GPU test | the CUDA runtime + `gpu-burn` (pinned commit) built for compute capabilities 6.1 / 7.0 / 7.5 / 8.0 / 8.6 / 8.9 / 9.0 / 12.0; a wrapper picks the kernel that fits the card |
| `k3s-stress-probe` | the temperature probe and the dashboard probes | busybox (pinned) |

Default location: `ghcr.io/doctorxcz/<name>:<version of the tool>` (for example `ghcr.io/doctorxcz/k3s-stress-tools:1.18.0`), for `linux/amd64`. The published images are amd64 only; for arm64 nodes build them on an arm64 machine (`./build.sh`, no QEMU needed there) and use `--registry`, or use the GitHub workflow below.
Each image is pulled **once per node** and stays in the node's image cache; later tests start in seconds.

## What the tool does with them
- Every pod script first checks `command -v <tool>` and calls `apt-get` only for what is missing. So the **same scripts work in both modes**.
- When a prebuilt image **cannot be pulled** (not published yet, no access, no internet) the tool says so, replaces that pod by one on plain Ubuntu / busybox
  (`ubuntu:24.04`, `busybox:1.36`, for the GPU `nvidia/cuda:12.9.1-devel-ubuntu24.04`) and goes on with apt - slower, with a warning, exactly like 1.17.0.
- `--no-prebuilt` skips the prebuilt images completely. `--registry HOST[:PORT]/PATH` takes all three from your own registry (see below).
  A custom GPU image (`--gpu-image`) is used as it is, without a fallback.
- The images run as root with the same pod settings as before (no privileged mode except the existing SMART / `--hw-privileged` pods).

## Build and test locally (podman or docker)
```bash
cd deploy/images
./build.sh                         # builds tools, probe and gpu (amd64) and checks that the tools run; nothing is pushed
./build.sh --only "tools probe"    # the GPU image downloads the CUDA devel image (~3.5 GB)
./build.sh --save ~/images         # also writes k3s-stress-<name>-<tag>.tar files
```
A multi-arch (`linux/amd64` + `linux/arm64`) publication is done by the GitHub workflow (`.github/workflows/images.yml` in the lab / main repository, QEMU); locally `arm64` needs an arm64 machine or `qemu-user-static`.

## Publish (GitHub Actions)
The workflow `.github/workflows/images.yml` (kept in the development repository, not in the release) builds and pushes all three images for `linux/amd64` and `linux/arm64` when a version tag (`v1.18.0`) is pushed, and records
the digests in the job summary. After the first push set each package to **public** (GitHub > your profile > Packages > the package > Package settings >
Change visibility), otherwise the nodes cannot pull it without a login. To pin exactly the tested bytes put the digests into `DIGESTS` in
`stress_test/images.py` (the pods then use `name:tag@sha256:...`).

## A cluster that cannot reach ghcr.io (offline, company network)
1. On a computer with internet: `./pull-to-folder.sh images-offline` (saves the three images as tar files).
2. Carry the folder over and load it into **your** registry (any registry the nodes can reach): `./push-to-registry.sh registry.local:5000/lab images-offline`
3. Run the tool with `--registry registry.local:5000/lab` (the tags stay the same). A registry without TLS must be allowed in `/etc/rancher/k3s/registries.yaml`
   of the nodes.

## Fewer downloads on a big cluster
Every node pulls an image itself (about 170 MB for `tools`, ~1.2 GB for `gpu`). k3s can share images between the nodes of the cluster: start the servers with
`--embedded-registry` and add a `mirrors: "*": {}` entry to `/etc/rancher/k3s/registries.yaml` (the embedded distributed registry, k3s 1.29+); a node then
takes the image from a neighbour that already has it. That is a setting of the cluster, not of this tool.
