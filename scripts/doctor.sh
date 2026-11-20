#!/usr/bin/env bash
# Preflight for a rented GPU. Run before spending an hour on a broken host.
set -uo pipefail

fail=0
ok()   { printf '  ok    %s\n' "$*"; }
bad()  { printf '  FAIL  %s\n' "$*"; fail=$((fail + 1)); }
warn() { printf '  warn  %s\n' "$*"; }

echo "== host =="
echo "  $(uname -srm)"
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=name,memory.total,driver_version,compute_cap \
             --format=csv,noheader 2>/dev/null | while IFS= read -r line; do echo "  gpu   $line"; done
else
  bad "nvidia-smi not found (no driver / not a GPU container)"
fi

echo "== python stack =="
python - <<'PY' || fail=$((fail + 1))
import sys
try:
    import torch
except Exception as exc:
    print(f"  FAIL  torch import: {exc}")
    sys.exit(1)
print(f"  ok    torch {torch.__version__} (cuda build {torch.version.cuda})")
print(f"  ok    cuda available: {torch.cuda.is_available()}")
if torch.cuda.is_available():
    print(f"  ok    device: {torch.cuda.get_device_name(0)}")
    free, total = torch.cuda.mem_get_info()
    print(f"  ok    vram: {free/2**30:.1f} GiB free / {total/2**30:.1f} GiB total")
    if total < 7 * 2**30:
        print("  warn  1.7B bf16 wants ~6 GiB; this card is tight")
try:
    import qwen_tts  # noqa: F401
    print("  ok    qwen_tts importable")
except Exception as exc:
    print(f"  FAIL  qwen_tts import: {exc}")
    sys.exit(1)
PY

echo "== model weights =="
MODEL_DIR="${MODEL_ID:-/models/Qwen3-TTS-12Hz-1.7B-Base}"
if [[ -f "$MODEL_DIR/model.safetensors" ]]; then
  size=$(du -sh "$MODEL_DIR" | cut -f1)
  ok "weights present at $MODEL_DIR ($size)"
else
  bad "no model.safetensors under $MODEL_DIR — run: download-models"
fi
if [[ -d "$MODEL_DIR/speech_tokenizer" ]]; then
  ok "speech tokenizer present"
else
  warn "no speech_tokenizer/ directory; Qwen3TTSModel may try to fetch it"
fi

echo "== voices =="
python - <<'PY' || fail=$((fail + 1))
import os
from pathlib import Path
from app.voices import VoiceRegistry
root = Path(os.environ.get("VOICES_DIR", "/voices"))
registry = VoiceRegistry(root)
print(f"  ok    {len(registry)} voices in {root}")
for problem in registry.errors[:10]:
    print(f"  warn  {problem}")
if not len(registry):
    print("  warn  no voices: import them, or unpack qwen-voices.tar.gz into /voices")
PY

echo "== tools =="
command -v ffmpeg >/dev/null 2>&1 && ok "ffmpeg $(ffmpeg -version 2>/dev/null | head -1 | awk '{print $3}')" || bad "ffmpeg missing"
command -v node >/dev/null 2>&1 && ok "node $(node --version)" || warn "node missing (voice import unavailable in-container)"

echo "== configuration =="
if [[ -n "${TTS_API_KEY:-}" ]]; then
  ok "TTS_API_KEY is set"
else
  warn "TTS_API_KEY is empty — anyone who reaches the port can spend your GPU time"
fi
echo "  ..... output dir ${OUTPUT_DIR:-/output} ($(df -h "${OUTPUT_DIR:-/output}" 2>/dev/null | awk 'NR==2 {print $4 " free"}'))"

echo
if (( fail > 0 )); then
  echo "doctor: $fail check(s) failed"
  exit 1
fi
echo "doctor: ready"
