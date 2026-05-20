"""avp_voice — voice-control peer for the visionOS Vision Pro app.

This node is NOT a dumb proxy. It composes the OpenAI Realtime session
configuration (persona + mesh-tool function list + live scene snapshot +
operator override), POSTs that rich payload to the on-device visionOS
HTTP server, and serves a private back-channel HTTP listener that the
device calls back to when the model invokes a tool.

Session state still lives on the visionOS app — we hold no `is_active`
bit here. We DO cache the per-session mesh-target table (built from
/v0/introspect at start_session time) so /tool_call lookups can resolve
function names to mesh edges without another round-trip.

Surfaces (mesh-side):
  status              tool  request_response   GET  /voice/status
  start_session       tool  request_response   composes + POST /voice/start
  stop_session        tool  request_response   POST /voice/stop
  session_status      tool  request_response   alias for status
  get_system_message  tool  request_response   {resolved, override, override_set, session_active}
  set_system_message  tool  request_response   persist to /data, return note
  scene_snapshot      tool  request_response   LLM-friendly scene digest
  speak               inbox fire_and_forget    POST /voice/inject {kind:"speak",…}
  tell                inbox fire_and_forget    POST /voice/inject {kind:"tell",…}

Private HTTP back-channel (aiohttp on 0.0.0.0:5182):
  POST /tool_call  body {call_id,name,arguments_json} → resolves via cached
                   mesh_targets, mesh-invokes the target, returns the ack
                   body as JSON so the on-device session can splice it into
                   the Realtime conversation as a function_call_output.
  GET  /healthz    {ok: true}
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import json
import os
import pathlib
import sys
import uuid
from typing import Optional

import aiohttp
from aiohttp import web
import httpx


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
NODE_ID = "avp_voice"

CORE_URL = os.environ.get("CORE_URL", "http://host.docker.internal:8000").rstrip("/")
AVP_VOICE_BASE_URL = os.environ.get(
    "AVP_VOICE_BASE_URL", "http://100.109.10.50:5181"
).rstrip("/")
AVP_SCENE_URL = os.environ.get(
    "AVP_SCENE_URL", "http://host.docker.internal:5180"
).rstrip("/")
AVP_VOICE_CALLBACK_URL = os.environ.get(
    "AVP_VOICE_CALLBACK_URL", "http://100.109.10.50:5182"
).rstrip("/")
AVP_VOICE_CALLBACK_PORT = int(os.environ.get("AVP_VOICE_CALLBACK_PORT", "5182"))

SECRET_RAW = os.environ.get("AVP_VOICE_SECRET")
if not SECRET_RAW:
    print("[avp_voice] FATAL: AVP_VOICE_SECRET not set", file=sys.stderr, flush=True)
    sys.exit(1)
SECRET = SECRET_RAW.encode()

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 15.0

# Persisted operator override for the system prompt — survives container
# restart so set_system_message tweaks aren't lost on bounce. The /data
# directory is bind-mounted from the host in docker-compose.yml.
SYSTEM_OVERRIDE_PATH = pathlib.Path("/data/avp_voice_system_override.txt")


# ---------------------------------------------------------------------------
# Envelope helpers — verbatim from voice.py / avp_node.py for parity.
# ---------------------------------------------------------------------------
def canonical(env: dict) -> bytes:
    body = {k: v for k, v in env.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def sign(env: dict) -> str:
    return hmac.new(SECRET, canonical(env), hashlib.sha256).hexdigest()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Disk persist for the operator system-prompt override.
# ---------------------------------------------------------------------------
def load_system_override() -> Optional[str]:
    try:
        if SYSTEM_OVERRIDE_PATH.exists():
            txt = SYSTEM_OVERRIDE_PATH.read_text().strip()
            return txt or None
    except Exception as e:  # noqa: BLE001
        print(f"[avp_voice] system override read failed: {e!r}",
              file=sys.stderr, flush=True)
    return None


def save_system_override(text: Optional[str]) -> None:
    SYSTEM_OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if text is None or not str(text).strip():
        try:
            SYSTEM_OVERRIDE_PATH.unlink()
        except FileNotFoundError:
            pass
        return
    SYSTEM_OVERRIDE_PATH.write_text(str(text).strip() + "\n")


# ---------------------------------------------------------------------------
# Per-target purpose hints used to compose the AVP-side voice system prompt.
# Tailored for the visionOS context (panel manipulation + mesh handoff).
# ---------------------------------------------------------------------------
_PURPOSE_HINTS: dict[tuple[str, str], str] = {
    ("avp", "list_panels"):
        "Query the AVP scene contents. Use when Colton asks what's in his "
        "view or wants to reference a specific panel id.",
    ("avp", "add_panel"):
        "Structured panel insertion. Use when Colton asks for a specific "
        "panel kind (chart, model3d, image, html, etc.). Replies with the "
        "new panel id.",
    ("avp", "show"):
        "Drop a simple text/markdown panel into the scene. The 80% surface "
        "for \"throw this on screen.\" Fire-and-forget — no id returned.",
    ("avp", "remove_panel"):
        "Remove a specific panel by id. Only call when Colton names a "
        "panel to drop.",
    ("avp", "update_panel"):
        "Edit a panel in-place: change its text, transform, or size. Pass "
        "{id, patch:{…}}.",
    ("avp", "clear_scene"):
        "Wipe the whole scene. Confirm before calling — destructive.",
    ("raven", "message"):
        "RAVEN agent (Mac mini). Hand off tasks needing reasoning, code, "
        "iMessage to Colton, or background work over minutes-to-hours.",
    ("edith", "chat"):
        "EDITH (Sonnet 4.6 daily-driver). Quick conversational asks, "
        "drafting, summarization, opinions. Reply arrives as a separate "
        "`tell` injection — do not wait synchronously.",
    ("control", "message"):
        "Colton's dashboard inbox. Surface alerts, status, or asks that "
        "should appear in his control-panel UI.",
    ("browser", "query"):
        "Browser-automation agent. Live web lookups, scraping, anything "
        "needing a real browser. Reply arrives separately.",
}


def _purpose_hint(node: str, surface: str) -> str:
    return _PURPOSE_HINTS.get(
        (node, surface),
        f"({node}.{surface} — purpose not annotated)",
    )


# ---------------------------------------------------------------------------
# Mesh helpers — invoke, respond.
# ---------------------------------------------------------------------------
async def mesh_invoke(mesh: aiohttp.ClientSession, to: str,
                      payload: dict) -> tuple[bool, str]:
    msg_id = str(uuid.uuid4())
    env = {
        "id": msg_id,
        # Pre-fill correlation_id BEFORE signing — Core's _route_invocation
        # calls env.setdefault("correlation_id", id) pre-verify; if we omit
        # it Core mutates the body and HMAC fails -> 401 bad_signature.
        "correlation_id": msg_id,
        "from": NODE_ID,
        "to": to,
        "kind": "invocation",
        "payload": payload,
        "timestamp": now_iso(),
    }
    env["signature"] = sign(env)
    try:
        async with mesh.post(f"{CORE_URL}/v0/invoke", json=env) as r:
            body = await r.text()
            return r.status in (200, 202), body
    except Exception as e:  # noqa: BLE001
        return False, f"mesh_invoke crash: {e!r}"


async def send_response(mesh: aiohttp.ClientSession, env_in: dict,
                        payload: dict, kind: str = "response") -> None:
    corr = env_in.get("correlation_id") or env_in.get("id")
    resp = {
        "id": str(uuid.uuid4()),
        "correlation_id": corr,
        "from": NODE_ID,
        "to": env_in.get("from"),
        "kind": kind,
        "payload": payload,
        "timestamp": now_iso(),
    }
    resp["signature"] = sign(resp)
    try:
        async with mesh.post(f"{CORE_URL}/v0/respond", json=resp) as r:
            body = await r.text()
            if r.status not in (200, 202):
                print(
                    f"[avp_voice] respond corr={str(corr)[:8]} "
                    f"status={r.status} body={body[:200]}",
                    file=sys.stderr, flush=True,
                )
    except Exception as e:  # noqa: BLE001
        print(f"[avp_voice] send_response crash: {e!r}",
              file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Mesh-tool builder — port of voice.py _build_mesh_tools(), filtered to
# edges where from == avp_voice.
# ---------------------------------------------------------------------------
async def build_mesh_tools(
    mesh: aiohttp.ClientSession,
) -> tuple[dict, list[dict]]:
    try:
        async with mesh.get(f"{CORE_URL}/v0/introspect") as r:
            data = await r.json()
    except Exception as e:  # noqa: BLE001
        print(f"[avp_voice] introspect failed; running tool-less: {e!r}",
              file=sys.stderr, flush=True)
        return {}, []
    node_index = {n["id"]: n for n in data.get("nodes", [])}
    targets: dict = {}
    tools: list[dict] = []
    for edge in data.get("relationships", []):
        if edge.get("from") != NODE_ID:
            continue
        target = edge.get("to", "")
        target_node, _, surface_name = target.partition(".")
        if target_node == NODE_ID:
            continue
        ndecl = node_index.get(target_node, {})
        sdecl = next(
            (s for s in ndecl.get("surfaces", []) if s["name"] == surface_name),
            {},
        )
        stype = sdecl.get("type")
        mode = sdecl.get("invocation_mode")
        tool_name = f"send_{target_node}_{surface_name}"
        if stype == "inbox":
            desc = (
                f"Send a free-form text message to {target}'s inbox "
                f"({ndecl.get('kind','node')}). Fire-and-forget — no "
                "immediate reply. Use this to hand off a task that needs "
                "reasoning, code, or external action beyond what you can "
                "do as the AVP voice."
            )
            params = {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Message body — phrase as a task or question.",
                    },
                },
                "required": ["message"],
            }
        elif stype == "tool" and mode == "request_response":
            desc = (
                f"Invoke the {target} tool surface and return its "
                "response. Use only when Colton explicitly asks for "
                "that capability."
            )
            params = {
                "type": "object",
                "properties": {
                    "payload": {
                        "type": "object",
                        "description": "Surface input.",
                    },
                },
                "required": ["payload"],
            }
        else:
            continue
        targets[tool_name] = {
            "target": target, "mode": mode, "type": stype,
            "node": target_node, "surface": surface_name,
        }
        tools.append({
            "type": "function",
            "name": tool_name,
            "description": desc,
            "parameters": params,
        })
    return targets, tools


# ---------------------------------------------------------------------------
# Scene snapshot — pulled from the FastAPI scene server, normalized into a
# compact LLM-friendly form. Used both by build_instructions() at session
# composition time and by the avp_voice.scene_snapshot tool surface.
# ---------------------------------------------------------------------------
async def fetch_scene_raw(scene_http: httpx.AsyncClient) -> dict:
    r = await scene_http.get("/scene")
    r.raise_for_status()
    return r.json()


def _panel_text_preview(panel: dict, limit: int = 200) -> str:
    txt = panel.get("text") or ""
    if not isinstance(txt, str):
        return ""
    return " ".join(txt.split())[:limit]


def compact_scene(scene: dict) -> dict:
    panels_out: list[dict] = []
    for p in scene.get("panels", []) or []:
        panels_out.append({
            "id": p.get("id"),
            "kind": p.get("kind", "text"),
            "text_preview": _panel_text_preview(p),
            "url": p.get("url"),
            "has_data": bool(p.get("data")),
        })
    return {
        "panels": panels_out,
        "count": len(panels_out),
        "version": scene.get("version"),
    }


# ---------------------------------------------------------------------------
# Instructions composition — persona + tool list + scene snapshot + override.
# ---------------------------------------------------------------------------
def build_instructions(
    targets: dict,
    scene_compact: dict,
    override: Optional[str],
) -> str:
    lines: list[str] = [
        "You are the voice surface inside Colton's Vision Pro. He speaks "
        "to you out loud; you reply through the Vision Pro speakers and "
        "you can SEE the panels currently floating around him via the "
        "scene snapshot below. Talk concisely — JARVIS-adjacent: "
        "confident, dry wit, low-fluff. Do not narrate internal steps.",
        "",
        "You can manipulate the scene directly (add, update, remove, list "
        "panels) and hand off heavier work to other mesh nodes (RAVEN, "
        "EDITH, browser). Use tools deliberately, not reflexively.",
        "",
        "Rules:",
        "  1. Default to conversation. Only call a tool when Colton is "
        "     asking for an action or info that requires a node — not "
        "     when he's thinking out loud or asking you to clarify.",
        "  2. At most ONE tool per user turn. Pick the best target.",
        "  3. After a tool call, give a one-line spoken summary of what "
        "     you dispatched. Don't read the full payload back.",
        "  4. Don't fabricate results. Inbox handoffs are fire-and-forget "
        "     — replies may arrive later as a `tell` injection.",
        "  5. Argument shape is {message: <free-form text>} for inbox "
        "     tools and {payload: {…}} for request_response tools.",
    ]

    if targets:
        lines.append("")
        lines.append("Available mesh tools (and what each is FOR):")
        for name, info in targets.items():
            purpose = _purpose_hint(info["node"], info["surface"])
            arrow = "inbox" if info["type"] == "inbox" else "tool"
            lines.append(
                f"  - {name} → {info['node']}.{info['surface']} ({arrow}): {purpose}"
            )
        lines.append("")
        lines.append("Examples:")
        lines.append(
            "  - \"throw a markdown panel up with my todo list\" → send_avp_show"
        )
        lines.append(
            "  - \"what's in my scene right now\" → consult the scene "
            "snapshot below; no tool needed unless he wants a fresh fetch"
        )
        lines.append(
            "  - \"ask raven to ship that PR\" → send_raven_message"
        )
        lines.append(
            "  - \"clear everything\" → send_avp_clear_scene (confirm first)"
        )
        lines.append(
            "  - \"thanks\" → NO TOOL, just acknowledge"
        )
    else:
        lines.append("")
        lines.append("(No mesh tools available — conversational only.)")

    lines.append("")
    lines.append("Currently visible in the scene:")
    panels = scene_compact.get("panels") or []
    if not panels:
        lines.append("  (scene is empty)")
    else:
        for p in panels:
            tag = f"{p.get('id')} ({p.get('kind')})"
            preview = p.get("text_preview") or ""
            url = p.get("url")
            if url:
                lines.append(f"  - panel {tag}: {url}")
            elif preview:
                lines.append(f"  - panel {tag}: {preview}")
            else:
                lines.append(f"  - panel {tag}")
    version = scene_compact.get("version")
    if version is not None:
        lines.append(f"  (scene version: {version})")

    if override and override.strip():
        lines.append("")
        lines.append("Operator-supplied instructions (override / additional):")
        lines.append(override.strip())

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Node — module-level state. Single global because this process owns exactly
# one mesh edge and one back-channel listener.
# ---------------------------------------------------------------------------
class AvpVoiceNode:
    def __init__(self) -> None:
        self.system_override: Optional[str] = load_system_override()
        # Cached mesh-target dict from the most recent start_session. The
        # /tool_call back-channel handler looks function names up here.
        self.mesh_targets: dict = {}
        # Last (and current) session_id reported by the visionOS app, kept
        # for log/debug parity with voice.py — not used to gate behavior.
        self.last_session_id: Optional[str] = None
        # References to the long-lived client sessions, populated in main().
        self.mesh: Optional[aiohttp.ClientSession] = None
        self.device_http: Optional[httpx.AsyncClient] = None
        self.scene_http: Optional[httpx.AsyncClient] = None


node = AvpVoiceNode()


# ---------------------------------------------------------------------------
# Upstream wrapper — normalize httpx errors into uniform mesh replies.
# ---------------------------------------------------------------------------
async def _call_device(
    method: str, path: str, json_body: dict | None = None
) -> dict:
    assert node.device_http is not None
    try:
        if method == "GET":
            r = await node.device_http.get(path)
        else:
            r = await node.device_http.post(path, json=json_body)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
        return {"ok": False, "error": "upstream_unreachable", "detail": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "http_crash", "detail": repr(e)}
    if r.status_code >= 400:
        return {
            "ok": False,
            "error": "upstream_http",
            "status": r.status_code,
            "detail": r.text[:300],
        }
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        data = r.text
    return {"ok": True, "data": data}


# ---------------------------------------------------------------------------
# Surface handlers
# ---------------------------------------------------------------------------
async def handle_status(env: dict) -> dict:
    return await _call_device("GET", "/voice/status")


async def handle_start_session(env: dict) -> dict:
    # Compose the rich payload: persona+tools+scene+override → push to device.
    assert node.mesh is not None and node.scene_http is not None
    targets, tools = await build_mesh_tools(node.mesh)
    try:
        scene_raw = await fetch_scene_raw(node.scene_http)
        scene_compact = compact_scene(scene_raw)
    except Exception as e:  # noqa: BLE001
        print(f"[avp_voice] scene fetch failed during start_session: {e!r}",
              file=sys.stderr, flush=True)
        scene_compact = {"panels": [], "count": 0, "version": None}
    instructions = build_instructions(targets, scene_compact, node.system_override)
    # Cache targets so /tool_call can resolve names without re-introspecting.
    node.mesh_targets = targets

    body = {
        "instructions": instructions,
        "tools": tools,
        "avp_voice_callback_url": AVP_VOICE_CALLBACK_URL,
    }
    # Optional caller passthroughs from the mesh envelope.
    payload_in = env.get("payload") or {}
    if "model" in payload_in:
        body["model"] = payload_in["model"]
    if "voice" in payload_in:
        body["voice"] = payload_in["voice"]

    result = await _call_device("POST", "/voice/start", body)
    if result.get("ok"):
        data = result.get("data")
        if isinstance(data, dict):
            sid = data.get("session_id")
            if isinstance(sid, str):
                node.last_session_id = sid
        # Include the composed details so callers (mesh-side ops, RAVEN,
        # inspector) can see what was actually sent without re-running it.
        result["composed"] = {
            "instruction_chars": len(instructions),
            "tool_count": len(tools),
            "tool_names": [t["name"] for t in tools],
            "scene_panel_count": scene_compact.get("count", 0),
            "scene_version": scene_compact.get("version"),
            "callback_url": AVP_VOICE_CALLBACK_URL,
        }
    return result


async def handle_stop_session(env: dict) -> dict:
    return await _call_device("POST", "/voice/stop", None)


async def handle_session_status(env: dict) -> dict:
    # Alias for status — preserve parity with voice.session_status callers.
    return await _call_device("GET", "/voice/status")


async def handle_get_system_message(env: dict) -> dict:
    # Build the prompt as it WOULD be composed right now. Realtime API
    # doesn't expose a way to read the live session's instructions, so the
    # "resolved" view is always "what the next start_session would send."
    assert node.mesh is not None and node.scene_http is not None
    targets, _ = await build_mesh_tools(node.mesh)
    try:
        scene_raw = await fetch_scene_raw(node.scene_http)
        scene_compact = compact_scene(scene_raw)
    except Exception:
        scene_compact = {"panels": [], "count": 0, "version": None}
    resolved = build_instructions(targets, scene_compact, node.system_override)

    # Session-active probe via device status — best-effort, never blocks.
    session_active = False
    try:
        s = await _call_device("GET", "/voice/status")
        if s.get("ok"):
            data = s.get("data")
            if isinstance(data, dict):
                session_active = (data.get("session") == "active")
    except Exception:
        pass

    return {
        "resolved": resolved,
        "override": node.system_override,
        "override_set": node.system_override is not None,
        "session_active": session_active,
    }


async def handle_set_system_message(env: dict) -> dict:
    payload = env.get("payload") or {}
    # Accept several payload shapes for caller flexibility.
    raw = (
        payload.get("override") if payload.get("override") is not None
        else payload.get("system_message") if payload.get("system_message") is not None
        else payload.get("message") if payload.get("message") is not None
        else payload.get("text")
    )
    if raw is None:
        return {
            "ok": False,
            "error": "missing_override",
            "detail": "send {message|text|override|system_message: <string>} or empty/null to reset",
        }
    if isinstance(raw, str) and raw.strip() == "":
        raw = None  # treat empty string as reset
    try:
        save_system_override(raw)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "persist_failed", "detail": str(e)[:300]}
    node.system_override = raw if isinstance(raw, str) and raw.strip() else None

    # Detect whether a session is live — the note text shifts accordingly.
    session_active = False
    try:
        s = await _call_device("GET", "/voice/status")
        if s.get("ok"):
            data = s.get("data")
            if isinstance(data, dict):
                session_active = (data.get("session") == "active")
    except Exception:
        pass

    if session_active:
        note = (
            "stored. Active session is using the old instructions until "
            "stop_session + start_session — Realtime API does not allow "
            "live mutation of instructions without reconnect. New sessions "
            "pick up the override automatically."
        )
    else:
        note = "stored. Will apply on next start_session."

    return {
        "ok": True,
        "override_set": node.system_override is not None,
        "override_chars": len(node.system_override or ""),
        "note": note,
    }


async def handle_scene_snapshot(env: dict) -> dict:
    assert node.scene_http is not None
    try:
        scene_raw = await fetch_scene_raw(node.scene_http)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "scene_fetch_failed", "detail": repr(e)}
    compact = compact_scene(scene_raw)
    return {"ok": True, **compact}


async def handle_speak(env: dict) -> None:
    payload = env.get("payload") or {}
    text = payload.get("text") or payload.get("message") or ""
    source = payload.get("source") or env.get("from")
    if not isinstance(text, str) or not text.strip():
        print(f"[avp_voice] speak: empty text from {env.get('from')!r}",
              flush=True)
        return
    body = {"kind": "speak", "text": text, "source": source}
    result = await _call_device("POST", "/voice/inject", body)
    if result.get("ok"):
        print(f"[avp_voice] speak forwarded source={source} chars={len(text)}",
              flush=True)
    else:
        print(
            f"[avp_voice] speak forward failed source={source} "
            f"err={result.get('error')} detail={str(result.get('detail'))[:160]}",
            file=sys.stderr, flush=True,
        )


async def handle_tell(env: dict) -> None:
    payload = env.get("payload") or {}
    text = payload.get("text") or payload.get("message") or ""
    source = payload.get("source") or env.get("from")
    if not isinstance(text, str) or not text.strip():
        print(f"[avp_voice] tell: empty text from {env.get('from')!r}",
              flush=True)
        return
    body = {"kind": "tell", "text": text, "source": source}
    result = await _call_device("POST", "/voice/inject", body)
    if result.get("ok"):
        print(f"[avp_voice] tell forwarded source={source} chars={len(text)}",
              flush=True)
    else:
        print(
            f"[avp_voice] tell forward failed source={source} "
            f"err={result.get('error')} detail={str(result.get('detail'))[:160]}",
            file=sys.stderr, flush=True,
        )


# ---------------------------------------------------------------------------
# Mesh dispatch
# ---------------------------------------------------------------------------
async def dispatch(env: dict) -> None:
    to = env.get("to", "")
    sender = env.get("from", "?")
    _, _, surface = to.partition(".")
    assert node.mesh is not None

    # Fire-and-forget inbox handlers: no /v0/respond.
    if surface == "speak":
        await handle_speak(env)
        return
    if surface == "tell":
        await handle_tell(env)
        return

    if surface == "status":
        result = await handle_status(env)
    elif surface == "start_session":
        result = await handle_start_session(env)
    elif surface == "stop_session":
        result = await handle_stop_session(env)
    elif surface == "session_status":
        result = await handle_session_status(env)
    elif surface == "get_system_message":
        result = await handle_get_system_message(env)
    elif surface == "set_system_message":
        result = await handle_set_system_message(env)
    elif surface == "scene_snapshot":
        result = await handle_scene_snapshot(env)
    else:
        print(f"[avp_voice] dispatch: unknown surface {surface!r}",
              file=sys.stderr, flush=True)
        return

    print(f"[avp_voice] {surface} from={sender} ok={result.get('ok', False)}",
          flush=True)
    await send_response(node.mesh, env, result)


# ---------------------------------------------------------------------------
# Private back-channel HTTP server — the visionOS VoiceSession POSTs every
# Realtime function_call here. We resolve the function name through the
# cached mesh_targets, invoke the target, and return the ack body to the
# device so it can splice it back into the Realtime conversation.
# ---------------------------------------------------------------------------
async def tool_call_handler(request: web.Request) -> web.Response:
    assert node.mesh is not None
    try:
        body = await request.json()
    except Exception as e:  # noqa: BLE001
        return web.json_response(
            {"ok": False, "error": "bad_json", "detail": str(e)},
            status=400,
        )

    call_id = body.get("call_id") or ""
    name = body.get("name") or ""
    arguments_json = body.get("arguments_json")
    if arguments_json is None:
        # Tolerate either {arguments_json: str} or {arguments: dict|str}.
        a = body.get("arguments")
        if isinstance(a, dict):
            arguments_json = json.dumps(a)
        elif isinstance(a, str):
            arguments_json = a
        else:
            arguments_json = "{}"

    info = node.mesh_targets.get(name)
    if not info:
        out = {"ok": False, "error": f"unknown tool: {name}"}
        print(f"[avp_voice] /tool_call {call_id[:8]} name={name} -> unknown",
              file=sys.stderr, flush=True)
        return web.json_response({"ok": False, "ack": out})

    try:
        args = json.loads(arguments_json) if arguments_json else {}
    except json.JSONDecodeError:
        args = {}

    try:
        if info["type"] == "inbox":
            text = args.get("message") or args.get("text") or ""
            payload = {
                "from": NODE_ID,
                "message": text,
                "text": text,
                "session_id": node.last_session_id,
                "timestamp": now_iso(),
            }
        else:
            payload = args.get("payload") or {}
        ok, ack = await mesh_invoke(node.mesh, info["target"], payload)
    except Exception as e:  # noqa: BLE001
        ok = False
        ack = f"tool_call crash: {e!r}"

    # Match voice.py's truncation cap so chained tool calls still see ids.
    if isinstance(ack, str) and len(ack) > 16384:
        ack = ack[:16384] + f"…[truncated, total {len(ack)} bytes]"

    print(
        f"[avp_voice] /tool_call {str(call_id)[:8]} name={name} "
        f"target={info['target']} ok={ok}",
        flush=True,
    )
    return web.json_response({
        "ok": ok,
        "call_id": call_id,
        "delivered_to": info["target"],
        "ack": ack,
    })


async def healthz_handler(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def make_back_channel_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/tool_call", tool_call_handler)
    app.router.add_get("/healthz", healthz_handler)
    return app


# ---------------------------------------------------------------------------
# Main loop — register, start back-channel server, drain Core SSE.
# ---------------------------------------------------------------------------
async def main() -> None:
    timeout = httpx.Timeout(
        connect=CONNECT_TIMEOUT,
        read=READ_TIMEOUT,
        write=READ_TIMEOUT,
        pool=READ_TIMEOUT,
    )
    async with aiohttp.ClientSession() as mesh, httpx.AsyncClient(
        base_url=AVP_VOICE_BASE_URL, timeout=timeout
    ) as device_http, httpx.AsyncClient(
        base_url=AVP_SCENE_URL, timeout=timeout
    ) as scene_http:
        node.mesh = mesh
        node.device_http = device_http
        node.scene_http = scene_http

        # Register with Core.
        reg = {"node_id": NODE_ID, "timestamp": now_iso()}
        reg["signature"] = sign(reg)
        async with mesh.post(f"{CORE_URL}/v0/register", json=reg) as r:
            if r.status != 200:
                print(
                    f"[avp_voice] register failed: {r.status} {await r.text()}",
                    file=sys.stderr, flush=True,
                )
                sys.exit(1)
            reg_resp = await r.json()
        session_id = reg_resp["session_id"]
        print(
            f"[avp_voice] registered session={session_id[:8]} "
            f"device={AVP_VOICE_BASE_URL} scene={AVP_SCENE_URL} "
            f"callback={AVP_VOICE_CALLBACK_URL} override_set={node.system_override is not None}",
            flush=True,
        )

        # Spin up the back-channel HTTP server before draining so the
        # device can POST tool_calls as soon as a session opens.
        web_app = make_back_channel_app()
        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", AVP_VOICE_CALLBACK_PORT)
        await site.start()
        print(
            f"[avp_voice] back-channel listening on 0.0.0.0:{AVP_VOICE_CALLBACK_PORT}",
            flush=True,
        )

        try:
            async with mesh.get(
                f"{CORE_URL}/v0/stream",
                params={"session": session_id},
                timeout=aiohttp.ClientTimeout(total=None),
            ) as r:
                event_type: Optional[str] = None
                buf: list[str] = []
                async for raw in r.content:
                    line = raw.decode().rstrip("\r\n")
                    if line == "":
                        if event_type == "deliver" and buf:
                            try:
                                data = json.loads("\n".join(buf))
                                asyncio.create_task(dispatch(data))
                            except Exception as e:  # noqa: BLE001
                                print(
                                    f"[avp_voice] handler crashed: {e!r}",
                                    file=sys.stderr, flush=True,
                                )
                        event_type, buf = None, []
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:"):
                        buf.append(line[5:].lstrip())
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
