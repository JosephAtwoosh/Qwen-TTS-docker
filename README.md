# Qwen-TTS Docker

A Dockerized, GPU-ready HTTP service around
[Qwen3-TTS-12Hz-1.7B-Base](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base),
built to turn a **multi-speaker dialogue script into a conversation soundtrack**:
one mixed WAV, one stem per line, and a timing manifest a film editor can read.

It was built for the [moviestudio](http://localhost/moviestudio/) film pipeline
(Cherry Tree Farm), where every shot already has a duration and a dialogue row.
The service is deliberately studio-agnostic: it takes JSON, returns audio.

- **Voice cloning** — 3-second references, ICL mode (audio **and** transcript).
- **Conversation mixing** — sequential turns or absolute timeline with overlaps.
- **Shot fitting** — optional per-line duration target, clamped to 0.8–1.25×.
- **Stems** — every line is also written on its own, for re-timing in the edit.
- **Voice registry** — a directory of `profile.json` + `ref.wav`; no database.
- **ElevenLabs import** — pull a voice's reference audio and transcribe it.
- **Vast.ai template** — image, port map and on-start script included.

Status: the image builds and its unit/API tests run on CPU in GitHub Actions.
It has **not yet been rendered on a rented GPU** (the Vast run is deliberately
left for an explicit go-ahead). Treat the first GPU run as a verification step,
not a production run.

---

## Requirements

| | Minimum | Recommended |
|---|---|---|
| GPU | NVIDIA 12 GB, compute 8.0+ | 24 GB (RTX 4090 / A6000 / L40S) |
| Driver | 525+ (CUDA 12.x) | 550+ |
| RAM | 16 GB | 32 GB |
| Disk | 40 GB | 60 GB (4 GB weights + outputs) |
| Docker | Engine 24+, NVIDIA Container Toolkit | current |

The 1.7B model in bf16 plus the 12 Hz tokenizer fits comfortably in 8–10 GB;
the headroom is for batched lines. `MAX_BATCH=6` is the 24 GB default; use 2–3
on a 12 GB card.

## Quick start (this machine, with Docker and a GPU)

```bash
cp .env.example .env          # set TTS_API_KEY
./scripts/run.sh build
./scripts/run.sh download     # ~4 GB checkpoint, once
./scripts/run.sh doctor
./scripts/run.sh up
curl -s localhost:8000/health | jq
```

The first `/health` can say `degraded` for a minute or two while the model
loads in the background; the healthcheck has a 240 s start period for that.

## Quick start (GHCR image, no local Docker)

```bash
docker run --gpus all --rm -p 8000:8000 \
  -v $PWD/models:/models -v $PWD/voices:/voices -v $PWD/output:/output \
  -e TTS_API_KEY=change-me \
  ghcr.io/josephatwoosh/qwen-tts-docker:latest
```

The image is built and pushed by
[`.github/workflows/build-image.yml`](.github/workflows/build-image.yml) on every
push to `main`.

> GHCR packages start **private**, even when the repository is public. For Vast
> to pull the image without a registry token, open the package page → *Package
> settings* → *Change visibility* → **Public**. See [docs/VAST.md](docs/VAST.md).

---

## Voices

The service finds a voice by scanning `/voices/*/profile.json`. Import the 29
film-cast voices from ElevenLabs with:

```bash
export ELEVENLABS_API_KEY=...
export OPENROUTER_API_KEY=...       # only needed if the ElevenLabs key lacks speech_to_text
node tools/import_voices.js \
  --voices-dir ./voices \
  --category generated \
  --transcribe auto \
  --pack
```

That writes `voices/<slug>/{profile.json,ref.wav}`, a `qwen-voices.tar.gz`
(24 MB for 29 voices), and a `.sha256`. The importer prefers an original sample
when the API exposes one, otherwise the voice's preview; it trims silence,
resamples to 24 kHz mono, and transcribes the clip for ICL cloning.

The transcript matters. Qwen3-TTS Base conditions on `ref_audio` **and**
`ref_text`; a mismatched transcript is the most common cause of a clone that
sounds like a different person. Backends:

| `--transcribe` | What it uses | Notes |
|---|---|---|
| `auto` | Scribe → Gemini → none | the default |
| `elevenlabs` | `POST /v1/speech-to-text` (`scribe_v1`) | key needs `speech_to_text`; our account's key does not |
| `openrouter` | Gemini 2.5 Flash audio input | ~$0.0009 per 20 s clip |
| `none` | `--transcripts file.json` | fully offline |

See [docs/VOICES.md](docs/VOICES.md) for quality notes, the voice table, the
profile format, and the ElevenLabs terms caveat.

---

## API

Full examples: [examples/curl.md](examples/curl.md).

```bash
# one line
curl -s -X POST localhost:8000/v1/tts \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"voice":"henry","text":"I say, that is a very large cabbage."}' \
  -o henry.wav

# a conversation, returning when the mix is ready
curl -s -X POST "localhost:8000/v1/soundtracks?wait=1" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  --data-binary @examples/dialogue.json -o soundtrack.json
```

A soundtrack request:

```json
{
  "title": "Scene 1",
  "mode": "sequential",
  "gap_s": 0.25,
  "duration_s": 42.0,
  "lines": [
    {"speaker": "the-major", "text": "Step aside, ladies."},
    {"speaker": "petra", "text": "Oh look, Ols! Aren't they adorable?"},
    {"speaker": "mrs-hubbard", "text": "A hundred and twenty pounds cash for all four."}
  ]
}
```

`mode: "timeline"` uses each line's `start_s` and allows overlaps (cross-talk);
overlaps are reported in `warnings`. `target_duration_s` fits a line to a shot;
changes beyond 0.8–1.25× are clamped and reported rather than applied silently.
`duration_s` never truncates speech — it sets `fits_duration: false` and warns.

| Endpoint | Purpose |
|---|---|
| `GET /health` | open; model, GPU, voice count, load errors |
| `GET /v1/voices` | registry + per-profile validation errors |
| `GET /v1/voices/{id}` | one voice |
| `POST /v1/tts` | one line → WAV |
| `POST /v1/tts/batch` | many lines, one voice → ZIP |
| `POST /v1/soundtracks` | script → job; `?wait=1` for synchronous |
| `GET /v1/soundtracks/{id}` | status + manifest |
| `GET /v1/soundtracks/{id}/files/{name}` | `mix.wav`, `mix.mp3`, `manifest.json` |
| `GET /v1/soundtracks/{id}/stems/{name}` | one line |

Authentication is `Authorization: Bearer <TTS_API_KEY>` (or `X-API-Key`). With
`TTS_API_KEY` empty the service is open — only acceptable on a private network.

---

## Connecting the moviestudio

This repository deliberately **does not modify the PHP studio**; it is the
service the studio calls. The studio already accepts a WAV per shot
(`shots.audio_file`, served by `?r=media&audio=<id>`), so the integration is:

1. On `?r=settings`, store the Vast endpoint and the API key. Two settings keys
   are enough: `tts_url`, `tts_key` (the page already has a settings form).
2. For each character asset, store the voice slug (`henry`, `petra`, …).
3. On "generate", POST one line to `/v1/tts`, or a scene to
   `/v1/soundtracks?wait=1`, then save `mix.wav` or the chosen stem as the
   shot's audio.

A minimal PHP call:

```php
$ch = curl_init($ttsUrl . '/v1/tts');
curl_setopt_array($ch, [
    CURLOPT_POST => true,
    CURLOPT_HTTPHEADER => ['Authorization: Bearer ' . $ttsKey, 'Content-Type: application/json'],
    CURLOPT_POSTFIELDS => json_encode(['voice' => 'henry', 'text' => $line], JSON_UNESCAPED_UNICODE),
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_TIMEOUT => 120,
]);
$wav = curl_exec($ch);
```

Because the PHP built-in server is single-threaded, prefer the per-shot call
(seconds) over a whole-scene synchronous call (tens of seconds).

---

## Vast.ai

The workflow is: CI builds → GHCR public → Vast template → rent → fetch weights
→ unpack voices → call the API. Full steps and the template fields are in
[docs/VAST.md](docs/VAST.md); the machine-readable template is
[`tools/vast-template.json`](tools/vast-template.json), and
[`tools/vast.js`](tools/vast.js) wraps search/rent/ssh/scp.

```bash
node tools/vast.js search "RTX 4090,RTX 5090,A6000" 24000 60
node tools/vast.js rent --offer <id> --image ghcr.io/josephatwoosh/qwen-tts-docker:latest \
  --disk 60 --env TTS_API_KEY=... --port 8000:8000 --yes
```

`rent` refuses without `--yes`; `destroy` refuses without `--yes` too.

---

## Performance

Measured numbers are deliberately absent: the first GPU run has not happened.
Expect the 1.7B model to be faster than real time on a 4090, with batched lines
(the service groups by voice and sends up to `MAX_BATCH` per model call) much
faster than one-by-one. Use `/health` and the `manifest.lines[].natural_seconds`
values to compute real-time factor on your card, and lower `MAX_BATCH` if a long
scene OOMs.

## How it fits together

```
app/
  config.py      environment -> Settings
  audio.py       WAV I/O, trim, peak normalise, stretch, MP3
  voices.py      profile.json directory -> VoiceRegistry
  engine.py      QwenEngine (lazy torch/qwen-tts) + MockEngine
  soundtrack.py  batching, layout, fitting, mixing, manifest
  jobs.py        one-at-a-time queue, status.json per job
  server.py      FastAPI routes + Bearer auth
tools/
  import_voices.js  ElevenLabs -> voices/ (+ pack)
  vast.js           search/rent/ssh/scp/destroy
  publish.js        push this repo without git
scripts/            entrypoint, doctor, download, host wrapper
docs/               VAST.md, VOICES.md
tests/              22 core tests + API tests (mock engine)
```

## Troubleshooting

**`/health` says `degraded`, `load_error` mentions a missing model.** Run
`qwen-tts-entrypoint download-models`; `MODEL_ID` must point at the directory
containing `model.safetensors` and `speech_tokenizer/`.

**CUDA out of memory.** Lower `MAX_BATCH` (try 2), split the scene, or use a
24 GB card. The engine trims silence before mixing, so a line's natural length
is already the speech, not the padded decode.

**`flash_attention_2` fails to import.** It is not installed by default; the
image uses PyTorch SDPA. Either keep `ATTN_IMPLEMENTATION=sdpa` or build a
custom image with `flash-attn` (needs nvcc and several GB of build time).

**401 from the API.** Send `Authorization: Bearer <TTS_API_KEY>`; `curl` with
`-H "X-API-Key: ..."` also works. `/health` is intentionally open.

**A clone sounds like someone else.** Check `ref_text` against the reference
audio. Re-import with `--force` and a better transcript; a 20–30 s clean clip
beats a 3 s one. Avoid music beds and overlapping speakers in references.

**Voice not found.** IDs are the directory names under `/voices`, lower-case
slugs (`the-major`, `mrs-hubbard`). `GET /v1/voices` lists them, and
`registry.errors` explains rejected profiles.

**The importer fails with `ffmpeg not runnable`.** Install ffmpeg or pass
`--ffmpeg <path>`. Under the DSH Windows sandbox the importer runs ffmpeg with
inherited stderr because piped stdio is denied; that is expected.

## Licensing and terms

The service code is MIT (see [LICENSE](LICENSE)). The model is Apache-2.0, per
its model card. Voice references are your material: this repository contains
none (the `voices/` directory is gitignored). Importing ElevenLabs voices into
another engine may conflict with ElevenLabs' terms — check them for your
account and use case before publishing the resulting soundtrack commercially.
