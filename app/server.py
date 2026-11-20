"""FastAPI service.

Endpoints
---------
GET  /health                              liveness + model/voice state (no auth)
GET  /v1/voices                           the imported voice registry
GET  /v1/voices/{id}                      one voice
POST /v1/tts                              one line -> WAV
POST /v1/tts/batch                        many lines, one voice -> ZIP of WAVs
POST /v1/soundtracks                      conversation script -> job (or ?wait=1)
GET  /v1/soundtracks                      recent jobs
GET  /v1/soundtracks/{id}                 job status + manifest
GET  /v1/soundtracks/{id}/files/{name}    mix.wav / mix.mp3 / manifest.json
GET  /v1/soundtracks/{id}/stems/{name}    one line's WAV
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import secrets
import socket
import threading
import time
import zipfile
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from . import __version__, audio
from .config import Settings, get_settings
from .engine import EngineError, TTSEngine, build_engine
from .jobs import Job, JobManager
from .schemas import BatchTTSIn, SoundtrackIn, TTSIn
from .voices import Voice, VoiceError, VoiceRegistry

log = logging.getLogger("qwen_tts.server")

_STEM_RE = re.compile(r"^[0-9]{3}_[a-z0-9-]+\.wav$")
_FILES = {
    "mix.wav": "audio/wav",
    "mix.mp3": "audio/mpeg",
    "manifest.json": "application/json",
}


def _preload(engine: TTSEngine, app: FastAPI) -> None:
    try:
        engine.load()
        log.info("model loaded: %s", engine.describe())
    except Exception as exc:  # keep serving; /health explains the failure
        app.state.load_error = str(exc)
        log.error("model preload failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    registry = VoiceRegistry(settings.voices_dir, settings.default_language)
    engine = build_engine(settings)
    app.state.settings = settings
    app.state.registry = registry
    app.state.engine = engine
    app.state.jobs = JobManager(settings, engine, registry)
    app.state.started_at = time.time()
    app.state.load_error = None

    log.info(
        "starting qwen-tts %s: engine=%s voices=%d dir=%s",
        __version__, settings.engine, len(registry), settings.voices_dir,
    )
    for problem in registry.errors:
        log.warning("voice registry: %s", problem)
    if settings.preload_model:
        threading.Thread(target=_preload, args=(engine, app), daemon=True, name="model-preload").start()
    try:
        yield
    finally:
        app.state.jobs.shutdown()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Qwen-TTS Docker",
        version=__version__,
        description="Voice-cloned conversation soundtracks from Qwen3-TTS-12Hz-1.7B-Base.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins) or ["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def require_key(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
        x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    ) -> None:
        expected = request.app.state.settings.api_key
        if not expected:
            return
        supplied = (x_api_key or "").strip()
        if not supplied and authorization:
            scheme, _, value = authorization.partition(" ")
            if scheme.lower() == "bearer":
                supplied = value.strip()
        if not supplied or not secrets.compare_digest(supplied, expected):
            raise HTTPException(
                status_code=401,
                detail="missing or invalid API key",
                headers={"WWW-Authenticate": "Bearer"},
            )

    router = APIRouter(dependencies=[Depends(require_key)])

    def base(request: Request) -> str:
        return str(request.base_url).rstrip("/")

    def voice_or_404(registry: VoiceRegistry, voice_id: str) -> Voice:
        try:
            return registry.get(voice_id)
        except VoiceError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def run_synth(engine: TTSEngine, voice: Voice, texts: list[str], language: str | None, seed: int | None):
        try:
            return engine.synthesize_batch(voice, texts, language=language, seed=seed)
        except EngineError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ------------------------------------------------------------------ health
    @app.get("/health", tags=["meta"])
    def health(request: Request) -> dict[str, Any]:
        state = request.app.state
        engine: TTSEngine = state.engine
        registry: VoiceRegistry = state.registry
        loaded = engine.loaded
        status = "ok"
        if state.load_error or (state.settings.engine != "mock" and not loaded):
            status = "degraded"
        return {
            "status": status,
            "version": __version__,
            "engine": engine.describe(),
            "auth_required": state.settings.auth_required,
            "voices": {"count": len(registry), "errors": registry.errors[:20]},
            "load_error": state.load_error,
            "hostname": socket.gethostname(),
            "uptime_seconds": round(time.time() - state.started_at, 1),
        }

    # ------------------------------------------------------------------ voices
    @router.get("/v1/voices", tags=["voices"])
    def list_voices(request: Request) -> dict[str, Any]:
        registry: VoiceRegistry = request.app.state.registry
        return {"count": len(registry), "voices": registry.public_list(), "errors": registry.errors}

    @router.get("/v1/voices/{voice_id}", tags=["voices"])
    def get_voice(voice_id: str, request: Request) -> dict[str, Any]:
        return voice_or_404(request.app.state.registry, voice_id).public()

    # --------------------------------------------------------------------- tts
    @router.post("/v1/tts", tags=["tts"], responses={200: {"content": {"audio/wav": {}}}})
    def tts(payload: TTSIn, request: Request) -> Response:
        state = request.app.state
        voice = voice_or_404(state.registry, payload.voice)
        samples = run_synth(state.engine, voice, [payload.text], payload.language, payload.seed)[0]
        if payload.normalize:
            samples = audio.normalize_peak(samples, -1.0)
        rate = state.engine.sample_rate
        data = audio.wav_bytes(samples, rate)
        return Response(
            content=data,
            media_type="audio/wav",
            headers={
                "X-Voice": voice.id,
                "X-Sample-Rate": str(rate),
                "X-Duration-Seconds": f"{len(samples) / float(rate):.3f}",
                "Content-Disposition": f'inline; filename="{voice.id}.wav"',
            },
        )

    @router.post("/v1/tts/batch", tags=["tts"], responses={200: {"content": {"application/zip": {}}}})
    def tts_batch(payload: BatchTTSIn, request: Request) -> Response:
        state = request.app.state
        voice = voice_or_404(state.registry, payload.voice)
        texts = [line.text for line in payload.lines]
        clips = run_synth(state.engine, voice, texts, payload.language, payload.seed)
        rate = state.engine.sample_rate
        buffer = io.BytesIO()
        entries = []
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for index, (line, clip) in enumerate(zip(payload.lines, clips), start=1):
                if payload.normalize:
                    clip = audio.normalize_peak(clip, -1.0)
                name = f"{index:03d}_{voice.id}.wav"
                archive.writestr(name, audio.wav_bytes(clip, rate))
                entries.append(
                    {
                        "file": name,
                        "text": line.text,
                        "language": line.language or payload.language or voice.language,
                        "seconds": round(len(clip) / float(rate), 3),
                    }
                )
            archive.writestr(
                "manifest.json",
                json.dumps(
                    {"voice": voice.id, "sample_rate": rate, "entries": entries},
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        return Response(
            content=buffer.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{voice.id}-lines.zip"'},
        )

    # ------------------------------------------------------------- soundtracks
    @router.post("/v1/soundtracks", tags=["soundtracks"])
    def create_soundtrack(
        payload: SoundtrackIn,
        request: Request,
        wait: bool = Query(default=False),
        timeout: float = Query(default=600.0, ge=1.0, le=3600.0),
    ) -> Response:
        state = request.app.state
        if len(payload.lines) > state.settings.max_lines:
            raise HTTPException(
                status_code=400,
                detail=f"{len(payload.lines)} lines exceeds MAX_LINES={state.settings.max_lines}",
            )
        job = state.jobs.submit(payload)
        if not wait:
            return JSONResponse(state.jobs.public(job, base(request)), status_code=202)
        if not state.jobs.wait(job, timeout):
            return JSONResponse(
                state.jobs.public(job, base(request)),
                status_code=202,
                headers={"Retry-After": "5"},
            )
        data = state.jobs.public(job, base(request))
        if job.status == "error":
            raise HTTPException(status_code=500, detail=job.error or "soundtrack failed")
        return JSONResponse(data, status_code=200)

    @router.get("/v1/soundtracks", tags=["soundtracks"])
    def list_soundtracks(request: Request, limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
        state = request.app.state
        return {"jobs": [state.jobs.public(job, base(request)) for job in state.jobs.recent(limit)]}

    @router.get("/v1/soundtracks/{job_id}", tags=["soundtracks"])
    def get_soundtrack(job_id: str, request: Request) -> dict[str, Any]:
        try:
            job: Job = request.app.state.jobs.get(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no soundtrack job {job_id!r}") from exc
        return request.app.state.jobs.public(job, base(request))

    @router.get("/v1/soundtracks/{job_id}/files/{name}", tags=["soundtracks"])
    def get_file(job_id: str, name: str, request: Request) -> FileResponse:
        if name not in _FILES:
            raise HTTPException(status_code=404, detail="unknown file")
        try:
            job = request.app.state.jobs.get(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no soundtrack job {job_id!r}") from exc
        path = job.directory / name
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"{name} is not ready")
        return FileResponse(
            path,
            media_type=_FILES[name],
            headers={"Content-Disposition": f'inline; filename="{name}"'},
        )

    @router.get("/v1/soundtracks/{job_id}/stems/{name}", tags=["soundtracks"])
    def get_stem(job_id: str, name: str, request: Request) -> FileResponse:
        if not _STEM_RE.match(name):
            raise HTTPException(status_code=404, detail="unknown stem")
        try:
            job = request.app.state.jobs.get(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no soundtrack job {job_id!r}") from exc
        stems = (job.directory / "stems").resolve()
        path = (stems / name).resolve()
        if path.parent != stems or not path.is_file():
            raise HTTPException(status_code=404, detail=f"{name} is not ready")
        return FileResponse(
            path,
            media_type="audio/wav",
            headers={"Content-Disposition": f'inline; filename="{name}"'},
        )

    app.include_router(router)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "info").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=os.environ.get("LOG_LEVEL", "info"))


if __name__ == "__main__":
    main()
