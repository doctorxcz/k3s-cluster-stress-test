# k3s-stress-probe: the temperature probe and the dashboard probes (they only read /sys and /proc, so busybox is all they need).
# Pinned, from our own registry path (no Docker Hub pull limits).
FROM docker.io/library/busybox:1.36

LABEL org.opencontainers.image.title="k3s-stress-probe" \
      org.opencontainers.image.description="Read-only probe for the k3s cluster stress test (busybox)" \
      org.opencontainers.image.source="https://github.com/doctorxcz/k3s-cluster-stress-test" \
      org.opencontainers.image.licenses="MIT"
CMD ["sleep", "infinity"]
