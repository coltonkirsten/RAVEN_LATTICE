# mac-mini host

This host runs **RAVEN_MESH Core** and the **EDITH** node.

## What lives here

- `start-core.sh` — boots RAVEN_MESH Core bound to `100.109.10.50:8000` with `manifest.yaml`.
- `.env.example` — env var template. Copy to `.env` and fill in real values.
- `audit.log` — created at runtime by Core; not committed.

## One-time setup

1. Make sure `~/Desktop/Projects/RAVEN_MESH/` is checked out.
2. Generate secrets:
   ```bash
   ../../scripts/bootstrap.sh
   ```
   This writes `hosts/mac-mini/.env` with fresh `ADMIN_TOKEN` and `EDITH_SECRET`.
3. Fill in `ANTHROPIC_API_KEY` in `.env` — EDITH needs it to call Claude.

Alternative (manual):
```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_hex(16))"  # paste twice, into ADMIN_TOKEN and EDITH_SECRET
```

## Boot

```bash
./start-core.sh                       # terminal 1 — Core
cd ../../nodes/edith && docker compose up   # terminal 2 — EDITH (Worker B provides the compose file)
```

The audit log lands at `hosts/mac-mini/audit.log`. Tail it while testing:
```bash
tail -f audit.log
```

## Safety

- `.env` is gitignored. Never commit it.
- `start-core.sh` aborts loudly if `ADMIN_TOKEN` or `EDITH_SECRET` are missing.
