import numpy as np
import pytest

from app import audio


def test_wav_roundtrip(tmp_path):
    rate = 24000
    t = np.arange(rate, dtype=np.float32) / rate
    samples = (0.4 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
    path = tmp_path / "tone.wav"
    audio.write_wav(path, samples, rate)
    back, got_rate = audio.read_audio(path)
    assert got_rate == rate
    assert len(back) == len(samples)
    assert np.max(np.abs(back - samples)) < 1e-3


def test_wav_bytes_is_riff():
    data = audio.wav_bytes(np.zeros(2400, dtype=np.float32), 24000)
    assert data[:4] == b"RIFF"
    assert data[8:12] == b"WAVE"


def test_trim_silence_removes_padding():
    rate = 24000
    tone = 0.3 * np.sin(2 * np.pi * 220 * np.arange(rate) / rate).astype(np.float32)
    padded = np.concatenate([np.zeros(rate // 2, np.float32), tone, np.zeros(rate // 2, np.float32)])
    trimmed = audio.trim_silence(padded, rate, threshold_dbfs=-45.0, pad_s=0.05)
    assert 0.9 <= len(trimmed) / rate <= 1.2


def test_normalize_peak():
    samples = np.array([0.0, 0.1, -0.2, 0.05], dtype=np.float32)
    out = audio.normalize_peak(samples, -6.0)
    assert audio.peak_dbfs(out) == pytest.approx(-6.0, abs=0.05)


def test_time_stretch_changes_length():
    rate = 24000
    samples = np.sin(2 * np.pi * 220 * np.arange(rate) / rate).astype(np.float32)
    faster = audio.time_stretch(samples, 2.0)
    assert 0.45 <= len(faster) / rate <= 0.55


def test_gain_is_reversible():
    samples = np.array([0.1, -0.1], dtype=np.float32)
    louder = audio.apply_gain_db(samples, 6.0)
    quieter = audio.apply_gain_db(louder, -6.0)
    assert np.allclose(samples, quieter, atol=1e-6)
