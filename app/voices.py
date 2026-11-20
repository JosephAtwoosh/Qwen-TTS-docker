"""The voice registry.

A voice is a directory under `/voices`:

    voices/henry/
      profile.json     metadata + reference transcript
      ref.wav          the reference clip (mono, 24 kHz recommended)

`profile.json` is written by tools/import_voices.js, but hand-written profiles
work too. The registry is intentionally a directory scan: adding a voice is a
file copy, not a database migration.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import audio

MIN_REFERENCE_SECONDS = 3.0
MAX_REFERENCE_SECONDS = 60.0


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "voice"


@dataclass
class Voice:
    id: str
    name: str
    ref_audio: Path
    ref_text: str
    language: str = "English"
    description: str = ""
    gender: str = ""
    source: str = "local"
    source_voice_id: str = ""
    x_vector_only: bool = False
    created_at: str = ""
    tags: list[str] = field(default_factory=list)
    ref_seconds: float = 0.0
    sha256: str = ""

    @property
    def uses_transcript(self) -> bool:
        return not self.x_vector_only

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "language": self.language,
            "description": self.description,
            "gender": self.gender,
            "source": self.source,
            "source_voice_id": self.source_voice_id,
            "x_vector_only": self.x_vector_only,
            "tags": list(self.tags),
            "ref_seconds": round(self.ref_seconds, 3),
            "has_transcript": bool(self.ref_text.strip()),
        }


class VoiceError(RuntimeError):
    pass


def load_voice(directory: Path) -> Voice:
    profile_path = directory / "profile.json"
    if not profile_path.is_file():
        raise VoiceError(f"{directory.name}: no profile.json")
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise VoiceError(f"{directory.name}: unreadable profile.json ({exc})") from exc
    if not isinstance(profile, dict):
        raise VoiceError(f"{directory.name}: profile.json is not an object")

    voice_id = str(profile.get("id") or directory.name).strip()
    if slugify(voice_id) != voice_id:
        raise VoiceError(f"{directory.name}: id must be a lowercase slug, got {voice_id!r}")

    ref_name = str(profile.get("ref_audio") or "ref.wav")
    ref_path = (directory / ref_name).resolve()
    try:
        ref_path.relative_to(directory.resolve())
    except ValueError as exc:
        raise VoiceError(f"{voice_id}: ref_audio escapes the voice directory") from exc
    if not ref_path.is_file():
        raise VoiceError(f"{voice_id}: missing reference audio {ref_name}")

    x_vector_only = bool(profile.get("x_vector_only", False))
    ref_text = str(profile.get("ref_text") or "").strip()
    if not x_vector_only and not ref_text:
        raise VoiceError(f"{voice_id}: ref_text is required for ICL cloning (set x_vector_only to disable)")

    seconds = float(profile.get("ref_seconds") or 0.0)
    if seconds <= 0:
        seconds = audio.probe_duration(ref_path) or 0.0

    return Voice(
        id=voice_id,
        name=str(profile.get("name") or voice_id),
        ref_audio=ref_path,
        ref_text=ref_text,
        language=str(profile.get("language") or "English"),
        description=str(profile.get("description") or ""),
        gender=str(profile.get("gender") or ""),
        source=str(profile.get("source") or "local"),
        source_voice_id=str(profile.get("source_voice_id") or ""),
        x_vector_only=x_vector_only,
        created_at=str(profile.get("created_at") or ""),
        tags=[str(t) for t in profile.get("tags") or []],
        ref_seconds=seconds,
        sha256=str(profile.get("sha256") or audio.sha256_file(ref_path)),
    )


class VoiceRegistry:
    """Directory-backed voice lookup with explicit validation errors."""

    def __init__(self, root: Path, default_language: str = "English"):
        self.root = Path(root)
        self.default_language = default_language
        self._voices: dict[str, Voice] = {}
        self._errors: list[str] = []
        self.reload()

    def reload(self) -> None:
        voices: dict[str, Voice] = {}
        errors: list[str] = []
        if not self.root.is_dir():
            self._voices, self._errors = {}, [f"voices directory does not exist: {self.root}"]
            return
        for directory in sorted(p for p in self.root.iterdir() if p.is_dir()):
            if not (directory / "profile.json").is_file():
                continue
            try:
                voice = load_voice(directory)
            except VoiceError as exc:
                errors.append(str(exc))
                continue
            if voice.id in voices:
                errors.append(f"{voice.id}: duplicate id in {directory.name}")
                continue
            if voice.ref_seconds and voice.ref_seconds < MIN_REFERENCE_SECONDS:
                errors.append(
                    f"{voice.id}: reference is only {voice.ref_seconds:.1f}s "
                    f"(under {MIN_REFERENCE_SECONDS:.0f}s; cloning will be weak)"
                )
            voices[voice.id] = voice
        self._voices = voices
        self._errors = errors

    @property
    def errors(self) -> list[str]:
        return list(self._errors)

    def __len__(self) -> int:
        return len(self._voices)

    def __contains__(self, voice_id: object) -> bool:
        return voice_id in self._voices

    def get(self, voice_id: str) -> Voice:
        try:
            return self._voices[voice_id]
        except KeyError as exc:
            known = ", ".join(sorted(self._voices)) or "(none)"
            raise VoiceError(f"unknown voice {voice_id!r}; known: {known}") from exc

    def find(self, name: str) -> Voice:
        """Look up by id, then by case-insensitive display name."""
        if name in self._voices:
            return self._voices[name]
        wanted = name.strip().lower()
        for voice in self._voices.values():
            if voice.name.strip().lower() == wanted:
                return voice
        raise VoiceError(f"unknown voice {name!r}")

    def list(self) -> list[Voice]:
        return sorted(self._voices.values(), key=lambda v: (v.name.lower(), v.id))

    def public_list(self) -> list[dict[str, Any]]:
        return [voice.public() for voice in self.list()]
