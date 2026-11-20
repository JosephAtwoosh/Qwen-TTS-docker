#!/usr/bin/env bash
# Vast.ai on-start entry point.
#
# Vast's `/.launch` ignores the image CMD, so the template must call this. It:
#   1. refuses to expose an open API when TTS_API_KEY is missing,
#   2. downloads the checkpoint once (the /models volume keeps it),
#   3. logs a doctor preflight,
#   4. starts the service (which also starts the 3-minute GPU probe).
set -euo pipefail
cd /app

if [[ -z "${TTS_API_KEY:-}" && "${ALLOW_OPEN:-0}" != "1" ]]; then
  echo "TTS_API_KEY is not set; refusing to start an open TTS API." >&2
  echo "Set TTS_API_KEY in the Vast template, or ALLOW_OPEN=1 for a private debug box." >&2
  exit 2
fi

MODEL_DIR="${MODEL_ID:-/models/Qwen3-TTS-12Hz-1.7B-Base}"
if [[ ! -f "$MODEL_DIR/model.safetensors" ]]; then
  echo "== downloading model weights =="
  /app/scripts/download-models.sh
fi

echo "== doctor =="
/app/scripts/doctor.sh || true

exec /usr/local/bin/qwen-tts-entrypoint serve
