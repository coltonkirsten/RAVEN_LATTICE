"""mesh_mcp.py — stdio MCP server exposing this node's outbound mesh edges.

Designed to be spawned by the Claude Code CLI (via --mcp-config). Reads the
LATTICE manifest on every `tools/list` request so newly added edges become
available without restarting the host process (hot reload). Each outbound
edge `from=<NODE_ID>, to=<target>.<surface>` becomes a tool named
`mesh_<target>_<surface>`.

Tool calls translate to a signed HMAC envelope POSTed to Core's
/v0/invoke. Fire-and-forget from the MCP's perspective: the tool returns
the 200/202 ack synchronously; any actual reply lands at this node's
inbox channel via the normal SSE stream (see SPEC §4 / §10).

Wire protocol: plain JSON-RPC 2.0 over stdin/stdout (the MCP transport).
Implemented directly to keep the container free of extra deps; pyyaml is
the only non-stdlib import.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.request
import uuid

import yaml


NODE_ID = os.environ["MESH_NODE_ID"]
CORE_URL = os.environ["CORE_URL"].rstrip("/")
MANIFEST_PATH = os.environ["MANIFEST_PATH"]
SECRET_ENV = os.environ.get("MESH_SECRET_ENV", f"{NODE_ID.upper()}_SECRET")
SECRET_FILE = os.environ.get("MESH_SECRET_FILE")  # optional .env-style file


def _load_secret() -> bytes:
    if SECRET_FILE:
        with open(SECRET_FILE) as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{SECRET_ENV}="):
                    return line.split("=", 1)[1].encode()
        raise RuntimeError(f"{SECRET_ENV} not present in {SECRET_FILE}")
    val = os.environ.get(SECRET_ENV)
    if not val:
        raise RuntimeError(
            f"{SECRET_ENV} not in env and MESH_SECRET_FILE not set"
        )
    return val.encode()


SECRET = _load_secret()


def canonical(env: dict) -> bytes:
    body = {k: v for k, v in env.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def sign(env: dict) -> str:
    return hmac.new(SECRET, canonical(env), hashlib.sha256).hexdigest()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_edges() -> list[tuple[str, str, dict]]:
    """Return [(to_node, surface_name, surface_meta), ...] for edges from NODE_ID.

    `core` is a reserved node not declared in manifest.nodes (SPEC §5) —
    we fall back to a request_response/tool default for any unknown target.
    """
    with open(MANIFEST_PATH) as f:
        manifest = yaml.safe_load(f) or {}
    nodes_by_id = {n["id"]: n for n in manifest.get("nodes") or []}
    out: list[tuple[str, str, dict]] = []
    for rel in manifest.get("relationships") or []:
        if rel.get("from") != NODE_ID:
            continue
        to = rel.get("to") or ""
        if "." not in to:
            continue
        to_node, surface = to.split(".", 1)
        target = nodes_by_id.get(to_node) or {}
        meta = {"name": surface, "type": "tool", "invocation_mode": "request_response"}
        for s in target.get("surfaces") or []:
            if s.get("name") == surface:
                meta = {**meta, **s}
                break
        out.append((to_node, surface, meta))
    return out


def _describe(to_node: str, surface: str, meta: dict) -> str:
    mode = meta.get("invocation_mode", "request_response")
    stype = meta.get("type", "tool")
    if to_node == "control" and surface == "message":
        hint = "Send a message to control's inbox (Colton's dashboard panel)."
    elif surface == "chat":
        hint = f"Send a chat message to {to_node}."
    elif surface == "message":
        hint = f"Send a message to {to_node}'s inbox."
    elif to_node == "core":
        hint = f"Invoke the {surface} surface on Core (mesh broker)."
    else:
        hint = f"Invoke {to_node}.{surface} on the mesh."
    return (
        f"{hint} Target surface: {to_node}.{surface} "
        f"(type={stype}, invocation_mode={mode}). Sends a signed envelope "
        "via LATTICE Core. Fire-and-forget from this tool's perspective: "
        "the return value confirms delivery to Core, not the target's "
        "reply. Any response (if any) arrives via your node's normal "
        "inbox channel."
    )


def build_tools() -> list[dict]:
    tools: list[dict] = []
    for to_node, surface, meta in read_edges():
        tools.append({
            "name": f"mesh_{to_node}_{surface}",
            "description": _describe(to_node, surface, meta),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "payload": {
                        "type": "object",
                        "description": (
                            "The envelope payload — passed verbatim as "
                            "envelope.payload. Schema is per-surface; "
                            "common shape is {\"message\": \"<text>\"}."
                        ),
                        "additionalProperties": True,
                    },
                    "conversation_id": {
                        "type": "string",
                        "description": (
                            "Optional. Threading hint folded into payload."
                            "conversation_id if not already set."
                        ),
                    },
                },
                "required": ["payload"],
                "additionalProperties": False,
            },
        })
    return tools


def invoke_mesh(to_node: str, surface: str, payload: dict) -> tuple[bool, str]:
    msg_id = str(uuid.uuid4())
    env = {
        "id": msg_id,
        # CRITICAL: pre-fill correlation_id BEFORE signing. Core's
        # _route_invocation calls env.setdefault("correlation_id", id)
        # pre-verify; if we omit it Core mutates the body and HMAC
        # mismatches -> 401 bad_signature.
        "correlation_id": msg_id,
        "from": NODE_ID,
        "to": f"{to_node}.{surface}",
        "kind": "invocation",
        "payload": payload,
        "timestamp": now_iso(),
    }
    env["signature"] = sign(env)
    req = urllib.request.Request(
        f"{CORE_URL}/v0/invoke",
        data=json.dumps(env).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read().decode("utf-8", errors="replace")
            # Cap at 16 KB so a runaway payload can't blow up the model's
            # tool-result context, but allow real responses (panel lists,
            # state dumps, audit windows) through intact instead of
            # truncating at 200 chars and stripping IDs mid-string.
            if len(body) > 16384:
                body = body[:16384] + f"…[truncated, total {len(body)} bytes]"
            return True, (
                f"Sent to {to_node}.{surface} (http={r.status}). "
                f"id={msg_id} core_ack={body}"
            )
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        if len(body) > 8192:
            body = body[:8192] + f"…[truncated, total {len(body)} bytes]"
        return False, f"mesh_invoke_error: HTTP {e.code} body={body}"
    except Exception as e:  # noqa: BLE001
        return False, f"mesh_invoke_error: {e!r}"


def handle_call(name: str, args: dict) -> tuple[bool, str]:
    if not isinstance(name, str) or not name.startswith("mesh_"):
        return False, f"mesh_invoke_error: unknown tool {name!r}"
    # Re-read manifest so calls work even if the target was just added
    # mid-session.
    for to_node, surface, _meta in read_edges():
        if f"mesh_{to_node}_{surface}" == name:
            payload = args.get("payload") if isinstance(args, dict) else None
            if not isinstance(payload, dict):
                return False, "mesh_invoke_error: payload must be an object"
            conv = args.get("conversation_id") if isinstance(args, dict) else None
            if conv and "conversation_id" not in payload:
                payload = {**payload, "conversation_id": conv}
            return invoke_mesh(to_node, surface, payload)
    return False, f"mesh_invoke_error: no allow-edge for tool {name}"


def _write(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def respond(req_id, result=None, error=None) -> None:
    out: dict = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    _write(out)


def main() -> None:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            continue
        method = req.get("method")
        req_id = req.get("id")
        params = req.get("params") or {}

        if method == "initialize":
            respond(req_id, {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "lattice_mesh", "version": "0.1.0"},
            })
        elif method in ("notifications/initialized", "initialized"):
            continue  # notification — no response
        elif method == "tools/list":
            try:
                respond(req_id, {"tools": build_tools()})
            except Exception as e:  # noqa: BLE001
                respond(req_id, error={
                    "code": -32603,
                    "message": f"manifest read failed: {e!r}",
                })
        elif method == "tools/call":
            ok, text = handle_call(params.get("name"), params.get("arguments") or {})
            respond(req_id, {
                "content": [{"type": "text", "text": text}],
                "isError": not ok,
            })
        elif method == "ping":
            respond(req_id, {})
        elif req_id is not None:
            respond(req_id, error={
                "code": -32601,
                "message": f"method not found: {method}",
            })


if __name__ == "__main__":
    main()
