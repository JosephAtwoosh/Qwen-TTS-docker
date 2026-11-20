#!/usr/bin/env bash
# Host-side wrapper around docker compose.
#
#   ./scripts/run.sh build            build the image
#   ./scripts/run.sh download         fetch the ~4 GB checkpoint
#   ./scripts/run.sh up               start the service in the background
#   ./scripts/run.sh serve            run it in the foreground
#   ./scripts/run.sh doctor           preflight inside the image
#   ./scripts/run.sh import           import ElevenLabs voices into ./voices
#   ./scripts/run.sh test             run the test suite inside the image
#   ./scripts/run.sh logs             follow logs
#   ./scripts/run.sh down             stop
set -euo pipefail
cd "$(dirname "$0")/.."

cmd="${1:-up}"
shift || true

case "$cmd" in
  build)    docker compose build "$@" ;;
  download) docker compose --profile download run --rm downloader ;;
  up)       docker compose up -d "$@" ;;
  serve)    docker compose run --rm --service-ports qwen-tts serve ;;
  doctor)   docker compose run --rm qwen-tts doctor ;;
  import)   docker compose run --rm qwen-tts import-voices "$@" ;;
  test)     docker compose run --rm qwen-tts test ;;
  logs)     docker compose logs -f "$@" ;;
  down)     docker compose down "$@" ;;
  *)
    echo "usage: $0 {build|download|up|serve|doctor|import|test|logs|down}" >&2
    exit 2
    ;;
esac
