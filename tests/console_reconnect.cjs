/* Exercise the shipped console script with a minimal DOM and deterministic API responses. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../src/decompnet/app/static/console.js"), "utf8");

function browser() {
  const elements = new Map();
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, {
      innerHTML: "", textContent: "", hidden: true, value: "", style: {},
      addEventListener() {}, querySelector: element, querySelectorAll: () => [],
    });
    return elements.get(id);
  };
  const env = {
    $: element, document: { addEventListener() {}, querySelectorAll: () => [] }, performance,
    esc: (s) => String(s).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
  };
  vm.createContext(env);
  vm.runInContext(source + `
    rcLog = () => ({});
    rcRenderSide = () => {};
    rcWelcome = () => { RC.view = "welcome"; };
    globalThis.subject = { RC, rcStatus, rcSettings, reaCall };
  `, env);
  const { RC, rcStatus, rcSettings, reaCall } = env.subject;
  const status = (id, version = "6.8.0") => ({
    configured: true, running: !!id,
    settings: { source: "saved", command: ["rea", "mcp"], env: {}, provider: "ghidra" }, providers: ["ghidra", "hopper"],
    connection: id ? { id, source: "installed", command: ["/tools/new/rea", "mcp"], server: { version } } : null,
    target: null, package: "rea-agents@6.7.0",
  });
  RC.status = status("old", "6.7.0");
  RC.tools = [{ name: "inspect", inputSchema: { properties: { procedure: { type: "string" } } }, risky: [] }];
  RC.toolMap = new Map(RC.tools.map((t) => [t.name, t]));
  RC.target = { path: "/old/target" };
  RC.fn = { address: "0x1000" };
  RC.procs = [{ address: "0x1000", name: "stale" }];
  RC.view = "tool"; RC.formName = "inspect";
  const freshTool = { name: "inspect", title: "Inspect", description: "", risky: ["writes files"],
    inputSchema: { type: "object", properties: { address: { type: "integer" } } } };
  let live = status("new"), fetches = 0;
  env.api = async (url) => {
    if (url === "/api/rea") return live;
    assert.equal(url, "/api/rea/tools");
    fetches++;
    live = status("new");
    return [freshTool];
  };
  return { env, RC, element, rcStatus, rcSettings, reaCall, status, setLive: (s) => { live = s; }, fetches: () => fetches };
}

(async () => {
  const b = browser();
  await b.rcStatus();
  assert.equal(b.fetches(), 1);
  assert.equal(b.RC.status.connection.server.version, "6.8.0");
  assert.equal(b.RC.target, null);
  assert.equal(b.RC.fn, null);
  assert.equal(b.RC.procs.length, 0);
  assert.match(b.element("rcStage").innerHTML, /data-k="address"/);
  assert.doesNotMatch(b.element("rcStage").innerHTML, /data-k="procedure"/);
  assert.match(b.element("rcStage").innerHTML, /writes files/);
  assert.equal(b.RC.view, "tool");
  b.element("rcStage").innerHTML = "user's edited form";
  b.element("rcProvider").value = "hopper";
  await b.rcStatus();
  assert.equal(b.fetches(), 1); // a live connection keeps its catalog and form inputs
  assert.equal(b.element("rcStage").innerHTML, "user's edited form");
  assert.match(b.element("rcProvider").innerHTML, /value="hopper" selected/);

  b.rcSettings(true);
  assert.match(b.element("rcSetup").innerHTML, /6\.8\.0/);
  assert.match(b.element("rcSetup").innerHTML, /Command source: <b>installed/);
  assert.match(b.element("rcSetup").innerHTML, /\/tools\/new\/rea/);
  b.RC.status.connection.server = {};
  b.rcSettings(true);
  assert.match(b.element("rcSetup").innerHTML, /version not reported/);
  assert.doesNotMatch(b.element("rcSetup").innerHTML.split("<div class=\"row\">")[0], /6\.7\.0/);
  b.RC.status.connection.server.version = "<script>";
  b.rcSettings(true);
  assert.match(b.element("rcSetup").innerHTML, /&lt;script>/);

  const removed = browser();
  removed.RC.formName = "removed_tool";
  await removed.rcStatus();
  assert.equal(removed.RC.view, "welcome");

  const stopped = browser();
  stopped.RC.status = null;
  stopped.setLive(stopped.status(null));
  await stopped.rcStatus();
  assert.equal(stopped.fetches(), 1); // recover even after a failed status request lost the old connection ID

  const stale = browser();
  stale.env.fetch = () => assert.fail("stale arguments must not be submitted to the replacement process");
  await assert.rejects(stale.reaCall("inspect", { procedure: "old" }, { quiet: true }), /REA reconnected/);
  console.log("REA console reconnect, form refresh, and runtime display checks passed");
})().catch((error) => { console.error(error); process.exitCode = 1; });
