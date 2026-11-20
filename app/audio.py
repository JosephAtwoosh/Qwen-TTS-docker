"""Audio primitives: WAV I/O, silence trimming, level control and stretching.

The image has `soundfile` (a qwen-tts dependency) and `librosa`; the unit tests
run on a bare Python + numpy, so both imports are optional and the wave-module
fallback is the path the tests exercise.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np

try:  # present in the image, absent from the bare test environment
    import soundfile as _sf
except Exception:  # pragma: no cover - exercised only without soundfile
    _sf = None

try:  # present in the image (qwen-tts dependency)
    import librosa as _librosa
except Exception:  # pragma: no cover
    _librosa = None


# --------------------------------------------------------------------- WAV I/O


def _read_with_wave(path: str | Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())

    if width == 1:
        data = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        data = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:
        raw = np.frombuffer(frames, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        value = raw[:, 0] | (raw[:, 1] << 8) | (raw[:, 2] << 16)
        value = np.where(value & 0x800000, value - 0x1000000, value)
        data = value.astype(np.float32) / 8388608.0
    elif width == 4:
        data = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"unsupported WAV sample width: {width} bytes")

    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(data, dtype=np.float32), int(rate)


def read_audio(path: str | Path) -> tuple[np.ndarray, int]:
    """Read a WAV (or anything libsndfile knows) as mono float32."""
    path = Path(path)
    if _sf is not None:
        data, rate = _sf.read(str(path), dtype="float32", always_2d=False)
        if getattr(data, "ndim", 1) > 1:
            data = data.mean(axis=1)
        return np.ascontiguousarray(data, dtype=np.float32), int(rate)
    return _read_with_wave(path)


def write_wav(path: str | Path, samples: np.ndarray, rate: int) -> Path:
    """Write mono float32 samples as 16-bit PCM."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = np.nan_to_num(np.asarray(samples, dtype=np.float32), nan=0.0, posinf=1.0, neginf=-1.0)
    samples = np.clip(samples, -1.0, 1.0)
    if _sf is not None:
        _sf.write(str(path), samples, int(rate), subtype="PCM_16")
        return path
    pcm = (samples * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(rate))
        handle.writeframes(pcm.tobytes())
    return path


def wav_bytes(samples: np.ndarray, rate: int) -> bytes:
    """Same encoding as write_wav, returned in memory for HTTP responses."""
    samples = np.nan_to_num(np.asarray(samples, dtype=np.float32), nan=0.0, posinf=1.0, neginf=-1.0)
    samples = np.clip(samples, -1.0, 1.0)
    buffer = io.BytesIO()
    if _sf is not None:
        _sf.write(buffer, samples, int(rate), format="WAV", subtype="PCM_16")
        return buffer.getvalue()
    pcm = (samples * 32767.0).astype("<i2")
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(rate))
        handle.writeframes(pcm.tobytes())
    return buffer.getvalue()


def duration_s(samples: np.ndarray, rate: int) -> float:
    return float(len(samples) / float(rate))


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


# ------------------------------------------------------------------- levels


def peak_dbfs(samples: np.ndarray) -> float:
    peak = float(np.max(np.abs(samples))) if len(samples) else 0.0
    return 20.0 * np.log10(peak) if peak > 0 else -120.0


def rms_dbfs(samples: np.ndarray) -> float:
    if not len(samples):
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
    return 20.0 * np.log10(rms) if rms > 0 else -120.0


def normalize_peak(samples: np.ndarray, target_dbfs: float = -1.0) -> np.ndarray:
    peak = float(np.max(np.abs(samples))) if len(samples) else 0.0
    if peak <= 0:
        return samples
    target = 10.0 ** (target_dbfs / 20.0)
    return (samples * (target / peak)).astype(np.float32)


def apply_gain_db(samples: np.ndarray, gain_db: float) -> np.ndarray:
    if gain_db == 0:
        return samples
    return (samples * (10.0 ** (gain_db / 20.0))).astype(np.float32)


def fade_edges(samples: np.ndarray, rate: int, ms: float = 6.0) -> np.ndarray:
    n = int(rate * ms / 1000.0)
    if n <= 1 or len(samples) < 2 * n:
        return samples
    ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
    out = samples.copy()
    out[:n] *= ramp
    out[-n:] *= ramp[::-1]
    return out


# --------------------------------------------------------------- trimming


def trim_silence(
    samples: np.ndarray,
    rate: int,
    threshold_dbfs: float = -45.0,
    pad_s: float = 0.05,
) -> np.ndarray:
    """Trim leading/trailing near-silence while keeping a short pad."""
    if len(samples) == 0:
        return samples
    frame = max(1, int(rate * 0.02))
    frames = len(samples) // frame
    if frames == 0:
        return samples
    trimmed = samples[: frames * frame].reshape(frames, frame)
    rms = np.sqrt(np.mean(np.square(trimmed, dtype=np.float64), axis=1))
    threshold = 10.0 ** (threshold_dbfs / 20.0)
    audible = np.flatnonzero(rms >= threshold)
    if len(audible) == 0:
        return samples
    pad = int(rate * pad_s)
    start = max(0, audible[0] * frame - pad)
    stop = min(len(samples), (audible[-1] + 1) * frame + pad)
    return np.ascontiguousarray(samples[start:stop], dtype=np.float32)


# --------------------------------------------------------------- stretching


def resample_linear(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate or len(samples) == 0:
        return samples
    count = max(1, int(round(len(samples) * target_rate / source_rate)))
    old = np.linspace(0.0, 1.0, len(samples), endpoint=False)
    new = np.linspace(0.0, 1.0, count, endpoint=False)
    return np.interp(new, old, samples).astype(np.float32)


def time_stretch(samples: np.ndarray, rate: float) -> np.ndarray:
    """Change speed without changing pitch when librosa is available.

    `rate` follows librosa's convention: 2.0 is twice as fast (half the length).
    The fallback is a linear resample, which does shift pitch; the soundtrack
    manifest records which method was used.
    """
    if abs(rate - 1.0) < 0.005 or len(samples) < 32:
        return samples
    if _librosa is not None:
        try:
            return np.ascontiguousarray(
                _librosa.effects.time_stretch(y=samples.astype(np.float32), rate=float(rate)),
                dtype=np.float32,
            )
        except Exception:  # pragma: no cover - fall through to resample
            pass
    count = max(1, int(round(len(samples) / rate)))
    old = np.linspace(0.0, 1.0, len(samples), endpoint=False)
    new = np.linspace(0.0, 1.0, count, endpoint=False)
    return np.interp(new, old, samples).astype(np.float32)


# --------------------------------------------------------------- encoding


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def encode_mp3(wav_path: str | Path, mp3_path: str | Path, bitrate: str = "192k") -> bool:
    """Best-effort MP3 encode; the WAV is always the canonical output."""
    if not have_ffmpeg():
        return False
    wav_path, mp3_path = Path(wav_path), Path(mp3_path)
    mp3_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(wav_path),
                "-codec:a", "libmp3lame", "-b:a", bitrate,
                str(mp3_path),
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except Exception:
        return False
    return proc.returncode == 0 and mp3_path.is_file() and mp3_path.stat().st_size > 0


def probe_duration(path: str | Path) -> float | None:
    """Duration from the WAV header, or from ffprobe when available."""
    try:
        samples, rate = read_audio(path)
        return duration_s(samples, rate)
    except Exception:
        pass
    if not have_ffmpeg():
        return None
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return float(proc.stdout.strip())
    except Exception:
        return None
