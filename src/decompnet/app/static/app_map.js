/* ------------------------------------------------------------------ map */
let colors = null, geo = null, animating = false;

function cssColor(name) {
  const probe = document.createElement("i");
  probe.style.color = `var(${name})`;
  document.body.append(probe);
  const rgb = getComputedStyle(probe).color.match(/\d+(\.\d+)?/g).slice(0, 3).map(Number);
  probe.remove();
  return rgb;
}
function readColors() {
  colors = { open: cssColor("--open"), tried: cssColor("--tried"), work: cssColor("--work"), match: cssColor("--match"), hi: cssColor("--match-hi"), ink: cssColor("--ink") };
}
const mix = (a, b, t) => a.map((v, i) => Math.round(v + (b[i] - v) * t));
const rgb = (c, a = 1) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;

function layoutMap() {
  const wrap = $("mapWrap"), canvas = $("map");
  if (!wrap.clientWidth) return;  // not on screen: laid out when the overview shows
  const w = Math.max(200, wrap.clientWidth - 32), n = Math.max(1, S.map.length);
  let size = Math.floor(Math.sqrt((w * 300) / n));
  size = Math.max(5, Math.min(26, size));
  const gap = size >= 12 ? 3 : 2;
  const cols = Math.max(1, Math.floor((w + gap) / (size + gap)));
  const rows = Math.ceil(n / cols);
  const h = rows * (size + gap) - gap;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
  canvas.style.height = h + "px";
  geo = { size, gap, cols, w, h, dpr };
  mapChanged();
  applyClaims();
}

function applyClaims(draw = true) {
  const working = new Map(S.claims.map((c) => [c.name, c.agent]));
  let changed = false;
  for (const f of S.map) {
    const status = ["matched", "asm", "library", "reconstructed", "unmatchable"].includes(f.status) ? f.status : working.has(f.name) ? "working" : "open";
    if (status !== f.status) { f.status = status; changed = true; }
    f.agent = working.get(f.name) || null;
  }
  if (changed) mapChanged();
  if (draw) drawMap();
}

/* New attempts, as the live feed brings them: each function's tries and best score, without a refetch. */
function applyAttempts(attempts) {
  for (const at of attempts) {
    const i = S.byName && S.byName.get(at.function);
    if (i == null) continue;
    const f = S.map[i];
    f.attempts = (f.attempts || 0) + 1;
    if (at.score != null && (f.best == null || at.score < f.best)) { f.best = at.score; mapChanged(); }
  }
}

function cellColor(f) {
  if (f.status === "matched") return [colors.match, 1];
  if (f.status === "reconstructed") return [colors.work, 0.7];
  if (f.status === "asm" || f.status === "unmatchable") return [colors.tried, 0.35];
  if (f.status === "library") return [colors.faint || colors.open, 0.3];
  if (f.best != null) return [mix(colors.tried, colors.hi, Math.pow(pctOf(f.best, f.insns) || 0, 3)), 1];
  return [colors.open, 1];
}

function drawBase() {
  const { size, gap, cols, dpr } = geo;
  base = base || document.createElement("canvas");
  base.width = $("map").width; base.height = $("map").height;
  const ctx = base.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const r = size >= 10 ? 2.5 : size >= 7 ? 1.5 : 1;
  moving = [];
  S.map.forEach((f, i) => {
    if (f.status === "working") { moving.push(i); return; }  // drawn every frame, pulsing
    const x = (i % cols) * (size + gap), y = Math.floor(i / cols) * (size + gap);
    const [c, a] = cellColor(f);
    ctx.fillStyle = rgb(c, a);
    ctx.beginPath(); ctx.roundRect(x, y, size, size, r); ctx.fill();
  });
  baseDirty = false;
}

function drawMap() {
  if (!geo || S.tab !== "overview") return;
  if (!colors) readColors();
  if (baseDirty || !base || base.width !== $("map").width || base.height !== $("map").height) drawBase();
  const ctx = $("map").getContext("2d");
  const { size, gap, cols, dpr } = geo;
  const now = performance.now();
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, $("map").width, $("map").height);
  ctx.drawImage(base, 0, 0);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const r = size >= 10 ? 2.5 : size >= 7 ? 1.5 : 1;
  const at = (i) => [(i % cols) * (size + gap), Math.floor(i / cols) * (size + gap)];
  let live = moving.length > 0;
  for (const i of moving) {
    const [x, y] = at(i);
    ctx.fillStyle = rgb(colors.work, 0.55 + 0.45 * Math.sin(now / 260 + i));
    ctx.beginPath(); ctx.roundRect(x, y, size, size, r); ctx.fill();
  }
  for (const [name, fl] of S.flash) {
    const i = S.byName && S.byName.get(name), t = (now - fl) / 1600;
    if (i == null || t >= 1) { S.flash.delete(name); continue; }
    live = true;
    const [x, y] = at(i), g = size * (0.6 + 1.8 * t);
    ctx.strokeStyle = rgb(colors.match, 1 - t);
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.roundRect(x - g / 2, y - g / 2, size + g, size + g, r + 2); ctx.stroke();
  }
  const sel = S.fn && S.byName ? S.byName.get(S.fn) : null;
  if (sel != null) { const [x, y] = at(sel); ctx.strokeStyle = rgb(colors.ink, 0.9); ctx.lineWidth = 1.5; ctx.strokeRect(x - 1.5, y - 1.5, size + 3, size + 3); }
  if (live && !animating) {  // about 30 frames a second is plenty for a pulse
    animating = true;
    setTimeout(() => requestAnimationFrame(() => { animating = false; drawMap(); }), 33);
  }
}

function mapIndexAt(ev) {
  if (!geo) return -1;
  const rect = $("map").getBoundingClientRect();
  const x = ev.clientX - rect.left, y = ev.clientY - rect.top;
  const col = Math.floor(x / (geo.size + geo.gap)), row = Math.floor(y / (geo.size + geo.gap));
  if (col < 0 || col >= geo.cols || x % (geo.size + geo.gap) > geo.size || y % (geo.size + geo.gap) > geo.size) return -1;
  const i = row * geo.cols + col;
  return i < S.map.length ? i : -1;
}
$("map").addEventListener("mousemove", (ev) => {
  const i = mapIndexAt(ev), tip = $("tip");
  if (i < 0) { tip.hidden = true; return; }
  const f = S.map[i];
  const analysis = S.ov && S.ov.kind === "analysis";
  const p = pctOf(f.best, f.insns);
  const status = f.status === "matched" ? `${analysis ? "analyzed" : "matched"}${f.matched_by ? " by " + esc(f.matched_by) : ""}` : f.status === "working" ? `${esc(f.agent)} is on it` : f.best != null ? `best ${pctText(p)}` : "not started";
  const where = S.ov && S.ov.kind === "fork" ? esc(f.unit || "") : analysis && !f.vram ? "task" : hex8(f.vram);
  const measure = analysis ? (f.size ? bytes(f.size) : "task") : `${f.insns} instructions`;
  tip.innerHTML = `<b>${esc(f.name)}</b><br>${where} · ${measure}<br>${status}`;
  tip.hidden = false;
  const wrap = $("mapWrap").getBoundingClientRect();
  let left = ev.clientX - wrap.left + 14, top = ev.clientY - wrap.top + 14;
  if (left + 240 > wrap.width) left -= 260;
  tip.style.left = left + "px"; tip.style.top = top + "px";
});
$("map").addEventListener("mouseleave", () => { $("tip").hidden = true; });
$("map").addEventListener("click", (ev) => { const i = mapIndexAt(ev); if (i >= 0) openFn(S.map[i].name); });
let resizeT = null;
window.addEventListener("resize", () => { clearTimeout(resizeT); resizeT = setTimeout(() => { if (S.map.length && S.tab === "overview") layoutMap(); }, 120); });
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { colors = null; baseDirty = true; drawMap(); });

