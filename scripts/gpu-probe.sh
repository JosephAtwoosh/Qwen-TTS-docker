#!/usr/bin/env bash
# Sample the GPU every GPU_PROBE_INTERVAL seconds into a CSV on /output.
#
# Started automatically by `qwen-tts-entrypoint serve` (disable with
# GPU_PROBE=0). A 3-minute cadence is 480 rows/day, small enough to keep for
# the whole render and read afterwards.
set -uo pipefail

INTERVAL="${GPU_PROBE_INTERVAL:-180}"
OUT="${GPU_PROBE_OUT:-/output/gpu-probe.csv}"

mkdir -p "$(dirname "$OUT")"
if [[ ! -s "$OUT" ]]; then
  printf 'timestamp,name,util_pct,mem_used_mib,mem_total_mib,temp_c,power_w\n' > "$OUT"
fi

while true; do
  line="$(nvidia-smi \
    --query-gpu=timestamp,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw \
    --format=csv,noheader,nounits 2>/dev/null | head -1 | sed 's/, /,/g')"
  if [[ -n "$line" ]]; then
    printf '%s\n' "$line" >> "$OUT"
  else
    printf '%s,unavailable\n' "$(date -Is)" >> "$OUT"
  fi
  sleep "$INTERVAL"
done
