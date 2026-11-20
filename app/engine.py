"""Model wrappers.

`QwenEngine` is the real thing; `MockEngine` produces deterministic tones so the
API, the timeline code and the tests all work on a machine without a GPU. The
service never imports torch at module import time: loading is lazy, which keeps
`/health` cheap and lets the test suite run on bare Python.
"""

from __future__ import annotations

import hashlib
import threading
from typing import Sequence

import numpy as np

from . import audio
from .config import Settings
from .voices import Voice


class EngineError(RuntimeError):
    pass


class TTSEngine:
    name = "base"
    sample_rate = 24000

    def load(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def synthesize_batch(
        self,
        voice: Voice,
        texts: Sequence[str],
        language: str | None = None,
        seed: int | None = None,
    ) -> list[np.ndarray]:  # pragma: no cover - interface
        raise NotImplementedError

    @property
    def loaded(self) -> bool:
        return False

    def describe(self) -> dict:
        return {"engine": self.name, "loaded": self.loaded, "sample_rate": self.sample_rate}


class QwenEngine(TTSEngine):
    name = "qwen"

    def __init__(self, settings: Settings):
        self.settings = settings
        self._model = None
        self._prompts: dict[str, list] = {}
        self._lock = threading.RLock()
        self._attention = settings.attn_implementation
        self._load_error: str | None = None

    # ------------------------------------------------------------------ load
    def load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            try:
                import torch
                from qwen_tts import Qwen3TTSModel
            except Exception as exc:  # pragma: no cover - image-only path
                raise EngineError(f"cannot import torch/qwen_tts: {exc}") from exc

            dtype = getattr(torch, self.settings.dtype, None)
            if dtype is None:
                raise EngineError(f"unknown DTYPE {self.settings.dtype!r} (try bfloat16 or float16)")

            last_error: Exception | None = None
            for attention in self._attention_candidates():
                kwargs = {"device_map": self.settings.device, "dtype": dtype}
                if attention:
                    kwargs["attn_implementation"] = attention
                if self.settings.model_id.startswith("/"):
                    kwargs["local_files_only"] = True
                try:
                    model = Qwen3TTSModel.from_pretrained(self.settings.model_id, **kwargs)
                except Exception as exc:  # pragma: no cover - needs a GPU
                    last_error = exc
                    if attention == "flash_attention_2":
                        continue
                    raise EngineError(f"model load failed: {exc}") from exc
                self._model = model
                self._attention = attention or "default"
                return
            raise EngineError(f"model load failed: {last_error}")

    def _attention_candidates(self) -> list[str | None]:
        wanted = (self.settings.attn_implementation or "").strip()
        if not wanted:
            return [None]
        # FlashAttention is a build-time extra. If it is not installed, fall
        # back to PyTorch SDPA instead of failing the container at startup.
        if wanted == "flash_attention_2":
            return ["flash_attention_2", "sdpa"]
        return [wanted]

    @property
    def loaded(self) -> bool:
        return self._model is not None

    # --------------------------------------------------------------- prompts
    def _prompt(self, voice: Voice) -> list:
        cached = self._prompts.get(voice.id)
        if cached is not None:
            return cached
        assert self._model is not None
        items = self._model.create_voice_clone_prompt(
            ref_audio=str(voice.ref_audio),
            ref_text=voice.ref_text if voice.uses_transcript else None,
            x_vector_only_mode=voice.x_vector_only,
        )
        self._prompts[voice.id] = items
        return items

    def preload_voice(self, voice: Voice) -> None:
        with self._lock:
            self.load()
            self._prompt(voice)

    # ------------------------------------------------------------- synthesis
    def synthesize_batch(
        self,
        voice: Voice,
        texts: Sequence[str],
        language: str | None = None,
        seed: int | None = None,
    ) -> list[np.ndarray]:
        texts = [str(t).strip() for t in texts]
        if not texts or any(not t for t in texts):
            raise EngineError("synthesize_batch needs at least one non-empty text")
        lang = language or voice.language or self.settings.default_language

        with self._lock:
            self.load()
            import torch

            prompt = self._prompt(voice)
            if seed is not None:
                torch.manual_seed(int(seed))
            try:
                wavs, rate = self._model.generate_voice_clone(  # pragma: no cover - GPU
                    text=list(texts),
                    language=[lang] * len(texts),
                    voice_clone_prompt=prompt,
                    **self.settings.gen_kwargs,
                )
            except torch.cuda.OutOfMemoryError as exc:  # pragma: no cover - GPU
                torch.cuda.empty_cache()
                raise EngineError(
                    "CUDA out of memory while generating a batch; lower MAX_BATCH "
                    "(or split the scene) and retry"
                ) from exc
            except Exception as exc:  # pragma: no cover - GPU
                raise EngineError(f"generation failed: {exc}") from exc

            if len(wavs) != len(texts):
                raise EngineError(f"model returned {len(wavs)} clips for {len(texts)} texts")

            self.sample_rate = int(rate)
            cleaned: list[np.ndarray] = []
            for wav in wavs:
                samples = np.asarray(wav, dtype=np.float32).reshape(-1)
                samples = audio.trim_silence(samples, self.sample_rate, threshold_dbfs=-50.0, pad_s=0.02)
                samples = audio.fade_edges(samples, self.sample_rate, ms=6.0)
                if len(samples) == 0:
                    raise EngineError("model produced an empty clip")
                cleaned.append(samples)
            return cleaned

    def describe(self) -> dict:
        info = super().describe()
        info.update({"model": self.settings.model_id, "device": self.settings.device, "attention": self._attention})
        if self._load_error:
            info["load_error"] = self._load_error
        return info


class MockEngine(TTSEngine):
    """Deterministic audible output for tests and for `ENGINE=mock` dry runs."""

    name = "mock"

    def load(self) -> None:
        return None

    @property
    def loaded(self) -> bool:
        return True

    def synthesize_batch(
        self,
        voice: Voice,
        texts: Sequence[str],
        language: str | None = None,
        seed: int | None = None,
    ) -> list[np.ndarray]:
        rate = self.sample_rate
        digest = hashlib.sha1(voice.id.encode("utf-8")).digest()
        base_freq = 110.0 + (digest[0] / 255.0) * 120.0
        out: list[np.ndarray] = []
        for index, text in enumerate(texts):
            seconds = min(12.0, max(0.5, 0.32 + 0.055 * len(text)))
            count = int(seconds * rate)
            t = np.arange(count, dtype=np.float32) / rate
            wobble = 1.0 + 0.02 * np.sin(2 * np.pi * 0.7 * t + index)
            signal = (
                0.16 * np.sin(2 * np.pi * base_freq * t * wobble)
                + 0.05 * np.sin(2 * np.pi * base_freq * 2.01 * t)
                + 0.02 * np.sin(2 * np.pi * base_freq * 3.03 * t)
            ).astype(np.float32)
            envelope = np.minimum(1.0, t / 0.02) * np.minimum(1.0, (seconds - t) / 0.05)
            out.append((signal * envelope).astype(np.float32))
        return out


def build_engine(settings: Settings) -> TTSEngine:
    if settings.engine == "mock":
        return MockEngine()
    if settings.engine in {"qwen", "qwen3", "qwen-tts"}:
        return QwenEngine(settings)
    raise EngineError(f"unknown ENGINE {settings.engine!r} (expected qwen or mock)")
