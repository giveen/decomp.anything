/* Exercise the shipped matrix view, including unverified zero scores and the full-matrix control. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const elements = new Map();
const element = (id) => {
  if (!elements.has(id)) elements.set(id, { innerHTML: "", textContent: "", checked: false, addEventListener() {} });
  return elements.get(id);
};
const calls = [];
const env = {
  $: element, window: { addEventListener() {} }, document: { addEventListener() {}, querySelectorAll: () => [] },
  S: { slug: "fork", editDirty: false }, clock: () => "now",
  esc: (s) => String(s).replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;"),
  api: async (url, options) => { calls.push({ url, ...options }); return { matched: false }; },
  toast() {},
};
vm.createContext(env);
const source = fs.readFileSync(path.join(__dirname, "../src/decompnet/app/static/app_inspector.js"), "utf8");
vm.runInContext(source + "\nrefreshFn = () => {}; globalThis.subject = {matrixCell, matrixTable, renderMatrix};", env);
const { matrixCell, matrixTable, renderMatrix } = env.subject;
assert.match(matrixCell({ compiled: true, matched: false, diffs: 0, percent: 100 }), />verify</);
assert.doesNotMatch(matrixCell({ compiled: true, matched: false, diffs: 0, percent: 100 }), />MATCH</);
assert.match(matrixCell({ matched: true }), />MATCH</);
const cell = { profile: "GCC 2.7.2", context: "alone", compiled: true, matched: false, diffs: 2,
  kinds: "delay slot scheduling x2", evidence: { reason: "function bytes differ" } };
const run = { source_hash: "abcdef", cells: [cell], evidence_text: "Eliminated 90%\nNOT A MATCH <script>" };
const table = matrixTable(run);
assert.match(table, /Eliminated 90%<br>NOT A MATCH &lt;script>/);
assert.match(table, /delay slot scheduling x2/);
assert.doesNotMatch(table, /<script>/);
renderMatrix({ name: "f", status: "open", matrix: { configured: true, runs: [run] } });
assert.match(element("fnMatrix").innerHTML, /All configurations/);
element("matrixExhaustive").checked = true;
(async () => {
  await element("matrixRun").onclick();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/matrix/run");
  assert.equal(calls[0].body.exhaustive, true);
})().catch((err) => { console.error(err); process.exitCode = 1; });
