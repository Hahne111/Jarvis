// JARVIS World Intelligence Globe (SPEC §13, Phase 10 steps 63/65/68).
// Canvas 2D orthographic globe: no library, no CDN. Renders only persisted data (GET /news/countries,
// GET /news) - country markers sized by event count, breaking stories pulse, click/tap selects a
// country. Adaptive quality tiers keep the frame budget (16.7 ms): the globe measures its own frame
// time and drops detail (graticule density, glow, pulses, frame cap) before it drops frames.
// Frame-time samples are reported in batches as telemetry.latency (point=globe_frame).

const DEG = Math.PI / 180;
const TIERS = {
  high: { grid: 10, glow: true, pulses: true, cap: 0, stars: 120 },
  medium: { grid: 15, glow: false, pulses: true, cap: 0, stars: 60 },
  low: { grid: 30, glow: false, pulses: false, cap: 33, stars: 0 },
};

export class Globe {
  constructor(canvas, { onSelect, onTelemetry, reducedMotion } = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d", { alpha: true });
    this.onSelect = onSelect || (() => {});
    this.onTelemetry = onTelemetry || (() => {});
    this.reducedMotion = !!reducedMotion;
    this.countries = []; // {iso, name, region, lat, lon, count}
    this.events = []; // recent events with lat/lon/breaking
    this.rot = -20 * DEG; // longitude rotation
    this.tilt = 18 * DEG; // latitude tilt
    this.speed = this.reducedMotion ? 0 : 0.12 * DEG; // rad per frame at 60fps
    this.selected = null;
    this.tier = "high";
    this.frames = []; this.lastTs = 0; this.lastReport = 0; this.raf = 0; this.running = false;
    this.drag = null;
    this.stars = [];
    this._bind();
  }

  // -- data ------------------------------------------------------------------------------------
  setCountries(list) { this.countries = list.filter((c) => c.lat != null); }
  setEvents(list) { this.events = list.filter((e) => e.lat != null); }
  select(iso) { this.selected = iso; if (iso) { const c = this.countries.find((x) => x.iso === iso); if (c) this.focus(c); } }
  focus(c) { this.target = { rot: -c.lon * DEG, tilt: Math.max(-60, Math.min(60, c.lat)) * DEG * 0.6 }; }

  // -- lifecycle ---------------------------------------------------------------------------------
  start() { if (this.running) return; this.running = true; this.lastTs = performance.now(); this.raf = requestAnimationFrame((t) => this.frame(t)); }
  stop() { this.running = false; cancelAnimationFrame(this.raf); }
  resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, this.tier === "low" ? 1 : 2);
    const r = this.canvas.getBoundingClientRect();
    const w = Math.max(64, Math.floor(r.width * dpr)), h = Math.max(64, Math.floor(r.height * dpr));
    if (this.canvas.width !== w || this.canvas.height !== h) { this.canvas.width = w; this.canvas.height = h; this.stars = []; }
    this.dpr = dpr;
  }

  // -- projection ------------------------------------------------------------------------------
  project(lat, lon, R, cx, cy) {
    const φ = lat * DEG, λ = lon * DEG + this.rot;
    const cosφ = Math.cos(φ), x = cosφ * Math.sin(λ), y = Math.sin(φ), z = cosφ * Math.cos(λ);
    // tilt around the x axis
    const y2 = y * Math.cos(this.tilt) - z * Math.sin(this.tilt), z2 = y * Math.sin(this.tilt) + z * Math.cos(this.tilt);
    return { x: cx + x * R, y: cy - y2 * R, front: z2 > 0, depth: z2 };
  }

  // -- frame -----------------------------------------------------------------------------------
  frame(ts) {
    if (!this.running) return;
    const dt = ts - this.lastTs; this.lastTs = ts;
    const t = TIERS[this.tier];
    if (t.cap && dt < t.cap) { this.raf = requestAnimationFrame((x) => this.frame(x)); return; }
    const t0 = performance.now();
    this.resize();
    if (this.target && !this.drag) {
      const k = 0.08; this.rot += (this.target.rot - this.rot) * k; this.tilt += (this.target.tilt - this.tilt) * k;
      if (Math.abs(this.target.rot - this.rot) < 0.002) this.target = null;
    } else if (!this.drag) this.rot += this.speed * (dt / 16.7);
    this.draw(ts);
    const cost = performance.now() - t0;
    this.frames.push(cost);
    if (ts - this.lastReport > 5000 && this.frames.length > 10) this.report(ts);
    this.raf = requestAnimationFrame((x) => this.frame(x));
  }

  report(ts) {
    const s = this.frames.slice().sort((a, b) => a - b);
    const p95 = s[Math.min(s.length - 1, Math.ceil(0.95 * s.length) - 1)];
    const before = this.tier;
    if (p95 > 14 && this.tier === "high") this.tier = "medium";
    else if (p95 > 14 && this.tier === "medium") this.tier = "low";
    else if (p95 < 6 && this.tier === "low") this.tier = "medium";
    else if (p95 < 6 && this.tier === "medium") this.tier = "high";
    this.onTelemetry({ point: "globe_frame", ms: Math.round(p95 * 100) / 100, samples: s.length, tier: this.tier, changed: before !== this.tier });
    this.frames = []; this.lastReport = ts;
  }

  // -- drawing ---------------------------------------------------------------------------------
  draw(ts) {
    const ctx = this.ctx, W = this.canvas.width, H = this.canvas.height, t = TIERS[this.tier];
    const R = Math.min(W, H) * 0.42, cx = W / 2, cy = H / 2;
    ctx.clearRect(0, 0, W, H);
    // stars (static per size)
    if (t.stars && !this.stars.length) for (let i = 0; i < t.stars; i++) this.stars.push([Math.random() * W, Math.random() * H, Math.random() * 1.2 + 0.3]);
    if (t.stars) { ctx.fillStyle = "rgba(207,227,238,.35)"; for (const [x, y, r] of this.stars) { ctx.beginPath(); ctx.arc(x, y, r, 0, 7); ctx.fill(); } }
    // sphere body
    const g = ctx.createRadialGradient(cx - R * 0.35, cy - R * 0.35, R * 0.1, cx, cy, R);
    g.addColorStop(0, "#123247"); g.addColorStop(0.7, "#0b1a26"); g.addColorStop(1, "#070c12");
    ctx.beginPath(); ctx.arc(cx, cy, R, 0, 7); ctx.fillStyle = g; ctx.fill();
    if (t.glow) { ctx.shadowColor = "rgba(87,215,255,.45)"; ctx.shadowBlur = 24 * this.dpr; ctx.strokeStyle = "rgba(87,215,255,.5)"; ctx.lineWidth = 1.5 * this.dpr; ctx.stroke(); ctx.shadowBlur = 0; }
    else { ctx.strokeStyle = "rgba(87,215,255,.45)"; ctx.lineWidth = 1 * this.dpr; ctx.stroke(); }
    // graticule (front hemisphere only)
    ctx.lineWidth = 0.6 * this.dpr; ctx.strokeStyle = "rgba(27,95,120,.9)";
    for (let lat = -80; lat <= 80; lat += t.grid) this.polyline(Array.from({ length: 73 }, (_, i) => [lat, -180 + i * 5]), R, cx, cy);
    for (let lon = -180; lon < 180; lon += t.grid) this.polyline(Array.from({ length: 37 }, (_, i) => [-90 + i * 5, lon]), R, cx, cy);
    // equator + orbit rings (identity motif)
    ctx.strokeStyle = "rgba(255,180,84,.45)"; ctx.lineWidth = 1 * this.dpr;
    this.polyline(Array.from({ length: 73 }, (_, i) => [0, -180 + i * 5]), R, cx, cy);
    ctx.strokeStyle = "rgba(255,180,84,.25)"; ctx.beginPath(); ctx.ellipse(cx, cy, R * 1.12, R * 0.32, -0.35, 0, 7); ctx.stroke();
    // markers
    this.hits = [];
    const maxCount = Math.max(1, ...this.countries.map((c) => c.count || 0));
    for (const c of this.countries) {
      const p = this.project(c.lat, c.lon, R, cx, cy);
      if (!p.front) continue;
      const n = c.count || 0, sel = c.iso === this.selected;
      const r = (n ? 3 + 6 * Math.sqrt(n / maxCount) : 1.4) * this.dpr;
      const a = 0.35 + 0.65 * p.depth;
      ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, 7);
      ctx.fillStyle = sel ? `rgba(255,180,84,${a})` : n ? `rgba(87,215,255,${a})` : `rgba(125,147,164,${a * 0.6})`;
      ctx.fill();
      if (sel) { ctx.strokeStyle = "rgba(255,180,84,.9)"; ctx.lineWidth = 1.5 * this.dpr; ctx.beginPath(); ctx.arc(p.x, p.y, r + 6 * this.dpr, 0, 7); ctx.stroke(); }
      if (n) this.hits.push({ x: p.x, y: p.y, r: Math.max(r + 8 * this.dpr, 14 * this.dpr), iso: c.iso });
    }
    // breaking pulses
    if (t.pulses) {
      const phase = (ts % 1800) / 1800;
      for (const e of this.events) {
        if (!e.breaking) continue;
        const p = this.project(e.lat, e.lon, R, cx, cy); if (!p.front) continue;
        ctx.beginPath(); ctx.arc(p.x, p.y, (4 + 18 * phase) * this.dpr, 0, 7);
        ctx.strokeStyle = `rgba(255,92,92,${(1 - phase) * 0.8})`; ctx.lineWidth = 1.2 * this.dpr; ctx.stroke();
      }
    }
    // labels for selected + top 3
    ctx.font = `${11 * this.dpr}px ui-monospace, Menlo, monospace`; ctx.fillStyle = "rgba(207,227,238,.9)";
    const top = this.countries.filter((c) => c.count).sort((a, b) => b.count - a.count).slice(0, 4);
    for (const c of top) { const p = this.project(c.lat, c.lon, R, cx, cy); if (p.front) ctx.fillText(`${c.name} · ${c.count}`, p.x + 10 * this.dpr, p.y - 6 * this.dpr); }
  }

  polyline(points, R, cx, cy) {
    const ctx = this.ctx; let pen = false; ctx.beginPath();
    for (const [lat, lon] of points) {
      const p = this.project(lat, lon, R, cx, cy);
      if (!p.front) { pen = false; continue; }
      if (pen) ctx.lineTo(p.x, p.y); else ctx.moveTo(p.x, p.y);
      pen = true;
    }
    ctx.stroke();
  }

  // -- interaction (drag rotates, click selects) --------------------------------------------------
  _bind() {
    const pos = (e) => { const r = this.canvas.getBoundingClientRect(); const s = e.touches ? e.touches[0] : e; return { x: (s.clientX - r.left) * this.dpr, y: (s.clientY - r.top) * this.dpr }; };
    const down = (e) => { this.drag = { ...pos(e), rot: this.rot, tilt: this.tilt, moved: false }; };
    const move = (e) => { if (!this.drag) return; const p = pos(e); const dx = p.x - this.drag.x, dy = p.y - this.drag.y; if (Math.abs(dx) + Math.abs(dy) > 4) this.drag.moved = true; this.rot = this.drag.rot + dx / (this.canvas.width * 0.3); this.tilt = Math.max(-1.2, Math.min(1.2, this.drag.tilt + dy / (this.canvas.height * 0.3))); this.target = null; };
    const up = (e) => {
      if (!this.drag) return;
      if (!this.drag.moved) { const p = e.changedTouches ? { x: (e.changedTouches[0].clientX - this.canvas.getBoundingClientRect().left) * this.dpr, y: (e.changedTouches[0].clientY - this.canvas.getBoundingClientRect().top) * this.dpr } : pos(e); this.pick(p); }
      this.drag = null;
    };
    this.canvas.addEventListener("mousedown", down); window.addEventListener("mousemove", move); window.addEventListener("mouseup", up);
    this.canvas.addEventListener("touchstart", down, { passive: true }); this.canvas.addEventListener("touchmove", move, { passive: true }); this.canvas.addEventListener("touchend", up);
  }
  pick(p) {
    let best = null, bd = Infinity;
    for (const h of this.hits || []) { const d = Math.hypot(h.x - p.x, h.y - p.y); if (d < h.r && d < bd) { bd = d; best = h; } }
    if (best) { this.selected = best.iso; this.onSelect(best.iso); }
  }
}
