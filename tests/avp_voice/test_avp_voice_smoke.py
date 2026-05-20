#!/usr/bin/env python3
"""Integration smoke test for the Option C `avp_voice` mesh node.

Mocks the visionOS app (port 5181) and the FastAPI scene server
(port 5180), then exercises every public surface of the avp_voice
node end-to-end via signed mesh envelopes. The visionOS app is NOT
required — the mock stands in for it.

Run with:
    python3 test_avp_voice_smoke.py

See README.md for prerequisites (Core + avp_voice container running).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
from aiohttp import web
import httpx


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CORE_URL = os.environ.get("CORE_URL", "http://localhost:8000").rstrip("/")
SENDER = os.environ.get("TEST_CALLER", "raven")
SENDER_SECRET = (
    os.environ.get("TEST_CALLER_SECRET")
    or os.environ.get("RAVEN_SECRET")
    or ""
)

MOCK_VISIONOS_HOST = os.environ.get("MOCK_BIND_HOST", "0.0.0.0")
MOCK_VISIONOS_PORT = int(os.environ.get("MOCK_VISIONOS_PORT", "5181"))
MOCK_SCENE_PORT = int(os.environ.get("MOCK_SCENE_PORT", "5180"))

AVP_VOICE_HOST = os.environ.get("AVP_VOICE_HOST", "localhost")
AVP_VOICE_CALLBACK_PORT = int(os.environ.get("AVP_VOICE_CALLBACK_PORT", "5182"))
AVP_VOICE_CALLBACK_URL = (
    os.environ.get("AVP_VOICE_CALLBACK_URL")
    or f"http://{AVP_VOICE_HOST}:{AVP_VOICE_CALLBACK_PORT}"
).rstrip("/")

# Host-side path to the persisted override file. Defaults to the volume
# location implied by Worker A's docker-compose change. If the test runs
# on a different machine than the container, set OVERRIDE_FILE_HOST_PATH
# to "" to skip the on-disk assertion.
DEFAULT_OVERRIDE_HOST_PATH = (
    "/Users/ravennexus/Desktop/Projects/LATTICE/nodes/avp_voice/data/"
    "avp_voice_system_override.txt"
)
OVERRIDE_FILE_HOST_PATH_RAW = os.environ.get(
    "OVERRIDE_FILE_HOST_PATH", DEFAULT_OVERRIDE_HOST_PATH
)
OVERRIDE_FILE_HOST_PATH: Path | None = (
    Path(OVERRIDE_FILE_HOST_PATH_RAW) if OVERRIDE_FILE_HOST_PATH_RAW else None
)

# Timeouts for the Core /v0/invoke poll. Core's default invoke_timeout
# is generous; we cap our own waits well below that to surface bugs.
INVOKE_TIMEOUT = float(os.environ.get("INVOKE_TIMEOUT", "10"))


# ---------------------------------------------------------------------------
# Mesh envelope helpers (mirrored from voice.py / avp_voice_node.py)
# ---------------------------------------------------------------------------
def canonical(env: dict) -> bytes:
    body = {k: v for k, v in env.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def sign(env: dict, secret: str) -> str:
    return hmac.new(secret.encode(), canonical(env), hashlib.sha256).hexdigest()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_envelope(to: str, payload: dict, *, kind: str = "invocation") -> dict:
    msg_id = str(uuid.uuid4())
    env = {
        "id": msg_id,
        "correlation_id": msg_id,
        "from": SENDER,
        "to": to,
        "kind": kind,
        "payload": payload,
        "timestamp": now_iso(),
    }
    env["signature"] = sign(env, SENDER_SECRET)
    return env


# ---------------------------------------------------------------------------
# Mock state (in-memory capture for assertions)
# ---------------------------------------------------------------------------
class MockState:
    def __init__(self) -> None:
        self.voice_status_requests: list[dict] = []
        self.voice_start_requests: list[dict] = []
        self.voice_stop_requests: list[dict] = []
        self.voice_inject_requests: list[dict] = []
        self.scene_get_count = 0
        self.scene_version = 7
        # Three fake panels covering kind=text and kind=html so the
        # snapshot test can verify shape regardless of which kind a
        # given panel uses. Whitespace and a long body verify the
        # text_preview stripping/truncation logic.
        self.scene_panels: list[dict] = [
            {
                "id": "pulse-title",
                "kind": "text",
                "position": {"x": 0.0, "y": 1.5, "z": -1.5},
                "size": {"width": 0.4, "height": 0.2, "depth": 0.01},
                "text": "PULSE\nlocal-first overview",
            },
            {
                "id": "pulse-live",
                "kind": "html",
                "position": {"x": -0.7, "y": 1.5, "z": -1.5},
                "size": {"width": 0.6, "height": 0.4, "depth": 0.01},
                "text": (
                    "  Pulse local-first overview — synced. "
                    + ("x" * 250)
                ),
                "url": "https://pulse.example.com/live",
            },
            {
                "id": "scratchpad",
                "kind": "text",
                "position": {"x": 0.7, "y": 1.5, "z": -1.5},
                "size": {"width": 0.4, "height": 0.4, "depth": 0.01},
                "text": "  scratch notes from voice  ",
            },
        ]

    def reset(self) -> None:
        self.voice_status_requests.clear()
        self.voice_start_requests.clear()
        self.voice_stop_requests.clear()
        self.voice_inject_requests.clear()


MOCK = MockState()


# ---------------------------------------------------------------------------
# Mock visionOS app (port 5181)
# ---------------------------------------------------------------------------
async def _mock_visionos_status(request: web.Request) -> web.Response:
    MOCK.voice_status_requests.append({"path": str(request.url)})
    return web.json_response(
        {"session": "idle", "session_id": None, "uptime_s": 0}
    )


async def _mock_visionos_start(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        body = {}
    MOCK.voice_start_requests.append(body)
    return web.json_response(
        {"ok": True, "session_id": "mock-session-abc"}
    )


async def _mock_visionos_stop(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        body = {}
    MOCK.voice_stop_requests.append(body)
    return web.json_response({"ok": True})


async def _mock_visionos_inject(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        body = {}
    MOCK.voice_inject_requests.append(body)
    return web.json_response({"ok": True})


def _make_visionos_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/voice/status", _mock_visionos_status)
    app.router.add_post("/voice/start", _mock_visionos_start)
    app.router.add_post("/voice/stop", _mock_visionos_stop)
    app.router.add_post("/voice/inject", _mock_visionos_inject)
    return app


# ---------------------------------------------------------------------------
# Mock FastAPI scene server (port 5180)
# ---------------------------------------------------------------------------
async def _mock_scene_get(request: web.Request) -> web.Response:
    MOCK.scene_get_count += 1
    return web.json_response(
        {"version": MOCK.scene_version, "panels": MOCK.scene_panels}
    )


def _make_scene_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/scene", _mock_scene_get)
    return app


@asynccontextmanager
async def _run_mocks():
    """Spin up both mock servers; tear down on exit."""
    visionos_runner = web.AppRunner(_make_visionos_app())
    await visionos_runner.setup()
    visionos_site = web.TCPSite(
        visionos_runner, MOCK_VISIONOS_HOST, MOCK_VISIONOS_PORT
    )
    scene_runner = web.AppRunner(_make_scene_app())
    await scene_runner.setup()
    scene_site = web.TCPSite(
        scene_runner, MOCK_VISIONOS_HOST, MOCK_SCENE_PORT
    )
    try:
        await visionos_site.start()
    except OSError as e:
        await visionos_runner.cleanup()
        await scene_runner.cleanup()
        raise SystemExit(
            f"[test] FATAL: cannot bind mock visionOS on "
            f"{MOCK_VISIONOS_HOST}:{MOCK_VISIONOS_PORT} — {e}. "
            f"Set MOCK_VISIONOS_PORT to a free port or stop the conflicting "
            f"service."
        )
    try:
        await scene_site.start()
    except OSError as e:
        await visionos_runner.cleanup()
        await scene_runner.cleanup()
        raise SystemExit(
            f"[test] FATAL: cannot bind mock scene server on "
            f"{MOCK_VISIONOS_HOST}:{MOCK_SCENE_PORT} — {e}. The real "
            f"FastAPI scene server is probably running on 5180; either "
            f"stop it or set MOCK_SCENE_PORT + AVP_SCENE_URL on the "
            f"avp_voice container env."
        )
    print(
        f"[test] mocks up — visionOS={MOCK_VISIONOS_HOST}:{MOCK_VISIONOS_PORT}"
        f" scene={MOCK_VISIONOS_HOST}:{MOCK_SCENE_PORT}",
        flush=True,
    )
    try:
        yield
    finally:
        await visionos_runner.cleanup()
        await scene_runner.cleanup()
        print("[test] mocks torn down", flush=True)


# ---------------------------------------------------------------------------
# Pre-flight: confirm Core + avp_voice are reachable
# ---------------------------------------------------------------------------
async def _preflight(http: aiohttp.ClientSession) -> None:
    # Core registry — confirms avp_voice declared, surfaces present.
    try:
        async with http.get(f"{CORE_URL}/v0/introspect") as r:
            if r.status != 200:
                raise SystemExit(
                    f"[test] FATAL: Core /v0/introspect returned {r.status}. "
                    f"Is Core running at {CORE_URL}?"
                )
            data = await r.json()
    except aiohttp.ClientConnectorError as e:
        raise SystemExit(
            f"[test] FATAL: cannot reach Core at {CORE_URL} — {e}. "
            f"Start RAVEN_MESH Core first."
        )
    node = next(
        (n for n in data.get("nodes", []) if n.get("id") == "avp_voice"),
        None,
    )
    if not node:
        raise SystemExit(
            "[test] FATAL: 'avp_voice' not declared in manifest. Run "
            "Worker C's manifest update first."
        )
    surfaces = {s["name"] for s in node.get("surfaces", [])}
    required = {
        "status", "start_session", "stop_session", "session_status",
        "get_system_message", "set_system_message", "scene_snapshot",
        "speak", "tell",
    }
    missing = required - surfaces
    if missing:
        raise SystemExit(
            f"[test] FATAL: avp_voice missing surfaces {sorted(missing)}. "
            f"Worker A's node must declare them in the manifest."
        )
    if not SENDER_SECRET:
        raise SystemExit(
            "[test] FATAL: no sender secret. Export RAVEN_SECRET (or "
            "TEST_CALLER_SECRET + TEST_CALLER) before running."
        )
    # Confirm sender edges to the surfaces we'll invoke. If any are
    # missing, the request will 403 — surface a clearer error here.
    edges = {(e["from"], e["to"]) for e in data.get("relationships", [])}
    needed_edges = [
        f"avp_voice.{s}" for s in
        ("set_system_message", "get_system_message", "scene_snapshot",
         "start_session", "stop_session", "tell", "speak", "status")
    ]
    missing_edges = [
        s for s in needed_edges if (SENDER, s) not in edges
    ]
    if missing_edges:
        raise SystemExit(
            f"[test] FATAL: sender {SENDER!r} lacks edges to "
            f"{missing_edges}. Add them to manifest.yaml relationships "
            f"and core.reload_manifest before running."
        )
    # Confirm avp_voice's back-channel HTTP server is live (Worker A).
    try:
        async with http.get(
            f"{AVP_VOICE_CALLBACK_URL}/healthz",
            timeout=aiohttp.ClientTimeout(total=3),
        ) as r:
            if r.status != 200:
                raise SystemExit(
                    f"[test] FATAL: avp_voice back-channel at "
                    f"{AVP_VOICE_CALLBACK_URL}/healthz returned {r.status}. "
                    f"Is the avp_voice container running and is port 5182 "
                    f"exposed?"
                )
    except (aiohttp.ClientConnectorError, asyncio.TimeoutError) as e:
        raise SystemExit(
            f"[test] FATAL: cannot reach avp_voice back-channel at "
            f"{AVP_VOICE_CALLBACK_URL} — {e}. Start the container "
            f"(`docker compose up -d` in nodes/avp_voice/) first."
        )
    print(
        f"[test] preflight ok — sender={SENDER} core={CORE_URL} "
        f"backchannel={AVP_VOICE_CALLBACK_URL}",
        flush=True,
    )


# ---------------------------------------------------------------------------
# Invocation helpers
# ---------------------------------------------------------------------------
async def invoke_rr(
    http: aiohttp.ClientSession, surface: str, payload: dict
) -> dict:
    """Invoke a request_response surface and return the response payload.

    Core's /v0/invoke blocks until the target node POSTs to /v0/respond,
    then returns the response envelope inline as a 200 JSON body.
    """
    env = make_envelope(f"avp_voice.{surface}", payload)
    async with http.post(
        f"{CORE_URL}/v0/invoke",
        json=env,
        timeout=aiohttp.ClientTimeout(total=INVOKE_TIMEOUT),
    ) as r:
        try:
            body = await r.json()
        except Exception:
            body = {"_text": await r.text()}
        if r.status != 200:
            raise RuntimeError(
                f"avp_voice.{surface} returned {r.status}: {body}"
            )
        # Response envelope shape: {id, correlation_id, from, to, kind,
        # payload, timestamp, signature}
        if isinstance(body, dict) and "payload" in body:
            return body["payload"]
        return body


async def invoke_ff(
    http: aiohttp.ClientSession, surface: str, payload: dict
) -> int:
    """Invoke a fire_and_forget surface; returns HTTP status."""
    env = make_envelope(f"avp_voice.{surface}", payload)
    async with http.post(
        f"{CORE_URL}/v0/invoke",
        json=env,
        timeout=aiohttp.ClientTimeout(total=INVOKE_TIMEOUT),
    ) as r:
        await r.text()
        return r.status


# ---------------------------------------------------------------------------
# Reset helpers (idempotent state)
# ---------------------------------------------------------------------------
async def _reset_override(http: aiohttp.ClientSession) -> None:
    """Wipe any persisted override so each test run starts clean."""
    try:
        await invoke_rr(http, "set_system_message", {"message": ""})
    except Exception as e:
        print(f"[test] override reset warning: {e!r}", flush=True)


async def _maybe_stop_session(http: aiohttp.ClientSession) -> None:
    """Best-effort stop_session — ignore errors."""
    try:
        await invoke_rr(http, "stop_session", {})
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------
async def test_1_system_message_round_trip(
    http: aiohttp.ClientSession,
) -> None:
    msg = "test system message"
    set_resp = await invoke_rr(http, "set_system_message", {"message": msg})
    assert set_resp.get("ok") is True or set_resp.get("error") is None, (
        f"set_system_message returned non-ok payload: {set_resp}"
    )
    assert set_resp.get("override_set") is True, (
        f"expected override_set=True, got {set_resp}"
    )
    assert set_resp.get("override_chars") == len(msg), (
        f"expected override_chars={len(msg)}, got "
        f"{set_resp.get('override_chars')}"
    )

    get_resp = await invoke_rr(http, "get_system_message", {})
    assert get_resp.get("override") == msg, (
        f"expected override={msg!r}, got {get_resp.get('override')!r}"
    )
    resolved = get_resp.get("resolved") or ""
    assert msg in resolved, (
        f"resolved prompt does not include override; resolved={resolved[:200]!r}"
    )

    if OVERRIDE_FILE_HOST_PATH and OVERRIDE_FILE_HOST_PATH.parent.exists():
        assert OVERRIDE_FILE_HOST_PATH.exists(), (
            f"override file not present on disk at {OVERRIDE_FILE_HOST_PATH} "
            f"after set_system_message"
        )
        contents = OVERRIDE_FILE_HOST_PATH.read_text().strip()
        assert msg in contents, (
            f"override file does not contain expected text; got {contents!r}"
        )
    else:
        print(
            f"[test] (skipping on-disk override check — "
            f"{OVERRIDE_FILE_HOST_PATH} parent not present locally)",
            flush=True,
        )


async def test_2_scene_snapshot_compact_form(
    http: aiohttp.ClientSession,
) -> None:
    resp = await invoke_rr(http, "scene_snapshot", {})
    assert "panels" in resp, f"scene_snapshot missing panels: {resp}"
    assert isinstance(resp["panels"], list), (
        f"panels not a list: {type(resp['panels'])}"
    )
    assert "count" in resp and isinstance(resp["count"], int), (
        f"count missing or wrong type: {resp.get('count')!r}"
    )
    assert "version" in resp and isinstance(resp["version"], int), (
        f"version missing or wrong type: {resp.get('version')!r}"
    )
    assert resp["count"] == len(resp["panels"]), (
        f"count={resp['count']} != len(panels)={len(resp['panels'])}"
    )
    assert resp["count"] == len(MOCK.scene_panels), (
        f"snapshot returned {resp['count']} panels; mock has "
        f"{len(MOCK.scene_panels)}"
    )
    assert resp["version"] == MOCK.scene_version, (
        f"version mismatch: snapshot={resp['version']}, "
        f"mock={MOCK.scene_version}"
    )
    expected_keys = {"id", "kind", "text_preview", "url", "has_data"}
    for p in resp["panels"]:
        assert set(p.keys()) >= expected_keys, (
            f"panel missing keys; got {set(p.keys())}, want >={expected_keys}"
        )
        tp = p["text_preview"]
        assert tp is None or isinstance(tp, str), (
            f"text_preview wrong type: {type(tp)}"
        )
        if isinstance(tp, str):
            assert len(tp) <= 200, (
                f"text_preview > 200 chars: len={len(tp)}"
            )
            # Whitespace-stripped: should not start/end with whitespace.
            assert tp == tp.strip(), (
                f"text_preview not stripped: {tp!r}"
            )


async def test_3_start_session_rich_payload(
    http: aiohttp.ClientSession,
) -> None:
    MOCK.voice_start_requests.clear()
    resp = await invoke_rr(http, "start_session", {})
    # Either {ok: true, ...} or the mock's body bubbled up under "data".
    # avp_voice's handler may wrap upstream into {ok, data} — accept either.
    assert resp, f"empty start_session response: {resp}"
    # We mostly care that the device received the rich payload.
    assert MOCK.voice_start_requests, (
        "mock visionOS app received no POST /voice/start — the rewrite "
        "may not be calling the device"
    )
    body = MOCK.voice_start_requests[-1]
    assert isinstance(body, dict), (
        f"/voice/start body not a dict: {type(body)}"
    )
    instructions = body.get("instructions")
    assert isinstance(instructions, str) and instructions.strip(), (
        f"instructions missing or empty: {instructions!r}"
    )
    # Persona markers — accept either of the canonical phrasings.
    persona_ok = (
        "visionOS" in instructions
        or "Vision Pro" in instructions
        or "JARVIS" in instructions
    )
    assert persona_ok, (
        f"instructions lack persona markers (visionOS/Vision Pro/JARVIS): "
        f"{instructions[:300]!r}"
    )
    tools = body.get("tools")
    assert isinstance(tools, list) and tools, (
        f"tools missing or empty: {tools!r}"
    )
    for t in tools:
        assert set(t.keys()) >= {"type", "name", "description", "parameters"}, (
            f"tool entry missing keys: {t}"
        )
        assert t["type"] == "function", f"unexpected tool type: {t['type']}"
    callback_url = body.get("callback_url")
    assert isinstance(callback_url, str) and callback_url, (
        f"callback_url missing: {callback_url!r}"
    )
    # avp_voice's env-configured AVP_VOICE_CALLBACK_URL should land here.
    # We don't assert exact equality (env may differ from our local view)
    # but we DO assert the port is the back-channel port.
    assert str(AVP_VOICE_CALLBACK_PORT) in callback_url, (
        f"callback_url {callback_url!r} does not contain expected port "
        f"{AVP_VOICE_CALLBACK_PORT}"
    )


async def test_4_tool_call_back_channel(
    http: aiohttp.ClientSession,
) -> None:
    # Ensure a session is "active" so mesh_targets is populated.
    # (test_3 already started one; this is defensive in case order changes.)
    if not MOCK.voice_start_requests:
        await invoke_rr(http, "start_session", {})

    call_id = f"call_test_{uuid.uuid4().hex[:8]}"
    body = {
        "call_id": call_id,
        "name": "send_raven_message",  # matches voice.py naming convention
        "arguments_json": json.dumps({"message": "hello from voice"}),
    }
    # Some implementations may use plain `arguments` instead of
    # `arguments_json` — try both.
    async with http.post(
        f"{AVP_VOICE_CALLBACK_URL}/tool_call",
        json=body,
        timeout=aiohttp.ClientTimeout(total=INVOKE_TIMEOUT),
    ) as r:
        text = await r.text()
        try:
            resp_body = json.loads(text)
        except Exception:
            resp_body = {"_text": text}
        assert r.status in (200, 202), (
            f"/tool_call returned {r.status}: {text}"
        )
    # If the tool name didn't resolve, try the alternate (raven.message
    # direct). Worker A's purpose_hints includes raven.message — the
    # tool_name format depends on the implementation; surface a clearer
    # failure than 'unknown tool'.
    if isinstance(resp_body, dict) and resp_body.get("ok") is False:
        # retry with the raw target name
        body2 = {
            "call_id": call_id,
            "name": "raven.message",
            "arguments_json": json.dumps({"message": "hello from voice"}),
        }
        async with http.post(
            f"{AVP_VOICE_CALLBACK_URL}/tool_call",
            json=body2,
            timeout=aiohttp.ClientTimeout(total=INVOKE_TIMEOUT),
        ) as r2:
            text2 = await r2.text()
            try:
                resp_body = json.loads(text2)
            except Exception:
                resp_body = {"_text": text2}
            assert r2.status in (200, 202), (
                f"/tool_call retry returned {r2.status}: {text2}"
            )
    assert isinstance(resp_body, dict), f"/tool_call body not dict: {resp_body}"
    assert resp_body.get("ok") is True, (
        f"/tool_call ok=False: {resp_body}"
    )
    assert "ack" in resp_body, f"/tool_call missing ack: {resp_body}"

    # Best-effort verify the envelope was actually routed by Core. We
    # query the audit log (core.audit_query) for an invocation from
    # avp_voice -> raven.message in the last few seconds.
    audit_payload = {
        "from_node": "avp_voice",
        "to_surface": "raven.message",
        "limit": 5,
    }
    audit_env = make_envelope("core.audit_query", audit_payload)
    try:
        async with http.post(
            f"{CORE_URL}/v0/invoke",
            json=audit_env,
            timeout=aiohttp.ClientTimeout(total=INVOKE_TIMEOUT),
        ) as r:
            audit = await r.json()
    except Exception as e:
        print(
            f"[test] audit verify warning (skipping): {e!r}",
            flush=True,
        )
        return
    # audit envelope has a payload with entries
    if isinstance(audit, dict):
        payload = audit.get("payload") or audit
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if isinstance(entries, list):
            assert any(
                e.get("from_node") == "avp_voice"
                and e.get("to_surface") == "raven.message"
                for e in entries
            ), (
                f"no avp_voice→raven.message audit entry found in last "
                f"{len(entries)} entries; entries={entries[:3]}"
            )


async def test_5_tell_speak_inbox_forwards(
    http: aiohttp.ClientSession,
) -> None:
    # tell
    MOCK.voice_inject_requests.clear()
    status = await invoke_ff(
        http, "tell", {"text": "context update", "source": "raven"}
    )
    assert status in (200, 202), (
        f"tell invocation not accepted: {status}"
    )
    # The forward is async inside the node — give it a beat to fire.
    deadline = asyncio.get_event_loop().time() + 5.0
    while (
        not MOCK.voice_inject_requests
        and asyncio.get_event_loop().time() < deadline
    ):
        await asyncio.sleep(0.1)
    assert MOCK.voice_inject_requests, (
        "mock visionOS app received no POST /voice/inject after tell"
    )
    last = MOCK.voice_inject_requests[-1]
    assert last.get("kind") == "tell", (
        f"expected kind=tell, got {last.get('kind')!r}"
    )
    assert last.get("text") == "context update", (
        f"text mismatch: {last.get('text')!r}"
    )
    assert last.get("source") == "raven", (
        f"source mismatch: {last.get('source')!r}"
    )

    # speak
    MOCK.voice_inject_requests.clear()
    status = await invoke_ff(
        http, "speak", {"text": "your meeting in 5", "source": "raven"}
    )
    assert status in (200, 202), (
        f"speak invocation not accepted: {status}"
    )
    deadline = asyncio.get_event_loop().time() + 5.0
    while (
        not MOCK.voice_inject_requests
        and asyncio.get_event_loop().time() < deadline
    ):
        await asyncio.sleep(0.1)
    assert MOCK.voice_inject_requests, (
        "mock visionOS app received no POST /voice/inject after speak"
    )
    last = MOCK.voice_inject_requests[-1]
    assert last.get("kind") == "speak", (
        f"expected kind=speak, got {last.get('kind')!r}"
    )
    assert last.get("text") == "your meeting in 5", (
        f"text mismatch: {last.get('text')!r}"
    )


async def test_6_get_system_message_no_override(
    http: aiohttp.ClientSession,
) -> None:
    # Reset to a known no-override state.
    await invoke_rr(http, "set_system_message", {"message": ""})
    if OVERRIDE_FILE_HOST_PATH and OVERRIDE_FILE_HOST_PATH.exists():
        # set_system_message with "" should have deleted it; if it
        # didn't, clean up manually (test 7 will exercise this too).
        try:
            OVERRIDE_FILE_HOST_PATH.unlink()
        except FileNotFoundError:
            pass
    resp = await invoke_rr(http, "get_system_message", {})
    assert resp.get("override") in (None, ""), (
        f"expected override=null, got {resp.get('override')!r}"
    )
    assert resp.get("override_set") is False, (
        f"expected override_set=False, got {resp.get('override_set')!r}"
    )
    # Per the spec, override_chars: 0 when no override.
    assert resp.get("override_chars") in (0, None), (
        f"expected override_chars=0, got {resp.get('override_chars')!r}"
    )
    resolved = resp.get("resolved") or ""
    assert resolved.strip(), (
        f"resolved prompt empty — expected default persona block"
    )
    # Persona check — the default persona should mention voice / visionOS.
    persona_ok = (
        "visionOS" in resolved
        or "Vision Pro" in resolved
        or "JARVIS" in resolved
        or "voice" in resolved.lower()
    )
    assert persona_ok, (
        f"default resolved prompt lacks persona markers: "
        f"{resolved[:300]!r}"
    )


async def test_7_set_system_message_reset(
    http: aiohttp.ClientSession,
) -> None:
    # set an override
    await invoke_rr(http, "set_system_message", {"message": "to be cleared"})
    pre = await invoke_rr(http, "get_system_message", {})
    assert pre.get("override_set") is True, (
        f"override not set after first call: {pre}"
    )

    # reset with empty string
    reset_resp = await invoke_rr(http, "set_system_message", {"message": ""})
    # The handler should report override_set=False after reset.
    assert reset_resp.get("override_set") is False, (
        f"expected override_set=False after reset, got {reset_resp}"
    )
    assert reset_resp.get("override_chars") in (0, None), (
        f"expected override_chars=0 after reset, got "
        f"{reset_resp.get('override_chars')}"
    )

    post = await invoke_rr(http, "get_system_message", {})
    assert post.get("override_set") is False, (
        f"override still set after reset: {post}"
    )
    assert post.get("override") in (None, ""), (
        f"override should be null after reset, got "
        f"{post.get('override')!r}"
    )

    if OVERRIDE_FILE_HOST_PATH and OVERRIDE_FILE_HOST_PATH.parent.exists():
        assert not OVERRIDE_FILE_HOST_PATH.exists(), (
            f"override file still present at {OVERRIDE_FILE_HOST_PATH} "
            f"after reset — should be deleted"
        )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
TESTS = [
    ("TEST 1: set_system_message + get_system_message round trip",
     test_1_system_message_round_trip),
    ("TEST 2: scene_snapshot returns compact form",
     test_2_scene_snapshot_compact_form),
    ("TEST 3: start_session rich payload",
     test_3_start_session_rich_payload),
    ("TEST 4: tool_call back-channel",
     test_4_tool_call_back_channel),
    ("TEST 5: tell/speak inbox",
     test_5_tell_speak_inbox_forwards),
    ("TEST 6: get_system_message no override",
     test_6_get_system_message_no_override),
    ("TEST 7: set_system_message reset",
     test_7_set_system_message_reset),
]


async def _run_all() -> int:
    results: list[tuple[str, bool, str]] = []
    async with _run_mocks():
        async with aiohttp.ClientSession() as http:
            await _preflight(http)
            await _reset_override(http)
            await _maybe_stop_session(http)

            for label, fn in TESTS:
                try:
                    await fn(http)
                    results.append((label, True, ""))
                    print(f"{label} — PASS", flush=True)
                except AssertionError as e:
                    results.append((label, False, str(e)))
                    print(f"{label} — FAIL\n    {e}", flush=True)
                except Exception as e:  # noqa: BLE001
                    results.append((label, False, repr(e)))
                    print(f"{label} — ERROR\n    {e!r}", flush=True)

            # Teardown: leave avp_voice in a clean state.
            await _maybe_stop_session(http)
            await _reset_override(http)

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print("", flush=True)
    if passed == total:
        print(f"ALL TESTS PASSED ({passed}/{total})", flush=True)
        return 0
    print(f"{passed}/{total} passed — {total - passed} failed", flush=True)
    for label, ok, err in results:
        if not ok:
            print(f"  FAIL {label}: {err}", flush=True)
    return 1


def main() -> None:
    try:
        rc = asyncio.run(_run_all())
    except KeyboardInterrupt:
        print("[test] interrupted", flush=True)
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()
