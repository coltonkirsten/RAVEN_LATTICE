"""browser — browser-automation node for the LATTICE mesh.

Same mesh shape as EDITH: registers as 'browser', subscribes to /v0/stream,
routes deliver events for the 'browser.query' surface, replies to the
sender by issuing a NEW signed invocation to the sender's inbox.

Backend: Claude Code CLI + Playwright MCP. The CLI is spawned with a
generated --mcp-config that points at @playwright/mcp@latest over npx.
Claude drives the browser via MCP tool calls (browser_navigate,
browser_click, browser_snapshot, etc) and returns a final text answer.

Why NOT browser-use? The first design used browser-use directly with
the Anthropic SDK, but:
  1. ANTHROPIC_API_KEY is out of credits.
  2. CLAUDE_CODE_OAUTH_TOKEN works through the Claude Code CLI path
     only; passing it as auth_token+beta-header on the raw SDK gets
     rate-limited 429 by Anthropic's edge.
The CLI path is the only one Anthropic supports for Max-plan OAuth, so
that's what we use here — same pattern EDITH already uses for chat.

The mesh MCP (./mesh_mcp.py) is ALSO loaded so the spawned CLI can call
back into the mesh — e.g. notify control.message or send the answer
straight to raven.message — same way EDITH does. The node-level
fallback below ensures the user gets a reply even when the CLI forgets
to call the mesh tool.
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
NODE_ID = "browser"
SURFACE = "browser.query"
SECRET = os.environ["BROWSER_SECRET"].encode()
MODEL = os.environ.get("BROWSER_MODEL", "claude-sonnet-4-6")
HEADLESS = os.environ.get("BROWSER_HEADLESS", "1") not in ("0", "false", "")
MAX_TURNS = int(os.environ.get("BROWSER_MAX_TURNS", "20"))
MANIFEST_PATH = os.environ.get("MANIFEST_PATH", "/app/manifest.yaml")

MCP_ENABLED = os.environ.get("BROWSER_MCP_ENABLED", "1") not in ("0", "false", "")
MCP_SCRIPT = os.environ.get("BROWSER_MCP_SCRIPT", "/app/mesh_mcp.py")
MCP_CONFIG_PATH = "/tmp/browser_mcp_config.json"

OAUTH_TOKEN = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
if not OAUTH_TOKEN:
    print(
        "[browser] FATAL: CLAUDE_CODE_OAUTH_TOKEN not set (required for CLI mode)",
        file=sys.stderr,
    )
    sys.exit(1)

AUTH_MODE = "oauth-cli"

# Conversation continuity (parity with EDITH).
SESSIONS: dict[str, str] = {}

PERSONA = (
    "You are the 'browser' node in the LATTICE mesh — Colton's browser "
    "automation agent. The user gives you a natural-language task that "
    "requires browsing the live web; you use the Playwright MCP tools "
    "(prefixed `mcp__playwright__browser_*`) to drive a real Chromium "
    "browser and answer.\n\n"
    "Available tools include: browser_navigate, browser_snapshot, "
    "browser_click, browser_type, browser_select_option, "
    "browser_press_key, browser_wait_for, browser_take_screenshot, "
    "browser_evaluate, browser_console_messages, browser_close.\n\n"
    "Workflow: navigate to a relevant URL, snapshot the page, locate "
    "the answer, and reply with ONLY the requested fact (no chatter). "
    "If you cannot find it after a few attempts, say so plainly.\n\n"
    "REPLY PROTOCOL — IMPORTANT. The browser.query surface is "
    "fire-and-forget. The original sender does NOT get your prose "
    "back automatically. Each incoming task message will begin with a "
    "tag of the form `[from: <sender_id>]` identifying the sender. "
    "After you compose your reply text, you SHOULD send it as a new "
    "mesh invocation to that sender's inbox surface using the "
    "appropriate mesh tool: `mcp__lattice_mesh__mesh_<sender>_<inbox>` "
    "(e.g. `mcp__lattice_mesh__mesh_raven_message`) with payload "
    "`{\"message\": \"<your reply>\"}`. If you forget, the node-side "
    "fallback ships your final answer to the sender's inbox anyway."
)


def canonical(env: dict) -> bytes:
    body = {k: v for k, v in env.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def sign(env: dict) -> str:
    return hmac.new(SECRET, canonical(env), hashlib.sha256).hexdigest()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_mcp_config() -> str:
    """Write the MCP config the CLI loads. Two servers:

    - `playwright`: @playwright/mcp@latest over npx — gives Claude tools
      like browser_navigate, browser_click, browser_snapshot.
    - `lattice_mesh`: this node's outbound-edge bridge (mesh_mcp.py) so
      Claude can reply via a signed mesh invocation.
    """
    servers: dict = {
        "playwright": {
            # @playwright/mcp ships its own Chromium that Playwright's
            # bundled-binaries flow installs to ~/.cache/ms-playwright
            # on first run. Headless is the default; we don't expose
            # the user-data dir, so each invocation gets a fresh
            # incognito-style profile.
            "command": "npx",
            "args": [
                "-y", "@playwright/mcp@latest",
                "--headless" if HEADLESS else "--isolated",
                "--browser", "chromium",
            ],
        },
    }
    if MCP_ENABLED and os.path.exists(MCP_SCRIPT):
        servers["lattice_mesh"] = {
            "command": "python3",
            "args": [MCP_SCRIPT],
            "env": {
                "MESH_NODE_ID": NODE_ID,
                "CORE_URL": CORE_URL,
                "MANIFEST_PATH": MANIFEST_PATH,
                "BROWSER_SECRET": os.environ["BROWSER_SECRET"],
            },
        }
    cfg = {"mcpServers": servers}
    with open(MCP_CONFIG_PATH, "w") as f:
        json.dump(cfg, f)
    return MCP_CONFIG_PATH


async def call_claude(conversation_id: str, user_message: str) -> tuple[str, set[str]]:
    """Spawn the claude CLI with Playwright + lattice_mesh MCP servers.

    Returns (final_text, mesh_tools_succeeded). Mesh tool success lets
    the caller skip the fallback reply when the CLI shipped the message
    itself.
    """
    cfg_path = _ensure_mcp_config()
    args = [
        "claude",
        "-p", user_message,
        "--output-format", "stream-json",
        "--verbose",
        "--model", MODEL,
        "--append-system-prompt", PERSONA,
        "--mcp-config", cfg_path,
        "--dangerously-skip-permissions",
    ]
    prior_session = SESSIONS.get(conversation_id)
    if prior_session:
        args.extend(["--resume", prior_session])

    # Strip ANTHROPIC_API_KEY so the CLI uses OAuth (Max plan). Same
    # trick as EDITH — if both are set the CLI prefers the API key
    # path, which has been billing-flagged on this account.
    cli_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    cli_env["HOME"] = cli_env.get("HOME", "/home/browser")

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
    mesh_calls: dict[str, str] = {}
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
                if name and not block.get("is_error"):
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
        result_text = stderr.strip() or "(empty response from claude CLI)"

    return result_text, mesh_tools_succeeded


def find_inbox_surface(node_id: str) -> str | None:
    try:
        with open(MANIFEST_PATH) as f:
            manifest = yaml.safe_load(f) or {}
    except FileNotFoundError:
        print(
            f"[browser] manifest not found at {MANIFEST_PATH}",
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
                    f"[browser] mesh_invoke to={to} failed: {r.status} {body[:200]}",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                print(
                    f"[browser] mesh_invoke to={to} status={r.status}",
                    flush=True,
                )
    except Exception as e:  # noqa: BLE001
        print(
            f"[browser] mesh_invoke to={to} crash: {e!r}",
            file=sys.stderr,
            flush=True,
        )


async def handle_deliver(session: aiohttp.ClientSession, env: dict) -> None:
    if env.get("to") != SURFACE:
        print(f"[browser] ignoring deliver to {env.get('to')!r}", flush=True)
        return

    invoker = env.get("from", "")
    msg_id = env["id"]
    payload = env.get("payload") or {}
    task = payload.get("task") or payload.get("message") or ""
    conv_id = payload.get("conversation_id") or "default"

    print(
        f"[browser] query from={invoker} conv={conv_id} task={task!r}",
        flush=True,
    )

    if not task:
        await _reply(session, invoker, msg_id, conv_id, "(empty task)")
        return

    tagged = f"[from: {invoker}] {task}"
    try:
        reply, mesh_ok = await call_claude(conv_id, tagged)
    except Exception as e:  # noqa: BLE001
        print(f"[browser] claude CLI error: {e!r}", file=sys.stderr, flush=True)
        await _reply(
            session, invoker, msg_id, conv_id,
            f"[browser error] claude_cli_error: {e}",
        )
        return

    target = find_inbox_surface(invoker)
    expected_tool = (
        f"mcp__lattice_mesh__mesh_{invoker}_{target}" if target else None
    )
    replied_via_cli = expected_tool is not None and expected_tool in mesh_ok
    print(
        f"[browser] reply len={len(reply)} chars mesh_ok={sorted(mesh_ok)} "
        f"expected={expected_tool!r} replied_via_cli={replied_via_cli}",
        flush=True,
    )

    if replied_via_cli:
        return
    await _reply(session, invoker, msg_id, conv_id, reply)


async def _reply(
    session: aiohttp.ClientSession,
    invoker: str,
    in_reply_to: str,
    conv_id: str,
    message: str,
) -> None:
    target = find_inbox_surface(invoker)
    if not target:
        print(
            f"[browser] no inbox surface declared for {invoker!r}; "
            "dropping reply",
            file=sys.stderr,
            flush=True,
        )
        return
    await mesh_invoke(
        session,
        to=f"{invoker}.{target}",
        payload={
            "message": message,
            "in_reply_to": in_reply_to,
            "conversation_id": conv_id,
        },
    )


async def main() -> None:
    async with aiohttp.ClientSession() as s:
        reg = {"node_id": NODE_ID, "timestamp": now_iso()}
        reg["signature"] = sign(reg)
        async with s.post(f"{CORE_URL}/v0/register", json=reg) as r:
            if r.status != 200:
                print(
                    f"[browser] register failed: {r.status} {await r.text()}",
                    file=sys.stderr,
                )
                sys.exit(1)
            reg_resp = await r.json()
        session_id = reg_resp["session_id"]
        print(
            f"[browser] registered session={session_id[:8]} model={MODEL} "
            f"auth={AUTH_MODE} headless={HEADLESS} mcp_enabled={MCP_ENABLED}",
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
                            # Don't block the SSE loop on a long browser task.
                            asyncio.create_task(handle_deliver(s, data))
                        except Exception as e:  # noqa: BLE001
                            print(
                                f"[browser] handler crashed: {e!r}",
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
