import express from "express";
import crypto from "node:crypto";
import path from "node:path";
import { fileURLToPath } from "node:url";
import fetch from "node-fetch";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const NODE_ID = "control";
const CORE_URL = process.env.CORE_URL || "http://100.109.10.50:8000";
const PORT = parseInt(process.env.PORT || "5190", 10);
const SECRET = process.env.CONTROL_SECRET;

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
          if (frame.startsWith(":")) continue;
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

(async () => {
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
