"""avp_voice — RAVEN_AVP voice-control proxy node for the LATTICE mesh.

Registers as 'avp_voice' (actor kind, Docker runtime, host=mac-mini).
Translates signed mesh envelopes into HTTPX calls against the visionOS
app's voice-control HTTP server at AVP_VOICE_BASE_URL (default
http://100.109.10.50:5181 — Tailscale-reachable from the Mac mini).

Surfaces (all request_response tools):
  avp_voice.status         — GET  /voice/status
  avp_voice.start_session  — POST /voice/start  (idempotent)
  avp_voice.stop_session   — POST /voice/stop   (idempotent)

The visionOS app is canonical; this node holds no session state and
only forwards. If the upstream is unreachable (initial smoke test
before the iOS worker deploys), we return a clean error envelope so
the mesh edge stays alive.
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

import aiohttp
import httpx


CORE_URL = os.environ.get("CORE_URL", "http://host.docker.internal:8000").rstrip("/")
AVP_VOICE_BASE_URL = os.environ.get(
    "AVP_VOICE_BASE_URL", "http://100.109.10.50:5181"
).rstrip("/")
NODE_ID = "avp_voice"

SECRET_RAW = os.environ.get("AVP_VOICE_SECRET")
if not SECRET_RAW:
    print("[avp_voice] FATAL: AVP_VOICE_SECRET not set", file=sys.stderr)
    sys.exit(1)
SECRET = SECRET_RAW.encode()

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 15.0


# ---------------------------------------------------------------------------
# Envelope helpers (copied verbatim from avp_node.py for parity)
# ---------------------------------------------------------------------------
def canonical(env: dict) -> bytes:
    body = {k: v for k, v in env.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def sign(env: dict) -> str:
    return hmac.new(SECRET, canonical(env), hashlib.sha256).hexdigest()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Upstream call wrapper — normalizes httpx exceptions into error envelopes.
# ---------------------------------------------------------------------------
async def _call_upstream(
    http: httpx.AsyncClient, method: str, path: str, json_body: dict | None = None
) -> dict:
    try:
        if method == "GET":
            r = await http.get(path)
        else:
            r = await http.post(path, json=json_body)
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
async def handle_status(http: httpx.AsyncClient, env: dict) -> dict:
    return await _call_upstream(http, "GET", "/voice/status")


async def handle_start_session(http: httpx.AsyncClient, env: dict) -> dict:
    payload = env.get("payload") or {}
    body: dict = {}
    if "model" in payload:
        body["model"] = payload["model"]
    return await _call_upstream(http, "POST", "/voice/start", body or None)


async def handle_stop_session(http: httpx.AsyncClient, env: dict) -> dict:
    return await _call_upstream(http, "POST", "/voice/stop", None)


# ---------------------------------------------------------------------------
# Mesh I/O — reply to request_response invocations via /v0/respond.
# ---------------------------------------------------------------------------
async def send_response(
    session: aiohttp.ClientSession,
    env_in: dict,
    payload: dict,
    kind: str = "response",
) -> None:
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
                    f"[avp_voice] respond corr={str(corr)[:8]} status={r.status} body={body[:200]}",
                    file=sys.stderr,
                    flush=True,
                )
    except Exception as e:  # noqa: BLE001
        print(f"[avp_voice] send_response crash: {e!r}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
async def dispatch(
    mesh: aiohttp.ClientSession,
    http: httpx.AsyncClient,
    env: dict,
) -> None:
    to = env.get("to", "")
    sender = env.get("from", "?")
    _, _, surface = to.partition(".")
    if surface == "status":
        result = await handle_status(http, env)
    elif surface == "start_session":
        result = await handle_start_session(http, env)
    elif surface == "stop_session":
        result = await handle_stop_session(http, env)
    else:
        print(
            f"[avp_voice] dispatch: unknown surface {surface!r}",
            file=sys.stderr,
            flush=True,
        )
        return
    print(
        f"[avp_voice] {surface} from={sender} ok={result.get('ok', False)}",
        flush=True,
    )
    await send_response(mesh, env, result)


# ---------------------------------------------------------------------------
# Main loop — register + drain SSE
# ---------------------------------------------------------------------------
async def main() -> None:
    timeout = httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=READ_TIMEOUT, pool=READ_TIMEOUT)
    async with aiohttp.ClientSession() as mesh, httpx.AsyncClient(
        base_url=AVP_VOICE_BASE_URL, timeout=timeout
    ) as http:
        reg = {"node_id": NODE_ID, "timestamp": now_iso()}
        reg["signature"] = sign(reg)
        async with mesh.post(f"{CORE_URL}/v0/register", json=reg) as r:
            if r.status != 200:
                print(
                    f"[avp_voice] register failed: {r.status} {await r.text()}",
                    file=sys.stderr,
                )
                sys.exit(1)
            reg_resp = await r.json()
        session_id = reg_resp["session_id"]
        print(
            f"[avp_voice] registered session={session_id[:8]} upstream={AVP_VOICE_BASE_URL}",
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
                                f"[avp_voice] handler crashed: {e!r}",
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
