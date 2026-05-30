# LATTICE

**Layered Agentic Topology with Typed Interactive Channels & Edges**

An opinionated daily-driver AI mesh built on top of the [RAVEN_MESH](https://github.com/coltonkirsten/RAVEN_MESH) protocol. RAVEN_MESH gives us the wire format, identity, routing, and audit. LATTICE adds the actual nodes Colton uses every day: Claude-backed agents, a control panel, a voice interface, AVP scene/voice proxies, a browser automation agent, and host-specific bootstrap scripts.

## Status

Daily driver. `manifest.yaml` declares 7 nodes — `edith`, `control`, `raven`, `voice`, `avp`, `browser`, `avp_voice` — across two hosts (Mac mini + Colton's Mac), wired by ~100 directed edges.

## Core

Core is the RAVEN_MESH broker (`python -m core.core`, run from the RAVEN_MESH repo against this repo's `manifest.yaml`). It is the single trusted process in the mesh: it owns identity verification, edge-based routing, payload validation, and the append-only audit log. Nodes never talk to each other directly — every envelope flows Core → SSE delivery → `POST` response.

Core also exposes its own control plane as the reserved built-in `core` node (SPEC §5). Envelopes addressed to `core.<surface>` are dispatched in-process but still traverse the full `/v0/invoke` path (HMAC, replay window, allow-edge, schema, audit). Core surfaces are all `request_response`:

```
core.state   core.processes  core.metrics  core.audit_query
core.set_manifest  core.reload_manifest
core.spawn  core.stop  core.restart  core.reconcile  core.drain
```

`raven` holds edges to all of them; `control` and `raven` can read `core.audit_query`. Two out-of-band operator endpoints sit outside mesh traffic and are bearer-gated by `ADMIN_TOKEN`: `GET /v0/admin/stream` (raw SSE tap) and `GET /v0/admin/metrics` (Prometheus).

## The node model

Every participant — agent, tool, device, human — is a **node** that exposes one or more typed **surfaces**. A surface is an addressable endpoint named `<node>.<surface>` with a JSON Schema for its input payload and an invocation mode.

**Surface taxonomy** — two kinds:

- `type: inbox` / `invocation_mode: fire_and_forget` — a work queue. The sender gets a `202 accepted`, not a result. A node that *processes* work declares exactly one inbox surface (e.g. `edith.chat`, `raven.message`, `browser.query`). Receivers reply by sending a **new signed invocation** back to the sender's inbox — never via a response leg.
- `type: tool` / `invocation_mode: request_response` — a callable. The sender blocks and gets the result envelope back (e.g. `avp.add_panel`, `voice.start_session`). Core surfaces are always `request_response`.

A node may mix both: `voice` and `avp_voice` expose `tool` surfaces for session control plus `inbox` surfaces (`speak`/`tell`) for fire-and-forget injection.

## Manifest schema

`manifest.yaml` is the single source of mesh topology. Core loads it at boot and on `core.reload_manifest`. Two top-level keys:

```yaml
nodes:
  - id: edith                       # unique node id
    kind: capability                # capability | actor | interface (descriptive)
    runtime: docker                 # docker | native | web
    identity_secret: env:EDITH_SECRET   # HMAC secret, resolved from env at boot
    metadata: { description: "...", host: mac-mini }
    surfaces:
      - name: chat
        type: inbox                 # inbox | tool
        invocation_mode: fire_and_forget   # fire_and_forget | request_response
        schema: shared/schemas/chat.json   # JSON Schema for the input payload
        # optional: purpose: |  ...human/LLM-facing description for tool surfaces

relationships:                      # directed allow-edges; Core rejects any
  - from: control                   # invocation whose (from → to.surface) edge
    to: raven.message               # is not listed here
```

## Identity, secrets & envelope signing

Each node has an `identity_secret` — a hex HMAC key referenced as `env:VAR_NAME` and resolved from the per-host `.env` at boot. **Real secrets are never committed**; `shared/secrets.yaml.example` documents the `{NODE_ID}_SECRET` naming convention and `scripts/bootstrap.sh` generates them (`secrets.token_hex(16)`).

Every envelope is signed: the signer canonicalizes the envelope JSON with the `signature` field removed (`json.dumps(sort_keys=True, separators=(",",":"))`) and computes `HMAC-SHA256(secret, canonical)`. Core re-derives the signature from the registered node's secret and rejects on mismatch. A timestamp **replay window** plus a nonce LRU reject stale or replayed envelopes. Payloads are then validated against the surface's JSON Schema before routing.

## Node bootstrap / registration lifecycle

The `node_sdk.MeshNode` client (from RAVEN_MESH) drives the lifecycle:

1. **Register** — `POST /v0/register` with a signed `{node_id, timestamp}` body. Core verifies HMAC + timestamp drift and returns `{session_id, surfaces, relationships}` (the node's own surfaces and allow-edges from the manifest).
2. **Stream** — open `GET /v0/stream?session=<id>` (SSE). Core pushes `deliver` events for inbound invocations targeting this node's surfaces.
3. **Dispatch** — the node runs the registered async handler per surface. For an inbox surface it does its work and, if a reply is wanted, calls `invoke()` to the sender's inbox. For a tool surface it `respond()`s on the correlated `/v0/respond` leg.

A node only learns the edges the manifest grants it; to add capabilities you edit the manifest and `core.reload_manifest`, no Core restart required.

## Worked example — add a `clock` node

1. **Manifest** — add to `manifest.yaml`:
   ```yaml
   nodes:
     - id: clock
       kind: capability
       runtime: native
       identity_secret: env:CLOCK_SECRET
       metadata: { description: "Returns the time.", host: mac-mini }
       surfaces:
         - name: now
           type: tool
           invocation_mode: request_response
           schema: shared/schemas/permissive.json
   relationships:
     - from: raven
       to: clock.now            # allow raven to call it
   ```
2. **Secret** — add `CLOCK_SECRET=$(python3 -c 'import secrets;print(secrets.token_hex(16))')` to `hosts/mac-mini/.env`.
3. **Node** — `nodes/clock/clock_node.py`, build on `MeshNode`:
   ```python
   node = MeshNode("clock", os.environ["CLOCK_SECRET"], CORE_URL)
   node.on("now", lambda env: {"iso": now_iso()})
   await node.start()
   ```
4. **Reload** — `core.reload_manifest` (or restart Core). `clock` registers, `raven` can now call `clock.now`.

See `nodes/browser/` for a full inbox-surface node (request in, reply via a new invocation to the sender) and `nodes/edith/` for the chat pattern.

## Repo layout

```
manifest.yaml           — declares all nodes + surfaces + edges in the mesh
nodes/
  edith/                — Sonnet 4.6 chat agent, Dockerized, Mac mini
  raven/                — RAVEN agent portal; inbox lands in RAVEN's queue
  control/              — web app, Colton's Mac, visualizes the live mesh
  voice/                — gpt-realtime-2 voice node (mic + speakers)
  avp/                  — RAVEN_AVP scene proxy (panels + 3D entities)
  avp_voice/            — RAVEN_AVP voice-control proxy
  browser/              — browser-automation agent (Claude CLI + Playwright MCP)
hosts/
  mac-mini/             — boot scripts (start-core.sh) + audit.log
  coltons-mac/          — boot notes for Colton's machine
shared/
  schemas/              — JSON Schemas for each surface's input payload
  secrets.yaml.example  — secret naming convention (never commit real secrets)
scripts/
  bootstrap.sh          — generates per-host secrets, fills .env templates
  quickstart_voice.sh   — boots the voice node
```

## Boot order

1. Run `scripts/bootstrap.sh` to generate per-host `.env` files; fill in `ANTHROPIC_API_KEY` (and `OPENAI_API_KEY` for voice).
2. Mac mini: `./hosts/mac-mini/start-core.sh` — boots RAVEN_MESH Core with this repo's `manifest.yaml`, bound to the Tailscale address.
3. Mac mini: start EDITH, RAVEN, and the other Dockerized/native nodes.
4. Colton's Mac: start the control panel (web app) — see `nodes/control/README.md`.
5. Nodes register with Core; the control panel renders the live topology.

## Conventions

- **Pull before you push.** This repo is collaborative.
- **Secrets via env vars only.** `manifest.yaml` references secrets as `env:VAR_NAME`. Never commit real secret values.
- **One subfolder per node.** New nodes get their own directory under `nodes/`.
- **One subfolder per host.** New machines joining the mesh get their own directory under `hosts/`.

## Built on

- Protocol layer: [RAVEN_MESH](https://github.com/coltonkirsten/RAVEN_MESH) — v0 wire protocol (`docs/SPEC.md`)
- Agent runtime: Anthropic Claude (Sonnet 4.6, via SDK and Claude Code CLI/OAuth)
- Transport: Tailscale-routed HTTP across hosts
