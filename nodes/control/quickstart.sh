#!/usr/bin/env bash
# quickstart.sh — one-shot boot for the LATTICE control panel.
#
# Run this from a fresh clone on Colton's Mac. It will:
#   1. Verify prerequisites (node, npm, python3)
#   2. Run scripts/bootstrap.sh if hosts/coltons-mac/.env is missing
#   3. Install npm deps if node_modules is missing
#   4. Source hosts/coltons-mac/.env
#   5. Probe Core's /v0/introspect to confirm reachability
#   6. Start the control server in the foreground (Ctrl-C to stop)
#
# Idempotent — safe to re-run. Will not regenerate the secret on subsequent runs.
#
# Usage:
#   cd nodes/control
#   ./quickstart.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_FILE="${REPO_ROOT}/hosts/coltons-mac/.env"

c_red()  { printf '\033[31m%s\033[0m\n' "$*"; }
c_grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
c_yel()  { printf '\033[33m%s\033[0m\n' "$*"; }
c_dim()  { printf '\033[2m%s\033[0m\n' "$*"; }

step() { printf '\n\033[1m▸ %s\033[0m\n' "$*"; }

# -----------------------------------------------------------------------------
# 1. Prerequisites
# -----------------------------------------------------------------------------
step "Checking prerequisites"

missing=()
for cmd in node npm python3; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    missing+=("$cmd")
  fi
done

if [[ ${#missing[@]} -gt 0 ]]; then
  c_red "Missing required commands: ${missing[*]}"
  c_dim "Install with: brew install node python3"
  exit 1
fi

NODE_MAJOR=$(node -p "process.versions.node.split('.')[0]")
if [[ "${NODE_MAJOR}" -lt 18 ]]; then
  c_red "Node ${NODE_MAJOR} detected — control panel requires Node 18+."
  c_dim "Upgrade with: brew upgrade node"
  exit 1
fi

c_grn "  ✓ node $(node -v), npm $(npm -v), python3 $(python3 --version | awk '{print $2}')"

# -----------------------------------------------------------------------------
# 2. Bootstrap .env if missing
# -----------------------------------------------------------------------------
step "Checking host env"

if [[ ! -f "${ENV_FILE}" ]]; then
  c_yel "  ${ENV_FILE} missing — running scripts/bootstrap.sh"
  bash "${REPO_ROOT}/scripts/bootstrap.sh"
else
  c_grn "  ✓ ${ENV_FILE} exists"
fi

# -----------------------------------------------------------------------------
# 3. Install npm deps
# -----------------------------------------------------------------------------
step "Checking npm dependencies"

if [[ ! -d "${SCRIPT_DIR}/node_modules" ]]; then
  c_yel "  node_modules missing — running npm install"
  ( cd "${SCRIPT_DIR}" && npm install --silent )
  c_grn "  ✓ deps installed"
else
  c_grn "  ✓ node_modules present"
fi

# -----------------------------------------------------------------------------
# 4. Source env
# -----------------------------------------------------------------------------
step "Loading env"

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

if [[ -z "${CONTROL_SECRET:-}" ]]; then
  c_red "  CONTROL_SECRET is empty in ${ENV_FILE}"
  c_dim "  Re-run scripts/bootstrap.sh after deleting the .env to regenerate."
  exit 1
fi

CORE_URL="${CORE_URL:-http://100.109.10.50:8000}"
PORT="${PORT:-5190}"

c_grn "  ✓ CONTROL_SECRET loaded (${#CONTROL_SECRET} chars)"
c_grn "  ✓ CORE_URL=${CORE_URL}"
c_grn "  ✓ PORT=${PORT}"

# -----------------------------------------------------------------------------
# 5. Probe Core
# -----------------------------------------------------------------------------
step "Probing Core at ${CORE_URL}"

if curl -sf --max-time 5 "${CORE_URL}/v0/introspect" >/dev/null; then
  c_grn "  ✓ Core reachable"
else
  c_red "  ✗ Core not reachable at ${CORE_URL}/v0/introspect"
  c_dim "  - Confirm Mac mini is online and Core process is running"
  c_dim "  - Confirm Tailscale is up on this machine (\`tailscale status\`)"
  c_dim "  - You can still launch the control panel; topology will be empty until Core is up."
  c_yel "  Continuing anyway in 3s — Ctrl-C to abort."
  sleep 3
fi

# -----------------------------------------------------------------------------
# 6. Launch
# -----------------------------------------------------------------------------
step "Starting control panel on http://localhost:${PORT}"
c_dim "  (Ctrl-C to stop)"
echo

exec env CONTROL_SECRET="${CONTROL_SECRET}" CORE_URL="${CORE_URL}" PORT="${PORT}" \
  node "${SCRIPT_DIR}/server.js"
