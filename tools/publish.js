#!/usr/bin/env node
"use strict";
/**
 * Publish this directory to GitHub in one commit, without git installed.
 *
 * This laptop has no `git`/`gh`, and its schannel TLS stack fails inside the
 * sandbox, so the GitHub Git Data API over Node's OpenSSL is the reliable path.
 *
 * Auth: GITHUB_TOKEN, or --token-file <path>. The token needs Contents: write
 * (and Administration: write the first time, to create the repository).
 *
 *   node tools/publish.js --owner JosephAtwoosh --repo Qwen-TTS-docker \
 *        --token-file C:\dsh\pat.txt --message "Qwen-TTS service"
 *
 * It never prints the token. Existing files are replaced; nothing is deleted.
 */
const fs = require("fs");
const path = require("path");
const https = require("https");

const ROOT = path.resolve(__dirname, "..");
const SKIP_DIRS = new Set([
  ".git", "voices", "models", "output", "node_modules", "__pycache__",
  ".pytest_cache", ".pip-tmp", ".test-work", ".venv", "venv",
]);
const TEXT_EXT = new Set([
  ".md", ".py", ".sh", ".js", ".json", ".yml", ".yaml", ".toml", ".txt",
  ".example", ".gitignore", ".dockerignore", ".gitattributes", ".cfg", ".ini",
]);

function arg(name, fallback) {
  const index = process.argv.indexOf(`--${name}`);
  if (index < 0) return fallback;
  return process.argv[index + 1];
}

const OWNER = arg("owner", "JosephAtwoosh");
const REPO = arg("repo", "Qwen-TTS-docker");
const BRANCH = arg("branch", "main");
const MESSAGE = arg("message", "Update Qwen-TTS-docker");
const DRY = process.argv.includes("--dry-run");
const tokenFile = arg("token-file", "");
const TOKEN = (process.env.GITHUB_TOKEN || (tokenFile ? fs.readFileSync(tokenFile, "utf8") : "")).trim();
if (!TOKEN) {
  console.error("No token. Set GITHUB_TOKEN or pass --token-file.");
  process.exit(2);
}

if (DRY) {
  const files = walk(ROOT).sort((a, b) => a.rel.localeCompare(b.rel));
  console.log(`would publish ${files.length} files to ${OWNER}/${REPO}@${BRANCH}`);
  for (const file of files) console.log(`  ${file.rel}`);
  process.exit(0);
}

function api(method, apiPath, body) {
  return new Promise((resolve, reject) => {
    const data = body === undefined ? null : JSON.stringify(body);
    const req = https.request(
      {
        host: "api.github.com",
        path: apiPath,
        method,
        timeout: 60000,
        headers: {
          Authorization: `Bearer ${TOKEN}`,
          "User-Agent": "qwen-tts-publish",
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          ...(data
            ? { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data) }
            : {}),
        },
      },
      (res) => {
        let buffer = "";
        res.on("data", (chunk) => (buffer += chunk));
        res.on("end", () => {
          let parsed = buffer;
          try {
            parsed = JSON.parse(buffer);
          } catch {
            /* text */
          }
          resolve({ status: res.statusCode, body: parsed, text: buffer });
        });
      },
    );
    req.on("error", reject);
    req.on("timeout", () => req.destroy(new Error("timeout")));
    if (data) req.write(data);
    req.end();
  });
}

function walk(directory, prefix = "") {
  const out = [];
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    if (entry.name.startsWith(".") && entry.name !== ".github" && entry.name !== ".gitignore" &&
        entry.name !== ".dockerignore" && entry.name !== ".gitattributes" && entry.name !== ".env.example" &&
        entry.name !== ".gitkeep") {
      continue;
    }
    if (entry.isSymbolicLink()) continue;
    const full = path.join(directory, entry.name);
    const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (entry.isDirectory()) {
      if (SKIP_DIRS.has(entry.name)) continue;
      out.push(...walk(full, rel));
    } else if (entry.isFile()) {
      if (/\.tar\.gz$|\.tar\.gz\.sha256$|\.zip$|\.pyc$/.test(entry.name)) continue;
      if (entry.name === ".env") continue;
      out.push({ full, rel });
    }
  }
  return out;
}

(async () => {
  const repoPath = `/repos/${OWNER}/${REPO}`;
  let repo = await api("GET", repoPath);
  if (repo.status === 404) {
    console.log(`creating ${OWNER}/${REPO} ...`);
    const created = await api("POST", "/user/repos", {
      name: REPO,
      description: "Qwen3-TTS-12Hz-1.7B-Base voice-clone service for film dialogue, Dockerized for Vast.ai",
      private: false,
      has_issues: true,
      auto_init: false,
    });
    if (created.status !== 201) {
      console.error(`create repo -> ${created.status}: ${created.text.slice(0, 400)}`);
      process.exit(1);
    }
    console.log(`created ${created.body.full_name}`);
    repo = { status: 200, body: created.body };
  } else if (repo.status !== 200) {
    console.error(`GET repo -> ${repo.status}: ${repo.text.slice(0, 300)}`);
    process.exit(1);
  }

  const files = walk(ROOT).sort((a, b) => a.rel.localeCompare(b.rel));
  console.log(`publishing ${files.length} files to ${OWNER}/${REPO}@${BRANCH}`);

  let parentSha = null;
  const ref = await api("GET", `${repoPath}/git/ref/heads/${BRANCH}`);
  if (ref.status === 200) parentSha = ref.body.object.sha;
  else if (ref.status !== 404 && ref.status !== 409) {
    console.error(`GET ref -> ${ref.status}: ${ref.text.slice(0, 300)}`);
    process.exit(1);
  }

  const tree = [];
  for (const file of files) {
    let buffer = fs.readFileSync(file.full);
    const ext = path.extname(file.rel).toLowerCase();
    const textual = TEXT_EXT.has(ext) || path.basename(file.rel) === "Dockerfile" || !buffer.includes(0);
    if (textual && buffer.includes(Buffer.from("\r\n"))) {
      buffer = Buffer.from(buffer.toString("utf8").replace(/\r\n/g, "\n"), "utf8");
    }
    const blob = await api("POST", `${repoPath}/git/blobs`, {
      content: buffer.toString("base64"),
      encoding: "base64",
    });
    if (blob.status !== 201) {
      console.error(`blob ${file.rel} -> ${blob.status}: ${blob.text.slice(0, 200)}`);
      process.exit(1);
    }
    const mode = /^scripts\/.*\.sh$/.test(file.rel) ? "100755" : "100644";
    tree.push({ path: file.rel, mode, type: "blob", sha: blob.body.sha });
    process.stdout.write(".");
  }
  console.log("");

  const treeBody = { tree };
  if (parentSha) treeBody.base_tree = parentSha;
  const treeResponse = await api("POST", `${repoPath}/git/trees`, treeBody);
  if (treeResponse.status !== 201) {
    console.error(`tree -> ${treeResponse.status}: ${treeResponse.text.slice(0, 300)}`);
    process.exit(1);
  }

  const commit = await api("POST", `${repoPath}/git/commits`, {
    message: MESSAGE,
    tree: treeResponse.body.sha,
    parents: parentSha ? [parentSha] : [],
  });
  if (commit.status !== 201) {
    console.error(`commit -> ${commit.status}: ${commit.text.slice(0, 300)}`);
    process.exit(1);
  }

  if (parentSha) {
    const update = await api("PATCH", `${repoPath}/git/refs/heads/${BRANCH}`, {
      sha: commit.body.sha,
      force: false,
    });
    if (update.status !== 200) {
      console.error(`ref update -> ${update.status}: ${update.text.slice(0, 300)}`);
      process.exit(1);
    }
  } else {
    const create = await api("POST", `${repoPath}/git/refs`, {
      ref: `refs/heads/${BRANCH}`,
      sha: commit.body.sha,
    });
    if (create.status !== 201) {
      console.error(`ref create -> ${create.status}: ${create.text.slice(0, 300)}`);
      process.exit(1);
    }
  }

  console.log(`pushed ${commit.body.sha.slice(0, 12)} (${files.length} files, ${tree.length} blobs)`);
  console.log(`https://github.com/${OWNER}/${REPO}/commit/${commit.body.sha}`);
})().catch((error) => {
  console.error("error:", error.message);
  process.exit(1);
});
