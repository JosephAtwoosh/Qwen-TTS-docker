#!/usr/bin/env bash
# Container entrypoint. The first argument is a subcommand; anything else is
# executed directly, so `docker run --rm image bash` still works.
set -euo pipefail

# Vast's launcher and `docker exec` sessions can start in /root; the package is
# at /app, so never rely on the caller's working directory.
cd /app

cmd="${1:-serve}"
case "$cmd" in
  serve)
    # Sample the GPU for the whole render, not just while an SSH session is up.
    if [[ "${GPU_PROBE:-1}" == "1" ]] && command -v nvidia-smi >/dev/null 2>&1; then
      if ! command -v pgrep >/dev/null 2>&1 \
        || ! pgrep -f '[g]pu-probe.sh' >/dev/null 2>&1; then
        nohup /app/scripts/gpu-probe.sh >/dev/null 2>&1 &
      fi
    fi
    if [[ "${GPU_WATCHDOG:-1}" == "1" ]] && command -v python3 >/dev/null 2>&1; then
      if ! command -v pgrep >/dev/null 2>&1 \
        || ! pgrep -f '[g]pu_watchdog.py' >/dev/null 2>&1; then
        WATCH_INTERVAL="${GPU_PROBE_INTERVAL:-120}" \
          nohup python3 /app/scripts/gpu_watchdog.py >/dev/null 2>&1 &
      fi
    fi
    exec python -m app.server
    ;;
  import-voices)
    shift
    exec node /app/tools/import_voices.js --voices-dir "${VOICES_DIR:-/voices}" "$@"
    ;;
  download-models)
    exec /app/scripts/download-models.sh
    ;;
  doctor)
    exec /app/scripts/doctor.sh
    ;;
  test)
    exec python -m pytest -q /app/tests
    ;;
  shell)
    exec bash
    ;;
  *)
    exec "$@"
    ;;
esac
