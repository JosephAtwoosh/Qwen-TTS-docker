import json
import os
import re
import sys
import uuid
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import audio  # noqa: E402
from app.voices import VoiceRegistry  # noqa: E402

RATE = 24000


# The DSH Windows sandbox gives pytest's own temporary directories a private
# DACL that the next process cannot read. When DSH_TEST_TMP points at a normal
# workspace directory, use that instead (run with `-p no:tmpdir`). CI uses the
# stock fixture, where this branch is inert.
if os.environ.get("DSH_TEST_TMP"):

    @pytest.fixture
    def tmp_path(request):
        root = Path(os.environ["DSH_TEST_TMP"])
        root.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.name)[:80]
        directory = root / f"{safe}-{uuid.uuid4().hex[:6]}"
        directory.mkdir(parents=True, exist_ok=False)
        return directory


def write_voice(root: Path, voice_id: str, name: str | None = None, seconds: float = 3.2) -> Path:
    directory = root / voice_id
    directory.mkdir(parents=True, exist_ok=True)
    count = int(seconds * RATE)
    t = np.arange(count, dtype=np.float32) / RATE
    samples = (0.2 * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)
    audio.write_wav(directory / "ref.wav", samples, RATE)
    profile = {
        "id": voice_id,
        "name": name or voice_id.title(),
        "ref_audio": "ref.wav",
        "ref_text": f"Reference line for {voice_id}.",
        "language": "English",
        "source": "test",
        "ref_seconds": seconds,
        "sha256": audio.sha256_file(directory / "ref.wav"),
    }
    (directory / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
    return directory


@pytest.fixture
def voices_dir(tmp_path: Path) -> Path:
    root = tmp_path / "voices"
    root.mkdir()
    write_voice(root, "henry", "Henry")
    write_voice(root, "mildred", "Mildred")
    return root


@pytest.fixture
def registry(voices_dir: Path) -> VoiceRegistry:
    return VoiceRegistry(voices_dir)
