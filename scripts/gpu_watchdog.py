#!/usr/bin/env python3
"""One JSONL row every WATCH_INTERVAL seconds: GPU, service, job progress.

Runs inside the container, independent of the SSH session. `batches` counts
"pad_token_id" lines in the service log, which is one line per model generate
call; it gives a usable progress signal for a long soundtrack job.
"""
import json
import os
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

INTERVAL = int(os.environ.get("WATCH_INTERVAL", "120"))
OUT = Path(os.environ.get("WATCH_OUT", "/output/gpu-watchdog.jsonl"))
LOG = Path("/var/log/qwen-tts.log")


def sh(args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def health_code():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=3) as res:
            return res.status
    except Exception as exc:
        return getattr(exc, "code", 0) or 0


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    while True:
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        gpu = sh(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                "--format=csv,noheader,nounits",
            ]
        ).replace(" ", "")
        procs = len(sh(["pgrep", "-f", "python -m app.server"]).splitlines())
        job = ""
        status = ""
        age = 0
        dirs = sorted(
            (p for p in Path("/output").glob("2026*") if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if dirs:
            job = dirs[0].name
            try:
                state = json.loads((dirs[0] / "status.json").read_text())
                status = str(state.get("status", ""))
                created = state.get("created_at")
                if created:
                    age = int(
                        (datetime.now(timezone.utc) - datetime.fromisoformat(created)).total_seconds()
                    )
            except Exception:
                pass
        batches = 0
        try:
            batches = LOG.read_text(errors="ignore").count("pad_token_id")
        except Exception:
            pass
        row = {
            "ts": stamp,
            "gpu": gpu,
            "server_procs": procs,
            "health": health_code(),
            "job": job,
            "status": status,
            "age_s": age,
            "batches": batches,
        }
        with OUT.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
