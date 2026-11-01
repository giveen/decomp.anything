/* ------------------------------------------------------------------ public decomps */
const PLAT_ORDER = ["gc", "wii", "ps", "ps2", "n64", "gba", "nds", "switch", "win32", "xbox360", "3ds", "xbox", "psp"];
const PLAT_SHORT = { gc: "GameCube", wii: "Wii", ps: "PS1", psx: "PS1", ps2: "PS2", n64: "N64", gba: "GBA", nds: "DS", switch: "Switch", win32: "PC", xbox360: "Xbox 360", "3ds": "3DS", xbox: "Xbox", psp: "PSP" };
const cat = { list: null, downloaded: null, plat: "", q: "", done: false };

/* The list decomp.dev had, as this install downloaded it before the directory was retired (none on a new one). */
async function loadCatalog() {
  if (!cat.list) ({ projects: cat.list, downloaded: cat.downloaded } = await api("/api/catalog"));
  return cat.list;
}

function ago(iso) {
  if (!iso) return "";
  const d = (Date.now() - Date.parse(iso)) / 86400000;
  return d < 1 ? "today" : d < 2 ? "yesterday" : d < 60 ? `${Math.round(d)} days ago` : d < 730 ? `${Math.round(d / 30)} months ago` : `${Math.round(d / 365)} years ago`;
}

async function showCatalog() {
  show("catalog");
  try { await loadCatalog(); } catch { cat.list = []; }
  const any = cat.list.length > 0;
  $("catTools").hidden = $("catChips").hidden = !any;
  if (!any) { $("catGrid").innerHTML = ""; $("catNote").textContent = ""; return; }
  if (!S.st) await loadState().catch(() => {});
  renderCatalog();
}

function renderCatalog() {
  const all = cat.list || [];
  const counts = {};
  const shown = all.filter((p) => cat.done || p.matched < 99.99);
  for (const p of shown) counts[p.platform] = (counts[p.platform] || 0) + 1;
  const plats = PLAT_ORDER.filter((k) => counts[k]).concat(Object.keys(counts).filter((k) => !PLAT_ORDER.includes(k)));
  $("catChips").innerHTML = [`<button class="chip ${cat.plat ? "" : "on"}" data-plat="" type="button">All <span>${shown.length}</span></button>`]
    .concat(plats.map((k) => `<button class="chip ${cat.plat === k ? "on" : ""}" data-plat="${esc(k)}" type="button">${esc(PLAT_SHORT[k] || k.toUpperCase())} <span>${counts[k]}</span></button>`)).join("");
  $("catChips").querySelectorAll(".chip").forEach((b) => { b.onclick = () => { cat.plat = b.dataset.plat; renderCatalog(); }; });
  const q = cat.q.toLowerCase();
  const forked = new Map((S.st?.projects || []).filter((p) => p.kind === "fork").map((p) => [p.repo_url, p.slug]));
  const rows = all.filter((p) => (!cat.plat || p.platform === cat.plat) && (cat.done || p.matched < 99.99)
    && (!q || `${p.name} ${p.id}`.toLowerCase().includes(q)));
  const when = cat.downloaded ? new Date(cat.downloaded * 1000).toLocaleDateString() : "earlier";
  $("catNote").textContent = `${rows.length} of ${all.length} projects${cat.done ? "" : " (finished ones hidden)"}: the list decomp.anything downloaded from decomp.dev ${cat.downloaded ? "on " + when : when}, before its directory was retired, with the progress of then. decomp.anything isn't affiliated with these projects.`;
  $("catGrid").innerHTML = rows.slice(0, 240).map((p) => `<button class="cat-card" type="button" data-id="${esc(p.id)}">
      <div class="cc-top"><span class="plat">${esc(PLAT_SHORT[p.platform] || p.platform_name)}</span>${forked.has(p.repo_url) ? `<span class="badge matched">forked</span>` : p.matched >= 99.99 ? `<span class="badge">done</span>` : ""}</div>
      <h4>${esc(p.name)}</h4>
      <p class="meta">${esc(p.id)}${p.version ? " · " + esc(p.version) : ""}</p>
      <div class="meter"><i data-w="${p.matched}"></i></div>
      <p class="meta"><b>${p.matched.toFixed(1)}%</b> of code · ${p.functions ? `${p.matched_functions.toLocaleString()} / ${p.functions.toLocaleString()} functions` : "functions unknown"}${p.updated ? " · " + ago(p.updated) : ""}</p>
    </button>`).join("") || `<p class="muted">Nothing matches.</p>`;
  requestAnimationFrame(() => $("catGrid").querySelectorAll(".meter i").forEach((i) => { i.style.width = i.dataset.w + "%"; }));
  $("catGrid").querySelectorAll(".cat-card").forEach((b) => {
    b.onclick = () => {
      const p = all.find((x) => x.id === b.dataset.id);
      if (forked.has(p.repo_url)) location.hash = `#/p/${forked.get(p.repo_url)}`;
      else openForkDialog(p);
    };
  });
}
$("catSearch").addEventListener("input", () => { cat.q = $("catSearch").value; renderCatalog(); });
$("catDone").addEventListener("change", () => { cat.done = $("catDone").checked; renderCatalog(); });
$("repoUrl").addEventListener("keydown", (e) => { if (e.key === "Enter") $("repoFork").click(); });
$("repoFork").onclick = () => {
  const url = $("repoUrl").value.trim();
  if (!url) return;
  openForkDialog({ name: url.replace(/\/+$/, "").split("/").pop().replace(/\.git$/, ""), repo_url: url, platform: "", platform_name: "", id: url });
};

/* What the project's own files say about AI, word for word, before anything is cloned. */
function policyHtml(pol) {
  if (pol.error) return `<div class="notice"><span>Couldn't read what the project says about AI (${esc(pol.error)}). Look at its README and contribution rules yourself before you fork it.</span></div>`;
  const quotes = pol.lines.map((l) => `<blockquote class="policy-line">“${esc(l.text)}” <span class="muted small">${esc(l.file)}</span></blockquote>`).join("");
  if (pol.lean === "no") return `<div class="notice bad"><span><b>This project says it doesn't want AI-made code.</b> Respect that: keep this fork for yourself, to study the game, and send it nothing your agents wrote.</span></div>${quotes}
    <label class="check"><input type="checkbox" id="forkOk"><span>I'll keep this fork to myself and won't send AI-made code to this project.</span></label>`;
  if (pol.lean === "silent") return `<p class="muted small">Its README and contribution files (${esc(pol.files.join(", ") || "none")}) say nothing about AI. Ask the project's team before you send it anything your agents wrote.</p>`;
  if (pol.lean === "rules") return `<p class="small"><b>This project allows AI, with rules of its own.</b> Follow them in anything you send it:</p>${quotes}`;
  return `<p class="small"><b>What the project says about AI:</b></p>${quotes}`;
}

function openForkDialog(p) {
  const body = dialog(`Fork ${p.name}`, `<p>decomp.anything clones <a class="link" href="${esc(p.repo_url)}" target="_blank" rel="noopener">${esc(p.id)}</a> to this computer and sets it up for your agents.</p>
    ${p.functions ? `<dl class="facts">${[["Platform", p.platform_name || "?"], ["Version", p.version || "the project's default"], ["Progress", `${p.matched.toFixed(1)}% of code`]]
      .map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl>
      <p class="muted small">Progress as decomp.dev last listed it; <a class="link" href="https://decomp.dev/${esc(p.id)}" target="_blank" rel="noopener">its page there</a> has the project's history, files and contributors.</p>` : ""}
    <div id="forkPolicy"><p class="muted small">Reading what the project says about AI and contributions…</p></div>
    <p class="muted small">Next you add <b>your own copy of the game</b>. Its code stays on your computer; your agent's provider sees the functions it works on. Setting up runs the project's own build scripts, as building it yourself would, so fork projects you trust. Matches are committed to a local <code>decomp-wtf</code> branch, and decomp.anything never pushes it anywhere: anything you send upstream is up to you and the project's rules.</p>
    <div class="row"><button class="primary" id="forkGo" type="button" disabled>Fork it</button><button class="ghost" data-close type="button">Cancel</button></div>
    <pre class="joblog" id="forkLog" hidden></pre>`);
  api("/api/fork/policy", { method: "POST", body: { repo: p.repo_url } }).catch((e) => ({ error: e.message, lines: [], files: [], lean: "silent" })).then((pol) => {
    if (!$("forkPolicy")) return;  // (the dialog was closed meanwhile)
    $("forkPolicy").innerHTML = policyHtml(pol);
    const ok = $("forkOk");
    $("forkGo").disabled = !!ok;
    if (ok) ok.onchange = () => { $("forkGo").disabled = !ok.checked; };
  });
  body.querySelector("#forkGo").onclick = async () => {
    $("forkGo").disabled = true;
    $("forkLog").hidden = false;
    try {
      const { job } = await api("/api/fork", { method: "POST", body: { repo: p.repo_url, name: p.name, platform: p.platform, version: p.version } });
      const r = await followJob(job, $("forkLog"));
      closeDialog();
      await loadState();
      loadNav();
      location.hash = `#/p/${r.slug}`;
    } catch (e) { toast(esc(e.message), "err"); $("forkGo").disabled = false; }
  };
}

function uploadFile(url, file, onProgress) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("POST", url);
    x.setRequestHeader("X-Decomp", "1");
    x.setRequestHeader("Content-Type", "application/octet-stream");
    x.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    x.onload = () => {
      let data = null;
      try { data = JSON.parse(x.responseText); } catch { /* not json */ }
      if (x.status >= 200 && x.status < 300) resolve(data); else reject(new Error((data && data.error) || `upload failed (${x.status})`));
    };
    x.onerror = () => reject(new Error("upload failed"));
    x.send(file);
  });
}

async function renderForkSetup() {
  const box = $("forkSetup");
  box.hidden = false;
  box.innerHTML = `<p class="muted">Looking at the project…</p>`;
  let n;
  try { n = await api(`/api/fork/needs?slug=${S.slug}`); } catch (e) { box.innerHTML = `<p class="muted">${esc(e.message)}</p>`; return; }
  const kinds = { manifest: "decomp.anything manifest", dtk: "dtk-template (configure.py + ninja)", configure: "configure.py + ninja", splat: "splat + make", objdiff: "objdiff project", custom: "its own setup" };
  const facts = `<dl class="facts one">${[["Repository", (S.ov.repo_url || "").replace(/^https?:\/\//, "") || "made by decomp.anything"], ["Layout", kinds[n.kind] || n.kind], ["Version", n.version || "—"], ["Game goes to", n.path || "see the README"]]
    .map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join("")}</dl>
    <div class="row"><button class="ghost small-btn" id="fsFolder" type="button">Open project folder</button></div>`;
  const run = async (start) => {
    $("fsLog").hidden = false;
    box.querySelectorAll("button, input").forEach((b) => { b.disabled = true; });
    try {
      const { job } = await start();
      await followJob(job, $("fsLog"));
      toast("The project is ready for your agents.", "match");
      loadNav();
      openProject(S.slug);
    } catch (e) {
      toast(esc(e.message), "err");
      $("fsLog").textContent += `\n\n✕ ${e.message}`;  // stays visible after the toast is gone
      $("fsLog").scrollTop = $("fsLog").scrollHeight;
      box.querySelectorAll("button, input").forEach((b) => { b.disabled = false; });
      if ($("fsOwn")) $("fsGo").disabled = !$("fsOwn").checked;
    }
  };
  if (n.auto) {
    box.innerHTML = `<div class="fs-main">
        <p class="eyebrow">Last step</p>
        <h2>Add your copy of the game</h2>
        <p class="muted">${esc(n.what)}. ${esc(n.help || "")}</p>
        <label class="check"><input type="checkbox" id="fsOwn"><span>This is my own copy of this game, obtained legally (for example, dumped from my console). The results stay with me for my own use.</span></label>
        <div class="row"><input type="file" id="fsFile" hidden><button class="primary" id="fsGo" type="button" disabled>Choose the game file…</button>
          <span class="muted small">or</span><input id="fsPath" class="mono grow" placeholder="paste its path on this computer"><button class="ghost" id="fsPathGo" type="button">Use path</button></div>
        <div class="upbar" id="fsUp" hidden><i></i></div>
        <pre class="joblog tall" id="fsLog" hidden></pre>
        <p class="muted small">Then decomp.anything runs the project's setup and build, which can download the project's own tools and take a few minutes. Put the game in place yourself? <button class="link-btn" id="fsBuild" type="button">Just build</button></p>
      </div><aside class="fs-side">${facts}</aside>`;
    $("fsOwn").onchange = () => { $("fsGo").disabled = !$("fsOwn").checked; };
    $("fsGo").onclick = () => $("fsFile").click();
    $("fsFile").onchange = () => {
      const f = $("fsFile").files[0];
      if (!f) return;
      $("fsUp").hidden = false;
      run(() => uploadFile(`/api/fork/game?slug=${encodeURIComponent(S.slug)}&confirmed=1&name=${encodeURIComponent(f.name)}`, f,
        (p) => { $("fsUp").firstElementChild.style.width = (p * 100).toFixed(1) + "%"; }));
    };
    $("fsPathGo").onclick = () => {
      if (!$("fsOwn").checked) return toast("Confirm that this is your own copy of the game first.", "err");
      if (!$("fsPath").value.trim()) return;
      run(() => api("/api/fork/setup", { method: "POST", body: { slug: S.slug, path: $("fsPath").value.trim(), confirmed: true } }));
    };
    $("fsBuild").onclick = () => run(() => api("/api/fork/setup", { method: "POST", body: { slug: S.slug } }));
  } else {
    box.innerHTML = `<div class="fs-main">
        <p class="eyebrow">Last step</p>
        <h2>Set it up the project's way</h2>
        <p class="muted">${esc(n.help)}</p>
        <ol class="steps"><li>Open a terminal in the project folder.</li><li>Follow the README: it says where your copy of the game goes and how to build.</li><li>Come back and click <b>Check again</b>.</li></ol>
        ${cmdBox(`cd "${S.ov.folder}"`)}
        ${n.readme ? `<p class="col-title">From the project's README</p><pre class="readme" id="fsReadme"></pre>` : ""}
        <div class="row"><button class="primary" id="fsCheck" type="button">Check again</button></div>
        <pre class="joblog tall" id="fsLog" hidden></pre>
      </div><aside class="fs-side">${facts}</aside>`;
    if (n.readme) $("fsReadme").textContent = n.readme;
    wireCopies(box);
    $("fsCheck").onclick = () => run(() => api("/api/fork/check", { method: "POST", body: { slug: S.slug } }));
  }
  $("fsFolder").onclick = () => openFolder();
}

