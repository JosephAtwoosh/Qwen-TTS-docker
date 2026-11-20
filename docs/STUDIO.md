# Moviestudio ↔ rented GPU, one speaker turn per call

This is the integration analysis for the PHP studio on this laptop talking to
`Qwen-TTS-docker` on a rented GPU **one turn at a time** — no long soundtrack
job, no 175-file pull.

## What works today

`POST /v1/tts` already is the one-turn endpoint:

```json
// request
{"voice":"henry","text":"I say, that is a very large cabbage.","language":"English","seed":1942}

// response: audio/wav
// headers: X-Voice, X-Duration-Seconds, X-Sample-Rate
```

Measured from the laptop over the public port:

| Turn | Voice | WAV | HTTP time |
|---|---|---|---:|
| shot001 | the-major | 6.74 s | 13.05 s |
| shot002 | mildred | 10.34 s | 18.61 s |
| shot003 | mildred | 9.24 s | 16.90 s |
| shot004 | mildred | 10.60 s | 19.70 s |

That is ~17 s per turn, ~0.4 MB per WAV, about **$0.0021 of GPU** at $0.44/h.
It is slower than the batched job (175 turns took 11m45s there) because each
request is one sequence; the trade is that every WAV arrives immediately and a
failure costs one turn, not the whole film.

## The studio gap is data, not transport

`bookend-repo/studio` already has everything needed to **store** one turn:

- `store_media($tmp, 'audio/wav')` — content-addressed WAV storage
- `audio_duration(media_path($file), 'audio/wav')` — duration from the WAV header
- `shot_audio_set($id, $stored, $seconds)` — attaches it to a shot
- `?r=media&audio=<id>` — serves it behind the session

What it does **not** have is the line itself. The `shots` table is:

```text
slug, position, start_text, stop_text, motion_prompt, duration_s,
audio_file, audio_mime, audio_bytes, audio_seconds, ...
```

There is no `dialogue`, no `speaker`, and `assets` has no voice ID. So the
studio cannot answer “what text, in whose voice?” for a shot. That has to be
imported before a one-turn button can work.

The old xmas studio already solved the attribution:

- [plan/legacy-take1-turns.json](../plan/legacy-take1-turns.json) — 167 turns,
  speaker, legacy voice ID, Qwen voice, text, timestamps
- [plan/cut-chunk-speakers.json](../plan/cut-chunk-speakers.json) — 179 cut
  chunks mapped to speaker/voice, with `start_s` and `duration`
- `assets` names map 1:1: Frederick→`the-major`, Mildred→`mildred`,
  Hilda→`hilda`, Gerry→`gerry`, Henry→`henry`, Mrs Hubbard→`mrs-hubbard`,
  Petra→`petra`, Oliver→`oliver`; narrator→`mildred`.

### Minimal schema additions

Add a turn table rather than duplicating dialogue on 250 shots:

```sql
CREATE TABLE IF NOT EXISTS tts_turns (
    id           INTEGER PRIMARY KEY,
    project_id   INTEGER NOT NULL,
    scene_no     INTEGER,
    turn_index   INTEGER,
    chunk        TEXT,
    speaker      TEXT NOT NULL,
    voice_id     TEXT NOT NULL,
    text         TEXT NOT NULL,
    seed         INTEGER NOT NULL DEFAULT 1942,
    audio_file   TEXT,
    audio_mime   TEXT,
    audio_bytes  INTEGER NOT NULL DEFAULT 0,
    audio_seconds REAL,
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending|running|done|error
    attempts     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    hash         TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shot_turns (
    shot_id  INTEGER NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
    turn_id  INTEGER NOT NULL REFERENCES tts_turns(id) ON DELETE CASCADE,
    PRIMARY KEY (shot_id, turn_id)
);

ALTER TABLE assets ADD COLUMN voice_id TEXT;   -- guarded migration
```

Import `plan/cut-chunk-speakers.json` one row per chunk, and `shot_turns` from
`plan/cut-250.json` (`shot → chunk`). Multiple shots share a chunk; that is the
point of the cache below.

If you want the absolute minimum change, add `dialogue`, `speaker`, `voice_id`
to `shots` instead. It duplicates text across split shots and needs the cache
to avoid paying twice, but it avoids a join.

## Transport: tunnel first, public port second

The GPU is a rental; its IP/port change. Two options:

**A. SSH local forward (recommended).**

```bash
ssh -N -p <ssh_port> root@<ssh_host> \
  -L 18000:127.0.0.1:8000 \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes
```

PHP then uses `tts_url = http://127.0.0.1:18000`. The API is not exposed to the
internet, the Bearer key never crosses a public network, and the studio talks to
localhost. The cost is that the tunnel must be supervised — the same flaky pipe
we saw. A small watchdog that restarts the `ssh -N` when it exits is enough.

**B. Public Vast port.**

`tts_url = http://<public_ip>:<mapped_port>`, Bearer key required, no TLS. This
is what the debug used; it is simpler but the key travels in clear text. If you
use it, restrict the Vast firewall to the laptop's address and rotate the key
every rental.

Do not put the key in JavaScript. The browser calls the PHP app; PHP calls the
GPU server-side.

## Synchronous vs queue

The studio runs on PHP's built-in server, which is **single-threaded**. A 17 s
`/v1/tts` call blocks every other page, including `?r=media` for images. Two
shapes:

### Synchronous (smallest change)

A `shot.tts` POST does the cURL call inline:

```php
set_time_limit(180);
$client = new TtsClient(setting('tts_url'), setting('tts_key'));
$turn = tts_turn_for_shot($shotId);
$wav = $client->synthesize($turn['text'], $turn['voice_id'], (int)$turn['seed']);
$tmp = tempnam(sys_get_temp_dir(), 'tts');
file_put_contents($tmp, $wav['wav']);
$stored = store_media($tmp, 'audio/wav');
shot_audio_set($shotId, $stored, audio_duration(media_path($stored['file']), 'audio/wav'));
```

Fine for one editor clicking one shot and waiting with a spinner. It is not fine
for “generate the scene”, which is 10–20 sequential calls.

### Queue + CLI worker (recommended)

The browser only enqueues; a separate PHP CLI process owns the GPU calls:

```text
tts_queue(id, turn_id, shot_id, status, attempts, error, created_at, updated_at)
```

- Web: `INSERT` pending, redirect back immediately.
- Worker: `php tools/tts_worker.php` loops, claims one row with
  `BEGIN IMMEDIATE`, calls `/v1/tts`, stores the WAV, updates the shot, marks
  done/error.
- Page: polls `?r=tts.status&id=…` (or reloads) and shows pending/running/done.

SQLite is in WAL mode, so the worker and the `php -S` process can share
`app.sqlite`. One worker is correct: the GPU service serializes generation
anyway.

## Caching and dedupe

`store_media` already hashes the bytes, so identical WAVs collapse to one file.
Add a request-level hash:

```php
$hash = hash('sha256', $voiceId . "\0" . $text . "\0" . $seed);
```

Look up `tts_turns.hash` first; if a done row exists, copy its `audio_file` to
the new shot instead of calling the GPU. The cut has 250 shots but only 179
chunks and 176 unique texts, so this is a large saving: a second scene pass is
free, and retries do not re-bill.

## Duration: one thing to add to `/v1/tts`

The one-turn endpoint returns **natural** duration. A shot has `duration_s`
(e.g. 10.03 s); the measured natural WAV was 6.74 s. That is fine if the editor
lets the audio end early, but not if the picture is cut to the line.

Recommended service change: accept `target_duration_s` on `POST /v1/tts` and
return the fitted WAV plus `X-Speed`, the same 0.8–1.25× clamp the soundtrack
job uses. Without it, the studio must either:

- accept natural length and adjust the shot, or
- run a local `ffmpeg -filter:a atempo=…` on the returned WAV (ffmpeg is at
  `C:\Z440\the-little-escape\node_modules\ffmpeg-static\ffmpeg.exe` on this
  laptop, but that is an extra dependency for the studio host).

The reference client sends `target_duration_s` when given, so it will work the
day the service accepts it.

## Failure handling

| Failure | Behaviour |
|---|---|
| Tunnel down / connection refused | cURL error 7; do not touch the shot; show “TTS offline” |
| 401 | key wrong; fail fast, never retry |
| 404 unknown voice | data bug; log the voice id, do not retry |
| 502 / CUDA OOM | retry once after 5 s, then mark error |
| Timeout (120 s) | mark error; the model may still be generating, so retry only after checking `/health` |
| Partial WAV / non-RIFF | reject; `store_media` never sees it |
| Duplicate click | hash cache or `tts_queue` unique index prevents a second call |

Always check `HTTP 200` **and** `substr($body, 0, 4) === 'RIFF'` before storing.
Record `X-Duration-Seconds`, elapsed time and HTTP code in the row for audit.

## Cost and latency budget

| Item | Per turn | 176 unique turns |
|---|---:|---:|
| GPU time | ~17 s | ~50 min |
| GPU cost | ~$0.0021 | ~$0.37 |
| WAV download | ~0.4 MB | ~70 MB |
| Transfer cost @ $2.67/TB | negligible | ~$0.0002 |

A synchronous “one shot” button is a 15–20 s wait. A queued scene of 15 turns
is ~4–5 minutes of GPU, but the editor can keep working. That is the trade
against the 12-minute batched full-film job: per-turn is ~4× slower in GPU
time, but it survives a broken pipe and produces a file after every call.

## Acceptance tests

1. From the laptop: `GET /health` 200.
2. `GET /v1/voices` 200 with Bearer, 401 without.
3. One turn: 200, RIFF, `X-Duration-Seconds` > 0, stored under
   `studio/data/media/`, playable at `?r=media&audio=<id>`.
4. Same turn twice: second is a cache hit, zero GPU calls, same file hash.
5. Stop the tunnel: request fails in <10 s, shot audio unchanged, UI shows the
   error.
6. Invalid voice: 404, no DB mutation.
7. Queue: three turns enqueued, worker processes them one at a time, statuses
   pending → running → done.
8. Kill the worker mid-run: claimed row returns to pending after a timeout (or
   stays `running` with an attempt count; a startup sweep resets stale rows).

## Recommended order

1. Add `tts_turns`, `shot_turns`, `assets.voice_id`; import
   `plan/cut-chunk-speakers.json`.
2. Add `tts_url`, `tts_key`, `tts_timeout`, `tts_enabled` to `settings` and the
   settings page.
3. Add the reference `TtsClient` and a synchronous single-shot button first;
   measure the real 15–20 s wait in the UI.
4. If that is tolerable, ship it. If not, add `tts_queue` and the CLI worker;
   the client code does not change.
5. Add `target_duration_s` to `/v1/tts` if shot sync matters.

The old studio's 167 turns and the 179-chunk cut are the data source; nothing
needs to be re-analysed for attribution.
