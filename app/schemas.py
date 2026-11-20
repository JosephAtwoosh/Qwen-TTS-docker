"""Request/response models for the HTTP API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class LineIn(BaseModel):
    speaker: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=800)
    language: str | None = Field(default=None, max_length=40)
    start_s: float | None = Field(default=None, ge=0.0, le=36000.0)
    gap_before_s: float = Field(default=0.0, ge=0.0, le=30.0)
    gap_after_s: float | None = Field(default=None, ge=0.0, le=30.0)
    target_duration_s: float | None = Field(default=None, gt=0.0, le=300.0)
    gain_db: float = Field(default=0.0, ge=-24.0, le=24.0)

    @field_validator("speaker", "text")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class SoundtrackIn(BaseModel):
    title: str = Field(default="", max_length=160)
    mode: Literal["sequential", "timeline"] = "sequential"
    lines: list[LineIn] = Field(min_length=1, max_length=200)
    gap_s: float = Field(default=0.15, ge=0.0, le=30.0)
    language: str = Field(default="English", max_length=40)
    duration_s: float | None = Field(default=None, gt=0.0, le=36000.0)
    normalize: bool = True
    seed: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TTSIn(BaseModel):
    voice: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=800)
    language: str | None = Field(default=None, max_length=40)
    seed: int | None = None
    normalize: bool = True

    @field_validator("text")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("text must not be blank")
        return value


class BatchLineIn(BaseModel):
    text: str = Field(min_length=1, max_length=800)
    language: str | None = Field(default=None, max_length=40)


class BatchTTSIn(BaseModel):
    voice: str = Field(min_length=1, max_length=80)
    lines: list[BatchLineIn] = Field(min_length=1, max_length=64)
    language: str | None = Field(default=None, max_length=40)
    seed: int | None = None
    normalize: bool = True
