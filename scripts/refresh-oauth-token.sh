#!/usr/bin/env bash
# refresh-oauth-token.sh — pull the current Claude Code OAuth access token
# from the macOS keychain and write it into hosts/mac-mini/.env so EDITH's
# next start picks it up.
#
# This script is host-only — it reads from macOS keychain, which is not
# accessible from inside the EDITH Docker container.
#
# Run it whenever you've re-authed Claude Code (or on a periodic cron) to
# keep EDITH on a fresh token. Restart the container afterward to apply.
#
# Usage:
#   ./scripts/refresh-oauth-token.sh           # update .env, don't restart
#   ./scripts/refresh-oauth-token.sh --restart # update .env + restart container

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${REPO_ROOT}/hosts/mac-mini/.env"

RESTART=false
for arg in "$@"; do
  case "$arg" in
    --restart) RESTART=true ;;
    -h|--help)
      sed -n '2,17p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown arg: $arg"; exit 2 ;;
  esac
done

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "ERROR: ${ENV_FILE} not found. Run scripts/bootstrap.sh first." >&2
  exit 1
fi

if ! command -v security >/dev/null 2>&1; then
  echo "ERROR: 'security' command not found (this script is macOS-only)." >&2
  exit 1
fi

# Pull token from keychain. The credential blob is JSON shaped like:
#   {"claudeAiOauth":{"accessToken":"sk-ant-oat01-...", ...}}
TOKEN=$(security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["claudeAiOauth"]["accessToken"])')

if [[ -z "${TOKEN}" ]]; then
  echo "ERROR: could not extract OAuth token from keychain." >&2
  echo "  - Confirm Claude Code is installed and you are logged in" >&2
  echo "  - Try: security find-generic-password -s 'Claude Code-credentials' -w" >&2
  exit 1
fi

# Patch (or append) CLAUDE_CODE_OAUTH_TOKEN in the env file.
python3 - "$ENV_FILE" "$TOKEN" <<'PY'
import pathlib, re, sys
p = pathlib.Path(sys.argv[1])
token = sys.argv[2]
text = p.read_text()
line = f"CLAUDE_CODE_OAUTH_TOKEN={token}"
if re.search(r'^CLAUDE_CODE_OAUTH_TOKEN=', text, flags=re.M):
    text = re.sub(r'^CLAUDE_CODE_OAUTH_TOKEN=.*$', line, text, flags=re.M)
else:
    if not text.endswith("\n"):
        text += "\n"
    text += line + "\n"
p.write_text(text)
PY

echo "[ok] CLAUDE_CODE_OAUTH_TOKEN written to ${ENV_FILE} (prefix: ${TOKEN:0:18}...)"

if $RESTART; then
  EDITH_DIR="${REPO_ROOT}/nodes/edith"
  if [[ ! -d "${EDITH_DIR}" ]]; then
    echo "WARN: ${EDITH_DIR} not found — skipping restart." >&2
    exit 0
  fi
  echo "Restarting EDITH container..."
  EDITH_SECRET=$(grep '^EDITH_SECRET=' "${ENV_FILE}" | cut -d= -f2-)
  CORE_URL=$(grep '^CORE_URL=' "${ENV_FILE}" | cut -d= -f2- || echo "http://100.109.10.50:8000")
  ANTHROPIC_API_KEY=$(grep '^ANTHROPIC_API_KEY=' "${ENV_FILE}" | cut -d= -f2- || echo "")
  cd "${EDITH_DIR}"
  CLAUDE_CODE_OAUTH_TOKEN="${TOKEN}" \
    EDITH_SECRET="${EDITH_SECRET}" \
    CORE_URL="${CORE_URL}" \
    ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY}" \
    docker compose up -d --force-recreate
  echo "[ok] EDITH restarted."
fi
