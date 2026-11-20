#!/usr/bin/env node
"use strict";
/**
 * Import ElevenLabs voices as Qwen3-TTS clone references.
 *
 * For each voice it downloads a clean reference clip (the voice's sample when
 * the API exposes one, otherwise its preview MP3), converts it to 24 kHz mono
 * WAV, transcribes it, and writes:
 *
 *   voices/<slug>/profile.json
 *   voices/<slug>/ref.wav
 *
 * The transcript matters: Qwen3-TTS Base clones in ICL mode, which conditions
 * on reference audio *and its transcript*. `--transcribe` chooses the backend:
 *
 *   auto       ElevenLabs Scribe, then OpenRouter (Gemini), then none
 *   elevenlabs POST /v1/speech-to-text  (the key needs speech_to_text)
 *   openrouter Gemini audio input       (OPENROUTER_API_KEY)
 *   none       require --transcripts <json> mapping name -> text
 *
 * Examples
 *   node tools/import_voices.js --voices-dir ./voices --category generated
 *   node tools/import_voices.js --voices-dir ./voices --names Henry,Mildred --pack
 *   node tools/import_voices.js --voices-dir ./voices --dry-run
 *
 * No npm dependencies. Node 18+.
 */
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { spawnSync } = require("child_process");

const API = "https://api.elevenlabs.io/v1";
const OPENROUTER = "https://openrouter.ai/api/v1/chat/completions";

function parseArgs(argv) {
  const args = {
    voicesDir: "./voices",
    category: "generated",
    language: "English",
    transcribe: "auto",
    asrModel: "google/gemini-2.5-flash",
    source: "auto",
    limit: 0,
    names: [],
    force: false,
    pack: false,
    dryRun: false,
    keepRaw: false,
    noCache: false,
    id: "",
    sampleId: "",
    ffmpeg: process.env.FFMPEG || "ffmpeg",
    transcripts: "",
    elevenKeyFile: process.env.ELEVENLABS_KEY_FILE || "",
    openrouterKeyFile: process.env.OPENROUTER_KEY_FILE || "",
    maxSeconds: 30,
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const next = () => {
      const v = argv[++i];
      if (v === undefined) throw new Error(`${a} needs a value`);
      return v;
    };
    switch (a) {
      case "--voices-dir": args.voicesDir = next(); break;
      case "--category": args.category = next(); break;
      case "--language": args.language = next(); break;
      case "--transcribe": args.transcribe = next(); break;
      case "--asr-model": args.asrModel = next(); break;
      case "--source": args.source = next(); break;
      case "--limit": args.limit = Number(next()); break;
      case "--names": args.names = next().split(",").map((s) => s.trim()).filter(Boolean); break;
      case "--id": args.id = next(); break;
      case "--sample-id": args.sampleId = next(); break;
      case "--transcripts": args.transcripts = next(); break;
      case "--ffmpeg": args.ffmpeg = next(); break;
      case "--eleven-key-file": args.elevenKeyFile = next(); break;
      case "--openrouter-key-file": args.openrouterKeyFile = next(); break;
      case "--max-seconds": args.maxSeconds = Number(next()); break;
      case "--force": args.force = true; break;
      case "--pack": args.pack = true; break;
      case "--dry-run": args.dryRun = true; break;
      case "--keep-raw": args.keepRaw = true; break;
      case "--no-cache": args.noCache = true; break;
      case "-h": case "--help": args.help = true; break;
      default: throw new Error(`unknown argument ${a}`);
    }
  }
  return args;
}

const args = parseArgs(process.argv.slice(2));
if (args.help) {
  console.log(fs.readFileSync(__filename, "utf8").split("*/")[0]);
  process.exit(0);
}

function keyFrom(envName, file) {
  if (process.env[envName]) return process.env[envName].trim();
  if (file) {
    try {
      const value = fs.readFileSync(file, "utf8").trim();
      if (value) return value;
    } catch {
      /* fall through */
    }
  }
  return "";
}

const ELEVEN_KEY = keyFrom("ELEVENLABS_API_KEY", args.elevenKeyFile);
const OPENROUTER_KEY = keyFrom("OPENROUTER_API_KEY", args.openrouterKeyFile);
if (!ELEVEN_KEY) {
  console.error("No ElevenLabs key. Set ELEVENLABS_API_KEY or pass --eleven-key-file.");
  process.exit(2);
}

const slug = (s) =>
  s
    .normalize("NFKD")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "") || "voice";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function request(url, options, attempt = 0) {
  try {
    const res = await fetch(url, { ...options, signal: AbortSignal.timeout(120000) });
    if (res.status === 429 && attempt < 4) {
      const wait = 2000 * 2 ** attempt;
      console.warn(`  rate limited; retrying in ${wait / 1000}s`);
      await sleep(wait);
      return request(url, options, attempt + 1);
    }
    return res;
  } catch (err) {
    if (attempt < 2) {
      await sleep(1500 * (attempt + 1));
      return request(url, options, attempt + 1);
    }
    throw err;
  }
}

async function listVoices() {
  const res = await request(`${API}/voices`, { headers: { "xi-api-key": ELEVEN_KEY } });
  const text = await res.text();
  if (!res.ok) throw new Error(`GET /v1/voices -> ${res.status}: ${text.slice(0, 300)}`);
  return JSON.parse(text).voices || [];
}

async function download(url, headers, dest) {
  const res = await request(url, { headers });
  if (!res.ok) throw new Error(`download ${url} -> ${res.status}`);
  const buf = Buffer.from(await res.arrayBuffer());
  fs.writeFileSync(dest, buf);
  return buf;
}

function ffmpegToWav(input, output, maxSeconds) {
  const trim =
    "silenceremove=start_periods=1:start_duration=0.05:start_threshold=-45dB";
  // Trim the end by reversing, trimming the (new) start, and reversing back.
  // `stop_periods` on the forward pass cuts at the first internal pause instead
  // of the end of the file -- measured on Henry's preview: 20.7s -> 0.25s.
  const filter = `${trim},areverse,${trim},areverse`;
  const result = spawnSync(
    args.ffmpeg,
    [
      "-hide_banner", "-loglevel", "error", "-y",
      "-i", input,
      "-af", filter,
      "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le",
      "-t", String(maxSeconds),
      output,
    ],
    // Inherited stderr keeps this working under the DSH Windows sandbox, which
    // refuses to give a child process a piped stdio handle.
    { stdio: ["ignore", "ignore", "inherit"] },
  );
  if (result.error) {
    throw new Error(
      `ffmpeg not runnable (${args.ffmpeg}): ${result.error.message}. ` +
        "Install ffmpeg or pass --ffmpeg <path>.",
    );
  }
  if (result.status !== 0) {
    throw new Error(`ffmpeg exited ${result.status} for ${path.basename(input)}`);
  }
}

function wavInfo(file) {
  const fd = fs.openSync(file, "r");
  try {
    const header = Buffer.alloc(44);
    fs.readSync(fd, header, 0, 44, 0);
    if (header.toString("ascii", 0, 4) !== "RIFF" || header.toString("ascii", 8, 12) !== "WAVE") {
      throw new Error("not a WAV file");
    }
    const rate = header.readUInt32LE(24);
    const channels = header.readUInt16LE(22);
    const bits = header.readUInt16LE(34);
    let offset = 12;
    let dataBytes = 0;
    while (offset + 8 <= header.length) {
      const id = header.toString("ascii", offset, offset + 4);
      const size = header.readUInt32LE(offset + 4);
      if (id === "data") {
        dataBytes = size;
        break;
      }
      offset += 8 + size + (size % 2);
      if (offset + 8 > header.length) break;
      // Re-read a larger header if chunks pushed the data past 44 bytes.
      const bigger = Buffer.alloc(Math.min(65536, fs.statSync(file).size));
      fs.readSync(fd, bigger, 0, bigger.length, 0);
      const idx = bigger.indexOf(Buffer.from("data"));
      if (idx > 0) {
        dataBytes = bigger.readUInt32LE(idx + 4);
        break;
      }
    }
    if (!dataBytes) dataBytes = Math.max(0, fs.statSync(file).size - 44);
    return { rate, channels, bits, seconds: dataBytes / (rate * channels * (bits / 8)) };
  } finally {
    fs.closeSync(fd);
  }
}

function hashFile(file) {
  return crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
}

function cleanTranscript(text) {
  let value = String(text || "")
    .replace(/\r/g, "")
    .replace(/\s+/g, " ")
    .trim();
  value = value.replace(/^["'“”]+|["'“”]+$/g, "").trim();
  value = value.replace(/^(transcript|text)\s*:\s*/i, "").trim();
  return value;
}

async function transcribeElevenlabs(wavPath) {
  const buf = fs.readFileSync(wavPath);
  const form = new FormData();
  form.append("model_id", "scribe_v1");
  form.append("file", new Blob([buf], { type: "audio/wav" }), path.basename(wavPath));
  const res = await request(`${API}/speech-to-text`, {
    method: "POST",
    headers: { "xi-api-key": ELEVEN_KEY },
    body: form,
  });
  const text = await res.text();
  if (!res.ok) {
    const error = new Error(`scribe ${res.status}: ${text.slice(0, 300)}`);
    error.status = res.status;
    error.body = text;
    throw error;
  }
  const parsed = JSON.parse(text);
  return { text: cleanTranscript(parsed.text), backend: "elevenlabs" };
}

async function transcribeOpenRouter(wavPath) {
  if (!OPENROUTER_KEY) throw new Error("OPENROUTER_API_KEY is not set");
  const data = fs.readFileSync(wavPath).toString("base64");
  const res = await request(OPENROUTER, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${OPENROUTER_KEY}`,
      "Content-Type": "application/json",
      "HTTP-Referer": "http://localhost/moviestudio/",
      "X-Title": "Qwen-TTS voice import",
    },
    body: JSON.stringify({
      model: args.asrModel,
      temperature: 0,
      messages: [
        {
          role: "user",
          content: [
            {
              type: "text",
              text:
                "Transcribe this audio verbatim. Output only the spoken words with normal punctuation and " +
                "capitalisation. Do not add commentary, labels, quotes or timestamps.",
            },
            { type: "input_audio", input_audio: { data, format: "wav" } },
          ],
        },
      ],
    }),
  });
  const text = await res.text();
  if (!res.ok) throw new Error(`openrouter ${res.status}: ${text.slice(0, 300)}`);
  const parsed = JSON.parse(text);
  return {
    text: cleanTranscript(parsed.choices?.[0]?.message?.content || ""),
    backend: "openrouter",
  };
}

async function transcribe(wavPath, known) {
  const order =
    args.transcribe === "auto"
      ? ["elevenlabs", "openrouter", "none"]
      : [args.transcribe];
  let lastError = null;
  for (const backend of order) {
    try {
      if (backend === "none") {
        if (known) return { text: cleanTranscript(known), backend: "provided" };
        throw new Error("no transcript provided (--transcripts)");
      }
      if (backend === "elevenlabs") return await transcribeElevenlabs(wavPath);
      if (backend === "openrouter") return await transcribeOpenRouter(wavPath);
      throw new Error(`unknown --transcribe ${backend}`);
    } catch (err) {
      lastError = err;
      if (args.transcribe !== "auto") throw err;
      const hint =
        err.status === 401 && /permission/i.test(err.body || "")
          ? " (the key lacks speech_to_text)"
          : "";
      console.warn(`  ${backend} transcription unavailable${hint}; trying next`);
    }
  }
  throw lastError || new Error("transcription failed");
}

async function main() {
  const voices = await listVoices();
  let selected = voices.filter((v) => !args.category || v.category === args.category);
  if (args.names.length) {
    const wanted = new Set(args.names.map((s) => s.toLowerCase()));
    selected = selected.filter(
      (v) => wanted.has(v.name.toLowerCase()) || wanted.has((v.voice_id || "").toLowerCase()),
    );
  }
  selected.sort((a, b) => a.name.localeCompare(b.name));
  if (args.limit > 0) selected = selected.slice(0, args.limit);

  if (!selected.length) {
    console.error(`No voices matched category=${args.category} names=${args.names.join(",") || "(all)"}`);
    console.error(`Account has ${voices.length} voices: ` +
      voices.map((v) => `${v.name} [${v.category}]`).join(", ").slice(0, 1200));
    process.exit(1);
  }

  console.log(`Importing ${selected.length} voice(s) into ${args.voicesDir}`);
  console.log(`  category=${args.category} language=${args.language} transcribe=${args.transcribe}`);
  if (args.dryRun) {
    for (const v of selected) console.log(`  would import ${v.name} (${v.category}, ${v.voice_id})`);
    return;
  }

  fs.mkdirSync(args.voicesDir, { recursive: true });
  fs.mkdirSync(path.join(args.voicesDir, ".raw"), { recursive: true });
  const transcriptCachePath = path.join(args.voicesDir, ".transcripts.json");
  let cache = {};
  try {
    cache = JSON.parse(fs.readFileSync(transcriptCachePath, "utf8"));
  } catch {
    cache = {};
  }

  const provided = {};
  if (args.transcripts) {
    const raw = JSON.parse(fs.readFileSync(args.transcripts, "utf8"));
    for (const [name, text] of Object.entries(raw)) provided[name.toLowerCase()] = text;
  }

  const usedSlugs = new Set(
    fs.existsSync(args.voicesDir)
      ? fs.readdirSync(args.voicesDir).filter((n) => fs.statSync(path.join(args.voicesDir, n)).isDirectory())
      : [],
  );
  const summary = [];
  let failures = 0;

  for (const voice of selected) {
    const name = String(voice.name || voice.voice_id);
    let id = args.id ? slug(args.id) : slug(name);
    if (usedSlugs.has(id)) {
      const existing = path.join(args.voicesDir, id, "profile.json");
      let same = false;
      try {
        same = JSON.parse(fs.readFileSync(existing, "utf8")).source_voice_id === voice.voice_id;
      } catch {
        /* different */
      }
      if (!same) {
        id = `${id}-${String(voice.voice_id).slice(0, 6).toLowerCase()}`;
      }
    }

    const dir = path.join(args.voicesDir, id);
    const profilePath = path.join(dir, "profile.json");
    const refPath = path.join(dir, "ref.wav");
    const exists = fs.existsSync(profilePath) && fs.existsSync(refPath);
    if (exists && !args.force) {
      try {
        const old = JSON.parse(fs.readFileSync(profilePath, "utf8"));
        if (old.source_voice_id === voice.voice_id && old.ref_text) {
          console.log(`  = ${name}: already imported (${id})`);
          summary.push({ name, id, seconds: old.ref_seconds, backend: "cached" });
          usedSlugs.add(id);
          continue;
        }
      } catch {
        /* re-import */
      }
    }

    try {
      console.log(`  + ${name} (${voice.category})`);
      const rawDir = path.join(args.voicesDir, ".raw");
      let rawPath = "";
      let rawBuf = null;

      // Prefer an original sample; fall back to the public preview.
      const samples = Array.isArray(voice.samples) ? voice.samples : [];
      if (args.source !== "preview" && samples.length) {
        const sample = args.sampleId
          ? samples.find((s) => (s.sample_id || s.sampleId) === args.sampleId)
          : samples.slice().sort((a, b) => (b.size_bytes || 0) - (a.size_bytes || 0))[0];
        if (!sample) throw new Error(`sample ${args.sampleId} not found on ${name}`);
        const sampleId = sample.sample_id || sample.sampleId;
        if (sampleId) {
          const url = `${API}/voices/${voice.voice_id}/samples/${sampleId}/audio`;
          const dest = path.join(rawDir, `${id}.sample`);
          try {
            rawBuf = await download(url, { "xi-api-key": ELEVEN_KEY, Accept: "audio/*" }, dest);
            rawPath = dest;
          } catch (err) {
            console.warn(`    sample download failed (${err.message}); using preview`);
          }
        }
      }
      if (!rawPath) {
        if (!voice.preview_url) throw new Error("no preview_url and no downloadable sample");
        const dest = path.join(rawDir, `${id}.mp3`);
        rawBuf = await download(voice.preview_url, {}, dest);
        rawPath = dest;
      }

      fs.mkdirSync(dir, { recursive: true });
      const tmpWav = path.join(rawDir, `${id}.wav`);
      ffmpegToWav(rawPath, tmpWav, args.maxSeconds);
      fs.copyFileSync(tmpWav, refPath);
      const info = wavInfo(refPath);
      // Cache on the *processed* reference, not the raw download: changing
      // --max-seconds changes the audio and must invalidate the transcript.
      const refHash = hashFile(refPath);
      const cacheKey = `${voice.voice_id}:${refHash.slice(0, 16)}`;
      if (info.seconds < 2.5) {
        console.warn(`    reference is only ${info.seconds.toFixed(2)}s; cloning will be weak`);
      }

      let transcript = args.noCache ? null : cache[cacheKey];
      let backend = "cached";
      if (!transcript) {
        const known = provided[name.toLowerCase()] || provided[id];
        const result = await transcribe(refPath, known);
        transcript = result.text;
        backend = result.backend;
        cache[cacheKey] = transcript;
        fs.writeFileSync(transcriptCachePath, JSON.stringify(cache, null, 2) + "\n");
      }
      if (!transcript || transcript.length < 2) {
        throw new Error("empty transcript; pass --transcripts with an explicit text");
      }

      const labels = voice.labels || {};
      const descriptionParts = [labels.description, labels.use_case, labels.accent, labels.age]
        .filter(Boolean)
        .map(String);
      const profile = {
        id,
        name,
        language: args.language,
        source: "elevenlabs",
        source_voice_id: voice.voice_id,
        source_category: voice.category,
        ref_audio: "ref.wav",
        ref_text: transcript,
        ref_seconds: Number(info.seconds.toFixed(3)),
        sha256: refHash,
        description: descriptionParts.join(" · "),
        gender: labels.gender || "",
        tags: [voice.category, labels.accent, labels.use_case].filter(Boolean).map(String),
        x_vector_only: false,
        created_at: new Date().toISOString(),
      };
      fs.writeFileSync(profilePath, JSON.stringify(profile, null, 2) + "\n");
      if (!args.keepRaw) {
        try { fs.rmSync(rawPath, { force: true }); } catch { /* ignore */ }
        try { fs.rmSync(tmpWav, { force: true }); } catch { /* ignore */ }
      }
      usedSlugs.add(id);
      console.log(
        `    ${info.seconds.toFixed(1)}s, ${transcript.length} chars, transcript=${backend}`,
      );
      summary.push({ name, id, seconds: info.seconds, backend });
    } catch (err) {
      failures++;
      console.error(`    FAILED: ${err.message}`);
    }
  }

  console.log(`\nImported ${summary.length - failures}/${selected.length} voices.`);
  if (failures) console.log(`${failures} failed — re-run with --force after fixing the cause.`);

  if (args.pack) {
    const packPath = path.join(path.dirname(path.resolve(args.voicesDir)), "qwen-voices.tar.gz");
    const result = spawnSync(
      "tar",
      [
        "-czf", packPath,
        "--exclude", `${path.basename(path.resolve(args.voicesDir))}/.raw`,
        "--exclude", `${path.basename(path.resolve(args.voicesDir))}/.transcripts.json`,
        "-C", path.dirname(path.resolve(args.voicesDir)),
        path.basename(path.resolve(args.voicesDir)),
      ],
      { stdio: ["ignore", "inherit", "inherit"] },
    );
    if (result.status !== 0) {
      console.error(`pack failed (tar exit ${result.status})`);
    } else {
      const digest = crypto.createHash("sha256").update(fs.readFileSync(packPath)).digest("hex");
      fs.writeFileSync(`${packPath}.sha256`, `${digest}  ${path.basename(packPath)}\n`);
      console.log(`packed ${packPath} (${(fs.statSync(packPath).size / 1e6).toFixed(1)} MB)`);
    }
  }
  process.exit(failures ? 1 : 0);
}

main().catch((err) => {
  console.error(err.stack || err.message);
  process.exit(1);
});
