# Prebuilt images of the tool

Since 1.18.0 the pods of the tool start from three ready-made images instead of installing their tools with `apt-get` in every single test
(on a slow node that was 130 s per test - mostly downloading and processing package lists):

| image | for | contains |
|---|---|---|
| `k3s-stress-tools` | CPU / RAM / disk / network tests, hardware and SMART detection, GPU scan | Ubuntu 24.04 + `stress-ng`, `fio`, `smartmontools`, `iperf3`, `iputils-ping`, `mtr`, `curl`, `dmidecode`, `pciutils`, `lshw`, `usbutils`, `lm-sensors`, `nvme-cli`, `htop`, `sysstat`, `iotop`, `ethtool`, `dig`, `traceroute`, `netcat`, `iproute2`, `numactl`, `jq`, `bc` (the same Ubuntu packages as before, so results stay comparable with older logs and baselines) |
| `k3s-stress-gpu` | the GPU test | the CUDA runtime + `gpu-burn` (pinned commit) built for compute capabilities 6.1 / 7.0 / 7.5 / 8.0 / 8.6 / 8.9 / 9.0 / 12.0; a wrapper picks the kernel that fits the card |
| `k3s-stress-probe` | the temperature probe and the dashboard probes | busybox (pinned) |

Default location: `ghcr.io/doctorxcz/<name>:<IMAGE_TAG>` (for example `ghcr.io/doctorxcz/k3s-stress-tools:1.18.0`). `IMAGE_TAG` (in `stress_test/images.py`) is the tag of the published images - **not** the version of the tool, so the tool can be released without new images.

**Architectures.** The tool reads the architecture of every node from Kubernetes and gives each pod an image that can run there. The 1.18.0 images are `linux/amd64` only, so an arm64 / armv7 node (Raspberry Pi, any ARM board) starts from the multi-arch fallback images (`ubuntu:24.04`, `busybox:1.36`) and installs the tools with apt (slower start, works). `images.PUBLISHED` says for which architectures the images of `IMAGE_TAG` exist; once a multi-arch tag is published (below) ARM nodes use the prebuilt images too. The GPU image stays amd64 only (gpu-burn and the NVIDIA runtime are built for x86 cards; the GPU test is refused on other nodes).
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
### Multi-arch (amd64 + arm64) - locally
```bash
sudo apt install qemu-user-binfmt qemu-user        # once (Ubuntu 26.04; on Debian / Ubuntu <= 24.04 it is `qemu-user-static`): lets this amd64 machine run arm64 containers (an arm64 machine needs nothing)
./build.sh 1.19.0 --platforms linux/amd64,linux/arm64 --only "tools probe"     # builds a manifest list, tests the tools on BOTH platforms (arm64 under QEMU: minutes); pushes nothing
./publish.sh 1.19.0                                # shows what it will push, asks, refuses to replace an existing tag; podman must be logged in to ghcr.io
./check_published.py 1.19.0                        # reads the registry: platforms + digests + the snippet for stress_test/images.py
```
Then put the snippet into `stress_test/images.py` (`IMAGE_TAG`, `DIGESTS` with the digests of the INDEXES, `PUBLISHED` with `arm64`), set new packages to public, and run `./check_published.py --verify-code` (exit 1 when the code and the registry disagree). Use a NEW tag (not 1.18.0): the digest pin of 1.18.0 and the nodes' image caches refer to it.

## Publish (GitHub Actions)
The workflow `.github/workflows/images.yml` (kept in the development repository, not in the release; to use it, it has to live in a GitHub repository) builds and pushes `tools` and `probe` for `linux/amd64` + `linux/arm64` and `gpu` for `linux/amd64` when a version tag (`v1.19.0`) is pushed, smoke-tests every platform (arm64 under QEMU), and records
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
