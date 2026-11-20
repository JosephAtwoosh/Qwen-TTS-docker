"""Dialogue script -> timed, mixed soundtrack.

The unit of work is a list of lines with a speaker, text and optional timing.
Two modes:

* ``sequential`` (default) lays lines end to end, with a gap between them. This
  is what a conversation is: turn, pause, turn.
* ``timeline`` places every line at an explicit ``start_s``; overlaps are
  allowed and reported, which is how cross-talk and interruptions are done.

Every line is also written as its own stem, so an editor can re-time a single
turn without regenerating the scene.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from . import audio
from .engine import EngineError, TTSEngine
from .voices import Voice, VoiceError, VoiceRegistry

MIN_SPEED_RATIO = 0.80
MAX_SPEED_RATIO = 1.25
LINE_PEAK_DBFS = -3.0
MIX_PEAK_DBFS = -1.0


@dataclass
class LineSpec:
    speaker: str
    text: str
    language: str | None = None
    start_s: float | None = None
    gap_before_s: float = 0.0
    gap_after_s: float | None = None
    target_duration_s: float | None = None
    gain_db: float = 0.0


@dataclass
class SoundtrackSpec:
    lines: list[LineSpec]
    mode: str = "sequential"
    gap_s: float = 0.15
    language: str = "English"
    duration_s: float | None = None
    normalize: bool = True
    seed: int | None = None
    title: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _chunks(items: list[int], max_items: int, texts: list[str], max_chars: int = 1200) -> list[list[int]]:
    chunks: list[list[int]] = []
    current: list[int] = []
    chars = 0
    for index in items:
        size = len(texts[index])
        if current and (len(current) >= max_items or chars + size > max_chars):
            chunks.append(current)
            current, chars = [], 0
        current.append(index)
        chars += size
    if current:
        chunks.append(current)
    return chunks


def _exact(samples: np.ndarray, count: int) -> np.ndarray:
    if len(samples) == count:
        return samples
    if len(samples) > count:
        return samples[:count]
    return np.pad(samples, (0, count - len(samples))).astype(np.float32)


def _fit_to(samples: np.ndarray, rate: int, target_s: float) -> tuple[np.ndarray, float, str | None]:
    """Return (audio, applied speed multiplier, warning)."""
    natural = len(samples) / float(rate)
    if natural <= 0 or target_s <= 0:
        return samples, 1.0, "target_duration_s must be positive; line was not fitted"
    ratio = target_s / natural  # >1 means the target is longer, so slow down
    warning = None
    if ratio < MIN_SPEED_RATIO or ratio > MAX_SPEED_RATIO:
        warning = (
            f"target {target_s:.2f}s needs a {ratio:.2f}x length change from "
            f"{natural:.2f}s; clamped to the {MIN_SPEED_RATIO:.2f}-{MAX_SPEED_RATIO:.2f} range"
        )
        ratio = min(MAX_SPEED_RATIO, max(MIN_SPEED_RATIO, ratio))
    wanted = natural * ratio
    # time_stretch's rate is a speed multiplier: 2.0 halves the length.
    speed = natural / wanted
    stretched = audio.time_stretch(samples, speed)
    stretched = _exact(stretched, int(round(wanted * rate)))
    return stretched, speed, warning


def _resolve(registry: VoiceRegistry, speaker: str) -> Voice:
    try:
        return registry.find(speaker)
    except VoiceError as exc:
        raise ValueError(str(exc)) from exc


def estimate_seconds(text: str, rate: float = 14.0) -> float:
    """Very rough duration used for validation before spending GPU time."""
    return max(0.4, len(text) / max(1.0, rate))


def build_soundtrack(
    engine: TTSEngine,
    registry: VoiceRegistry,
    spec: SoundtrackSpec,
    out_dir: str | Path,
    job_id: str,
    max_batch: int = 6,
) -> dict[str, Any]:
    if not spec.lines:
        raise ValueError("the script has no lines")
    if spec.mode not in {"sequential", "timeline"}:
        raise ValueError(f"unknown mode {spec.mode!r}")
    if spec.mode == "timeline" and any(line.start_s is None for line in spec.lines):
        raise ValueError("timeline mode needs start_s on every line")

    out_dir = Path(out_dir)
    stems_dir = out_dir / "stems"
    stems_dir.mkdir(parents=True, exist_ok=True)

    voices: list[Voice] = []
    languages: list[str] = []
    for line in spec.lines:
        text = line.text.strip()
        if not text:
            raise ValueError(f"empty text for speaker {line.speaker!r}")
        voice = _resolve(registry, line.speaker)
        voices.append(voice)
        languages.append(line.language or voice.language or spec.language)

    def synth(voice: Voice, texts: list[str], language: str, seed: int | None) -> list[np.ndarray]:
        """Batch, then fall back to one-at-a-time so one bad line cannot sink a scene."""
        try:
            return engine.synthesize_batch(voice, texts, language=language, seed=seed)
        except Exception as batch_error:
            if len(texts) == 1:
                raise
            out: list[np.ndarray] = []
            for text in texts:
                try:
                    out.extend(engine.synthesize_batch(voice, [text], language=language, seed=seed))
                except Exception as exc:
                    raise EngineError(
                        f"voice {voice.id!r} failed on {text[:60]!r}: {exc} "
                        f"(batch error was: {batch_error})"
                    ) from exc
            return out

    # ---- synthesise, grouped by voice so the clone prompt is reused ---------
    groups: dict[tuple[str, str], list[int]] = {}
    for index, voice in enumerate(voices):
        groups.setdefault((voice.id, languages[index]), []).append(index)

    generated: dict[int, np.ndarray] = {}
    rate = engine.sample_rate
    batch_warnings: list[str] = []
    all_texts = [line.text.strip() for line in spec.lines]
    for (voice_id, language), indices in groups.items():
        voice = registry.get(voice_id)
        for chunk_no, chunk in enumerate(_chunks(indices, max(1, max_batch), all_texts)):
            chunk_texts = [all_texts[i] for i in chunk]
            seed = None if spec.seed is None else int(spec.seed) + chunk_no
            try:
                clips = synth(voice, chunk_texts, language, seed)
            except Exception as exc:
                raise EngineError(f"voice {voice_id!r} failed: {exc}") from exc
            for index, clip in zip(chunk, clips):
                generated[index] = clip
                rate = engine.sample_rate
    if len(generated) != len(spec.lines):
        raise EngineError(f"only {len(generated)} of {len(spec.lines)} lines were generated")

    # ---- lay out -----------------------------------------------------------
    results: list[dict[str, Any]] = []
    placed: list[np.ndarray] = []
    cursor = 0.0
    for index, (line, voice) in enumerate(zip(spec.lines, voices)):
        samples = generated[index]
        natural = len(samples) / float(rate)
        speed = 1.0
        fit_warning = None
        if line.target_duration_s is not None:
            samples, speed, fit_warning = _fit_to(samples, rate, float(line.target_duration_s))
        samples = audio.fade_edges(samples, rate, ms=5.0)
        samples = audio.normalize_peak(samples, LINE_PEAK_DBFS)
        if line.gain_db:
            samples = audio.apply_gain_db(samples, float(line.gain_db))
        placed_seconds = len(samples) / float(rate)

        if spec.mode == "timeline":
            start = float(line.start_s or 0.0)
        else:
            start = cursor + max(0.0, float(line.gap_before_s))
        end = start + placed_seconds

        if spec.mode == "sequential":
            cursor = end + (spec.gap_s if line.gap_after_s is None else max(0.0, float(line.gap_after_s)))

        result = {
            "index": index,
            "speaker": line.speaker,
            "voice": voice.id,
            "text": line.text.strip(),
            "language": languages[index],
            "start_s": round(start, 4),
            "end_s": round(end, 4),
            "natural_seconds": round(natural, 4),
            "placed_seconds": round(placed_seconds, 4),
            "speed": round(speed, 4),
            "gain_db": round(float(line.gain_db), 2),
            "stem": "",
            "warnings": [fit_warning] if fit_warning else [],
        }
        results.append(result)
        placed.append(samples)
        if fit_warning:
            batch_warnings.append(f"line {index + 1} ({line.speaker}): {fit_warning}")

    # ---- overlaps and the project duration ---------------------------------
    warnings = list(batch_warnings)
    ordered = sorted(range(len(results)), key=lambda i: results[i]["start_s"])
    for previous, current in zip(ordered, ordered[1:]):
        if results[current]["start_s"] < results[previous]["end_s"] - 1e-3:
            warning = (
                f"lines {previous + 1} and {current + 1} overlap by "
                f"{results[previous]['end_s'] - results[current]['start_s']:.2f}s"
            )
            results[current]["warnings"].append(warning)
            warnings.append(warning)

    total = max((r["end_s"] for r in results), default=0.0)
    fits = True
    if spec.duration_s is not None:
        if total > float(spec.duration_s) + 1e-3:
            fits = False
            warnings.append(
                f"dialogue runs {total - float(spec.duration_s):.2f}s past the "
                f"{float(spec.duration_s):.2f}s target"
            )
        total = max(total, float(spec.duration_s))

    # ---- mix ---------------------------------------------------------------
    mix = np.zeros(int(round(total * rate)) + 1, dtype=np.float32)
    for result, samples in zip(results, placed):
        offset = int(round(result["start_s"] * rate))
        if offset >= len(mix):
            continue
        end = min(len(mix), offset + len(samples))
        mix[offset:end] += samples[: end - offset]
    mix = mix[: int(round(total * rate))]
    if spec.normalize and len(mix):
        mix = audio.normalize_peak(mix, MIX_PEAK_DBFS)
    audio.fade_edges(mix, rate, ms=8.0)

    # ---- write -------------------------------------------------------------
    for result, samples in zip(results, placed):
        stem_name = f"{result['index'] + 1:03d}_{result['voice']}.wav"
        audio.write_wav(stems_dir / stem_name, samples, rate)
        result["stem"] = f"stems/{stem_name}"

    mix_wav = out_dir / "mix.wav"
    audio.write_wav(mix_wav, mix, rate)
    mix_mp3 = out_dir / "mix.mp3"
    has_mp3 = audio.encode_mp3(mix_wav, mix_mp3)

    manifest: dict[str, Any] = {
        "id": job_id,
        "title": spec.title,
        "created_at": _now(),
        "mode": spec.mode,
        "language": spec.language,
        "sample_rate": rate,
        "total_seconds": round(total, 3),
        "duration_target_s": spec.duration_s,
        "fits_duration": fits,
        "normalized": spec.normalize,
        "seed": spec.seed,
        "metadata": spec.metadata,
        "engine": engine.describe(),
        "files": {
            "mix_wav": "mix.wav",
            "mix_mp3": "mix.mp3" if has_mp3 else None,
            "manifest": "manifest.json",
        },
        "warnings": warnings,
        "lines": results,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest
