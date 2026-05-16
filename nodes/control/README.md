# nodes/control — LATTICE control panel

Web app that registers as the `control` node, polls Core for the live topology,
streams audit entries (when authorized), and lets you invoke any surface
`control` has an outgoing edge to.

## Install

```bash
cd nodes/control
npm install
```

`node_modules/` is gitignored — never commit it.

## Run

```bash
CONTROL_SECRET=... CORE_URL=http://100.109.10.50:8000 npm start
```

Then open <http://localhost:5190>.

In normal use, env values come from `../../hosts/coltons-mac/.env` (the
host-bootstrap script sources it). The two values you need are:

- `CONTROL_SECRET` — identity secret matching `control.identity_secret` in
  `manifest.yaml`. Required.
- `CORE_URL` — base URL of RAVEN_MESH Core. Defaults to
  `http://100.109.10.50:8000` (the Mac mini's Tailscale IP).
- `PORT` — defaults to `5190`.

If `CONTROL_SECRET` is unset or registration is rejected, the server logs the
reason and exits non-zero.

## What you see

1. **Topology** (top panel) — force-directed graph of nodes and edges,
   refreshed every 2s from `GET /v0/introspect`. `core` is gray, actor nodes
   green, capability nodes blue. Disconnected nodes render as outlines only.
2. **Audit log** (bottom-left) — newest entries first. Each row shows
   `timestamp · from → to_surface · decision`. Hover a row to see the raw
   JSON entry.
3. **Send** (bottom-right) — dropdown is populated from edges where
   `from == "control"`. Submit invokes the surface via Core; response shown
   inline.

## Follow-up: enable audit streaming

The audit panel needs `control` to be authorized to call `core.audit_query`.
Edit `manifest.yaml` at the repo root and add under `relationships:`

```yaml
- { from: control, to: core.audit_query }
```

Then reload Core (kick the supervisor or call `core.reload_manifest`).
Until that edge exists, the audit panel shows a note and stays empty —
the topology graph and send form still work.

If you also want the `Send` dropdown to include `core.state` /
`core.metrics`, add those edges too:

```yaml
- { from: control, to: core.state }
- { from: control, to: core.metrics }
```

(Worker A owns `manifest.yaml`. If you're spinning up the mesh manually,
edit it yourself — but do not edit it from this directory.)

## Architecture

- `server.js` — Express on `localhost:5190`. Registers with Core (HMAC-SHA256
  over canonical JSON per RAVEN_MESH SPEC §3.1), keeps an SSE consumer on
  `/v0/stream` alive (auto-reconnect on drop), proxies introspect, audit
  polls, and invocations to the browser.
- `web/` — single page, vanilla JS, vis-network from CDN. No build step.

## Endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET  /api/introspect` | proxies Core's `/v0/introspect` |
| `GET  /api/audit/poll` | invokes `core.audit_query` if the edge exists; otherwise returns `{audit: [], note: "..."}` |
| `POST /api/invoke`     | body `{to, payload}` → signed envelope → Core's `/v0/invoke` |
| `GET  /api/health`     | session id + outgoing edges |
