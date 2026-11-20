# API in one screen

The service listens on `:8000` and takes `Authorization: Bearer $TTS_API_KEY`
(or `X-API-Key`). `/health` is open.

```bash
export TTS=http://127.0.0.1:8000
export KEY=change-me-before-you-rent

# 1. Is the model up, and which voices do I have?
curl -s $TTS/health | jq
curl -s -H "Authorization: Bearer $KEY" $TTS/v1/voices | jq '.voices[].id'

# 2. One line -> WAV
curl -s -X POST "$TTS/v1/tts" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"voice":"henry","text":"I say, that is a very large cabbage."}' \
  -o henry.wav

# 3. A conversation -> soundtrack, waiting for the result
curl -s -X POST "$TTS/v1/soundtracks?wait=1" \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  --data-binary @examples/dialogue.json \
  -o soundtrack.json

# 4. Download the mix and one stem
id=$(jq -r .id soundtrack.json)
curl -s "$TTS/v1/soundtracks/$id/files/mix.wav" -o mix.wav
curl -s "$TTS/v1/soundtracks/$id/stems/002_petra.wav" -o petra.wav
```

`examples/dialogue.json` shows the whole request shape: `speaker`, `text`, and
optionally `start_s` (timeline mode), `target_duration_s`, `gap_before_s`,
`gap_after_s`, `gain_db`.

## Timelines

Sequential is the default: each line starts after the previous one plus
`gap_s`. For cross-talk, use `mode: "timeline"` and give every line `start_s`:

```json
{
  "mode": "timeline",
  "duration_s": 8.0,
  "lines": [
    {"speaker": "henry", "text": "Wait—", "start_s": 0.2},
    {"speaker": "mildred", "text": "Don't you dare.", "start_s": 0.45},
    {"speaker": "henry", "text": "...never mind.", "start_s": 2.1}
  ]
}
```

Overlaps are allowed and listed under `warnings`. The mix is peak-normalised to
-1 dBFS; each line is normalised to -3 dBFS before placement, so a quiet
reference does not disappear under a loud one.

## Fitting a shot

`target_duration_s` stretches a line to fit. The service clamps changes to
0.8x–1.25x (beyond that it sounds drunk) and records both the request and the
clamp under `warnings`:

```json
{"speaker": "henry", "text": "It was the best of times.", "target_duration_s": 2.4}
```

`duration_s` on the request is the shot length. The response sets
`fits_duration: false` and adds a warning when the dialogue overruns it; it does
not truncate speech.

## Async jobs

`POST /v1/soundtracks` (without `?wait=1`) returns `202` with an id. Poll
`GET /v1/soundtracks/{id}`; `status` is `queued`, `running`, `done` or `error`.
Files appear under `/v1/soundtracks/{id}/files/…` and `/stems/…`.
