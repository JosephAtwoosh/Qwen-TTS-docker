#!/usr/bin/env bash
# Container entrypoint. The first argument is a subcommand; anything else is
# executed directly, so `docker run --rm image bash` still works.
set -euo pipefail

cmd="${1:-serve}"
case "$cmd" in
  serve)
    exec python -m app.server
    ;;
  import-voices)
    shift
    exec node /app/tools/import_voices.js --voices-dir "${VOICES_DIR:-/voices}" "$@"
    ;;
  download-models)
    exec /app/scripts/download-models.sh
    ;;
  doctor)
    exec /app/scripts/doctor.sh
    ;;
  test)
    exec python -m pytest -q /app/tests
    ;;
  shell)
    exec bash
    ;;
  *)
    exec "$@"
    ;;
esac
