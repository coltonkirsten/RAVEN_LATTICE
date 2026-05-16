#!/usr/bin/env bash
# Start the LATTICE voice node (gpt-realtime-2).
# Runs natively (no Docker) so it can reach PortAudio mic/speakers.
# Voice node host is coltons-mac; falls back to mac-mini for legacy setups.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LATTICE_ROOT="$(cd "$HERE/../.." && pwd)"

ENV_FILE=""
for candidate in \
  "$LATTICE_ROOT/hosts/coltons-mac/.env" \
  "$LATTICE_ROOT/hosts/mac-mini/.env"; do
  if [[ -f "$candidate" ]]; then
    ENV_FILE="$candidate"
    break
  fi
done

if [[ -z "$ENV_FILE" ]]; then
  echo "[voice] no host .env found under hosts/. Run scripts/bootstrap.sh first." >&2
  exit 1
fi

VOICE_SECRET="$(grep '^VOICE_SECRET=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
CORE_URL="$(grep '^CORE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2- || echo 'http://127.0.0.1:8000')"
OPENAI_API_KEY="$(grep '^OPENAI_API_KEY=' "$ENV_FILE" | head -1 | cut -d= -f2- || echo '')"

if [[ -z "${VOICE_SECRET}" ]]; then
  echo "[voice] VOICE_SECRET is empty in $ENV_FILE. Run scripts/quickstart_voice.sh or set it manually." >&2
  exit 1
fi

export VOICE_SECRET CORE_URL OPENAI_API_KEY

cd "$HERE"
# python3 resolves to the venv interpreter when this script is launched
# from an active venv (recommended path via quickstart_voice.sh).
exec python3 -u voice.py
