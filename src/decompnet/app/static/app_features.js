/* ------------------------------------------------------------------ features (JavaScript/Electron)
   A feature is one REA trace through the application graph. Its nodes become work items in the project's queue, so
   agents analyze a feature instead of a flat list of procedures. */
const FS = { data: null, slug: null, detail: null, busy: false };

function featureSeedText(seed = {}) {
  return `${seed.kind || "seed"} ${JSON.stringify(seed.value ?? "")}${seed.match && seed.match !== "exact" ? ` (${seed.match})` : ""}`;
}
function featureCoverage(f = {}) {
  return (f.coverage || {}).status || "unknown";
}

async function renderFeatures(force = false) {
  const panel = $("featuresPanel");
  if (!S.ov || S.ov.kind !== "analysis" || !(S.ov.analysis || {}).features_traceable) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  if (FS.slug !== S.slug) { FS.slug = S.slug; FS.data = null; FS.detail = null; }
  if (!FS.data || force) {
    try {
      FS.data = await api(`/api/analysis/features?slug=${encodeURIComponent(S.slug)}`);
    } catch (e) {
      $("featureList").innerHTML = `<p class="bad small">${esc(e.message)}</p>`;
      return;
    }
  }
  const features = FS.data.features || [];
  $("featuresCount").textContent = features.length ? `${features.length} traced` : "none yet";
  $("featureList").innerHTML = features.length ? features.map((f) => `
    <div class="feature-row" data-feature="${esc(f.name)}">
      <div class="feature-main"><b>${esc(f.name)}</b><span class="muted small">${esc(featureSeedText(f.seed))} · ${esc(f.direction || "both")}</span></div>
      <div class="feature-stats"><span class="chip-s">${f.node_count} nodes</span><span class="chip-s">${f.item_count} work items</span><span class="chip-s">${esc(featureCoverage(f))}</span></div>
    </div>`).join("")
    : `<p class="muted small">No feature traced yet. Pick a route, string, IPC channel or module and let REA walk the app from there.</p>`;
  paintFeatureDetail();
}

function featureDetailHtml(f) {
  const rows = (f.nodes || []).map((n) => `<div class="feature-node">
      <span class="feature-kind">${esc(n.kind || "")}</span>
      <span class="feature-name" title="${esc(n.path || n.name || n.node_id)}">${esc(n.path || n.name || n.node_id)}</span>
      <span class="feature-reach" title="${esc(n.reached_by || "")}">${esc(n.reached_by || "seed")}</span>
      <span class="feature-item">${n.work_item ? `<span class="fn" data-feature-fn="${esc(n.work_item)}">${esc(n.work_item)}</span><small>${esc(n.status || "open")}</small>` : `<small class="muted">in the graph</small>`}</span>
    </div>`).join("");
  return `<div class="contents-detail-head"><b>${esc(f.name)}</b><span class="grow"></span>
      <button class="ghost small-btn" type="button" data-feature-close="1">Close</button>
      <button class="ghost small-btn" type="button" data-feature-remove="${esc(f.name)}">Remove</button></div>
    <dl class="contents-facts">
      <dt>Seed</dt><dd>${esc(featureSeedText(f.seed))}</dd>
      <dt>Direction</dt><dd>${esc(f.direction || "both")}</dd>
      <dt>Coverage</dt><dd>${esc(featureCoverage(f))}</dd>
      <dt>Graph</dt><dd>${f.node_count} nodes · ${f.edge_count} edges · ${f.summary?.terminal_paths || 0} paths</dd>
      ${(f.limitations || []).length ? `<dt>Limits</dt><dd>${f.limitations.map(esc).join("<br>")}</dd>` : ""}
    </dl>
    <div class="feature-nodes">${rows || `<p class="muted small">The trace reached no named nodes.</p>`}</div>`;
}

function paintFeatureDetail() {
  const box = $("featureDetail");
  if (!FS.detail) { box.hidden = true; box.innerHTML = ""; return; }
  box.hidden = false;
  box.innerHTML = featureDetailHtml(FS.detail);
}

async function openFeature(name) {
  try {
    const feature = await api(`/api/analysis/feature?slug=${encodeURIComponent(S.slug)}&name=${encodeURIComponent(name)}`);
    if (FS.slug !== S.slug) return;
    FS.detail = feature;
    paintFeatureDetail();
  } catch (e) { toast(esc(e.message), "err"); }
}

async function removeFeature(name) {
  if (FS.busy) return;
  FS.busy = true;
  try {
    await api("/api/analysis/feature/remove", { method: "POST", body: { slug: S.slug, name } });
    FS.detail = null;
    FS.data = null;
    await renderFeatures(true);
    await refreshMap(true);
    toast("Feature removed; work items nobody analyzed are gone.", "match");
  } catch (err) { toast(esc(err.message), "err"); }
  finally { FS.busy = false; }
}

$("featureList").addEventListener("click", async (e) => {
  const row = e.target.closest("[data-feature]");
  if (!row || FS.busy) return;
  FS.busy = true;
  try { await openFeature(row.dataset.feature); } finally { FS.busy = false; }
});
$("featureDetail").addEventListener("click", (e) => {
  const fn = e.target.closest("[data-feature-fn]");
  if (fn) { openFn(fn.dataset.featureFn); return; }
  if (e.target.closest("[data-feature-close]")) { FS.detail = null; paintFeatureDetail(); return; }
  const remove = e.target.closest("[data-feature-remove]");
  if (remove) removeFeature(remove.dataset.featureRemove);
});

function addFeatureDialog() {
  const body = dialog("Trace a feature", `<p class="muted">REA walks the application graph from one literal seed and reports every part it reaches. Each module, channel or endpoint becomes a work item.</p>
    <label class="field"><span>Seed kind</span><select id="featKind">${["route", "string", "api", "channel", "module", "native-export", "node-id"].map((k) => `<option value="${k}">${k}</option>`).join("")}</select></label>
    <label class="field"><span>Seed value</span><input id="featValue" placeholder="/api/update or ipc:check-update or src/main.js"></label>
    <div class="row"><label class="field grow"><span>Match</span><select id="featMatch"><option value="">default</option><option value="exact">exact</option><option value="contains">contains</option></select></label>
      <label class="field grow"><span>Direction</span><select id="featDir"><option value="both">both</option><option value="outgoing">outgoing</option><option value="incoming">incoming</option></select></label></div>
    <label class="field"><span>Name <span class="muted">(optional)</span></span><input id="featName" placeholder="Update flow"></label>
    <pre class="joblog" id="featLog" hidden></pre>
    <div class="row"><button class="primary" id="featGo" type="button">Trace it</button><button class="ghost" data-close type="button">Cancel</button></div>`);
  body.querySelector("#featGo").onclick = async (e) => {
    const value = body.querySelector("#featValue").value.trim();
    if (!value) { toast("Give the feature a seed value.", "err"); return; }
    const log = body.querySelector("#featLog");
    log.hidden = false; log.textContent = "Tracing… a big application can take a few minutes.";
    e.target.disabled = true;
    try {
      const start = await api("/api/analysis/feature/add", { method: "POST", body: {
        slug: S.slug, kind: body.querySelector("#featKind").value, value,
        match: body.querySelector("#featMatch").value || null,
        direction: body.querySelector("#featDir").value, name: body.querySelector("#featName").value.trim(),
      } });
      const result = await followJob(start.job, log);
      closeDialog();
      FS.data = null;
      await renderFeatures(true);
      await refreshMap(true);
      toast(`Feature ${esc(result.name)}: ${result.node_count} nodes, ${result.item_count} work items.`, "match");
    } catch (err) {
      log.textContent = err.message;
      toast(esc(err.message), "err");
      e.target.disabled = false;
    }
  };
}

