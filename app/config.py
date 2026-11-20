"""Environment-driven configuration.

Everything has a working default for the container; the host wrapper only has
to set the paths and the API key.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(_env(name, str(default))))
    except ValueError:
        return default


def _gen_kwargs() -> dict[str, Any]:
    raw = os.environ.get("GEN_KWARGS", "").strip()
    defaults: dict[str, Any] = {
        "do_sample": True,
        "top_k": 50,
        "top_p": 1.0,
        "temperature": 0.9,
        "repetition_penalty": 1.05,
        "subtalker_dosample": True,
        "subtalker_top_k": 50,
        "subtalker_top_p": 1.0,
        "subtalker_temperature": 0.9,
        "max_new_tokens": 2048,
    }
    if raw:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("GEN_KWARGS must be a JSON object")
        defaults.update(parsed)
    return defaults


@dataclass(frozen=True)
class Settings:
    model_id: str
    device: str
    dtype: str
    attn_implementation: str
    voices_dir: Path
    output_dir: Path
    api_key: str
    engine: str
    default_language: str
    max_batch: int
    max_lines: int
    job_workers: int
    preload_model: bool
    cors_origins: tuple[str, ...]
    gen_kwargs: dict[str, Any]
    host: str
    port: int

    @property
    def auth_required(self) -> bool:
        return bool(self.api_key)


def _cors() -> tuple[str, ...]:
    raw = _env("CORS_ORIGINS", "*")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        model_id=_env("MODEL_ID", "/models/Qwen3-TTS-12Hz-1.7B-Base"),
        device=_env("DEVICE", "cuda:0"),
        dtype=_env("DTYPE", "bfloat16"),
        attn_implementation=_env("ATTN_IMPLEMENTATION", "sdpa"),
        voices_dir=Path(_env("VOICES_DIR", "/voices")),
        output_dir=Path(_env("OUTPUT_DIR", "/output")),
        api_key=_env("TTS_API_KEY", ""),
        engine=_env("ENGINE", "qwen"),
        default_language=_env("DEFAULT_LANGUAGE", "English"),
        max_batch=_int("MAX_BATCH", 6),
        max_lines=_int("MAX_LINES", 200),
        job_workers=_int("JOB_WORKERS", 1),
        preload_model=_flag("PRELOAD_MODEL", True),
        cors_origins=_cors(),
        gen_kwargs=_gen_kwargs(),
        host=_env("HOST", "0.0.0.0"),
        port=_int("PORT", 8000, 1),
    )


def reset_settings() -> None:
    """Test helper: forget the cached environment."""
    get_settings.cache_clear()
