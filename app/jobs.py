"""A one-at-a-time soundtrack job queue.

Soundtracks are minutes of work, so the API must not hold the request open by
default. Jobs are directories under `/output`; `status.json` is rewritten at
each transition, which makes a crashed container's state inspectable.
"""

from __future__ import annotations

import json
import logging
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .engine import TTSEngine
from .schemas import SoundtrackIn
from .soundtrack import LineSpec, SoundtrackSpec, build_soundtrack
from .voices import VoiceRegistry

log = logging.getLogger("qwen_tts.jobs")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    directory: Path
    request: dict[str, Any]
    status: str = "queued"
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    manifest: dict[str, Any] | None = None
    error: str | None = None
    done: threading.Event = field(default_factory=threading.Event)


class JobManager:
    def __init__(self, settings: Settings, engine: TTSEngine, registry: VoiceRegistry):
        self.settings = settings
        self.engine = engine
        self.registry = registry
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, settings.job_workers), thread_name_prefix="soundtrack"
        )

    def submit(self, payload: SoundtrackIn) -> Job:
        job_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        directory = self.settings.output_dir / job_id
        directory.mkdir(parents=True, exist_ok=True)
        request = payload.model_dump()
        (directory / "request.json").write_text(
            json.dumps(request, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        job = Job(id=job_id, directory=directory, request=request)
        with self._lock:
            self._jobs[job_id] = job
        self._write(job)
        self._executor.submit(self._run, job, payload)
        return job

    def _write(self, job: Job) -> None:
        job.updated_at = _now()
        state = {
            "id": job.id,
            "status": job.status,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "error": job.error,
            "title": job.request.get("title", ""),
            "lines": len(job.request.get("lines", [])),
        }
        try:
            (job.directory / "status.json").write_text(
                json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except OSError:
            log.warning("could not write status.json for %s", job.id, exc_info=True)

    def _run(self, job: Job, payload: SoundtrackIn) -> None:
        job.status = "running"
        self._write(job)
        try:
            spec = SoundtrackSpec(
                lines=[
                    LineSpec(
                        speaker=line.speaker,
                        text=line.text,
                        language=line.language,
                        start_s=line.start_s,
                        gap_before_s=line.gap_before_s,
                        gap_after_s=line.gap_after_s,
                        target_duration_s=line.target_duration_s,
                        gain_db=line.gain_db,
                    )
                    for line in payload.lines
                ],
                mode=payload.mode,
                gap_s=payload.gap_s,
                language=payload.language,
                duration_s=payload.duration_s,
                normalize=payload.normalize,
                seed=payload.seed,
                title=payload.title,
                metadata=payload.metadata,
            )
            manifest = build_soundtrack(
                engine=self.engine,
                registry=self.registry,
                spec=spec,
                out_dir=job.directory,
                job_id=job.id,
                max_batch=self.settings.max_batch,
            )
            job.manifest = manifest
            job.status = "done"
        except Exception as exc:
            job.error = str(exc)
            job.status = "error"
            log.error("soundtrack %s failed: %s", job.id, exc)
            log.debug("%s", traceback.format_exc())
        finally:
            self._write(job)
            job.done.set()

    def get(self, job_id: str) -> Job:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def recent(self, limit: int = 20) -> list[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        jobs.sort(key=lambda job: job.created_at, reverse=True)
        return jobs[: max(0, limit)]

    def wait(self, job: Job, timeout: float) -> bool:
        return job.done.wait(timeout=max(0.0, timeout))

    def public(self, job: Job, base_url: str = "") -> dict[str, Any]:
        base = base_url.rstrip("/")
        data: dict[str, Any] = {
            "id": job.id,
            "status": job.status,
            "title": job.request.get("title", ""),
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "error": job.error,
        }
        if job.status == "done" and job.manifest is not None:
            manifest = dict(job.manifest)
            manifest["urls"] = {
                "mix_wav": f"{base}/v1/soundtracks/{job.id}/files/mix.wav",
                "mix_mp3": f"{base}/v1/soundtracks/{job.id}/files/mix.mp3"
                if (job.directory / "mix.mp3").is_file()
                else None,
                "manifest": f"{base}/v1/soundtracks/{job.id}/files/manifest.json",
            }
            data["manifest"] = manifest
        return data

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
