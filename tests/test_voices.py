import json

import pytest

from app.voices import VoiceError, VoiceRegistry, slugify
from conftest import write_voice


def test_slugify():
    assert slugify("The Major") == "the-major"
    assert slugify("  Petra!  ") == "petra"
    assert slugify("---") == "voice"


def test_registry_lists_voices(registry):
    assert len(registry) == 2
    names = [voice.name for voice in registry.list()]
    assert names == ["Henry", "Mildred"]
    assert registry.get("henry").ref_seconds > 3


def test_find_by_display_name(registry):
    assert registry.find("Mildred").id == "mildred"
    with pytest.raises(VoiceError):
        registry.find("Nobody")


def test_public_does_not_leak_paths(registry):
    item = registry.public_list()[0]
    assert "ref_audio" not in item
    assert item["has_transcript"] is True


def test_missing_transcript_is_an_error(tmp_path):
    root = tmp_path / "voices"
    root.mkdir()
    directory = write_voice(root, "broken")
    profile = json.loads((directory / "profile.json").read_text())
    profile["ref_text"] = ""
    (directory / "profile.json").write_text(json.dumps(profile))
    registry = VoiceRegistry(root)
    assert len(registry) == 0
    assert any("ref_text" in problem for problem in registry.errors)


def test_duplicate_ids_are_rejected(tmp_path):
    root = tmp_path / "voices"
    root.mkdir()
    write_voice(root, "henry")
    clone = root / "henry-copy"
    clone.mkdir()
    (clone / "ref.wav").write_bytes((root / "henry" / "ref.wav").read_bytes())
    (clone / "profile.json").write_text((root / "henry" / "profile.json").read_text())
    registry = VoiceRegistry(root)
    assert len(registry) == 1
    assert any("duplicate" in problem for problem in registry.errors)
