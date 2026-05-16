# RAVEN portal node

The RAVEN AI agent's gateway into the LATTICE mesh.

## What it does

**Inbound:** registers as the `raven` actor node, exposes the
`raven.message` surface. Lattice messages routed to that surface
get written into RAVEN's unified message queue
(`~/raven/data/message_queue.json`) — the same queue that holds
iMessages, scheduled cron tasks, and task-agent pickups. The main
RAVEN loop picks them up on its next idle cycle.

Each inbound message is acked immediately (kind=`ack`) so the
invoking node doesn't time out. RAVEN's real reply lands later via
its own output channels (iMessage by default, or back through this
portal's outbox if a lattice reply is desired).

**Outbound:** RAVEN can send messages into the lattice by writing a
JSON array to `~/raven/data/lattice_outbox.json`. The portal polls
that file once a second, drains it (atomically), and dispatches each
entry to Core's `/v0/invoke` endpoint signed with this node's secret.

Outbox entry format:

```json
[
  {
    "to": "edith.chat",
    "kind": "invocation",
    "payload": {"message": "what's up", "conversation_id": "raven-debug"}
  }
]
```

## Runtime

Native Python on host (not docker). Direct filesystem access to
RAVEN's queue files is the whole point of this node.

## Running

```bash
./start.sh
```

Or with the lattice quickstart pattern. Reads `RAVEN_SECRET` and
`CORE_URL` from `../../hosts/mac-mini/.env`.

## Topology

By design, this node has edges to **every** other surface in the
mesh — Colton's call so RAVEN can debug freely. Edits to
`manifest.yaml` are needed when new surfaces come online.
