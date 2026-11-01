/* ------------------------------------------------------------------ project */
async function openProject(slug, tab = "overview", fn = "") {
  if (S.timer) { clearInterval(S.timer); S.timer = null; }
  if (S.statsTimer) { clearInterval(S.statsTimer); S.statsTimer = null; }
  const openToken = ++S.openSeq || (S.openSeq = 1);
  show("project");
  S.slug = slug; S.e = 0; S.a = 0; S.seq = {}; S.scores = new Map(); S.flash = new Map(); S.sessions = []; S.claims = [];
  S.apFeed = null; S.stats = null; S.map = []; S.mapSig = null; geo = null;
  $("activity").innerHTML = ""; $("thoughts").innerHTML = ""; $("now").innerHTML = ""; $("expLog").hidden = true; $("expResult").innerHTML = ""; $("teamBox").innerHTML = "";
  $("intakePanel").hidden = true; $("intakeBody").textContent = ""; $("intakeSummary").textContent = "";
  try {
    if (!S.st) await loadState();
    if (openToken !== S.openSeq) return;
    S.ov = await api(`/api/project?slug=${slug}`);
    if (openToken !== S.openSeq) return;
  } catch (e) {
    if (openToken !== S.openSeq) return;
    toast(esc(e.message), "err");
    location.hash = "#/";
    return;
  }
  document.querySelectorAll("#ptabs [data-ptab]").forEach((a) => { a.href = `#/p/${slug}/${a.dataset.ptab}`; });
  const fork = S.ov.kind === "fork";
  const analysis = S.ov.kind === "analysis";
  const native = isNativeProject(S.ov);
  $("summaryTab").hidden = !analysis;
  S.summary = null;
  $("mapTitle").textContent = analysis ? "Work map" : fork ? "Function map" : native ? "Function map" : "ROM map";
  const archive = analysis && !!((S.ov.analysis || {}).artifact || {}).entries;
  $("mapHint").textContent = analysis
    ? archive ? "Every square is a file or task. Files with an analyzer open as child projects; other files open in Contents."
      : "Every square is a procedure or task. Procedures open in the RE console, the deep viewer behind this project."
    : fork ? "Every square is a function, grouped by source file. Click one to see it." : native ? "Every square is a function, in the binary's order. Click one to see it." : "Every square is a function, in ROM order. Click one to see it.";
  $("thAddr").textContent = fork ? "File" : "Address";
  $("thInsns").textContent = analysis ? "Size" : "Instructions";
  $("fnSearch").placeholder = analysis ? "Find a work item or address  ( / )" : "Find a function, address or file  ( / )";
  $("fnTabLabel").textContent = analysis ? "Work items" : "Functions";
  $("kpiMatchedLabel").textContent = analysis ? "Analyzed" : "Matched code";
  $("kpiFnLabel").textContent = analysis ? "Work items" : "Functions";
  $("kpiPaceLabel").textContent = analysis ? "Findings" : "Pace";
  $("recentTitle").textContent = analysis ? "Recently analyzed" : "Recent matches";
  $("recentAll").textContent = analysis ? "All analyzed" : "All matched";
  $("boardTitle").textContent = analysis ? "Who analyzed what" : "Who matched what";
  $("sourceTab").hidden = analysis;
  $("teamTab").hidden = analysis;
  $("dataTab").hidden = analysis || fork;
  $("addTask").hidden = !analysis;
  $("thBest").hidden = analysis;
  $("thTries").hidden = analysis;
  $("thHits").hidden = analysis;
  $("addTask").onclick = analysis ? addAnalysisTask : null;
  $("addFeature").onclick = analysis ? addFeatureDialog : null;
  S.analysis = analysis;
  if (analysis && (tab === "source" || tab === "team")) tab = "overview";
  groups.clear(); S.claimSig = "";
  L.q = ""; L.filter = "all"; L.limit = 200; $("fnSearch").value = "";
  $("instrEdit").value = S.ov.instructions || "";
  renderHeader(S.ov);
  markNav();
  const setup = fork && !S.ov.ready;
  $("ptabs").hidden = setup;
  $("forkSetup").hidden = !setup;
  document.querySelectorAll("[data-pane]").forEach((p) => { p.hidden = true; });
  if (setup) { renderForkSetup(); return; }
  setTab(tab);
  renderStats(S.ov);
  renderIntake(slug);
  renderContents();
  await refreshMap();
  if (openToken !== S.openSeq) return;
  await renderFeatures(true);
  if (openToken !== S.openSeq) return;
  await renderSources(true);
  if (openToken !== S.openSeq) return;
  await poll(true);
  if (openToken !== S.openSeq) return;
  if (fn) openFn(fn);  // a link to one function (#/p/slug/functions/name) opens on it
  if (S.timer) clearInterval(S.timer);
  if (S.statsTimer) clearInterval(S.statsTimer);
  S.timer = setInterval(() => poll(false), 1000);
  S.statsTimer = setInterval(loadStats, 8000);
  loadStats();
}

/* ------------------------------------------------------------------ archive contents */

function contentsDirs(nodes, out = []) {
  for (const n of nodes || []) {
    if (n.kind === "directory") { out.push(n.path); contentsDirs(n.children, out); }
  }
  return out;
}

function contentsFind(nodes, path) {
  for (const n of nodes || []) {
    if (n.path === path) return n;
    if (n.kind === "directory") {
      const found = contentsFind(n.children, path);
      if (found) return found;
    }
  }
  return null;
}

function contentsRow(n, depth) {
  const dir = n.kind === "directory";
  const open = dir && !CT.collapsed.has(n.path);
  const selected = n.path === CT.selected ? " sel" : "";
  const caret = dir ? `<button class="contents-caret" type="button" data-ctoggle="${esc(n.path)}" aria-expanded="${open}">${open ? "▾" : "▸"}</button>` : `<span class="contents-caret"></span>`;
  const type = dir ? "DIR" : (n.entry_format || n.entry_type || "FILE");
  const size = dir ? "" : bytes(n.size || 0);
  const child = n.child ? `<span class="contents-child">child</span>` : "";
  return `<div class="contents-row${selected}${dir ? " dir" : ""}" data-cpath="${esc(n.path)}" data-depth="${depth}" role="treeitem">
    ${caret}<span class="contents-type">${esc(type)}</span><span class="contents-name" title="${esc(n.path)}">${esc(n.name)}</span>
    <span class="contents-size">${esc(size)}</span><span class="contents-marks">${child}</span></div>`;
}

function contentsRows(nodes, depth = 0) {
  const out = [];
  for (const n of nodes || []) {
    out.push(contentsRow(n, depth));
    if (n.kind === "directory" && !CT.collapsed.has(n.path)) out.push(...contentsRows(n.children, depth + 1));
  }
  return out;
}

function contentsDetail(n) {
  if (!n) return "";
  const facts = [["Path", n.path], ["Type", n.entry_type || "unknown"], ["Format", n.entry_format || "—"],
    ["Size", n.size == null ? "—" : bytes(n.size)], ["SHA-256", n.sha256 || "—"], ["Tool", n.tool || "—"]];
  let action = "";
  if (n.child) action = `<a class="primary small-btn as-btn" href="#/p/${esc(n.child)}">Open child project</a>`;
  else if (n.analyzable) action = `<button class="primary small-btn" type="button" data-canalyze="${esc(n.path)}">Analyze this file</button>`;
  else action = `<span class="muted small">No child analyzer for this type yet.</span>`;
  return `<div class="contents-detail-head"><b>${esc(n.path)}</b><span class="grow"></span>${action}</div>
    <dl class="contents-facts">${facts.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>`;
}

function renderContentsDetail() {
  const box = $("contentsDetail");
  const n = CT.selected ? contentsFind(CT.data?.nodes, CT.selected) : null;
  box.hidden = !n;
  box.innerHTML = contentsDetail(n);
  if (n && n.kind === "file") {
    const pre = document.createElement("pre");
    pre.className = "contents-preview";
    pre.id = "contentsPreview";
    pre.textContent = "Reading…";
    box.append(pre);
    loadContentsPreview(n.path);
  }
}

async function loadContentsPreview(path) {
  CT.preview = { slug: S.slug, path };
  const pre = $("contentsPreview");
  if (!pre) return;
  try {
    const text = await api(`/api/contents/read?slug=${encodeURIComponent(S.slug)}&entry=${encodeURIComponent(path)}&length=4096`);
    if (CT.preview?.slug === S.slug && CT.preview.path === path && pre.isConnected) pre.textContent = text;
  } catch (e) {
    if (CT.preview?.slug === S.slug && CT.preview.path === path && pre.isConnected) pre.textContent = e.message;
  }
}

function renderContentsTree() {
  const box = $("contentsTree");
  if (!CT.data) { box.innerHTML = `<p class="muted small">Reading the archive…</p>`; return; }
  const rows = contentsRows(CT.data.nodes);
  box.innerHTML = rows.length ? rows.join("") : `<p class="muted small">No files in this archive.</p>`;
  box.querySelectorAll("[data-depth]").forEach((el) => el.style.setProperty("--depth", el.dataset.depth));
  $("contentsCount").textContent = `${(CT.data.entries || 0).toLocaleString()} entries · ${(CT.data.files || 0).toLocaleString()} files`;
  renderContentsDetail();
}

async function renderContents() {
  if (!S.ov || S.ov.kind !== "analysis" || !S.ov.analysis?.artifact?.entries) {
    $("contentsPanel").hidden = true;
    return;
  }
  $("contentsPanel").hidden = false;
  if (CT.slug !== S.slug) {
    CT.slug = S.slug; CT.data = null; CT.selected = null; CT.preview = null; CT.busy = false;
    CT.collapsed = new Set();
  }
  try {
    const data = await api(`/api/contents?slug=${encodeURIComponent(S.slug)}`);
    if (CT.slug !== S.slug) return;
    const fresh = CT.data === null;
    CT.data = data;
    if (fresh) CT.collapsed = new Set(contentsDirs(data.nodes));
    if (CT.pendingSelect) {
      const pending = CT.pendingSelect;
      CT.pendingSelect = null;
      if (pending.slug === S.slug && contentsFind(data.nodes, pending.path)) {
        CT.selected = pending.path;
        const parts = pending.path.split("/");
        for (let i = 1; i < parts.length; i++) CT.collapsed.delete(parts.slice(0, i).join("/"));
      }
    }
    renderContentsTree();
  } catch (e) {
    $("contentsTree").innerHTML = `<p class="bad small">${esc(e.message)}</p>`;
  }
}

$("contentsTree").addEventListener("click", (e) => {
  const toggle = e.target.closest("[data-ctoggle]");
  if (toggle) {
    e.preventDefault(); e.stopPropagation();
    const path = toggle.dataset.ctoggle;
    if (CT.collapsed.has(path)) CT.collapsed.delete(path); else CT.collapsed.add(path);
    renderContentsTree();
    return;
  }
  const row = e.target.closest("[data-cpath]");
  if (!row) return;
  const path = row.dataset.cpath;
  const node = contentsFind(CT.data?.nodes, path);
  if (!node) return;
  if (node.kind === "directory") {
    if (CT.collapsed.has(path)) CT.collapsed.delete(path); else CT.collapsed.add(path);
    renderContentsTree();
    return;
  }
  CT.selected = path;
  renderContentsTree();
});

$("contentsDetail").addEventListener("click", async (e) => {
  const button = e.target.closest("[data-canalyze]");
  if (!button || CT.busy) return;
  const entry = button.dataset.canalyze;
  CT.busy = true;
  button.disabled = true;
  button.textContent = "Opening…";
  try {
    const start = await api("/api/analysis/entry", { method: "POST", body: { slug: S.slug, entry } });
    const result = await followJob(start.job);
    loadNav();
    location.hash = `#/p/${result.slug}`;
  } catch (err) {
    toast(esc(err.message), "err");
    button.disabled = false;
    button.textContent = "Analyze this file";
  } finally {
    CT.busy = false;
  }
});

