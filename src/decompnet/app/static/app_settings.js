/* exported showToolsPage, showSettingsPage -- both are route handlers called from app.js */
/* ------------------------------------------------------------------ project settings */
async function renderProjectSettings() {
  const ov = S.ov;
  if (!ov) return;
  const fork = ov.kind === "fork";
  const analysis = ov.kind === "analysis";
  $("compilerPanel").hidden = fork || analysis;
  $("factsPanel").hidden = fork || analysis;
  $("boundaryPanel").hidden = fork || analysis || !["windows", "linux"].includes(ov.platform);
  $("debugPanel").hidden = fork || analysis;
  $("comparePanel").hidden = fork || analysis || !["windows", "linux"].includes(ov.platform);
  $("tracePanel").hidden = analysis;
  $("traceMsg").textContent = ov.traced
    ? `${ov.traced.toLocaleString()} functions traced · ${(ov.hits || 0).toLocaleString()} hits`
    : "No runtime trace imported yet.";
  const dbg = ov.debug;
  $("debugMsg").textContent = dbg
    ? `${dbg.symbols || 0} names and ${dbg.types || 0} types from ${dbg.source}${dbg.imported_at ? ` · ${since(dbg.imported_at)}` : ""}`
    : "No debug information imported yet. A PDB beside a Windows program, DWARF in a Linux program, or a MAP file can name functions and provide types.";
  if (!fork && !analysis) {
    if (!S.st) await loadState();
    fillCompilers($("setTc"), ov.compiler.toolchain, { n64: "N64", psx: "PS1", ps2: "PS2", gba: "GBA" }[ov.platform]);
    $("setFlags").value = ov.compiler.flags.join(" ");
    const suggested = (ov.detected || {}).suggested_flags || [];
    const current = ov.compiler.flags || [];
    if (suggested.length && suggested.join(" ") !== current.join(" ")) {
      $("flagHint").innerHTML = `Detected build flags: <code>${esc(suggested.join(" "))}</code>
        <button class="ghost small-btn" id="flagApply" type="button">Use detected flags</button>
        <span class="muted">${esc(((ov.detected || {}).flag_evidence || []).join(" · "))}</span>`;
      $("flagApply").onclick = async () => {
        $("flagApply").disabled = true;
        try {
          S.ov = await api("/api/compiler", { method: "POST", body: { slug: S.slug, toolchain: ov.compiler.toolchain, flags: suggested } });
          renderProjectSettings();
          renderStats(S.ov);
          toast("Detected build flags applied.");
        } catch (e) { toast(esc(e.message), "err"); $("flagApply").disabled = false; }
      };
    } else if (suggested.length) {
      $("flagHint").textContent = "Flags match the detected build.";
    } else {
      $("flagHint").textContent = "";
    }
    const built = (ov.detected || {}).label || "";
    if (/MSVC/.test(built) && !/^(clang-cl|msvc)/.test(ov.compiler.toolchain || "")) {
      $("tcCheck").textContent = `${built} built this program. GCC-compatible code can't reproduce MSVC prologues `
        + `and register choices: “Check matches” with clang-cl or an installed cl.exe before switching.`;
    } else {
      $("tcCheck").textContent = "";
    }
  }
  const t = (ov.analysis || {}).target || {};
  const native = isNativeProject(ov);
  const detected = ov.detected;
  const mismatch = detected && !detected.matchable;
  const facts = analysis ? [
    ["Target", t.path], ["Format", t.format || "unknown"], ["Architecture", t.architecture || "unknown"],
    ["Provider", (ov.analysis || {}).provider || "automatic"], ["Work items", `${ov.functions.toLocaleString()}`],
    ["Analyzed", `${ov.matched.toLocaleString()}`], ["Findings", `${(ov.findings || 0).toLocaleString()}`],
    ["Folder", ov.folder],
  ] : [["Platform", ov.platform_name], [native ? "Program" : "Game", ov.rom?.title || ov.title],
    !fork && !analysis ? ["Source", `${ov.source_language || "C"}${ov.source_ext ? ` (${ov.source_ext})` : ""}`] : null,
    !fork && native && detected ? ["Built with", detected.label] : null,
    !fork && mismatch ? ["Matching", `C/C++ can't rebuild ${detected.language}`] : null,
    !fork && !native ? ["Game code", ov.rom?.game_code || ov.version] : null,
    !fork && !native && ov.rom?.region ? ["Region", ov.rom.region] : null,
    fork ? ["Layout", ov.origin === "scratch" ? (LAYOUT[ov.platform] || "decomp") + " (from scratch)" : ov.fork_kind] : ["Compiler", `${ov.compiler.name} ${ov.compiler.flags.join(" ")}`],
    fork && ov.repo_url ? ["Repository", ov.repo_url.replace(/^https?:\/\//, "")] : null,
    !fork && !analysis ? ["Data", `${(ov.data || 0).toLocaleString()} symbols`] : null,
    ["Functions", `${ov.functions.toLocaleString()} (${bytes(ov.bytes || 0)} of code)`], ["Folder", ov.folder]].filter(Boolean);
  $("projFacts").innerHTML = facts.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v || "—")}</dd></div>`).join("");
}
$("factsRecheck").onclick = async () => {
  $("factsRecheck").disabled = true;
  $("factsMsg").textContent = "Re-reading the target…";
  try {
    const r = await api("/api/project/recheck", { method: "POST", body: { slug: S.slug } });
    $("factsMsg").textContent = r.changed.length
      ? `${r.changed.join("; ")}. INTAKE.md and the agents' project notes are updated.`
      : `No facts changed: still ${r.detected.label || r.compiler.name}. INTAKE.md was refreshed.`;
    S.ov = await api(`/api/project?slug=${S.slug}`);
    await refreshMap(true);
    renderProjectSettings(S.ov);
    renderStats(S.ov);
    toast(r.changed.length ? `Re-checked: ${r.changed.length} fact${r.changed.length === 1 ? "" : "s"} updated.`
      : "No facts changed.", r.changed.length ? "match" : "");
  } catch (e) {
    $("factsMsg").textContent = e.message;
    toast(esc(e.message), "err");
  } finally { $("factsRecheck").disabled = false; }
};
$("boundaryFix").onclick = async () => {
  if (!confirm("Merge every open fragment into the function it belongs to? Matched functions are left alone.")) return;
  $("boundaryFix").disabled = true;
  $("boundaryMsg").textContent = "Merging fragments…";
  try {
    const r = await api("/api/boundaries/repair", { method: "POST", body: { slug: S.slug } });
    $("boundaryMsg").textContent = r.merged
      ? `Merged ${r.merged.toLocaleString()} fragment${r.merged === 1 ? "" : "s"}. ${r.open.toLocaleString()} open functions left.`
      : `No false boundaries left. ${r.open.toLocaleString()} open functions.`;
    S.ov = await api(`/api/project?slug=${S.slug}`);
    await refreshMap(true);
    renderProjectSettings(S.ov);
    renderStats(S.ov);
    toast(r.merged ? `Merged ${r.merged} fragment${r.merged === 1 ? "" : "s"}.` : "No false boundaries left.", "match");
  } catch (e) {
    $("boundaryMsg").textContent = e.message;
    toast(esc(e.message), "err");
  } finally { $("boundaryFix").disabled = false; }
};
$("setTcReset").onclick = () => { $("setFlags").value = ""; };
$("setTcCheck").onclick = async () => {
  const flags = $("setFlags").value.trim();
  $("setTcCheck").disabled = true;
  $("tcCheck").textContent = "Recompiling the functions that already match…";
  try {
    const r = await api("/api/compiler/preview", {
      method: "POST",
      body: { slug: S.slug, toolchain: $("setTc").value, flags: flags ? flags.split(/\s+/) : null },
    });
    if (!r.installed) {
      $("tcCheck").textContent = `${r.name} isn't installed on this computer yet: install it first, then check.`;
      return;
    }
    if (!r.checked) {
      $("tcCheck").textContent = `${r.name}: there are no matched functions to compare yet.`;
      return;
    }
    let text = `${r.name}: ${r.kept} of ${r.checked} matched function${r.checked === 1 ? "" : "s"} still match`;
    if (r.broke) text += `, ${r.broke} would break`;
    if (r.failed) text += `, ${r.failed} didn't compile`;
    text += `.`;
    if (r.broke_names.length) text += ` Breaking: ${r.broke_names.join(", ")}.`;
    if (r.failed_names.length) text += ` Not compiling: ${r.failed_names.join(", ")}.`;
    $("tcCheck").textContent = text;
  } catch (e) {
    $("tcCheck").textContent = e.message;
    toast(esc(e.message), "err");
  } finally { $("setTcCheck").disabled = false; }
};
$("setTcSave").onclick = async () => {
  try {
    const flags = $("setFlags").value.trim();
    S.ov = await api("/api/compiler", { method: "POST", body: { slug: S.slug, toolchain: $("setTc").value, flags: flags ? flags.split(/\s+/) : null } });
    renderStats(S.ov);
    renderProjectSettings();
    toast("Compiler settings saved.");
  } catch (e) { toast(esc(e.message), "err"); }
};
$("debugImport").onclick = async () => {
  const path = $("debugPath").value.trim();
  $("debugImport").disabled = true;
  $("debugLog").hidden = false;
  $("debugLog").textContent = "reading debug information…";
  $("debugMsg").textContent = "";
  try {
    const { job } = await api("/api/debug/import", { method: "POST", body: { slug: S.slug, path } });
    const r = await followJob(job, $("debugLog"));
    const split = r.split ? `, ${r.split} merged function${r.split === 1 ? "" : "s"} split at a symbol boundary` : "";
    $("debugMsg").textContent = r.warning
      ? `Imported ${r.source}: ${r.renamed} renamed${split}, ${r.symbols} new symbols. Types were not added: ${r.warning}`
      : `Imported ${r.source}: ${r.renamed} renamed${split}, ${r.symbols} new symbols, ${r.types} types${r.types_added ? " added to the header" : ""}.`;
    toast("Debug information imported.", "match");
    S.ov = await api(`/api/project?slug=${S.slug}`);
    await refreshMap(true);
    renderProjectSettings(S.ov);
    renderStats(S.ov);
  } catch (e) {
    $("debugMsg").textContent = e.message;
    toast(esc(e.message), "err");
  } finally { $("debugImport").disabled = false; }
};
$("debugExport").onclick = async () => {
  $("debugExport").disabled = true;
  try {
    const r = await api("/api/debug/export", { method: "POST", body: { slug: S.slug } });
    $("debugMsg").textContent = `Wrote ${r.symbols} symbols to ${r.symbol_addrs} and types to ${r.types_header}.`;
    toast("Symbols and types exported into the project folder.", "match");
  } catch (e) { toast(esc(e.message), "err"); }
  finally { $("debugExport").disabled = false; }
};
function compareTable(r) {
  const s = r.summary;
  const hex = (n) => n == null ? "—" : `0x${n.toString(16).toUpperCase()}`;
  const status = (x) => x === "same" ? `<span class="ok">same</span>` : x === "changed"
    ? `<span class="warn-text">changed</span>` : x === "added" ? `<span class="ok">added</span>` : `<span class="muted">removed</span>`;
  const rows = r.rows.slice(0, 300).map((x) => `<tr>
    <td class="name">${esc(x.name || "—")}<div class="mono dim">${hex(x.address)}</div></td>
    <td>${status(x.status)}</td>
    <td class="name">${esc(x.other_name || "—")}<div class="mono dim">${hex(x.other_address)}</div></td>
    <td class="r mono">${x.similarity ? `${Math.round(x.similarity * 100)}%` : ""}</td>
    <td class="r mono">${x.size == null ? "—" : x.size}${x.other_size == null ? "" : ` → ${x.other_size}`}</td>
  </tr>`).join("");
  return `<div class="facts-line" style="margin:10px 0"><b>${s.same}</b> same${s.moved ? ` (${s.moved} moved)` : ""} · <b>${s.changed}</b> changed · <b>${s.added}</b> added · <b>${s.removed}</b> removed</div>
    <div class="fn-table-wrap"><table class="fn-table"><thead><tr><th>This version</th><th>Status</th><th>Other version</th><th class="r">Similar</th><th class="r">Bytes</th></tr></thead><tbody>${rows || `<tr class="none"><td colspan="5">No functions found.</td></tr>`}</tbody></table></div>
    ${r.rows.length > 300 ? `<p class="muted small">Showing the first 300 of ${r.rows.length} rows.</p>` : ""}`;
}
$("traceImport").onclick = async () => {
  const path = $("tracePath").value.trim();
  if (!path) { toast("Choose a trace file first.", "err"); return; }
  $("traceImport").disabled = true;
  $("traceLog").hidden = false;
  $("traceLog").textContent = "reading trace…";
  try {
    const { job } = await api("/api/trace/import", { method: "POST", body: { slug: S.slug, path } });
    const r = await followJob(job, $("traceLog"));
    $("traceMsg").textContent = `${r.hits.toLocaleString()} hits in ${r.functions.toLocaleString()} functions` +
      (r.unmatched ? ` · ${r.unmatched} hits did not match a function` : "");
    toast("Runtime coverage imported.", "match");
    S.ov = await api(`/api/project?slug=${S.slug}`);
    await refreshMap(true);
    renderProjectSettings(S.ov);
  } catch (e) {
    $("traceMsg").textContent = e.message;
    toast(esc(e.message), "err");
  } finally { $("traceImport").disabled = false; }
};
$("compareGo").onclick = async () => {
  const path = $("comparePath").value.trim();
  if (!path) { toast("Choose another copy first.", "err"); return; }
  $("compareGo").disabled = true;
  $("compareMsg").textContent = "comparing instructions…";
  $("compareResult").innerHTML = "";
  try {
    const { job } = await api("/api/compare", { method: "POST", body: { slug: S.slug, path } });
    const r = await followJob(job);
    $("compareMsg").textContent = `${r.old.kind} ${r.old.arch} → ${r.new.kind} ${r.new.arch}`;
    $("compareResult").innerHTML = compareTable(r);
  } catch (e) {
    $("compareMsg").textContent = e.message;
    toast(esc(e.message), "err");
  } finally { $("compareGo").disabled = false; }
};
$("folderBtn").onclick = () => openFolder();
$("deleteBtn").onclick = () => {
  dialog("Remove project", `<p>Remove <b>${esc(S.ov.title)}</b> from decomp.anything? Matched functions and notes are deleted.</p>
    <label class="check"><input type="checkbox" id="delFiles"><span>Also delete the project folder (your copy of the target and exported sources in it)</span></label>
    <div class="row"><button class="primary" id="delGo" type="button">Remove</button><button class="ghost" data-close type="button">Cancel</button></div>`);
  $("delGo").onclick = async () => {
    try {
      await api("/api/delete", { method: "POST", body: { slug: S.slug, files: $("delFiles").checked } });
      closeDialog();
      loadNav();
      location.hash = "#/";
    } catch (e) { toast(esc(e.message), "err"); }
  };
};

/* ------------------------------------------------------------------ agents & models page */
async function showAgentsPage() {
  show("agentsPage");
  $("agList").innerHTML = `<p class="muted small">Looking for your agents…</p>`;
  await loadState();
  renderAgentsPage();
}
function renderAgentsPage() {
  const rows = S.st.agents.filter((a) => a.id !== "api");
  $("agList").innerHTML = rows.map((a) => `<div class="agent-row">
      <div class="ar-head"><b>${esc(a.name)}</b><span class="muted small">${esc(a.vendor)}</span><span class="grow"></span>
        ${a.installed ? `<span class="ok-mark">installed</span>` : `<button class="primary small-btn" data-inst="${esc(a.id)}" type="button" ${a.needs ? "disabled" : ""}>Install</button>`}</div>
      ${a.installed ? `<dl class="facts"><div><dt>Version</dt><dd>${esc(a.version || "?")}</dd></div><div><dt>Default model</dt><dd>${esc(a.default_model || "the app's own")}</dd></div><div><dt>Models offered</dt><dd>${(a.models || []).length ? (a.models || []).length : "any you type"}</dd></div></dl>
        ${(a.models || []).length ? `<div class="model-chips">${a.models.slice(0, 12).map((m) => `<span class="chip-s mono" title="${esc(m.note || "")}">${esc(m.name)}</span>`).join("")}${a.models.length > 12 ? `<span class="muted small">+${a.models.length - 12} more</span>` : ""}</div>` : ""}
        <p class="muted small">${esc(a.signin)}</p>`
      : `<p class="muted small">${a.needs ? `Needs ${esc(a.needs)} first. ` : ""}Or install it yourself:</p>${cmdBox(a.install)}`}
      <pre class="joblog" data-ilog="${esc(a.id)}" hidden></pre></div>`).join("");
  wireCopies($("agList"));
  $("agList").querySelectorAll("[data-inst]").forEach((b) => {
    b.onclick = async () => {
      const log = $("agList").querySelector(`[data-ilog="${b.dataset.inst}"]`);
      log.hidden = false; b.disabled = true;
      try {
        const { job } = await api("/api/agents/install", { method: "POST", body: { kind: b.dataset.inst } });
        await followJob(job, log);
        toast("Installed.", "match");
        showAgentsPage();
      } catch (e) { toast(esc(e.message), "err"); b.disabled = false; }
    };
  });
  const eps = S.st.settings.endpoints || [];
  const card = (e) => `<div class="ep-card" data-id="${esc(e.id || "")}">
      ${e.id ? `<div class="ar-head"><b>${esc(e.name)}</b><span class="muted small mono">${esc(e.model)} · ${esc(hostOf(e.base_url))}</span></div>` : `<div class="ar-head"><b>A new endpoint</b></div>`}
      <div class="lz-fields">${endpointFields(e)}</div>
      <div class="row"><button class="primary small-btn" data-save type="button">${e.id ? "Save" : "Add"}</button>
        ${e.has_key ? `<button class="ghost small-btn" data-forget type="button">Forget the key</button>` : ""}
        ${e.id ? `<button class="ghost small-btn danger" data-remove type="button">Remove</button>` : eps.length ? `<button class="ghost small-btn" data-cancel type="button">Cancel</button>` : ""}</div></div>`;
  $("apiBox").innerHTML = `<p class="muted small">Any OpenAI-compatible API runs through decomp.anything's built-in agent loop: DeepSeek, OpenRouter, OpenAI, or models on your own machines with llama.cpp, Ollama or LM Studio. Save one endpoint per machine or provider: when you start agents you tick the ones to use, and the sessions are spread across them. Each key stays on this computer and is only sent to its own endpoint.</p>
    ${eps.map(card).join("")}
    <div id="epNew" ${eps.length && !S.addingEndpoint ? "hidden" : ""}>${card({ provider: "custom" })}</div>
    ${eps.length && !S.addingEndpoint ? `<div class="row"><button class="ghost small-btn" id="epAdd" type="button">Add an endpoint</button></div>` : ""}
    <p class="muted small">Context size, for a local model: what you started it with (found by itself from llama.cpp). The agent keeps its conversation within it. Give it at least 16384 (llama.cpp: <code>-c 16384</code>; Ollama: <code>OLLAMA_CONTEXT_LENGTH=16384</code>, or it quietly cuts the start off). Sessions at once: for a machine that serves one request at a time, 1.</p>`;
  wireEndpointFields($("apiBox"));
  const post = async (body, said) => {
    try {
      S.st.settings = await api("/api/settings", { method: "POST", body });
      S.addingEndpoint = false;
      if (said) toast(said);
      renderAgentsPage();
    } catch (e) { toast(esc(e.message), "err"); }
  };
  $("apiBox").querySelectorAll(".ep-card").forEach((c) => {
    const id = c.dataset.id;
    c.querySelector("[data-save]").onclick = () => post({ endpoint: endpointData(c, id) }, id ? "Saved." : "Added. Tick it when you start agents (“API key”).");
    const forget = c.querySelector("[data-forget]"), remove = c.querySelector("[data-remove]"), cancel = c.querySelector("[data-cancel]");
    if (forget) forget.onclick = () => post({ endpoint: { id, clear_key: true } }, "The key is gone from this computer.");
    if (remove) remove.onclick = () => post({ remove_endpoint: id }, "Removed.");
    if (cancel) cancel.onclick = () => { S.addingEndpoint = false; renderAgentsPage(); };
  });
  if ($("epAdd")) $("epAdd").onclick = () => { S.addingEndpoint = true; renderAgentsPage(); };
  $("mcpBox").innerHTML = `<p class="muted small">Connect the agent you talk to, once. Then ask it in your terminal: <i>“decompile my game at ~/roms/game.z64”</i>. It imports it, opens this view and works with every tool here.</p>
    <div class="setup-group" id="mcpRows"><p class="muted small">Looking…</p></div>
    <p class="col-title">Any other MCP client (Cursor, Claude Desktop, Windsurf…)</p><pre class="code" id="mcpJson"></pre>
    <p class="col-title">A client that connects by URL (HTTP MCP)</p>
    <p class="muted small">It needs a token: a password for this app's tools, so keep it to yourself. Making a new one for the same name replaces the old one.</p>
    <div class="lz-row"><label class="field"><span>Name it goes by</span><input id="mcpName" class="mono" value="http-client"></label><button class="ghost small-btn" id="mcpToken" type="button">Make a token</button></div>
    <pre class="code" id="mcpHttp" hidden></pre>`;
  connectRows($("mcpRows")).then((r) => { if (r) $("mcpJson").textContent = JSON.stringify({ mcpServers: { decomp: { command: r.bridge[0], args: r.bridge.slice(1) } } }, null, 2); });
  $("mcpToken").onclick = async () => {
    try {
      const t = await api("/api/agents/token", { method: "POST", body: { name: $("mcpName").value.trim() || "http-client" } });
      $("mcpHttp").hidden = false;
      $("mcpHttp").textContent = JSON.stringify({ mcpServers: { decomp: { type: "http", url: t.url, headers: { Authorization: `Bearer ${t.token}` } } } }, null, 2);
    } catch (e) { toast(esc(e.message), "err"); }
  };
}
$("agRefresh").onclick = showAgentsPage;

/* ------------------------------------------------------------------ compilers & tools page */
// Compilers are grouped by the console they target. Everything else used to share one "Tools" tab, which is how a
// decompiler, an Android packager and a packet-capture reader ended up in the same list of thirty: the registry's
// `kind` field says "tool" for all of them. These sets say what each one is actually for.
//
// An id missing from TOOL_KIND falls into "Other" rather than disappearing, and "All" always shows everything, so
// adding a toolchain to the TOML can never make a card invisible.
// Order matters: the consoles first, then what the other tools are for. A group the data has but this list does
// not name still gets a tab (see renderTools), so a new toolchain can never become unreachable.
const TOOL_GROUPS = [["N64", "N64"], ["PS1", "PS1"], ["PS2", "PS2"], ["GBA", "GBA"], ["cw", "GameCube, Wii, DS"],
                     ["analysis", "Analysis"], ["build", "Decomp tooling"], ["android", "Android"],
                     ["captures", "Captures"], ["debug", "Debug"], ["service", "Services"]];
const TOOL_KIND = {};
const put = (kind, ids) => ids.forEach((id) => { TOOL_KIND[id] = kind; });
put("analysis", ["rea", "ghidra", "rizin", "m2c", "wabt", "detect-it-easy", "objdiff-cli", "luvdis", "dsd"]);
put("build", ["dtk", "dtk-template", "wibo", "coddog", "maspsx", "sjiswrap", "binutils-mips", "arm-gnu", "upx",
              "runtime-i386", "gc-wii-binutils"]);
put("android", ["jadx-mcp", "apktool", "adb"]);
put("captures", ["sigrok-cli", "tshark", "capinfos", "mitmproxy"]);
put("debug", ["gdb", "pwntools", "java", "llvm-tools"]);
put("service", ["cloudflared"]);
let toolFilter = "";
async function showToolsPage() {
  show("toolsPage");
  await loadState();
  renderTools();
}
function renderTools() {
  const all = S.st.toolchains;
  // A platform is the console it builds for, and it is the reliable signal: several registry entries carry a
  // platform without `kind = "compiler"` (gcc-2.7.2-kmc and both IDO versions among them), which is how they used
  // to end up in the one "Tools" list beside the Android tools. Everything else is grouped by purpose.
  const group = (t) => {
    if (/^mwcc/.test(t.id) || t.id === "gc-wii-binutils") return "cw";
    if (t.platform) return t.platform;
    return TOOL_KIND[t.id] || "other";
  };
  const counts = new Map();
  all.forEach((t) => counts.set(group(t), (counts.get(group(t)) || 0) + 1));
  const named = TOOL_GROUPS.map(([k]) => k);
  const tabs = [["", "All"],
    ...TOOL_GROUPS.filter(([k]) => counts.has(k)),
    // anything the data has that the list above does not name, under its own name, so nothing is unreachable
    ...[...counts.keys()].filter((k) => k && !named.includes(k) && k !== "other").sort().map((k) => [k, k]),
    ...(counts.has("other") ? [["other", "Other"]] : [])];
  if (toolFilter && !tabs.some(([k]) => k === toolFilter)) toolFilter = "";  // the active tab stopped existing
  const chips = document.createDocumentFragment();
  tabs.forEach(([k, label]) => {
    const b = document.createElement("button");
    b.className = `chip ${toolFilter === k ? "on" : ""}`;
    b.dataset.g = k;
    b.type = "button";
    b.append(label, " ");
    const n = document.createElement("span");
    n.textContent = String(k ? (counts.get(k) || 0) : all.length);
    b.append(n);
    b.onclick = () => { toolFilter = b.dataset.g; renderTools(); };
    chips.append(b);
  });
  $("toolChips").replaceChildren(chips);
  const rows = all.filter((t) => !toolFilter || group(t) === toolFilter);
  $("toolGrid").innerHTML = rows.map((t) => `<div class="tool-card ${t.installed ? "ok" : ""}">
      <div class="tc-top"><b>${esc(t.name)}</b>${t.license === "proprietary" ? `<span class="chip-s warn">proprietary</span>` : ""}${t.platform ? `<span class="chip-s">${esc(t.platform)}</span>` : ""}${t.wsl ? `<span class="chip-s" title="A Linux program: on Windows it runs inside WSL 2">WSL 2</span>` : ""}</div>
      <p class="muted small">${esc(t.use)}</p>
      <div class="row">${!t.supported ? `<span class="muted small">not available for this computer</span>` : t.installed ? `<span class="ok-mark">✓ installed</span>` : t.system ? `<span class="muted small">system package: <code>${esc(t.command || t.id)}</code> — ${t.id === "llvm-tools" ? "install LLVM" : "install it with your package manager"}${t.download ? ` · <a href="${esc(t.download)}" target="_blank" rel="noopener">Download</a>` : ""}</span>` : `<button class="ghost small-btn" data-tc="${esc(t.id)}" data-prop="${t.license === "proprietary" ? 1 : ""}" type="button">Install</button>`}</div></div>`).join("");
  $("toolGrid").querySelectorAll("[data-tc]").forEach((b) => { b.onclick = () => { b.disabled = true; installToolchain(b.dataset.tc, !!b.dataset.prop).finally(() => { b.disabled = false; }); }; });
}

/* ------------------------------------------------------------------ settings page */
async function showSettingsPage() {
  show("settingsPage");
  const st = await loadState();
  $("setAgents").innerHTML = `
    <label class="field"><span>Functions per agent session (a fresh session keeps the agent's context small)</span><input id="perSession" type="number" min="1" max="20" value="${st.settings.per_session}"></label>
    <label class="field"><span>Budget: stop helper agents when a project's agents have spent this much (US dollars, as the agents report it; 0 = no limit)</span><input id="budgetSet" type="number" min="0" step="0.5" value="${st.settings.budget_usd || 0}"></label>
    <label class="check"><input type="checkbox" id="autopilotSet" ${st.settings.autopilot !== false ? "checked" : ""}><span><b>Autopilot after each import</b>: try every function's machine draft first, so free matches cost no agent time.</span></label>`;
  const theme = savedTheme();
  $("setComputer").innerHTML = `
    <div class="field"><span>Appearance</span><div class="seg" id="themeSeg">${[["", "System"], ["light", "Light"], ["dark", "Dark"]].map(([k, l]) => `<button type="button" data-theme="${k}" class="${theme === k ? "on" : ""}">${l}</button>`).join("")}</div></div>
    <div class="field"><span>Start with this computer (keeps team rooms open, resumes agents)</span><div class="row" id="svcRow2"><span class="muted small">Checking…</span></div></div>
    <div class="field"><span>Projects live in</span>${cmdBox(st.home)}</div>`;
  const where = { wsl: " (in its own WSL 2 Linux)", docker: " (in Docker)" }[st.runtime] || "";
  const inst = st.install || {};
  const from = inst.channel === "branch" ? `the ${esc(inst.branch)} branch @ <code>${esc((inst.commit || "?").slice(0, 7))}</code>` : "this repository's release feed";
  $("setAbout").innerHTML = `<p class="small">decomp.anything ${esc(st.version)} on ${esc(st.platform)}${esc(where)}. Free and open source (MIT). Everything runs on this computer: no account, nothing sent to us.</p>
    <div class="field"><span>Updates come from ${from}</span><div class="row"><span class="muted small grow" id="upText">Checking…</span><button class="ghost small-btn" id="upCheck" type="button">Check now</button></div></div>
    <div class="field"><span>REA toolset updates come from the rea-agents package on npm</span><div class="row"><span class="muted small grow" id="reaUpText">Checking…</span><button class="primary small-btn" id="reaUpGo" type="button" hidden>Update REA</button></div></div>
    <label class="check"><input type="checkbox" id="checkUpdateSet" ${st.settings.check_updates ? "checked" : ""}><span><b>Look for updates</b> now and then (GitHub for decomp.anything and npm for managed REA; nothing about you or your projects is sent).</span></label>
    <label class="check"><input type="checkbox" id="autoUpdateSet" ${st.settings.auto_update ? "checked" : ""}><span><b>Install updates by itself</b> when no helper agent is working (it restarts in a few seconds; agents carry on).</span></label>
    <label class="check"><input type="checkbox" id="autoReaUpdateSet" ${st.settings.auto_update_rea ? "checked" : ""}><span><b>Install REA updates automatically</b> when helpers and background jobs are idle (restarts decomp.anything to refresh connections).</span></label>
    <div class="row"><a class="link small" href="https://decomp.wtf" target="_blank" rel="noopener">decomp.wtf (upstream)</a><a class="link small" href="https://github.com/giveen/decomp.anything" target="_blank" rel="noopener">Source code</a></div>`;
  const showUpdate = (u) => {
    if (!$("upText") || !u) return;
    $("upText").textContent = u.error ? u.error : u.available ? `New: ${updateName(u)}` : u.disabled ? "Not looking for updates." : "Up to date.";
    const r = u.rea || {}, rc = r.current || {};
    $("reaUpText").textContent = r.error || r.why_not || (r.disabled ? "Not looking for REA updates." : r.available ? `REA ${r.latest.version} is ready (current: ${rc.version || "unknown"}).` : `REA ${rc.version || "version unknown"} · ${rc.source || "not configured"}`);
    $("reaUpGo").hidden = !r.available;
    $("reaUpGo").disabled = !r.can_apply;
    $("reaUpGo").onclick = offerReaUpdate;
    if (u.available && !$("upGo2")) {
      const b = document.createElement("button");
      b.className = "primary small-btn"; b.id = "upGo2"; b.type = "button"; b.textContent = "Update now";
      b.onclick = offerUpdate;
      $("upCheck").before(b);
    }
  };
  checkUpdate(false, false).then(showUpdate);
  $("upCheck").onclick = async () => { $("upText").textContent = "Checking…"; showUpdate(await checkUpdate(true, false)); };
  $("checkUpdateSet").onchange = () => save({ check_updates: $("checkUpdateSet").checked }, $("checkUpdateSet").checked ? "decomp.anything looks for updates." : "decomp.anything won't look for updates (Check now still does).");
  $("autoUpdateSet").onchange = () => save({ auto_update: $("autoUpdateSet").checked }, $("autoUpdateSet").checked ? "decomp.anything installs updates by itself." : "decomp.anything asks before updating.");
  $("autoReaUpdateSet").onchange = () => save({ auto_update_rea: $("autoReaUpdateSet").checked }, "REA update preference saved.");
  wireCopies($("setComputer"));
  serviceRow($("svcRow2"), "decomp.anything runs while you have it open.");
  $("themeSeg").querySelectorAll("[data-theme]").forEach((b) => { b.onclick = () => { setTheme(b.dataset.theme); $("themeSeg").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b)); }; });
  const save = (body, msg) => api("/api/settings", { method: "POST", body }).then((s) => { S.st.settings = s; toast(msg); }).catch((e) => toast(esc(e.message), "err"));
  $("perSession").onchange = () => save({ per_session: Number($("perSession").value) }, "Saved.");
  $("autopilotSet").onchange = () => save({ autopilot: $("autopilotSet").checked }, "Saved.");
  $("budgetSet").onchange = () => save({ budget_usd: Number($("budgetSet").value) || 0 }, "Budget saved.");
}

/* ------------------------------------------------------------------ updates (app/updates.py)
   The app checks now and then; when there's something newer, it asks once (per version) and installs it with one
   click: it restarts in place, and this tab reloads itself once the new version answers. */
const UPDATE_SKIP = "decomp-wtf-skip-update";
let updateInfo = null;
const updateTarget = (u) => (u && u.latest ? u.latest.commit || u.latest.version || "" : "");
function updateName(u) {
  const l = u.latest || {}, c = u.current || {};
  if (c.channel === "branch") return `${c.branch} @ ${(l.commit || "").slice(0, 7)}${l.version && l.version !== c.version ? ` (${l.version})` : ""}`;
  return `decomp.anything ${l.version}`;
}
async function checkUpdate(refresh = false, ask = true) {
  try { updateInfo = await api(`/api/update${refresh ? "?refresh=1" : ""}`); } catch { return null; }
  $("navUpdate").hidden = !updateInfo.available && !updateInfo.rea?.available;
  const job = updateInfo.job || {};
  if (job.state === "installing" || job.state === "restarting") { showUpdating(); return updateInfo; }
  const rjob = updateInfo.rea?.job || {};
  if (rjob.state === "installing" || rjob.state === "restarting") { showUpdating("rea"); return updateInfo; }
  let skipped = "";
  let skippedRea = "";
  try { skipped = localStorage.getItem(UPDATE_SKIP) || ""; } catch { /* storage off */ }
  try { skippedRea = localStorage.getItem(`${UPDATE_SKIP}:rea`) || ""; } catch { /* storage off */ }
  if (ask && updateInfo.available && skipped !== updateTarget(updateInfo) && $("dialog").hidden && $("drawer").hidden) offerUpdate();
  else if (ask && updateInfo.rea?.available && skippedRea !== updateTarget(updateInfo.rea) && $("dialog").hidden && $("drawer").hidden) offerReaUpdate();
  return updateInfo;
}
function offerUpdate() {
  const u = updateInfo;
  if (!u) return;
  const c = u.current || {};
  const notes = (u.notes || []).map((n) => `<li>${esc(n)}</li>`).join("");
  const body = dialog("A new decomp.anything is ready", `<p>${c.channel === "branch"
      ? `New on the <b>${esc(c.branch)}</b> branch: <code>${esc((u.latest.commit || "").slice(0, 7))}</code> (you have <code>${esc((c.commit || "?").slice(0, 7))}</code>).`
      : `decomp.anything <b>${esc(u.latest.version)}</b> is out. You have ${esc(c.version)}.`}</p>
    ${notes ? `<ul class="up-notes">${notes}</ul>` : ""}
    ${u.link ? `<p class="small"><a class="link" href="${esc(u.link)}" target="_blank" rel="noopener">Every change</a></p>` : ""}
    <p class="muted small">${u.can_apply ? "One click: decomp.anything installs it and restarts by itself, and this tab follows along. Helper agents pause for a moment and carry on." : esc(u.why_not)}</p>
    <div class="row"><button class="primary" id="upGo" type="button" ${u.can_apply ? "" : "disabled"}>Update now</button><button class="ghost" id="upLater" type="button">Later</button></div>`);
  body.querySelector("#upLater").onclick = () => {
    try { localStorage.setItem(UPDATE_SKIP, updateTarget(u)); } catch { /* storage off */ }
    closeAll();
  };
  body.querySelector("#upGo").onclick = async (e) => {
    e.target.disabled = true;
    try { await api("/api/update", { method: "POST", body: { apply: true } }); } catch (err) { e.target.disabled = false; return toast(esc(err.message), "err"); }
    showUpdating();
  };
}
function offerReaUpdate() {
  const u = updateInfo?.rea;
  if (!u?.available) return;
  const body = dialog("A new REA toolset is ready", `<p>REA <b>${esc(u.latest.version)}</b> is available. Current version: ${esc(u.current?.version || "unknown")}.</p>
    ${(u.notes || []).map((n) => `<p class="small">${esc(n)}</p>`).join("")}
    <p>The update is installed separately and its MCP tool catalog is checked before use. decomp.anything then restarts to refresh agent connections. Helpers resume afterwards.</p>
    <p class="muted small">${u.can_apply ? "Your provider settings are kept. This updates REA; provider tools keep their own versions." : esc(u.why_not)}</p>
    <p><a class="link" href="https://www.npmjs.com/package/rea-agents" target="_blank" rel="noopener">REA releases</a></p>
    <div class="row"><button class="primary" id="reaGo" type="button" ${u.can_apply ? "" : "disabled"}>Update REA now</button><button class="ghost" id="reaLater" type="button">Later</button></div>`);
  body.querySelector("#reaLater").onclick = () => {
    try { localStorage.setItem(`${UPDATE_SKIP}:rea`, updateTarget(u)); } catch { /* storage off */ }
    closeAll();
  };
  body.querySelector("#reaGo").onclick = async (e) => {
    e.target.disabled = true;
    try { await api("/api/update", { method: "POST", body: { target: "rea", apply: true } }); }
    catch (err) { e.target.disabled = false; return toast(esc(err.message), "err"); }
    showUpdating("rea");
  };
}
async function showUpdating(target = "app") {
  if (S.updating) return;
  S.updating = true;
  const reaVersion = updateInfo?.rea?.job?.version || updateInfo?.rea?.latest?.version;
  const was = { version: S.st ? S.st.version : "", build: S.st ? S.st.build || "" : "" };
  dialog(target === "rea" ? "Updating REA" : "Updating decomp.anything", `<p id="upStep">Installing the update…</p><pre class="joblog tall" id="upLog"></pre>
    <p class="muted small">This tab reloads by itself once the new version is running.</p>`);
  let away = 0;
  for (;;) {
    await sleep(1000);
    let u = null;
    try { u = await api("/api/update"); away = 0; } catch { away++; }
    const job = (target === "rea" ? u?.rea?.job : u?.job) || {};
    if (target === "rea" && job.state === "idle" && u?.rea?.current?.version === reaVersion) { location.reload(); return; }
    if (u && $("upLog")) { $("upLog").textContent = (job.log || []).join("\n"); $("upLog").scrollTop = $("upLog").scrollHeight; }
    if (job.state === "error") {
      S.updating = false;
      if ($("upStep")) $("upStep").textContent = `The update didn't go through: ${job.error}`;
      return;
    }
    if (job.state === "restarting" && $("upStep")) $("upStep").textContent = "Restarting…";
    try {
      const h = await (await fetch("/health", { cache: "no-store" })).json();
      if (h.version !== was.version || (h.build || "") !== was.build) { location.reload(); return; }
    } catch { /* not back yet */ }
    if (away > 180) {
      S.updating = false;
      if ($("upStep")) $("upStep").textContent = "decomp.anything didn't come back after the update. Start it again (decomp.anything in your apps, or the decomp command).";
      return;
    }
  }
}
$("navUpdate").onclick = () => (updateInfo?.available ? offerUpdate() : updateInfo?.rea?.available ? offerReaUpdate() : checkUpdate(true, true));

$("quitBtn").onclick = async () => {
  if (!confirm("Quit decomp.anything? Running agents stop.")) return;
  await api("/api/quit", { method: "POST" }).catch(() => {});
  document.body.innerHTML = `<main class="view"><p class="muted">decomp.anything has stopped. You can close this tab.</p></main>`;
};

loadNav();
applyTheme(savedTheme());  // the saved choice survives a reload (it used to apply only until the page was refreshed)
route();
setTimeout(() => checkUpdate(false, true), 4000);
setInterval(() => checkUpdate(false, true), 10 * 60 * 1000);
