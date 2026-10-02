#!/bin/sh
# gpu_burn with the compare kernel (PTX) that fits the card: the highest compute capability <= the card's (e.g. 6.1 -> compare-61.fatbin, 8.9 -> compare-89.fatbin).
# GPU_BURN_CC=NN overrides the detection. Everything else is passed to the real gpu_burn.
dir=/opt/gpu-burn
cc="${GPU_BURN_CC:-$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -n1 | tr -d '. \r')}"
best=""
for f in "$dir"/compare-*.fatbin; do
    n=${f##*/compare-}; n=${n%.fatbin}
    if [ -n "$cc" ] && [ "$n" -le "$cc" ] 2>/dev/null; then
        if [ -z "$best" ] || [ "$n" -gt "$best" ]; then best=$n; fi
    fi
done
[ -n "$best" ] || best=61
exec "$dir/gpu_burn" -c "$dir/compare-$best.fatbin" "$@"
