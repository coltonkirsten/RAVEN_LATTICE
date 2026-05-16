# avp — LATTICE node

Thin proxy that translates LATTICE mesh envelopes into HTTPX calls against
the RAVEN_AVP FastAPI scene server (default `http://100.109.10.50:5180`,
Tailscale-reachable). The FastAPI server is canonical; this node holds no
scene state.

Host: **mac-mini** (Docker). Runtime: `docker`. Identity secret:
`env:AVP_SECRET`.

## Surfaces

| name | type | mode | purpose |
| --- | --- | --- | --- |
| `show` | inbox | fire_and_forget | "Put this on screen near X." The 80% surface; voice + EDITH default here. |
| `add_panel` | tool | request_response | Add a panel, returns `{ok, panel}`. |
| `update_panel` | tool | request_response | Partial-merge fields on a panel. Payload: `{id, patch}` or `{id, …flat fields}`. |
| `remove_panel` | tool | request_response | Delete by id (resolves to index, issues JSON Patch remove). |
| `list_panels` | tool | request_response | Returns short form `[{id, kind, position, size}, …]`. |
| `clear_scene` | tool | request_response | Wipes `panels` to `[]`. |

### `avp.show` payload shape

```json
{
  "kind": "text|markdown|html|image|chart|mermaid|model3d|group",
  "text": "...",
  "url":  "...",
  "data": "...",
  "id":   "optional-explicit-id",
  "near": "panel-id",
  "position": [x, y, z],
  "size":     [width, height],
  "near_offset": [dx, dy, dz],
  "rotation_yaw_degrees": 0
}
```

Default position when neither `near` nor `position` is given:
`[0, 1.65, -1.3]` (about eye height, just in front of the user).
Default sizes per `kind` mirror `DEFAULT_SIZES` in
`RAVEN_AVP/server/mcp_server.py`.

The `show` handler logs a one-liner of the form
`[avp] show kind=<kind> id=<id>` so the audit trail lives in Docker logs.

## Bootstrap

1. **Generate the secret** (on the Mac mini):

   ```sh
   openssl rand -hex 16
   ```

2. **Add it to `hosts/mac-mini/.env`** (already has a placeholder line):

   ```
   AVP_SECRET=<paste hex here>
   ```

   The same value must be present in RAVEN_MESH Core's env so Core can
   verify this node's signatures.

3. **Reload Core's manifest** so the `avp` node + edges are picked up:

   ```sh
   # via raven (or any caller authorized for core.reload_manifest)
   ```

4. **Build the image**:

   ```sh
   cd nodes/avp
   docker compose --env-file ../../hosts/mac-mini/.env build
   ```

5. **Start it**:

   ```sh
   docker compose --env-file ../../hosts/mac-mini/.env up -d
   docker logs -f lattice-avp
   ```

   You should see `[avp] registered session=… upstream=http://100.109.10.50:5180`.

## Environment

| Var | Default | Notes |
| --- | --- | --- |
| `AVP_SECRET` | *(required)* | HMAC key for envelope signing. |
| `CORE_URL` | `http://host.docker.internal:8000` | Mesh Core endpoint. The Mac mini host overrides via `hosts/mac-mini/.env`. |
| `AVP_BASE_URL` | `http://100.109.10.50:5180` | FastAPI scene server. Tailscale IP by default so the proxy works regardless of host-bridge resolution. |

## Relationship to RAVEN_AVP's MCP server

`RAVEN_AVP/server/mcp_server.py` exposes 19 stdio MCP tools — full
fidelity, ideal for Claude Code / Claude Desktop. This LATTICE node
exposes the conversational 80/20 (one inbox + five tool surfaces) so
voice, EDITH, and RAVEN can drive the scene over signed envelopes. Both
layers POST to the same FastAPI server.
