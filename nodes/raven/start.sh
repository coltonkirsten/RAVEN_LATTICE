#!/usr/bin/env bash
# Start the RAVEN portal node.
# Runs natively on host (not docker) so it can access RAVEN's queue files.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LATTICE_ROOT="$(cd "$HERE/../.." && pwd)"

# Load shared env.
ENV_FILE="$LATTICE_ROOT/hosts/mac-mini/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "[raven-node] $ENV_FILE not found. Run scripts/bootstrap.sh first." >&2
  exit 1
fi

# Extract specific vars (the set -a; source pattern was unreliable here).
RAVEN_SECRET="$(grep '^RAVEN_SECRET=' "$ENV_FILE" | cut -d= -f2-)"
CORE_URL="$(grep '^CORE_URL=' "$ENV_FILE" | cut -d= -f2- || echo 'http://127.0.0.1:8000')"

if [[ -z "${RAVEN_SECRET}" ]]; then
  echo "[raven-node] RAVEN_SECRET is empty. Re-run scripts/bootstrap.sh or set it manually." >&2
  exit 1
fi

export RAVEN_SECRET CORE_URL

cd "$HERE"
exec python3 -u raven_node.py
