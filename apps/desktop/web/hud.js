// JARVIS HUD client (ADR-0003). Renders only persisted Core events (docs/HUD_EVENTS.md).
// Fluidity: events are batched per animation frame; no synchronous work in the render path;
// the websocket reconnects from the last seen seq; /health is polled at most every 5 s.

const $ = (id) => document.getElementById(id);
const state = { lastSeq: 0, events: [], filter: "", latency: {}, presence: null, missions: [], approvals: [] };
const RAIL_MAX = 400;

// -------- helpers -------------------------------------------------------------------------
const area = (type) => type.split(".")[0];
const short = (s) => (s || "").slice(0, 8);
const fmtTime = (iso) => (iso || "").slice(11, 19);
async function api(path, opts) {
  const r = await fetch(path, { headers: { "content-type": "application/json" }, ...opts });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}
const proof = (method) => ({ method, device_id: "hud", device_trusted: true, reference: "hud" });

// -------- render (called from rAF only) ------------------------------------------------------
let dirty = { rail: false, presence: false, latency: false };
function scheduleRender() { if (!scheduleRender.pending) { scheduleRender.pending = true; requestAnimationFrame(render); } }
function render() {
  scheduleRender.pending = false;
  if (dirty.presence && state.presence) renderPresence();
  if (dirty.rail) renderRail();
  if (dirty.latency) renderLatency();
  dirty = { rail: false, presence: false, latency: false };
}
function renderPresence() {
  const p = state.presence;
  document.body.dataset.state = p.state;
  $("coreLabel").textContent = p.state.replace("_", " ").toUpperCase();
  $("coreDevice").textContent = p.device_id;
  $("coreSince").textContent = fmtTime(p.since);
  $("coreMission").textContent = p.active_mission ? `mission ${short(p.active_mission)}` : "";
}
function renderRail() {
  const rail = $("rail");
  const frag = document.createDocumentFragment();
  const shown = state.events.filter((e) => !state.filter || area(e.type) === state.filter).slice(-RAIL_MAX);
  for (const e of shown) {
    const d = document.createElement("div");
    d.className = `ev ${area(e.type)} ${e.priority === "critical" ? "critical" : ""}`;
    d.title = JSON.stringify(e.payload);
    d.innerHTML = `<span class="s">#${e.seq} ${fmtTime(e.timestamp)}</span> <span class="t">${e.type}</span> <span class="s">${e.source} · ${short(e.correlation_id)}</span> ${summary(e)}`;
    frag.appendChild(d);
  }
  rail.replaceChildren(frag);
  rail.scrollTop = rail.scrollHeight;
}
function summary(e) {
  const p = e.payload || {};
  if (e.type.startsWith("voice.transcript")) return `“${p.text}”${p.final ? "" : " …"}`;
  if (e.type.startsWith("capability.")) return p.invocation ? `${p.invocation.capability} ${p.invocation.error || ""}` : "";
  if (e.type.startsWith("verification.")) return p.verification ? `${p.verification.capability} → ${p.verification.outcome}` : "";
  if (e.type.startsWith("permission.")) return p.decision ? `${p.decision.request.action} P${p.decision.request.risk} ${p.decision.rule}` : "";
  if (e.type.startsWith("agent.run.")) return p.run ? `${p.run.role} ${p.run.outcome || ""} ${p.run.error || ""}` : "";
  if (e.type.startsWith("mission.")) return p.reason || p.goal || "";
  if (e.type === "telemetry.latency") return `${p.point} ${p.ms} ms${p.within_budget ? "" : " ⚠"}`;
  if (e.type === "presence.changed") return `${p.device_id} → ${p.state}`;
  if (e.type.startsWith("memory.")) return p.memory ? `${p.memory.subject} ${p.memory.predicate}` : "";
  return "";
}
function renderLatency() {
  const tiles = Object.entries(state.latency).map(([point, s]) => {
    const p95 = s.values.length ? s.values.slice().sort((a, b) => a - b)[Math.ceil(0.95 * s.values.length) - 1] : 0;
    const over = s.budget && p95 > s.budget;
    return `<div class="tile ${over ? "over" : ""}">${point} <b>${Math.round(p95)} ms</b> p95${s.budget ? ` / ${s.budget}` : ""}</div>`;
  });
  $("latency").innerHTML = tiles.join("");
}

// -------- data ------------------------------------------------------------------------------
function ingest(e) {
  state.events.push(e);
  if (state.events.length > RAIL_MAX * 2) state.events.splice(0, state.events.length - RAIL_MAX);
  state.lastSeq = Math.max(state.lastSeq, e.seq);
  dirty.rail = true;
  if (e.type === "presence.changed") { state.presence = e.payload; dirty.presence = true; }
  if (e.type === "telemetry.latency") {
    const s = (state.latency[e.payload.point] ||= { values: [], budget: e.payload.budget_ms });
    s.values.push(e.payload.ms); if (s.values.length > 50) s.values.shift();
    dirty.latency = true;
  }
  if (e.type.startsWith("mission.") || e.type.startsWith("permission.") || e.type.startsWith("memory.")) queueRefresh();
  scheduleRender();
}
function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws/events?after_seq=${state.lastSeq}`);
  ws.onopen = () => { $("status").textContent = "live"; };
  ws.onmessage = (m) => ingest(JSON.parse(m.data));
  ws.onclose = () => { $("status").textContent = "reconnecting…"; setTimeout(connect, 1000); };
}
let refreshTimer = null;
function queueRefresh() { if (!refreshTimer) refreshTimer = setTimeout(() => { refreshTimer = null; refreshLists(); }, 150); }
async function refreshLists() {
  const [missions, approvals, memory] = await Promise.all([
    api("/missions"), api("/approvals"), api(`/memory${$("memQuery").value ? `?q=${encodeURIComponent($("memQuery").value)}` : ""}`),
  ]);
  $("approvalCount").textContent = approvals.length;
  $("approvals").innerHTML = approvals.length ? approvals.map((d) => `
    <div class="card approval">${d.request.action} <small>P${d.request.risk} · ${d.rule} · needs ${["none", "voice", "confirm", "strong"][d.required_strength]}</small>
      <div><small>${JSON.stringify(d.request.context.args || {})}</small></div>
      <div class="row"><button class="primary" data-approve="${d.decision_id}" data-method="ui_confirm">CONFIRM</button>
      <button class="primary" data-approve="${d.decision_id}" data-method="passkey">PASSKEY</button>
      <button class="danger" data-deny="${d.decision_id}">DENY</button></div></div>`).join("") : `<div class="empty">Nothing waiting for you.</div>`;
  $("missions").innerHTML = missions.slice(-30).reverse().map((m) => `<div class="mission"><span class="st ${m.status}">${m.status}</span><span>${m.goal}</span></div>`).join("");
  $("memory").innerHTML = memory.length ? memory.slice(0, 25).map((x) => `
    <div class="card">${x.pinned ? "📌 " : ""}<small>[${x.type} · ${x.confidence} · ${x.source}]</small> ${x.subject} ${x.predicate}: ${typeof x.value === "string" ? x.value : JSON.stringify(x.value)}
      <div class="row"><button data-mem="${x.memory_id}" data-act="${x.pinned ? "unpin" : "pin"}">${x.pinned ? "unpin" : "pin"}</button>
      <button data-mem="${x.memory_id}" data-act="correct">correct</button><button class="danger" data-mem="${x.memory_id}" data-act="forget">forget</button></div></div>`).join("") : `<div class="empty">Nothing learned yet.</div>`;
}
async function refreshHealth() {
  try {
    const h = await api("/health");
    $("version").textContent = `v${h.version}`;
    $("status").textContent = `${h.status} · ${h.events} events · ${h.agent_ready ? "agent ready" : "no provider"}`;
    if (!state.presence) { const dev = Object.values(h.presence.devices)[0]; if (dev) { state.presence = { ...dev, state: h.halted ? "halted" : dev.state }; dirty.presence = true; scheduleRender(); } }
  } catch { $("status").textContent = "core unreachable"; }
}

// -------- actions ---------------------------------------------------------------------------
$("cmdForm").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const text = $("cmdInput").value.trim(); if (!text) return;
  $("cmdInput").value = "";
  await api("/commands", { method: "POST", body: JSON.stringify({ text, device_id: "hud", device_trusted: $("trusted").checked }) });
});
document.addEventListener("click", async (ev) => {
  const b = ev.target.closest("button"); if (!b) return;
  if (b.dataset.approve) await api(`/approvals/${b.dataset.approve}/approve`, { method: "POST", body: JSON.stringify(proof(b.dataset.method)) }).catch(alert);
  if (b.dataset.deny) await api(`/approvals/${b.dataset.deny}/deny`, { method: "POST", body: JSON.stringify({ reason: "hud" }) }).catch(alert);
  if (b.dataset.mem) {
    const act = b.dataset.act;
    if (act === "correct") { const v = prompt("Corrected value:"); if (v) await api(`/memory/${b.dataset.mem}/correct`, { method: "POST", body: JSON.stringify({ value: v }) }); }
    else await api(`/memory/${b.dataset.mem}/${act}`, { method: "POST" });
    queueRefresh();
  }
});
$("killBtn").addEventListener("click", () => api("/kill", { method: "POST" }));
$("resumeBtn").addEventListener("click", () => api("/resume", { method: "POST", body: JSON.stringify(proof("passkey")) }).catch(alert));
$("railFilter").addEventListener("change", (e) => { state.filter = e.target.value; dirty.rail = true; scheduleRender(); });
$("memQuery").addEventListener("input", queueRefresh);

// -------- boot ------------------------------------------------------------------------------
connect(); refreshHealth(); refreshLists(); setInterval(refreshHealth, 5000);
