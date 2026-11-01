"use strict";
/* decomp.anything GUI. Plain JS, no build step. Everything shown from agents or ROMs is untrusted text:
   it only ever reaches the page through textContent or esc(). */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const hex8 = (n) => "0x" + (n >>> 0).toString(16).toUpperCase().padStart(8, "0");
const clock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const pctOf = (best, insns) => (best == null ? null : Math.max(0, 1 - best / Math.max(1, insns * 100)));
/* A match percentage that never reads 100% before the function really matches (99.6% stays 99.6%). */
const pctText = (p) => (p >= 0.99 ? `${(Math.floor(p * 1000) / 10).toFixed(1)}%` : `${Math.floor(p * 100)}%`);
const pctNum = (p) => (p < 10 ? p.toFixed(2) : p.toFixed(1));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const plural = (n, word) => `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
function since(t) {
  if (!t) return "";
  const s = Date.now() / 1000 - t;
  return s < 60 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${Math.round(s / 3600)} h ago`
    : s < 172800 ? "yesterday" : `${Math.round(s / 86400)} days ago`;
}
function duration(h) { return h < 1 ? `${Math.max(1, Math.round(h * 60))} min` : h < 48 ? `${h.toFixed(h < 10 ? 1 : 0)} h` : `${Math.round(h / 24)} days`; }
function bytes(n) { return n >= 1 << 20 ? `${(n / 1048576).toFixed(1)} MB` : n >= 1024 ? `${(n / 1024).toFixed(1)} KB` : `${n} bytes`; }
function money(usd) { return usd >= 10 ? `$${usd.toFixed(0)}` : `$${usd.toFixed(2)}`; }

async function api(path, { method = "GET", body, raw } = {}) {
  const opts = { method, headers: {} };
  if (method !== "GET") opts.headers["X-Decomp"] = "1";
  if (raw !== undefined) { opts.body = raw; opts.headers["Content-Type"] = "application/octet-stream"; }
  else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
  const res = await fetch(path, opts);
  let data = null;
  try { data = await res.json(); } catch { /* not json */ }
  if (!res.ok) throw new Error((data && data.error) || `request failed (${res.status})`);
  return data;
}

/* Toasts: at most three, each dismissable, and a run of matches collapses into the newest one ("+4 more matched")
   instead of stacking a wall of cards over the corner. Hovering holds a toast long enough to read. */
const TOAST_MAX = 3;
let lastMatchToast = null;
function armToast(t, ms) {
  clearTimeout(t._fade); clearTimeout(t._gone);
  t._fade = setTimeout(() => { t.style.opacity = "0"; t.style.transition = "opacity .4s"; }, ms);
  t._gone = setTimeout(() => t.remove(), ms + 500);
}
function dismissToast(t) {
  clearTimeout(t._fade); clearTimeout(t._gone);
  t.style.opacity = "0"; t.style.transition = "opacity .25s";
  setTimeout(() => t.remove(), 260);
}
function toast(html, kind = "", count = false) {
  const box = $("toasts");
  if (count && lastMatchToast?.isConnected && lastMatchToast.dataset.count === "1"
      && lastMatchToast.dataset.slug === (S.slug || "")) {
    const n = Number(lastMatchToast.dataset.more || 0) + 1;
    lastMatchToast.dataset.more = String(n);
    let more = lastMatchToast.querySelector(".toast-more");
    if (!more) { more = document.createElement("span"); more.className = "toast-more"; lastMatchToast.append(more); }
    more.textContent = `+${n} more matched`;
    armToast(lastMatchToast, 4200);
    return lastMatchToast;
  }
  const t = document.createElement("div");
  t.className = "toast " + kind;
  t.innerHTML = html;
  t.dataset.slug = S.slug || "";
  t.dataset.count = count ? "1" : "0";
  t.title = "Click to dismiss";
  t.onclick = () => dismissToast(t);
  t.onmouseenter = () => { clearTimeout(t._fade); clearTimeout(t._gone); };
  t.onmouseleave = () => armToast(t, 2500);
  armToast(t, kind === "err" ? 7000 : 4200);
  box.append(t);
  if (count) lastMatchToast = t;
  while (box.children.length > TOAST_MAX) box.firstElementChild.remove();
  return t;
}

async function followJob(id, logEl, onUpdate) {
  for (;;) {
    const job = await api(`/api/job?id=${id}`);
    if (logEl) { logEl.textContent = job.log.join("\n"); logEl.scrollTop = logEl.scrollHeight; }
    if (onUpdate) onUpdate(job.log);
    if (job.state === "done") return job.result;
    if (job.state === "error") { const err = new Error(job.error); err.job = job; throw err; }
    await sleep(500);
  }
}

/* The map's still cells are drawn once into `base` (again only when one of them changes); a frame copies it and
   draws just what moves on top: the functions being worked on, fresh matches, the selection. A big game is tens of
   thousands of cells, and redrawing them all 60 times a second while agents or autopilot work made the app lag. */
let base = null, baseDirty = true, moving = [];
function mapChanged() { baseDirty = true; }

/* ------------------------------------------------------------------ theme */
const THEME_KEY = "decomp-wtf-theme";
const THEMES = ["", "light", "dark"];  // "" follows this computer (the stylesheet's prefers-color-scheme)
const THEME_NAME = { "": "system", light: "light", dark: "dark" };
const themeName = (t) => THEME_NAME[t] || "system";
function applyTheme(t) {
  if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
  colors = null; baseDirty = true;
  const i = Math.max(0, THEMES.indexOf(t)), next = THEMES[(i + 1) % THEMES.length];
  const button = $("themeBtn");
  button.title = `Theme: ${themeName(t)} (click for ${themeName(next)})`;
  button.setAttribute("aria-label", button.title);
  const seg = $("themeSeg");  // the Settings page shows the same three states
  if (seg) seg.querySelectorAll("[data-theme]").forEach((b) => b.classList.toggle("on", (b.dataset.theme || "") === (t || "")));
  if (S.map.length) drawMap();
}
function setTheme(t) {
  try { if (t) localStorage.setItem(THEME_KEY, t); else localStorage.removeItem(THEME_KEY); } catch { /* storage off */ }
  applyTheme(t);
}
function savedTheme() { try { const t = localStorage.getItem(THEME_KEY) || ""; return THEMES.includes(t) ? t : ""; } catch { return ""; } }
/* One button, three states: light -> dark -> follow the computer. The old one could never get back to "system". */
$("themeBtn").onclick = () => setTheme(THEMES[(THEMES.indexOf(savedTheme()) + 1) % THEMES.length]);

/* ------------------------------------------------------------------ state + routing */
const S = {
  st: null, slug: null, ov: null, map: [], byName: new Map(), e: 0, a: 0, seq: {}, sessions: [], tab: "overview",
  scores: new Map(), claims: [], flash: new Map(), timer: null, statsTimer: null, lastMapFetch: 0, fn: null, stats: null,
  projects: [],
};
const VIEWS = ["home", "catalog", "project", "agentsPage", "toolsPage", "settingsPage", "consolePage"];
const CT = { slug: null, data: null, collapsed: new Set(), selected: null, pendingSelect: null, preview: null, busy: false };
const TABS = ["overview", "functions", "summary", "data", "agents", "activity", "team", "source", "settings"];

/* The project folder in the computer's file manager (decomp.anything in Docker says where it is instead). */
function openFolder() {
  return api("/api/open", { method: "POST", body: { slug: S.slug } })
    .then((r) => { if (r && r.message) toast(esc(r.message)); })
    .catch((e) => toast(esc(e.message), "err"));
}

async function loadState() { S.st = await api("/api/state"); $("navVersion").textContent = `v${S.st.version}`; markPlatforms(); return S.st; }

function route() {
  const h = location.hash;
  const m = h.match(/^#\/p\/([\w-]+)(?:\/(\w+))?(?:\/([^/?#]+))?/);
  closeAll(false);  // navigating: the new view decides what has focus, and the URL is already the target
  closeNav();
  hideLaunch();
  if (m) {
    const fn = m[3] ? decodeURIComponent(m[3]) : "";
    if (S.slug === m[1] && !$("project").hidden) {
      setTab(m[2] || "overview");
      if (fn) openFn(fn);
      markNav();
      return;
    }
    leaveProject();
    openProject(m[1], m[2] || "overview", fn);
  } else {
    leaveProject();
    if (h === "#/catalog") showCatalog();
    else if (h === "#/agents") showAgentsPage();
    else if (h === "#/tools") showToolsPage();
    else if (h === "#/settings") showSettingsPage();
    else if (h === "#/console" || h.startsWith("#/console?")) showConsole();
    else showHome();
  }
  markNav();
}
window.addEventListener("hashchange", route);

function show(view) {
  for (const v of VIEWS) $(v).hidden = view !== v;
  $("main").scrollTo(0, 0);
  window.scrollTo(0, 0);
}
function leaveProject() {
  stopPolling();
  S.slug = null;
  clearInterval(teamTimer);
}

/* ------------------------------------------------------------------ sidebar */
/* The page's CSP allows no inline style attributes: widths are set through the DOM instead. */
function setWidths(root) { root.querySelectorAll("[data-w]").forEach((el) => { el.style.width = el.dataset.w + "%"; }); }

function markNav() {
  const h = location.hash || "#/";
  const route = h.startsWith("#/p/") ? "project" : (h.replace("#/", "").split("?")[0] || "home");
  document.querySelectorAll(".nav [data-route]").forEach((a) => a.classList.toggle("on", a.dataset.route === route));
  document.querySelectorAll(".nav-proj").forEach((a) => a.classList.toggle("on", a.dataset.slug === S.slug && route === "project"));
}
async function loadNav() {
  try { S.projects = await api("/api/projects"); connOk(); } catch (e) {
    if (String(e.message).includes("Failed to fetch")) connLost();  // the app went away, wherever you are
    if (!S.projects?.length) $("navProjects").innerHTML = `<p class="nav-empty">Couldn't load your projects: ${esc(e.message)}</p>`;
    return;
  }
  renderNav();
}
function navProject(p, child = false, level = 0) {
  const pct = p.kind === "analysis" ? (p.functions ? (100 * p.matched) / p.functions : 0) : p.bytes ? (100 * p.matched_bytes) / p.bytes : 0;
  const short = child ? `↳ ${p.entry || ""}` : PLAT_SHORT[p.platform] || (p.platform_name || "").replace("Nintendo ", "") || "?";
  const cls = child ? ` child${level > 1 ? ` d${Math.min(level, 3)}` : ""}` : "";
  const state = p.working ? `<span class="dot on" title="${p.working} agent${p.working === 1 ? "" : "s"} working"></span>`
    : p.autopilot === "running" ? `<span class="dot work" title="autopilot running"></span>` : "";
  if (p.broken) return `<a class="nav-proj broken${cls}" href="#/p/${esc(p.slug)}" data-slug="${esc(p.slug)}" title="${esc(p.broken)}">
    <span class="np-top"><span class="np-name">${esc(p.title)}</span></span>
    <span class="np-meta"><span>can't be opened: hover for why</span></span></a>`;
  return `<a class="nav-proj${cls}" href="#/p/${esc(p.slug)}" data-slug="${esc(p.slug)}" title="${esc(p.title)}">
    <span class="np-top"><span class="np-name">${esc(p.title)}</span>${state}${p.room ? `<span class="np-room" title="${p.room === "host" ? "you share this project" : "in a team room"}">●●</span>` : ""}</span>
    <span class="np-meta"><span>${esc(short)}</span><span class="grow"></span><span>${p.kind === "fork" && !p.ready ? "set up" : pctNum(pct) + "%"}</span></span>
    <span class="np-bar"><i data-w="${pct.toFixed(2)}"></i></span></a>`;
}

function renderNav() {
  const box = $("navProjects");
  if (!S.projects.length) { box.innerHTML = `<p class="nav-empty">None yet: drop in a binary.</p>`; return; }
  const parents = S.projects.filter((p) => !p.parent);
  const known = new Set(S.projects.map((p) => p.slug));
  const kids = new Map();
  for (const p of S.projects) {
    if (!p.parent) continue;
    if (!kids.has(p.parent)) kids.set(p.parent, []);
    kids.get(p.parent).push(p);
  }
  const orphans = S.projects.filter((p) => p.parent && !known.has(p.parent));
  const tree = (p, child = false, level = 0) => navProject(p, child, level)
    + (kids.get(p.slug) || []).map((k) => tree(k, true, level + 1)).join("");
  box.innerHTML = [...parents, ...orphans].map((p) => tree(p)).join("");
  setWidths(box);
  markNav();
}
setInterval(loadNav, 5000);
function openNav() { document.body.classList.add("nav-open"); $("navShade").hidden = false; }
function closeNav() { document.body.classList.remove("nav-open"); $("navShade").hidden = true; }
$("navOpen").onclick = openNav;
$("navClose").onclick = closeNav;
$("navShade").onclick = closeNav;
$("navJoin").onclick = () => { closeNav(); openJoin(); };

