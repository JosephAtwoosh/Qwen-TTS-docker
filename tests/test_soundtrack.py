import json

import numpy as np
import pytest

from app import audio
from app.engine import MockEngine
from app.soundtrack import LineSpec, SoundtrackSpec, build_soundtrack


def build(tmp_path, registry, spec, name="job"):
    return build_soundtrack(MockEngine(), registry, spec, tmp_path / name, name)


def test_sequential_conversation(tmp_path, registry):
    spec = SoundtrackSpec(
        title="Scene 1",
        lines=[LineSpec("henry", "Hello there."), LineSpec("mildred", "Hi.")],
        gap_s=0.2,
    )
    manifest = build(tmp_path, registry, spec)
    first, second = manifest["lines"]
    assert second["start_s"] == pytest.approx(first["end_s"] + 0.2, abs=1e-3)
    assert manifest["total_seconds"] >= second["end_s"]
    assert (tmp_path / "job" / "mix.wav").is_file()
    assert (tmp_path / "job" / "manifest.json").is_file()
    for line in manifest["lines"]:
        assert (tmp_path / "job" / line["stem"]).is_file()


def test_timeline_overlap_is_reported(tmp_path, registry):
    spec = SoundtrackSpec(
        mode="timeline",
        lines=[
            LineSpec("henry", "This is a deliberately long line that will overlap.", start_s=0.0),
            LineSpec("mildred", "Hi.", start_s=0.2),
        ],
    )
    manifest = build(tmp_path, registry, spec)
    assert manifest["mode"] == "timeline"
    assert any("overlap" in warning for warning in manifest["warnings"])
    assert manifest["lines"][1]["start_s"] == pytest.approx(0.2)


def test_target_duration_is_respected(tmp_path, registry):
    spec = SoundtrackSpec(lines=[LineSpec("henry", "Hi.", target_duration_s=0.55)])
    manifest = build(tmp_path, registry, spec)
    assert manifest["lines"][0]["placed_seconds"] == pytest.approx(0.55, abs=0.01)


def test_impossible_target_is_clamped_and_reported(tmp_path, registry):
    spec = SoundtrackSpec(lines=[LineSpec("henry", "Hi.", target_duration_s=5.0)])
    manifest = build(tmp_path, registry, spec)
    line = manifest["lines"][0]
    assert line["speed"] == pytest.approx(0.8, abs=0.01)
    assert any("clamped" in warning for warning in manifest["warnings"])


def test_unknown_speaker_fails(tmp_path, registry):
    spec = SoundtrackSpec(lines=[LineSpec("nobody", "Hello.")])
    with pytest.raises(ValueError, match="unknown voice"):
        build(tmp_path, registry, spec)


def test_blank_text_is_rejected(tmp_path, registry):
    spec = SoundtrackSpec(lines=[LineSpec("henry", "   ")])
    with pytest.raises(ValueError, match="empty text"):
        build(tmp_path, registry, spec)


def test_timeline_needs_start_times(tmp_path, registry):
    spec = SoundtrackSpec(mode="timeline", lines=[LineSpec("henry", "Hello.")])
    with pytest.raises(ValueError, match="start_s"):
        build(tmp_path, registry, spec)


def test_overrun_reports_not_fits(tmp_path, registry):
    spec = SoundtrackSpec(
        lines=[LineSpec("henry", "Hello there, this is a line.")],
        duration_s=0.2,
    )
    manifest = build(tmp_path, registry, spec)
    assert manifest["fits_duration"] is False
    assert any("past the" in warning for warning in manifest["warnings"])


def test_mix_is_normalized_and_audible(tmp_path, registry):
    spec = SoundtrackSpec(lines=[LineSpec("henry", "Hello there.")])
    manifest = build(tmp_path, registry, spec)
    mix, rate = audio.read_audio(tmp_path / "job" / "mix.wav")
    assert rate == 24000
    assert audio.peak_dbfs(mix) == pytest.approx(-1.0, abs=0.2)
    assert np.max(np.abs(mix)) < 1.0


def test_manifest_is_json_serialisable(tmp_path, registry):
    spec = SoundtrackSpec(lines=[LineSpec("henry", "Hello.")])
    manifest = build(tmp_path, registry, spec)
    json.dumps(manifest)
