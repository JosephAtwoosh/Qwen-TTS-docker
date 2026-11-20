#!/usr/bin/env node
"use strict";
/**
 * Minimal Vast.ai client for this image.
 *
 * Why Node: this laptop's Windows TLS stack breaks PowerShell/curl/git inside
 * the sandbox, while Node's OpenSSL works. No dependencies.
 *
 * Auth: VAST_API_KEY, or ~/.config/vastai/vast_api_key, ~/.vastai/vastai_api_key.
 *
 *   node tools/vast.js whoami
 *   node tools/vast.js instances
 *   node tools/vast.js search "RTX 4090,RTX 5090" 24000 60
 *   node tools/vast.js rent --offer 123456 --image ghcr.io/...:latest --disk 60 \
 *        --label qwen-tts --env TTS_API_KEY=... --port 8000:8000 --yes
 *   node tools/vast.js ssh 123456
 *   node tools/vast.js scp 123456 ./qwen-voices.tar.gz /root/
 *   node tools/vast.js destroy 123456 --yes
 *
 * `rent` spends money. It refuses without --yes.
 */
const https = require("https");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawnSync } = require("child_process");

const HOST = "console.vast.ai";

function findKey() {
  if (process.env.VAST_API_KEY) return { key: process.env.VAST_API_KEY.trim(), from: "env VAST_API_KEY" };
  const candidates = [
    path.join(os.homedir(), ".config", "vastai", "vast_api_key"),
    path.join(os.homedir(), ".vastai", "vastai_api_key"),
    path.join(process.env.APPDATA || "", "vastai", "vast_api_key"),
  ];
  for (const candidate of candidates) {
    try {
      const key = fs.readFileSync(candidate, "utf8").trim();
      if (key) return { key, from: candidate };
    } catch {
      /* next */
    }
  }
  return null;
}

function request(method, apiPath, body, key) {
  return new Promise((resolve, reject) => {
    const data = body === undefined ? null : JSON.stringify(body);
    const headers = { Accept: "application/json" };
    if (key) headers.Authorization = `Bearer ${key}`;
    if (data) {
      headers["Content-Type"] = "application/json";
      headers["Content-Length"] = Buffer.byteLength(data);
    }
    const req = https.request({ hostname: HOST, path: apiPath, method, headers, timeout: 45000 }, (res) => {
      let buffer = "";
      res.on("data", (chunk) => (buffer += chunk));
      res.on("end", () => {
        let parsed = buffer;
        try {
          parsed = JSON.parse(buffer);
        } catch {
          /* text */
        }
        resolve({ status: res.statusCode, body: parsed });
      });
    });
    req.on("error", reject);
    req.on("timeout", () => req.destroy(new Error("request timed out")));
    if (data) req.write(data);
    req.end();
  });
}

function needKey() {
  const found = findKey();
  if (!found) {
    console.error(
      "No Vast API key. Set VAST_API_KEY, run `vastai set api-key <key>`, " +
        "or write the key to ~/.config/vastai/vast_api_key",
    );
    process.exit(2);
  }
  return found;
}

function parseFlags(argv) {
  const flags = { env: [], _: [] };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--yes") flags.yes = true;
    else if (arg === "--port") flags.env.push(`-p ${argv[++i]}`);
    else if (arg === "--env") flags.env.push(argv[++i]);
    else if (arg === "--offer") flags.offer = argv[++i];
    else if (arg === "--image") flags.image = argv[++i];
    else if (arg === "--disk") flags.disk = Number(argv[++i]);
    else if (arg === "--label") flags.label = argv[++i];
    else if (arg === "--onstart") flags.onstart = argv[++i];
    else if (arg === "--name") flags.name = argv[++i];
    else if (arg === "--json") flags.json = argv[++i];
    else if (arg === "--max-inet") flags.maxInet = Number(argv[++i]);
    else if (arg === "--transfer-gb") flags.transferGb = Number(argv[++i]);
    else if (arg === "--allow-inet") flags.allowInet = true;
    else flags._.push(arg);
  }
  return flags;
}

function envMap(list) {
  const env = {};
  for (const item of list) {
    if (item.startsWith("-p ")) {
      const [key, value] = item.slice(3).split("=");
      env[`-p ${key}`] = value || "1";
      continue;
    }
    const index = item.indexOf("=");
    if (index < 1) continue;
    env[item.slice(0, index)] = item.slice(index + 1);
  }
  return env;
}

/** Vast returns both a $/GB field and an explicit $/TB field; prefer the latter. */
function perTb(offer, tbKey, gbKey) {
  if (typeof offer[tbKey] === "number") return offer[tbKey];
  return (offer[gbKey] || 0) * 1024;
}

async function offerById(id, key) {
  // Vast's bundle filter has no `id` predicate, so fetch the same pool the
  // `search` command uses and match locally.
  const query = {
    gpu_name: { in: ["RTX 4090", "RTX 5090", "A6000", "L40S"] },
    num_gpus: { gte: 1 },
    gpu_ram: { gte: 23000 },
    disk_space: { gte: 60 },
    rentable: { eq: true },
    verified: { eq: true },
    type: "ondemand",
    limit: 1000,
  };
  const r = await request("POST", "/api/v0/bundles/", query, key);
  if (r.status !== 200) return null;
  return (r.body.offers || []).find((o) => String(o.id) === String(id)) || null;
}

function scp(id, local, remote) {
  const key = needKey();
  request("GET", "/api/v1/instances/", undefined, key.key).then((r) => {
    const list = r.body.instances || r.body;
    const inst = (Array.isArray(list) ? list : []).find((i) => String(i.id) === String(id));
    if (!inst) {
      console.error(`no instance ${id}`);
      process.exit(1);
    }
    const host = inst.ssh_host || inst.public_ipaddr;
    const port = inst.ssh_port || 22;
    const result = spawnSync("scp", ["-P", String(port), local, `root@${host}:${remote}`], {
      stdio: ["ignore", "inherit", "inherit"],
    });
    process.exit(result.status === null ? 1 : result.status);
  });
}

async function main() {
  const [cmd, ...rest] = process.argv.slice(2);
  const flags = parseFlags(rest);

  switch (cmd) {
    case "whoami": {
      const key = needKey();
      const r = await request("GET", "/api/v0/users/current/", undefined, key.key);
      console.log("key source:", key.from);
      console.log("status:", r.status);
      console.log(
        JSON.stringify(
          { id: r.body.id, email: r.body.email, credit: r.body.credit, can_pay: r.body.can_pay },
          null,
          2,
        ),
      );
      break;
    }

    case "instances": {
      const key = needKey();
      const r = await request("GET", "/api/v1/instances/", undefined, key.key);
      const list = r.body.instances || r.body;
      if (!Array.isArray(list)) {
        console.log(JSON.stringify(r.body, null, 2).slice(0, 4000));
        break;
      }
      console.log(`status ${r.status}, ${list.length} instance(s)`);
      console.table(
        list.map((i) => ({
          id: i.id,
          gpu: `${i.num_gpus || 1}x ${i.gpu_name}`,
          status: i.actual_status || i.status,
          ssh: `${i.ssh_host || i.public_ipaddr || "?"}:${i.ssh_port || 22}`,
          dph: i.dph_total,
          label: i.label,
        })),
      );
      break;
    }

    case "search": {
      const names = (flags._[0] || "RTX 4090,RTX 5090,A6000").split(",").map((s) => s.trim());
      const minVram = Number(flags._[1] || 24000);
      const minDisk = Number(flags._[2] || 60);
      const query = {
        gpu_name: { in: names },
        num_gpus: { gte: 1 },
        gpu_ram: { gte: minVram },
        disk_space: { gte: minDisk },
        rentable: { eq: true },
        verified: { eq: true },
        type: "ondemand",
        order: [["dph_total", "asc"]],
        limit: 200,
      };
      const r = await request("POST", "/api/v0/bundles/", query, findKey()?.key);
      if (r.status !== 200) {
        console.error("status", r.status, JSON.stringify(r.body).slice(0, 500));
        process.exit(1);
      }
      const maxInet = flags.maxInet ?? 4;
      const offers = (r.body.offers || [])
        .map((o) => ({
          ...o,
          downTb: perTb(o, "internet_down_cost_per_tb", "inet_down_cost"),
          upTb: perTb(o, "internet_up_cost_per_tb", "inet_up_cost"),
        }))
        .filter((o) => o.downTb < maxInet && o.upTb < maxInet);
      if (!offers.length) {
        console.error(`No offers under $${maxInet}/TB both ways. Raise with --max-inet.`);
        process.exit(1);
      }
      console.table(
        offers.slice(0, 15).map((o) => ({
          offer: o.id,
          gpu: `${o.num_gpus}x ${o.gpu_name}`,
          vram_gb: Math.round(o.gpu_total_ram / 1024),
          ram_gb: Math.round(o.cpu_ram / 1024),
          disk_gb: Math.round(o.disk_space),
          down_tb: Number(o.downTb.toFixed(2)),
          up_tb: Number(o.upTb.toFixed(2)),
          xfer10_usd: Number(((10 / 1024) * o.downTb + (1 / 1024) * o.upTb).toFixed(3)),
          mbps: `${Math.round(o.inet_down)}/${Math.round(o.inet_up)}`,
          driver: o.driver_version,
          usd_hr: Number(o.dph_total.toFixed(3)),
        })),
      );
      console.log(`\nFiltered to < $${maxInet}/TB in both directions. Driver must be >= 525 (CUDA 12.x).`);
      break;
    }

    case "offer": {
      const key = needKey();
      const o = await offerById(flags._[0], key.key);
      if (!o) {
        console.error(`no offer ${flags._[0]}`);
        process.exit(1);
      }
      console.log(
        JSON.stringify(
          {
            id: o.id,
            gpu: o.gpu_name,
            vram_gb: Math.round(o.gpu_total_ram / 1024),
            dph: o.dph_total,
            down_tb: perTb(o, "internet_down_cost_per_tb", "inet_down_cost"),
            up_tb: perTb(o, "internet_up_cost_per_tb", "inet_up_cost"),
            down_mbps: o.inet_down,
            up_mbps: o.inet_up,
            driver: o.driver_version,
            geo: o.geolocation,
          },
          null,
          2,
        ),
      );
      break;
    }

    case "rent": {
      const key = needKey();
      if (!flags.offer || !flags.image) {
        console.error("rent needs --offer <id> and --image <ghcr image>");
        process.exit(2);
      }
      if (!flags.yes) {
        console.error("Refusing to spend money without --yes");
        process.exit(2);
      }
      const maxInet = flags.maxInet ?? 4;
      const offer = await offerById(flags.offer, key.key);
      if (!offer) {
        console.error(
          `Could not read offer ${flags.offer}. Run \`search\` and pass --allow-inet ` +
            "only if you have checked the transfer rate yourself.",
        );
        process.exit(2);
      }
      const downTb = perTb(offer, "internet_down_cost_per_tb", "inet_down_cost");
      const upTb = perTb(offer, "internet_up_cost_per_tb", "inet_up_cost");
      console.log(
        `offer ${flags.offer}: $${Number(offer.dph_total).toFixed(3)}/h, ` +
          `transfer down $${downTb.toFixed(2)}/TB up $${upTb.toFixed(2)}/TB`,
      );
      const transferGb = flags.transferGb ?? 10;
      console.log(
        `projected transfer (${transferGb} GB down + 1 GB up): ` +
          `$${(((transferGb / 1024) * downTb) + ((1 / 1024) * upTb)).toFixed(3)}`,
      );
      if (!flags.allowInet && (downTb >= maxInet || upTb >= maxInet)) {
        console.error(
          `Refusing: transfer is not under $${maxInet}/TB both ways ` +
            `(down $${downTb.toFixed(2)}, up $${upTb.toFixed(2)}). ` +
            "Pick another offer, or override with --allow-inet.",
        );
        process.exit(2);
      }
      const body = {
        client_id: "me",
        image: flags.image,
        disk: flags.disk || 60,
        runtype: "ssh",
        label: flags.label || "qwen-tts",
        env: envMap(flags.env),
      };
      if (flags.onstart) body.onstart = flags.onstart;
      const r = await request("PUT", `/api/v0/asks/${flags.offer}/`, body, key.key);
      console.log("status:", r.status);
      console.log(JSON.stringify(r.body, null, 2).slice(0, 1200));
      if (r.status >= 300) process.exit(1);
      console.log("\nNext: node tools/vast.js instances   (wait for 'running', then scp the voices pack)");
      break;
    }

    case "template": {
      const key = needKey();
      if (!flags.name || !flags.image) {
        console.error("template needs --name and --image");
        process.exit(2);
      }
      const body = {
        name: flags.name,
        image: flags.image,
        disk_space: flags.disk || 60,
        runtype: "ssh",
        env: envMap(flags.env),
      };
      if (flags.onstart) body.onstart = flags.onstart;
      const r = await request("POST", "/api/v0/template/", body, key.key);
      console.log("status:", r.status);
      console.log(JSON.stringify(r.body, null, 2).slice(0, 1200));
      if (r.status >= 300) {
        console.error("Template API refused; use tools/vast-template.json in the Vast web UI instead.");
        process.exit(1);
      }
      break;
    }

    case "ssh": {
      const key = needKey();
      const r = await request("GET", "/api/v1/instances/", undefined, key.key);
      const list = r.body.instances || r.body;
      const inst = (Array.isArray(list) ? list : []).find((i) => String(i.id) === String(flags._[0]));
      if (!inst) {
        console.error(`no instance ${flags._[0]}`);
        process.exit(1);
      }
      const host = inst.ssh_host || inst.public_ipaddr;
      const port = inst.ssh_port || 22;
      console.log(`ssh -p ${port} root@${host}`);
      const result = spawnSync("ssh", ["-p", String(port), `root@${host}`], {
        stdio: ["inherit", "inherit", "inherit"],
      });
      process.exit(result.status === null ? 1 : result.status);
    }

    case "scp": {
      const [id, local, remote] = flags._;
      if (!id || !local || !remote) {
        console.error("scp <id> <local> <remote>");
        process.exit(2);
      }
      scp(id, local, remote);
      break;
    }

    case "stop":
    case "start": {
      const key = needKey();
      const id = flags._[0];
      if (!id) {
        console.error(`${cmd} <instance_id>`);
        process.exit(2);
      }
      const state = cmd === "stop" ? "stopped" : "running";
      const r = await request("PUT", `/api/v0/instances/${id}/`, { state }, key.key);
      console.log(`${cmd} ${id} -> ${r.status} ${JSON.stringify(r.body).slice(0, 300)}`);
      break;
    }

    case "destroy": {
      const key = needKey();
      const id = flags._[0];
      if (!id || !flags.yes) {
        console.error("Refusing to destroy without an instance id and --yes");
        process.exit(2);
      }
      const r = await request("DELETE", `/api/v0/instances/${id}/`, undefined, key.key);
      console.log("status:", r.status, JSON.stringify(r.body).slice(0, 500));
      break;
    }

    default:
      console.error(
        "usage: node tools/vast.js {whoami|instances|search|rent|template|ssh|scp|destroy} [...]\n" +
          "  search 'RTX 4090,RTX 5090' 24000 60\n" +
          "  rent --offer 123 --image ghcr.io/... --env TTS_API_KEY=... --port 8000:8000 --yes",
      );
      process.exit(2);
  }
}

main().catch((err) => {
  console.error("error:", err.message);
  process.exit(1);
});
