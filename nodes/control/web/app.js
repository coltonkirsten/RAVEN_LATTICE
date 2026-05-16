"use strict";

const POLL_INTROSPECT_MS = 2000;
const POLL_AUDIT_MS = 2000;
const MAX_AUDIT_ENTRIES = 500;

const statusEl = document.getElementById("status");
const auditListEl = document.getElementById("audit-list");
const auditNoteEl = document.getElementById("audit-note");
const auditRefreshBtn = document.getElementById("audit-refresh");
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

  const edgeKeys = new Set();
  const visEdges = [];
  for (const e of edgesIn) {
    if (!e || !e.from || !e.to) continue;
    const toNode = String(e.to).split(".")[0];
    const key = `${e.from}->${e.to}`;
    if (edgeKeys.has(key)) continue;
    edgeKeys.add(key);
    visEdges.push({
      id: key,
      from: e.from,
      to: toNode,
      label: String(e.to).split(".")[1] || "",
      font: { color: "#8492a6", size: 10, strokeWidth: 0, align: "middle" },
      title: `${e.from} → ${e.to}`,
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

pollIntrospect();
pollAudit();
setInterval(pollIntrospect, POLL_INTROSPECT_MS);
setInterval(pollAudit, POLL_AUDIT_MS);
