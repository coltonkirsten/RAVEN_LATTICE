# browser

A LATTICE mesh node that turns a natural-language task into a browser
session. Surface: `browser.query` (inbox, fire-and-forget). The agent
loop is **browser-use** + Claude Sonnet 4.6 running a headed Chromium
against an Xvfb virtual display inside the container.

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

## Boot

```bash
cd nodes/browser
docker compose --env-file ../../hosts/mac-mini/.env up -d --build
docker logs -f lattice-browser
```

Expect:
```
[browser] registered session=<8 hex> model=claude-sonnet-4-6 auth=api-key headless=True
```

## Auth — API key vs OAuth

browser-use's `ChatAnthropic` calls the raw Anthropic SDK, which means:
- **`ANTHROPIC_API_KEY`** (`sk-ant-api03-...`) — straightforward, billed
  pay-as-you-go on console.anthropic.com. **Preferred.**
- **`CLAUDE_CODE_OAUTH_TOKEN`** (`sk-ant-oat01-...`) — Max-plan OAuth.
  The node falls back to this when no API key is set, passing
  `auth_token=` + `anthropic-beta: oauth-2025-04-20`. This *may* still
  401 if Anthropic's edge requires the Claude Code system-prompt prefix
  the CLI injects — if you see auth failures in the logs, set the API
  key instead.

EDITH uses OAuth via the Claude Code CLI, which is the supported path
for OAuth. browser-use bypasses that CLI, so API key is the path of
least resistance.

## Costs

Sonnet 4.6 pricing (~$3/MTok in, ~$15/MTok out). A 3-step Wikipedia
lookup runs ~5–15k input tokens + ~1k output → roughly **$0.02–0.05
per query**. Long multi-step research jobs can climb to **$0.30**.

## Config knobs (env)

- `BROWSER_MODEL` (default `claude-sonnet-4-6`)
- `BROWSER_HEADLESS` (`1`/`0`, default `1` — Xvfb makes headed work too)
- `BROWSER_MAX_STEPS` (default `25`)

## Limits

- Single-session. Concurrent invocations spawn a fresh Chromium per task.
- No credential vault — no logged-in flows.
- Reliability on bot-protected sites (Cloudflare/DataDome) is not the
  goal of v1.
