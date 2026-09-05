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
  if (CODING_TYPES.some((t) => e.type.startsWith(t))) coding.onEvent(e);
  scheduleRender();
}

// -------- coding mode (SPEC §12.1) --------------------------------------------------------------
// Editor (Monaco from /hud/vendor, never a CDN; textarea fallback), diff, terminal, preview,
// agent rail, quality and artifacts - all derived from persisted events of the selected mission.
const CODING_TYPES = ["workspace.", "agent.", "artifact.", "verification.", "mission."];
const CODING_LOG_MAX = 2000;
const ERR = "\u0001"; // marks stderr chunks in the terminal buffer
const editor = {
  inst: null, monaco: null,
  async boot() {
    const loaded = await new Promise((res) => {
      const sc = document.createElement("script"); sc.src = "/hud/vendor/monaco/vs/loader.js";
      sc.onload = () => res(true); sc.onerror = () => res(false); document.head.appendChild(sc);
    });
    if (!loaded || !window.require) return this.info("textarea editor · run vendor/fetch_monaco.py for Monaco");
    window.require.config({ paths: { vs: "/hud/vendor/monaco/vs" } });
    await new Promise((res) => window.require(["vs/editor/editor.main"], () => res(true), () => res(false)));
    if (!window.monaco) return this.info("textarea editor · Monaco failed to load");
    this.monaco = window.monaco;
    const host = document.createElement("div"); host.className = "monaco-host";
    $("codeArea").hidden = true; $("codeView").appendChild(host); $("codeView").dataset.editor = "monaco";
    this.inst = this.monaco.editor.create(host, { value: "", theme: "vs-dark", automaticLayout: true, fontSize: 12, minimap: { enabled: false }, scrollBeyondLastLine: false });
    this.inst.onDidChangeModelContent(() => coding.markDirty());
    this.info("monaco");
  },
  info(t) { $("editorInfo").textContent = t; },
  set(mission, path, content) {
    if (!this.inst) { $("codeArea").value = content; return; }
    const uri = this.monaco.Uri.parse(`jarvis://${mission}/${path}`);
    const old = this.monaco.editor.getModel(uri); if (old) old.dispose();
    const prev = this.inst.getModel();
    this.inst.setModel(this.monaco.editor.createModel(content, undefined, uri));
    if (prev) prev.dispose();
  },
  get() { return this.inst ? this.inst.getValue() : $("codeArea").value; },
};
const coding = {
  mission: null, file: null, tab: "files", term: [], dirty: false, editing: false, log: new Map(),
  onEvent(e) {
    const cid = e.correlation_id;
    const arr = this.log.get(cid) || []; arr.push(e); if (arr.length > CODING_LOG_MAX) arr.shift(); this.log.set(cid, arr);
    if (e.type === "workspace.file.changed" && (!this.mission || cid === this.mission)) {
      this.mission ||= cid;
      this.dirty = true; queueCoding();
      if (e.payload.path === this.file) { if (this.editing) editor.info(`${this.file} changed on disk by ${e.payload.actor}`); else this.loadFile(this.file, String(e.payload.actor).startsWith("owner:")); }
      $("diffView").querySelector("code").innerHTML = colorDiff(e.payload.diff || "");
    }
    if (cid !== this.mission) return;
    if (e.type === "workspace.run.started") { this.term = [`$ ${e.payload.command} ${(e.payload.args || []).join(" ")}\n`]; this.renderTerm(); }
    if (e.type === "workspace.run.output") { this.term.push(e.payload.stream === "stderr" ? ERR + e.payload.chunk : e.payload.chunk); this.renderTerm(); }
    if (e.type === "workspace.run.finished") { this.term.push(`\n[exit ${e.payload.exit_code}${e.payload.timed_out ? " · timeout" : ""} · ${e.payload.duration_ms} ms]\n`); this.renderTerm(); }
    if (e.type.startsWith("agent.") || e.type.startsWith("mission.") || e.type.startsWith("verification.") || e.type === "workspace.run.finished" || e.type === "artifact.created") queueCoding();
  },
  renderTerm() {
    const code = $("terminalView").querySelector("code");
    code.innerHTML = this.term.map((c) => c.startsWith(ERR) ? `<span class="err">${esc(c.slice(1))}</span>` : esc(c)).join("");
    $("terminalView").scrollTop = $("terminalView").scrollHeight;
  },
  events() { return this.log.get(this.mission) || []; },
  renderAgents() {
    const runs = new Map(); const timeline = [];
    for (const e of this.events()) {
      const p = e.payload || {};
      if (e.type === "agent.run.started" || e.type === "agent.subrun.started") runs.set(p.run.run_id, { ...p.run, status: "running", tools: [], rejected: 0 });
      const r = runs.get(p.run ? p.run.run_id : p.run_id);
      if (!r) { if (e.type.startsWith("mission.")) timeline.push(`${fmtTime(e.timestamp)} ${e.type.slice(8)} ${p.reason || ""}`); continue; }
      if (e.type === "agent.tool.proposed") r.tools.push(p.call.name);
      if (e.type === "agent.tool.rejected") r.rejected++;
      if (e.type === "agent.run.paused") r.status = "awaiting approval";
      if (e.type === "agent.run.resumed") r.status = "running";
      if (e.type === "agent.run.budget_exceeded") r.status = `budget ${p.dimension}`;
      if (e.type === "agent.run.finished" || e.type === "agent.subrun.finished") Object.assign(r, p.run, { status: p.run.outcome });
    }
    const cls = (st) => st === "completed" ? "ok" : st === "running" ? "" : st === "awaiting approval" ? "wait" : "bad";
    const rows = [...runs.values()].map((r) => `<div class="row ${r.depth ? "sub" : ""}"><span class="role">${r.depth ? "↳ " : ""}${esc(r.role || "coordinator")}</span><span class="${cls(r.status)}">${esc(r.status || "")}</span><span class="muted">${r.steps} steps · ${r.tools.length} tools${r.rejected ? ` · ${r.rejected} rejected` : ""} · $${r.cost_usd}</span>${r.error ? `<span class="bad">${esc(r.error).slice(0, 80)}</span>` : ""}</div>`);
    $("agentRail").innerHTML = (rows.length ? rows.join("") : `<div class="empty">No agent runs for this mission.</div>`) + (timeline.length ? `<div class="muted" style="margin-top:8px">TIMELINE</div>` + timeline.map((t) => `<div class="row muted">${esc(t)}</div>`).join("") : "");
  },
  renderQuality() {
    const runs = []; let last = null; let verdict = "";
    for (const e of this.events()) {
      const p = e.payload || {};
      if (e.type === "workspace.run.started") last = { cmd: `${p.command} ${(p.args || []).join(" ")}`, exit: null, ms: null, verified: "…" };
      if (e.type === "workspace.run.finished" && last) { Object.assign(last, { exit: p.exit_code, ms: p.duration_ms, timed_out: p.timed_out }); runs.push(last); }
      if (e.type.startsWith("verification.") && e.type !== "verification.skipped" && p.verification && p.verification.capability === "workspace.run" && runs.length) runs[runs.length - 1].verified = p.verification.outcome;
      if (e.type === "agent.run.finished") verdict = `${p.run.outcome}${p.run.error ? ` · ${p.run.error}` : ""}`;
    }
    const green = runs.length && runs[runs.length - 1].exit === 0 && runs[runs.length - 1].verified === "achieved";
    $("qualityView").innerHTML = `<div class="row"><b class="${green ? "ok" : "bad"}">${green ? "✓ last run green" : runs.length ? "✗ last run red" : "no run yet"}</b><span class="muted">${runs.length} runs · mission ${esc(verdict || "in progress")}</span></div>` +
      runs.map((r) => `<div class="row"><span class="${r.exit === 0 ? "ok" : "bad"}">${r.exit === 0 ? "✓" : "✗"}</span><span>${esc(r.cmd)}</span><span class="muted">exit ${r.exit}${r.timed_out ? " · timeout" : ""} · ${r.ms} ms · verifier ${r.verified}</span></div>`).join("");
  },
  renderArtifacts() {
    const arts = this.events().filter((e) => e.type === "artifact.created").map((e) => e.payload);
    $("artifactView").innerHTML = arts.length ? arts.map((a) => `<div class="row"><span>${esc(a.path)}</span><span class="muted">${a.size} B · ${short(a.sha256)}</span><button data-open="${esc(a.path)}">open</button>${/\.(html?|svg|png|jpe?g|gif)$/i.test(a.path) ? `<button data-preview="${esc(a.path)}">preview</button>` : ""}</div>`).join("") : `<div class="empty">No artifacts yet - they appear when a coding mission completes.</div>`;
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
    if (!$("previewFrame").dataset.pinned) $("previewFrame").src = html ? `/workspace/${current}/preview/${html.path}` : "about:blank";
    this.renderAgents(); this.renderQuality(); this.renderArtifacts();
  },
  async loadFile(path, keepInfo = false) {
    this.file = path;
    const { content } = await api(`/workspace/${this.mission}/file?path=${encodeURIComponent(path)}`);
    editor.set(this.mission, path, content); this.editing = false; $("saveBtn").disabled = true; if (!keepInfo) editor.info(path);
    const { diff } = await api(`/workspace/${this.mission}/diff?path=${encodeURIComponent(path)}`);
    $("diffView").querySelector("code").innerHTML = diff ? colorDiff(diff) : "No changes since the last version.";
    for (const el of $("fileTree").children) el.classList.toggle("active", el.dataset.path === path);
  },
  markDirty() { if (this.file) { this.editing = true; $("saveBtn").disabled = false; editor.info(`${this.file} · unsaved`); } },
  async save() {
    if (!this.file || !this.mission) return;
    const body = { path: this.file, content: editor.get(), device_id: "hud", device_trusted: $("trusted").checked };
    const r = await api(`/workspace/${this.mission}/file`, { method: "PUT", body: JSON.stringify(body) }).catch((err) => ({ status: "error", error: String(err) }));
    if (r.status === "completed") { this.editing = false; $("saveBtn").disabled = true; editor.info(`${this.file} · saved · verified ${r.verification.outcome}`); }
    else if (r.status === "waiting_for_approval") editor.info(`${this.file} · waiting for approval`);
    else editor.info(`${this.file} · ${r.status}: ${r.error || ""}`);
  },
  showTab(tab) {
    this.tab = tab;
    for (const el of document.querySelectorAll('.coding-main [data-tab]')) el.hidden = el.dataset.tab !== tab;
    for (const b of $("codingTabs").children) b.classList.toggle("active", b.dataset.tab === tab);
  },
};
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const colorDiff = (d) => esc(d).split("\n").map((l) => l.startsWith("+") && !l.startsWith("+++") ? `<span class="add">${l}</span>` : l.startsWith("-") && !l.startsWith("---") ? `<span class="del">${l}</span>` : l.startsWith("@@") ? `<span class="hunk">${l}</span>` : l).join("\n");
let codingTimer = null;
function queueCoding() { if (!codingTimer) codingTimer = setTimeout(() => { codingTimer = null; coding.refresh(); }, 200); }
$("codingTabs").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) coding.showTab(b.dataset.tab); });
$("codingMission").addEventListener("change", (e) => { coding.mission = e.target.value; coding.file = null; delete $("previewFrame").dataset.pinned; coding.refresh(); });
$("fileTree").addEventListener("click", (e) => { const d = e.target.closest("div[data-path]"); if (d && d.dataset.dir !== "true") { coding.loadFile(d.dataset.path); coding.showTab("files"); } });
$("codeArea").addEventListener("input", () => coding.markDirty());
$("saveBtn").addEventListener("click", () => coding.save());
$("artifactView").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.dataset.open) { coding.loadFile(b.dataset.open); coding.showTab("files"); }
  if (b.dataset.preview) { $("previewFrame").dataset.pinned = "1"; $("previewFrame").src = `/workspace/${coding.mission}/preview/${b.dataset.preview}`; coding.showTab("preview"); }
});
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
connect(); refreshHealth(); refreshLists(); coding.refresh(); editor.boot(); setInterval(refreshHealth, 5000);
