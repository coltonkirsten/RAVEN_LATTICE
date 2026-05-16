"""EDITH — Sonnet 4.6 daily-driver node for the LATTICE mesh.

Registers as 'edith', subscribes to /v0/stream, routes deliver events for
the 'edith.chat' surface through Claude, and responds via /v0/respond.

Conversation history is held in-memory, keyed by conversation_id, capped
at MAX_TURNS to prevent runaway memory.
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
from anthropic import Anthropic, APIError


CORE_URL = os.environ.get("CORE_URL", "http://host.docker.internal:8000")
NODE_ID = "edith"
SURFACE = "edith.chat"
SECRET = os.environ["EDITH_SECRET"].encode()
MODEL = os.environ.get("EDITH_MODEL", "claude-sonnet-4-6")
MAX_TURNS = 20

SYSTEM_PROMPT = (
    "You are EDITH, Colton's daily-driver AI agent running on his Mac mini "
    "as a node in the LATTICE mesh. You are concise, capable, and dry-witted "
    "(think JARVIS). Match the user's energy. Respond in plain text suitable "
    "for display in a chat panel."
)

# conversation_id -> list of {"role": "user"|"assistant", "content": str}
HISTORY: dict[str, list[dict]] = {}

claude = Anthropic()  # picks up ANTHROPIC_API_KEY from env


def canonical(env: dict) -> bytes:
    body = {k: v for k, v in env.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def sign(env: dict) -> str:
    return hmac.new(SECRET, canonical(env), hashlib.sha256).hexdigest()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def call_claude(conversation_id: str, user_message: str) -> str:
    history = HISTORY.setdefault(conversation_id, [])
    history.append({"role": "user", "content": user_message})

    resp = claude.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=history,
    )
    # Concatenate any text blocks in the response.
    reply = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")

    history.append({"role": "assistant", "content": reply})
    # Cap at MAX_TURNS user+assistant entries (2*MAX_TURNS messages).
    if len(history) > 2 * MAX_TURNS:
        del history[: len(history) - 2 * MAX_TURNS]
    return reply


async def respond(
    session: aiohttp.ClientSession,
    *,
    to: str,
    correlation_id: str,
    kind: str,
    payload: dict,
) -> None:
    env = {
        "id": str(uuid.uuid4()),
        "correlation_id": correlation_id,
        "from": NODE_ID,
        "to": to,
        "kind": kind,
        "payload": payload,
        "timestamp": now_iso(),
    }
    env["signature"] = sign(env)
    async with session.post(f"{CORE_URL}/v0/respond", json=env) as r:
        if r.status != 200:
            body = await r.text()
            print(f"[edith] respond failed: {r.status} {body}", file=sys.stderr, flush=True)


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

    try:
        reply = await asyncio.to_thread(call_claude, conv_id, user_message)
    except APIError as e:
        print(f"[edith] claude API error: {e}", file=sys.stderr, flush=True)
        await respond(
            session,
            to=invoker,
            correlation_id=msg_id,
            kind="error",
            payload={"error": f"claude_api_error: {e}"},
        )
        return
    except Exception as e:  # noqa: BLE001 — surface anything else as an error envelope
        print(f"[edith] unexpected error: {e!r}", file=sys.stderr, flush=True)
        await respond(
            session,
            to=invoker,
            correlation_id=msg_id,
            kind="error",
            payload={"error": f"internal_error: {e!r}"},
        )
        return

    print(f"[edith] reply len={len(reply)} chars", flush=True)
    await respond(
        session,
        to=invoker,
        correlation_id=msg_id,
        kind="response",
        payload={"reply": reply},
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
        print(f"[edith] registered session={session_id[:8]} model={MODEL}", flush=True)

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
