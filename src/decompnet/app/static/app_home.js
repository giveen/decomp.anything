/* ------------------------------------------------------------------ home */
let picked = null;

async function showHome() {
  show("home");
  resetDrop();
  const st = await loadState();
  const box = $("projects");
  box.innerHTML = "";
  const shownProjects = st.projects.filter((p) => !p.parent || !st.projects.some((x) => x.slug === p.parent));
  $("recent").hidden = !shownProjects.length;
  for (const p of shownProjects) {
    const pct = p.kind === "analysis" ? (p.functions ? (100 * p.matched) / p.functions : 0) : p.bytes ? (100 * p.matched_bytes) / p.bytes : 0;
    const a = document.createElement("a");
    a.className = "card";
    a.href = `#/p/${p.slug}`;
    if (p.broken) {
      a.innerHTML = `<div class="cc-top"><span class="plat">?</span></div><div><h4>${esc(p.title)}</h4>
        <p class="meta err-text">Can't be opened: ${esc(p.broken)}</p></div>`;
      box.append(a);
      continue;
    }
    const meta = p.kind === "analysis"
      ? `${esc(p.platform_name || "Binary")} · REA${p.entries ? ` · ${Number(p.entries).toLocaleString()} entries` : ""} · ${(p.functions || 0).toLocaleString()} work items`
      : p.kind === "fork"
      ? `${p.origin === "scratch" ? "from scratch" : "public decomp"}${p.version ? " · " + esc(p.version) : ""}${p.ready ? "" : " · needs setup"}`
      : `${esc(p.rom?.game_code || "")}${p.rom?.region ? " · " + esc(p.rom.region) : ""} · ${esc(p.compiler?.name || "")}`;
    a.innerHTML = `<div class="cc-top"><span class="plat">${esc(p.platform_name || "?")}</span><span class="meta">${esc(since(p.last_activity))}</span></div>
      <div><h4>${esc(p.title)}</h4><p class="meta">${meta}</p></div>
      <div class="meter"><i></i></div><p class="meta"><b>${pctNum(pct)}%</b> ${p.kind === "analysis" ? "analyzed" : "of code"} · ${(p.matched || 0).toLocaleString()} / ${(p.functions || 0).toLocaleString()} ${p.kind === "analysis" ? "work items" : "functions"}</p>`;
    box.append(a);
    requestAnimationFrame(() => { a.querySelector(".meter i").style.width = pct + "%"; });
  }
}

function intakeValues() {
  return {
    notes: $("intakeNotes").value.trim(), symbols: $("intakeSymbols").value.trim(),
    types: $("intakeTypes").value.trim(), refs: $("intakeRefs").value.trim(),
    trace: $("intakeTrace").value.trim(), libs: $("intakeLibs").value.trim(),
  };
}
function intakeQuery(values) {
  return Object.entries(values).filter(([, v]) => v).map(([k, v]) => `&${k}=${encodeURIComponent(v)}`).join("");
}
function resetDrop() {
  picked = null;
  $("dropIdle").hidden = false;
  $("dropPicked").hidden = true;
  $("dropBusy").hidden = true;
  $("dropError").hidden = true;
  $("own").checked = false;
  $("ownText").textContent = "This is my own copy of this file, or I'm authorized to analyze it. The results stay with me for my own use.";
  $("analyse").textContent = "Start decompiling";
  $("analyse").disabled = true;
  $("file").value = "";
  for (const id of ["intakeNotes", "intakeSymbols", "intakeTypes", "intakeRefs", "intakeTrace", "intakeLibs"]) $(id).value = "";
}

const drop = $("drop");
drop.addEventListener("click", (e) => { if (!$("dropIdle").hidden && !e.target.closest("button")) $("file").click(); });
drop.addEventListener("keydown", (e) => { if ((e.key === "Enter" || e.key === " ") && !$("dropIdle").hidden && e.target === drop) { e.preventDefault(); $("file").click(); } });
drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); pickFiles(e.dataTransfer.files); });
$("file").addEventListener("change", () => pickFiles($("file").files));
$("own").addEventListener("change", updateAnalyse);
$("cancelPick").onclick = resetDrop;

/* Read the header in the browser so the user sees what they picked before anything happens. */
function parseRom(buf) {
  const b = new Uint8Array(buf);
  const magic = [b[0], b[1], b[2], b[3]].map((x) => x.toString(16).padStart(2, "0")).join("");
  const order = { "80371240": "z64", "37804012": "v64", "40123780": "n64" }[magic];
  if (!order) throw new Error("This doesn't look like an N64 ROM (.z64, .n64 or .v64).");
  const z = new Uint8Array(Math.min(b.length, 0x101000));
  for (let i = 0; i < z.length; i += 4) {
    const w = order === "z64" ? [b[i], b[i + 1], b[i + 2], b[i + 3]] : order === "v64" ? [b[i + 1], b[i], b[i + 3], b[i + 2]] : [b[i + 3], b[i + 2], b[i + 1], b[i]];
    z.set(w, i);
  }
  const text = (from, to) => Array.from(z.slice(from, to)).map((c) => (c >= 32 && c < 127 ? String.fromCharCode(c) : "")).join("").trim();
  const dv = new DataView(z.buffer);
  let jumps = 0, branches = 0;
  for (let i = 0x1000; i + 4 <= z.length; i += 4) {
    const w = dv.getUint32(i);
    if (w >>> 26 === 2) jumps++;
    else if ((w & 0xffff0000) >>> 0 === 0x10000000) branches++;
  }
  const regions = { E: "North America", J: "Japan", P: "Europe", D: "Germany", F: "France", U: "Australia", I: "Italy", S: "Spain", A: "All regions", X: "Europe", Y: "Europe" };
  const code = text(0x3b, 0x3f);
  return {
    title: text(0x20, 0x34) || "Untitled", code, region: regions[code[3]] || "Unknown",
    size: b.length, order, entry: dv.getUint32(8), compiler: branches > jumps ? "IDO" : "KMC",
  };
}

/* What the user dropped, from its first bytes (nothing is uploaded until they confirm). */
const PLATFORM_NAME = { n64: "Nintendo 64", psx: "PlayStation", ps2: "PlayStation 2", playstation: "PlayStation 1 or 2",
  gc: "GameCube", wii: "Wii", gcwii: "GameCube or Wii", gba: "Game Boy Advance", nds: "Nintendo DS",
  windows: "Windows", linux: "Linux" };
// PC programs are split like games, but they are not ROMs and their words are different.
const PC_PLATFORMS = new Set(["windows", "linux", "binary"]);
const isNativeProject = (ov) => !!ov && !!(ov.binary || PC_PLATFORMS.has(ov.platform));
function ownCopyText(info) {
  if (info.platform === "binary" || info.platform === "archive") return "I'm authorized to analyze this file. The results stay with me for my own use.";
  if (PC_PLATFORMS.has(info.platform)) return "This is my own copy of this program, or a binary I'm authorized to analyze. The results stay with me for my own use.";
  return "This is my own copy of this game, obtained legally (for example, dumped from my own console or cartridge). The results stay with me for my own use.";
}
const CW_PACK = {
  gcwii: { id: "mwcc", text: "Install CodeWarrior, the compilers GameCube and Wii games were built with (about 80 MB). It's proprietary software, kept by the decomp community: only if you accept that." },
  nds: { id: "mwccarm", text: "Install CodeWarrior for DS, the compilers DS games were built with (about 50 MB). It's proprietary software, kept by the decomp community: only if you accept that." },
};
function crc16(bytes, from, to) {
  let crc = 0xffff;
  for (let i = from; i < to; i++) {
    crc ^= bytes[i];
    for (let k = 0; k < 8; k++) crc = crc & 1 ? (crc >>> 1) ^ 0xa001 : crc >>> 1;
  }
  return crc;
}
const MB = (n) => (n < 1048576 ? `${Math.max(1, Math.round(n / 1024))} KiB` : `${(n / 1048576).toFixed(n < 10485760 ? 1 : 0)} MiB`);

function identifyHead(head, file) {
  const dv = new DataView(head.buffer);
  const text = (a, b) => String.fromCharCode(...head.slice(a, b));
  const ascii = (a, b) => text(a, b).split("\0")[0].replace(/[^\x20-\x7e]/g, "").trim();
  const stem = file.name.replace(/\.[^.]*$/, "");
  const base = { title: stem, size: file.size };
  if (text(0, 2) === "MZ" && head.length > 0x40) {  // Windows PE: MZ, e_lfanew, "PE\0\0", an x86 machine
    const peAt = dv.getUint32(0x3c, true);
    if (peAt + 6 <= head.length && text(peAt, peAt + 4) === "PE\0\0") {
      const machine = dv.getUint16(peAt + 4, true);
      if (machine === 0x14c || machine === 0x8664) return { ...base, platform: "windows", order: machine === 0x14c ? "PE32 (x86)" : "PE32+ (x86-64)" };
    }
    return null;  // a DOS program or a PE for another CPU: not something this app can split
  }
  if (text(0, 8) === "PS-X EXE") return { ...base, platform: "psx", order: "PS-X EXE", entry: dv.getUint32(0x10, true) };
  if (head.length > 0xc0 && head[0xb2] === 0x96 && head[3] === 0xea) {  // GBA: fixed byte, header checksum, ARM branch
    let sum = 0;
    for (let i = 0xa0; i < 0xbd; i++) sum += head[i];
    if (((-(sum + 0x19)) & 0xff) === head[0xbd]) return { ...base, platform: "gba", order: "ROM", code: ascii(0xac, 0xb0), title: ascii(0xa0, 0xac) || stem };
  }
  if (head.length >= 0x200 && crc16(head, 0, 0x15e) === dv.getUint16(0x15e, true)) {  // DS: the header's own checksum
    const ram = dv.getUint32(0x28, true), entry = dv.getUint32(0x24, true);
    if (ram >= 0x02000000 && ram < 0x02400000 && entry >= ram) return { ...base, platform: "nds", order: "ROM", code: ascii(12, 16), title: ascii(0, 12) || stem, entry };
  }
  if (text(0x8001, 0x8006) === "CD001" || text(0x9319, 0x931e) === "CD001" || text(0x9311, 0x9316) === "CD001") return { ...base, platform: "playstation", order: "disc image" };
  if (text(0, 8) === "MComprHD") return { ...base, platform: "playstation", order: "CHD disc image" };
  if (text(0, 4) === "\x7fELF" && head[5] === 1 && dv.getUint16(18, true) === 8) return { ...base, platform: "ps2", order: "ELF program", entry: dv.getUint32(24, true) };
  if (text(0, 4) === "\x7fELF" && head[5] === 1 && [3, 62].includes(dv.getUint16(18, true)) && [2, 3].includes(dv.getUint16(16, true))) {
    const wide = head[4] === 2;  // ELFCLASS64: the entry point is 8 bytes
    return { ...base, platform: "linux", order: dv.getUint16(18, true) === 62 ? "ELF (x86-64)" : "ELF (x86)",
      entry: wide ? Number(dv.getBigUint64(24, true)) : dv.getUint32(24, true) };
  }
  if (head.length > 0x60 && dv.getUint32(0x1c) === 0xc2339f3d) return { ...base, platform: "gc", order: "disc image", code: ascii(0, 6), title: ascii(0x20, 0x80) || stem };
  if (head.length > 0x60 && dv.getUint32(0x18) === 0x5d1c9ea3) return { ...base, platform: "wii", order: "disc image", code: ascii(0, 6), title: ascii(0x20, 0x80) || stem };
  const packed = { "RVZ\x01": "RVZ", "WIA\x01": "WIA", WBFS: "WBFS", CISO: "CISO" }[text(0, 4)] || (dv.getUint32(0, true) === 0xb10bc001 ? "GCZ" : null);
  if (packed) return { ...base, platform: "gcwii", order: `${packed} disc image` };
  if (head.length >= 0x100) {  // a DOL: section offsets, addresses and sizes that make sense, entry in RAM
    const entry = dv.getUint32(0xe0);
    let used = 0, ok = entry >= 0x80000000 && entry < 0x81800000;
    for (let i = 0; i < 18 && ok; i++) {
      const off = dv.getUint32(i * 4), addr = dv.getUint32(72 + i * 4), size = dv.getUint32(144 + i * 4);
      if (!size) continue;
      used++;
      ok = off >= 0x100 && off + size <= file.size && addr >= 0x80000000 && addr < 0x81800000;
    }
    if (ok && used) return { ...base, platform: "gc", order: "program (DOL)", entry };
  }
  return null;
}

/* Files people try that decomp.anything can't start from: say what they are and why, not just "no". */
function notSupported(head, file) {
  const text = (a, b) => String.fromCharCode(...head.slice(a, b));
  const ext = (file.name.match(/\.([^.]+)$/) || [])[1]?.toLowerCase() || "";
  if (text(0, 2) === "MZ") return "That's a Windows executable, but not a 32-bit or 64-bit x86 PE this app can split.";
  if (text(0, 4) === "\x7fELF") return "That's an ELF program, but not an x86 or x86-64 Linux program (or a PS2 game) this app can split.";
  if (text(0, 4) === "XEX2" || ext === "xex") return "That's an Xbox 360 program. Xbox 360 games aren't supported yet.";
  if (["nsp", "xci", "nso", "nro", "nca"].includes(ext) || text(0, 4) === "NSO0" || text(0, 4) === "PFS0") return "That's a Nintendo Switch file. Switch games aren't supported yet.";
  if (["sfc", "smc"].includes(ext)) return "That's a Super Nintendo ROM. SNES games were written in assembly, not compiled from C, so there's no C source to match.";
  if (["nes", "gb", "gbc", "md", "gen", "sms", "gg", "pce"].includes(ext)) return "Games for that console were written in assembly, not compiled from C, so there's no C source to match.";
  if (ext === "cue") return "That's the .cue sheet: pick the .bin file next to it (the track with the game's data, usually Track 1).";
  if (["7z", "rar"].includes(ext)) return "That's an archive format this app can't inventory yet: use a ZIP, or unpack it and drop the file inside.";
  if (ext === "apk") return "Android apps aren't supported yet.";
  if (ext === "pbp" || ext === "cso") return "That's a PSP file. PSP games aren't supported yet.";
  return null;
}
const NOT_A_GAME = "That isn't a game file decomp.anything can start from scratch (yet). For other platforms, continue a public decomp.";

const COMPANION_NAMES = /^(?:symbol_addrs|symbols)\.txt$|^(?:coverage|trace)\.csv$|^(?:types|context)\.h$/i;
const COMPANION_EXT = /\.(?:pdb|map|sym|dbg|trace|cov)$/i;
function isCompanion(file) {
  return COMPANION_NAMES.test(file.name) || COMPANION_EXT.test(file.name);
}

async function pickFiles(files) {
  const list = [...files].filter(Boolean);
  if (!list.length) return;
  if (list.length === 1) return pickFile(list[0]);
  const companions = list.filter(isCompanion);
  const mains = list.filter((f) => !isCompanion(f));
  if (!mains.length) return toast("Drop the binary as well: the other files are companions.", "err");
  mains.sort((a, b) => b.size - a.size);
  await pickFile(mains[0], companions);
}

async function pickFile(file, companions = []) {
  const head = new Uint8Array(await file.slice(0, 0x9400).arrayBuffer());
  const ext = (file.name.match(/\.([^.]+)$/) || [])[1]?.toLowerCase() || "";
  let info = identifyHead(head, file), buf = file, binary = false;
  const archive = !info && ["zip", "jar", "whl", "ipa", "tar", "tgz"].includes(ext);
  if (archive) {
    binary = true;
    info = { title: file.name, platform: "archive", order: `${ext.toUpperCase()} archive`, size: file.size, code: "", region: "" };
  } else if (!info) {
    const why = notSupported(head, file);
    if (why) return toast(esc(why), "err");
    if (file.size > 64 * 1024 * 1024) return toast("This file is too large to open through the browser: use its path instead.", "err");
    buf = await file.arrayBuffer();
    try { info = parseRom(buf); info.platform = "n64"; } catch (e) {
      binary = true;
      info = { title: file.name, platform: "binary", order: "REA", size: file.size, code: "", region: "" };
      buf = file;
    }
  }
  picked = { buf, info, name: file.name, companions };
  if (!S.st) await loadState().catch(() => {});  // dropped before the page finished loading: the compiler lists
  $("romFile").textContent = file.name;
  $("romTitle").textContent = info.title;
  $("ownText").textContent = ownCopyText(info);
  $("analyse").textContent = "Start decompiling";
  if (binary) {
    const isArchive = info.platform === "archive";
    $("romFacts").innerHTML = [["File", file.name], ["Format", isArchive ? info.order : "REA analysis"], ["Size", MB(file.size)],
      ["Agents", isArchive ? "one work item per file" : "work through its functions"],
      ...(companions.length ? [["With", companions.map((f) => f.name).join(", ")]] : [])]
      .map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join("");
    $("romStatus").hidden = false;
    $("romStatus").className = "plat-status";
    $("romStatus").innerHTML = isArchive
      ? `<b>Archive analysis.</b> REA inventories every file in this archive. Each file becomes a work item, and agents read it and record findings.`
      : `<b>REA analysis.</b> This isn't a game this app can split or match, so REA opens it: its functions become work items, and agents read them and record findings. Matching needs the original compiler and build.`;
    $("ownText").textContent = "I'm authorized to analyze this file. The results stay with me for my own use.";
    $("analyse").textContent = isArchive ? "Analyze archive" : "Analyze with REA";
    $("compilerRow").hidden = true;
    $("compilerNote").hidden = true;
    $("mwConsent").hidden = true;
    $("dropIdle").hidden = true;
    $("dropPicked").hidden = false;
    updateAnalyse();
    return;
  }
  const p = info.platform;
  const facts = p === "n64"
    ? [["Game code", info.code || "—"], ["Region", info.region], ["Size", MB(info.size)], ["Format", info.order],
       ["Entry", hex8(info.entry)], ["Looks like", info.compiler === "IDO" ? "IDO" : "GCC (KMC)"]]
    : [["Platform", PLATFORM_NAME[p]], ["Format", info.order], ["Size", MB(info.size)],
       info.code ? ["Game ID", info.code] : ["Entry", info.entry != null ? hex8(info.entry) : "from the disc"],
       ["Project", ["gc", "wii", "gcwii"].includes(p) ? "dtk-template" : p === "nds" ? "ds-decomp" : p === "gba" ? "luvdis" : p === "windows" ? "PE (functions from .pdata, symbols, calls)" : p === "linux" ? "ELF (functions from .eh_frame, symbols, calls)" : "splat"]];
  $("romFacts").innerHTML = [...facts, ...(companions.length ? [["With", companions.map((f) => f.name).join(", ")]] : [])]
    .map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join("");
  const level = platformStatus(p);
  $("romStatus").hidden = !level;
  $("romStatus").className = `plat-status ${level || ""}`;
  $("romStatus").innerHTML = level === "recommended"
    ? `<b>★ Recommended platform.</b> The most tested path to a full decomp you can port and mod.`
    : PC_PLATFORMS.has(p)
    ? `<b>Beta.</b> ${esc(PLATFORM_NAME[p] || "This platform")} programs work and are tested on smaller ones, less so on real ones yet. Problems are worth <a href="https://github.com/giveen/decomp.anything/issues" target="_blank" rel="noopener">reporting</a>.`
    : `<b>Beta.</b> ${esc(PLATFORM_NAME[p] || "This platform")} works and is tested on homemade games, less so on real ones yet. Problems are worth <a href="https://github.com/giveen/decomp.anything/issues" target="_blank" rel="noopener">reporting</a>.`;
  setupCompilers(info);
  $("dropIdle").hidden = true;
  $("dropPicked").hidden = false;
  updateAnalyse();
}

function setupCompilers(info) {
  const p = info.platform, select = $("compilerPick"), note = $("compilerNote");
  const cw = S.st?.codewarrior || { installed: false, ds_installed: false, gc: [], wii: [], ds: [] };
  $("compilerRow").hidden = false; note.hidden = true; $("mwConsent").hidden = true; $("mwAccept").checked = false;
  if (p === "windows") {  // the server picks MinGW or clang-cl, C or C++, and 32- or 64-bit from the PE itself
    fillCompilers(select, "", "Windows");
    select.insertAdjacentHTML("afterbegin", `<option value="" selected>Automatic · the compiler and architecture the program names</option>`);
  }
  else if (p === "linux") {  // the server picks GCC or Clang, C or C++, from what the program says built it
    fillCompilers(select, "", "Linux");
    select.insertAdjacentHTML("afterbegin", `<option value="" selected>Automatic · the compiler the program names (GCC or Clang, C or C++)</option>`);
  } else if (p === "n64") fillCompilers(select, info.compiler === "IDO" ? "ido-7.1" : "gcc-2.7.2-kmc", "N64");
  else if (p === "psx") fillCompilers(select, "gcc-2.7.2-psx", "PS1");
  else if (p === "ps2") fillCompilers(select, "ee-gcc-2.96", "PS2");
  else if (p === "gba") fillCompilers(select, "agbcc", "GBA");
  else if (p === "playstation") {
    $("compilerRow").hidden = true; note.hidden = false;
    note.textContent = "The compiler is picked from the disc: PSY-Q-era GCC for PlayStation, EE-GCC for PlayStation 2. You can change it later.";
  } else if (p === "nds") {
    select.innerHTML = `<optgroup label="Nintendo DS">${cw.ds.map((v) => `<option value="${esc(v)}" ${v === "DS/2.0/sp1p5" ? "selected" : ""}>CodeWarrior for DS ${esc(v.slice(3))}</option>`).join("")}</optgroup>`;
    note.hidden = false;
    note.textContent = "Not sure? Keep the default; you can change it later in tools/configure.py if functions don't match.";
    $("mwText").textContent = CW_PACK.nds.text;
    $("mwConsent").hidden = cw.ds_installed;
  } else {
    $("mwText").textContent = CW_PACK.gcwii.text;
    const group = (label, list, dflt) => `<optgroup label="${label}">${list.map((v) => `<option value="${esc(v)}" ${v === dflt ? "selected" : ""}>CodeWarrior ${esc(v)}</option>`).join("")}</optgroup>`;
    select.innerHTML = (p !== "wii" ? group("GameCube", cw.gc, "GC/1.3.2") : "") + (p !== "gc" ? group("Wii", cw.wii, p === "wii" ? "Wii/1.0" : "") : "");
    note.hidden = false;
    note.textContent = "Not sure? Keep the default; you can change it later in configure.py if functions don't match.";
    $("mwConsent").hidden = cw.installed;
  }
}

function updateAnalyse() {
  const needsConsent = !$("mwConsent").hidden;
  $("analyse").disabled = !$("own").checked || (needsConsent && !$("mwAccept").checked);
}
$("mwAccept").addEventListener("change", updateAnalyse);

function fillCompilers(select, current, platform) {
  const tcs = (S.st?.toolchains || []).filter((t) => t.kind === "compiler" && (!platform || !t.platform || t.platform === platform));
  select.innerHTML = tcs.map((t) => {
    const note = !t.supported ? " (not available on this computer)" : t.installed ? ""
      : t.system ? " (not found on PATH)" : t.license === "proprietary" ? " (install in Compilers & tools)" : " (will be installed)";
    return `<option value="${esc(t.id)}" ${t.id === current ? "selected" : ""} ${t.supported ? "" : "disabled"}>${esc(t.name)} · ${esc(t.use)}${note}</option>`;
  }).join("");
}

async function runImport(query, file, title, name = "") {
  $("dropIdle").hidden = true;
  $("dropPicked").hidden = true;
  $("dropBusy").hidden = false;
  $("busyTitle").textContent = title;
  $("busyStep").textContent = "starting";
  $("jobLog").textContent = "";
  $("impUp").hidden = !(file instanceof Blob);
  const t0 = Date.now();
  const tick = setInterval(() => {
    const s = Math.round((Date.now() - t0) / 1000);
    $("busyTime").textContent = s >= 3 ? ` · ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : "";
  }, 1000);
  try {
    let started;
    if (file instanceof Blob) {
      started = await uploadFile(`/api/import?${query}&name=${encodeURIComponent(name || file.name || "game.bin")}`, file,
        (p) => { $("impUp").firstElementChild.style.width = (p * 100).toFixed(1) + "%"; $("busyTitle").textContent = p < 1 ? `Copying ${title.replace(/^\w+ /, "")} into decomp.anything… ${(p * 100).toFixed(0)}%` : title; });
    } else {
      started = await api(`/api/import?${query}`, { method: "POST", raw: file ?? new ArrayBuffer(0) });
    }
    $("impUp").hidden = true;
    const res = await followJob(started.job, $("jobLog"), (log) => {
      const last = log.filter((l) => l.trim()).pop();
      if (last) $("busyStep").textContent = last.trim().replace(/\s+/g, " ").slice(0, 120);
    });
    loadNav();
    location.hash = `#/p/${res.slug}`;
  } catch (e) {
    importFailed(name || title.replace(/^\w+ |…$/g, ""), e, $("jobLog").textContent);
  } finally { clearInterval(tick); $("busyTime").textContent = ""; }
}

/* A failed import stays on screen with what happened, ready to report: a toast is gone before it's read. */
function importFailed(name, e, logText) {
  const job = e.job || {};
  const log = (job.log ? job.log.join("\n") : logText || "").trim();
  $("dropIdle").hidden = true; $("dropPicked").hidden = true; $("dropBusy").hidden = true;
  $("dropError").hidden = false;
  $("errName").textContent = name || "the file";
  $("errMsg").textContent = e.message;
  $("errLog").textContent = [log, job.trace].filter(Boolean).join("\n\n") || "(nothing was logged)";
  $("errCopy").onclick = async () => {
    const st = S.st || {};
    const report = [`decomp.anything ${st.version || "?"}${st.build ? ` (${st.build})` : ""} · ${st.runtime || st.platform || navigator.platform}`,
      `Importing: ${name}`, `Error: ${e.message}`, "", "Log:", "```", log.split("\n").slice(-80).join("\n"), job.trace || "", "```"].join("\n");
    try { await navigator.clipboard.writeText(report); toast("Copied: paste it into a GitHub issue"); }
    catch { toast("Couldn't reach the clipboard: select the text under What happened instead", "err"); }
  };
}
$("errBack").onclick = (e) => { e.stopPropagation(); resetDrop(); };

async function ensureCodeWarrior() {
  if ($("mwConsent").hidden) return;
  $("busyTitle").textContent = "Installing CodeWarrior…";
  const pack = picked && picked.info.platform === "nds" ? CW_PACK.nds : CW_PACK.gcwii;
  const { job } = await api("/api/toolchain", { method: "POST", body: { id: pack.id, accept_license: true } });
  await followJob(job, $("jobLog"));
  await loadState();
}

$("analyse").onclick = async () => {
  if (!picked || $("analyse").disabled) return;
  const tc = $("compilerRow").hidden ? "" : $("compilerPick").value;
  const title = `Analysing ${picked.info.title}…`;
  if (!$("mwConsent").hidden) {
    $("dropPicked").hidden = true; $("dropBusy").hidden = false; $("impUp").hidden = true;
    try { await ensureCodeWarrior(); } catch (e) { importFailed("CodeWarrior", e, $("jobLog").textContent); return; }
  }
  const body = picked.buf instanceof Blob ? picked.buf : new Blob([picked.buf]);
  let extra = "";
  if (picked.companions && picked.companions.length) {
    const token = crypto.randomUUID().replace(/-/g, "").slice(0, 16);
    $("dropPicked").hidden = true; $("dropBusy").hidden = false; $("busyTitle").textContent = "Copying companion files…";
    try {
      for (const f of picked.companions) {
        await uploadFile(`/api/companion?token=${token}&name=${encodeURIComponent(f.name)}`, f, (p) => {
          $("busyStep").textContent = `${f.name}: ${(p * 100).toFixed(0)}%`;
        });
      }
      extra = `&companions=${token}`;
    } catch (e) { importFailed(picked.name, e, ""); return; }
  }
  runImport(`confirmed=1${tc ? `&toolchain=${encodeURIComponent(tc)}` : ""}${intakeQuery(intakeValues())}${extra}`, body, title, picked.name);
};
$("pathPick").onclick = (e) => {
  e.stopPropagation();
  const body = dialog("Use a file on this computer", `<p class="muted">decomp.anything reads the file where it is: a disc image is never copied through the browser, and REA can open a binary of any size.</p>
    <label class="field"><span>Path to the file</span><input id="ppPath" class="mono" placeholder="/home/me/games/my-game.iso"></label>
    <details class="intake"><summary><b>Starting context</b> <span class="muted">optional: companions beside the file are added automatically</span></summary>
      <div class="intake-grid">
        <label class="field wide"><span>What is this? Focus, version, engine, known behavior</span><textarea id="ppNotes" rows="2"></textarea></label>
        <label class="field"><span>Debug/symbol file</span><input id="ppSymbols" class="mono"></label>
        <label class="field"><span>Type/header file</span><input id="ppTypes" class="mono"></label>
        <label class="field"><span>Reference source folder</span><input id="ppRefs" class="mono"></label>
        <label class="field"><span>Runtime trace</span><input id="ppTrace" class="mono"></label>
        <label class="field"><span>Known libraries</span><input id="ppLibs" class="mono" placeholder="zlib, libpng"></label>
      </div>
    </details>
    <label class="check"><input type="checkbox" id="ppOwn"><span>This is my own copy of this file, or I'm authorized to analyze it. The results stay with me for my own use.</span></label>
    <div class="row"><button class="primary" id="ppGo" type="button" disabled>Open it</button><button class="ghost" data-close type="button">Cancel</button></div>`);
  body.querySelector("#ppOwn").onchange = () => { $("ppGo").disabled = !$("ppOwn").checked; };
  body.querySelector("#ppGo").onclick = () => {
    const path = $("ppPath").value.trim();
    if (!path) return;
    const intake = { notes: $("ppNotes").value.trim(), symbols: $("ppSymbols").value.trim(),
      types: $("ppTypes").value.trim(), refs: $("ppRefs").value.trim(),
      trace: $("ppTrace").value.trim(), libs: $("ppLibs").value.trim() };
    closeDialog();
    runImport(`confirmed=1&path=${encodeURIComponent(path)}${intakeQuery(intake)}`, undefined, "Opening your binary…");
  };
};
$("demoBtn").onclick = () => runImport("demo=1", undefined, "Opening Demo Quest 64…");

