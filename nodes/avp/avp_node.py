"""avp — RAVEN_AVP scene-server proxy node for the LATTICE mesh.

Registers as 'avp' (actor kind, Docker runtime, host=mac-mini). Translates
signed mesh envelopes into HTTPX calls against the FastAPI scene server
running on Colton's Mac at AVP_BASE_URL (default
http://100.109.10.50:5180 — Tailscale-reachable from the Mac mini).

Surfaces:
  avp.show          (inbox,  fire_and_forget) — 80% surface. "Put this on
                    screen near X." Builds a panel from the payload and
                    POSTs /scene/panel. No reply.
  avp.add_panel     (tool,   request_response) — like show, but replies
                    {ok, panel}. Use when caller needs the new id.
  avp.update_panel  (tool,   request_response) — POST /scene/panel/{id}
                    with the merge body.
  avp.remove_panel  (tool,   request_response) — PATCH /scene with a
                    JSON Patch remove op (index resolved server-side).
  avp.list_panels   (tool,   request_response) — GET /scene, return the
                    short form [{id, kind, position, size}, …].
  avp.clear_scene   (tool,   request_response) — PATCH /scene replacing
                    /panels with [].

The FastAPI server is canonical; this node holds no scene state.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import json
import math
import os
import sys
import uuid

import aiohttp
import httpx


CORE_URL = os.environ.get("CORE_URL", "http://host.docker.internal:8000").rstrip("/")
AVP_BASE_URL = os.environ.get("AVP_BASE_URL", "http://host.docker.internal:5180").rstrip("/")
NODE_ID = "avp"

SECRET_RAW = os.environ.get("AVP_SECRET")
if not SECRET_RAW:
    print("[avp] FATAL: AVP_SECRET not set", file=sys.stderr)
    sys.exit(1)
SECRET = SECRET_RAW.encode()


# Default panel sizes per kind (meters). Ported verbatim from RAVEN_AVP
# mcp_server.py DEFAULT_SIZES so behavior matches when an LLM routes the
# same intent through either the stdio MCP server or this mesh node.
DEFAULT_SIZES: dict[str, tuple[float, float]] = {
    "text":     (0.5, 0.4),
    "html":     (0.7, 0.5),
    "image":    (0.5, 0.4),
    "markdown": (0.5, 0.45),
    "chart":    (0.5, 0.4),
    "mermaid":  (0.55, 0.45),
    "model3d":  (0.35, 0.35),
    "group":    (0.8, 0.6),
}

# Default landing spot when neither `position` nor `near` is given.
# Spec calls for 1.65m eye height (slightly above mcp_server.py's 1.5m).
DEFAULT_POSITION: list[float] = [0.0, 1.65, -1.3]
DEFAULT_NEAR_OFFSET: list[float] = [0.55, 0.0, 0.0]


# ---------------------------------------------------------------------------
# Envelope helpers (copied verbatim from edith.py / voice.py for parity)
# ---------------------------------------------------------------------------
def canonical(env: dict) -> bytes:
    body = {k: v for k, v in env.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def sign(env: dict) -> str:
    return hmac.new(SECRET, canonical(env), hashlib.sha256).hexdigest()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Panel construction — ports _resolve_position / _size_or_default / _build_panel
# from RAVEN_AVP/server/mcp_server.py so semantics match the stdio MCP path.
# ---------------------------------------------------------------------------
async def _get_scene(http: httpx.AsyncClient) -> dict:
    r = await http.get("/scene")
    r.raise_for_status()
    return r.json()


async def _resolve_position(
    http: httpx.AsyncClient,
    position: list[float] | None,
    near: str | None,
    near_offset: list[float] | None,
) -> tuple[list[float], list[float]]:
    if position is not None:
        return [float(v) for v in position], [0.0, 0.0, 0.0]
    if near is not None:
        scene = await _get_scene(http)
        target = next((p for p in scene.get("panels", []) if p.get("id") == near), None)
        if target is None:
            raise ValueError(f"near: panel {near!r} not found")
        offset = near_offset if near_offset is not None else DEFAULT_NEAR_OFFSET
        tp = target["transform"]["position"]
        rot = target["transform"]["rotation"]
        return (
            [float(tp[0]) + float(offset[0]),
             float(tp[1]) + float(offset[1]),
             float(tp[2]) + float(offset[2])],
            [float(v) for v in rot],
        )
    return list(DEFAULT_POSITION), [0.0, 0.0, 0.0]


def _size_or_default(kind: str, size: list[float] | None) -> dict:
    if size is not None:
        return {"width": float(size[0]), "height": float(size[1])}
    w, h = DEFAULT_SIZES.get(kind, (0.5, 0.4))
    return {"width": w, "height": h}


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _build_panel(http: httpx.AsyncClient, payload: dict) -> dict:
    kind = str(payload.get("kind", "text"))
    pos, rot = await _resolve_position(
        http,
        payload.get("position"),
        payload.get("near"),
        payload.get("near_offset"),
    )
    yaw_deg = payload.get("rotation_yaw_degrees")
    if yaw_deg is not None:
        rot = [rot[0], math.radians(float(yaw_deg)), rot[2]]

    pid = payload.get("id") or _new_id(kind)
    panel: dict = {
        "id": pid,
        "kind": kind,
        "text": payload.get("text", "") or "",
        "transform": {"position": pos, "rotation": rot, "scale": [1.0, 1.0, 1.0]},
        "size": _size_or_default(kind, payload.get("size")),
    }
    if payload.get("url") is not None:
        panel["url"] = str(payload["url"])
    if payload.get("data") is not None:
        # `data` is a JSON-encoded string (chart spec, group members, etc.)
        # If a caller hands us a dict, serialize it for them — the FastAPI
        # server expects a string.
        d = payload["data"]
        panel["data"] = d if isinstance(d, str) else json.dumps(d)
    if payload.get("style") is not None:
        panel["style"] = payload["style"]
    return panel


def _short(panel: dict) -> dict:
    # Include a short content preview so list_panels callers can identify
    # which panel they made without a second round-trip. The preview is
    # capped per-field to keep the response small even on scenes with
    # many large panels; full panel content is still in the SceneDoc.
    PREVIEW_MAX = 120
    kind = panel.get("kind", "text")
    out: dict = {
        "id": panel.get("id"),
        "kind": kind,
        "position": panel.get("transform", {}).get("position"),
        "size": panel.get("size"),
    }
    # Pull the most relevant content field per kind. AVP's Panel schema
    # uses different fields for different kinds (text/markdown -> text,
    # html -> html, image/model3d -> url, chart/group -> data string).
    content_field = {
        "text": "text",
        "markdown": "text",
        "html": "html",
        "image": "url",
        "model3d": "url",
        "chart": "data",
        "mermaid": "text",
        "group": "data",
    }.get(kind)
    if content_field:
        val = panel.get(content_field)
        if isinstance(val, str) and val:
            out["preview"] = val[:PREVIEW_MAX] + ("…" if len(val) > PREVIEW_MAX else "")
    return out


# ---------------------------------------------------------------------------
# Mesh I/O
# ---------------------------------------------------------------------------
async def send_response(
    session: aiohttp.ClientSession,
    env_in: dict,
    payload: dict,
    kind: str = "response",
) -> None:
    """Reply to a request_response invocation via /v0/respond."""
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
        async with session.post(f"{CORE_URL}/v0/respond", json=resp) as r:
            body = await r.text()
            if r.status not in (200, 202):
                print(
                    f"[avp] respond corr={str(corr)[:8]} status={r.status} body={body[:200]}",
                    file=sys.stderr,
                    flush=True,
                )
    except Exception as e:  # noqa: BLE001
        print(f"[avp] send_response crash: {e!r}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Surface handlers
# ---------------------------------------------------------------------------
async def _add_panel_upstream(http: httpx.AsyncClient, panel: dict) -> httpx.Response:
    """Append a panel to the scene. Tries POST /scene/panel first (newer
    upstream), falls back to PATCH /scene with a JSON Patch add op
    (older upstream that predates the /scene/panel POST endpoint)."""
    r = await http.post("/scene/panel", json=panel)
    if r.status_code == 404:
        r = await http.patch(
            "/scene",
            json=[{"op": "add", "path": "/panels/-", "value": panel}],
        )
    return r


async def _merge_panel_upstream(
    http: httpx.AsyncClient, pid: str, body: dict
) -> httpx.Response:
    """Merge a partial update into a panel. Tries POST /scene/panel/{id}
    first (newer upstream), falls back to PATCH /scene with a JSON Patch
    replace op against the current panel object (older upstream)."""
    r = await http.post(f"/scene/panel/{pid}", json=body)
    if r.status_code != 404:
        return r
    # Older upstream: resolve id -> index, fetch the current panel, merge
    # the patch into it, and replace the whole object.
    scene = await _get_scene(http)
    panels = scene.get("panels", [])
    idx = next((i for i, p in enumerate(panels) if p.get("id") == pid), None)
    if idx is None:
        # Return the original 404 — caller will surface it.
        return r
    merged = dict(panels[idx])
    for k, v in body.items():
        if k == "id":
            continue
        if k == "transform" and isinstance(v, dict) and isinstance(merged.get("transform"), dict):
            merged["transform"] = {**merged["transform"], **v}
        elif k == "size" and isinstance(v, dict) and isinstance(merged.get("size"), dict):
            merged["size"] = {**merged["size"], **v}
        else:
            merged[k] = v
    return await http.patch(
        "/scene",
        json=[{"op": "replace", "path": f"/panels/{idx}", "value": merged}],
    )


async def handle_show(http: httpx.AsyncClient, env: dict) -> None:
    """Fire-and-forget: build a panel and add it to the scene. No mesh reply."""
    payload = env.get("payload") or {}
    try:
        panel = await _build_panel(http, payload)
    except Exception as e:  # noqa: BLE001
        print(f"[avp] show build_panel failed: {e!r}", file=sys.stderr, flush=True)
        return
    try:
        r = await _add_panel_upstream(http, panel)
        if r.status_code >= 400:
            print(
                f"[avp] show kind={panel['kind']} id={panel['id']} "
                f"HTTP {r.status_code} body={r.text[:200]}",
                file=sys.stderr,
                flush=True,
            )
            return
        print(f"[avp] show kind={panel['kind']} id={panel['id']}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[avp] show POST crash: {e!r}", file=sys.stderr, flush=True)


async def handle_add_panel(http: httpx.AsyncClient, env: dict) -> dict:
    payload = env.get("payload") or {}
    try:
        panel = await _build_panel(http, payload)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "build_panel_failed", "detail": str(e)}
    try:
        r = await _add_panel_upstream(http, panel)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "http_crash", "detail": str(e)}
    if r.status_code >= 400:
        return {"ok": False, "error": "upstream_error",
                "status": r.status_code, "detail": r.text[:500]}
    return {"ok": True, "panel": panel}


async def handle_update_panel(http: httpx.AsyncClient, env: dict) -> dict:
    payload = env.get("payload") or {}
    pid = payload.get("id")
    if not pid:
        return {"ok": False, "error": "missing_id"}
    # Accept either {id, patch: {…}} (preferred) or {id, …fields} (flat).
    patch = payload.get("patch")
    if patch is None:
        patch = {k: v for k, v in payload.items() if k != "id"}
    if not isinstance(patch, dict):
        return {"ok": False, "error": "patch_must_be_object"}
    body = dict(patch)
    body["id"] = pid
    try:
        r = await _merge_panel_upstream(http, pid, body)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "http_crash", "detail": str(e)}
    if r.status_code >= 400:
        return {"ok": False, "error": "upstream_error",
                "status": r.status_code, "detail": r.text[:500]}
    return {"ok": True, "scene": r.json()}


async def handle_remove_panel(http: httpx.AsyncClient, env: dict) -> dict:
    payload = env.get("payload") or {}
    pid = payload.get("id")
    if not pid:
        return {"ok": False, "error": "missing_id"}
    # Resolve id -> index against the live scene so we can send a JSON
    # Patch remove op (the FastAPI server's PATCH /scene is index-based).
    try:
        scene = await _get_scene(http)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "get_scene_failed", "detail": str(e)}
    panels = scene.get("panels", [])
    idx = next((i for i, p in enumerate(panels) if p.get("id") == pid), None)
    if idx is None:
        return {"ok": False, "error": "not_found", "id": pid}
    try:
        r = await http.patch("/scene", json=[{"op": "remove", "path": f"/panels/{idx}"}])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "http_crash", "detail": str(e)}
    if r.status_code >= 400:
        return {"ok": False, "error": "upstream_error",
                "status": r.status_code, "detail": r.text[:500]}
    return {"ok": True, "removed": pid, "remaining_count": len(panels) - 1}


async def handle_list_panels(http: httpx.AsyncClient, env: dict) -> dict:
    try:
        scene = await _get_scene(http)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "get_scene_failed", "detail": str(e)}
    panels = [_short(p) for p in scene.get("panels", [])]
    return {"ok": True, "panels": panels,
            "version": scene.get("version"), "seq": scene.get("seq")}


async def handle_clear_scene(http: httpx.AsyncClient, env: dict) -> dict:
    try:
        r = await http.patch(
            "/scene",
            json=[{"op": "replace", "path": "/panels", "value": []}],
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "http_crash", "detail": str(e)}
    if r.status_code >= 400:
        return {"ok": False, "error": "upstream_error",
                "status": r.status_code, "detail": r.text[:500]}
    return {"ok": True}


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
async def dispatch(
    mesh: aiohttp.ClientSession,
    http: httpx.AsyncClient,
    env: dict,
) -> None:
    to = env.get("to", "")
    _, _, surface = to.partition(".")
    if surface == "show":
        await handle_show(http, env)
        return
    if surface == "add_panel":
        result = await handle_add_panel(http, env)
    elif surface == "update_panel":
        result = await handle_update_panel(http, env)
    elif surface == "remove_panel":
        result = await handle_remove_panel(http, env)
    elif surface == "list_panels":
        result = await handle_list_panels(http, env)
    elif surface == "clear_scene":
        result = await handle_clear_scene(http, env)
    else:
        print(f"[avp] dispatch: unknown surface {surface!r}", file=sys.stderr, flush=True)
        return
    await send_response(mesh, env, result)


# ---------------------------------------------------------------------------
# Main loop — register + drain SSE
# ---------------------------------------------------------------------------
async def main() -> None:
    async with aiohttp.ClientSession() as mesh, httpx.AsyncClient(
        base_url=AVP_BASE_URL, timeout=10.0
    ) as http:
        reg = {"node_id": NODE_ID, "timestamp": now_iso()}
        reg["signature"] = sign(reg)
        async with mesh.post(f"{CORE_URL}/v0/register", json=reg) as r:
            if r.status != 200:
                print(
                    f"[avp] register failed: {r.status} {await r.text()}",
                    file=sys.stderr,
                )
                sys.exit(1)
            reg_resp = await r.json()
        session_id = reg_resp["session_id"]
        print(
            f"[avp] registered session={session_id[:8]} upstream={AVP_BASE_URL}",
            flush=True,
        )

        async with mesh.get(
            f"{CORE_URL}/v0/stream",
            params={"session": session_id},
            timeout=aiohttp.ClientTimeout(total=None),
        ) as r:
            event_type: str | None = None
            buf: list[str] = []
            async for raw in r.content:
                line = raw.decode().rstrip("\r\n")
                if line == "":
                    if event_type == "deliver" and buf:
                        try:
                            data = json.loads("\n".join(buf))
                            await dispatch(mesh, http, data)
                        except Exception as e:  # noqa: BLE001
                            print(
                                f"[avp] handler crashed: {e!r}",
                                file=sys.stderr,
                                flush=True,
                            )
                    event_type, buf = None, []
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    buf.append(line[5:].lstrip())


if __name__ == "__main__":
    asyncio.run(main())
