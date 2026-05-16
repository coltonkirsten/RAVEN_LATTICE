"""RAVEN portal node for the LATTICE mesh.

Registers as 'raven' (actor kind), exposes the 'raven.message' surface.

Inbound: lattice messages routed to raven.message are written into
RAVEN's unified message queue (~/raven/data/message_queue.json), where
the main RAVEN loop will pick them up alongside iMessages, cron tasks,
and worker pickups. We respond immediately with kind=ack so the
invoking node doesn't time out — the actual RAVEN reply will surface
later via main loop output (sent through whatever channel RAVEN
chooses, including potentially calling back into other lattice
surfaces via this node's outbound invoke path).

Outbound: this node also exposes a local Unix-socket / HTTP control
plane so RAVEN can send messages out to any other lattice surface.
Initial implementation: simple `invoke(to, payload)` helper, plus a
queue-watcher that drains an outbound file (~/raven/data/lattice_outbox.json)
into Core's /v0/invoke endpoint.

Runs natively on host (NOT docker) because it needs direct filesystem
access to RAVEN's queue files.
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
from pathlib import Path

import aiohttp

# Wire RAVEN's queue module into sys.path so we can write directly into
# the same queue file the imessage_watcher / enqueue.py / task_agents
# pickup all use.
RAVEN_ROOT = Path.home() / "raven"
sys.path.insert(0, str(RAVEN_ROOT))
from messaging.queue import get_queue, QueuedMessage  # noqa: E402


CORE_URL = os.environ.get("CORE_URL", "http://127.0.0.1:8000")
NODE_ID = "raven"
SURFACE = "raven.message"

_secret_env = os.environ.get("RAVEN_SECRET")
if not _secret_env:
    print("[raven-node] FATAL: RAVEN_SECRET not set", file=sys.stderr)
    sys.exit(1)
SECRET = _secret_env.encode()

OUTBOX_FILE = RAVEN_ROOT / "data" / "lattice_outbox.json"
OUTBOX_POLL_SEC = 1.0


def canonical(env: dict) -> bytes:
    body = {k: v for k, v in env.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def sign(env: dict) -> str:
    return hmac.new(SECRET, canonical(env), hashlib.sha256).hexdigest()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------
# INBOUND: lattice deliver -> RAVEN queue
# ---------------------------------------------------------------------

def enqueue_lattice_message(from_node: str, payload: dict, correlation_id: str) -> str:
    """Write an incoming lattice message into RAVEN's unified queue.

    Format the content so it's distinguishable from iMessages/crons,
    and tag the source so RAVEN can route responses back through the
    portal's outbox if it wants to reply via lattice.
    """
    text = payload.get("message") or payload.get("text") or json.dumps(payload)
    content = f"[LATTICE from={from_node}] {text}"
    queue = get_queue()
    msg = QueuedMessage.create(
        source="lattice",
        sender=from_node,
        content=content,
    )
    # Store the correlation_id under agent_id so RAVEN's outbound reply
    # can thread it back to the originating invocation if needed.
    msg.agent_id = correlation_id
    msg_id = queue.enqueue(msg)
    return msg_id


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
            print(
                f"[raven-node] respond failed: {r.status} {body}",
                file=sys.stderr,
                flush=True,
            )


async def handle_deliver(session: aiohttp.ClientSession, env: dict) -> None:
    if env.get("to") != SURFACE:
        print(f"[raven-node] ignoring deliver to {env.get('to')!r}", flush=True)
        return

    invoker = env.get("from", "")
    msg_id = env["id"]
    payload = env.get("payload") or {}

    try:
        queue_id = enqueue_lattice_message(invoker, payload, msg_id)
        print(
            f"[raven-node] queued lattice msg id={queue_id} from={invoker} corr={msg_id[:8]}",
            flush=True,
        )
        # Core only accepts kind=response|error on /v0/respond. Send a
        # synchronous "queued, RAVEN will follow up" response immediately
        # so the invoking node doesn't time out; the real reply (if any)
        # flows back later through the outbox -> /v0/invoke path.
        await respond(
            session,
            to=invoker,
            correlation_id=msg_id,
            kind="response",
            payload={
                "queued": True,
                "queue_id": queue_id,
                "note": "RAVEN received your message. Reply will arrive asynchronously.",
            },
        )
    except Exception as e:  # noqa: BLE001
        print(f"[raven-node] enqueue failed: {e!r}", file=sys.stderr, flush=True)
        await respond(
            session,
            to=invoker,
            correlation_id=msg_id,
            kind="error",
            payload={"error": f"enqueue_failed: {e!r}"},
        )


# ---------------------------------------------------------------------
# OUTBOUND: RAVEN drops files into outbox -> we invoke lattice surfaces
# ---------------------------------------------------------------------

async def drain_outbox(session: aiohttp.ClientSession) -> None:
    """Poll the outbox file for messages RAVEN wants to send into the lattice.

    Format expected (JSON array, each entry):
      {"to": "<surface>", "payload": {...}, "kind": "invocation" | "request"}

    File is read+cleared atomically using fcntl. If RAVEN writes a new
    array of messages, all get dispatched on the next poll.
    """
    import fcntl

    while True:
        await asyncio.sleep(OUTBOX_POLL_SEC)
        if not OUTBOX_FILE.exists():
            continue
        try:
            with open(OUTBOX_FILE, "r+") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                raw = f.read()
                if not raw.strip():
                    continue
                try:
                    msgs = json.loads(raw)
                except json.JSONDecodeError as e:
                    print(
                        f"[raven-node] outbox JSON parse failed: {e}, clearing",
                        file=sys.stderr,
                        flush=True,
                    )
                    msgs = []
                # Clear the file so we don't re-send.
                f.seek(0)
                f.truncate()
                f.write("[]")
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception as e:  # noqa: BLE001
            print(f"[raven-node] outbox read crash: {e!r}", file=sys.stderr, flush=True)
            continue

        if not isinstance(msgs, list):
            continue
        for m in msgs:
            await send_outbound(session, m)


async def send_outbound(session: aiohttp.ClientSession, msg: dict) -> None:
    to = msg.get("to")
    payload = msg.get("payload") or {}
    kind = msg.get("kind", "invocation")
    if not to:
        print(f"[raven-node] outbound missing 'to', skip: {msg}", flush=True)
        return
    # Pre-fill id + correlation_id so Core's setdefault() inside
    # _route_invocation doesn't mutate the envelope post-sign. Core mutates
    # the envelope BEFORE verifying signature; if we leave correlation_id
    # off, Core adds it and the HMAC over the mutated body no longer matches.
    msg_id = str(uuid.uuid4())
    env = {
        "id": msg_id,
        "correlation_id": msg.get("correlation_id", msg_id),
        "from": NODE_ID,
        "to": to,
        "kind": kind,
        "payload": payload,
        "timestamp": now_iso(),
    }
    env["signature"] = sign(env)
    try:
        async with session.post(f"{CORE_URL}/v0/invoke", json=env) as r:
            body = await r.text()
            print(
                f"[raven-node] outbound to={to} status={r.status} body={body[:200]}",
                flush=True,
            )
    except Exception as e:  # noqa: BLE001
        print(
            f"[raven-node] outbound to={to} crash: {e!r}",
            file=sys.stderr,
            flush=True,
        )


# ---------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------

async def main() -> None:
    async with aiohttp.ClientSession() as s:
        reg = {"node_id": NODE_ID, "timestamp": now_iso()}
        reg["signature"] = sign(reg)
        async with s.post(f"{CORE_URL}/v0/register", json=reg) as r:
            if r.status != 200:
                body = await r.text()
                print(
                    f"[raven-node] register failed: {r.status} {body}",
                    file=sys.stderr,
                )
                sys.exit(1)
            reg_resp = await r.json()
        session_id = reg_resp["session_id"]
        print(
            f"[raven-node] registered session={session_id[:8]} surfaces={reg_resp.get('surfaces')}",
            flush=True,
        )

        # Start outbox drain in background.
        outbox_task = asyncio.create_task(drain_outbox(s))

        try:
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
                                print(
                                    f"[raven-node] handler crashed: {e!r}",
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
        finally:
            outbox_task.cancel()
            try:
                await outbox_task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    asyncio.run(main())
