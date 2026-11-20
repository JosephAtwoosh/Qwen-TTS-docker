#!/usr/bin/env bash
# Download the Base checkpoint (which includes the 12 Hz speech tokenizer).
set -euo pipefail

MODEL_REPO="${MODEL_REPO:-Qwen/Qwen3-TTS-12Hz-1.7B-Base}"
MODEL_DIR="${MODEL_DIR:-/models/Qwen3-TTS-12Hz-1.7B-Base}"

echo "fetching ${MODEL_REPO} -> ${MODEL_DIR} (~4 GB)"
python - "$MODEL_REPO" "$MODEL_DIR" <<'PY'
import os
import sys
from huggingface_hub import snapshot_download

repo, dest = sys.argv[1], sys.argv[2]
path = snapshot_download(
    repo_id=repo,
    local_dir=dest,
    token=os.environ.get("HF_TOKEN") or None,
    max_workers=4,
    resume_download=True,
)
print(f"done: {path}")
PY

du -sh "$MODEL_DIR"
