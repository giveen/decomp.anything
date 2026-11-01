/* Load the page's scripts the way the browser does and smoke it: every script evaluates, in order, in one shared
   scope, the page boots, and two views render from canned API responses.

   What this protects is the property a split can break and a source comparison cannot see: the scripts share one global
   scope and run top-level code (theme, state, routing, polling), so a `const` that moves to a later file is a
   ReferenceError that only appears at load. The script list comes from index.html, so this test needs no edit when
   app.js becomes several files -- it loads whatever the page loads, in the page's order.

   No dependency, no browser: node:vm and a permissive DOM stub, like tests/console_reconnect.cjs. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

// APP_STATIC lets a test point the harness at a copy of the page (a split, or one with its scripts misordered).
const STATIC = process.env.APP_STATIC || path.join(__dirname, "../src/decompnet/app/static");

/* ------------------------------------------------------------------ the page's scripts, in load order */

function pageScripts() {
  const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
  const names = [...html.matchAll(/<script\s+src="\/assets\/([^"?]+)(?:\?[^"]*)?"/g)].map((m) => m[1]);
  assert.ok(names.length, "index.html loads no scripts");
  return names.map((name) => {
    const file = path.join(STATIC, name);
    assert.ok(fs.existsSync(file), `index.html loads ${name}, which does not exist`);
    return { name, source: fs.readFileSync(file, "utf8") };
  });
}

/* ------------------------------------------------------------------ a browser, as much as the page touches */

const CANNED = {
  "/api/state": {
    version: "0.11.0", build: "abc123", home: "/home/user", platform: "linux", platform_name: "Linux",
    projects: [
      { slug: "demo-rom", title: "Demo Quest 64", kind: "rom", platform: "n64", platform_name: "N64",
        functions: 4, matched: 2, bytes: 4096, matched_bytes: 2048, last_activity: 1 },
      { slug: "demo-analysis", title: "Some App", kind: "analysis", platform: "windows", platform_name: "Windows",
        functions: 10, matched: 4, entries: 12, last_activity: 2 },
    ],
    toolchains: [], agents: [], api_presets: [], settings: {}, mcp_url: "http://127.0.0.1:1/mcp",
    platform_status: { n64: "recommended" }, codewarrior: { installed: false, ds_installed: false },
  },
  "/api/projects": [
    { slug: "demo-rom", title: "Demo Quest 64", kind: "rom", platform_name: "N64", functions: 4, matched: 2, working: 0 },
  ],
  "/api/catalog": { projects: [{ repo: "decomp/demo", name: "Demo", platform: "n64" }], downloaded: 1 },
  "/api/update": { disabled: true, available: false, current: {}, job: { state: "idle" },
                   rea: { disabled: true, available: false, current: {}, job: { state: "idle" } } },
  "/health": { version: "0.11.0" },
  "/api/project": {
    slug: "demo-rom", title: "Demo Quest 64", kind: "rom", platform: "n64", platform_name: "N64", ready: true,
    entries: [], files: 1, functions: 4, matched: 2, bytes: 4096, matched_bytes: 2048,
    compiler: { toolchain: "gcc-2.7.2-kmc", name: "KMC GCC 2.7.2", flags: [] },
    rom: { game_code: "NDQE", region: "USA", program: "demo.z64" }, context: "", instructions: "",
    room: null, autopilot: null,
  },
  "/api/live": { e: 0, a: 0, sessions: [], claims: [], events: [], functions: [] },
  "/api/map": { signature: "s", functions: [] },
  "/api/stats": { functions: 4, matched: 2, bytes: 4096, matched_bytes: 2048, library: 0, insns: 100, hits: 0, traced: 0 },
  "/api/sources": { slug: "demo-rom", kind: "rom", tier: "match", files: [], counts: {} },
  "/api/activity": { rows: [] },
  "/api/agents": { running: [] },
  "/api/rea": { configured: false, running: false, settings: {}, providers: [], connection: null, target: null },
};

function respond(url) {
  const pathname = url.split("?")[0];
  if (pathname in CANNED) return { ok: true, status: 200, body: CANNED[pathname] };
  return { ok: false, status: 404, body: { error: `no canned response for ${url}` } };
}

function element(name) {
  const el = {
    name, innerHTML: "", textContent: "", value: "", title: "", hidden: true, disabled: false, checked: false,
    scrollTop: 0, scrollHeight: 0, clientWidth: 1024, clientHeight: 768, offsetWidth: 1024, offsetHeight: 768,
    width: 1024, height: 768, dataset: {}, style: {}, files: [], children: [],
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    append(...nodes) { this.children.push(...nodes); },
    appendChild(node) { this.children.push(node); return node; },
    insertBefore(node) { this.children.push(node); return node; },
    removeChild() {}, remove() {}, replaceChildren() {}, insertAdjacentHTML() {},
    addEventListener() {}, removeEventListener() {}, dispatchEvent() {},
    setAttribute() {}, getAttribute: () => null, removeAttribute() {}, hasAttribute: () => false,
    querySelector: () => element(`${name} >`), querySelectorAll: () => [],
    closest: () => null, matches: () => false, contains: () => false,
    focus() {}, blur() {}, click() {}, scrollTo() {}, scrollIntoView() {},
    getBoundingClientRect: () => ({ top: 0, left: 0, right: 1024, bottom: 768, width: 1024, height: 768 }),
    getContext: () => context2d,
    toDataURL: () => "data:,",
  };
  return el;
}

const context2d = new Proxy({}, {
  get(_, key) {
    if (key === "measureText") return () => ({ width: 10 });
    if (key === "createLinearGradient" || key === "createRadialGradient") return () => ({ addColorStop() {} });
    if (key === "getImageData") return () => ({ data: new Uint8ClampedArray(4) });
    return () => undefined;
  },
  set: () => true,
});

function browser() {
  const elements = new Map();
  const byId = (id) => {
    if (!elements.has(id)) elements.set(id, element(id));
    return elements.get(id);
  };
  const fetches = [];
  const timers = { interval: [], timeout: [], frame: [] };
  const storage = () => {
    const data = new Map();
    return { getItem: (k) => (data.has(k) ? data.get(k) : null), setItem: (k, v) => data.set(k, String(v)),
             removeItem: (k) => data.delete(k), clear: () => data.clear(), key: (i) => [...data.keys()][i] ?? null,
             get length() { return data.size; } };
  };
  const document = {
    getElementById: byId, createElement: (tag) => element(`<${tag}>`),
    createTextNode: (text) => ({ textContent: text }),
    createDocumentFragment: () => element("<fragment>"),
    querySelector: (sel) => byId(sel), querySelectorAll: () => [],
    addEventListener() {}, removeEventListener() {},
    body: byId("body"), documentElement: byId("html"), head: byId("head"),
    activeElement: byId("active"), cookie: "", readyState: "complete", title: "decomp.anything",
  };
  const noop = () => {};
  const env = {
    document,
    window: null,
    navigator: { platform: "Linux", userAgent: "node", clipboard: { writeText: async () => {} } },
    location: { hash: "", pathname: "/", search: "", href: "http://127.0.0.1/", reload: noop, assign: noop },
    history: { replaceState: noop, pushState: noop, back: noop },
    localStorage: storage(), sessionStorage: storage(),
    matchMedia: () => ({ matches: false, addEventListener: noop, removeEventListener: noop, addListener: noop }),
    requestAnimationFrame: (fn) => { timers.frame.push(fn); return timers.frame.length; },
    cancelAnimationFrame: noop,
    setTimeout: (fn, ms) => { timers.timeout.push({ fn, ms }); return timers.timeout.length; },
    clearTimeout: noop,
    setInterval: (fn, ms) => { timers.interval.push({ fn, ms }); return timers.interval.length; },
    clearInterval: noop,
    fetch: async (url, options = {}) => {
      const answer = respond(String(url));
      fetches.push({ url: String(url), method: options.method || "GET", ok: answer.ok });
      return { ok: answer.ok, status: answer.status, headers: new Map(),
               json: async () => answer.body, text: async () => JSON.stringify(answer.body) };
    },
    alert: noop, confirm: () => true, prompt: () => null,
    addEventListener: noop, removeEventListener: noop, dispatchEvent: noop,
    scrollTo: noop, getComputedStyle: () => ({ getPropertyValue: () => "" }),
    devicePixelRatio: 1, innerWidth: 1024, innerHeight: 768, open: noop, close: noop, focus: noop,
    crypto: { randomUUID: () => "00000000-0000-4000-8000-000000000000" },
    performance: { now: () => 0, mark: noop, measure: noop },
    Image: function Image() { return element("<img>"); },
    ResizeObserver: function ResizeObserver() { return { observe: noop, disconnect: noop }; },
    IntersectionObserver: function IntersectionObserver() { return { observe: noop, disconnect: noop }; },
    console,
    URL, URLSearchParams, TextEncoder, TextDecoder, Blob: function Blob() {}, FormData: function FormData() {},
    atob: (s) => Buffer.from(s, "base64").toString("binary"),
    btoa: (s) => Buffer.from(s, "binary").toString("base64"),
  };
  env.globalThis = env;
  env.window = env;
  env.self = env;
  env.document.defaultView = env;
  return { env, fetches, timers, byId, elements };
}

/* ------------------------------------------------------------------ run it */

async function settle(turns = 12) {
  for (let i = 0; i < turns; i++) await Promise.resolve();
  await new Promise((done) => setImmediate(done));
}

async function main() {
  const scripts = pageScripts();
  const { env, fetches, timers, byId } = browser();
  const context = vm.createContext(env, { name: "decomp.anything" });

  /* 1. Every script the page loads evaluates, in order, in one shared scope. A move that changes load order or drops a
        declaration fails here, by file name. */
  for (const script of scripts) {
    try {
      vm.runInContext(script.source, context, { filename: script.name });
    } catch (e) {
      const where = e && e.stack ? e.stack.split("\n")[0] : "";
      throw new Error(`${script.name} threw while loading: ${e && e.name ? e.name + ": " : ""}${e && e.message ? e.message : e} (${where})`);
    }
  }
  await settle();  // the boot's async work (loadState, loadNav) runs on microtasks

  /* 2. The page's entry points are reachable the way the next <script> reaches them. Top-level const/let are
        script-scoped by design (they are not window properties even in a browser), so a probe evaluated in the same
        realm is what proves the pieces share a scope: this is exactly what console.js and a later part of app.js do. */
  const required = ["$", "esc", "api", "route", "show", "showHome", "openProject", "setTab", "loadState", "loadNav",
                    "renderNav", "markNav", "toast", "poll", "closeAll", "S", "TABS", "VIEWS"];
  const reachable = vm.runInContext(
    `globalThis.__page = { ${required.map((n) => `${JSON.stringify(n)}: typeof ${n} === "undefined" ? undefined : ${n}`).join(", ")} };
     globalThis.__page`, context, { filename: "probe.js" });
  const missing = required.filter((name) => reachable[name] === undefined);
  assert.deepEqual(missing, [], `names the page's scripts need are not reachable from a later script: ${missing.join(", ")}`);
  for (const name of ["$", "esc", "api", "route", "showHome", "openProject", "setTab"]) {
    assert.equal(typeof reachable[name], "function", `${name} is not a function`);
  }
  assert.ok(Array.isArray(reachable.S.projects), "S (the page's state) is not the object the page uses");

  /* 3. Boot ran to the end of the file: the page fetched its state and drew the sidebar's version, and the polling
        intervals the tail registers are there. (A truncated or misordered split loses the tail.) */
  const urls = fetches.map((f) => f.url.split("?")[0]);
  assert.ok(urls.includes("/api/state"), `boot never asked for /api/state (fetched: ${urls.join(", ")})`);
  assert.ok(urls.includes("/api/projects"), "boot never asked for /api/projects (loadNav)");
  assert.equal(byId("navVersion").textContent, "v0.11.0", "the version never reached the sidebar");
  assert.ok(timers.interval.length >= 2, `the page's polling timers were never registered (${timers.interval.length})`);

  /* 4. The home view rendered the projects it was given, through the DOM, not just without throwing. */
  const cards = byId("projects").children;
  assert.equal(cards.length, 2, `the home view drew ${cards.length} project cards, expected 2`);
  assert.ok(cards.some((c) => c.innerHTML.includes("Demo Quest 64")), "no card carries the project's title");

  /* 5. Routing reaches another view: #/catalog swaps the visible view and asks for the catalog. */
  env.location.hash = "#/catalog";
  reachable.route();
  await settle();
  assert.equal(byId("catalog").hidden, false, "#/catalog did not show the catalog view");
  assert.equal(byId("home").hidden, true, "#/catalog left the home view visible");
  assert.ok(fetches.some((f) => f.url.startsWith("/api/catalog")), "the catalog view fetched nothing");

  /* 6. Nothing asked for an endpoint the page is not known to use: a section wired to the wrong route after a move
        shows up here rather than as an empty panel. */
  const unexpected = [...new Set(fetches.filter((f) => !f.ok).map((f) => f.url))];
  assert.deepEqual(unexpected, [], `the page called endpoints with no canned response: ${unexpected.join(", ")}`);

  console.log(`ok: ${scripts.length} script(s) loaded (${scripts.map((s) => s.name).join(", ")}), ` +
              `${fetches.length} requests, home + catalog rendered`);
}

main().catch((e) => {
  console.error(e && e.message ? e.message : e);
  process.exit(1);
});
