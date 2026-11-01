/* ------------------------------------------------------------------ feeds */
function fnLink(name) { return `<span class="fn" data-fn="${esc(name)}">${esc(name)}</span>`; }
/* Who did it: the model that did it ("deepseek-chat"), not the session id the store keeps ("api-1"). A CLI session
   with no model picked is named by its app instead, and the id stays a tooltip so two sessions stay apart. */
function whoHtml(agent) {
  if (!agent) return "";
  const s = (S.sessions || []).find((x) => x.agent === agent);
  const label = s ? s.model || s.label : "";
  return label && label !== agent ? `<span class="who" title="${esc(agent)}">${esc(label)}</span>` : `<span class="who">${esc(agent)}</span>`;
}
function eventHtml(ev) {
  const who = whoHtml(ev.agent);
  const d = ev.detail || "";
  switch (ev.kind) {
    case "claim": return `${who} started ${fnLink(d)}`;
    case "match": return `<span class="ok">✓</span> ${who} matched ${fnLink(d)}`;
    case "analyze": return `<span class="ok">✓</span> ${who} analyzed ${fnLink(d)}`;
    case "task": return `${who ? who + " " : ""}added the task ${esc(d)}`;
    case "feature": return `${who ? who + " " : ""}traced the feature ${esc(d)}`;
    case "link": return `<span class="ok">🔗</span> ${esc(d)} is linked: the game builds from its C, byte for byte`;
    case "release": return `${who} set ${fnLink(d)} aside, with notes for the next agent`;
    case "note": return `${who}: ${esc(d)}`;
    case "expired": return `${fnLink(d)} was left unfinished`;
    case "context": return `${who} updated ${ev.agent ? "shared code" : "context"}: ${esc(d)}`;
    case "rename": return `${who} renamed ${esc(d)}`;
    case "split": return `${who} split ${esc(d)}`;
    case "files": return `${who ? who + " " : ""}gave code without a source file one: ${esc(d)}`;
    case "import": return `Target opened: ${esc(d)}`;
    case "compiler": return `compiler set to ${esc(d)}`;
    case "collab": return esc(d);
    default: return `${who} ${esc(ev.kind)} ${esc(d)}`;
  }
}
/* One line per agent and function, updated in place: "claude-opus-4-1 · lerp · 4 compiles · best 87%" until it's matched. */
const groups = new Map();
function groupHtml(g) {
  const who = whoHtml(g.agent);
  const p = pctOf(g.best, g.insns);
  if (g.matched) return `<span class="ok">✓</span> ${who} matched ${fnLink(g.fn)}${g.tries ? ` <span class="score">after ${plural(g.tries, "compile")}</span>` : ""}`;
  if (g.released) return `${who} set ${fnLink(g.fn)} aside, with notes for the next agent${g.tries ? ` <span class="score">${plural(g.tries, "compile")}${p != null ? ` · best ${pctText(p)}` : ""}</span>` : ""}`;
  if (!g.tries) return `${who} started ${fnLink(g.fn)}`;
  const last = g.lastScore === undefined ? "" : g.lastScore === null ? ` · <span class="bad">last one didn't compile</span>` : "";
  return `${who} working on ${fnLink(g.fn)} <span class="score">${plural(g.tries, "compile")}${p != null ? ` · best ${pctText(p)}` : ""}</span>${last}`;
}
/* Feeds put new lines at the top while agents work. Someone scrolled down reading an older entry shouldn't have the
   list move under them: the line still goes in, the list keeps its place, and a pill counts what has arrived. */
const FEED_EDGE = 24;
const feedAway = (list) => list.scrollTop > FEED_EDGE;
function feedSeen(list) {
  const pill = $(list.id + "New");
  if (pill) { pill.hidden = true; pill.dataset.n = "0"; }
}
function feedArrived(list, n = 1) {
  const pill = $(list.id + "New");
  if (!pill) return;
  const total = Number(pill.dataset.n || 0) + n;
  pill.dataset.n = String(total);
  pill.textContent = `${total} new`;
  pill.setAttribute("aria-label", `${plural(total, "new line")} above: jump to them`);
  pill.hidden = false;
}
function prependFeed(list, el, first) {
  const away = !first && feedAway(list);
  const top = list.scrollTop, height = list.scrollHeight;
  list.prepend(el);
  if (!away) return;
  list.scrollTop = top + (list.scrollHeight - height);
  feedArrived(list);
}
for (const id of ["activity", "thoughts"]) {
  const list = $(id), pill = $(id + "New");
  if (!list || !pill) continue;
  pill.onclick = () => { list.scrollTop = 0; feedSeen(list); };
  list.addEventListener("scroll", () => { if (!feedAway(list)) feedSeen(list); }, { passive: true });
}
function addLine(t, html, first) {
  const list = $("activity");
  const li = document.createElement("li");
  if (!first) li.className = "fresh";
  li.innerHTML = `<time>${clock(t)}</time><span>${html}</span>`;
  list.querySelector(".placeholder")?.remove();
  prependFeed(list, li, first);
  return li;
}
function touchGroup(agent, fn, t, change, first) {
  const key = agent + "\u0000" + fn;
  let g = groups.get(key);
  if (!g) { g = { agent, fn, tries: 0, best: null, insns: 1, li: null }; groups.set(key, g); }
  change(g);
  if (g.li && g.li.isConnected) {
    g.li.querySelector("time").textContent = clock(t);
    g.li.querySelector("span").innerHTML = groupHtml(g);
    const list = $("activity");
    if (!feedAway(list) && g.li !== list.firstElementChild) list.prepend(g.li);
    if (!first) { g.li.classList.remove("fresh"); void g.li.offsetWidth; g.li.classList.add("fresh"); }
  } else g.li = addLine(t, groupHtml(g), first);
  if (g.matched || g.released) groups.delete(key);
}

const AUTO = "autopilot";
function autopilotLine(t, first, done) {
  const a = S.apFeed || (S.apFeed = { tried: 0, matched: 0, li: null });
  const html = `<span class="who">autopilot</span> ${done ? esc(done) : `tried ${plural(a.tried, "machine draft")}${a.matched ? ` · <span class="ok">${a.matched} matched</span>` : ""}`}`;
  if (a.li && a.li.isConnected) { a.li.querySelector("time").textContent = clock(t); a.li.querySelector("span").innerHTML = html; }
  else a.li = addLine(t, html, first);
}
function addActivity(events, attempts, first) {
  const items = [...events.map((e) => ({ t: e.created_at, ev: e })), ...attempts.map((a) => ({ t: a.created_at, at: a }))];
  items.sort((a, b) => a.t - b.t);
  for (const it of items) {
    const auto = (who) => who === AUTO || who === "@" + AUTO;  // ours, or the room host's
    if ((it.at && auto(it.at.agent)) || (it.ev && auto(it.ev.agent) && ["match", "claim", "autopilot"].includes(it.ev.kind))) {
      const a = S.apFeed || (S.apFeed = { tried: 0, matched: 0, li: null });
      if (it.at) a.tried++;
      if (it.ev && it.ev.kind === "match") a.matched++;
      autopilotLine(it.t, first, it.ev && it.ev.kind === "autopilot" ? it.ev.detail : null);
      continue;
    }
    if (it.at) {
      const at = it.at;
      touchGroup(at.agent, at.function, it.t, (g) => {
        g.tries++; g.insns = at.n_insns; g.lastScore = at.score;
        if (at.score != null) g.best = g.best == null ? at.score : Math.min(g.best, at.score);
      }, first);
    } else if (it.ev.kind === "claim") touchGroup(it.ev.agent, it.ev.detail, it.t, () => {}, first);
    else if (it.ev.kind === "match") touchGroup(it.ev.agent, it.ev.detail, it.t, (g) => { g.matched = true; }, first);
    else if (it.ev.kind === "release") touchGroup(it.ev.agent, it.ev.detail, it.t, (g) => { g.released = true; }, first);
    else addLine(it.t, eventHtml(it.ev), first);
  }
  const list = $("activity");
  while (list.children.length > 300) list.lastChild.remove();
  list.querySelectorAll(".fn").forEach((x) => { x.onclick = () => openFn(x.dataset.fn); });
  if (!list.children.length) list.innerHTML = `<li class="placeholder"><time></time><span class="muted">Nothing yet. Start an agent and its work shows up here.</span></li>`;
}

/* Agents write Markdown. Escape everything first, then allow only **bold** and `code`. */
function miniMarkdown(text) {
  return esc(text)
    .replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>")
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/^#{1,4} (.*)$/gm, "<b>$1</b>")
    .replace(/^- /gm, "• ");
}

function addThoughts(sessions, first) {
  const list = $("thoughts");
  const items = [];
  for (const s of sessions) for (const f of s.feed) items.push({ ...f, label: s.label });
  items.sort((a, b) => a.t - b.t);
  for (const it of items) {
    const li = document.createElement("li");
    if (!first) li.className = "fresh";
    const time = document.createElement("time"); time.textContent = clock(it.t);
    const src = document.createElement("span"); src.className = "src"; src.textContent = it.label;
    const body = document.createElement("span"); body.className = "t-" + it.type;
    if (it.type === "text") body.innerHTML = miniMarkdown(it.text.slice(0, 1600));
    else body.textContent = it.type === "think" ? it.text.slice(0, 1200) : it.text;
    li.append(time, src, body);
    prependFeed(list, li, first);
  }
  while (list.children.length > 400) list.lastChild.remove();
  if (!list.children.length) list.innerHTML = `<li class="placeholder"><time></time><span class="muted">Helper agents you start from decomp.anything think out loud here.</span></li>`;
  else list.querySelector(".placeholder")?.remove();
}

