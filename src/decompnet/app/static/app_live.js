/* ------------------------------------------------------------------ now working */
function spark(points, w = 240, h = 34) {
  const vals = points.filter((p) => p.score != null);
  if (!vals.length) return `<svg viewBox="0 0 ${w} ${h}"></svg>`;
  const max = Math.max(...vals.map((p) => p.score), 1);
  const step = vals.length > 1 ? w / (vals.length - 1) : 0;
  const pts = vals.map((p, i) => [i * step, 3 + (h - 6) * (p.score / max)]);
  const d = pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join("");
  const last = pts[pts.length - 1];
  const done = vals[vals.length - 1].score === 0;
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><path d="${d}" fill="none" stroke="var(${done ? "--match" : "--work"})" stroke-width="1.6" vector-effect="non-scaling-stroke"/>
    <circle cx="${last[0]}" cy="${last[1]}" r="2.6" fill="var(${done ? "--match" : "--work"})"/></svg>`;
}

/* No compile yet: the agent is reading (the function, its file, look-alikes), for this long so far */
function reading(c) {
  const min = c.claimed_at ? Math.floor((Date.now() / 1000 - c.claimed_at) / 60) : 0;
  return min >= 1 ? `reading · ${min} min` : "reading";
}

function renderNow(claims) {
  const box = $("now");
  claims = claims.filter((c) => c.agent !== AUTO);
  if (!claims.length) {
    if (box.innerHTML !== "") {
      box._lastHtml = "";
      box.innerHTML = "";
    }
    return;
  }
  const html = claims.map((c) => {
    const sc = S.scores.get(c.name) || [];
    const p = pctOf(c.best_score, c.n_insns);
    const [sym, unit] = c.name.split(/@(.*)/s);  // a fork's symbol@file: the symbol first, the file after it
    return `<div class="job" data-fn="${esc(c.name)}"><div class="job-top"><span class="job-name" title="${esc(c.name)}">${esc(sym)}${unit ? `<small>@${esc(unit)}</small>` : ""}</span><span class="job-who" title="${esc(c.agent)}">${esc(c.agent)}</span></div>
      ${spark(sc)}<div class="job-foot"><span>${c.n_insns} insns · ${c.attempts} tries</span><b>${p == null ? reading(c) : pctText(p)}</b></div></div>`;
  }).join("");
  if (box._lastHtml === html) return;
  box._lastHtml = html;
  box.innerHTML = html;
  box.querySelectorAll(".job").forEach((j) => { j.onclick = () => openFn(j.dataset.fn); });
}

/* ------------------------------------------------------------------ agents on this project */
const STATE_TEXT = { starting: "starting…", working: "working", waiting: "next session in a moment", stopped: "stopped", error: "stopped with an error" };

function renderSessions(sessions) {
  const box = $("sessions");
  const board = new Map((S.stats?.agents || []).map((r) => [r.agent, r]));
  const running = sessions.filter((s) => ["starting", "working", "waiting"].includes(s.state));
  $("stopAll").hidden = running.length === 0;  // one agent needs it too, and the route stops every helper here
  const own = new Set(sessions.map((s) => s.agent));
  const outside = [...new Set(S.claims.filter((c) => !own.has(c.agent) && c.agent !== AUTO).map((c) => c.agent))];
  $("noSessions").hidden = sessions.length + outside.length > 0;

  const html = sessions.map((s) => {
    const claim = S.claims.find((c) => c.agent === s.agent);
    const dot = s.state === "error" ? "err" : s.state === "working" ? "on" : s.state === "stopped" ? "" : "work";
    const live = ["starting", "working", "waiting"].includes(s.state);
    const b = board.get(s.agent);
    const facts = [s.model ? esc(s.model) : "default model", s.runs ? `session ${s.runs}` : "", b && b.matched ? `<b class="ok">${b.matched} matched</b>` : "", b ? plural(b.compiles, "compile") : "",
      s.usd ? money(s.usd) : s.tokens ? `${(s.tokens / 1000).toFixed(0)}k tokens` : "", s.started_at ? `since ${clock(s.started_at).slice(0, 5)}` : ""].filter(Boolean);
    return `<div class="session"><div class="session-top"><span class="dot ${dot}"></span><b>${esc(s.label)}</b>
        <span class="state ${s.state === "error" ? "err" : ""}">${esc(STATE_TEXT[s.state] || s.state)}</span>
        <button class="ghost" data-stop="${esc(s.id)}" data-remove="${live ? "" : "1"}" type="button">${live ? "Stop" : "Remove"}</button></div>
      <span class="facts-line">${facts.join(" · ")}</span>
      ${s.state === "error" ? `<span class="state err">${esc(s.error)}</span>` : ""}
      ${claim ? `<span class="last">on ${fnLink(claim.name)}</span>` : ""}</div>`;
  }).join("") + outside.map((a) => `<div class="session ext"><div class="session-top"><span class="dot on"></span><b>${esc(a)}</b><span class="tag">${a.startsWith("@") ? "in the room" : "connected agent"}</span></div>
      <span class="last">on ${S.claims.filter((c) => c.agent === a).map((c) => fnLink(c.name)).join(", ")}</span></div>`).join("");

  if (box._lastHtml === html) return;
  box._lastHtml = html;
  box.innerHTML = html;
  box.querySelectorAll(".fn").forEach((x) => { x.onclick = () => openFn(x.dataset.fn); });
  box.querySelectorAll("[data-stop]").forEach((b) => {
    b.onclick = async () => {
      await api("/api/agents/stop", { method: "POST", body: { id: b.dataset.stop, remove: !!b.dataset.remove } }).catch((e) => toast(esc(e.message), "err"));
      poll(false);
    };
  });
}
$("stopAll").onclick = async () => {
  if (!confirm("Stop every agent on this project? Their work so far is kept.")) return;
  await api("/api/agents/stop", { method: "POST", body: { slug: S.slug } }).catch((e) => toast(esc(e.message), "err"));
  toast("Stopping every agent on this project.");
  poll(false);
};

/* ------------------------------------------------------------------ the launcher: agent, model, how many */
const LZ = { kind: null, model: "", count: 1, per: null };
function launcherInit() {
  if (LZ.kind || !S.st) return;
  const l = S.st.settings.launcher || {};
  const ready = S.st.agents.filter((a) => a.installed && a.id !== "api");
  const known = S.st.agents.find((a) => a.id === l.kind && (a.installed || a.id === "api"));
  LZ.kind = known ? known.id : (ready[0] ? ready[0].id : "api");
  LZ.model = known ? (l.model || "") : "";
  LZ.count = l.count || 1;
  const eps = S.st.settings.endpoints || [];
  LZ.eps = (l.endpoints || []).filter((id) => eps.some((e) => e.id === id));
  if (!LZ.eps.length && eps.length) LZ.eps = [eps[0].id];
}
function modelOptions(a) {
  const recent = (S.st.settings.recent_models || {})[a.id] || [];
  const opts = [{ id: "", name: a.default_model ? `Default · ${a.default_model}` : "Default", note: "" }];
  for (const m of a.models || []) opts.push(m);
  for (const r of recent) if (!opts.some((o) => o.id === r)) opts.push({ id: r, name: r, note: "used before" });
  return opts;
}
function modelName(a, model) {
  if (!model) return "";
  const m = (a.models || []).find((x) => x.id === model);
  return m ? m.name : model;
}
function launchLabel(a, model, count) {
  if (a.id === "api") {
    const picked = (S.st.settings.endpoints || []).filter((e) => (LZ.eps || []).includes(e.id));
    if (picked.length > 1) return `Start ${count} across ${picked.length} endpoints`;
    return `Start ${picked[0] ? picked[0].name : "API model"}${count > 1 ? ` × ${count}` : ""}`;
  }
  return `Start ${a.name}${model ? ` · ${modelName(a, model)}` : ""}${count > 1 ? ` × ${count}` : ""}`;
}

async function startAgents(a, model, count, btn, extra = {}) {
  if (btn) btn.disabled = true;
  try {
    const options = { ...extra };
    if (model && a.id !== "api") options.model = model;
    if (LZ.per) options.per_session = LZ.per;
    await api("/api/agents/start", { method: "POST", body: { kind: a.id, slug: S.slug, count, options } });
    S.st.settings.launcher = { kind: a.id, model: model || "", count };
    if (model && a.id !== "api") {
      const rec = (S.st.settings.recent_models ||= {});
      rec[a.id] = [model, ...(rec[a.id] || []).filter((m) => m !== model)].slice(0, 8);
    }
    hideLaunch();
    toast(`${esc(a.id === "api" ? "Your API model" : a.name)} ${count > 1 ? `× ${count} are` : "is"} starting. Watch it on the map and in Activity.`, "match");
    poll(false);
    loadNav();
  } catch (e) { toast(esc(e.message), "err"); } finally { if (btn) btn.disabled = false; }
}

function renderLauncher(box, compact = false) {
  if (!box || !S.st) return;
  launcherInit();
  const list = S.st.agents;
  const a = list.find((x) => x.id === LZ.kind) || list[0];
  const kinds = list.map((x) => `<button type="button" class="lz-kind ${x.id === a.id ? "on" : ""}" data-k="${esc(x.id)}">
      <b>${esc(x.name)}</b><span>${x.id === "api" ? "DeepSeek, OpenRouter, Ollama…" : x.installed ? esc(x.version ? x.version.replace(/\s*\(.*\)/, "") : x.vendor) : "not installed"}</span>
      ${x.installed || x.id === "api" ? "" : `<em>install</em>`}</button>`).join("");
  let body = "";
  if (!a.installed) {
    body = `<p class="small"><b>${esc(a.name)}</b> isn't installed on this computer.${a.needs ? ` It needs ${esc(a.needs)} first.` : ""}</p>
      ${a.install ? cmdBox(a.install) : ""}
      <div class="row"><button class="primary" data-act="install" type="button" ${a.needs ? "disabled" : ""}>Install it for me</button><a class="link small" href="${esc(a.docs)}" target="_blank" rel="noopener">About ${esc(a.name)}</a></div>
      <p class="muted small">${esc(a.signin)}</p><pre class="joblog" data-log hidden></pre>`;
  } else if (a.id === "api") {
    const eps = S.st.settings.endpoints || [];
    LZ.eps = (LZ.eps || []).filter((id) => eps.some((e) => e.id === id));
    if (!LZ.eps.length && eps.length) LZ.eps = [eps[0].id];
    body = eps.length && !LZ.adding ? `<div class="ep-pick">${eps.map((e) => `<label class="check inline"><input type="checkbox" data-ep="${esc(e.id)}" ${LZ.eps.includes(e.id) ? "checked" : ""}>
        <span><b>${esc(e.name)}</b> <span class="mono small">${esc(e.model)}</span><br><span class="muted small mono">${esc(hostOf(e.base_url))}${e.max_sessions ? ` · at most ${e.max_sessions} at once` : ""}</span></span></label>`).join("")}</div>
      <p class="muted small">${eps.length > 1 ? "Tick several to spread the sessions across them. " : ""}<a class="link" href="#/agents">Manage endpoints</a> · <button type="button" class="link-btn" data-act="add-ep">Add one</button></p>`
      : `<div class="lz-fields">${endpointFields({ provider: "deepseek" }, compact)}</div>${eps.length ? `<p class="muted small"><button type="button" class="link-btn" data-act="pick-ep">Back to your endpoints</button></p>` : ""}`;
  } else {
    const opts = modelOptions(a);
    const known = opts.some((o) => o.id === LZ.model);
    body = `<label class="field"><span>Model</span><select data-f="model">${opts.map((o) => `<option value="${esc(o.id)}" ${known && o.id === LZ.model ? "selected" : ""}>${esc(o.name)}${o.note ? ` — ${esc(o.note)}` : ""}</option>`).join("")}
        <option value="__other" ${known ? "" : "selected"}>Another model…</option></select></label>
      <input data-f="custom" class="mono lz-custom" placeholder="model name, as ${esc(a.name)} calls it" value="${known ? "" : esc(LZ.model)}" ${known ? "hidden" : ""}>`;
  }
  const ready = a.installed;
  box.innerHTML = `<div class="lz-kinds">${kinds}</div>
    <div class="lz-body">${body}
      ${ready ? `<div class="lz-row">
        <div class="field"><span>How many</span><span class="stepper"><button type="button" data-step="-1">−</button><output>${LZ.count}</output><button type="button" data-step="1">+</button></span></div>
        ${compact ? "" : `<label class="field"><span>Functions per session</span><input data-f="per" type="number" min="1" max="20" value="${LZ.per || S.st.settings.per_session || 2}"></label>`}
        <span class="grow"></span>
        <button class="primary" data-act="start" type="button">${esc(launchLabel(a, LZ.model, LZ.count))}</button></div>
        <p class="muted small">${a.id === "api" ? "A built-in agent loop drives the model through the decomp tools." : esc(a.signin)} Each agent takes a different function; a fresh session every few functions keeps its context small.</p>` : ""}
    </div>`;
  wireCopies(box);
  const rerender = () => { renderLauncher(box, compact); if (box !== $("launcher") && S.tab === "agents") renderLauncher($("launcher")); };
  box.querySelectorAll(".lz-kind").forEach((b) => { b.onclick = () => { LZ.kind = b.dataset.k; LZ.model = (S.st.settings.launcher || {}).kind === LZ.kind ? S.st.settings.launcher.model || "" : ""; rerender(); }; });
  const sel = box.querySelector('select[data-f="model"]'), custom = box.querySelector('[data-f="custom"]');
  const label = () => { const btn = box.querySelector('[data-act="start"]'); if (btn) btn.textContent = launchLabel(a, LZ.model, LZ.count); };
  if (sel) sel.onchange = () => { if (sel.value === "__other") { custom.hidden = false; custom.focus(); LZ.model = custom.value.trim(); } else { custom.hidden = true; LZ.model = sel.value; } label(); };
  if (custom) custom.oninput = () => { LZ.model = custom.value.trim(); label(); };
  box.querySelectorAll("[data-step]").forEach((b) => { b.onclick = () => { LZ.count = Math.max(1, Math.min(8, LZ.count + Number(b.dataset.step))); box.querySelector(".stepper output").textContent = LZ.count; label(); }; });
  const per = box.querySelector('[data-f="per"]');
  if (per) per.onchange = () => { LZ.per = Math.max(1, Math.min(20, Number(per.value) || 4)); };
  wireEndpointFields(box);
  box.querySelectorAll("[data-ep]").forEach((c) => { c.onchange = () => { LZ.eps = [...box.querySelectorAll("[data-ep]:checked")].map((x) => x.dataset.ep); label(); }; });
  const addEp = box.querySelector('[data-act="add-ep"]'), pickEp = box.querySelector('[data-act="pick-ep"]');
  if (addEp) addEp.onclick = () => { LZ.adding = true; rerender(); };
  if (pickEp) pickEp.onclick = () => { LZ.adding = false; rerender(); };
  const inst = box.querySelector('[data-act="install"]');
  if (inst) inst.onclick = async () => {
    const log = box.querySelector("[data-log]");
    log.hidden = false; inst.disabled = true;
    try {
      const { job } = await api("/api/agents/install", { method: "POST", body: { kind: a.id } });
      await followJob(job, log);
      toast(`${esc(a.name)} installed. ${esc(a.signin)}`, "match");
      await loadState();
      rerender();
    } catch (e) { toast(esc(e.message), "err"); inst.disabled = false; }
  };
  const go = box.querySelector('[data-act="start"]');
  if (go) go.onclick = async () => {
    if (a.id === "api") {
      if (box.querySelector('[data-f="prov"]')) {  // a new endpoint, typed in here: saved, then started on
        const before = new Set((S.st.settings.endpoints || []).map((e) => e.id));
        try { S.st.settings = await api("/api/settings", { method: "POST", body: { endpoint: endpointData(box) } }); } catch (e) { return toast(esc(e.message), "err"); }
        const added = (S.st.settings.endpoints || []).find((e) => !before.has(e.id));
        LZ.eps = added ? [added.id] : LZ.eps;
        LZ.adding = false;
      }
      if (!(LZ.eps || []).length) return toast("Tick an endpoint to start on.", "err");
      return startAgents(a, "", LZ.count, go, { endpoints: LZ.eps });
    }
    startAgents(a, LZ.model, LZ.count, go);
  };
}

/* API endpoints: OpenAI-compatible servers (DeepSeek, a llama.cpp box on the network...), each with its own address,
   model and key. The same fields in the launcher and on the Agents & models page. */
function hostOf(url) { return String(url || "").replace(/^\w+:\/\//, "").split("/")[0]; }
function endpointFields(e, compact = false) {
  const presets = S.st.api_presets, prov = presets[e.provider] ? e.provider : "custom";
  return `<label class="field"><span>Provider</span><select data-f="prov">${Object.entries(presets).map(([k, p]) => `<option value="${k}" ${k === prov ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select></label>
    <label class="field"><span>Model</span><input data-f="model" class="mono" value="${esc(e.model || presets[prov].model)}"></label>
    <label class="field wide"><span>API address</span><input data-f="base" class="mono" value="${esc(e.base_url || presets[prov].base_url)}" placeholder="http://gpu-box:8080/v1"></label>
    <label class="field wide"><span>API key (stored only on this computer, sent only to this address)</span><input data-f="key" type="password" placeholder="${e.has_key ? esc(e.api_key) : "paste your key (none for local models)"}" autocomplete="off"></label>
    ${compact ? "" : `<label class="field"><span>Name</span><input data-f="name" value="${esc(e.id ? e.name : "")}" placeholder="${esc(e.id ? e.name : "e.g. gpu-box")}"></label>
    <label class="field"><span>Context size (tokens)</span><input data-f="ctx" class="mono" inputmode="numeric" placeholder="automatic" value="${esc(String(e.context_tokens || ""))}"></label>
    <label class="field"><span>Sessions at once</span><input data-f="max" class="mono" inputmode="numeric" placeholder="no limit" value="${esc(String(e.max_sessions || ""))}"></label>`}`;
}
function wireEndpointFields(box) {
  box.querySelectorAll('[data-f="prov"]').forEach((sel) => {
    sel.onchange = () => {
      const p = S.st.api_presets[sel.value], scope = sel.closest(".ep-card, .lz-fields") || box;
      scope.querySelector('[data-f="model"]').value = p.model;
      scope.querySelector('[data-f="base"]').value = p.base_url;
    };
  });
}
function endpointData(scope, id = "") {
  const f = (k) => scope.querySelector(`[data-f="${k}"]`);
  const out = { provider: f("prov").value, base_url: f("base").value, model: f("model").value, api_key: f("key").value };
  if (id) out.id = id;
  if (f("name")) Object.assign(out, { name: f("name").value, context_tokens: f("ctx").value, max_sessions: f("max").value });
  return out;
}

function showLaunch() {
  if (!S.st || !S.slug) return;
  $("launchPop").hidden = false;
  renderLauncher($("launchPopBody"), true);
}
function hideLaunch() { $("launchPop").hidden = true; }
$("phStart").onclick = (e) => { e.stopPropagation(); if ($("launchPop").hidden) showLaunch(); else hideLaunch(); };
// (the path as clicked: choosing an agent re-renders the popover, so the clicked tile isn't in the page any more)
document.addEventListener("click", (e) => {
  if (!$("launchPop").hidden && !e.composedPath().some((el) => el.classList && el.classList.contains("pop-wrap"))) hideLaunch();
});

