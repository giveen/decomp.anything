/* ------------------------------------------------------------------ sources (recovered / reconstructed)
   Source maps give original files back exactly; modules without one become reconstruction items agents write with
   write_source. Verified and unverified are never mixed up. */
const SRC = { data: null, slug: null, busy: false };

function sourceState(f) {
  if (f.state === "verified") return "verified";
  if (f.state === "reconstructed") return "reconstructed";
  return SRC.data && SRC.data.kind === "analysis" ? "to reconstruct" : "no code yet";
}

async function renderSources(force = false) {
  const panel = $("sourcesPanel");
  const rec = S.ov && S.ov.kind !== "fork"
    ? (S.ov.analysis ? (S.ov.analysis || {}).reconstruction : S.ov.reconstruction) : null;
  if (!rec) { panel.hidden = true; SRC.data = null; return; }
  panel.hidden = false;
  if (SRC.slug !== S.slug) { SRC.slug = S.slug; SRC.data = null; }
  if (!SRC.data || force) {
    try { SRC.data = await api(`/api/sources?slug=${encodeURIComponent(S.slug)}`); }
    catch (e) { $("sourceList").innerHTML = `<p class="bad small">${esc(e.message)}</p>`; return; }
  }
  const c = SRC.data.counts || {};
  const tier = SRC.data.tier || (SRC.data.kind === "rom" ? "match" : "source-map");
  $("sourcesCount").textContent = tier === "match"
    ? `${c.matches || 0} matched · ${c.reconstructed || 0} reconstructed · ${c.pending || 0} without code`
    : tier === "binary"
    ? `${c.reconstructed || 0} reconstructed · ${c.pending || 0} without code`
    : `${c.recovered || 0} recovered · ${c.reconstructed || 0} reconstructed · ${c.pending || 0} to go`;
  $("sourcesNote").textContent = tier === "binary"
    ? "This target was analyzed, not matched: there is no byte-exact reference, so every file here is a reconstruction."
    : "Original files and matched code are verified; reconstructed code is kept apart.";
  $("sourceList").innerHTML = (SRC.data.files || []).map((f) => `
    <div class="source-row" data-source="${esc(f.path)}">
      <span class="source-state ${f.state === "verified" ? "ok" : f.state === "reconstructed" ? "warn" : "open"}">${esc(sourceState(f))}</span>
      <span class="source-path" title="${esc(f.path)}">${esc(f.path)}</span>
      <span class="source-size">${f.size ? bytes(f.size) : ""}</span>
    </div>`).join("") || `<p class="muted small">No reconstructed code yet: claim a work item and use write_source.</p>`;
}

async function openSource(path) {
  try {
    const f = await api(`/api/sources/file?slug=${encodeURIComponent(S.slug)}&path=${encodeURIComponent(path)}`);
    const state = f.state === "verified" ? (f.kind === "rom" || SRC.data?.kind === "rom" ? "matched byte for byte"
      : "recovered from its source map") : f.state === "reconstructed" ? "reconstructed by an agent (unverified)"
      : "no code yet";
    const body = dialog(f.path, `<p class="muted small">${esc(state)}${f.map ? ` · ${esc(f.map)}` : ""}</p>
      <pre class="intake-md source-code">${esc(f.source || "(no source yet: claim it and use write_source)")}</pre>
      <div class="row"><button class="ghost" id="copySource" type="button">Copy</button></div>`);
    body.querySelector("#copySource").onclick = () => navigator.clipboard.writeText(f.source || "");
  } catch (e) { toast(esc(e.message), "err"); }
}

$("sourceList").addEventListener("click", (e) => {
  const row = e.target.closest("[data-source]");
  if (row) openSource(row.dataset.source);
});
$("exportSources").onclick = async (e) => {
  if (SRC.busy) return;
  SRC.busy = true;
  e.target.disabled = true;
  try {
    const start = await api("/api/sources/export", { method: "POST", body: { slug: S.slug } });
    const result = await followJob(start.job);
    toast(`Exported ${result.files} files (${result.verified} verified, ${result.unverified} unverified) to ${esc(result.zip)}`, "match");
  } catch (err) { toast(esc(err.message), "err"); }
  finally { SRC.busy = false; e.target.disabled = false; }
};

let summaryReq = 0;
async function renderSummary() {
  if (!S.ov || S.slug == null || S.ov.kind !== "analysis") return;
  const req = ++summaryReq;
  $("summaryText").textContent = "Building the summary…";
  $("summaryMeta").textContent = "";
  try {
    const report = await api(`/api/summary?slug=${encodeURIComponent(S.slug)}`);
    if (req !== summaryReq || S.slug == null) return;
    S.summary = report;
    $("summaryText").textContent = report.markdown || "Nothing to summarize yet.";
    $("summaryMeta").textContent = report.summary || "";
    $("summaryDownload").disabled = !report.markdown;
    $("summaryCopy").disabled = !report.markdown;
  } catch (e) {
    if (req === summaryReq) $("summaryText").textContent = e.message;
  }
}
$("summaryDownload").onclick = () => {
  if (!S.summary?.markdown) return;
  const blob = new Blob([S.summary.markdown], { type: "text/markdown;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${S.slug}-summary.md`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
};
$("summaryCopy").onclick = async () => {
  if (!S.summary?.markdown) return;
  try { await navigator.clipboard.writeText(S.summary.markdown); toast("Summary copied."); }
  catch { toast("Couldn't reach the clipboard: select the text instead.", "err"); }
};
$("summaryFolder").onclick = () => openFolder();

async function renderData() {
  if (!S.ov || S.tab !== "data") return;
  const q = $("dataSearch").value.trim();
  $("dataShown").textContent = "loading…";
  try {
    const r = await api(`/api/data?slug=${S.slug}&q=${encodeURIComponent(q)}&limit=2000`);
    $("dataRows").innerHTML = r.rows.length ? r.rows.map((d) => `<tr>
      <td class="mono dim">${hex8(d.address)}</td><td class="mono name">${esc(d.name)}</td>
      <td>${esc(d.kind)}</td><td class="r mono">${d.size || "—"}</td>
      <td class="mono dim">${esc(d.preview || "")}</td></tr>`).join("")
      : `<tr class="none"><td colspan="5">No data symbols${q ? ` matching “${esc(q)}”` : ""}.</td></tr>`;
    $("dataShown").textContent = `${r.rows.length} data symbol${r.rows.length === 1 ? "" : "s"}`;
  } catch (e) { $("dataShown").textContent = e.message; }
}
$("dataSearch").oninput = () => { clearTimeout(S.dataTimer); S.dataTimer = setTimeout(renderData, 250); };
$("dataSearch").onkeydown = (e) => { if (e.key === "Enter") renderData(); };
function addAnalysisTask() {
  const body = dialog("Add a task", `<p class="muted">A task is a work item without an address: an area, a question or a goal for one agent. Procedures already in the target are listed as their own items.</p>
    <label class="field"><span>Task</span><input id="taskName" placeholder="Map the update protocol"></label>
    <label class="field"><span>Brief for the agent</span><textarea id="taskBrief" rows="5" placeholder="What to find out, where to start, what counts as done"></textarea></label>
    <div class="row"><button class="primary" id="taskGo" type="button">Add task</button><button class="ghost" data-close type="button">Cancel</button></div>`);
  body.querySelector("#taskGo").onclick = async (e) => {
    const name = body.querySelector("#taskName").value.trim();
    if (!name) return;
    e.target.disabled = true;
    try {
      await api("/api/analysis/task", { method: "POST", body: { slug: S.slug, name, brief: body.querySelector("#taskBrief").value } });
      closeDialog();
      await refreshMap(true);
      toast("Task added.", "match");
    } catch (err) { toast(esc(err.message), "err"); e.target.disabled = false; }
  };
}

function stopPolling() {
  if (S.timer) clearInterval(S.timer);
  if (S.statsTimer) clearInterval(S.statsTimer);
  S.timer = null; S.statsTimer = null;
  connOk();
}

function setTab(tab) {
  if (S.ov && S.ov.kind === "analysis" && (tab === "source" || tab === "team")) tab = "overview";
  if (S.ov && S.ov.kind !== "analysis" && tab === "summary") tab = "overview";
  if (!TABS.includes(tab)) tab = "overview";
  S.tab = tab;
  document.querySelectorAll("#ptabs [data-ptab]").forEach((a) => {
    const on = a.dataset.ptab === tab;
    a.classList.toggle("on", on);
    a.setAttribute("aria-selected", on);
    a.tabIndex = on ? 0 : -1;  // one stop in the Tab order: the tab bar, then the arrows move between tabs
  });
  if (S.ov && S.ov.kind === "fork" && !S.ov.ready) return;
  document.querySelectorAll("[data-pane]").forEach((p) => { p.hidden = p.dataset.pane !== tab; });
  if (tab === "overview") { if (S.map.length) layoutMap(); renderOverview(); renderContents(); renderFeatures(); renderSources(); }
  if (tab === "functions") renderList();
  if (tab === "summary") renderSummary();
  if (tab === "data") renderData();
  if (tab === "agents") { renderLauncher($("launcher")); renderSessions(S.sessions); renderPrio(); }
  if (tab === "team") renderTeam();
  if (tab === "source" && S.ov) renderSource(S.ov);
  if (tab === "settings") renderProjectSettings();
  if (tab !== "team") clearInterval(teamTimer);
}
/* The tab bar as a real tab bar: panes are tied to their tab for screen readers, and the arrow keys (Home/End too)
   move between them instead of making all nine tab stops of their own. */
document.querySelectorAll("#ptabs [data-ptab]").forEach((a) => {
  const tab = a.dataset.ptab, pane = document.querySelector(`[data-pane="${tab}"]`);
  const tabId = a.id || (a.id = `tab-${tab}`);  // keep the ids the app already uses (summaryTab, dataTab, ...)
  a.tabIndex = -1;
  if (!pane) return;
  if (!pane.id) pane.id = `pane-${tab}`;
  pane.setAttribute("role", "tabpanel");
  pane.setAttribute("aria-labelledby", tabId);
  a.setAttribute("aria-controls", pane.id);
});
$("ptabs").addEventListener("keydown", (e) => {
  const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key], end = { Home: false, End: true }[e.key];
  if (step === undefined && end === undefined) return;
  const tabs = [...document.querySelectorAll("#ptabs [data-ptab]")].filter((a) => !a.hidden);
  const at = tabs.indexOf(document.activeElement);
  if (at < 0) return;
  e.preventDefault();
  const next = end === undefined ? tabs[(at + step + tabs.length) % tabs.length] : tabs[end ? tabs.length - 1 : 0];
  next.focus();
  next.click();
});
document.querySelectorAll("[data-goto]").forEach((a) => {
  a.onclick = (e) => {
    e.preventDefault();
    if (a.dataset.filter) { L.filter = a.dataset.filter; L.limit = 200; }
    location.hash = `#/p/${S.slug}/${a.dataset.goto}`;
  };
});

/* The whole map only when something changed (`force`: a rename or a split, which the signature can't see). */
async function refreshMap(force = true) {
  const slug = S.slug;
  const data = await api(`/api/map?slug=${slug}${!force && S.mapSig ? `&since=${encodeURIComponent(S.mapSig)}` : ""}`);
  if (slug !== S.slug) return;
  S.lastMapFetch = Date.now();
  if (data.same) return;
  S.mapSig = data.sig;
  S.map = data.rows.map((r) => { const f = {}; data.keys.forEach((k, i) => { f[k] = r[i]; }); return f; });
  S.byName = new Map(S.map.map((f, i) => [f.name, i]));
  S.map.forEach((f, i) => { f.i = i; });
  baseDirty = true;
  $("fnTabCount").textContent = S.map.length.toLocaleString();
  $("dataTabCount").textContent = S.ov && S.ov.data ? S.ov.data.toLocaleString() : "";
  if (S.tab === "overview") layoutMap(); else applyClaims(false);
  renderList();
  renderProgress();
  renderOverview();
}

/* Helpers that just stopped on an error (the provider out of credit, a sign-in...): said once per error, as eight
   agents on one account stop together */
function toastStopped(before, now) {
  const was = new Map(before.map((s) => [s.id, s.state]));
  const why = new Map();
  for (const s of now) {
    if (s.state !== "error" || !was.has(s.id) || was.get(s.id) === "error") continue;
    if (!why.has(s.error)) why.set(s.error, []);
    why.get(s.error).push(s.label);
  }
  for (const [error, labels] of why) {
    const who = labels.length > 1 ? `${labels[0]} and ${labels.length - 1} more` : labels[0];
    toast(`${esc(who)} stopped: ${esc(error || "an error")}`, "err");
  }
}

/* When the local app goes away (a restart, an update installing) polling keeps retrying every second. Say it once,
   where it is visible from every tab, instead of only in the Activity pane's tiny hint. */
let connSince = 0, connTimer = null;
function connLost() {
  if (connTimer) return;
  connSince = Date.now();
  const tick = () => {
    const s = Math.max(1, Math.round((Date.now() - connSince) / 1000));
    $("connBanner").textContent = `Can't reach decomp.anything · retrying… ${s}s`;
  };
  tick();
  $("connBanner").hidden = false;
  connTimer = setInterval(tick, 1000);
}
function connOk() {
  if (!connTimer) return;
  clearInterval(connTimer);
  connTimer = null;
  $("connBanner").hidden = true;
}

let polling = false;
async function poll(first) {
  if (polling || !S.slug) return;
  polling = true;
  const slug = S.slug;
  try {
    const live = await api(`/api/live?slug=${slug}&e=${S.e}&a=${S.a}&s=${encodeURIComponent(JSON.stringify(S.seq))}`);
    if (slug !== S.slug) return;
    connOk();
    S.ov = live.overview;
    let mapDirty = false;
    for (const ev of live.events) {
      S.e = Math.max(S.e, ev.id);
      if (["match", "split", "rename"].includes(ev.kind)) mapDirty = true;
      if (ev.kind === "match" && !first) {
        S.flash.set(ev.detail, performance.now());
        if (ev.agent !== AUTO && ev.agent !== "@" + AUTO) toast(`<code>${esc(ev.detail)}</code>&nbsp;matched by ${esc(ev.agent || "an agent")}`, "match", true);
      }
      if (ev.kind === "autopilot" && !first) toast(`Autopilot: ${esc(ev.detail)}`, "match");
      if (ev.kind === "link" && !first) toast(`🔗 <code>${esc(ev.detail)}</code>&nbsp;is linked into the game`, "match");
    }
    for (const at of live.attempts) {
      S.a = Math.max(S.a, at.id);
      if (!S.scores.has(at.function)) S.scores.set(at.function, []);
      S.scores.get(at.function).push({ score: at.score, t: at.created_at, insns: at.n_insns });
    }
    if (!first) applyAttempts(live.attempts);
    S.claims = live.claims;
    if (!first) toastStopped(S.sessions || [], live.sessions);
    S.sessions = live.sessions;  // before the feed draws: its lines name who did the work by model
    addActivity(live.events, live.attempts, first);
    for (const s of live.sessions) {
      if (s.feed.length) S.seq[s.id] = s.feed[s.feed.length - 1].seq;
    }
    addThoughts(live.sessions, first);
    if (S.tab === "agents") renderSessions(live.sessions);
    renderNow(live.claims);
    renderStats(live.overview);
    const claimSig = live.claims.map((c) => c.name + "\u0000" + c.agent).join("\n");
    if (mapDirty || Date.now() - S.lastMapFetch > 20000) await refreshMap(mapDirty);
    else {
      applyClaims();
      if (claimSig !== S.claimSig || live.attempts.length) renderList();
    }
    S.claimSig = claimSig;
    if (S.fn && !$("drawer").hidden) {
      const touched = live.attempts.some((a) => a.function === S.fn) || live.events.some((e) => e.detail === S.fn);
      if (touched) refreshFn(false); else fnBadge();
    }
    $("feedHint").textContent = "";
  } catch (e) {
    if (String(e.message).includes("Failed to fetch")) {
      connLost();
      $("feedHint").textContent = "app not reachable";
    }
  } finally {
    polling = false;
  }
}

async function loadStats() {
  if (!S.slug || S.tab !== "overview") return;
  try { S.stats = await api(`/api/stats?slug=${S.slug}`); } catch { return; }
  renderOverview();
}

/* Agents at work on this project: the helpers this app runs, plus outside agents (yours, the room's) with a claim. */
function workingAgents() {
  const running = S.sessions.filter((s) => ["starting", "working", "waiting"].includes(s.state));
  const own = new Set(S.sessions.map((s) => s.agent));
  const outside = [...new Set(S.claims.filter((c) => !own.has(c.agent) && c.agent !== AUTO).map((c) => c.agent))];
  return { running, outside, n: running.length + outside.length };
}

/* recommended | beta | "" for a platform key, as the browser or a project names it. */
function platformStatus(p) {
  const st = (S.st && S.st.platform_status) || {};
  const key = { gcwii: "gc", ps: "psx", playstation: "psx" }[p] || p;
  return st[key] || "";
}
function markPlatforms() {
  const any = Object.keys((S.st && S.st.platform_status) || {}).length;
  const installed = new Set(((S.st && S.st.toolchains) || []).filter((t) => t.installed).map((t) => t.id));
  const needs = {
    gc: ["mwcc"], wii: ["mwcc"], nds: ["mwccarm"],
    n64: ["gcc-2.7.2-kmc", "ido-7.1", "ido-5.3"],
    psx: ["gcc-2.7.2-psx", "gcc-2.8.1-psx"], ps2: ["ee-gcc-2.96"], gba: ["agbcc"],
    windows: ["mingw-x86_64", "mingw-x86", "mingwxx-x86_64", "mingwxx-x86",
              "clang-cl-x86_64", "clang-cl-x86", "clang-clxx-x86_64", "clang-clxx-x86",
              "msvc-x86_64", "msvc-x86", "msvcxx-x86_64", "msvcxx-x86"],
    linux: ["gcc-linux-x86_64", "gxx-linux-x86_64", "clang-linux-x86_64", "clangxx-linux-x86_64",
            "gcc-linux-x86", "gxx-linux-x86", "clang-linux-x86", "clangxx-linux-x86"],
  };
  document.querySelectorAll("#platRow [data-p]").forEach((s) => {
    const rec = platformStatus(s.dataset.p) === "recommended";
    const on = (needs[s.dataset.p] || []).some((id) => installed.has(id));
    s.classList.toggle("rec", rec);
    s.classList.toggle("on", on);
    s.title = rec ? "Recommended: the most tested path to a full decomp you can port and mod"
      : on ? "Tools installed for this platform" : "Tools for this platform aren't installed yet";
    if (rec && !s.querySelector(".rec-mark")) s.insertAdjacentHTML("afterbegin", `<span class="rec-mark">★</span>`);
  });
  $("platLegend").hidden = !any;
}

function renderHeader(ov) {
  const fork = ov.kind === "fork";
  const analysis = ov.kind === "analysis";
  const native = isNativeProject(ov);
  const detected = ov.detected;
  const chips = [`<span class="plat">${esc(ov.platform_name || "?")}</span>`];
  if (native && detected) {
    const why = detected.matchable ? "Detected from the binary" : `${detected.label} was detected in the binary; decomp.wtf matches C/C++, so this target can be inspected but not rebuilt yet`;
    chips.push(`<span class="chip-s${detected.matchable ? "" : " beta"}" title="${esc(why)}">${esc(detected.label)}</span>`);
  }
  if (native && ov.source_language && (!detected || !detected.matchable)) chips.push(`<span class="chip-s" title="The language agents write and the compiler checks">${esc(ov.source_language)}${detected && !detected.matchable ? " (matching)" : ""}</span>`);
  if (!analysis && !native && platformStatus(ov.platform) === "beta") chips.push(`<span class="chip-s beta" title="This platform works and is tested on homemade games, less so on real ones yet">beta</span>`);
  if (ov.rom && ov.rom.game_code) chips.push(`<span class="chip-s mono">${esc(ov.rom.game_code)}</span>`);
  if (fork) chips.push(`<span class="chip-s">${ov.origin === "scratch" ? `from scratch · ${esc(LAYOUT[ov.platform] || "decomp")}` : "public decomp"}</span>`);
  else if (!analysis && !native && ov.rom && ov.rom.region) chips.push(`<span class="chip-s">${esc(ov.rom.region)}</span>`);
  const comp = fork ? (ov.compiler_name ? cwName(ov.compiler_name) : "") : ov.compiler.name;
  if (comp) chips.push(`<span class="chip-s" title="${esc(fork || analysis ? "" : ov.compiler.flags.join(" "))}">${esc(comp)}</span>`);
  $("phChips").innerHTML = chips.join("");
  $("phTitle").textContent = ov.title;
  const pct = analysis ? (ov.functions ? (100 * ov.matched) / ov.functions : 0) : ov.bytes ? (100 * ov.matched_bytes) / ov.bytes : 0;
  $("phPct").textContent = pctNum(pct);
  if ($("phPct").nextSibling) $("phPct").nextSibling.textContent = analysis ? "% analyzed" : "% of code";
  $("phBar").style.width = pct + "%";
  $("phSub").textContent = fork && !ov.ready ? "set up the project to start"
    : analysis ? `${ov.matched.toLocaleString()} / ${ov.functions.toLocaleString()} work items analyzed · ${plural(ov.findings || 0, "finding")}`
    : `${ov.matched.toLocaleString()} / ${ov.functions.toLocaleString()} functions${linkedText(ov)} · ${plural(ov.attempts || 0, "compile")}${S.today ? ` · ${S.today} matched today` : ""}`;
  const w = workingAgents();
  $("phWorking").hidden = !w.n;
  $("phWorkingText").textContent = `${w.n} agent${w.n === 1 ? "" : "s"} working`;
  $("phStartText").textContent = w.running.length ? "Add agents" : "Start agents";
  $("phStart").hidden = fork && !ov.ready;
  const room = ov.room;
  const pill = $("phRoom");
  pill.hidden = !room;
  $("phTogether").hidden = analysis || !!room || (S.ov.kind === "fork" && !S.ov.ready);
  if (room) {
    const members = room.members || [];
    const online = members.filter((m) => m.online).length;
    pill.classList.toggle("off", room.role === "guest" && !room.connected);
    pill.innerHTML = room.role === "host" ? `<span class="dot ${online ? "on" : ""}"></span>Sharing · ${online} online`
      : /waiting for the host/i.test(room.error || "") ? `<span class="dot work"></span>Waiting to be let in`
      : `<span class="dot ${room.connected ? "on" : "err"}"></span>${room.syncing ? "Syncing the room's work…" : room.connected ? `In a room as ${esc(room.member)}`
        : room.failures ? `Room offline · retry in ${Math.ceil(room.retry_in)}s` : "Room offline"}`;
    $("teamTabCount").textContent = members.length ? members.length : "";
  } else $("teamTabCount").textContent = "";
  $("agTabCount").textContent = w.n || "";
}
$("phWorking").onclick = () => { location.hash = `#/p/${S.slug}/agents`; };
$("phRoom").onclick = () => { location.hash = `#/p/${S.slug}/team`; };
$("phTogether").onclick = () => openTogether();

/* Decompile together: open a room for this project in one step (a quick tunnel, an invite link); the Team tab has
   every option (an address of your own, approving members, who's in). */
function openTogether() {
  const ov = S.ov, fork = ov.kind === "fork";
  const body = dialog("Decompile together", `<p>Friends who own this game join from their own decomp.anything, and their agents work with yours: nobody does the same function twice, and every match is checked here, against your copy, before it counts.</p>
    <p class="muted small">${fork ? "They get this project's source tree from you and build it with their own copy of the game and their own compilers." : "Each of them brings their own copy, and decomp.anything checks it's exactly this game."} Only the work travels between you, never the game. You decide who's in.</p>
    <div class="field"><span>Who can join</span><div class="seg" id="tgVis"><button type="button" data-vis="private" class="on">Private: an invite link</button><button type="button" data-vis="public">Public: anyone with the address</button></div></div>
    <label class="check"><input type="checkbox" id="tgApprove"><span>I let each new member in (they wait until I say yes)</span></label>
    <div class="row"><button class="primary" id="tgGo" type="button">Open the room</button><a class="link small" href="#/p/${esc(S.slug)}/team">More options: your own address (LAN, Tailscale, a server)</a></div>
    <pre class="joblog" id="tgLog" hidden></pre><div id="tgDone"></div>`);
  body.querySelectorAll("[data-vis]").forEach((b) => { b.onclick = () => body.querySelectorAll("[data-vis]").forEach((x) => x.classList.toggle("on", x === b)); });
  body.querySelector("#tgGo").onclick = async (e) => {
    e.target.disabled = true;
    body.querySelector("#tgLog").hidden = false;
    try {
      const { job } = await api("/api/collab/start", { method: "POST", body: { slug: S.slug, mode: "tunnel",
        visibility: body.querySelector("[data-vis].on").dataset.vis, approve: body.querySelector("#tgApprove").checked } });
      const res = await followJob(job, body.querySelector("#tgLog"));
      body.querySelector("#tgLog").hidden = true;
      body.querySelector("#tgDone").innerHTML = `<p><b>The room is open.</b> Send this link to friends who own the game:</p>${cmdBox(res.link)}
        <p class="muted small">They open decomp.anything, choose <b>Join a team</b>, paste it and pick their own copy (or run <code>decomp join &lt;link&gt; &lt;their copy&gt; --name &lt;name&gt;</code>). The link works while this app is open; for a room that stays up, use your own address in the Team tab.</p>
        <div class="row"><a class="primary small-btn as-btn" href="#/p/${esc(S.slug)}/team">See who's in</a></div>`;
      wireCopies(body.querySelector("#tgDone"));
      S.ov = await api(`/api/project?slug=${S.slug}`);
    } catch (err) { toast(esc(err.message), "err"); e.target.disabled = false; }
  };
}
$("phFolder").onclick = () => openFolder();

/* Forks: how much of the game is built from linked C (decomp.dev's "linked"), next to what matches. */
function linkedText(ov) {
  const p = ov.kind === "fork" && ov.measures ? ov.measures.complete_code_percent : null;
  return p == null ? "" : ` · ${pctNum(Number(p))}% linked`;
}

function renderStats(ov) {
  renderHeader(ov);
  const analysis = ov.kind === "analysis";
  const pct = analysis ? (ov.functions ? (100 * ov.matched) / ov.functions : 0) : ov.bytes ? (100 * ov.matched_bytes) / ov.bytes : 0;
  $("pct").textContent = pctNum(pct);
  $("pctSub").textContent = analysis
    ? `${ov.matched.toLocaleString()} of ${ov.functions.toLocaleString()} work items analyzed · ${plural(ov.findings || 0, "finding")}`
    : (ov.bytes ? `${bytes(ov.matched_bytes)} of ${bytes(ov.bytes)}` : "") + linkedText(ov) + (S.today ? ` · ${S.today} matched today` : "");
  $("fnCount").textContent = `${ov.matched.toLocaleString()} / ${ov.functions.toLocaleString()}`;
  renderAutopilot(ov);
  renderSteering(ov);
  if (S.tab === "source") renderSource(ov);
  renderCompilerNotice(ov);
  renderAgentsKpi(ov);
  renderNext(ov);
}

function renderCompilerNotice(ov) {
  const n = $("compilerNotice");
  if (ov.kind === "analysis") {
    const t = (ov.analysis || {}).target || {};
    n.hidden = false;
    n.className = "notice";
    const what = (ov.analysis?.artifact || {}).entries ? "each file is a work item" : "agents claim its procedures";
    n.innerHTML = `<span><b>${esc(ov.platform_name || "Binary")}</b> · ${esc(t.path || "")} — REA reads this target; ${esc(what)} and agents record findings. No compiler is needed.</span>`;
    return;
  }
  const unc = ov.kind === "fork" && ov.ready ? ov.uncovered : null;
  if (unc && unc.functions) {
    n.hidden = false;
    n.className = "notice";
    n.innerHTML = `<span><b>${unc.functions.toLocaleString()} functions</b> are in code no source file covers yet, so agents can't decompile them. decomp.anything can give each stretch of code a file (checked against the game's checksum, committed to git).</span><span class="grow"></span><button class="primary small-btn" id="startFiles" type="button"${S.startingFiles ? " disabled" : ""}>${S.startingFiles ? "Splitting the game…" : "Give them source files"}</button>`;
    $("startFiles").onclick = async () => {
      S.startingFiles = true;
      renderCompilerNotice(ov);
      try {
        const { job } = await api("/api/fork/start-files", { method: "POST", body: { slug: S.slug } });
        const res = await followJob(job);
        toast(`${(res && res.functions || 0).toLocaleString()} more functions can be decompiled now.`, "match");
        await refreshMap();
      } catch (e) { toast(esc(e.message), "err"); }
      S.startingFiles = false;
    };
    return;
  }
  if (ov.kind === "fork" || ov.compiler.installed) { n.hidden = true; return; }
  n.hidden = false;
  if (ov.compiler.system) {
    n.className = "notice bad";
    const dl = ov.compiler.download
      ? ` <a href="${esc(ov.compiler.download)}" target="_blank" rel="noopener">Download ${esc(ov.compiler.toolchain.startsWith("clang-cl") ? "LLVM" : "MinGW-w64")}</a>.`
      : "";
    n.innerHTML = `<span><b>${esc(ov.compiler.name)}</b> is a compiler on this computer, not something decomp.anything downloads: ` +
      `<code>${esc(ov.compiler.command || "its command")}</code> isn't on PATH.${dl} Install it and add its bin folder to PATH, ` +
      `then reopen the app, or pick another compiler in this project's Settings.</span>`;
    return;
  }
  if (!ov.compiler.supported) {
    n.className = "notice bad";
    n.innerHTML = ov.compiler.why
      ? `<span><b>${esc(ov.compiler.name)}</b> has no build for this computer: ${esc(ov.compiler.why)}.</span>`
      : `<span><b>${esc(ov.compiler.name)}</b> has no build for this computer yet. Pick another compiler in this project's Settings.</span>`;
    return;
  }
  n.className = "notice";
  n.innerHTML = ov.compiler.proprietary
    ? `<span><b>${esc(ov.compiler.name)}</b> isn't installed. It's proprietary software (recompiled by the decomp community); install it only if you accept that.</span><span class="grow"></span><button class="primary small-btn" id="tcInstall" type="button">Install…</button>`
    : `<span><b>${esc(ov.compiler.name)}</b> isn't installed yet.${ov.compiler.wsl ? ` It's a Linux program: on Windows it runs inside WSL 2 (one-time setup: <code>wsl --install</code> in an administrator PowerShell, then restart and open Ubuntu once to finish — <a href="https://aka.ms/wslinstall" target="_blank" rel="noopener">guide</a>).` : ""}</span><span class="grow"></span><button class="primary small-btn" id="tcInstall" type="button">Install now</button>`;
  $("tcInstall").onclick = () => installToolchain(ov.compiler.toolchain, ov.compiler.proprietary);
}

function fnCounts() {
  const c = { matched: 0, working: 0, tried: 0, recon: 0, open: 0, asm: 0, library: 0, unmatchable: 0, hot: 0 };
  for (const f of S.map) { c[fnState(f)]++; if ((f.hits || 0) > 0) c.hot++; }
  return c;
}

/* The overview's numbers: function states, pace, who matched what, recent matches. */
let intakeReq = 0;
async function renderIntake(slug) {
  const box = $("intakePanel");
  if (!S.ov || S.ov.kind === "fork") { box.hidden = true; return; }
  const req = ++intakeReq;
  try {
    const report = await api(`/api/intake?slug=${encodeURIComponent(slug)}`);
    if (req !== intakeReq || S.slug !== slug || !report.markdown) return;
    box.hidden = false;
    $("intakeSummary").textContent = report.summary || "";
    $("intakeBody").textContent = report.markdown;
  } catch { if (req === intakeReq) box.hidden = true; }
}

function renderOverview() {
  if (!S.ov || S.tab !== "overview") return;
  const analysis = S.ov.kind === "analysis";
  const label = analysis ? "analyzed" : "matched";
  const c = fnCounts(), total = Math.max(1, S.map.length);
  const seg = (k, cls) => (c[k] ? `<i class="${cls}" data-w="${(100 * c[k]) / total}" title="${c[k]} ${k === "open" ? "to do" : k === "matched" ? label : k}"></i>` : "");
  $("fnStack").innerHTML = seg("matched", "s-match") + seg("recon", "s-recon") + seg("working", "s-work") + seg("tried", "s-tried") + seg("asm", "s-asm") + seg("unmatchable", "s-asm") + seg("library", "s-lib") + seg("open", "s-open");
  setWidths($("fnStack"));
  const togo = S.ov.functions - S.ov.matched;
  $("fnSub").textContent = `${togo.toLocaleString()} to go${c.recon ? ` · ${c.recon} reconstructed` : ""}${c.tried ? ` · ${c.tried.toLocaleString()} tried` : ""}${c.working ? ` · ${c.working} in progress` : ""}${c.asm ? ` · ${c.asm} assembly` : ""}${c.unmatchable ? ` · ${c.unmatchable} retired` : ""}${c.library ? ` · ${c.library} library` : ""}`;
  const st = S.stats;
  if (st) {
    const ph = st.matches_per_hour, max = Math.max(1, ...ph);
    const last3 = ph.slice(-3).reduce((a, b) => a + b, 0) / 3;
    $("paceNum").textContent = analysis ? `${st.compiles_last_hour}` : `${ph[23]}`;
    $("paceBars").innerHTML = ph.map((n, i) => `<rect x="${i * 10 + 1}" y="${28 - Math.max(n ? 3 : 1, (26 * n) / max)}" width="8" height="${Math.max(n ? 3 : 1, (26 * n) / max)}" rx="1.5" class="${n ? "on" : ""}"><title>${n} ${label} ${23 - i ? `${23 - i} h ago` : "this hour"}</title></rect>`).join("");
    $("paceSub").textContent = analysis
      ? `${ph[23]} analyzed in the last hour · ${plural(st.compiles_last_hour, "note")}`
      : `matched in the last hour · ${plural(st.compiles_last_hour, "compile")}` + (last3 > 0 && togo > 0 ? ` · ≈ ${duration(togo / last3)} to go at this pace` : "");
    $("board").innerHTML = st.agents.length ? st.agents.slice(0, 8).map((r) => `<li><span class="who">${esc(r.agent)}</span><span class="grow"></span>
        <span class="mono small">${r.matched ? `<b class="ok">${r.matched.toLocaleString()}</b> ${label} · ` : ""}${plural(r.compiles, analysis ? "note" : "compile")}</span></li>`).join("")
      : `<li class="muted small">Nobody yet. ${analysis ? "Findings" : "Matches"} by agents, the autopilot and you show up here.</li>`;
  }
  const recent = S.map.filter((f) => f.status === "matched" && f.matched_at).sort((a, b) => b.matched_at - a.matched_at).slice(0, 8);
  $("recentMatches").innerHTML = recent.length ? recent.map((f) => `<li><span class="ok">✓</span>${fnLink(f.name)}<span class="grow"></span><span class="muted small">${esc(f.matched_by || "")} · ${esc(since(f.matched_at))}</span></li>`).join("")
    : `<li class="muted small">${analysis ? "No work item analyzed yet." : "No matches yet."}</li>`;
  $("recentMatches").querySelectorAll(".fn").forEach((x) => { x.onclick = () => openFn(x.dataset.fn); });
}

function renderAgentsKpi(ov) {
  const w = workingAgents();
  $("agNum").textContent = w.n;
  const names = [...w.running.map((s) => s.label), ...w.outside];
  $("agSub").textContent = names.length ? names.slice(0, 4).join(", ") + (names.length > 4 ? ` +${names.length - 4}` : "") : "none working right now";
  const sp = ov.spend, n = ov.helper_matches || 0, budget = S.st?.settings?.budget_usd || 0;
  const unit = ov.kind === "analysis" ? "finding" : "match";
  $("spendSub").textContent = sp && sp.usd ? `${money(sp.usd)} spent${n ? ` · ${money(sp.usd / n)} per ${unit}` : ""}${budget ? ` · budget ${money(budget)}` : ""}`
    : sp && sp.tokens ? `${(sp.tokens / 1000).toFixed(0)}k tokens used` : budget ? `budget ${money(budget)}` : "";
}

/* What to do next, so a first project is plug and play. */
function renderNext(ov) {
  const box = $("nextStep");
  const w = workingAgents();
  const done = ov.functions > 0 && ov.matched >= ov.functions;
  if (ov.kind === "analysis") {
    if (done) {
      box.hidden = false;
      box.innerHTML = `<div class="next-text"><b>Every work item is analyzed.</b><span class="muted small">Read the findings together, download the report, or add a task to go deeper.</span></div>
        <a class="primary small-btn as-btn" href="#/p/${esc(S.slug)}/summary">View summary</a>
        <button class="ghost small-btn" id="nextTask" type="button">Add a task</button>`;
      $("nextTask").onclick = addAnalysisTask;
      return;
    }
    if (w.n || !S.st) { box.hidden = true; return; }
    launcherInit();
    const a = S.st.agents.find((x) => x.id === LZ.kind);
    box.hidden = false;
    box.innerHTML = `<div class="next-text"><b>Next: start your agents</b><span class="steps-line"><span class="ok">✓</span> Target opened: ${plural(ov.functions, "work item")}<span class="todo">○</span> Agents analyze in parallel</span></div>
      ${a && (a.installed || a.id === "api") ? `<button class="primary" id="nextGo" type="button">${esc(launchLabel(a, LZ.model, LZ.count))}</button>` : ""}
      <button class="ghost" id="nextPick" type="button">Choose agent &amp; model</button>`;
    const go = $("nextGo");
    if (go) go.onclick = () => a.id !== "api" ? startAgents(a, LZ.model, LZ.count, go)
      : (LZ.eps || []).length ? startAgents(a, "", LZ.count, go, { endpoints: LZ.eps }) : showLaunch();
    $("nextPick").onclick = () => showLaunch();
    return;
  }
  const compilerOk = ov.kind === "fork" || ov.compiler.installed;
  if (done) {
    box.hidden = false;
    box.innerHTML = `<div class="next-text"><b>Every function matches.</b><span class="muted small">Export the project: a source tree that rebuilds your game byte for byte.</span></div><a class="primary small-btn as-btn" href="#/p/${esc(S.slug)}/source">Export</a>`;
    return;
  }
  if (ov.kind === "fork" && ov.ready && !ov.functions) {
    box.hidden = false;
    box.innerHTML = `<div class="next-text"><b>Not measured yet</b><span class="muted small">objdiff compares every unit of the project with your game: it takes a moment.</span></div><button class="primary" id="nextMeasure" type="button">Measure now</button>`;
    $("nextMeasure").onclick = async () => {
      $("nextMeasure").disabled = true;
      toast("Measuring the whole project with objdiff…");
      try { const { job } = await api("/api/fork/check", { method: "POST", body: { slug: S.slug } }); await followJob(job); toast("Measured.", "match"); await refreshMap(); }
      catch (e) { toast(esc(e.message), "err"); $("nextMeasure").disabled = false; }
    };
    return;
  }
  if (w.n || !compilerOk || !S.st) { box.hidden = true; return; }
  launcherInit();
  const a = S.st.agents.find((x) => x.id === LZ.kind);
  const ap = ov.autopilot;
  const steps = [`<span class="ok">✓</span> ${ov.kind === "fork" ? "Project set up" : "Game analysed"}: ${plural(ov.functions, "function")}`];
  if (ap) steps.push(ap.state === "running" ? `<span class="spin-dot"></span> Autopilot trying machine drafts` : `<span class="ok">✓</span> Autopilot: ${ap.matched.toLocaleString()} free match${ap.matched === 1 ? "" : "es"}`);
  steps.push(`<span class="todo">○</span> Agents`, `<span class="todo">○</span> Export`);
  box.hidden = false;
  box.innerHTML = `<div class="next-text"><b>Next: start your agents</b><span class="steps-line">${steps.join("")}</span></div>
    ${a && (a.installed || a.id === "api") ? `<button class="primary" id="nextGo" type="button">${esc(launchLabel(a, LZ.model, LZ.count))}</button>` : ""}
    <button class="ghost" id="nextPick" type="button">Choose agent &amp; model</button>`;
  const go = $("nextGo");
  if (go) go.onclick = () => a.id !== "api" ? startAgents(a, LZ.model, LZ.count, go)
    : (LZ.eps || []).length ? startAgents(a, "", LZ.count, go, { endpoints: LZ.eps }) : showLaunch();
  $("nextPick").onclick = () => showLaunch();
}

