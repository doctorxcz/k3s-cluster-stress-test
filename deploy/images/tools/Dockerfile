# k3s-stress-tools: everything the CPU / RAM / disk / network tests, the hardware and SMART detection and the GPU scan need, already installed.
# Same base and same Ubuntu packages the tool used to install with apt in every pod, so results stay comparable with older logs and baselines.
FROM docker.io/library/ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends \
      stress-ng fio smartmontools iperf3 iputils-ping mtr-tiny curl ca-certificates \
      dmidecode pciutils usbutils lshw lm-sensors nvme-cli \
      htop sysstat iotop-c procps psmisc ethtool dnsutils traceroute netcat-openbsd iproute2 \
      numactl jq bc less \
 && apt-get clean && rm -rf /var/lib/apt/lists/* /usr/share/doc/* /usr/share/man/* /usr/share/locale/*

LABEL org.opencontainers.image.title="k3s-stress-tools" \
      org.opencontainers.image.description="Tools for the k3s cluster stress test: stress-ng, fio, smartmontools, iperf3, ping, mtr, dmidecode, pciutils, lm-sensors, htop ..." \
      org.opencontainers.image.source="https://github.com/doctorxcz/k3s-cluster-stress-test" \
      org.opencontainers.image.licenses="MIT"
CMD ["sleep", "infinity"]
