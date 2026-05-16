#!/usr/bin/env bash
# start-core.sh — boot RAVEN_MESH Core on the Mac mini with the LATTICE manifest.
#
# Sources hosts/mac-mini/.env, validates required secrets, then launches Core
# bound to the Tailscale address. Audit log lands beside this script.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"
AUDIT_LOG="${SCRIPT_DIR}/audit.log"
MANIFEST="${REPO_ROOT}/manifest.yaml"
RAVEN_MESH_DIR="${HOME}/Desktop/Projects/RAVEN_MESH"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "ERROR: ${ENV_FILE} not found. Run scripts/bootstrap.sh first." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

: "${ADMIN_TOKEN:?ADMIN_TOKEN is not set in ${ENV_FILE}}"
: "${EDITH_SECRET:?EDITH_SECRET is not set in ${ENV_FILE}}"
: "${RAVEN_SECRET:?RAVEN_SECRET is not set in ${ENV_FILE}}"

if [[ ! -d "${RAVEN_MESH_DIR}" ]]; then
  echo "ERROR: RAVEN_MESH repo not found at ${RAVEN_MESH_DIR}" >&2
  exit 1
fi

echo "Booting RAVEN_MESH Core"
echo "  manifest : ${MANIFEST}"
echo "  audit log: ${AUDIT_LOG}"
echo "  bind     : 100.109.10.50:8000"
echo

cd "${RAVEN_MESH_DIR}"
exec python3 -m core.core \
  --host 100.109.10.50 \
  --port 8000 \
  --manifest "${MANIFEST}" \
  --audit-log "${AUDIT_LOG}"
