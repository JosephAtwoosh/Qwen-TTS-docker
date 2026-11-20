# Running on Vast.ai

The image is built on GitHub and pulled by the instance; Vast does not allow
`docker build` inside an instance. The flow is:

1. Push to `main` → GitHub Actions builds `ghcr.io/josephatwoosh/qwen-tts-docker`.
2. Make the GHCR package **public** (or give Vast a registry token).
3. Create a Vast template from [../tools/vast-template.json](../tools/vast-template.json).
4. Rent a GPU with that template.
5. First boot: fetch the 4 GB checkpoint, unpack the voices, start the API.
6. Point the studio at `http://<host>:<port>` and keep the instance for the
   session; destroy it when the soundtrack is rendered.

---

## 1. The image

The workflow pushes two tags: `latest` and `sha-<7>`.

```bash
docker pull ghcr.io/josephatwoosh/qwen-tts-docker:latest
```

**Make it public.** GitHub Container Registry packages are private by default,
even when the source repository is public. On the package page:
*Package settings → Danger Zone → Change visibility → Public*. Without this,
Vast's pull gets `unauthorized` and the instance sits in `creating`.

If you must keep it private, create a Vast registry credential for
`ghcr.io` with a GitHub token that can read packages, and attach it to the
template. That is more setup than most runs justify.

### Transfer cost

GPU price is not the whole price. Vast hosts bill ingress/egress separately,
and the spread is huge: the first host this project rented was **~$16/TB down,
~$17/TB up**; the one it settled on was **$1.33/TB down, $2.67/TB up**. Pulling
the 4.8 GB image and 4 GB of weights is ~9 GB, so the difference between those
hosts is roughly $0.30 per boot — small, but it compounds and the user's rule
here is **under $4/TB in both directions**.

`tools/vast.js search` filters on `internet_down_cost_per_tb` and
`internet_up_cost_per_tb` and shows both columns. `rent` re-checks the offer and
refuses anything at or above `--max-inet` (default 4) unless `--allow-inet` is
passed. Do not bypass it without reading the two columns.

## 2. The template

**Vast's launcher (`/.launch`) replaces the image entrypoint.** The image's
`CMD ["serve"]` does not run on a Vast instance; without an on-start script the
container sits there with SSH only and `/health` is unreachable. The on-start
script is mandatory, and Vast writes it to `/app/onstart.sh`.

Fields in the Vast UI (*Templates → New Template*):

| Field | Value |
|---|---|
| Name | `qwen-tts-1.7b` |
| Image | `ghcr.io/josephatwoosh/qwen-tts-docker:latest` |
| Disk | `60` GB (weights 4 GB + outputs + HF cache) |
| Run type | `SSH` |
| Ports | `8000` |
| Env `-p 8000:8000` | `1` (this is how Vast publishes a port) |
| Env `TTS_API_KEY` | a long random string, **not** the placeholder |
| Env `ENGINE` | `qwen` |
| Env `PRELOAD_MODEL` | `1` |
| On-start | see below |

On-start script (idempotent: downloads once, then always serves):

```bash
bash /app/scripts/vast-onstart.sh
```

That script refuses to expose an open API when `TTS_API_KEY` is empty,
downloads the checkpoint if `/models` is empty, logs a doctor preflight, then
starts the service. Set `ALLOW_OPEN=1` only for a private debug instance.

`/models`, `/voices` and `/output` are declared volumes. On a Vast instance
they live on the instance's disk, which survives a stop but **not** a destroy.

The machine-readable template is [../tools/vast-template.json](../tools/vast-template.json);
`node tools/vast.js template --name qwen-tts-1.7b --image ... --env ...` tries
the API, but the UI is the supported path if the API shape has moved.

## 3. Rent

```bash
# 24 GB cards, 60 GB disk, cheapest first
node tools/vast.js search "RTX 4090,RTX 5090,A6000,L40S" 24000 60

node tools/vast.js rent \
  --offer 123456 \
  --image ghcr.io/josephatwoosh/qwen-tts-docker:latest \
  --disk 60 \
  --label qwen-tts \
  --max-inet 4 \
  --env TTS_API_KEY=replace-with-a-long-random-string \
  --port 8000:8000 \
  --yes
```

`rent` spends money and refuses without `--yes`; it also refuses an offer whose
transfer rate is not under `--max-inet` USD/TB both ways. Check the offer's
driver column: it must be ≥ 525 for the CUDA 12.x image. Prefer verified hosts.

A 4090 at roughly $0.30–0.45/h is the sweet spot. The 1.7B model does not need
an A100; 24 GB is plenty.

## 4. First boot

```bash
node tools/vast.js instances          # wait for actual_status: running
node tools/vast.js ssh <id>
```

Inside the instance:

```bash
qwen-tts-entrypoint doctor
```

`doctor` reports the GPU, VRAM, torch/CUDA, weights, voice count and API key.
If the on-start script is still downloading, `doctor` fails on the weights;
wait and re-run.

**Environment quirk:** Vast injects the template env into PID 1, but SSH
sessions start without it. So `env | grep TTS` in an SSH shell can be empty
while the service started by on-start has the key. Check the real container env
with:

```bash
tr '\0' '\n' < /proc/1/environ | grep -E 'TTS_API_KEY|PRELOAD_MODEL'
```

If you start the service by hand, export `TTS_API_KEY` in that same command or
it will run open.

### GPU sampling

`serve` starts `scripts/gpu-probe.sh` automatically. It appends one `nvidia-smi`
row every `GPU_PROBE_INTERVAL` seconds (default **120**) to
`/output/gpu-probe.csv`:

```text
timestamp,name,util_pct,mem_used_mib,mem_total_mib,temp_c,power_w
2026/11/20 15:14:02.123,NVIDIA GeForce RTX 4090,97,5120,24564,61,318.4
```

It is independent of the SSH session, so it keeps sampling through the render.
Set `GPU_PROBE=0` to disable, or `GPU_PROBE_INTERVAL=60` for finer detail.

`serve` also starts `scripts/gpu_watchdog.py`, which writes one JSON object per
interval to `/output/gpu-watchdog.jsonl` with the GPU line, server process
count, `/health` code, the latest job id/status/age and the model-call count
(`batches`). Set `GPU_WATCHDOG=0` to disable. This is the file to read after a
pipe failure: it shows whether the job kept advancing while the SSH session was
blocked.

## 5. Voices

The repository does not contain voice references. Two ways to get the 29
imported voices onto the instance:

**A. Upload the pack (recommended).** Build it on the laptop once:

```bash
node tools/import_voices.js --voices-dir ./voices --category generated --pack
node tools/vast.js scp <id> ./qwen-voices.tar.gz /root/
```

Then on the instance:

```bash
tar -xzf /root/qwen-voices.tar.gz -C /      # pack contains voices/...
ls /voices | head
```

**B. Import on the instance.** Put `ELEVENLABS_API_KEY` (and, if the key lacks
`speech_to_text`, `OPENROUTER_API_KEY`) in the template env, then:

```bash
qwen-tts-entrypoint import-voices --category generated --transcribe auto
```

That costs a few cents of transcription and re-downloads the previews.

Restart the service after adding voices if it was already running, or call
`GET /v1/voices` to see whether the registry picked them up (it reloads only on
process start; restarting is the reliable path).

## 6. Reach the API

Vast maps the container port to a host port; the instance page shows it under
*Open Ports* (`8000/tcp → <host>:<port>`).

```bash
curl -s http://<host>:<port>/health | jq
curl -s -H "Authorization: Bearer $TTS_API_KEY" http://<host>:<port>/v1/voices | jq .
```

The service has no HTTPS. Put it behind a tunnel or restrict the Vast firewall
if you are not on a trusted network; the Bearer key is the only gate.

## 7. Cost and teardown

- Model download ~4 GB: a few minutes and a little egress.
- The GPU is billed while the instance runs, including idle time. Stop it when
  you are not generating; stop keeps the disk (and the voice pack), destroy
  removes both.
- `node tools/vast.js destroy <id> --yes` is irreversible.

Typical session: rent → download (~5 min) → render a scene (~minutes) → pull
the WAVs → destroy. At 4090 prices this is cents to a couple of dollars, not
an hourly commitment.

## Troubleshooting

**Instance stuck in `creating`.** The image pull is failing. Check the package
visibility first; then the Vast instance log. A private GHCR package without a
credential is the usual cause.

**`CUDA driver version is insufficient`.** The host driver is older than the
CUDA 12.8 user-space requirement (minor version compatibility still needs
525+). Pick another offer.

**Port 8000 unreachable.** The `-p 8000:8000` env entry is easy to miss; it is
what tells Vast to publish the port. Re-check the template env, not just the
`Ports` field.

**`/health` degrades after a restart.** The on-start download runs only when
`model.safetensors` is missing. If the disk was destroyed, it downloads again;
if the disk is full, it fails — check `df -h`.
