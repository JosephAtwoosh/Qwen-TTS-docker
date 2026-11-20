#!/usr/bin/env node
"use strict";
/**
 * Render one speaker turn per HTTP request.
 *
 * This is the slow, reliable path: no long server job, no 175-file scp. Each
 * call returns a WAV that is written immediately, so a broken pipe or a dead
 * instance costs at most one turn. Resume by re-running; existing files are
 * skipped.
 *
 *   node tools/render_turns.js --map plan/cut-chunk-speakers.json \
 *     --out .tts-test/turns --base-url http://host:port --key-file key.txt
 *
 * Options: --start N (1-based), --limit N, --seed N, --timeout S.
 */
const fs = require("fs");
const path = require("path");

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i < 0 ? fallback : process.argv[i + 1];
}

const MAP = arg("map", "plan/cut-chunk-speakers.json");
const OUT = arg("out", ".tts-test/turns");
const BASE = arg("base-url", "http://127.0.0.1:8000").replace(/\/+$/, "");
const KEY_FILE = arg("key-file", ".tts-test/vast-key.txt");
const START = Number(arg("start", "1"));
const LIMIT = Number(arg("limit", "0"));
const SEED = Number(arg("seed", "1942"));
const TIMEOUT = Number(arg("timeout", "180")) * 1000;

const key = fs.readFileSync(KEY_FILE, "utf8").trim();
const items = JSON.parse(fs.readFileSync(MAP, "utf8")).filter((item) => item.text);
fs.mkdirSync(OUT, { recursive: true });
const logPath = path.join(OUT, "render.jsonl");

function log(row) {
  fs.appendFileSync(logPath, JSON.stringify(row) + "\n");
}

async function one(item, index) {
  const name = `${String(index).padStart(3, "0")}_${item.chunk}_${item.qwen_voice}.wav`;
  const dest = path.join(OUT, name);
  if (fs.existsSync(dest) && fs.statSync(dest).size > 1000) {
    return { skipped: true, file: name };
  }
  let lastError = null;
  for (let attempt = 1; attempt <= 3; attempt++) {
    const started = Date.now();
    try {
      const res = await fetch(`${BASE}/v1/tts`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${key}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ voice: item.qwen_voice, text: item.text, seed: SEED + index }),
        signal: AbortSignal.timeout(TIMEOUT),
      });
      const buffer = Buffer.from(await res.arrayBuffer());
      if (!res.ok || buffer.length < 1000) {
        throw new Error(`HTTP ${res.status}: ${buffer.toString("utf8").slice(0, 200)}`);
      }
      fs.writeFileSync(dest, buffer);
      const row = {
        ts: new Date().toISOString(),
        index,
        chunk: item.chunk,
        scene: item.scene_number,
        speaker: item.speaker,
        voice: item.qwen_voice,
        text: item.text,
        target_s: item.duration,
        wav_s: Number(res.headers.get("x-duration-seconds")),
        bytes: buffer.length,
        elapsed_s: Number(((Date.now() - started) / 1000).toFixed(2)),
        attempt,
        status: res.status,
        file: name,
      };
      log(row);
      return row;
    } catch (err) {
      lastError = err;
      log({
        ts: new Date().toISOString(),
        index,
        chunk: item.chunk,
        voice: item.qwen_voice,
        attempt,
        status: 0,
        error: String(err.message || err),
        elapsed_s: Number(((Date.now() - started) / 1000).toFixed(2)),
      });
      await new Promise((r) => setTimeout(r, 2000 * attempt));
    }
  }
  return { failed: true, error: String(lastError), file: name };
}

(async () => {
  const slice = items.slice(START - 1, LIMIT > 0 ? START - 1 + LIMIT : undefined);
  console.log(`turns: ${slice.length} (start ${START}), out ${OUT}, base ${BASE}`);
  let done = 0;
  let failed = 0;
  let audio = 0;
  const started = Date.now();
  for (const [offset, item] of slice.entries()) {
    const index = START + offset;
    const result = await one(item, index);
    if (result.failed) {
      failed++;
      console.error(`${index}/${items.length} FAILED ${item.chunk}: ${result.error}`);
    } else if (!result.skipped) {
      done++;
      audio += result.wav_s || 0;
      console.log(
        `${index}/${items.length} ${item.chunk} ${item.qwen_voice.padEnd(12)} ` +
          `${result.wav_s}s in ${result.elapsed_s}s -> ${result.file}`,
      );
    }
  }
  console.log(
    JSON.stringify({
      done,
      failed,
      audio_seconds: Number(audio.toFixed(2)),
      elapsed_seconds: Number(((Date.now() - started) / 1000).toFixed(1)),
      log: logPath,
    }),
  );
  process.exit(failed ? 1 : 0);
})();
