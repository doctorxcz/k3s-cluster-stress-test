# k3s-stress-gpu: the CUDA runtime with gpu-burn already built, for several GPU generations.
# Stage 1 builds gpu-burn (pinned commit) with the CUDA devel image: one `compare-<cc>.fatbin` (the compare kernel, PTX for that compute capability) per compute capability.
# Stage 2 is the much smaller image: gpu_burn + the compare kernels + a wrapper `gpu_burn` that picks the best kernel for the card it finds
# (the highest compute capability that is not above the card's; PTX of an older generation runs on newer cards through the driver's JIT).
ARG CUDA_VERSION=12.9.1
FROM docker.io/nvidia/cuda:${CUDA_VERSION}-devel-ubuntu24.04 AS build
ARG GPU_BURN_REPO=https://github.com/wilicc/gpu-burn
ARG GPU_BURN_COMMIT=3ead140434da9473582b68452f7115967a7a0581
# compute capabilities to build for (6.1 = Pascal, e.g. Quadro P620; 7.0 Volta; 7.5 Turing; 8.0 / 8.6 Ampere; 8.9 Ada; 9.0 Hopper; 12.0 Blackwell)
ARG COMPUTE_LIST="61 70 75 80 86 89 90 120"
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends git make g++ ca-certificates \
 && git init -q /src && cd /src && git fetch -q --depth 1 ${GPU_BURN_REPO} ${GPU_BURN_COMMIT} && git checkout -q FETCH_HEAD \
 && mkdir -p /out && make -s COMPUTE=61 >/dev/null && cp gpu_burn /out/gpu_burn \
 && for cc in ${COMPUTE_LIST}; do rm -f compare.fatbin && make -s COMPUTE=${cc} compare.fatbin >/dev/null 2>&1 && cp compare.fatbin /out/compare-${cc}.fatbin || echo "skipped compute ${cc}"; done \
 && ls /out

# the `base` image (about 0.3 GB) + only what gpu_burn links against (cuBLAS, the CUDA runtime); the NVIDIA `runtime` image is 3.9 GB (NCCL, cuDNN ...)
FROM docker.io/nvidia/cuda:${CUDA_VERSION}-base-ubuntu24.04
ARG CUDA_PKG_VERSION=12-9
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends libcublas-${CUDA_PKG_VERSION} cuda-cudart-${CUDA_PKG_VERSION} \
 && apt-get clean && rm -rf /var/lib/apt/lists/*
COPY --from=build /out/ /opt/gpu-burn/
COPY gpu_burn_wrapper.sh /usr/local/bin/gpu_burn
RUN chmod 0755 /usr/local/bin/gpu_burn /opt/gpu-burn/gpu_burn
LABEL org.opencontainers.image.title="k3s-stress-gpu" \
      org.opencontainers.image.description="gpu-burn (pinned commit) built for several NVIDIA GPU generations, for the k3s cluster stress test" \
      org.opencontainers.image.source="https://github.com/doctorxcz/k3s-cluster-stress-test" \
      org.opencontainers.image.licenses="MIT"
CMD ["sleep", "infinity"]
