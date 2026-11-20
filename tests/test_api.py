import importlib
import time
from urllib.parse import urlparse

import pytest

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

AUTH = {"Authorization": "Bearer secret"}


@pytest.fixture
def client(tmp_path, voices_dir, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setenv("ENGINE", "mock")
    monkeypatch.setenv("VOICES_DIR", str(voices_dir))
    monkeypatch.setenv("OUTPUT_DIR", str(out))
    monkeypatch.setenv("TTS_API_KEY", "secret")
    monkeypatch.setenv("PRELOAD_MODEL", "0")
    monkeypatch.setenv("MAX_BATCH", "2")

    from app.config import reset_settings

    reset_settings()
    import app.server as server

    importlib.reload(server)
    app = server.create_app()
    with TestClient(app) as test_client:
        yield test_client
    reset_settings()


def test_health_is_open(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["voices"]["count"] == 2
    assert body["auth_required"] is True


def test_auth_is_enforced(client):
    assert client.get("/v1/voices").status_code == 401
    assert client.get("/v1/voices", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/v1/voices", headers={"X-API-Key": "secret"}).status_code == 200


def test_voice_listing(client):
    body = client.get("/v1/voices", headers=AUTH).json()
    assert [voice["id"] for voice in body["voices"]] == ["henry", "mildred"]
    assert body["errors"] == []


def test_tts_returns_wav(client):
    response = client.post(
        "/v1/tts",
        headers=AUTH,
        json={"voice": "henry", "text": "Hello there."},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/wav")
    assert response.content[:4] == b"RIFF"
    assert float(response.headers["x-duration-seconds"]) > 0.5


def test_unknown_voice_is_404(client):
    response = client.post("/v1/tts", headers=AUTH, json={"voice": "nobody", "text": "Hi."})
    assert response.status_code == 404


def test_batch_returns_zip(client):
    response = client.post(
        "/v1/tts/batch",
        headers=AUTH,
        json={"voice": "henry", "lines": [{"text": "One."}, {"text": "Two."}]},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.content[:2] == b"PK"


def test_soundtrack_wait_and_download(client):
    payload = {
        "title": "Scene 1",
        "lines": [
            {"speaker": "henry", "text": "Hello there."},
            {"speaker": "mildred", "text": "Hi."},
        ],
        "gap_s": 0.1,
    }
    response = client.post("/v1/soundtracks?wait=1", headers=AUTH, json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "done"
    manifest = body["manifest"]
    assert len(manifest["lines"]) == 2

    mix_url = urlparse(manifest["urls"]["mix_wav"]).path
    mix = client.get(mix_url, headers=AUTH)
    assert mix.status_code == 200
    assert mix.content[:4] == b"RIFF"

    job_id = body["id"]
    stem = manifest["lines"][0]["stem"]
    stem_response = client.get(f"/v1/soundtracks/{job_id}/stems/{stem.split('/')[-1]}", headers=AUTH)
    assert stem_response.status_code == 200

    manifest_response = client.get(f"/v1/soundtracks/{job_id}/files/manifest.json", headers=AUTH)
    assert manifest_response.status_code == 200


def test_async_job_polls_to_done(client):
    payload = {"lines": [{"speaker": "henry", "text": "A short line."}]}
    response = client.post("/v1/soundtracks", headers=AUTH, json=payload)
    assert response.status_code == 202
    job_id = response.json()["id"]

    for _ in range(100):
        body = client.get(f"/v1/soundtracks/{job_id}", headers=AUTH).json()
        if body["status"] in {"done", "error"}:
            break
        time.sleep(0.05)
    assert body["status"] == "done", body
    assert body["manifest"]["total_seconds"] > 0


def test_unknown_job_is_404(client):
    assert client.get("/v1/soundtracks/nope", headers=AUTH).status_code == 404
