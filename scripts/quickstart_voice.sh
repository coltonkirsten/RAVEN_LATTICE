#!/usr/bin/env bash
# quickstart_voice.sh — install deps, ensure VOICE_SECRET is in .env,
# launch the LATTICE voice node in the background, and print the
# inspector URL.
#
# Re-runnable: every step is idempotent. The script does NOT restart
# Core — Core picks up VOICE_SECRET from env at startup, so if VOICE_SECRET
# was added to .env after Core booted, you must restart Core separately
# (otherwise Core will autogen a different secret and reject voice's
# signed envelopes with `bad_signature`).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${REPO_ROOT}/hosts/mac-mini/.env"
LOG_FILE="${VOICE_LOG_FILE:-/tmp/lattice-voice.log}"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "[voice] ${ENV_FILE} not found. Run scripts/bootstrap.sh first." >&2
  exit 1
fi

# 1) deps
echo "[voice] installing python deps (--break-system-packages, idempotent)…"
pip3 install --break-system-packages --quiet \
  'openai>=1.50.0' \
  'sounddevice>=0.4.6' \
  'numpy>=1.24.0' \
  'aiohttp>=3.9.0' \
  'pyyaml>=6.0' \
  || { echo "[voice] pip install failed" >&2; exit 1; }

# 2) VOICE_SECRET in .env (generate if missing)
if grep -q '^VOICE_SECRET=' "${ENV_FILE}"; then
  current="$(grep '^VOICE_SECRET=' "${ENV_FILE}" | cut -d= -f2-)"
  if [[ -z "${current}" ]]; then
    new="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
    # macOS-portable in-place edit.
    /usr/bin/sed -i '' "s/^VOICE_SECRET=.*/VOICE_SECRET=${new}/" "${ENV_FILE}"
    echo "[voice] populated empty VOICE_SECRET=${new:0:6}…"
  else
    echo "[voice] VOICE_SECRET already present (${current:0:6}…)"
  fi
else
  new="$(python3 -c 'import secrets; print(secrets.token_hex(16))')"
  printf '\nVOICE_SECRET=%s\n' "${new}" >> "${ENV_FILE}"
  echo "[voice] appended VOICE_SECRET=${new:0:6}…"
fi

# 3) Reminder about Core
if pgrep -f 'core\.core .*manifest.yaml' >/dev/null 2>&1; then
  echo "[voice] note: Core is running. If VOICE_SECRET was just added,"
  echo "       restart Core so it picks it up:"
  echo "         pkill -INT -f 'core\\.core .*manifest.yaml' && hosts/mac-mini/start-core.sh"
fi

# 4) Launch start.sh in background
if pgrep -f 'voice/voice\.py' >/dev/null 2>&1; then
  echo "[voice] already running. Tail with: tail -f ${LOG_FILE}"
else
  cd "${REPO_ROOT}/nodes/voice"
  nohup ./start.sh > "${LOG_FILE}" 2>&1 &
  echo "[voice] started PID $! (log: ${LOG_FILE})"
fi

echo
echo "Inspector: http://127.0.0.1:8807"
echo "Logs:      tail -f ${LOG_FILE}"
