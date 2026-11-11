"""Matrix learning and verification without proprietary tools.

The runner really consumes jobs in a subprocess; its outputs are synthetic MIPS ELF objects. Only objdiff is
replaced, so inflated similarity scores exercise the independent byte/relocation gate through the public engine.
"""

import hashlib
import json
import shutil
import struct
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from decompnet import elfobj
from decompnet.app import tools
from decompnet.app.engine import RomNetwork
from decompnet.app.server import build
from decompnet.forks import compilerdiff, matrixverify, objdiff
from decompnet.forks.literals import Mismatch
from decompnet.network import NetworkError


def _elf(names=("f",), value=1, reference=None):
    """A small, parseable ELF32 MIPS object, with explicit function bounds and an optional external relocation."""
    code = struct.pack("<III", 0x24020000 | value, 0x03E00008, 0)
    strings = b"\0"
    syms = bytes(16)
    for i, name in enumerate(names):
        syms += struct.pack("<IIIBBH", len(strings), i * len(code), len(code), 0x12, 0, 1)
        strings += name.encode() + b"\0"
    relocs = b""
    if reference:
        syms += struct.pack("<IIIBBH", len(strings), 0, 0, 0x10, 0, 0)
        strings += reference.encode() + b"\0"
        relocs = b"".join(struct.pack("<II", i * len(code), ((len(names) + 1) << 8) | 5) for i in range(len(names)))
    sections = [("", 0, 0, b"", 0, 0, 0), (".text", 1, 6, code * len(names), 0, 0, 0),
                (".symtab", 2, 0, syms, 3, 1, 16), (".strtab", 3, 0, strings, 0, 0, 0),
                (".rel.text", 9, 0, relocs, 2, 1, 8)]
    shstrings = b"\0.text\0.symtab\0.strtab\0.rel.text\0.shstrtab\0"
    sections.append((".shstrtab", 3, 0, shstrings, 0, 0, 0))
    data = bytearray(52)
    headers = []
    for name, kind, flags, payload, link, info, entsize in sections:
        data += bytes((-len(data)) % 4)
        headers.append(struct.pack("<IIIIIIIIII", shstrings.index(name.encode() + b"\0") if name else 0,
                                   kind, flags, 0, len(data) if name else 0, len(payload), link, info, 4 if name else 0, entsize))
        data += payload
    data += bytes((-len(data)) % 4)
    shoff = len(data)
    data += b"".join(headers)
    data[:52] = struct.pack("<16sHHIIIIIHHHHHH", b"\x7fELF\x01\x01\x01" + bytes(9), 1, 8, 1,
                            0, 0, shoff, 0, 52, 0, 0, 40, len(headers), len(headers) - 1)
    return bytes(data)


RUNNER = '''import json
import shutil
import sys
from pathlib import Path
job = json.loads(Path(sys.argv[1]).read_text())
calls = Path("calls.json")
history = json.loads(calls.read_text()) if calls.exists() else []
history.append([[b["profile"], b["context"]] for b in job["builds"]])
calls.write_text(json.dumps(history))
said = {}
for b in job["builds"]:
    src = Path("objects") / (b["profile"] + ".o")
    if src.exists():
        shutil.copyfile(src, b["object"])
    said[b["id"]] = {"ok": src.exists(), "log": "fixture compiler failure"}
Path(job["result"]).write_text(json.dumps({"current": "default", "builds": said}))
'''


@pytest.fixture
def matrix(tmp_path, monkeypatch):
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "objects").mkdir()
    names = [f"f{i}" for i in range(14)]
    target = _elf(names)
    (root / "target.o").write_bytes(target)
    for p, v in (("default", 2), ("gcc272", 1), ("gcc281", 3)):
        (root / "objects" / f"{p}.o").write_bytes(_elf(names, v))
    source = "\n".join(f"int {name}(int a) {{ return a + 1; }}" for name in names)
    (root / "src" / "game.c").write_text(source)
    (root / "build_matrix.py").write_text(RUNNER)
    (root / ".decomp.wtf.json").write_text(json.dumps({"game": {}, "matrix": {
        "build": ["python", "build_matrix.py", "{job}"], "profiles": ["default", "gcc272", "gcc281"],
        "contexts": ["file", "alone"], "auto": False}}))
    (root / "objdiff.json").write_text(json.dumps({"units": [{"name": "game", "target_path": "target.o",
        "base_path": "normal.o", "metadata": {"source_path": "src/game.c"}}]}))
    net = RomNetwork(tmp_path / "state.db", tmp_path)
    agent, _ = net.local_agent("matrix-test")
    with net.store.tx() as db:
        db.execute("INSERT INTO projects(id,slug,root,config) VALUES(1,'fork',?,?)",
                   (str(root), json.dumps({"kind": "fork", "ready": True})))
        for i, name in enumerate(names, 1):
            db.execute("INSERT INTO functions(id,project_id,name,size,n_insns,status,unit,meta) "
                       "VALUES(?,1,?,12,3,'open','game',?)", (i, name, json.dumps({"symbol": name, "source": "src/game.c"})))
    case = SimpleNamespace(net=net, agent=agent, root=root, names=names, overrides={})

    def diff(original, candidate, symbol, **kwargs):
        a = elfobj.extract_function(original.read_bytes(), symbol)
        data = candidate.read_bytes()
        b = elfobj.extract_function(data, symbol)
        equal = a is not None and b is not None and a.code == b.code
        rows = [(" ", "jr ra", "jr ra")] if equal else [("|", "addiu v0, zero, 1", "addiu v0, zero, 2")]
        override = case.overrides.get(data, {})
        return objdiff.FunctionDiff(symbol, override.get("percent", 100.0 if equal else 80.0),
                                   override.get("rows", rows), len(a.code), {}, [], {},
                                   override.get("renamed", []), override.get("literals", []))

    monkeypatch.setattr(objdiff, "diff", diff)
    case.run = lambda name="f0", **kw: net.forks.matrix(agent, "fork", name, commit=False, **kw)
    case.calls = lambda: json.loads((root / "calls.json").read_text())
    try:
        yield case
    finally:
        net._pool.shutdown(wait=True)
        net.store.conn.close()


def test_ten_distinct_matches_prioritize_the_same_file_and_context(matrix):
    for name in matrix.names[:10]:
        assert matrix.run(name, profiles="gcc272", contexts="alone")["matched"]
    # Re-running the same function cannot turn one success into multiple votes.
    matrix.run("f0", profiles="gcc272", contexts="alone", again=True)
    result = matrix.run("f10")
    assert matrix.calls()[-1] == [["gcc272", "alone"]]
    assert len(result["cells"]) == 1 and result["matched"]
    assert result["cells"][0]["evidence"]["learning"]["matches"] == 10
    assert "built 1 of 6" in result["text"]
    assert (matrix.root / "src" / "game.c").read_text().startswith("int f0")
    # History is persisted, includes the early stop, and retains exactly one run when forced to repeat.
    matrix.run("f10", again=True)
    again = matrix.run("f10")
    assert again["repeat"] and len(again["cells"]) == 1
    assert matrix.net.forks._matrix_current(again["cells"]) == "default"
    history = matrix.net.forks.matrix_runs("fork", "f10")
    assert len(history) == 2 and "10 other function(s)" in history[0]["evidence_text"]


@pytest.mark.parametrize("failure", ["bytes", "compile", "unverified"])
def test_a_learned_recipe_that_fails_falls_back_to_every_other_cell(matrix, failure):
    matrix.run("f0", profiles="gcc272", contexts="alone")
    winning = matrix.root / "objects" / "gcc272.o"
    if failure == "compile":
        winning.unlink()
    else:
        wrong = _elf(matrix.names, 7)
        winning.write_bytes(wrong)
        if failure == "unverified":
            matrix.overrides[wrong] = {"percent": 100.0, "rows": [(" ", "jr ra", "jr ra")]}
    result = matrix.run("f1")
    assert not result["matched"] and len(result["cells"]) == 6
    assert matrix.calls()[-2] == [["gcc272", "alone"]]
    assert len(matrix.calls()[-1]) == 5 and ["gcc272", "alone"] not in matrix.calls()[-1]
    assert all(not c["matched"] and (c["score"] or 1) > 0 for c in result["cells"])
    assert "NOT A MATCH" in result["text"]
    assert not list((matrix.root / ".decomp-wtf" / "matrix").iterdir())


def test_exhaustive_and_explicit_filters_are_respected(matrix):
    matrix.run("f0", profiles="gcc272", contexts="alone")
    assert len(matrix.run("f1", exhaustive=True)["cells"]) == 6
    restricted = matrix.run("f2", profiles="gcc281", contexts="file")
    assert matrix.calls()[-1] == [["gcc281", "file"]] and not restricted["matched"]


@pytest.mark.parametrize("change", ["file", "project", "config", "target", "legacy", "latest_failure"])
def test_learning_excludes_unrelated_stale_or_unverified_history(matrix, change):
    matrix.run("f0", profiles="gcc272", contexts="alone")
    db = matrix.net.store.conn
    if change == "file":
        db.execute("UPDATE functions SET unit='another' WHERE name='f0'")
    elif change == "project":
        db.execute("UPDATE experiments SET project_id=99")
    elif change == "config":
        path = matrix.root / ".decomp.wtf.json"
        manifest = json.loads(path.read_text())
        manifest["matrix"]["profiles"][1] = {"name": "gcc272", "flags": "-O3"}
        path.write_text(json.dumps(manifest))
    elif change == "target":
        (matrix.root / "target.o").write_bytes(_elf(matrix.names, 9))
    elif change == "legacy":
        db.execute("UPDATE experiments SET evidence=NULL")
    else:
        (matrix.root / "objects" / "gcc272.o").write_bytes(_elf(matrix.names, 7))
        matrix.run("f0", profiles="gcc272", contexts="alone", again=True)
    result = matrix.run("f1")
    assert len(result["cells"]) == 6 and not any(c["evidence"].get("learning") for c in result["cells"])


def test_a_function_cannot_teach_itself_and_a_changed_target_invalidates_repeat(matrix):
    matrix.run("f0", profiles="gcc272", contexts="alone")
    assert len(matrix.run("f0")["cells"]) == 6
    (matrix.root / "target.o").write_bytes(_elf(matrix.names, 9))
    result = matrix.run("f0")
    assert not result["repeat"] and not result["matched"]


def test_an_alternative_verified_recipe_is_saved_without_changing_the_tree(matrix):
    before = (matrix.root / "src" / "game.c").read_bytes()
    result = matrix.net.forks.matrix(matrix.agent, "fork", "f0", profiles="gcc272", contexts="alone")
    assert result["matched"] and not result["committed"] and "MATRIX MATCH" in result["text"]
    assert (matrix.root / "src" / "game.c").read_bytes() == before
    fn = matrix.net.forks._fn("fork", "f0")
    assert fn["status"] == "open" and json.loads(fn["meta"])["matrix"]["verified"] == matrixverify.VERSION
    assert matrix.net.forks.matrix_matches("fork")[0]["function"] == "f0"
    assert matrix.net.claim_row(matrix.agent, "fork")["name"] != "f0"


def test_a_matrix_hit_cannot_commit_a_normal_build_that_only_has_a_perfect_score(matrix, monkeypatch):
    target = (matrix.root / "target.o").read_bytes()
    (matrix.root / "objects" / "default.o").write_bytes(target)
    wrong = _elf(matrix.names, 7)
    matrix.overrides[wrong] = {"percent": 100.0, "rows": [(" ", "jr ra", "jr ra")]}

    def normal_build(*_args):
        (matrix.root / "normal.o").write_bytes(wrong)
        return True, ""

    monkeypatch.setattr(matrix.net.forks, "_build_unit", normal_build)
    monkeypatch.setattr(matrix.net.forks, "_baseline", lambda *_args: ({"f0": 0.0}, True))
    before = (matrix.root / "src" / "game.c").read_bytes()
    result = matrix.net.forks.matrix(matrix.agent, "fork", "f0", profiles="default", contexts="file")
    assert result["matched"] and not result["committed"]
    assert "Byte verification failed in the normal build" in result["text"]
    assert (matrix.root / "src" / "game.c").read_bytes() == before
    assert matrix.net.forks._fn("fork", "f0")["status"] == "open"
    assert matrix.net.forks.matrix_matches("fork") == []


@pytest.mark.parametrize("failure", ["bytes", "references", "literals", "unreadable"])
def test_a_perfect_similarity_score_cannot_override_verification(matrix, failure):
    target = (matrix.root / "target.o").read_bytes()
    candidate = target
    override = {"percent": 100.0, "rows": [(" ", "jr ra", "jr ra")]}
    if failure == "bytes":
        candidate = _elf(matrix.names, 7)
    elif failure == "references":
        override["renamed"] = [("right_global", "wrong_global")]
    elif failure == "literals":
        override["literals"] = [Mismatch(0, "lw", "constant", b"\x01\0\0\0", b"\x02\0\0\0", False)]
    else:
        # A parsed but absent function is unavailable evidence even if a diff tool says 100%.
        candidate = _elf(["missing"])
    (matrix.root / "objects" / "gcc272.o").write_bytes(candidate)
    matrix.overrides[candidate] = override
    result = matrix.run(profiles="gcc272", contexts="file")
    assert not result["matched"] and result["cells"][0]["score"] > 0
    assert result["cells"][0]["diffs"] == 0 and "verify" in result["text"] and "NOT A MATCH" in result["text"]
    assert matrix.net.forks.matrix_matches("fork") == []


def test_raw_bytes_and_relocation_identities_are_both_required(tmp_path):
    a, b = tmp_path / "a.o", tmp_path / "b.o"
    a.write_bytes(_elf(reference="correct"))
    b.write_bytes(_elf(reference="wrong"))
    evidence = matrixverify.verify(a, b, "f")
    assert evidence["byte_diffs"] == 0 and evidence["relocations_equal"] is False and not evidence["byte_exact"]
    b.write_bytes(a.read_bytes())
    evidence = matrixverify.verify(a, b, "f")
    assert evidence["byte_exact"] and evidence["target_sha256"] == evidence["base_sha256"]
    b.write_bytes(b"not an object")
    assert not matrixverify.verify(a, b, "f")["byte_exact"]
    b.write_bytes(_elf(["different_name"]))
    assert matrixverify.verify(a, b, "f")["reason"] == "function bytes unavailable"


def test_ninety_percent_improvement_keeps_the_remaining_source_error_visible(matrix):
    rows = [("|", "addiu v0, zero, 1", "addiu v0, zero, 2")]
    for p, n in (("default", 10), ("gcc272", 1), ("gcc281", 8)):
        data = _elf(matrix.names, n + 2)
        (matrix.root / "objects" / f"{p}.o").write_bytes(data)
        matrix.overrides[data] = {"percent": 100.0 - n, "rows": rows * n}
    result = matrix.run(contexts="file")
    assert not result["matched"]
    assert "eliminated 90.0%" in result["text"] and "(10 -> 1)" in result["text"]
    assert "unexplained code differences x1" in result["text"] and "NOT A MATCH" in result["text"]
    history = matrix.net.forks.matrix_runs("fork", "f0")[0]
    assert "eliminated 90.0%" in history["evidence_text"]
    best = next(c for c in history["cells"] if c["profile"] == "gcc272")
    assert best["counts"] == {"other": 1} and best["examples"]["other"][0]["base"].endswith("2")


@pytest.mark.parametrize("left,right", [
    ("lw v0, %gp_rel(right)(gp)", "lw v0, %gp_rel(wrong)(gp)"),
    ("lui v0, %hi(global+4)", "lui v0, %hi(global+8)"),
    ("lw v0, %lo(global)(at)", "sw v0, %lo(global)(at)"),
    ("jal good", "jal bad"),
])
def test_changed_symbols_addends_memory_operations_and_calls_are_not_compiler_verdicts(left, right):
    result = compilerdiff.classify([("|", left, right)])
    assert result.counts == {"other": 1} and result.verdict == "source"
    assert "not proof" in result.note()


def test_marked_identical_text_is_not_silently_discarded():
    result = compilerdiff.classify([("i", "lw v0, 0(a0)", "lw v0, 0(a0)")])
    assert result.counts == {"encoding": 1} and result.total == 1


def test_the_requested_pattern_counts_are_observations_not_proof():
    rows = [(" ", "jr ra", "jr ra"), ("<", "nop", "")] * 2
    rows += [("|", "lw v0, %gp_rel(global)(gp)", "lw v0, %lo(global)(v0)")] * 3
    rows += [("|", "addu v0, a0, a1", "addu v1, a0, a1")] * 8
    classes = compilerdiff.classify(rows)
    assert classes.counts == {"delay_slot": 2, "gp_rel": 3, "regalloc": 8} and classes.total == 13
    assert "not proof" in classes.note() and "source error remains" in classes.note()


def test_a_changed_current_profile_between_batches_is_not_used_as_a_baseline(matrix, monkeypatch):
    matrix.run("f0", profiles="gcc272", contexts="alone")
    (matrix.root / "objects" / "gcc272.o").write_bytes(_elf(matrix.names, 8))
    original = matrix.net.forks._matrix_build
    batches = []

    def changed(*args, **kw):
        cells, current = original(*args, **kw)
        batches.append(current)
        return cells, current if len(batches) == 1 else "gcc281"

    monkeypatch.setattr(matrix.net.forks, "_matrix_build", changed)
    with pytest.raises(NetworkError, match="profile changed between batches"):
        matrix.run("f1")
    assert not matrix.net.forks.matrix_runs("fork", "f1")


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js for matrix browser logic")
def test_browser_displays_evidence_and_requests_exhaustive_runs():
    result = subprocess.run([shutil.which("node"), str(Path(__file__).with_name("matrix_evidence.cjs"))],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_api_and_agent_dispatch_expose_exhaustive_and_persist_evidence(matrix):
    matrix.run("f0", profiles="gcc272", contexts="alone")
    # Limit to misses so this transport test never asks the fake normal build to commit.
    text = tools.call(matrix.net, matrix.agent, "fork", "compiler_matrix",
                      {"function": "f1", "profiles": "gcc281", "exhaustive": True})
    assert "NOT A MATCH" in text
    schema = next(t["function"] for t in tools.tool_schemas(fork=True) if t["function"]["name"] == "compiler_matrix")
    assert schema["parameters"]["properties"]["exhaustive"]["type"] == "boolean"
    (matrix.root / "objects" / "gcc272.o").write_bytes(_elf(matrix.names, 8))
    mcp, _manager, security = build(matrix.net, matrix.root, 6499, "matrix-secret")
    app = mcp.streamable_http_app(transport_security=security, host="127.0.0.1")
    with TestClient(app, base_url="http://127.0.0.1:6499") as client:
        client.cookies.set("decomp_live", "matrix-secret")
        response = client.post("/api/matrix/run", json={"slug": "fork", "name": "f2", "exhaustive": True},
                               headers={"X-Decomp": "1"})
        assert response.status_code == 200, response.text
        assert not response.json()["matched"] and len(matrix.calls()[-1]) == 6
    assert matrix.net.forks.matrix_runs("fork", "f2")[0]["cells"][0]["evidence"]["target_sha256"]


def test_legacy_recipes_are_unverified_and_remain_available_to_claim(matrix):
    matrix.net.store.conn.execute("UPDATE functions SET meta=? WHERE name='f0'",
                                  (json.dumps({"matrix": {"profile": "old", "context": "file"}}),))
    fn = matrix.net.forks._fn("fork", "f0")
    fp = matrix.net.forks.get("fork")[1]
    assert "no strict byte evidence" in " ".join(matrix.net.forks.matrix_brief(fn, fp))
    assert matrix.net.forks.matrix_matches("fork") == []
    assert matrix.net.claim_row(matrix.agent, "fork")["name"] == "f0"


def test_target_changes_during_build_are_not_recorded(matrix, monkeypatch):
    original = matrix.net.forks._matrix_build

    def moved(*args, **kw):
        result = original(*args, **kw)
        (matrix.root / "target.o").write_bytes(_elf(matrix.names, 8))
        return result

    monkeypatch.setattr(matrix.net.forks, "_matrix_build", moved)
    with pytest.raises(NetworkError, match="original object changed"):
        matrix.run()
    assert not matrix.net.store.all("SELECT * FROM experiments")


def test_learning_survives_reopening_the_database(matrix):
    matrix.run("f0", profiles="gcc272", contexts="alone")
    reopened = RomNetwork(matrix.net.store.conn.execute("PRAGMA database_list").fetchone()[2], matrix.root.parent)
    try:
        proj, fp, _ = reopened.forks.get("fork")
        fn = reopened.forks._fn("fork", "f1")
        votes = reopened.forks._matrix_learning(proj, fn, reopened.forks.matrix_config(fp), "src/game.c",
                                               hashlib.sha256((matrix.root / "target.o").read_bytes()).hexdigest())
        assert [(v["profile"], v["context"], v["matches"]) for v in votes] == [("gcc272", "alone", 1)]
    finally:
        reopened._pool.shutdown(wait=True)
        reopened.store.conn.close()
