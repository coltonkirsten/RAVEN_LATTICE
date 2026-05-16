#!/usr/bin/env bash
# Start the LATTICE voice node (gpt-realtime-2).
# Runs natively on the Mac mini so it can reach PortAudio mic/speakers.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LATTICE_ROOT="$(cd "$HERE/../.." && pwd)"

ENV_FILE="$LATTICE_ROOT/hosts/mac-mini/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "[voice] $ENV_FILE not found. Run scripts/bootstrap.sh first." >&2
  exit 1
fi

VOICE_SECRET="$(grep '^VOICE_SECRET=' "$ENV_FILE" | cut -d= -f2-)"
CORE_URL="$(grep '^CORE_URL=' "$ENV_FILE" | cut -d= -f2- || echo 'http://127.0.0.1:8000')"
OPENAI_API_KEY="$(grep '^OPENAI_API_KEY=' "$ENV_FILE" | cut -d= -f2- || echo '')"

if [[ -z "${VOICE_SECRET}" ]]; then
  echo "[voice] VOICE_SECRET is empty. Run scripts/quickstart_voice.sh or set it manually." >&2
  exit 1
fi

export VOICE_SECRET CORE_URL OPENAI_API_KEY

cd "$HERE"
exec python3 -u voice.py
