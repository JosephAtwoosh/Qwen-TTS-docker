# Voices: import, transcript and profile format

Qwen3-TTS Base clones in **ICL mode**: it conditions on a reference clip
(`ref.wav`) *and the transcript of that clip* (`ref_text`). The transcript is
not optional metadata — a transcript that does not match the audio produces a
clone that drifts to a different speaker. `x_vector_only: true` skips the
transcript but drops quality; do not use it for film dialogue.

A reference should be:

- **3 s minimum**, 15–30 s ideal (Qwen advertises 3-second rapid clone; longer
  clean speech is more stable across sentences).
- **One speaker**, no music, no second voice, no heavy room tone.
- 24 kHz mono WAV here, but the loader resamples anything librosa reads.

## The import

```bash
export ELEVENLABS_API_KEY=...
export OPENROUTER_API_KEY=...        # optional transcript fallback
node tools/import_voices.js \
  --voices-dir ./voices \
  --category generated \
  --transcribe auto \
  --pack
```

What happens per voice:

1. `GET /v1/voices` → filter `category` / `--names`.
2. Prefer the largest downloadable original sample; otherwise the public
   `preview_url` MP3.
3. ffmpeg: mono, 24 kHz, PCM16, silence trimmed at both ends (reverse-trim for
   the tail — `stop_periods` cuts at the first internal pause, which once turned
   a 20.7 s clip into 0.25 s).
4. Transcribe (Scribe → Gemini → provided JSON), cache by audio hash in
   `voices/.transcripts.json`.
5. Write `voices/<slug>/profile.json` and `ref.wav`; skip existing voices unless
   `--force`.
6. `--pack` writes `qwen-voices.tar.gz` (plus `.sha256`) beside `voices/`,
   excluding `.raw` and the transcript cache.

Useful flags: `--dry-run`, `--limit N`, `--source preview|samples|auto`,
`--max-seconds 60`, `--keep-raw`, `--ffmpeg <path>`.

The account this was built against has 72 voices: 22 premade, 29 generated
(the film cast), 21 professional. This repo's pack is the 29 generated voices.

## The 29 film-cast voices

| Voice | id | ref |
|---|---|---|
| Alistair | `alistair` | 22.8 s |
| Amelia | `amelia` | 22.1 s |
| Annabelle | `annabelle` | 20.4 s |
| Beryl | `beryl` | 19.4 s |
| Bill Paterson | `bill-paterson` | 22.6 s |
| Boy | `boy` | 18.6 s |
| Clara | `clara` | 20.7 s |
| Debbie | `debbie` | 18.5 s |
| Dick | `dick` | 28.2 s |
| Dorothy | `dorothy` | 32.2 s |
| Gerry | `gerry` | 17.2 s |
| Heather | `heather` | 20.7 s |
| Henry | `henry` | 20.2 s |
| Hilda | `hilda` | 20.7 s |
| Jim | `jim` | 25.8 s |
| Jo Jo | `jo-jo` | 15.7 s |
| Margaret | `margaret` | 30.6 s |
| Matthew | `matthew` | 16.9 s |
| Michael Patterson | `michael-patterson` | 15.4 s |
| Mildred | `mildred` | 21.4 s |
| Miss Pearl | `miss-pearl` | 34.0 s |
| Mrs Hubbard | `mrs-hubbard` | 21.6 s |
| Oliver | `oliver` | 23.8 s |
| Petra | `petra` | 22.9 s |
| Star | `star` | 19.8 s |
| The Major | `the-major` | 24.0 s |
| Titch | `titch` | 26.1 s |
| Toby | `toby` | 16.8 s |
| Ugly Face | `ugly-face` | 31.2 s |

## Profile format

```json
{
  "id": "henry",
  "name": "Henry",
  "language": "English",
  "source": "elevenlabs",
  "source_voice_id": "GKkeIyNCXi4NCuhfWPJ7",
  "source_category": "generated",
  "ref_audio": "ref.wav",
  "ref_text": "Oh, hello there. So lovely to meet you. I'm Henrietta, by the way…",
  "ref_seconds": 20.2,
  "sha256": "…",
  "description": "",
  "gender": "",
  "tags": ["generated"],
  "x_vector_only": false,
  "created_at": "2026-11-20T14:20:00.000Z"
}
```

To add a voice by hand, create the directory, drop a WAV, and write the profile;
`GET /v1/voices` will list it after a restart. The registry rejects a profile
with a missing `ref_audio`, an empty `ref_text` (unless `x_vector_only`), or an
`id` that is not a lower-case slug.

## Quality notes

- The previews are ElevenLabs' own synthesis of the voice. Cloning a preview is
  second-generation cloning: timbre survives, fine prosody does not. If the
  account exposes the original uploaded samples, use `--source samples`.
- Twenty seconds is a good compromise. The importer caps at 30 s by default
  because a long reference slows every prompt build and decode; raise
  `--max-seconds` if a voice's only clean sample is longer.
- A transcript error is visible immediately: the clone adopts the reference's
  accent but the wrong mouth shapes. Re-transcribe that one voice with
  `--force` and a better backend rather than regenerating the whole pack.
- The service normalises each line to −3 dBFS and the mix to −1 dBFS. Relative
  loudness of a quiet reference is therefore preserved; use per-line `gain_db`
  for dramatic whispers rather than changing the reference.

## ElevenLabs terms

Importing a voice into another synthesis engine is a use of ElevenLabs' output
and service that their terms may restrict, particularly for voices created with
their models. The voices here are the account owner's own cast; before
publishing a soundtrack commercially, check the terms that applied when the
voices were created. The repository stores no voice audio — only the importer
that reads your account at your direction.
