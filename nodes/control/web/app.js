"use strict";

const POLL_INTROSPECT_MS = 2000;
const POLL_AUDIT_MS = 2000;
const POLL_INBOX_MS = 3000;
const MAX_AUDIT_ENTRIES = 500;
const FILTERED_CORR_CAP = 1000;
const FILTER_AUDIT_POLLS_KEY = "lattice_control_filter_audit_polls";

const statusEl = document.getElementById("status");
const auditListEl = document.getElementById("audit-list");
const auditNoteEl = document.getElementById("audit-note");
const auditRefreshBtn = document.getElementById("audit-refresh");
const auditFilterEl = document.getElementById("audit-filter-polls");
const inboxListEl = document.getElementById("inbox-list");
const inboxEmptyEl = document.getElementById("inbox-empty");
const inboxTitleEl = document.getElementById("inbox-title");
const inboxClearBtn = document.getElementById("inbox-clear");
const sendToEl = document.getElementById("send-to");
const sendForm = document.getElementById("send-form");
const sendPayloadEl = document.getElementById("send-payload");
const sendResponseEl = document.getElementById("send-response");
const graphEl = document.getElementById("graph");

const nodesDataset = new vis.DataSet([]);
const edgesDataset = new vis.DataSet([]);

const network = new vis.Network(
  graphEl,
  { nodes: nodesDataset, edges: edgesDataset },
  {
    nodes: {
      shape: "dot",
      size: 22,
      font: { color: "#d6deeb", face: "ui-monospace, Menlo, monospace", size: 13 },
      borderWidth: 2,
    },
    edges: {
      arrows: { to: { enabled: true, scaleFactor: 0.6 } },
      color: { color: "#3a4252", highlight: "#6cb0ff", hover: "#6cb0ff" },
      smooth: { type: "dynamic" },
      width: 1.2,
    },
    physics: {
      solver: "forceAtlas2Based",
      forceAtlas2Based: { gravitationalConstant: -45, springLength: 120, springConstant: 0.08 },
      stabilization: { iterations: 120 },
    },
    interaction: { hover: true, tooltipDelay: 200, dragNodes: true, zoomView: true },
  }
);

// Freeze layout once initial stabilization completes — prevents the
// visualizer from jumping every poll. Topology changes will re-enable
// physics briefly via setPhysicsForTopologyChange().
network.once("stabilizationIterationsDone", () => {
  network.setOptions({ physics: { enabled: false } });
});

function setPhysicsForTopologyChange() {
  network.setOptions({ physics: { enabled: true } });
  // Let it re-stabilize then freeze again.
  network.once("stabilizationIterationsDone", () => {
    network.setOptions({ physics: { enabled: false } });
  });
  network.stabilize(80);
}

let lastNodeIds = new Set();
let lastEdgeKeys = new Set();
let seenAuditIds = new Set();
let currentRelationships = [];

// Track correlation_ids of audit-poll invocations we've filtered out, so we
// can also filter the paired response rows (from=core, to=control). Bounded
// to avoid unbounded growth; oldest entries evicted FIFO.
const filteredCorrIds = new Set();
const filteredCorrOrder = [];

function rememberFilteredCorr(id) {
  if (!id || filteredCorrIds.has(id)) return;
  filteredCorrIds.add(id);
  filteredCorrOrder.push(id);
  while (filteredCorrOrder.length > FILTERED_CORR_CAP) {
    const old = filteredCorrOrder.shift();
    filteredCorrIds.delete(old);
  }
}

function shouldFilterAuditEntry(entry) {
  if (!auditFilterEl?.checked) return false;
  const from = entry.from_node;
  const to = entry.to_surface;
  const corr = entry.correlation_id;
  // The poll invocation itself.
  if (from === "control" && to === "core.audit_query") {
    rememberFilteredCorr(corr || entry.id);
    return true;
  }
  // The paired response (core -> control), matched by correlation_id.
  if (from === "core" && to === "control" && corr && filteredCorrIds.has(corr)) {
    return true;
  }
  return false;
}

function buildEdgeTitle(edges) {
  const wrap = document.createElement("div");
  wrap.className = "edge-tooltip";
  const table = document.createElement("table");
  for (const e of edges) {
    const tr = document.createElement("tr");
    const tdFrom = document.createElement("td");
    tdFrom.className = "from";
    tdFrom.textContent = e.from;
    const tdArrow = document.createElement("td");
    tdArrow.className = "arrow";
    tdArrow.textContent = "→";
    const tdTo = document.createElement("td");
    tdTo.className = "to";
    tdTo.textContent = e.to;
    tr.append(tdFrom, tdArrow, tdTo);
    table.appendChild(tr);
  }
  wrap.appendChild(table);
  return wrap;
}

function colorForKind(kind, isCore) {
  if (isCore) return { background: "#374151", border: "#6b7280" };
  if (kind === "actor") return { background: "#1f3a2a", border: "#6ee7b7" };
  if (kind === "capability") return { background: "#1f2a3a", border: "#6cb0ff" };
  return { background: "#2a223a", border: "#c4b5fd" };
}

function rebuildGraph(introspect) {
  const nodesIn = introspect.nodes || [];
  const edgesIn = introspect.relationships || introspect.edges || [];

  const connectedFrom = new Set();
  const connectedTo = new Set();
  for (const e of edgesIn) {
    if (!e || !e.from || !e.to) continue;
    connectedFrom.add(e.from);
    const targetNode = String(e.to).split(".")[0];
    connectedTo.add(targetNode);
  }

  const nodeIds = new Set();
  const visNodes = [];
  for (const n of nodesIn) {
    const id = n.id || n.node_id;
    if (!id) continue;
    nodeIds.add(id);
    const isCore = id === "core";
    const kind = n.kind || n.metadata?.kind;
    const color = colorForKind(kind, isCore);
    const isConnected = connectedFrom.has(id) || connectedTo.has(id);
    const surfaces = (n.surfaces || []).map((s) => s.name).join(", ");
    visNodes.push({
      id,
      label: id,
      color: isConnected
        ? color
        : { background: "transparent", border: color.border },
      borderWidth: isConnected ? 2 : 2,
      title: `${id}${kind ? ` (${kind})` : ""}${surfaces ? `\nsurfaces: ${surfaces}` : ""}`,
    });
  }

  // Bundle edges by unordered node-pair so multiple surface relationships
  // between the same two nodes collapse to a single visual edge. Self-loops
  // (from === to) are bundled per-node.
  const edgeBundles = new Map(); // pairKey -> { a, b, edges[], aToB, bToA }
  const selfBundles = new Map(); // nodeId -> edges[]
  for (const e of edgesIn) {
    if (!e || !e.from || !e.to) continue;
    const fromNode = String(e.from).split(".")[0];
    const toNode = String(e.to).split(".")[0];
    if (fromNode === toNode) {
      if (!selfBundles.has(fromNode)) selfBundles.set(fromNode, []);
      selfBundles.get(fromNode).push(e);
      continue;
    }
    const sorted = [fromNode, toNode].sort();
    const pairKey = `${sorted[0]}|${sorted[1]}`;
    let bundle = edgeBundles.get(pairKey);
    if (!bundle) {
      bundle = { a: sorted[0], b: sorted[1], edges: [], aToB: false, bToA: false };
      edgeBundles.set(pairKey, bundle);
    }
    bundle.edges.push(e);
    if (fromNode === sorted[0]) bundle.aToB = true;
    else bundle.bToA = true;
  }

  const edgeKeys = new Set();
  const visEdges = [];
  const labelFont = {
    color: "#d6deeb",
    size: 11,
    strokeWidth: 0,
    background: "#161b22",
    align: "middle",
    face: "ui-monospace, Menlo, monospace",
  };
  for (const [pairKey, bundle] of edgeBundles) {
    edgeKeys.add(pairKey);
    visEdges.push({
      id: pairKey,
      from: bundle.a,
      to: bundle.b,
      label: String(bundle.edges.length),
      font: labelFont,
      title: buildEdgeTitle(bundle.edges),
      arrows: {
        to: { enabled: bundle.aToB, scaleFactor: 0.6 },
        from: { enabled: bundle.bToA, scaleFactor: 0.6 },
      },
    });
  }
  for (const [nodeId, edges] of selfBundles) {
    const selfKey = `${nodeId}|${nodeId}`;
    edgeKeys.add(selfKey);
    visEdges.push({
      id: selfKey,
      from: nodeId,
      to: nodeId,
      label: String(edges.length),
      font: labelFont,
      title: buildEdgeTitle(edges),
      arrows: { to: { enabled: true, scaleFactor: 0.6 } },
    });
  }

  const topologyChanged =
    nodeIds.size !== lastNodeIds.size ||
    edgeKeys.size !== lastEdgeKeys.size ||
    [...nodeIds].some((id) => !lastNodeIds.has(id)) ||
    [...edgeKeys].some((k) => !lastEdgeKeys.has(k));

  // Diff-update nodes: update existing in place (preserves layout positions),
  // add new, remove gone.
  const removedNodeIds = [...lastNodeIds].filter((id) => !nodeIds.has(id));
  if (removedNodeIds.length) nodesDataset.remove(removedNodeIds);
  nodesDataset.update(visNodes);

  // Same for edges.
  const removedEdgeKeys = [...lastEdgeKeys].filter((k) => !edgeKeys.has(k));
  if (removedEdgeKeys.length) edgesDataset.remove(removedEdgeKeys);
  edgesDataset.update(visEdges);

  if (topologyChanged) {
    setPhysicsForTopologyChange();
  }

  lastNodeIds = nodeIds;
  lastEdgeKeys = edgeKeys;
  currentRelationships = edgesIn;
  updateSendOptions(edgesIn);
}

function updateSendOptions(edges) {
  const targets = [
    ...new Set(edges.filter((e) => e.from === "control").map((e) => e.to)),
  ].sort();
  const prev = sendToEl.value;
  sendToEl.innerHTML = "";
  if (targets.length === 0) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "(no outgoing edges from control)";
    opt.disabled = true;
    opt.selected = true;
    sendToEl.appendChild(opt);
  } else {
    for (const t of targets) {
      const opt = document.createElement("option");
      opt.value = t;
      opt.textContent = t;
      sendToEl.appendChild(opt);
    }
    if (targets.includes(prev)) sendToEl.value = prev;
  }
}

function decisionClass(decision) {
  if (!decision) return "other";
  if (decision === "routed") return "routed";
  if (decision.startsWith("denied") || decision === "timeout") return "denied";
  return "other";
}

function renderAuditEntry(entry) {
  const id = entry.id || `${entry.timestamp}-${entry.from_node}-${entry.to_surface}`;
  if (seenAuditIds.has(id)) return null;
  seenAuditIds.add(id);
  if (shouldFilterAuditEntry(entry)) return null;
  const li = document.createElement("li");
  const ts = (entry.timestamp || "").replace("T", " ").replace("Z", "");
  const route = `${entry.from_node || "?"} → ${entry.to_surface || "?"}`;
  const dec = entry.decision || entry.type || "?";
  li.innerHTML =
    `<span class="ts">${escapeHtml(ts)}</span>` +
    `<span class="route">${escapeHtml(route)}</span>` +
    `<span class="decision ${decisionClass(dec)}">${escapeHtml(dec)}</span>`;
  li.title = JSON.stringify(entry, null, 2);
  return li;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function pollIntrospect() {
  try {
    const r = await fetch("/api/introspect");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    rebuildGraph(data);
    statusEl.textContent = `connected · ${(data.nodes || []).length} nodes`;
    statusEl.className = "status ok";
  } catch (e) {
    statusEl.textContent = `introspect error: ${e.message}`;
    statusEl.className = "status err";
  }
}

async function pollAudit() {
  try {
    const r = await fetch("/api/audit/poll");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    if (data.note) {
      auditNoteEl.textContent = data.note;
    } else {
      auditNoteEl.textContent = "";
    }
    const entries = data.audit || [];
    const sorted = [...entries].sort((a, b) => {
      const ta = a.timestamp || "";
      const tb = b.timestamp || "";
      return ta < tb ? -1 : ta > tb ? 1 : 0;
    });
    for (const entry of sorted) {
      const li = renderAuditEntry(entry);
      if (li) auditListEl.insertBefore(li, auditListEl.firstChild);
    }
    while (auditListEl.childElementCount > MAX_AUDIT_ENTRIES) {
      auditListEl.removeChild(auditListEl.lastChild);
    }
  } catch (e) {
    auditNoteEl.textContent = `audit error: ${e.message}`;
  }
}

sendForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const to = sendToEl.value;
  if (!to) return;
  let payload;
  try {
    payload = JSON.parse(sendPayloadEl.value);
  } catch (e) {
    sendResponseEl.textContent = `payload parse error: ${e.message}`;
    return;
  }
  const btn = sendForm.querySelector("button");
  btn.disabled = true;
  sendResponseEl.textContent = "sending…";
  try {
    const r = await fetch("/api/invoke", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ to, payload }),
    });
    const body = await r.json();
    sendResponseEl.textContent = JSON.stringify(body, null, 2);
  } catch (e) {
    sendResponseEl.textContent = `invoke error: ${e.message}`;
  } finally {
    btn.disabled = false;
  }
});

if (auditRefreshBtn) {
  auditRefreshBtn.addEventListener("click", async () => {
    auditRefreshBtn.disabled = true;
    auditRefreshBtn.classList.add("spinning");
    try {
      await pollAudit();
    } finally {
      setTimeout(() => {
        auditRefreshBtn.disabled = false;
        auditRefreshBtn.classList.remove("spinning");
      }, 300);
    }
  });
}

function resetAuditList() {
  seenAuditIds = new Set();
  filteredCorrIds.clear();
  filteredCorrOrder.length = 0;
  auditListEl.innerHTML = "";
}

if (auditFilterEl) {
  const stored = localStorage.getItem(FILTER_AUDIT_POLLS_KEY);
  auditFilterEl.checked = stored === null ? true : stored === "1";
  auditFilterEl.addEventListener("change", () => {
    localStorage.setItem(FILTER_AUDIT_POLLS_KEY, auditFilterEl.checked ? "1" : "0");
    resetAuditList();
    pollAudit();
  });
}

// -------------------------------------------------------------------
// Inbox
// -------------------------------------------------------------------

let inboxKnownIds = new Set();
let inboxFirstLoad = true;

function formatInboxTs(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function renderInbox(entries) {
  const unread = entries.filter((e) => !e.read).length;
  inboxTitleEl.innerHTML =
    `inbox` + (unread > 0 ? ` <span class="unread-badge">${unread}</span>` : "");

  inboxEmptyEl.style.display = entries.length === 0 ? "block" : "none";
  inboxClearBtn.disabled = entries.length === 0;

  const currentIds = new Set(entries.map((e) => e.id));
  const newlyArrived = inboxFirstLoad
    ? new Set()
    : new Set([...currentIds].filter((id) => !inboxKnownIds.has(id)));

  inboxListEl.innerHTML = "";
  for (const entry of entries) {
    const li = document.createElement("li");
    li.dataset.id = entry.id;
    if (!entry.read) li.classList.add("unread");
    if (newlyArrived.has(entry.id)) li.classList.add("flash");

    const text =
      entry.payload && typeof entry.payload === "object" && typeof entry.payload.text === "string"
        ? entry.payload.text
        : null;

    const head = `<div class="inbox-head">` +
      `<span class="inbox-from">${escapeHtml(entry.from || "?")}</span>` +
      `<span class="inbox-ts">${escapeHtml(formatInboxTs(entry.received_at))}</span>` +
      `</div>`;

    let bodyHtml;
    if (text !== null) {
      bodyHtml =
        `<div class="inbox-text">${escapeHtml(text)}</div>` +
        `<details class="inbox-raw"><summary>raw</summary>` +
        `<pre>${escapeHtml(JSON.stringify(entry.payload, null, 2))}</pre></details>`;
    } else {
      bodyHtml = `<pre class="inbox-raw"><code>${escapeHtml(JSON.stringify(entry.payload, null, 2))}</code></pre>`;
    }

    li.innerHTML = head + bodyHtml;
    li.addEventListener("click", (ev) => {
      // Don't mark-read on clicks inside <details> / <summary> toggles.
      if (ev.target instanceof Element && ev.target.closest("details, summary, pre")) return;
      if (entry.read) return;
      markInboxRead(entry.id, li);
    });
    inboxListEl.appendChild(li);
  }

  inboxKnownIds = currentIds;
  inboxFirstLoad = false;
}

async function markInboxRead(id, li) {
  li.classList.remove("unread");
  const fromEl = li.querySelector(".inbox-from");
  if (fromEl) fromEl.style.color = "";
  try {
    await fetch(`/api/inbox/${encodeURIComponent(id)}/read`, { method: "POST" });
  } catch (e) {
    console.error("[inbox] mark-read failed:", e);
  }
  pollInbox();
}

async function pollInbox() {
  try {
    const r = await fetch("/api/inbox");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    renderInbox(data.inbox || []);
  } catch (e) {
    console.error("[inbox] poll failed:", e.message);
  }
}

inboxClearBtn.addEventListener("click", async () => {
  const count = inboxListEl.childElementCount;
  if (count === 0) return;
  if (!confirm(`Clear all ${count} message${count === 1 ? "" : "s"}?`)) return;
  inboxClearBtn.disabled = true;
  try {
    await fetch("/api/inbox/clear", { method: "POST" });
    inboxFirstLoad = true; // don't flash on next render
    await pollInbox();
  } catch (e) {
    console.error("[inbox] clear failed:", e);
    inboxClearBtn.disabled = false;
  }
});

pollIntrospect();
pollAudit();
pollInbox();
setInterval(pollIntrospect, POLL_INTROSPECT_MS);
setInterval(pollAudit, POLL_AUDIT_MS);
setInterval(pollInbox, POLL_INBOX_MS);
