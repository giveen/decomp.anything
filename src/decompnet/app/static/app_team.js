/* ------------------------------------------------------------------ team (rooms) */
let teamTimer = null;
async function renderTeam() {
  const box = $("teamBox");
  clearInterval(teamTimer);
  if (!S.ov) return;
  const room = S.ov.room;
  if (room && room.role === "guest") { renderGuest(box, room); teamTimer = setInterval(() => { if (S.tab === "team" && S.ov.room) renderGuest(box, S.ov.room); }, 4000); return; }
  if (S.ov.kind === "fork" && !S.ov.ready) {
    box.innerHTML = `<h2 class="pane-title">Decompile together</h2>
      <p class="muted">Set the project up first (Overview): then friends who own the game can join it from their own decomp.anything.</p>
      <div class="row"><button class="ghost" id="tmJoin" type="button">Join a team's room</button></div>`;
    $("tmJoin").onclick = openJoin;
    return;
  }
  if (!box.firstElementChild) box.innerHTML = `<p class="muted">Looking at this computer's network…</p>`;
  const info = await api(`/api/collab?slug=${S.slug}`).catch((e) => { box.innerHTML = `<p class="bad">${esc(e.message)}</p>`; return null; });
  if (!info || S.tab !== "team") return;
  renderHost(box, info);
  teamTimer = setInterval(async () => {
    if (S.tab !== "team" || !S.slug) { clearInterval(teamTimer); return; }
    if (box.contains(document.activeElement) && document.activeElement.matches("input")) return;  // typing
    const inf = await api(`/api/collab?slug=${S.slug}`).catch(() => null);
    if (inf && inf.sharing && S.tab === "team") renderHost(box, inf);
  }, 5000);
}

function renderGuest(box, room) {
  const members = room.members || [];
  const waiting = /waiting for the host/i.test(room.error || "");
  const people = members.map((m) => `<div class="line-item"><span class="dot ${m.online ? "on" : ""}"></span><div class="txt"><b>${esc(m.name)}${m.name === room.member ? " (you)" : ""}</b>
      <span>${m.agents && m.agents.length ? esc(m.agents.join(", ")) : "no agents yet"} · ${m.matched || 0} matched${m.working && m.working.length ? ` · on ${esc(m.working.slice(0, 3).join(", "))}` : ""}</span></div></div>`).join("");
  const syncing = !!room.syncing;
  const last = room.last_sync == null ? "never"
    : room.last_sync < 90 ? `${Math.max(0, Math.round(room.last_sync))}s ago`
      : `${Math.round(room.last_sync / 60)} min ago`;
  const skipped = (room.layout && room.layout.skipped) || {};
  const missed = (skipped.no_owner || 0) + (skipped.matched || 0) + (skipped.unsplittable || 0);
  const sample = (room.layout && room.layout.sample) || [];
  const missedNote = missed ? `<div class="notice"><span>${missed} room function${missed === 1 ? "" : "s"} couldn't be applied here: the two splits differ${sample.length ? ` (${sample.map((s) => esc(s)).join(", ")}${missed > sample.length ? ", …" : ""})` : ""}. The room keeps them; this copy doesn't get those matches until its split matches the host's.</span></div>` : "";
  box.innerHTML = `<h2 class="pane-title">${waiting ? "Waiting for the host to let you in" : syncing ? "Syncing the room's work…" : room.connected ? "In a team room" : "The room is offline"}</h2>
    <p class="muted">${syncing ? `The host is sending the room's work: a big room's first sync takes a moment. Your agents start as soon as it lands.`
      : waiting ? `You asked to join as <b>${esc(room.member)}</b>. Your agents start working with the room as soon as the host lets you in.`
      : room.connected ? `You're <b>${esc(room.member)}</b>. Your agents claim functions through the room, so nobody does the same one twice; matches are checked and shared by the host.`
      : `${esc(room.error || "Waiting for the host…")} ${room.failures ? `Retrying in ${Math.ceil(room.retry_in)}s;` : ""} your agents wait and carry on when it's back.`}</p>
    <dl class="facts"><div><dt>Host</dt><dd>${esc(room.host)}</dd></div><div><dt>Last sync</dt><dd>${last}${room.sync_ms ? ` · ${room.sync_ms} ms` : ""}</dd></div><div><dt>Members</dt><dd>${members.length}</dd></div><div><dt>Online</dt><dd>${members.filter((m) => m.online).length}</dd></div></dl>
    ${missedNote}
    ${people ? `<div class="setup-group"><h3>In the room</h3>${people}</div>` : ""}
    <div class="row"><button class="ghost" id="roomReconnect" type="button">${room.connected ? "The host shared a new link?" : "Reconnect with a new link"}</button><button class="ghost danger" id="roomLeave" type="button">Leave room</button></div>`;
  $("roomReconnect").onclick = openReconnect;
  $("roomLeave").onclick = async () => {
    if (!confirm("Leave this room? Your local copy of the project stays; it stops syncing.")) return;
    await api("/api/collab/leave", { method: "POST", body: { slug: S.slug } }).catch((e) => toast(esc(e.message), "err"));
  };
}

function renderHost(box, info) {
  const intro = `<h2 class="pane-title">Decompile together</h2>
    <p class="muted">Your team works on this game from their own computers, each with <b>their own copy of exactly this game</b> (decomp.anything checks it's identical) and their own agents. This computer is the room's host: it keeps the shared work and checks every match against your copy.${S.ov.kind === "fork" ? " Members get this project's source tree from you (only what git tracks: never your game or its extracted files) and follow every commit made here." : ""} Only the work travels, never ROM data. You host the room and decide who's in.</p>`;
  const opts = (inf) => `<div class="setup-group"><h3>Who can join</h3>
      <div class="seg" id="visSeg"><button type="button" data-vis="private" class="${inf.settings.visibility !== "public" ? "on" : ""}">Private: invite link</button><button type="button" data-vis="public" class="${inf.settings.visibility === "public" ? "on" : ""}">Public: anyone with the address</button></div>
      <label class="check"><input type="checkbox" id="apprChk" ${inf.settings.approve ? "checked" : ""}><span>I let each new member in (they wait until you approve)</span></label></div>`;
  if (!info.sharing) {
    const sugg = (info.addresses || []).map((a) => `<button type="button" class="chip" data-addr="${esc(a.address)}">${esc(a.label)}: <span>${esc(a.address)}</span></button>`).join("");
    box.innerHTML = `${intro}${info.was_sharing ? `<div class="notice"><span>This room was shared before the app restarted. Share it again: members keep their place.</span></div>` : ""}
      <div class="setup-group"><h3>How teammates reach this computer</h3>
        <label class="check"><input type="radio" name="mode" value="tunnel" ${info.mode !== "address" ? "checked" : ""} ${info.tunnel_supported ? "" : "disabled"}><span><b>Quick tunnel</b>: works from anywhere, nothing to set up (Cloudflare's free tunnel) — so everyone's source, notes and progress pass through Cloudflare's servers on the way between you, and the invite link is the only thing keeping them private. On a network you control, the address option keeps the room on your LAN. The address changes when the app restarts; members then paste the new link.</span></label>
        <label class="check"><input type="radio" name="mode" value="address" ${info.mode === "address" ? "checked" : ""}><span><b>An address you control</b>: this network, Tailscale, or a server with its own domain. It stays the same, so the room reopens by itself after a restart.</span></label>
        <div id="addrBox" ${info.mode === "address" ? "" : "hidden"}>
          <label class="field"><span>The address teammates use (the room listens on port <input id="addrPort" class="mono port" type="number" value="${info.port || 6481}">)</span><input id="addrIn" class="mono" placeholder="http://192.168.1.20:6481" value="${esc(info.address || "")}"></label>
          <div class="chips">${sugg}</div></div></div>
      ${opts(info)}
      <div class="row"><button class="primary" id="startShare" type="button">Start sharing</button></div><pre class="joblog" id="shareLog" hidden></pre>`;
    box.querySelectorAll('[name="mode"]').forEach((r) => { r.onchange = () => { box.querySelector("#addrBox").hidden = r.value !== "address" || !r.checked; }; });
    box.querySelectorAll("[data-addr]").forEach((b) => { b.onclick = () => { box.querySelector("#addrIn").value = b.dataset.addr; }; });
    box.querySelectorAll("[data-vis]").forEach((b) => { b.onclick = () => box.querySelectorAll("[data-vis]").forEach((x) => x.classList.toggle("on", x === b)); });
    box.querySelector("#startShare").onclick = async () => {
      const mode = box.querySelector('[name="mode"]:checked')?.value || "tunnel";
      const visibility = box.querySelector("[data-vis].on")?.dataset.vis || "private";
      box.querySelector("#shareLog").hidden = false;
      box.querySelector("#startShare").disabled = true;
      try {
        const { job } = await api("/api/collab/start", { method: "POST", body: { slug: S.slug, mode, visibility,
          approve: box.querySelector("#apprChk").checked, address: box.querySelector("#addrIn")?.value || "",
          port: Number(box.querySelector("#addrPort")?.value) || 6481, keep: info.was_sharing } });
        await followJob(job, box.querySelector("#shareLog"));
        renderHost(box, await api(`/api/collab?slug=${S.slug}`));
      } catch (e) { toast(esc(e.message), "err"); box.querySelector("#startShare").disabled = false; }
    };
    return;
  }
  const members = info.room?.members || [];
  const online = members.filter((m) => m.online).length;
  const people = members.map((m) => `<div class="line-item"><span class="dot ${m.online ? "on" : ""}"></span><div class="txt"><b>${esc(m.name)}</b>
      <span>${m.agents.length ? esc(m.agents.join(", ")) : "no agents yet"} · ${m.matched} matched${m.working.length ? ` · on ${esc(m.working.slice(0, 3).join(", "))}` : ""}</span></div>
      <button class="ghost small-btn danger" data-remove="${esc(m.name)}" type="button">Remove</button></div>`).join("");
  const waiting = (info.pending || []).map((p) => `<div class="line-item"><div class="txt"><b>${esc(p.name)}</b><span>asks to join</span></div>
      <button class="primary small-btn" data-approve="${esc(p.name)}" type="button">Let in</button><button class="ghost small-btn" data-decline="${esc(p.name)}" type="button">Decline</button></div>`).join("");
  box.innerHTML = `${intro}<label class="field"><span>${info.settings.visibility === "public" ? "The room's address (anyone with it and their own copy of the game can join)" : "Invite link (send it to teammates who own this game)"}</span></label>${cmdBox(info.link || "")}
    <p class="muted small">${info.mode === "address" ? "Shared at your own address: the room reopens by itself when this app restarts." : "Shared through a quick tunnel: the address changes if this app restarts."} Teammates choose <i>Join a team</i> in decomp.anything, or run <code>decomp join &lt;link&gt; &lt;their copy&gt; --name &lt;name&gt;</code>.</p>
    ${waiting ? `<div class="setup-group"><h3>Waiting to join</h3>${waiting}</div>` : ""}
    <div class="setup-group"><h3>In the room · ${members.length} member${members.length === 1 ? "" : "s"}, ${online} online</h3>${people || `<p class="muted small">Nobody yet.</p>`}</div>
    ${opts(info)}
    <div class="setup-group"><h3>Keep it open</h3><div class="row" id="svcRow"><span class="muted small">Checking…</span></div></div>
    <div class="row"><button class="ghost" id="rotate" type="button">New invite link</button><button class="ghost danger" id="stopShare" type="button">Stop sharing</button></div>`;
  wireCopies(box);
  const manage = async (payload) => {
    try { await api("/api/collab/manage", { method: "POST", body: { slug: S.slug, ...payload } }); renderHost(box, await api(`/api/collab?slug=${S.slug}`)); }
    catch (e) { toast(esc(e.message), "err"); }
  };
  box.querySelectorAll("[data-remove]").forEach((b) => { b.onclick = () => { if (confirm(`Remove ${b.dataset.remove} from the room? Their agents stop working with it.`)) manage({ action: "remove", name: b.dataset.remove }); }; });
  box.querySelectorAll("[data-approve]").forEach((b) => { b.onclick = () => manage({ action: "approve", name: b.dataset.approve }); });
  box.querySelectorAll("[data-decline]").forEach((b) => { b.onclick = () => manage({ action: "decline", name: b.dataset.decline }); });
  box.querySelectorAll("[data-vis]").forEach((b) => { b.onclick = () => manage({ action: "settings", visibility: b.dataset.vis }); });
  box.querySelector("#apprChk").onchange = (e) => manage({ action: "settings", approve: e.target.checked });
  box.querySelector("#rotate").onclick = () => { if (confirm("Make a new invite link? The old one stops working for people who haven't joined yet; members stay in.")) manage({ action: "rotate" }); };
  box.querySelector("#stopShare").onclick = async () => {
    await api("/api/collab/stop", { method: "POST", body: { slug: S.slug } });
    renderHost(box, await api(`/api/collab?slug=${S.slug}`));
  };
  serviceRow(box.querySelector("#svcRow"), "The room is open while decomp.anything runs.");
}

/* Start decomp.anything with the computer (systemd user service, launchd, or a log-on task). */
function serviceRow(row, offText) {
  if (!row) return;
  if (S.st && S.st.runtime === "docker") { row.innerHTML = `<span class="muted small">Docker keeps decomp.anything running in the background.</span>`; return; }
  if (S.st && S.st.runtime === "wsl") { row.innerHTML = `<span class="muted small">decomp.anything runs while its window is open (the Start menu's decomp.anything).</span>`; return; }
  api("/api/service").then((sv) => {
    if (!row.isConnected) return;
    row.innerHTML = sv.installed ? `<span class="ok-mark">decomp.anything starts with this computer</span><button class="ghost small-btn" data-svc="remove" type="button">Don't</button>`
      : `<span class="muted small grow">${esc(offText)}</span><button class="ghost small-btn" data-svc="install" type="button">Start decomp.anything with this computer</button>`;
    row.querySelector("[data-svc]").onclick = async (e) => {
      try {
        const r = await api("/api/service", { method: "POST", body: { action: e.target.dataset.svc } });
        if (r.message) toast(esc(r.message));
        serviceRow(row, offText);
      } catch (err) { toast(esc(err.message), "err"); }
    };
  }).catch(() => { row.innerHTML = ""; });
}

function openReconnect() {
  const body = dialog("Reconnect to the room", `<p class="muted">When the host restarts decomp.anything with a quick tunnel, their invite link changes. Paste the new one; you stay the same member and keep everything you've done.</p>
    <label class="field"><span>New invite link</span><input id="rLink" class="mono" placeholder="https://….trycloudflare.com/join/…"></label>
    <div class="row"><button class="primary" id="rGo" type="button">Reconnect</button></div><pre class="joblog" id="rLog" hidden></pre>`);
  body.querySelector("#rGo").onclick = async () => {
    if (!$("rLink").value.trim()) return toast("Paste the host's new invite link.", "err");
    $("rLog").hidden = false;
    $("rGo").disabled = true;
    try {
      const { job } = await api("/api/collab/reconnect", { method: "POST", body: { slug: S.slug, link: $("rLink").value.trim() } });
      await followJob(job, $("rLog"));
      closeDialog();
      toast("Back in the room.", "match");
    } catch (e) { toast(esc(e.message), "err"); $("rGo").disabled = false; }
  };
}

function openJoin() {
  const body = dialog("Join a team's room", `<p class="muted">Paste the invite link (or a public room's address), then pick <b>your own copy</b> of the same game. decomp.anything checks it's identical to the host's; your copy itself is never sent.</p>
    <label class="field"><span>Invite link</span><input id="jLink" class="mono" placeholder="https://….trycloudflare.com/join/…"></label>
    <label class="field"><span>Your name in the room</span><input id="jName" maxlength="24" placeholder="e.g. alex"></label>
    <label class="field"><span>Your copy of the game</span><input id="jRom" type="file"></label>
    <label class="field"><span>…or its path on this computer (best for a GameCube or Wii disc: it's read where it is)</span><input id="jPath" class="mono" placeholder="${S.st && S.st.host_os === "windows" ? "C:\\Games\\game.rvz" : "/home/you/games/game.iso"}"></label>
    <label class="check"><input type="checkbox" id="jOwn"><span>This is my own copy of this game, obtained legally. I'll keep what the room makes private.</span></label>
    <p class="muted small">A GameCube, Wii or DS project comes with its whole source tree from the host, and is then built here with your copy and your compilers: the first time takes a few minutes.</p>
    <div class="row"><button class="primary" id="jGo" type="button">Join</button></div><pre class="joblog" id="jLog" hidden></pre>`);
  body.querySelector("#jGo").onclick = async () => {
    const file = $("jRom").files[0], path = $("jPath").value.trim();
    if (!$("jLink").value.trim() || !/^[A-Za-z0-9_-]{1,24}$/.test($("jName").value) || !(file || path) || !$("jOwn").checked) {
      return toast("Fill in the link, a name (letters, digits, - or _), your copy of the game, and confirm it's your own.", "err");
    }
    $("jLog").hidden = false;
    $("jGo").disabled = true;
    try {
      let q = `link=${encodeURIComponent($("jLink").value.trim())}&name=${encodeURIComponent($("jName").value)}&confirmed=1`;
      q += path ? `&path=${encodeURIComponent(path)}` : `&filename=${encodeURIComponent(file.name)}`;
      // the file streams from disk as it's sent: a disc image is never read into the page whole
      const { job } = await api(`/api/collab/join?${q}`, { method: "POST", raw: path ? new Blob([]) : file });
      const res = await followJob(job, $("jLog"));
      closeDialog();
      loadNav();
      location.hash = `#/p/${res.slug}`;
    } catch (e) { toast(esc(e.message), "err"); $("jGo").disabled = false; }
  };
}

async function installToolchain(id, proprietary) {
  if (proprietary && !confirm("This downloads a proprietary compiler, kept by the decomp community. Install it only if you accept that. Continue?")) return;
  toast("Installing…");
  try {
    const { job } = await api("/api/toolchain", { method: "POST", body: { id, accept_license: proprietary } });
    await followJob(job);
    toast("Installed.", "match");
    await loadState();
    if (S.slug) renderStats(await api(`/api/project?slug=${S.slug}`));
    if (!$("toolsPage").hidden) renderTools();
  } catch (e) {
    if (/WSL 2 (?:isn't installed|has no Linux distribution|didn't start)|WSL 1 can't run/.test(e.message)) {
      if (!showWslSetup(e.message, id, proprietary)) {
        toast(`${esc(e.message)} <a href="https://aka.ms/wslinstall" target="_blank" rel="noopener">Microsoft's WSL install guide</a>`, "err");
      }
      return;
    }
    toast(esc(e.message), "err");
  }
}

/* A WSL failure must stay on screen: installing WSL needs an administrator PowerShell and a restart. */
function showWslSetup(message, id, proprietary) {
  const n = $("compilerNotice");
  if (!n || n.hidden) return false;
  const command = /switch to WSL 2/.test(message) ? "wsl --set-default-version 2" : "wsl --install";
  n.hidden = false;
  n.className = "notice bad";
  n.innerHTML = `<span><b>WSL 2 needs setup.</b> ${esc(message)}</span><span class="grow"></span>` +
    `<button class="small-btn" id="wslCopy" type="button">Copy command</button>` +
    `<a class="small-btn" href="https://aka.ms/wslinstall" target="_blank" rel="noopener">Microsoft guide</a>` +
    `<button class="primary small-btn" id="wslRetry" type="button">Try again</button>`;
  $("wslCopy").onclick = async () => {
    try { await navigator.clipboard.writeText(command); toast("Copied. Run it in an administrator PowerShell.", "match"); }
    catch { toast(command, "match"); }
  };
  $("wslRetry").onclick = () => installToolchain(id, proprietary);
  return true;
}

