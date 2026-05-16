# browser

A LATTICE mesh node that turns a natural-language task into a browser
session. Surface: `browser.query` (inbox, fire-and-forget). Backend:
the Claude Code CLI spawned with a `@playwright/mcp` MCP server, so
Claude Sonnet 4.6 drives a headless Chromium via the standard MCP
browser tools (`browser_navigate`, `browser_click`, `browser_snapshot`,
…) — same auth path EDITH uses (Max-plan OAuth).

## Payload

```jsonc
// invocation -> browser.query
{ "task": "What is the population of Sacramento on Wikipedia?",
  "conversation_id": "optional-thread-id" }
```

The reply is a NEW signed invocation to the sender's inbox surface
(`raven.message`, `edith.chat`, `control.message`):

```jsonc
{ "message": "<final agent result text>",
  "in_reply_to": "<original msg id>",
  "conversation_id": "..." }
```

The spawned CLI is ALSO loaded with `mesh_mcp.py`, so it can reply
directly via `mcp__lattice_mesh__mesh_<sender>_<inbox>`. If it forgets,
the node-side fallback ships the final text on its behalf.

## Boot

```bash
cd nodes/browser
docker compose --env-file ../../hosts/mac-mini/.env up -d --build
docker logs -f lattice-browser
```

Expect:
```
[browser] registered session=<8 hex> model=claude-sonnet-4-6 auth=oauth-cli headless=True mcp_enabled=True
```

## Auth — OAuth via Claude Code CLI

This node uses the Max-plan OAuth flow exclusively. browser_node.py
strips `ANTHROPIC_API_KEY` before spawning the CLI so the CLI falls
back to `CLAUDE_CODE_OAUTH_TOKEN` (same trick EDITH uses). Pay-as-you-go
API keys are not consumed.

Why not browser-use? An earlier design used browser-use with the
Anthropic SDK directly — but the SDK can't broker OAuth, and the
account's pay-as-you-go credit balance is $0. The CLI is the only
supported OAuth path.

## Costs

Counted against the Max plan's monthly bucket — no per-task billing.
Headroom is the shared pool with EDITH and any other CLI-backed nodes.

## Config knobs (env)

- `BROWSER_MODEL` (default `claude-sonnet-4-6`)
- `BROWSER_HEADLESS` (`1`/`0`, default `1` — Xvfb makes headed work too)
- `BROWSER_MAX_TURNS` (default `20`)
- `BROWSER_MCP_ENABLED` (`1`/`0`, default `1` — mounts the mesh outbound
  bridge so the CLI can reply directly via signed mesh invocations)

## Limits

- Single-session. Concurrent invocations spawn a fresh Chromium per task.
- No credential vault — no logged-in flows.
- Reliability on bot-protected sites (Cloudflare/DataDome) is not the
  goal of v1.
