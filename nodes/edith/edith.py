"""EDITH — Sonnet 4.6 daily-driver node for the LATTICE mesh.

Registers as 'edith', subscribes to /v0/stream, routes deliver events for
the 'edith.chat' surface through the Claude Code CLI. The chat surface is
declared `type: inbox, invocation_mode: fire_and_forget`, so Core acks the
caller's /v0/invoke with 202 directly — EDITH does NOT call /v0/respond
on the incoming envelope. To reply, EDITH issues a NEW signed invocation
to the sender's own inbox surface (e.g. raven.message), so the reply is
itself a first-class mesh message.

Why the CLI instead of the SDK?
  Raw Anthropic SDK calls with an OAuth (oat01) token can trip Anthropic's
  edge anti-abuse rate limiter even when usage buckets are well under quota.
  The Claude Code CLI handles request shaping, retries, and backoff so its
  traffic isn't flagged. EDITH spawns the CLI as a subprocess and parses
  its stream-json output. CLAUDE_CODE_OAUTH_TOKEN is read from env by the
  CLI automatically.

Conversation continuity is handled via the CLI's --resume <session_id>
flag. We keep a {conversation_id -> claude_session_id} map in memory.
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
import yaml


CORE_URL = os.environ.get("CORE_URL", "http://host.docker.internal:8000")
NODE_ID = "edith"
SURFACE = "edith.chat"
SECRET = os.environ["EDITH_SECRET"].encode()
MODEL = os.environ.get("EDITH_MODEL", "claude-sonnet-4-6")

# Mesh MCP: expose this node's outbound edges to the spawned Claude CLI
# as tools so EDITH can send messages back into the lattice (e.g. to
# control.message). Gated on EDITH_MCP_ENABLED (default on).
MCP_ENABLED = os.environ.get("EDITH_MCP_ENABLED", "1") not in ("0", "false", "")
MCP_SCRIPT = os.environ.get("EDITH_MCP_SCRIPT", "/app/mesh_mcp.py")
MANIFEST_PATH = os.environ.get("MANIFEST_PATH", "/app/manifest.yaml")
MCP_CONFIG_PATH = "/tmp/edith_mcp_config.json"

OAUTH_TOKEN = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
if not OAUTH_TOKEN:
    print(
        "[edith] FATAL: CLAUDE_CODE_OAUTH_TOKEN not set (required for CLI mode)",
        file=sys.stderr,
    )
    sys.exit(1)

AUTH_MODE = "oauth-cli"

# EDITH persona — appended to Claude Code's required system prefix via
# --append-system-prompt. The CLI handles the "You are Claude Code…"
# prefix automatically when invoked with the OAuth token.
EDITH_PERSONA = (
    "Operate as EDITH — Colton's daily-driver AI agent running on his Mac mini "
    "as a node in the LATTICE mesh. You are concise, capable, and dry-witted "
    "(think JARVIS). Match the user's energy. Respond in plain text suitable "
    "for display in a chat panel. You have mesh tools available "
    "(prefixed `mcp__lattice_mesh__mesh_*`) for sending messages to other "
    "nodes in the LATTICE mesh (e.g. `mesh_control_message` to notify "
    "Colton's dashboard). Tool returns confirm delivery to Core, not the "
    "target's reply.\n\n"
    "REPLY PROTOCOL — IMPORTANT. The chat surface you receive on is "
    "fire-and-forget. The original sender does NOT get your prose back "
    "automatically. Each incoming chat message will begin with a tag of "
    "the form `[from: <sender_id>]` identifying the sender node. After you "
    "compose your reply text, you MUST send it as a new mesh invocation to "
    "that sender's inbox surface using the appropriate mesh tool: if the "
    "tag says `[from: raven]`, call `mcp__lattice_mesh__mesh_raven_message` "
    "with payload `{\"message\": \"<your reply>\"}`. If `[from: control]`, "
    "use `mcp__lattice_mesh__mesh_control_message`. Do not skip this — "
    "sending the reply via the mesh is how the user actually receives "
    "your response. If you do not call the mesh tool, the user gets "
    "nothing."
)

# conversation_id -> claude session_id (returned by CLI on first turn,
# passed back via --resume on subsequent turns to preserve history)
SESSIONS: dict[str, str] = {}


def canonical(env: dict) -> bytes:
    body = {k: v for k, v in env.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def sign(env: dict) -> str:
    return hmac.new(SECRET, canonical(env), hashlib.sha256).hexdigest()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_mcp_config() -> str | None:
    """Write (idempotently) the MCP config that points the CLI at mesh_mcp.py.

    Returns the config path, or None when MCP is disabled / script missing.
    Re-writing is cheap and keeps the file in sync if env vars change.
    """
    if not MCP_ENABLED:
        return None
    if not os.path.exists(MCP_SCRIPT):
        print(
            f"[edith] mesh MCP script missing at {MCP_SCRIPT}; skipping --mcp-config",
            file=sys.stderr,
            flush=True,
        )
        return None
    cfg = {
        "mcpServers": {
            "lattice_mesh": {
                "command": "python3",
                "args": [MCP_SCRIPT],
                "env": {
                    "MESH_NODE_ID": NODE_ID,
                    "CORE_URL": CORE_URL,
                    "MANIFEST_PATH": MANIFEST_PATH,
                    "EDITH_SECRET": os.environ["EDITH_SECRET"],
                },
            }
        }
    }
    try:
        with open(MCP_CONFIG_PATH, "w") as f:
            json.dump(cfg, f)
        return MCP_CONFIG_PATH
    except Exception as e:  # noqa: BLE001
        print(
            f"[edith] failed to write MCP config: {e!r}",
            file=sys.stderr,
            flush=True,
        )
        return None


async def call_claude(conversation_id: str, user_message: str) -> tuple[str, set[str]]:
    """Spawn the claude CLI and return (result_text, mesh_tools_called).

    Uses --output-format stream-json so we can extract the final result,
    the session_id for resume, and observe which `mcp__lattice_mesh__mesh_*`
    tool_use blocks the CLI emitted. The CLI inherits
    CLAUDE_CODE_OAUTH_TOKEN from our env and uses it automatically.
    """
    args = [
        "claude",
        "-p", user_message,
        "--output-format", "stream-json",
        "--verbose",
        "--model", MODEL,
        "--append-system-prompt", EDITH_PERSONA,
        "--dangerously-skip-permissions",
    ]
    mcp_cfg = _ensure_mcp_config()
    if mcp_cfg:
        args.extend(["--mcp-config", mcp_cfg])
    prior_session = SESSIONS.get(conversation_id)
    if prior_session:
        args.extend(["--resume", prior_session])

    # Strip ANTHROPIC_API_KEY from the spawn env: when both are present
    # the claude CLI prefers ANTHROPIC_API_KEY over CLAUDE_CODE_OAUTH_TOKEN,
    # which routes to console.anthropic.com pay-as-you-go billing instead
    # of the Max plan OAuth path. We want OAuth.
    cli_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    cli_env["HOME"] = cli_env.get("HOME", "/home/edith")

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd="/tmp",
        env=cli_env,
    )

    stdout_bytes, stderr_bytes = await proc.communicate()
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        raise RuntimeError(
            f"claude CLI exited {proc.returncode}: {stderr.strip() or stdout.strip()}"
        )

    result_text = ""
    new_session_id: str | None = None
    # tool_use_id -> mesh tool name (only for our mesh tools)
    mesh_calls: dict[str, str] = {}
    # mesh tool names whose tool_result came back without is_error
    mesh_tools_succeeded: set[str] = set()

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mtype = msg.get("type")
        if mtype == "system" and msg.get("subtype") == "init":
            sid = msg.get("session_id")
            if sid:
                new_session_id = sid
        elif mtype == "assistant":
            inner = msg.get("message") or {}
            for block in inner.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = str(block.get("name", ""))
                if name.startswith("mcp__lattice_mesh__mesh_"):
                    use_id = str(block.get("id", ""))
                    if use_id:
                        mesh_calls[use_id] = name
        elif mtype == "user":
            inner = msg.get("message") or {}
            for block in inner.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                use_id = str(block.get("tool_use_id", ""))
                name = mesh_calls.get(use_id)
                if not name:
                    continue
                if not block.get("is_error"):
                    mesh_tools_succeeded.add(name)
        elif mtype == "result":
            if msg.get("result"):
                result_text = msg["result"]
            if msg.get("is_error") and msg.get("errors"):
                errs = msg["errors"]
                if isinstance(errs, list):
                    result_text = "; ".join(str(e) for e in errs)

    if new_session_id:
        SESSIONS[conversation_id] = new_session_id

    if not result_text:
        # Last-ditch: try the raw stderr in case CLI wrote a useful message
        result_text = stderr.strip() or "(empty response from claude CLI)"

    return result_text, mesh_tools_succeeded


def find_inbox_surface(node_id: str) -> str | None:
    """Return surface name for the inbox surface of node_id, or None.

    Manifest is re-read every call so newly-added inbox surfaces are
    visible without restarting EDITH. Assumes a single inbox surface
    per node (current LATTICE invariant).
    """
    try:
        with open(MANIFEST_PATH) as f:
            manifest = yaml.safe_load(f) or {}
    except FileNotFoundError:
        print(
            f"[edith] manifest not found at {MANIFEST_PATH}",
            file=sys.stderr,
            flush=True,
        )
        return None
    for n in manifest.get("nodes") or []:
        if n.get("id") != node_id:
            continue
        for s in n.get("surfaces") or []:
            if s.get("type") == "inbox":
                return s.get("name")
    return None


async def mesh_invoke(
    session: aiohttp.ClientSession,
    *,
    to: str,
    payload: dict,
) -> None:
    """POST a signed invocation envelope to Core /v0/invoke.

    Used as the fallback reply path when the spawned CLI did not call a
    `mcp__lattice_mesh__mesh_*` tool itself. `to` is a surface id like
    `raven.message`.
    """
    msg_id = str(uuid.uuid4())
    env = {
        "id": msg_id,
        "correlation_id": msg_id,
        "from": NODE_ID,
        "to": to,
        "kind": "invocation",
        "payload": payload,
        "timestamp": now_iso(),
    }
    env["signature"] = sign(env)
    try:
        async with session.post(f"{CORE_URL}/v0/invoke", json=env) as r:
            body = await r.text()
            if r.status not in (200, 202):
                print(
                    f"[edith] mesh_invoke to={to} failed: {r.status} {body[:200]}",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                print(
                    f"[edith] mesh_invoke to={to} status={r.status}",
                    flush=True,
                )
    except Exception as e:  # noqa: BLE001
        print(
            f"[edith] mesh_invoke to={to} crash: {e!r}",
            file=sys.stderr,
            flush=True,
        )


async def handle_deliver(session: aiohttp.ClientSession, env: dict) -> None:
    if env.get("to") != SURFACE:
        print(f"[edith] ignoring deliver to {env.get('to')!r}", flush=True)
        return

    invoker = env.get("from", "")
    msg_id = env["id"]
    payload = env.get("payload") or {}
    user_message = payload.get("message", "")
    conv_id = payload.get("conversation_id") or "default"

    print(
        f"[edith] chat from={invoker} conv={conv_id} msg={user_message!r}",
        flush=True,
    )

    # Tag the inbound message with the sender id so the CLI persona can
    # route the reply to the right inbox tool (mesh_<sender>_<surface>).
    tagged_message = f"[from: {invoker}] {user_message}"

    try:
        reply, mesh_tools_succeeded = await call_claude(conv_id, tagged_message)
    except Exception as e:  # noqa: BLE001
        print(f"[edith] claude CLI error: {e!r}", file=sys.stderr, flush=True)
        # Best-effort error reply to the sender's inbox surface.
        target = find_inbox_surface(invoker)
        if target:
            await mesh_invoke(
                session,
                to=f"{invoker}.{target}",
                payload={
                    "message": f"[edith error] claude_cli_error: {e}",
                    "in_reply_to": msg_id,
                    "conversation_id": conv_id,
                },
            )
        return

    target = find_inbox_surface(invoker)
    expected_tool = f"mcp__lattice_mesh__mesh_{invoker}_{target}" if target else None
    replied_via_cli = expected_tool is not None and expected_tool in mesh_tools_succeeded
    print(
        f"[edith] reply len={len(reply)} chars mesh_ok={sorted(mesh_tools_succeeded)} "
        f"expected={expected_tool!r} replied_via_cli={replied_via_cli}",
        flush=True,
    )

    if replied_via_cli:
        # CLI already shipped the reply to the sender's inbox.
        return

    # Fallback: CLI either skipped the mesh tool entirely or routed to the
    # wrong inbox. Deliver the prose reply directly to the sender's inbox
    # so the protocol invariant ("inbox sender always gets a reply on
    # their inbox") holds regardless of CLI behavior.
    if not target:
        print(
            f"[edith] no inbox surface declared for {invoker!r}; "
            "skipping fallback reply",
            file=sys.stderr,
            flush=True,
        )
        return
    await mesh_invoke(
        session,
        to=f"{invoker}.{target}",
        payload={
            "message": reply,
            "in_reply_to": msg_id,
            "conversation_id": conv_id,
        },
    )


async def main() -> None:
    async with aiohttp.ClientSession() as s:
        reg = {"node_id": NODE_ID, "timestamp": now_iso()}
        reg["signature"] = sign(reg)
        async with s.post(f"{CORE_URL}/v0/register", json=reg) as r:
            if r.status != 200:
                print(f"[edith] register failed: {r.status} {await r.text()}", file=sys.stderr)
                sys.exit(1)
            reg_resp = await r.json()
        session_id = reg_resp["session_id"]
        print(
            f"[edith] registered session={session_id[:8]} model={MODEL} auth={AUTH_MODE}",
            flush=True,
        )

        async with s.get(
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
                            await handle_deliver(s, data)
                        except Exception as e:  # noqa: BLE001
                            print(f"[edith] handler crashed: {e!r}", file=sys.stderr, flush=True)
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
