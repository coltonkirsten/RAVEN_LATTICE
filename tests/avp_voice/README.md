# avp_voice integration smoke test

End-to-end smoke test for the Option C `avp_voice` mesh node — the
mesh-side peer that composes prompts/tools/scene context for the
visionOS voice surface, owns the back-channel for OpenAI Realtime
tool calls, and forwards `tell`/`speak` to the device.

The test stands up local mocks for the visionOS app and the FastAPI
scene server, then drives every public surface of `avp_voice` over
real signed mesh envelopes through Core.

## Prerequisites

1. **RAVEN_MESH Core running** at `CORE_URL` (default
   `http://localhost:8000`).
2. **`avp_voice` container running.** Build and start with:
   ```
   cd nodes/avp_voice && docker compose up -d
   ```
   The container exposes the back-channel HTTP server on `5182` and
   talks to whatever host:port `AVP_VOICE_BASE_URL` and `AVP_SCENE_URL`
   point at.
3. **Manifest updates from Worker C** loaded into Core:
   - `avp_voice` declares surfaces: `status`, `start_session`,
     `stop_session`, `session_status`, `get_system_message`,
     `set_system_message`, `scene_snapshot`, `speak`, `tell`.
   - Sender (default `raven`) has edges to each surface under test.
   - Outbound edges from `avp_voice` (e.g. `avp_voice → raven.message`,
     `avp_voice → avp.list_panels`) so the tool-call back-channel can
     resolve names.
4. **`avp_voice` container env points at the mocks.** Before tests
   that exercise `start_session` / `tell` / `speak` / `scene_snapshot`,
   the container must be configured with:
   ```
   AVP_VOICE_BASE_URL=http://host.docker.internal:5181
   AVP_SCENE_URL=http://host.docker.internal:5180
   ```
   These are the Worker-A defaults — confirm via
   `docker compose config` or `docker inspect`. The test machine must
   be the same host as the avp_voice container (so `host.docker.internal`
   resolves to it) or you must set up an equivalent network route.
5. **Sender secret.** Export `RAVEN_SECRET` (or
   `TEST_CALLER_SECRET` + `TEST_CALLER` if using a different sender):
   ```
   export RAVEN_SECRET=$(grep '^RAVEN_SECRET=' hosts/mac-mini/.env | cut -d= -f2)
   ```
6. **Ports 5180 and 5181 free.** The mocks bind on `0.0.0.0:5180`
   (scene) and `0.0.0.0:5181` (visionOS). On the Mac mini the real
   FastAPI scene server normally lives on 5180 — **stop it** before
   running tests, or override both `MOCK_SCENE_PORT` here and
   `AVP_SCENE_URL` on the avp_voice container.

## How to run

```
cd tests/avp_voice
python3 test_avp_voice_smoke.py
```

Successful run:
```
[test] mocks up — visionOS=0.0.0.0:5181 scene=0.0.0.0:5180
[test] preflight ok — sender=raven core=http://localhost:8000 backchannel=http://localhost:5182
TEST 1: set_system_message + get_system_message round trip — PASS
TEST 2: scene_snapshot returns compact form — PASS
TEST 3: start_session rich payload — PASS
TEST 4: tool_call back-channel — PASS
TEST 5: tell/speak inbox — PASS
TEST 6: get_system_message no override — PASS
TEST 7: set_system_message reset — PASS

ALL TESTS PASSED (7/7)
```

Exit code is non-zero on any failure.

## What each test verifies

| # | Test | What it checks |
|---|------|----------------|
| 1 | `set_system_message` + `get_system_message` round trip | `set` persists the override, response payload reports `override_set=True` and correct `override_chars`. `get` returns the override and includes it in `resolved`. If the host can see the container's mounted `/data` volume, asserts the override file is present on disk. |
| 2 | `scene_snapshot` compact form | Returns `{panels, count, version}`. Each panel has `{id, kind, text_preview, url, has_data}`. `text_preview` is ≤200 chars and whitespace-stripped. Count and version match what the mock served. |
| 3 | `start_session` rich payload | Mock visionOS app receives a POST `/voice/start` with `{instructions, tools, callback_url}`. Instructions is a non-empty string containing persona markers (`visionOS` / `Vision Pro` / `JARVIS`). Tools is a non-empty list of OpenAI Realtime function-tool dicts. `callback_url` includes the back-channel port. |
| 4 | `/tool_call` back-channel | Direct POST to `avp_voice`'s `:5182/tool_call` with `{call_id, name, arguments_json}` returns `{ok: true, ack: {...}}`. Verified via Core's `audit_query` that a `avp_voice → raven.message` invocation was actually routed (best-effort; warns if audit unavailable). |
| 5 | `tell` / `speak` inbox forwards | Fire-and-forget invocations to `avp_voice.tell` and `avp_voice.speak` cause the node to POST `{kind, text, source}` to the mock visionOS `/voice/inject`. |
| 6 | `get_system_message` with no override | After reset, response reports `override=null`, `override_set=False`, `override_chars=0`. `resolved` is non-empty and contains the default persona block. |
| 7 | `set_system_message` reset | Setting an override, then calling `set_system_message` with `{message: ""}` clears it: `override_set=False`, override file removed from disk (if visible). |

## Mocks

The test spins up two `aiohttp` servers and tears them down on exit
(via `contextlib.asynccontextmanager`).

**Mock visionOS app — `0.0.0.0:5181`:**
- `GET /voice/status` → `{"session": "idle", "session_id": null, "uptime_s": 0}`
- `POST /voice/start` → captures body, returns `{"ok": true, "session_id": "mock-session-abc"}`
- `POST /voice/stop` → `{"ok": true}`
- `POST /voice/inject` → captures body, returns `{"ok": true}`

**Mock FastAPI scene server — `0.0.0.0:5180`:**
- `GET /scene` → three fake panels (text + html) with deterministic
  text bodies including whitespace and a >200-char body so the
  snapshot test can verify stripping/truncation.

All captured request bodies are kept in an in-memory `MockState` for
assertion.

## Known limitations

- **Does NOT exercise the OpenAI Realtime API.** The mock visionOS
  app accepts `/voice/start` and returns a fake `session_id` — no
  WebSocket to OpenAI is opened. Realtime-side regressions land in
  the Worker B Swift test plan, not here.
- **Does NOT run the real visionOS app.** The on-device server in
  `RAVEN_AVP` isn't reached by these tests. End-to-end-with-device
  validation is a manual step (see the avp_voice node README).
- **Test 4's audit check is best-effort.** If `core.audit_query` is
  unreachable or returns an unexpected shape, the test logs a
  warning and trusts the synchronous `/tool_call` ack.
- **Override-file disk check is conditional.** If
  `OVERRIDE_FILE_HOST_PATH` (default
  `nodes/avp_voice/data/avp_voice_system_override.txt`) is not
  visible to the test process (e.g. the test runs on a different
  machine than the container), the on-disk assertion is skipped
  with a log line.
- **Tests assume the sender (`raven`) has edges to every
  `avp_voice.*` surface under test.** Worker C's manifest changes
  must be in place; preflight fails fast with a clear error if not.

## Configuration

Environment variables (all optional unless noted):

| Var | Default | Purpose |
|-----|---------|---------|
| `CORE_URL` | `http://localhost:8000` | Where Core lives. |
| `RAVEN_SECRET` | — *(required)* | Sender HMAC secret. |
| `TEST_CALLER` | `raven` | Override sender identity. |
| `TEST_CALLER_SECRET` | — | Override sender secret (else uses `RAVEN_SECRET`). |
| `MOCK_BIND_HOST` | `0.0.0.0` | Bind address for both mocks. |
| `MOCK_VISIONOS_PORT` | `5181` | visionOS mock port. |
| `MOCK_SCENE_PORT` | `5180` | Scene mock port. |
| `AVP_VOICE_HOST` | `localhost` | Where the avp_voice back-channel HTTP server lives. |
| `AVP_VOICE_CALLBACK_PORT` | `5182` | Back-channel port. |
| `AVP_VOICE_CALLBACK_URL` | derived | Override full URL. |
| `OVERRIDE_FILE_HOST_PATH` | `nodes/avp_voice/data/avp_voice_system_override.txt` | Host-side path to the persisted override file (or `""` to skip on-disk asserts). |
| `INVOKE_TIMEOUT` | `10` | Per-invocation timeout (seconds). |

## Idempotency

Each run resets state in setup (clears any persisted override; sends a
best-effort `stop_session`) and again in teardown. Re-running back-to-back
should be clean. If a run is killed mid-way and leaves a stale override,
either run the test again (the next setup wipes it) or remove
`nodes/avp_voice/data/avp_voice_system_override.txt` manually.
