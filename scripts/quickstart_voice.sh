#!/usr/bin/env bash
# quickstart_voice.sh — set up a local venv, install deps, verify the
# host .env has VOICE_SECRET + OPENAI_API_KEY, launch the LATTICE voice
# node in the background, and print the inspector URL.
#
# Voice runs on whichever host has it declared in manifest.yaml (currently
# coltons-mac). The script auto-picks the matching .env under hosts/.
#
# Re-runnable: every step is idempotent. The script does NOT restart Core
# — Core (on the Mac mini) must already know VOICE_SECRET in its own env.
# If you just added it, restart Core on the mini before running this.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${REPO_ROOT}/nodes/voice/.venv"
LOG_FILE="${VOICE_LOG_FILE:-/tmp/lattice-voice.log}"

# 1) Pick the right host .env. Prefer coltons-mac (voice host); fall back
#    to mac-mini for legacy setups.
ENV_FILE=""
for candidate in \
  "${REPO_ROOT}/hosts/coltons-mac/.env" \
  "${REPO_ROOT}/hosts/mac-mini/.env"; do
  if [[ -f "${candidate}" ]]; then
    ENV_FILE="${candidate}"
    break
  fi
done

if [[ -z "${ENV_FILE}" ]]; then
  echo "[voice] no host .env found under hosts/. Run scripts/bootstrap.sh first." >&2
  exit 1
fi
echo "[voice] using env: ${ENV_FILE}"

# 2) Verify required keys are present.
require_key() {
  local key="$1"
  if ! grep -q "^${key}=" "${ENV_FILE}"; then
    echo "[voice] ${ENV_FILE} is missing ${key}." >&2
    return 1
  fi
  local val
  val="$(grep "^${key}=" "${ENV_FILE}" | head -1 | cut -d= -f2-)"
  if [[ -z "${val}" ]]; then
    echo "[voice] ${ENV_FILE} has empty ${key}." >&2
    return 1
  fi
}

missing=()
require_key VOICE_SECRET || missing+=("VOICE_SECRET")
require_key OPENAI_API_KEY || missing+=("OPENAI_API_KEY")
require_key CORE_URL || missing+=("CORE_URL")
if [[ ${#missing[@]} -gt 0 ]]; then
  echo "[voice] add the missing key(s) to ${ENV_FILE} and re-run." >&2
  echo "[voice] VOICE_SECRET must match the value in mac-mini/.env (so Core can verify signatures)." >&2
  exit 1
fi

# 3) Create / activate venv.
if [[ ! -d "${VENV_DIR}" ]]; then
  echo "[voice] creating venv at ${VENV_DIR}…"
  python3 -m venv "${VENV_DIR}"
fi

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
echo "[voice] venv active: $(which python)"

# 4) Install deps into the venv.
echo "[voice] installing python deps into venv (idempotent)…"
pip install --quiet --upgrade pip
pip install --quiet \
  'openai>=1.50.0' \
  'sounddevice>=0.4.6' \
  'numpy>=1.24.0' \
  'aiohttp>=3.9.0' \
  'pyyaml>=6.0' \
  || { echo "[voice] pip install failed" >&2; exit 1; }

# 5) Launch start.sh in background with venv's python on PATH.
if pgrep -f 'voice/voice\.py' >/dev/null 2>&1; then
  echo "[voice] already running. Tail with: tail -f ${LOG_FILE}"
else
  cd "${REPO_ROOT}/nodes/voice"
  # Inherit current PATH so start.sh's `python3` resolves to the venv.
  nohup ./start.sh > "${LOG_FILE}" 2>&1 &
  echo "[voice] started PID $! (log: ${LOG_FILE})"
fi

echo
echo "Inspector: http://127.0.0.1:8807"
echo "Logs:      tail -f ${LOG_FILE}"
