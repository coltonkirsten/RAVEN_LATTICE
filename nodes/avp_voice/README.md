# avp_voice — LATTICE node

Proxies mesh invocations to the RAVEN_AVP voice-control HTTP endpoints
over Tailscale. The visionOS app on the Vision Pro exposes a small
HTTP server on port 5181; this node translates signed mesh envelopes
into HTTPX calls and returns the response. The app is canonical; this
node holds no session state.

Host: **mac-mini** (Docker). Runtime: `docker`. Identity secret:
`env:AVP_VOICE_SECRET`.

## Surfaces

| name | type | mode | maps to |
| --- | --- | --- | --- |
| `status`        | tool | request_response | `GET  /voice/status` |
| `start_session` | tool | request_response | `POST /voice/start` |
| `stop_session`  | tool | request_response | `POST /voice/stop` |

No `speak` / `tell` surfaces in v1 — the existing `voice` node still
owns text-to-speech.

## Coordination contract

The visionOS app exposes an HTTP server bound to `0.0.0.0:5181`,
reachable at `http://100.109.10.50:5181`:

| method | path | response shape |
| --- | --- | --- |
| GET  | `/voice/status` | `{"session": "idle"\|"active", "session_id": str\|null, "uptime_s": float}` |
| POST | `/voice/start`  | `{"ok": true, "session_id": "..."}` (idempotent) |
| POST | `/voice/stop`   | `{"ok": true}` (idempotent) |

If the upstream is unreachable (e.g. the iOS worker hasn't deployed
yet), surfaces return `{"ok": false, "error": "upstream_unreachable",
"detail": "..."}`. That is the expected initial smoke-test behavior —
it confirms the mesh edge is alive and the failure is graceful.

## Build & run

```sh
cd /Users/ravennexus/Desktop/Projects/LATTICE/nodes/avp_voice
docker compose --env-file ../../hosts/mac-mini/.env up -d --build
docker logs -f lattice-avp-voice
```

You should see
`[avp_voice] registered session=... upstream=http://100.109.10.50:5181`.

## Smoke tests

```sh
python3 /tmp/mesh_invoke.py avp_voice.status
python3 /tmp/mesh_invoke.py avp_voice.start_session
python3 /tmp/mesh_invoke.py avp_voice.stop_session
```

Until the visionOS app is built and deployed, each of these returns
`{ok: false, error: "upstream_unreachable", ...}` — that's success at
the mesh layer.

## Environment

| Var | Default | Notes |
| --- | --- | --- |
| `AVP_VOICE_SECRET` | *(required)* | HMAC key for envelope signing. RAVEN populates this post-build. |
| `CORE_URL` | `http://host.docker.internal:8000` | Mesh Core endpoint. The Mac mini host overrides via `hosts/mac-mini/.env`. |
| `AVP_VOICE_BASE_URL` | `http://100.109.10.50:5181` | visionOS voice-control HTTP server. Tailscale IP by default. |

## Bootstrap (first-time)

1. **Generate the secret** (on the Mac mini):

   ```sh
   openssl rand -hex 16
   ```

2. **Add it to `hosts/mac-mini/.env`** (placeholder line already present):

   ```
   AVP_VOICE_SECRET=<paste hex here>
   ```

   The same value must also be present in RAVEN_MESH Core's env so
   Core can verify this node's signatures.

3. **Reload Core's manifest** so the `avp_voice` node + edges are
   picked up:

   ```sh
   # via raven (or any caller authorized for core.reload_manifest)
   ```

4. **Build and start** (see Build & run above).
