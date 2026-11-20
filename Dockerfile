# syntax=docker/dockerfile:1.7
#
# Qwen3-TTS-12Hz-1.7B-Base in a CUDA 12.8 image.
#
# The base is PyTorch's runtime image because it already carries the exact
# torch/torchaudio pair (2.8.0 + cu128) that qwen-tts resolves against. Weights
# are NOT baked in: they are ~4 GB and live on the /models volume.
#
# Why CUDA 12.8 rather than 13.x: Vast.ai hosts commonly run R550/R570 drivers.
# CUDA 12.x minor-version compatibility means a 12.8 container starts on any
# 12.x driver >= 525, while a 13.x image would refuse to start on those hosts.

FROM node:20-bookworm-slim AS node

FROM pytorch/pytorch:2.8.0-cuda12.8-cudnn9-runtime

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models/.hf \
    TOKENIZERS_PARALLELISM=false \
    PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
    PATH=/opt/conda/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
        libsndfile1 \
        sox \
        libsox-fmt-all \
        git \
    && rm -rf /var/lib/apt/lists/*

# Node only runs tools/import_voices.js (voice import). A copied static binary
# avoids adding a NodeSource apt repository to the image.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
RUN node --version

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt \
    && python -c "import torch, qwen_tts, fastapi; print('torch', torch.__version__, '| cuda', torch.version.cuda)"

COPY app ./app
COPY tools ./tools
COPY scripts ./scripts
COPY tests ./tests
COPY examples ./examples
COPY README.md ./
COPY compose.yaml ./

RUN mkdir -p /models /voices /output \
    && chmod +x scripts/*.sh \
    && cp scripts/entrypoint.sh /usr/local/bin/qwen-tts-entrypoint \
    && cp scripts/doctor.sh /usr/local/bin/qwen-tts-doctor \
    && chmod +x /usr/local/bin/qwen-tts-entrypoint /usr/local/bin/qwen-tts-doctor

VOLUME ["/models", "/voices", "/output"]
EXPOSE 8000

# First start loads ~4 GB of weights; give the model time before failing.
HEALTHCHECK --interval=30s --timeout=5s --start-period=240s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

ENTRYPOINT ["/usr/local/bin/qwen-tts-entrypoint"]
CMD ["serve"]
