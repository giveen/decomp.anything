/* ------------------------------------------------------------------ function list */
const L = { q: "", filter: "all", sort: "i", dir: 1, limit: 200, rows: [] };
const FILTERS = [["all", "All"], ["open", "To do"], ["working", "Working"], ["recon", "Reconstructed"], ["tried", "Tried"], ["matched", "Matched"], ["hot", "Coverage"], ["library", "Library"], ["unmatchable", "Retired"]];
const fnState = (f) => (f.status === "matched" ? "matched" : f.status === "reconstructed" ? "recon" : f.status === "library" ? "library" : f.status === "asm" ? "asm" : f.status === "unmatchable" ? "unmatchable" : f.status === "working" ? "working" : f.best != null ? "tried" : "open");
const STATE_RANK = { working: 0, recon: 1, tried: 2, open: 3, matched: 4, asm: 5, unmatchable: 6, library: 7 };

function renderList() {
  if (!S.map.length || S.tab !== "functions") return;
  const analysis = S.ov && S.ov.kind === "analysis";
  const counts = { all: S.map.length, open: 0, working: 0, recon: 0, tried: 0, matched: 0, asm: 0, library: 0, unmatchable: 0, hot: 0 };
  for (const f of S.map) { counts[fnState(f)]++; if ((f.hits || 0) > 0) counts.hot++; }
  $("fnFilter").innerHTML = FILTERS.map(([k, label]) => `<button type="button" role="radio" aria-checked="${L.filter === k}" class="${L.filter === k ? "on" : ""}" data-filter="${k}">${analysis && k === "matched" ? "Analyzed" : label}<span>${counts[k].toLocaleString()}</span></button>`).join("");
  const q = L.q.trim().toLowerCase();
  let rows = S.map.filter((f) => (L.filter === "all" || (L.filter === "hot" ? (f.hits || 0) > 0 : fnState(f) === L.filter))
    && (!q || f.name.toLowerCase().includes(q) || hex8(f.vram).toLowerCase().includes(q) || (f.unit || "").toLowerCase().includes(q)));
  const key = {
    i: (f) => f.i, name: (f) => f.name.toLowerCase(), insns: (f) => f.insns, attempts: (f) => f.attempts || 0,
    best: (f) => (f.status === "matched" ? 2 : f.best == null ? -1 : pctOf(f.best, f.insns)), status: (f) => STATE_RANK[fnState(f)],
    hits: (f) => f.hits || 0,
  }[L.sort === "vram" ? "i" : L.sort];
  rows = rows.slice().sort((a, b) => { const x = key(a), y = key(b); return (x < y ? -1 : x > y ? 1 : a.i - b.i) * L.dir; });
  L.rows = rows;
  const fork = S.ov && S.ov.kind === "fork";
  const shown = rows.slice(0, L.limit);
  let focusAt = shown.findIndex((f) => f.name === S.fn);
  if (focusAt < 0) focusAt = 0;  // one row is a tab stop; the arrows (and j/k) walk the rest
  $("fnRows").innerHTML = shown.map((f, i) => {
    const st = fnState(f), p = pctOf(f.best, f.insns);
    const best = st === "matched" ? `<span class="ok">100%</span>` : p == null ? `<span class="dim">–</span>` : pctText(p);
    const status = st === "matched" ? `<span class="st matched" title="${esc(f.matched_by ? "by " + f.matched_by : "")}">${analysis ? "analyzed" : "matched"}</span>` : st === "recon" ? `<span class="st recon" title="reconstructed by an agent: unverified, not a match">reconstructed</span>` : st === "library" ? `<span class="st" title="library/CRT code: not the program's own">${esc(f.library || "library")}</span>` : st === "asm" ? `<span class="st" title="hand-written assembly: no C compiles to it">assembly</span>` : st === "unmatchable" ? `<span class="st" title="retired: no C can reproduce it (see its notes)">retired</span>` : st === "working" ? `<span class="st working">${esc(f.agent || "working")}</span>`
      : st === "tried" ? `<span class="st tried">tried</span>` : `<span class="st">${analysis ? "to do" : "to do"}</span>`;
    const where = analysis ? (f.vram ? hex8(f.vram) : ["artifact-entry", "recovered-source", "reconstruction"].includes(f.kind) ? "file" : "task")
      : fork ? esc(f.unit || "") : hex8(f.vram);
    const measure = analysis ? (f.size ? bytes(f.size) : "—") : f.insns.toLocaleString();
    const clickTitle = f.child ? "Open child project" : analysis && f.kind === "artifact-entry" ? "Show this file in Contents" : analysis && (f.kind === "recovered-source" || f.kind === "reconstruction") ? "Open the recovered source" : analysis ? "Open in the RE console" : "Open function";
    return `<tr data-fn="${esc(f.name)}" class="${S.fn === f.name ? "sel" : ""}" tabindex="${i === focusAt ? 0 : -1}" title="${esc(clickTitle)}"><td class="mono dim">${where}</td>
      <td class="mono name">${(S.ov && (S.ov.priority || []).includes(f.name)) ? `<span class="star" title="agents take it next">★</span>` : ""}${esc(f.name)}${f.child ? ` <span class="dim" title="child project">↗</span>` : ""}</td><td class="r mono">${measure}</td>
      ${analysis ? "" : `<td class="r mono">${best}</td><td class="r mono">${f.attempts || `<span class="dim">0</span>`}</td><td class="r mono">${f.hits || `<span class="dim">–</span>`}</td>`}<td>${status}</td></tr>`;
  }).join("") || `<tr class="none"><td colspan="${analysis ? 4 : 6}">No work item matches${q ? ` “${esc(L.q)}”` : ""}.</td></tr>`;
  $("fnShown").textContent = rows.length > L.limit ? `${L.limit.toLocaleString()} of ${rows.length.toLocaleString()} shown` : plural(rows.length, analysis ? "work item" : "function");
  $("fnMore").hidden = rows.length <= L.limit;
  document.querySelectorAll(".fn-table th").forEach((th) => {
    th.classList.toggle("sorted", th.dataset.sort === L.sort || (th.dataset.sort === "vram" && L.sort === "i"));
    th.classList.toggle("desc", (th.dataset.sort === L.sort || (th.dataset.sort === "vram" && L.sort === "i")) && L.dir < 0);
  });
}
$("fnFilter").addEventListener("click", (e) => { const b = e.target.closest("[data-filter]"); if (b) { L.filter = b.dataset.filter; L.limit = 200; renderList(); } });
$("fnSearch").addEventListener("input", () => { L.q = $("fnSearch").value; L.limit = 200; renderList(); });
$("fnMore").onclick = () => { L.limit += 500; renderList(); };
$("fnRows").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-fn]"); if (tr) openFn(tr.dataset.fn); });
$("fnRows").addEventListener("keydown", (e) => {
  const tr = e.target.closest("tr[data-fn]");
  if (!tr) return;
  if (e.key === "Enter") { openFn(tr.dataset.fn); return; }
  const rows = [...$("fnRows").querySelectorAll("tr[data-fn]")];
  let k = rows.indexOf(tr);
  if (e.key === "ArrowDown" || e.key === "j") k++;
  else if (e.key === "ArrowUp" || e.key === "k") k--;
  else if (e.key === "Home") k = 0;
  else if (e.key === "End") k = rows.length - 1;
  else return;
  e.preventDefault();
  const next = rows[Math.max(0, Math.min(rows.length - 1, k))];
  if (!next) return;
  rows.forEach((r) => { r.tabIndex = r === next ? 0 : -1; });
  next.focus();
  next.scrollIntoView({ block: "nearest" });
});
document.querySelectorAll(".fn-table th[data-sort]").forEach((th) => {
  th.onclick = () => {
    const k = th.dataset.sort === "vram" ? "i" : th.dataset.sort;
    if (L.sort === k) L.dir = -L.dir;
    else { L.sort = k; L.dir = ["best", "attempts", "insns"].includes(k) ? -1 : 1; }
    renderList();
  };
});

/* Matched code over time, from when each function was matched. */
function renderProgress() {
  const svg = $("progress"), total = S.map.reduce((n, f) => n + (f.size || 0), 0);
  const done = S.map.filter((f) => f.status === "matched");
  S.today = done.filter((f) => f.matched_at > Date.now() / 1000 - 86400).length;
  const dated = done.filter((f) => f.matched_at).sort((a, b) => a.matched_at - b.matched_at);
  if (!total || dated.length < 2) { svg.innerHTML = ""; return; }
  let cum = done.filter((f) => !f.matched_at).reduce((n, f) => n + (f.size || 0), 0);
  const t0 = dated[0].matched_at, t1 = Math.max(Date.now() / 1000, dated[dated.length - 1].matched_at + 1);
  const top = cum + dated.reduce((n, f) => n + (f.size || 0), 0);  // scaled to what's done, so early progress shows
  const X = (t) => (200 * (t - t0)) / (t1 - t0), Y = (v) => 42 - (40 * v) / Math.max(top, 1);
  let line = `M0,${Y(cum).toFixed(1)}`;
  for (const f of dated) { cum += f.size || 0; line += `H${X(f.matched_at).toFixed(1)}V${Y(cum).toFixed(1)}`; }
  line += "H200";
  svg.innerHTML = `<path class="area" d="${line}V44H0Z"/><path class="line" d="${line}"/>`;
}

/* ------------------------------------------------------------------ drawers, keys */
/* Drawers are this app's modals. While one is open the page behind is inert (no clicks, no tab stops, nothing read
   out), the card takes focus so the dialog is announced, and closing gives focus back to what opened it. */
let modalReturn = null;
const modalOpen = () => !$("drawer").hidden || !$("dialog").hidden;
function lockBehind(on) {
  $("app").inert = on;
  document.body.classList.toggle("locked", on);
}
function focusInto(card, firstControl) {
  if (!firstControl) { card.focus({ preventScroll: true }); return; }  // the dialog itself: its name is read out
  const shown = (el) => !el.hidden && !el.closest("[hidden]") && getComputedStyle(el).display !== "none";
  const fields = [...card.querySelectorAll("input:not([type=hidden]):not([disabled]), select:not([disabled]), textarea:not([disabled])")].filter(shown);
  const buttons = [...card.querySelectorAll("button:not([disabled])")].filter(shown);
  (fields[0] || buttons[0] || card).focus({ preventScroll: true });
}
function openModal(id, focus = true) {
  if (!modalOpen()) modalReturn = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  $(id).hidden = false;
  lockBehind(true);
  if (focus) focusInto($(id).querySelector(".drawer-card"), id === "dialog");
}
function closeModal(id, restore = true) {
  $(id).hidden = true;
  if (modalOpen()) return;
  lockBehind(false);
  const back = modalReturn;
  modalReturn = null;
  if (restore && back && back.isConnected) back.focus({ preventScroll: true });
}
function closeAll(restore = true) {
  const wasOpen = modalOpen();
  if (!$("drawer").hidden) giveBack(null);
  for (const id of ["drawer", "dialog"]) $(id).hidden = true;
  lockBehind(false);
  S.fn = null;
  if (S.map.length) drawMap();
  const back = modalReturn;
  modalReturn = null;
  if (wasOpen && restore) {
    resetFnUrl();
    if (back && back.isConnected) back.focus({ preventScroll: true });
  }
}
document.querySelectorAll(".drawer").forEach((d) => {
  d.addEventListener("click", (e) => {
    if (e.target !== d && !e.target.closest("[data-close]")) return;
    const wasFn = d.id === "drawer";
    if (wasFn) { giveBack(null); S.fn = null; drawMap(); }
    closeModal(d.id);
    if (wasFn) resetFnUrl();
  });
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { if (!$("launchPop").hidden) { hideLaunch(); return; } closeAll(); return; }
  if (e.target.closest("input, textarea, select") || e.metaKey || e.ctrlKey || e.altKey) return;  // typing
  if (!$("drawer").hidden && (e.key === "j" || e.key === "k")) { e.preventDefault(); stepFn(e.key === "j" ? 1 : -1); return; }
  if (e.key === "/" && S.slug && $("drawer").hidden) {
    e.preventDefault();
    if (S.tab !== "functions") location.hash = `#/p/${S.slug}/functions`;
    setTimeout(() => $("fnSearch").focus(), 50);
  }
});

function cmdBox(cmd) {
  return `<div class="cmd"><code>${esc(cmd)}</code><button type="button" data-copy="${esc(cmd)}">Copy</button></div>`;
}
/* Copying can be refused (no permission, not a secure context): say so instead of a button that silently does nothing. */
async function copyText(text, button) {
  try {
    if (!navigator.clipboard || !navigator.clipboard.writeText) throw new Error("no clipboard");
    await navigator.clipboard.writeText(text);
    if (button) {
      const was = button.textContent;
      button.textContent = "Copied";
      setTimeout(() => { button.textContent = was; }, 1500);
    }
    return true;
  } catch {
    toast("Couldn't copy: select the text and press Ctrl+C.", "err");
    return false;
  }
}
function wireCopies(root) {
  root.querySelectorAll("[data-copy]").forEach((b) => { b.onclick = () => copyText(b.dataset.copy, b); });
}

function dialog(title, html) {
  $("dialogTitle").textContent = title;
  $("dialogBody").innerHTML = html;
  openModal("dialog");
  wireCopies($("dialogBody"));
  return $("dialogBody");
}
function closeDialog() { closeModal("dialog"); }

$("joinBtn").onclick = openJoin;
$("askBtn").onclick = openConnect;
$("talkBtn").onclick = openConnect;
$("connectBtn").onclick = openConnect;

/* Your own agent with decomp.anything's tools: registered once, then you just ask it. */
async function connectRows(box) {
  const render = (rows) => {
    box.innerHTML = rows.map((a) => `<div class="line-item"><div class="txt"><b>${esc(a.name)}</b><span class="mono">${esc(a.command)}</span></div>
      ${!a.installed ? `<span class="muted small">not installed</span>` : a.connected ? `<span class="ok-mark">connected</span>` : `<button class="primary small-btn" data-connect="${esc(a.id)}" type="button">Connect</button>`}</div>`).join("");
    box.querySelectorAll("[data-connect]").forEach((b) => {
      b.onclick = async () => {
        b.disabled = true; b.textContent = "Connecting…";
        try {
          await api("/api/connect", { method: "POST", body: { id: b.dataset.connect } });
          toast(`Connected. Ask it to decompile your game.`, "match");
          render((await api("/api/connect")).agents);
        } catch (e) { toast(esc(e.message), "err"); b.disabled = false; b.textContent = "Connect"; }
      };
    });
  };
  try {
    const r = await api("/api/connect");
    render(r.agents);
    return r;
  } catch (e) { box.innerHTML = `<p class="bad">${esc(e.message)}</p>`; return null; }
}
const HOST_NAMES = { windows: "Windows", macos: "your Mac", linux: "this computer" };
async function openConnect() {
  if (S.st && S.st.runtime && S.st.runtime !== "native") return openHostConnect();
  const body = dialog("Use decomp.anything from your AI agent", `<p>Connect the agent you already use, once. Then ask it in your terminal, for example:</p>
    ${cmdBox("decompile my game at ~/roms/game.z64")}
    <p class="muted small">It confirms the game is your own copy, imports it, opens this live view and works with every tool here: the byte-for-byte judge, the autopilot, helper agents, export. Anything it does shows up here as it happens.</p>
    <div class="setup-group" id="connectRows"><p class="muted small">Looking for your agents…</p></div>
    <div class="setup-group"><h3>Any other MCP client (Cursor, Claude Desktop, Windsurf, your own)</h3><pre class="code" id="bridgeJson"></pre>
      <p class="muted small">It starts decomp.anything when needed; no token, nothing to update when the app restarts.</p></div>`);
  const r = await connectRows(body.querySelector("#connectRows"));
  if (r) body.querySelector("#bridgeJson").textContent = JSON.stringify({ mcpServers: { decomp: { command: r.bridge[0], args: r.bridge.slice(1) } } }, null, 2);
}

/* decomp.anything in WSL (Windows) or in Docker: the agents you talk to run on the computer itself, so they're
   connected from there, by the `decomp` command the installer put on it. */
async function openHostConnect() {
  const host = HOST_NAMES[S.st.host_os] || "your computer";
  const shell = S.st.host_os === "windows" ? "PowerShell" : "a terminal";
  const body = dialog("Use decomp.anything from your AI agent", `<p>Run this once in ${esc(shell)} on ${esc(host)}: it connects Claude Code, Codex, Antigravity CLI and opencode there.</p>
    ${cmdBox("decomp connect")}
    <p>Then ask your agent, for example:</p>
    ${cmdBox(S.st.host_os === "windows" ? "decompile my game at C:\\Games\\game.z64" : "decompile my game at ~/roms/game.z64")}
    <p class="muted small">decomp.anything runs in ${S.st.runtime === "wsl" ? "its own WSL 2 Linux on Windows" : "Docker"}; your agent reaches it through the command below. Anything it does shows up here as it happens.</p>
    <div class="setup-group"><h3>Any other MCP client (Cursor, Claude Desktop, Windsurf, your own)</h3><pre class="code" id="bridgeJson"></pre></div>`);
  try {
    const r = await api("/api/connect");
    body.querySelector("#bridgeJson").textContent = JSON.stringify({ mcpServers: { decomp: { command: r.bridge[0], args: r.bridge.slice(1) } } }, null, 2);
  } catch (e) { body.querySelector("#bridgeJson").textContent = e.message; }
}

/* ------------------------------------------------------------------ steering: instructions, next in line */
function renderSteering(ov) {
  const sp = ov.spend;
  const n = ov.helper_matches || 0;
  const unit = ov.kind === "analysis" ? "finding" : "match";
  $("spendLine").textContent = sp && sp.usd ? `· ${money(sp.usd)} spent${n ? ` · ${money(sp.usd / n)}/${unit}` : ""}`
    : sp && sp.tokens ? `· ${(sp.tokens / 1000).toFixed(0)}k tokens` : "";
  $("instrState").textContent = ov.instructions ? "agents get these" : "none yet";
}
$("instrSave").onclick = async () => {
  try {
    S.ov = await api("/api/instructions", { method: "POST", body: { slug: S.slug, text: $("instrEdit").value } });
    renderSteering(S.ov);
    toast("Saved. Agents get them from their next task on.");
  } catch (e) { toast(esc(e.message), "err"); }
};
async function togglePrio(name, on) {
  try {
    const r = await api("/api/priority", { method: "POST", body: { slug: S.slug, name, on } });
    S.ov.priority = r.priority;
    toast(on ? `Agents take <code>${esc(name)}</code> next.` : "No longer first in line.");
    renderList(); renderPrio();
  } catch (e) { toast(esc(e.message), "err"); }
}
function renderPrio() {
  const list = (S.ov && S.ov.priority) || [];
  const box = $("prioList");
  if (S.ov && S.ov.kind === "fork") { box.innerHTML = `<li class="muted small">Agents pick the easiest open functions first.</li>`; return; }
  if (S.ov && S.ov.kind === "analysis") { box.innerHTML = `<li class="muted small">Nothing pinned: agents take the next open work item. Add tasks in Work items.</li>`; return; }
  box.innerHTML = list.length ? list.map((n) => `<li><span class="star">★</span>${fnLink(n)}<span class="grow"></span><button class="link-btn" data-unpin="${esc(n)}" type="button">remove</button></li>`).join("")
    : `<li class="muted small">Nothing pinned: agents take the easiest open functions first. Open a function and click ★ Next to put it first.</li>`;
  box.querySelectorAll(".fn").forEach((x) => { x.onclick = () => openFn(x.dataset.fn); });
  box.querySelectorAll("[data-unpin]").forEach((b) => { b.onclick = () => togglePrio(b.dataset.unpin, false); });
}

/* Autopilot: free matches before any agent spends a token: identical code (twins) of what's matched, then every
   function's machine draft through the judge; the permuter works on near misses meanwhile. */
function renderAutopilot(ov) {
  const box = $("autopilot"), ap = ov.autopilot, tw = ov.twins, pm = ov.permuter;
  if (ov.kind === "analysis") { box.hidden = true; return; }
  const canRun = (ov.kind !== "fork" || ov.ready) && ov.functions - ov.matched > 0;
  box.hidden = !ap && !canRun && !tw && !pm;
  if (box.hidden) return;
  const running = ap && ap.state === "running";
  const twinsRunning = tw && tw.state === "running";
  const done = ap ? (ap.done ?? ap.tried ?? 0) : 0;
  const failed = (ap && ap.failed) || 0, cached = (ap && ap.cached) || 0;
  const reasons = (ap && ap.reasons) || [];
  box.classList.toggle("on", !!(running || twinsRunning));
  $("apBar").hidden = !running || ap.phase === "twins";
  const perm = pm && (pm.tried || pm.state === "running")
    ? ` The permuter ${pm.state === "running" ? `is rewriting near misses${pm.current ? ` (now ${pm.current})` : ""}` : "rewrote near misses"}: ${plural(pm.tried, "function")}, ${pm.matched} matched.` : "";
  const twinsDone = (ap && ap.twins) || (tw && tw.matched) || 0;
  if (running && ap.phase === "twins") {
    $("apTitle").textContent = "Autopilot: matching code the game has more than once (twins of matched functions)";
    $("apSub").textContent = `Each match's code is tried on its identical copies, names swapped; the judge decides.${perm}`;
    $("apBtn").textContent = "Stop";
  } else if (running) {
    $("apTitle").textContent = `Autopilot: trying each function's machine draft · ${ap.matched.toLocaleString()} matched for free`;
    const bits = [`${done.toLocaleString()} of ${ap.total.toLocaleString()} done`];
    if (failed) bits.push(`${failed.toLocaleString()} didn't build`);
    if (cached) bits.push(`${cached.toLocaleString()} unchanged since the last run`);
    if (ap.near) bits.push(`${ap.near} more are 90%+ there`);
    if (ap.twins) bits.push(`${ap.twins.toLocaleString()} from identical code`);
    $("apSub").textContent = `${bits.join(" · ")}. Agents work alongside it.${perm}`;
    $("apBar").firstElementChild.style.width = `${(100 * done) / Math.max(1, ap.total)}%`;
    $("apBtn").textContent = "Stop";
  } else if (ap || tw) {
    const total = (ap ? ap.matched : 0) + (ap ? 0 : twinsDone);
    $("apTitle").textContent = twinsRunning ? `Matching identical code: ${tw.matched.toLocaleString()} so far`
      : total ? `Autopilot matched ${total.toLocaleString()} function${total === 1 ? "" : "s"} for free` : "Autopilot found no free matches this time";
    const parts = [];
    if (twinsDone) parts.push(`${twinsDone.toLocaleString()} from identical code`);
    if (tw && tw.asm) parts.push(`${tw.asm.toLocaleString()} copies of assembly set aside (not for agents)`);
    if (ap && ap.tried) parts.push(`${ap.tried.toLocaleString()} machine drafts tried${ap.near ? `, ${ap.near} more 90%+ there` : ""}`);
    if (failed) parts.push(`${failed.toLocaleString()} didn't build`);
    if (cached) parts.push(`${cached.toLocaleString()} unchanged, not tried again`);
    $("apSub").textContent = (parts.length ? parts.join(" · ") + ". " : "")
      + (reasons.length ? `Why not: ${reasons.slice(0, 3).join("; ")}. ` : "")
      + (ap && ap.no_drafts ? `${ap.no_drafts}.` : "Run it again after agents add types and names: drafts get better.") + perm;
    $("apBtn").textContent = "Run again";
  } else {
    $("apTitle").textContent = "Free matches first";
    $("apSub").textContent = (ov.kind === "fork" && ov.origin !== "scratch"
      ? "Autopilot matches the code this game has more than once from its matched copies (names swapped, judged like any attempt)"
      : "Autopilot matches identical copies of matched code and tries every function's machine draft (m2c) through the judge") + `; whatever matches costs no agent time.${perm}`;
    $("apBtn").textContent = "Run autopilot";
  }
  $("apBtn").onclick = async () => {
    $("apBtn").disabled = true;
    try {
      S.ov = await api("/api/autopilot", { method: "POST", body: { slug: S.slug, action: running ? "stop" : "start" } });
      renderAutopilot(S.ov);
    } catch (e) { toast(esc(e.message), "err"); } finally { $("apBtn").disabled = false; }
  };
}

const LAYOUT = { gc: "dtk-template", wii: "dtk-template", nds: "ds-decomp" };
const cwName = (c) => (c.startsWith("DS/") ? `CodeWarrior for DS ${c.slice(3)}` : `CodeWarrior ${c}`);

/* ------------------------------------------------------------------ source: the base for ports and mods */
function renderSource(ov) {
  const ex = ov.export, fork = ov.kind === "fork";
  $("srcExport").hidden = fork || !ov.exportable;
  $("srcWhen").textContent = !fork && ex ? `exported ${since(ex.t)}` : "";
  if (fork) {
    const rb = ov.rebuild;
    $("srcText").innerHTML = ov.origin === "scratch"
      ? `This project <b>is</b> the source tree: a ${LAYOUT[ov.platform] || "decomp"} project that rebuilds your game on every build. Every match is committed to git in the project folder.`
        + (rb ? `<p class="small ${rb.identical ? "ok" : "warn-text"}">${rb.identical ? "✓ The rebuilt game is your copy, byte for byte" + (/padding/.test(rb.detail) ? " (up to the padding at its end: your copy is trimmed)." : ".")
          : `The rebuilt game isn't your copy byte for byte yet: ${esc(rb.detail)}. Decompiling works anyway: every function is compared with your copy's own code.`}</p>` : "")
      : `This project <b>is</b> the source tree${ov.repo_url ? ` (a fork of <a class="link" href="${esc(ov.repo_url)}" target="_blank" rel="noopener">${esc(ov.repo_url.replace(/^https?:\/\//, ""))}</a>)` : ""}. Every match is committed to the <code>decomp-wtf</code> branch, ready to send upstream.`;
    $("srcState").innerHTML = `<div class="row"><button class="ghost small-btn" id="forkMeasure" type="button">Re-measure with objdiff</button>${rb ? `<button class="ghost small-btn" id="romCheck" type="button">Rebuild the game and compare</button>` : ""}</div>`;
    if (rb) $("romCheck").onclick = async () => {
      $("romCheck").disabled = true;
      toast("Rebuilding the game and comparing it with your copy…");
      try {
        const { job } = await api("/api/fork/rom-check", { method: "POST", body: { slug: S.slug } });
        const r = await followJob(job);
        toast(r.identical ? "The rebuilt game is your copy, byte for byte." : `Not identical yet: ${esc(r.detail)}`, r.identical ? "match" : "");
        S.ov = await api(`/api/project?slug=${S.slug}`);
        renderSource(S.ov);
      } catch (e) { toast(esc(e.message), "err"); if ($("romCheck")) $("romCheck").disabled = false; }
    };
    $("forkMeasure").onclick = async () => {
      toast("Measuring the whole project with objdiff…");
      try { const { job } = await api("/api/fork/check", { method: "POST", body: { slug: S.slug } }); await followJob(job); toast("Progress updated.", "match"); refreshMap(); }
      catch (e) { toast(esc(e.message), "err"); }
    };
    return;
  }
  if (!ov.exportable) {
    $("srcText").textContent = `Exporting a rebuildable project isn't available for ${ov.platform_name || "this platform"} yet. Your matched ${ov.source_language || "C"} is kept in the project.`;
    $("srcState").innerHTML = "";
    return;
  }
  if (["windows", "linux"].includes(ov.platform)) {
    $("srcText").innerHTML = "decomp.anything writes a normal <b>CMake/IDE project</b>: one source file per matched function, <code>include/context.h</code>, <code>compile_commands.json</code> and <code>judge.py</code>. Open it in VS Code, CLion, Visual Studio or your editor, compile with your own toolchain, and run <code>python judge.py --all</code> to verify the matched functions byte for byte. It does not relink a PE/ELF; that stays in your normal toolchain.";
    const ex2 = ov.export;
    $("srcState").innerHTML = ex2
      ? `<div class="src-line ok">✓ ${ex2.matched || 0} matched function${ex2.matched === 1 ? "" : "s"} exported</div>${ex2.path ? `<div class="src-line">${esc(ex2.path)}</div>` : ""}`
      : `<span class="muted small">Not exported yet.</span>`;
    $("srcExport").textContent = ex2 ? "Export again" : "Export IDE project";
    return;
  }
  $("srcText").innerHTML = "decomp.anything writes a complete decompilation project: the matched functions as C, everything else as assembly, a linker script and <code>build.py</code>. Then it builds it to prove it rebuilds your game <b>byte for byte</b>. It's the base for ports and mods; game code and data stay out of git, so the tree is safe to put in a repository.";
  const stale = ex && ex.matched != null && ex.matched < ov.matched;
  $("srcState").innerHTML = !ex ? `<span class="muted small">Not exported yet.</span>`
    : `<div class="src-line ${ex.identical === false ? "bad" : "ok"}">${ex.identical === false ? "✕ the last rebuild differed" : "✓ rebuilds byte for byte"}</div>
       <div class="src-line">${ex.matched != null ? `${ex.matched.toLocaleString()} of ${ex.functions.toLocaleString()} functions in C` : ""}${stale ? ` · <b>${ov.matched - ex.matched} new matches since</b>` : ""}</div>
       ${ex.path ? `<div class="src-line">${esc(ex.path)}</div>` : ""}`;
  $("srcExport").textContent = ex ? "Export again" : "Export project";
}
$("srcOpen").onclick = () => openFolder();
$("srcIde").onclick = async () => {
  try { const r = await api("/api/ide", { method: "POST", body: { slug: S.slug } }); toast(`Opening in ${esc(r.editor)}.`); }
  catch (e) { toast(esc(e.message), "err"); }
};
$("srcExport").onclick = async () => {
  $("srcExport").disabled = true;
  $("expLog").hidden = false;
  $("expResult").innerHTML = "";
  try {
    const { job } = await api("/api/export", { method: "POST", body: { slug: S.slug } });
    const r = await followJob(job, $("expLog"));
    if (r.kind === "pc") {
      $("expResult").innerHTML = `<div class="notice room"><span><b>IDE project exported.</b> ${r.matched || 0} matched function${r.matched === 1 ? "" : "s"}; open the folder in your IDE and verify with <code>python judge.py --all</code>.</span></div>${cmdBox(`cd "${r.path}" && python judge.py --all`)}`;
    } else {
      $("expResult").innerHTML = r.identical === false
        ? `<div class="notice bad"><span><b>Exported, but the rebuild differs.</b> ${esc(r.log.split("\n").filter(Boolean).pop() || "")}</span></div>`
        : `<div class="notice room"><span><b>Rebuilds byte for byte.</b> ${r.matched != null ? `${r.matched} of ${r.functions} functions are C.` : ""}</span></div>${cmdBox(`cd "${r.path}" && python build.py`)}`;
    }
    wireCopies($("expResult"));
    S.ov = await api(`/api/project?slug=${S.slug}`);
    renderSource(S.ov);
  } catch (e) { toast(esc(e.message), "err"); } finally { $("srcExport").disabled = false; }
};

