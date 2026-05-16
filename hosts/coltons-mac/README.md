# coltons-mac host

This host runs the **control panel** web app — Colton's interactive view into the mesh.

## What lives here

- `.env.example` — env var template. Copy to `.env` and fill in.

The control panel app itself lives at `nodes/control/` (Worker C).

## One-time setup

1. Generate the control node's identity secret:
   ```bash
   ../../scripts/bootstrap.sh
   ```
   This writes `hosts/coltons-mac/.env` with a fresh `CONTROL_SECRET` and the default `CORE_URL`.

Alternative (manual):
```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_hex(16))"  # paste into CONTROL_SECRET
```

2. Confirm `CORE_URL` points at the Mac mini's Tailscale address (default `http://100.109.10.50:8000`).

## Boot

See `nodes/control/README.md` for the actual start command (provided by Worker C).

## Safety

- `.env` is gitignored. Never commit it.
- The control panel only ever talks to Core at `CORE_URL`. It does not call Anthropic directly.
