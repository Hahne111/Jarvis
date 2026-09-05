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
  if (e.type.startsWith("workspace.")) coding.onEvent(e);
  scheduleRender();
}

// -------- coding mode (SPEC §12.1) --------------------------------------------------------------
const coding = {
  mission: null, file: null, tab: "files", term: [], dirty: false,
  onEvent(e) {
    if (e.type === "workspace.file.changed" && (!this.mission || e.correlation_id === this.mission)) {
      this.mission ||= e.correlation_id;
      this.dirty = true; queueCoding();
      if (e.payload.path === this.file) this.loadFile(this.file);
      $("diffView").querySelector("code").innerHTML = colorDiff(e.payload.diff || "");
    }
    if (e.type === "workspace.run.started" && e.correlation_id === this.mission) { this.term = [`$ ${e.payload.command} ${(e.payload.args || []).join(" ")}\n`]; this.renderTerm(); }
    if (e.type === "workspace.run.output" && e.correlation_id === this.mission) { this.term.push(e.payload.stream === "stderr" ? `\u0001${e.payload.chunk}` : e.payload.chunk); this.renderTerm(); }
    if (e.type === "workspace.run.finished" && e.correlation_id === this.mission) { this.term.push(`\n[exit ${e.payload.exit_code}${e.payload.timed_out ? " · timeout" : ""} · ${e.payload.duration_ms} ms]\n`); this.renderTerm(); }
  },
  renderTerm() {
    const code = $("terminalView").querySelector("code");
    code.innerHTML = this.term.map((c) => c.startsWith("\u0001") ? `<span class="err">${esc(c.slice(1))}</span>` : esc(c)).join("");
    $("terminalView").scrollTop = $("terminalView").scrollHeight;
  },
  async refresh() {
    const missions = await api("/missions");
    const sel = $("codingMission");
    const current = this.mission || (missions.length ? missions[missions.length - 1].mission_id : null);
    sel.innerHTML = missions.slice(-20).reverse().map((m) => `<option value="${m.mission_id}" ${m.mission_id === current ? "selected" : ""}>${short(m.mission_id)} · ${esc(m.goal).slice(0, 40)}</option>`).join("");
    if (!current) return;
    this.mission = current;
    const { files } = await api(`/workspace/${current}/files`).catch(() => ({ files: [] }));
    $("fileTree").innerHTML = files.length ? files.map((f) => `<div class="${f.dir ? "dir" : "file"} ${f.path === this.file ? "active" : ""}" data-path="${f.path}" data-dir="${f.dir}">${f.dir ? "▸ " : ""}${esc(f.path)}</div>`).join("") : `<div class="empty">Empty workspace.</div>`;
    const html = files.find((f) => !f.dir && f.path.endsWith("index.html"));
    $("previewFrame").src = html ? `/workspace/${current}/preview/${html.path}` : "about:blank";
  },
  async loadFile(path) {
    this.file = path;
    const { content } = await api(`/workspace/${this.mission}/file?path=${encodeURIComponent(path)}`);
    $("codeView").querySelector("code").textContent = content;
    const { diff } = await api(`/workspace/${this.mission}/diff?path=${encodeURIComponent(path)}`);
    $("diffView").querySelector("code").innerHTML = diff ? colorDiff(diff) : "No changes since the last version.";
    for (const el of $("fileTree").children) el.classList.toggle("active", el.dataset.path === path);
  },
  showTab(tab) {
    this.tab = tab;
    for (const el of document.querySelectorAll('.coding-main [data-tab]')) el.hidden = el.dataset.tab !== tab;
    for (const b of $("codingTabs").children) b.classList.toggle("active", b.dataset.tab === tab);
  },
};
const esc = (s) => String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const colorDiff = (d) => esc(d).split("\n").map((l) => l.startsWith("+") && !l.startsWith("+++") ? `<span class="add">${l}</span>` : l.startsWith("-") && !l.startsWith("---") ? `<span class="del">${l}</span>` : l.startsWith("@@") ? `<span class="hunk">${l}</span>` : l).join("\n");
let codingTimer = null;
function queueCoding() { if (!codingTimer) codingTimer = setTimeout(() => { codingTimer = null; coding.refresh(); }, 200); }
$("codingTabs").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) coding.showTab(b.dataset.tab); });
$("codingMission").addEventListener("change", (e) => { coding.mission = e.target.value; coding.file = null; coding.refresh(); });
$("fileTree").addEventListener("click", (e) => { const d = e.target.closest("div[data-path]"); if (d && d.dataset.dir !== "true") { coding.loadFile(d.dataset.path); coding.showTab("files"); } });
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
connect(); refreshHealth(); refreshLists(); coding.refresh(); setInterval(refreshHealth, 5000);
