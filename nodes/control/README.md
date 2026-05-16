# nodes/control — LATTICE control panel

Web app that registers as the `control` node, polls Core for the live topology,
streams audit entries (when authorized), and lets you invoke any surface
`control` has an outgoing edge to.

## Quickstart (recommended)

```bash
cd nodes/control
./quickstart.sh
```

This script verifies node/npm/python3, runs `scripts/bootstrap.sh` if
`hosts/coltons-mac/.env` is missing, installs npm deps, sources env,
probes Core, and launches the server. Idempotent — safe to re-run.
Ctrl-C to stop.

## Manual install / run

```bash
cd nodes/control
npm install
CONTROL_SECRET=... CORE_URL=http://100.109.10.50:8000 npm start
```

Then open <http://localhost:5190>.

`node_modules/` is gitignored — never commit it.

In normal use, env values come from `../../hosts/coltons-mac/.env` (the
host-bootstrap script sources it). The two values you need are:

- `CONTROL_SECRET` — identity secret matching `control.identity_secret` in
  `manifest.yaml`. Required. Used both for signing outbound envelopes and
  (implicitly, via Core) authenticating inbound deliveries to
  `control.message`.
- `CORE_URL` — base URL of RAVEN_MESH Core. Defaults to
  `http://100.109.10.50:8000` (the Mac mini's Tailscale IP).
- `PORT` — defaults to `5190`.
- `CONTROL_INBOX_PATH` — where inbox entries persist as a JSON array.
  Defaults to `./inbox.json` next to `server.js`. The file is created
  empty (`[]`) on first start and written atomically (tmp + rename) on
  every mutation.

If `CONTROL_SECRET` is unset or registration is rejected, the server logs the
reason and exits non-zero.

## What you see

1. **Topology** (top panel) — force-directed graph of nodes and edges,
   refreshed every 2s from `GET /v0/introspect`. `core` is gray, actor nodes
   green, capability nodes blue. Disconnected nodes render as outlines only.
2. **Audit log** (bottom-left) — newest entries first. Each row shows
   `timestamp · from → to_surface · decision`. Hover a row to see the raw
   JSON entry. A **"hide audit polling"** checkbox in the header (on by
   default) hides rows for control's own `core.audit_query` polls plus
   their paired response rows (matched by `correlation_id`). State is
   persisted in `localStorage` under `lattice_control_filter_audit_polls`.
3. **Inbox** (bottom-middle) — messages delivered to `control.message`
   (newest first). Each entry shows sender, received timestamp, and the
   payload's `text` field (with the raw JSON in a collapsible). Unread
   entries are highlighted; click an entry to mark it read. The header
   shows an unread count and a **clear** button (with confirm dialog)
   that empties the inbox.
4. **Send** (bottom-right) — dropdown is populated from edges where
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
| `GET  /api/inbox`      | full inbox array (newest first) |
| `POST /api/inbox/:id/read` | marks one message read, persists |
| `POST /api/inbox/clear`    | empties the inbox, persists `[]` |

## Inbox surface

`control.message` is an `inbox` / `fire_and_forget` surface declared in
the root `manifest.yaml`. Inbound envelopes arrive via the SSE
`/v0/stream` `deliver` event (Core has already verified the sender's
HMAC signature before delivery — the SSE channel is the trust boundary,
which is how RAVEN_MESH nodes consume deliveries in general). Because
the surface is `fire_and_forget`, no response is posted back to
`/v0/respond`; Core returns `202` to the invoker on its own per SPEC
§4.2.

Each stored entry wraps the envelope plus:

- `received_at` — ISO timestamp when control accepted the envelope
- `read: false` — flipped to `true` when the UI marks it read

Deliveries are deduped by envelope id, so SSE replays do not double-write.

Currently allowed inbound edges (see `manifest.yaml`):

- `raven  -> control.message`
- `edith  -> control.message`
