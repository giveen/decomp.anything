/* ------------------------------------------------------------------ inspector */
function highlightAsm(text) {
  return text.split("\n").map((line) => {
    if (/^\s*(glabel|endlabel)/.test(line)) return `<span class="cm">${esc(line)}</span>`;
    if (/^\.L[0-9A-F]+:/.test(line)) return `<span class="lbl">${esc(line)}</span>`;
    const m = line.match(/^(\/\* [^*]+ \*\/)(\s+)(\S+)(.*)$/);
    if (!m) return esc(line);
    const addr = m[1].split(" ")[2] || "";
    return `<span class="addr">${esc(addr)}</span>  <span class="op">${esc(m[3].padEnd(8))}</span> ${esc(m[4].trimStart()).replace(/(\.L[0-9A-F]{8})/g, '<span class="lbl">$1</span>')}`;
  }).join("\n");
}
function highlightC(text) {
  const kw = /\b(void|int|char|short|long|float|double|unsigned|signed|struct|union|enum|typedef|static|extern|const|volatile|return|if|else|for|while|do|switch|case|default|break|continue|goto|sizeof|s8|u8|s16|u16|s32|u32|s64|u64|f32|f64)\b/g;
  return esc(text).split("\n").map((line) => {
    if (/^\s*(\/\*|\*|\/\/)/.test(line)) return `<span class="cm">${line}</span>`;
    return line.replace(kw, '<span class="kw">$1</span>').replace(/\b(0x[0-9A-Fa-f]+|\d+)\b/g, '<span class="num">$1</span>');
  }).join("\n");
}

/* The open function lives in the URL, so a refresh keeps it open and a teammate can be sent a link to it. */
function setFnUrl(name) {
  if (!S.slug || !S.ov || S.ov.kind === "analysis") return;
  const url = `#/p/${S.slug}/${S.tab}/${encodeURIComponent(name)}`;
  if (location.hash !== url) history.replaceState(null, "", url);
}
function resetFnUrl() {
  if (!S.slug || !S.ov || S.ov.kind === "analysis") return;
  const url = `#/p/${S.slug}/${S.tab}`;
  if (location.hash !== url) history.replaceState(null, "", url);
}

async function openFn(name) {
  if (S.ov && S.ov.kind === "analysis") {
    const f = S.map.find((x) => x.name === name);
    if (f && (f.kind === "recovered-source" || f.kind === "reconstruction") && f.path) {
      openSource(f.path);  // the code this entry handed back: read it where it lives
      return;
    }
    if (f && f.kind === "artifact-entry") {
      if (f.child) { location.hash = `#/p/${f.child}`; return; }
      if (f.path) CT.pendingSelect = { slug: S.slug, path: f.path };
      const overview = `#/p/${S.slug}/overview`;
      if (location.hash === overview) renderContents(); else location.hash = overview;
      return;
    }
    const t = (S.ov.analysis || {}).target || {};
    if (!t.path) return;
    const fn = f && f.vram ? `&fn=0x${f.vram.toString(16)}` : "";
    try { sessionStorage.setItem("decomp-return", JSON.stringify({ slug: S.slug, title: S.ov.title || S.slug })); } catch { /* storage off */ }
    location.hash = `#/console?path=${encodeURIComponent(t.path)}${fn}`;
    return;
  }
  const again = S.fn === name && !$("drawer").hidden;
  if (!again) { giveBack(name); S.editDirty = false; $("tryMsg").hidden = true; }
  S.fn = name;
  openModal("drawer", !again);
  setFnUrl(name);
  if (!again) { S.ftab = null; S.which = "latest"; S.diffKey = null; $("diff").innerHTML = ""; $("diffSum").textContent = ""; }
  drawMap();
  document.querySelectorAll("#fnRows tr").forEach((tr) => tr.classList.toggle("sel", tr.dataset.fn === name));
  await refreshFn(true);
}

function fnBadge(d = S.fnDetail) {
  if (!d) return;
  const working = S.claims.find((c) => c.name === d.name);
  const st = $("fnStatus");
  st.className = "badge " + (d.status === "matched" ? "matched" : working ? "working" : "");
  st.textContent = d.status === "matched" ? "matched" : working ? `${working.agent} working` : d.best != null ? `best ${pctText(pctOf(d.best, d.insns))}` : "not started";
}

function showFnTab(tab) {
  document.querySelectorAll("[data-ftab]").forEach((b) => b.classList.toggle("on", b.dataset.ftab === tab));
  $("fnDiffView").hidden = tab !== "diff";
  $("fnCodeView").hidden = tab !== "code";
  if (tab === "diff") loadDiff();
}
document.querySelectorAll("[data-ftab]").forEach((b) => { b.onclick = () => { S.ftab = b.dataset.ftab; showFnTab(S.ftab); }; });
document.querySelectorAll("#diffWhich [data-which]").forEach((b) => {
  b.onclick = () => {
    S.which = b.dataset.which;
    document.querySelectorAll("#diffWhich [data-which]").forEach((x) => x.classList.toggle("on", x === b));
    loadDiff();
  };
});

$("fnConsole").onclick = () => {
  const d = S.fnDetail;
  if (!d || !S.ov.binary) return;
  const path = `${S.ov.folder.replace(/[\\/]$/, "")}/${S.ov.binary}`;
  location.hash = `#/console?path=${encodeURIComponent(path)}&fn=0x${d.vram.toString(16)}`;
};

function renderPrefill(d) {
  const box = $("fnPrefill");
  // a live refresh mid-job would wipe the progress log out of the drawer: keep the running one on screen
  if (prefillBusy && box.querySelector("#prefillLog")) return;
  const pf = d.prefill;
  const items = pf && Array.isArray(pf.items) ? pf.items : [];
  if (!items.length) {
    if (!d.prefillable) { box.innerHTML = ""; return; }
    box.innerHTML = `<p class="col-title">Starting functions</p>
      <p class="muted small">No starting functions cached yet. REA can decompile a starting batch for this item now.</p>
      <div class="row"><button class="ghost small-btn" id="prefillGo" type="button">Decompile starting functions</button></div>
      <pre class="joblog" id="prefillLog" hidden></pre>`;
    $("prefillGo").onclick = () => runPrefill(d.name);
    return;
  }
  const label = (it) => (it.name && it.name !== it.address
    ? `${esc(it.name)} <span class="muted">${esc(it.address || "")}</span>`
    : esc(it.address || it.name || "function"));
  box.innerHTML = `<p class="col-title">${esc(pf.heading || "Starting functions")}</p>`
    + (pf.note ? `<p class="muted small">${esc(pf.note)}</p>` : "")
    + items.map((it, i) => (it.error != null
      ? `<div class="err">${label(it)}: ${esc(it.error || "REA couldn't decompile it")}</div>`
      : `<details${i === 0 ? " open" : ""}><summary class="mono">${label(it)}</summary><pre class="code c">${highlightC(it.code || "")}</pre></details>`)).join("")
    + (pf.more ? `<p class="muted small">${pf.more} more function${pf.more === 1 ? "" : "s"} were decompiled; ask an agent or the RE console for them.</p>` : "");
}

let prefillBusy = false;

async function runPrefill(name) {
  const btn = $("prefillGo"), log = $("prefillLog");
  prefillBusy = true;
  if (btn) { btn.disabled = true; btn.textContent = "Decompiling…"; }
  if (log) { log.hidden = false; log.textContent = "Starting…"; }
  try {
    const started = await api("/api/analysis/prefill", { method: "POST", body: { slug: S.slug, name } });
    const res = await followJob(started.job, log);
    prefillBusy = false;
    if (S.fn !== name) return;
    S.fnDetail = { ...S.fnDetail, prefill: res.prefill };
    renderPrefill(S.fnDetail);
    toast("Starting functions decompiled.", "match");
  } catch (e) {
    prefillBusy = false;
    if (log) log.textContent += `\n✕ ${e.message}`;
    if (btn) { btn.disabled = false; btn.textContent = "Decompile starting functions"; }
    toast(esc(e.message), "err");
  }
}

let fnBusy = false;
async function refreshFn(first) {
  if (fnBusy || !S.fn) return;
  fnBusy = true;
  try {
    const d = await api(`/api/function?slug=${S.slug}&name=${encodeURIComponent(S.fn)}`);
    if (d.name !== S.fn) return;
    S.fnDetail = d;
    $("fnAddr").textContent = S.ov.kind === "fork"
      ? `${d.unit} · ${d.insns} instructions${d.match_percent != null ? ` · ${d.match_percent >= 100 ? "100" : (Math.floor(d.match_percent * 10) / 10).toFixed(1)}% now` : ""}`
      : `${hex8(d.vram)} · ${isNativeProject(S.ov) ? "file" : "ROM"} 0x${d.rom_offset.toString(16).toUpperCase()} · ${d.insns} instructions`;
    $("fnName").textContent = d.name;
    $("fnConsole").hidden = !S.ov.binary;  // PC programs: REA reads the program itself
    fnBadge(d);
    const pinned = (S.ov.priority || []).includes(d.name);
    $("fnPin").hidden = S.ov.kind === "fork" || d.status !== "open";
    $("fnPin").classList.toggle("on", pinned);
    $("fnPin").textContent = pinned ? "★ Next (on)" : "★ Next";
    $("fnPin").onclick = () => togglePrio(d.name, !pinned).then(() => refreshFn(false));
    $("noteAdd").onclick = async () => {
      const text = $("noteText").value.trim();
      if (!text) return;
      try {
        await api("/api/note", { method: "POST", body: { slug: S.slug, function: d.name, text } });
        $("noteText").value = "";
        toast("Note saved: the next agent on this function reads it.");
        refreshFn(false);
      } catch (e) { toast(esc(e.message), "err"); }
    };
    $("fnAsm").innerHTML = highlightAsm(d.asm);
    const lang = S.ov.kind === "analysis" ? "" : (S.ov.source_language || "C");
    const kinds = { matched: lang ? `Matching ${lang}` : "Matching source", reconstructed: `Reconstructed ${lang || "code"} (unverified)`, best: "Best attempt so far", latest: S.ov.kind === "fork" ? "In the source now" : "Latest attempt", draft: "Machine draft (m2c)" };
    $("fnSrcKind").textContent = kinds[d.source_kind] || (lang ? `No ${lang} yet` : "No source yet");
    if (!S.editDirty || first) setEditor(d.source || "", d);
    $("copySrc").onclick = () => copyText($("fnEdit").value);
    renderFnChart(d);
    $("diffWhich").hidden = d.status === "matched" || S.ov.kind === "fork" || d.scores.filter((x) => x.score != null).length < 2;
    const xr = [];
    if (d.calls.length) xr.push(`<div><b>calls</b>${d.calls.map(fnLink).join("")}</div>`);
    if (d.called_by.length) xr.push(`<div><b>called by</b>${d.called_by.map(fnLink).join("")}</div>`);
    $("fnXref").innerHTML = xr.join("");
    $("fnXref").querySelectorAll(".fn").forEach((x) => { x.onclick = () => openFn(x.dataset.fn.split("+")[0]); });
    renderPrefill(d);
    $("fnNotes").innerHTML = d.notes.length ? `<p class="col-title">Notes from agents</p>` + d.notes.map((n) => `<div class="note"><small>${esc(n.agent)} · ${clock(n.created_at)}</small>${esc(n.text)}</div>`).join("") : "";
    renderMatrix(d);
    const tab = S.ftab || (d.status === "matched" || !d.scores.length ? "code" : "diff");
    showFnTab(tab);
  } catch (e) {
    if (first) toast(esc(e.message), "err");
  } finally { fnBusy = false; }
}

/* The compiler matrix (forks whose project declares one): the function's C under every compiler profile and
   translation-unit context, as a table of differing instructions, with the runs kept per function. */
let matrixBusy = false;
function matrixCell(c) {
  if (!c) return `<td class="muted">·</td>`;
  if (c.matched) return `<td class="ok" title="byte-identical">MATCH</td>`;
  if (!c.compiled) return `<td class="muted" title="${esc(c.error || "didn't compile")}">error</td>`;
  if (c.diffs == null) return `<td class="muted" title="${esc(c.error || "")}">-</td>`;
  return `<td title="${esc(c.kinds || "")}${c.percent != null ? ` · ${(Math.floor(c.percent * 10) / 10).toFixed(1)}%` : ""}">${c.diffs}</td>`;
}
function matrixTable(run) {
  const order = ["file", "alone", "neighbors", "extern", "tentative"];
  const ctxs = order.filter((c) => run.cells.some((x) => x.context === c));
  const profs = [...new Set(run.cells.map((x) => x.profile))];
  const at = new Map(run.cells.map((x) => [`${x.profile}|${x.context}`, x]));
  const current = (run.cells.find((x) => x.current) || {}).profile;
  const head = `<tr><th></th>${ctxs.map((c) => `<th>${esc(c)}</th>`).join("")}</tr>`;
  const rows = profs.map((p) => `<tr><th title="${esc((at.get(`${p}|${ctxs[0]}`) || {}).flags || "")}">${esc(p)}${p === current ? " *" : ""}</th>${ctxs.map((c) => matrixCell(at.get(`${p}|${c}`))).join("")}</tr>`).join("");
  return `<small class="muted">${esc(run.agent || "")} · ${clock(run.at)} · source ${esc((run.source_hash || "").slice(0, 8))}</small><table class="matrix-table">${head}${rows}</table>`;
}
function renderMatrix(d) {
  const box = $("fnMatrix");
  if (!box) return;
  const m = d.matrix;
  if (!m || !m.configured) { box.innerHTML = ""; return; }
  const saved = m.saved && d.status !== "matched"
    ? `<div class="note"><span class="ok">▦</span> Matches under <b>${esc(m.saved.profile)}</b> / <b>${esc(m.saved.context)}</b>: the C is right, the project's build for this file has to change.</div>` : "";
  const runs = (m.runs || []).map(matrixTable).join("");
  box.innerHTML = `<p class="col-title">Compiler matrix<button class="link-btn" id="matrixRun" type="button">${matrixBusy ? "Running…" : "Run"}</button><button class="link-btn" id="matrixSweep" type="button" title="Every open function that has C, closest first; sources already tried are skipped">Run on all</button></p>${saved}${runs || `<p class="muted small">Not tried yet: differing instructions per compiler profile (rows) and translation-unit context (columns).</p>`}${runs ? `<p class="muted small">Cells: differing instructions. * the file's own profile.</p>` : ""}`;
  $("matrixRun").onclick = async () => {
    if (matrixBusy) return;
    matrixBusy = true;
    $("matrixRun").textContent = "Running…";
    try {
      const dirty = S.editDirty ? $("fnEdit").value : "";
      const r = await api("/api/matrix/run", { method: "POST", body: { slug: S.slug, name: d.name, source: dirty, again: true } });
      toast(r.committed ? "Matched and committed." : r.matched ? "Matches under another profile or translation unit: saved with its recipe." : "No profile or context matches yet.", r.matched ? undefined : "err");
    } catch (e) { toast(esc(e.message), "err"); } finally { matrixBusy = false; refreshFn(false); }
  };
  $("matrixSweep").onclick = async () => {
    try {
      const st = await api("/api/matrix/sweep", { method: "POST", body: { slug: S.slug } });
      toast(st.sweep ? `Compiler matrix running on ${st.sweep.total || "the"} functions in the background.` : "Started.");
    } catch (e) { toast(esc(e.message), "err"); }
  };
}

/* Editing a function yourself: a highlighted editor over a textarea, judged like an agent's attempt. */
function setEditor(source, d) {
  const ta = $("fnEdit");
  ta.value = source || (d && d.status !== "matched" ? `/* write ${d.name} here */\n` : "");
  paintEditor();
  S.editDirty = false;
}
function paintEditor() {
  const ta = $("fnEdit");
  $("fnSrc").innerHTML = highlightC(ta.value) + "\n";
  $("fnSrc").scrollTop = ta.scrollTop; $("fnSrc").scrollLeft = ta.scrollLeft;
}
$("fnEdit").addEventListener("input", () => { S.editDirty = true; paintEditor(); });
$("fnEdit").addEventListener("scroll", () => { $("fnSrc").scrollTop = $("fnEdit").scrollTop; $("fnSrc").scrollLeft = $("fnEdit").scrollLeft; });
$("fnEdit").addEventListener("keydown", (e) => {
  const ta = e.target;
  if (e.key === "Tab" && !e.shiftKey) {
    e.preventDefault();
    const { selectionStart: a, selectionEnd: b } = ta;
    ta.setRangeText("    ", a, b, "end");
    S.editDirty = true; paintEditor();
  } else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    tryIt();
  }
});
$("tryBtn").onclick = () => tryIt();

async function tryIt() {
  const name = S.fn, source = $("fnEdit").value, msg = $("tryMsg");
  if (!name || !source.trim() || $("tryBtn").disabled) return;
  $("tryBtn").disabled = true;
  msg.hidden = false; msg.className = "try-msg"; msg.textContent = "Compiling…";
  try {
    const r = await api("/api/try", { method: "POST", body: { slug: S.slug, name, source } });
    if (r.matched) {
      S.myClaim = null;
      msg.className = "try-msg ok"; msg.textContent = "Byte-identical. Saved as a match, credited to you.";
      toast(`<code>${esc(name)}</code>&nbsp;matched by you`, "match", true);
    } else {
      S.myClaim = name;
      msg.className = "try-msg" + (r.score == null ? " bad" : "");
      msg.textContent = r.score == null ? "It didn't compile:" : (r.text.split("\n")[0] || `score ${r.score}`);
      if (r.score == null) msg.textContent += "\n" + r.text.split("\n").slice(1, 12).join("\n");
    }
    S.editDirty = false;
    S.diffKey = null;
    await refreshFn(false);
    if (r.score != null) { S.ftab = "diff"; showFnTab("diff"); }
  } catch (e) {
    msg.className = "try-msg bad"; msg.textContent = e.message;
  } finally { $("tryBtn").disabled = false; }
}
/* The function stays yours only while you're on it: give it back when you move on. */
function giveBack(next) {
  if (S.myClaim && S.myClaim !== next && S.slug) {
    api("/api/release", { method: "POST", body: { slug: S.slug, name: S.myClaim } }).catch(() => {});
    S.myClaim = null;
  }
}

/* Score per compile, as a match percentage: dots for each compile, a line for the best so far. */
function renderFnChart(d) {
  const wrap = $("fnChartWrap"), svg = $("fnChart");
  const pts = d.scores;
  wrap.hidden = !pts.length;
  if (!pts.length) return;
  const w = Math.max(300, svg.clientWidth || 600), h = 92, padL = 34, padR = 10, padT = 8, padB = 16;
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  const n = pts.length;
  const X = (i) => padL + (n === 1 ? (w - padL - padR) / 2 : (i * (w - padL - padR)) / (n - 1));
  const Y = (p) => padT + (1 - p) * (h - padT - padB);
  let out = "";
  for (const g of [0, 0.5, 1]) out += `<line class="grid" x1="${padL}" x2="${w - padR}" y1="${Y(g)}" y2="${Y(g)}"/><text class="axis" x="${padL - 6}" y="${Y(g) + 3.5}">${g * 100}%</text>`;
  let best = null, path = "";
  const agents = [...new Set(pts.map((p) => p.agent))];
  pts.forEach((p, i) => {
    if (p.score != null) best = best == null ? p.score : Math.min(best, p.score);
    if (best != null) {
      const y = Y(pctOf(best, d.insns));
      path += path ? `H${X(i).toFixed(1)}V${y.toFixed(1)}` : `M${X(i).toFixed(1)},${y.toFixed(1)}`;
    }
  });
  if (path) out += `<path class="bestline" d="${path}"/>`;
  pts.forEach((p, i) => {
    const title = `<title>${esc(p.agent || "")} · ${clock(p.t)} · ${p.score == null ? "didn't compile" : p.score === 0 ? "byte-identical" : `score ${p.score}`}</title>`;
    if (p.score == null) out += `<g class="fail"><path d="M${X(i) - 3},${Y(0) + 5}l6,6m0,-6l-6,6"/>${title}</g>`;
    else out += `<circle class="${p.score === 0 ? "hit" : "pt"} a${agents.indexOf(p.agent) % 4}" cx="${X(i).toFixed(1)}" cy="${Y(pctOf(p.score, d.insns)).toFixed(1)}" r="${p.score === 0 ? 4.5 : 3}">${title}</circle>`;
  });
  svg.innerHTML = out;
  const fails = pts.filter((p) => p.score == null).length;
  const bp = best == null ? null : pctOf(best, d.insns);
  $("fnChartNote").innerHTML = `<b>${plural(n, "compile")}</b>${bp != null ? ` · best ${best === 0 ? "byte-identical" : pctText(bp)}` : ""}${fails ? ` · ${fails} didn't compile` : ""}${agents.length > 1 ? ` · ${agents.map(esc).join(", ")}` : ` · ${esc(agents[0] || "")}`}`;
}

/* The judge's diff: target and attempt side by side, instruction by instruction. */
const MARK = { " ": "eq", r: "arg", s: "arg", i: "arg", "|": "repl", "<": "del", ">": "ins", o: "mov" };
const MARK_TEXT = { r: "registers differ", s: "stack offset differs", i: "operand differs", "|": "different instruction", "<": "missing in yours", ">": "extra in yours", o: "in a different place" };
function insnHtml(text, other, cls) {
  if (!text) return "";
  const m = text.match(/^(\S+)(\s*)(.*)$/);
  if (!m) return esc(text);
  let ops = esc(m[3]);
  if (cls === "arg" && other) {
    const a = m[3].split(/(,\s*|\(|\))/), b = (other.match(/^\S+\s*(.*)$/) || [, ""])[1].split(/(,\s*|\(|\))/);
    ops = a.map((tok, k) => (tok !== b[k] && !/^(,\s*|\(|\))$/.test(tok) ? `<mark>${esc(tok)}</mark>` : esc(tok))).join("");
  }
  return `<span class="op">${esc(m[1])}</span>${m[2] ? " " : ""}${ops}`;
}
function renderDiff(d) {
  const sum = $("diffSum"), box = $("diff"), probs = $("diffProblems");
  probs.innerHTML = "";
  if (!d.rows) {
    sum.innerHTML = d.error ? `<span class="bad">The ${d.kind === "best" ? "best" : "latest"} attempt didn't compile.</span>` : "No attempt yet. The diff appears after the first compile.";
    box.innerHTML = d.error ? `<pre class="code errlog">${esc(d.error)}</pre>` : "";
    return;
  }
  const rows = d.rows;
  const same = rows.filter((r) => r[0] === " ").length, target = rows.filter((r) => r[1]).length;
  const lang = S.ov.kind === "analysis" ? "" : (S.ov.source_language || "C");
  const label = { matched: lang ? `Matching ${lang}` : "Matching source", best: "Best attempt", latest: S.ov.kind === "fork" ? "The source now" : "Latest attempt" }[d.kind] || "Attempt";
  sum.innerHTML = `<b>${label}</b> · ${d.percent === 100 && d.score === 0 ? `<span class="ok">byte-identical</span>` : `${d.percent}%${d.score != null ? ` · score ${d.score}` : ""}`} · ${same} of ${target} instructions identical
    <span class="diff-key"><i class="k arg"></i>operands <i class="k repl"></i>different <i class="k mov"></i>moved</span>`;
  const html = [];
  let t = 0, run = [];
  const rowHtml = (r) => {
    const cls = MARK[r[0]] || "repl";
    if (r[1]) t++;
    return `<div class="dr ${cls}"${r[0] !== " " ? ` title="${MARK_TEXT[r[0]] || ""}"` : ""}><span class="dn">${r[1] ? t : ""}</span><code>${insnHtml(r[1], r[2], cls)}</code><span class="dm">${r[0] === " " ? "" : esc(r[0])}</span><code>${insnHtml(r[2], r[1], cls)}</code></div>`;
  };
  const flush = (end) => {
    if (run.length > 10 && end !== "all") {
      const head = run.slice(0, end === "start" ? 0 : 3), tail = run.slice(end === "end" ? run.length : run.length - 3);
      const mid = run.slice(head.length, run.length - tail.length);
      html.push(...head.map(rowHtml));
      html.push(`<button class="dfold" type="button">··· ${mid.length} identical instructions ···</button><div class="dfolded" hidden>${mid.map(rowHtml).join("")}</div>`);
      html.push(...tail.map(rowHtml));
    } else html.push(...run.map(rowHtml));
    run = [];
  };
  rows.forEach((r) => {
    if (r[0] === " ") { run.push(r); return; }
    flush(html.length ? "mid" : "start");
    html.push(rowHtml(r));
  });
  flush(html.length ? "end" : "all");
  box.innerHTML = `<div class="dh"><span></span><span>Target (your ${isNativeProject(S.ov) ? "program" : "game"})</span><span></span><span>${esc(label)}</span></div>` + html.join("");
  box.querySelectorAll(".dfold").forEach((b) => { b.onclick = () => { b.nextElementSibling.hidden = false; b.remove(); }; });
  probs.innerHTML = (d.problems || []).map((p) => `<li>${esc(p)}</li>`).join("");
}
async function loadDiff() {
  const d0 = S.fnDetail;
  if (!d0) return;
  const key = `${d0.name}|${S.which}|${d0.scores.length}|${d0.status}`;
  if (key === S.diffKey) return;
  S.diffKey = key;
  if (!$("diff").innerHTML) $("diffSum").textContent = "Compiling…";
  try {
    const d = await api(`/api/diff?slug=${S.slug}&name=${encodeURIComponent(d0.name)}&which=${S.which}`);
    if (S.fnDetail && S.fnDetail.name === d0.name) renderDiff(d);
  } catch (e) { $("diffSum").innerHTML = `<span class="bad">${esc(e.message)}</span>`; S.diffKey = null; }
}

function stepFn(delta) {
  if (!S.fn || !S.map.length) return;
  const order = S.tab === "functions" && L.rows.length ? L.rows : S.map;
  const k = order.findIndex((f) => f.name === S.fn);
  const next = order[(k + delta + order.length) % order.length];
  if (next) openFn(next.name);
}
$("fnPrev").onclick = () => stepFn(-1);
$("fnNext").onclick = () => stepFn(1);

