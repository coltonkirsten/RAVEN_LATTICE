"""browser — browser-automation node for the LATTICE mesh.

Same mesh shape as EDITH: registers as 'browser', subscribes to /v0/stream,
routes deliver events for the 'browser.query' surface through a
browser-use agent loop, replies to the sender by issuing a NEW signed
invocation to the sender's inbox surface.

Why browser-use directly (no Claude Code CLI in the loop)?
  browser-use already runs its own LLM-driven agent loop and handles
  retries/backoff via the Anthropic SDK. Wrapping it in another CLI
  loop would double-LLM the request. We call `Agent.run()` and ship
  the final result back to the sender.

Auth.
  browser-use's ChatAnthropic uses the raw Anthropic SDK. It accepts
  either `api_key` (x-api-key, console.anthropic.com billing) or
  `auth_token` (Bearer header, OAuth Max-plan billing) plus a beta
  header for the OAuth path. We prefer ANTHROPIC_API_KEY when set
  because it's the well-trodden path; if only CLAUDE_CODE_OAUTH_TOKEN
  is present we try the OAuth route with the beta header.
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
MAX_STEPS = int(os.environ.get("BROWSER_MAX_STEPS", "25"))
MANIFEST_PATH = os.environ.get("MANIFEST_PATH", "/app/manifest.yaml")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY") or ""
OAUTH_TOKEN = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or ""
if not ANTHROPIC_API_KEY and not OAUTH_TOKEN:
    print(
        "[browser] FATAL: neither ANTHROPIC_API_KEY nor "
        "CLAUDE_CODE_OAUTH_TOKEN is set",
        file=sys.stderr,
    )
    sys.exit(1)

AUTH_MODE = "api-key" if ANTHROPIC_API_KEY else "oauth-token"


def canonical(env: dict) -> bytes:
    body = {k: v for k, v in env.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def sign(env: dict) -> str:
    return hmac.new(SECRET, canonical(env), hashlib.sha256).hexdigest()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_llm():
    """Return a browser-use LLM client wired to whichever auth we have.

    Imported lazily — browser-use is heavy and pulls in Playwright + a
    network call to verify Chromium on import in some versions.
    """
    from browser_use import ChatAnthropic  # type: ignore[import-not-found]

    if ANTHROPIC_API_KEY:
        return ChatAnthropic(model=MODEL, api_key=ANTHROPIC_API_KEY)
    # OAuth path — pass Bearer token + the beta header Anthropic's edge
    # uses to route Claude Code / Max plan requests. May still 401 if
    # Anthropic later requires the Claude Code system-prompt prefix; if
    # so we surface the error back to the sender.
    return ChatAnthropic(
        model=MODEL,
        auth_token=OAUTH_TOKEN,
        default_headers={"anthropic-beta": "oauth-2025-04-20"},
    )


async def run_browser_task(task: str) -> str:
    """Execute a browser-use agent loop and return the final result string."""
    from browser_use import Agent, Browser  # type: ignore[import-not-found]

    llm = _build_llm()
    browser = Browser(headless=HEADLESS)
    agent = Agent(task=task, llm=llm, browser=browser)
    try:
        history = await agent.run(max_steps=MAX_STEPS)
    finally:
        # browser-use's Browser holds Playwright resources; close cleanly.
        try:
            await browser.close()
        except Exception:  # noqa: BLE001
            pass

    # `history` is an AgentHistoryList with .final_result() in 0.12.x.
    final = None
    try:
        final = history.final_result()
    except Exception:  # noqa: BLE001
        final = None
    if not final:
        # Fall back to the last extracted_content if final_result is empty.
        try:
            extracted = history.extracted_content()
            if isinstance(extracted, list) and extracted:
                final = extracted[-1]
            elif isinstance(extracted, str):
                final = extracted
        except Exception:  # noqa: BLE001
            pass
    return final or "(browser-use produced no final result)"


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
    # Accept either {"task": "..."} (preferred) or {"message": "..."} for
    # symmetry with the chat-surface payload shape callers may copy.
    task = payload.get("task") or payload.get("message") or ""
    conv_id = payload.get("conversation_id") or "default"

    print(
        f"[browser] query from={invoker} conv={conv_id} task={task!r}",
        flush=True,
    )

    if not task:
        await _reply(session, invoker, msg_id, conv_id, "(empty task)")
        return

    try:
        result = await run_browser_task(task)
    except Exception as e:  # noqa: BLE001
        print(f"[browser] agent crash: {e!r}", file=sys.stderr, flush=True)
        await _reply(
            session,
            invoker,
            msg_id,
            conv_id,
            f"[browser error] {type(e).__name__}: {e}",
        )
        return

    print(
        f"[browser] reply len={len(result)} chars for conv={conv_id}",
        flush=True,
    )
    await _reply(session, invoker, msg_id, conv_id, result)


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
            f"auth={AUTH_MODE} headless={HEADLESS}",
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
                            # Spawn a task so a long-running browser job
                            # doesn't block subsequent SSE events.
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
