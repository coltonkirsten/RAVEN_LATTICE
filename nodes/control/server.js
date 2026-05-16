import express from "express";
import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import fetch from "node-fetch";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const NODE_ID = "control";
const INBOX_SURFACE = "control.message";
const CORE_URL = process.env.CORE_URL || "http://100.109.10.50:8000";
const PORT = parseInt(process.env.PORT || "5190", 10);
const SECRET = process.env.CONTROL_SECRET;
const INBOX_PATH = process.env.CONTROL_INBOX_PATH || path.join(__dirname, "inbox.json");

if (!SECRET) {
  console.error("[control] CONTROL_SECRET env var is required");
  process.exit(1);
}

const SECRET_BUF = Buffer.from(SECRET, "utf8");

let SESSION_ID = null;
let SURFACES = [];
let RELATIONSHIPS = [];

function nowIso() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

function canonical(obj) {
  const { signature: _sig, ...rest } = obj;
  return canonicalStringify(rest);
}

function canonicalStringify(value) {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) {
    return "[" + value.map(canonicalStringify).join(",") + "]";
  }
  const keys = Object.keys(value).sort();
  const parts = keys.map((k) => JSON.stringify(k) + ":" + canonicalStringify(value[k]));
  return "{" + parts.join(",") + "}";
}

function sign(obj) {
  const hmac = crypto.createHmac("sha256", SECRET_BUF);
  hmac.update(canonical(obj));
  return hmac.digest("hex");
}

async function register() {
  const body = { node_id: NODE_ID, timestamp: nowIso() };
  body.signature = sign(body);
  const res = await fetch(`${CORE_URL}/v0/register`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`register failed: ${res.status} ${text}`);
  }
  const json = await res.json();
  SESSION_ID = json.session_id;
  SURFACES = json.surfaces || [];
  RELATIONSHIPS = json.relationships || [];
  console.log(`[control] registered, session=${SESSION_ID}`);
  return json;
}

function parseSseFrame(frame) {
  let eventType = null;
  const dataLines = [];
  for (const raw of frame.split("\n")) {
    const line = raw.replace(/\r$/, "");
    if (line.startsWith(":")) continue;
    if (line.startsWith("event:")) {
      eventType = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).replace(/^ /, ""));
    }
  }
  return { eventType, data: dataLines.join("\n") };
}

function startSseConsumer() {
  if (!SESSION_ID) return;
  const url = `${CORE_URL}/v0/stream?session=${encodeURIComponent(SESSION_ID)}`;
  console.log(`[control] opening SSE stream ${url}`);
  fetch(url, { headers: { accept: "text/event-stream" } })
    .then(async (res) => {
      if (!res.ok || !res.body) {
        console.error(`[control] SSE open failed: ${res.status}`);
        scheduleReconnect();
        return;
      }
      const decoder = new TextDecoder();
      let buf = "";
      for await (const chunk of res.body) {
        buf += decoder.decode(chunk, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) !== -1) {
          const frame = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          if (!frame) continue;
          const { eventType, data } = parseSseFrame(frame);
          if (eventType === "deliver" && data) {
            let envelope;
            try {
              envelope = JSON.parse(data);
            } catch (e) {
              console.error("[control] deliver parse error:", e.message);
              continue;
            }
            handleDeliver(envelope).catch((err) => {
              console.error("[control] deliver handler crashed:", err.message);
            });
          }
        }
      }
      console.warn("[control] SSE stream ended; reconnecting");
      scheduleReconnect();
    })
    .catch((err) => {
      console.error("[control] SSE error:", err.message);
      scheduleReconnect();
    });
}

// ---------------------------------------------------------------------
// INBOX: persistence + delivery handler
// ---------------------------------------------------------------------
// Core verifies the sender's HMAC signature before pushing a `deliver`
// event over our SSE stream, so the SSE channel itself is the trust
// boundary. We don't have other nodes' identity secrets and therefore
// can't re-verify their signatures here — we accept what Core delivers.

let inboxWriteChain = Promise.resolve();

async function readInbox() {
  try {
    const raw = await fs.readFile(INBOX_PATH, "utf8");
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch (e) {
    if (e.code === "ENOENT") return [];
    throw e;
  }
}

async function writeInboxAtomic(arr) {
  const tmp = `${INBOX_PATH}.tmp`;
  await fs.writeFile(tmp, JSON.stringify(arr, null, 2), "utf8");
  await fs.rename(tmp, INBOX_PATH);
}

function withInbox(mutator) {
  // Serialize all inbox mutations to avoid lost updates between concurrent
  // SSE deliveries and HTTP API writes.
  const next = inboxWriteChain.then(async () => {
    const current = await readInbox();
    const updated = await mutator(current);
    if (updated !== undefined) {
      await writeInboxAtomic(updated);
      return updated;
    }
    return current;
  });
  inboxWriteChain = next.catch(() => {});
  return next;
}

async function initInbox() {
  try {
    await fs.access(INBOX_PATH);
  } catch {
    await writeInboxAtomic([]);
    console.log(`[control] initialized inbox at ${INBOX_PATH}`);
  }
}

async function handleDeliver(envelope) {
  if (envelope?.to !== INBOX_SURFACE) {
    return; // not for us
  }
  const entry = {
    id: envelope.id,
    correlation_id: envelope.correlation_id,
    from: envelope.from,
    to: envelope.to,
    kind: envelope.kind,
    payload: envelope.payload ?? {},
    sender_timestamp: envelope.timestamp,
    received_at: nowIso(),
    read: false,
  };
  await withInbox((arr) => {
    if (arr.some((e) => e.id === entry.id)) return undefined; // dedupe replays
    arr.push(entry);
    return arr;
  });
  console.log(`[control] inbox <- from=${entry.from} id=${String(entry.id).slice(0, 8)}`);
}

let reconnectTimer = null;
function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(async () => {
    reconnectTimer = null;
    try {
      await register();
      startSseConsumer();
    } catch (e) {
      console.error("[control] re-register failed:", e.message);
      scheduleReconnect();
    }
  }, 3000);
}

function buildEnvelope(to, payload) {
  const id = crypto.randomUUID();
  const env = {
    id,
    correlation_id: id,
    from: NODE_ID,
    to,
    kind: "invocation",
    payload,
    timestamp: nowIso(),
  };
  env.signature = sign(env);
  return env;
}

async function invokeSurface(to, payload) {
  const env = buildEnvelope(to, payload);
  const res = await fetch(`${CORE_URL}/v0/invoke`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(env),
  });
  const text = await res.text();
  let body;
  try {
    body = JSON.parse(text);
  } catch {
    body = { raw: text };
  }
  return { status: res.status, body };
}

function hasEdgeTo(target) {
  return RELATIONSHIPS.some((r) => r.from === NODE_ID && r.to === target);
}

const app = express();
app.use(express.json({ limit: "256kb" }));
app.use(express.static(path.join(__dirname, "web")));

app.get("/api/introspect", async (_req, res) => {
  try {
    const r = await fetch(`${CORE_URL}/v0/introspect`);
    const text = await r.text();
    res.status(r.status).type("application/json").send(text);
  } catch (e) {
    res.status(502).json({ error: "introspect_failed", detail: e.message });
  }
});

app.get("/api/audit/poll", async (req, res) => {
  if (!hasEdgeTo("core.audit_query")) {
    return res.json({
      audit: [],
      note: "edge control->core.audit_query not in manifest; add it and reload Core to populate this feed",
    });
  }
  try {
    const last_n = Math.min(parseInt(req.query.last_n || "100", 10) || 100, 1000);
    const r = await invokeSurface("core.audit_query", { last_n });
    if (r.status !== 200) {
      return res.status(r.status).json({ audit: [], error: r.body });
    }
    const payload = r.body?.payload ?? r.body;
    // Core's core.audit_query returns {results, scanned, truncated}.
    // Older shape may have been {audit: [...]} or a bare array. Accept all.
    const arr = Array.isArray(payload)
      ? payload
      : payload?.results ?? payload?.audit ?? [];
    res.json({ audit: arr });
  } catch (e) {
    res.status(502).json({ audit: [], error: e.message });
  }
});

app.post("/api/invoke", async (req, res) => {
  const { to, payload } = req.body || {};
  if (typeof to !== "string" || !to.includes(".")) {
    return res.status(400).json({ error: "missing_or_bad_to" });
  }
  if (payload === undefined || payload === null || typeof payload !== "object") {
    return res.status(400).json({ error: "missing_payload" });
  }
  try {
    const r = await invokeSurface(to, payload);
    res.status(r.status === 200 || r.status === 202 ? 200 : r.status).json({
      core_status: r.status,
      response: r.body,
    });
  } catch (e) {
    res.status(502).json({ error: "invoke_failed", detail: e.message });
  }
});

app.get("/api/health", (_req, res) => {
  res.json({
    node_id: NODE_ID,
    session_id: SESSION_ID,
    core_url: CORE_URL,
    edges_out: RELATIONSHIPS.filter((r) => r.from === NODE_ID),
  });
});

app.get("/api/inbox", async (_req, res) => {
  try {
    const arr = await readInbox();
    // Storage order is chronological (writes are serialized through
    // inboxWriteChain), so reverse for newest-first. received_at is
    // second-precision and can tie within a single second.
    res.json({ inbox: [...arr].reverse() });
  } catch (e) {
    res.status(500).json({ error: "inbox_read_failed", detail: e.message });
  }
});

app.post("/api/inbox/:id/read", async (req, res) => {
  const { id } = req.params;
  try {
    let found = false;
    await withInbox((arr) => {
      for (const entry of arr) {
        if (entry.id === id) {
          entry.read = true;
          found = true;
        }
      }
      return found ? arr : undefined;
    });
    if (!found) return res.status(404).json({ error: "not_found" });
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ error: "inbox_mark_read_failed", detail: e.message });
  }
});

app.post("/api/inbox/clear", async (_req, res) => {
  try {
    let cleared = 0;
    await withInbox((arr) => {
      cleared = arr.length;
      return [];
    });
    res.json({ ok: true, cleared });
  } catch (e) {
    res.status(500).json({ error: "inbox_clear_failed", detail: e.message });
  }
});

(async () => {
  await initInbox();
  try {
    await register();
  } catch (e) {
    console.error(`[control] ${e.message}`);
    console.error("[control] check CORE_URL is reachable and CONTROL_SECRET matches the manifest");
    process.exit(1);
  }
  startSseConsumer();
  app.listen(PORT, () => {
    console.log(`[control] http://localhost:${PORT}`);
  });
})();
