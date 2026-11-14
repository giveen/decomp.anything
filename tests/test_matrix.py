"""The compiler matrix: a function's C under each compiler profile and translation-unit context of a fork.

Three layers, tested where each can be: reading a diff for compiler-like differences (rows as objdiff formats MIPS)
and making translation-unit variants are text in, text out; the run itself (app/forkmatrix.py) is driven through the
demo fork with a matrix its test declares: the project's GCC at -O2 (how the game was built) and at -O1.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from decompnet import toolchains
from decompnet.app import forkmatrix, forkutil, tools
from decompnet.app.engine import RomNetwork
from decompnet.app.forkengine import ForkEngine
from decompnet.forks import compilerdiff, csource, tucontext
from decompnet.forks.project import ForkProject
from decompnet.network import NetworkError
from decompnet.store import Store

ROOT = Path(__file__).resolve().parent.parent
FORK_SRC = ROOT / "projects" / "demo-fork"
ROM = ROOT / "src" / "decompnet" / "app" / "static" / "demo" / "demo-quest-64.z64"
REFERENCE = (FORK_SRC / "tools" / "reference" / "game.c").read_text()

needs_tools = pytest.mark.skipif(
    toolchains.installed("gcc-2.7.2-kmc") is None or toolchains.installed("objdiff-cli") is None
    or not shutil.which("git"), reason="needs gcc-2.7.2-kmc, objdiff-cli and git")

# ------------------------------------------------------------------ what a miss differs in


def _kinds(rows):
    return compilerdiff.classify(rows)


def test_a_global_read_through_gp_on_one_side_is_a_gp_difference():
    rows = [(" ", "addiu sp, sp, -0x18", "addiu sp, sp, -0x18"),
            ("|", "lw v0, %gp_rel(D_80070000)(gp)", "lui v0, %hi(D_80070000)"),
            (">", "", "lw v0, %lo(D_80070000)(v0)"),
            (" ", "jr ra", "jr ra"), (" ", "addiu sp, sp, 0x18", "addiu sp, sp, 0x18")]
    c = _kinds(rows)
    assert c.counts == {"gp_rel": 2} and c.gp == (1, 0) and c.verdict == "compiler"
    assert "GP-relative addressing x2" in c.summary() and "compiler_matrix" in c.note()


def test_the_same_address_split_another_way_is_a_hi_lo_difference():
    rows = [("|", "lui at, %hi(D_80070000)", "lui v0, %hi(D_80070000)"),
            ("|", "lw v0, %lo(D_80070000)(at)", "lw v0, %lo(D_80070000)(v0)")]
    c = _kinds(rows)
    assert c.counts == {"hi_lo": 2} and c.verdict == "compiler"


def test_nops_are_told_apart_by_what_comes_before_them():
    rows = [(" ", "lw v0, 0x0(a0)", "lw v0, 0x0(a0)"),
            ("<", "nop ", ""),                                   # after a load, the original only: load delay
            (" ", "addiu v0, v0, 0x1", "addiu v0, v0, 0x1"),
            (" ", "beqz v0, 0x40", "beqz v0, 0x40"),
            ("|", "nop ", "addu a0, s0, zero"),                  # the delay slot: a nop against an instruction
            (" ", "sll v0, v0, 2", "sll v0, v0, 2"),
            (">", "", "nop ")]                                   # nowhere special
    c = _kinds(rows)
    assert c.counts == {"load_delay": 1, "delay_slot": 1, "nop": 1} and c.verdict == "compiler"


def test_registers_alone_are_a_maybe_and_other_code_is_the_source():
    regs = _kinds([("|", "addu v0, a0, a1", "addu v1, a0, a1"), ("|", "sw v0, 0x10(sp)", "sw v1, 0x10(sp)")])
    assert regs.counts == {"regalloc": 2} and regs.verdict == "maybe"
    code = _kinds([("|", "addiu v0, v0, 0x1", "addiu v0, v0, 0x2"), ("<", "jal func_80012345", "")])
    assert code.counts == {"other": 2} and code.verdict == "source" and "compiler_matrix" not in code.note()
    both = _kinds([("|", "lw v0, %gp_rel(D_1)(gp)", "lw v0, %lo(D_1)(v0)"), ("|", "sltiu v0, v0, 0x5", "slti v0, v0, 0x5")])
    assert both.verdict == "mixed"
    stack = _kinds([("|", "sw ra, 0x14(sp)", "sw ra, 0x1c(sp)")])
    assert stack.counts == {"stack": 1} and stack.verdict == "source"
    assert _kinds([(" ", "jr ra", "jr ra")]).verdict == "unknown" and _kinds([]).note() == ""


def test_a_branch_target_moved_by_a_longer_side_is_counted_and_named_for_what_it_is():
    """Rows of a real PlayStation near miss. An extra instruction explains a shifted target without proving the
    branch still reaches the same block: the row stays counted, as a kind that routes an experiment."""
    rows = [("i", "lw v0, 0x0(v1)", "lw v1, 0x0(v1)"), (" ", "nop ", "nop "),
            ("i", "bgtz v0, 0xb8", "bgtz v1, 0xbc"),
            (" ", "addiu v0, zero, 0x20", "addiu v0, zero, 0x20"),
            (">", "", "lui at, %hi(D_80072F68-0x28)"),
            ("i", "sw v0, -0x28(v1)", "sw v0, %lo(D_80072F68-0x28)(at)"),
            ("i", "beqz v0, 0xc0", "beqz v0, 0xc4")]
    c = _kinds(rows)
    assert c.counts == {"hi_lo": 2, "regalloc": 2, "branch_shift": 1} and c.total == 5 and c.verdict == "compiler"
    # the same length on both sides: a branch to another place is a difference
    assert _kinds([("i", "beqz v0, 0xc0", "beqz v0, 0xc4")]).counts == {"other": 1}


def test_the_advice_follows_where_the_projects_matrix_stands():
    c = _kinds([("|", "lw v0, %gp_rel(D_1)(gp)", "lui v0, %hi(D_1)"), (">", "", "lw v0, %lo(D_1)(v0)")])
    assert "compiler_matrix tests" in c.note() and "compiler_matrix" not in c.note("none")
    assert "declares no compiler matrix" in c.note("none") and "No tested configuration" in c.note("done")
    regs = _kinds([("|", "addu v0, a0, a1", "addu v1, a0, a1")])
    assert "compiler_matrix tests" in regs.note() and "both the C and the build settings" in regs.note("done")


def test_an_instruction_moved_into_a_delay_slot_is_scheduling():
    rows = [("|", "jal func_80011111", "addu a0, s0, zero"),
            ("|", "addu a0, s0, zero", "jal func_80011111"),
            (">", "", "nop ")]
    c = _kinds(rows)
    assert c.counts == {"delay_slot": 3} and c.verdict == "compiler"


def test_classes_survive_the_database():
    c = _kinds([("|", "lw v0, %gp_rel(D_1)(gp)", "lui v0, %hi(D_1)"), ("|", "addu v0, a0, a1", "addu v1, a0, a1")])
    back = compilerdiff.from_json(c.to_json())
    assert back.counts == c.counts and back.total == c.total and back.gp == c.gp and back.verdict == c.verdict
    assert back.examples == c.examples
    assert compilerdiff.from_json(None).total == 0 and compilerdiff.from_json("not json").verdict == "unknown"


# ------------------------------------------------------------------ translation-unit contexts

FILE = '''#include "common.h"

typedef struct { s32 x; s32 y; } Vec;
extern s32 D_80070000;
extern u8 D_80071000[];
extern void (*D_callback)(void);
s32 g_count;
s32 g_table[4] = { 1, 2, 3, 4 };
u8 g_flag = 1;
static s32 s_private;

INCLUDE_ASM("asm/nonmatchings/game", func_80010000);

static inline s32 twice(s32 v) {
    return v * 2;
}

s32 first(s32 a) {
    if (a) { return twice(a); }
    return g_count;
}

s32 second(Vec *v)
{
    return v->x + D_80070000;
}

void third(void) {
    g_flag = 0;
}

void fourth(void) {
    third();
}
'''


def test_definitions_are_found_in_order_and_stubs_are_not_functions():
    names = [n for n, _, _ in tucontext.definitions(FILE)]
    assert names == ["twice", "first", "second", "third", "fourth"]
    for name, start, end in tucontext.definitions(FILE):
        assert FILE[start:end] == csource.get(FILE, name)
    assert tucontext.prototype(csource.get(FILE, "second")) == "s32 second(Vec *v);\n"


def test_alone_keeps_the_function_and_turns_the_others_into_prototypes():
    ctx = tucontext.contexts(FILE, "second")
    assert list(ctx) == ["file", "alone", "neighbors", "extern", "tentative"]
    assert ctx["file"] == FILE
    alone = ctx["alone"]
    assert "return v->x + D_80070000;" in alone and "s32 first(s32 a);" in alone and "void third(void);" in alone
    assert "return twice(a)" not in alone and "g_flag = 0;" not in alone
    assert "static inline s32 twice(s32 v) {" in alone and "return v * 2;" in alone  # inline code is the caller's
    assert 'INCLUDE_ASM("asm/nonmatchings/game", func_80010000);' in alone and "typedef struct" in alone
    # same order as the file: a prototype stands where its definition stood
    assert alone.index("s32 first(s32 a);") < alone.index("s32 second(Vec *v)") < alone.index("void third(void);")


def test_neighbors_keep_the_function_before_and_after():
    near = tucontext.contexts(FILE, "second")["neighbors"]
    assert "return twice(a)" in near and "g_flag = 0;" in near and "void fourth(void);" in near
    assert "third();\n}" not in near


def test_globals_are_redeclared_one_way_or_the_other():
    ctx = tucontext.contexts(FILE, "second")
    ext = ctx["extern"]
    assert "extern s32 g_count;" in ext and "extern u8 g_flag;" in ext
    assert "static s32 s_private;" in ext            # a static can't be extern
    assert "s32 g_table[4] = { 1, 2, 3, 4 };" in ext  # a braced initializer is left as it is
    assert "typedef struct { s32 x; s32 y; } Vec;" in ext
    tent = ctx["tentative"]
    assert "\ns32 D_80070000;" in tent and "\nu8 D_80071000[];" in tent and "extern s32 D_80070000" not in tent
    assert "extern void (*D_callback)(void);" in tent  # a function pointer keeps its declaration
    assert "s32 g_count;" in tent


def test_a_context_that_changes_nothing_is_not_built_twice():
    one = "s32 only(s32 a) {\n    return a + 1;\n}\n"
    assert list(tucontext.contexts(one, "only")) == ["file"]
    assert list(tucontext.contexts(FILE, "second", ["file", "extern"])) == ["file", "extern"]
    assert list(tucontext.contexts(FILE, "missing")) == ["file"]


# ------------------------------------------------------------------ what a project declares


def _engine(tmp_path, matrix):
    (tmp_path / ".decomp.wtf.json").write_text(json.dumps({"game": {}, "setup": [], "matrix": matrix}))
    return ForkEngine(net=object()).matrix_config(ForkProject(tmp_path))


def test_the_matrix_a_project_declares(tmp_path):
    cfg = _engine(tmp_path, {"build": ["python", "tools/m.py", "{job}"],
                             "profiles": ["2.7.2-psx", {"name": "2.8.1-psx", "compiler": "GCC 2.8.1", "flags": "-O2 -G8"}],
                             "contexts": ["file", "alone", "nonsense"]})
    assert [p["name"] for p in cfg["profiles"]] == ["2.7.2-psx", "2.8.1-psx"] and cfg["profiles"][1]["flags"] == "-O2 -G8"
    assert cfg["contexts"] == ["file", "alone"] and cfg["auto"] is True and len(cfg["digest"]) == 12
    again = _engine(tmp_path, {"build": ["python", "tools/m.py", "{job}"], "profiles": ["2.7.2-psx"]})
    assert again["digest"] != cfg["digest"] and again["contexts"] == list(tucontext.CONTEXTS)


@pytest.mark.parametrize("matrix", [None, {}, {"build": ["make"], "profiles": ["a"]},
                                    {"build": ["x", "{job}"], "profiles": []}, {"build": "x {job}", "profiles": ["a"]}])
def test_a_matrix_without_a_job_command_or_profiles_is_no_matrix(tmp_path, matrix):
    assert _engine(tmp_path, matrix) is None


def test_the_tools_are_for_forks_and_the_guide_is_shipped(tmp_path):
    assert {"compiler_matrix", "experiment_history"} <= set(tools.tool_names(fork=True))
    assert not {"compiler_matrix", "experiment_history"} & set(tools.tool_names(fork=False))
    assert not {"compiler_matrix", "experiment_history"} & set(tools.tool_names(analysis=True))
    assert "matrix" in tools.GUIDES and "MATRIX MATCH" in tools._guide("matrix")
    names = {t["function"]["name"] for t in tools.tool_schemas(fork=True)}
    assert {"compiler_matrix", "experiment_history"} <= names
    cols = {r[1] for r in Store(tmp_path / "t.db").conn.execute("PRAGMA table_info(experiments)")}
    assert {"function_id", "run", "source_hash", "profile", "flags", "context", "diffs", "matched", "classes"} <= cols


# ------------------------------------------------------------------ the run, in the demo fork

MATRIX_BUILD = '''"""The demo fork's matrix build: the project's compiler at -O2 (how the game was built) and at -O1."""
import json
import subprocess
import sys
from pathlib import Path

from decompnet import toolchains

ROOT = Path(__file__).resolve().parent.parent
job = json.loads((ROOT / sys.argv[1]).read_text())
tc = toolchains.get("gcc-2.7.2-kmc")
tc_dir = toolchains.require(tc.id)
cc = [a.replace("{tc}", str(tc_dir)) for a in tc.cc]
FLAGS = {"O2": tc.flags, "O1": ["-O1" if f == "-O2" else f for f in tc.flags]}
said = {}
for b in job["builds"]:
    r = subprocess.run([*cc, *FLAGS[b["profile"]], "-I", str(ROOT / "include"), str(ROOT / b["source"]),
                        "-o", str(ROOT / b["object"])], capture_output=True, text=True)
    said[b["id"]] = {"ok": r.returncode == 0, "log": r.stderr[-800:]}
current = ROOT / "tools" / "current_profile.txt"  # a test's way of saying "this file is built another way"
(ROOT / job["result"]).write_text(json.dumps({"current": current.read_text().strip() if current.exists() else "O2",
                                              "builds": said}))
'''


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                               "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin", "HOME": str(cwd)}).stdout


@pytest.fixture(scope="module")
def fork(tmp_path_factory):
    base = tmp_path_factory.mktemp("matrix")
    upstream = base / "upstream"
    shutil.copytree(FORK_SRC, upstream)
    manifest = json.loads((upstream / ".decomp.wtf.json").read_text())
    manifest["matrix"] = {"build": ["python", "tools/matrix_build.py", "{job}"],
                          "profiles": [{"name": "O2", "compiler": "GCC 2.7.2 KMC", "flags": "-O2"},
                                       {"name": "O1", "compiler": "GCC 2.7.2 KMC", "flags": "-O1"}]}
    (upstream / ".decomp.wtf.json").write_text(json.dumps(manifest))
    (upstream / "tools" / "matrix_build.py").write_text(MATRIX_BUILD)
    _git(upstream, "init", "-q", "-b", "main")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-q", "-m", "initial")
    net = RomNetwork(base / "app.db", base / "work")
    (base / "work").mkdir()
    slug = net.forks.clone(str(upstream), name="Demo Quest decomp", platform="n64", log=lambda *_: None)
    net.forks.setup(slug, ROM.read_bytes(), "demo.z64", log=lambda *_: None)
    agent, _ = net.local_agent("claude-1")
    return net, slug, agent


def _ref(name: str) -> str:
    return csource.get(REFERENCE, name).strip()


def _cells(res, **want):
    return [c for c in res["cells"] if all(c[k] == v for k, v in want.items())]


@needs_tools
def test_a_match_the_files_own_build_makes_is_committed(fork):
    net, slug, agent = fork
    root = Path(net.project_row(slug)["root"])
    before = net.overview(slug)["matched"]
    res = net.forks.matrix(agent, slug, "lerp", _ref("lerp"), exhaustive=True)
    assert res["matched"] and res["committed"] and not res["repeat"], res["text"]
    assert _cells(res, profile="O2", context="file")[0]["matched"]
    assert {c["profile"] for c in res["cells"]} == {"O2", "O1"} and all(c["compiled"] for c in res["cells"])
    assert "Compiler matrix for lerp" in res["text"] and "O2 *" in res["text"] and "MATCH!" in res["text"]
    assert net.overview(slug)["matched"] == before + 1
    assert "Match lerp" in _git(root, "log", "--oneline", "decomp-wtf")
    assert not (root / ".decomp-wtf" / "matrix").exists() or not list((root / ".decomp-wtf" / "matrix").iterdir())
    rows = net.store.all("SELECT * FROM experiments WHERE function_id=(SELECT id FROM functions WHERE name='lerp')")
    assert len(rows) == len(res["cells"]) and {r["flags"] for r in rows} == {"-O2", "-O1"}
    assert all(r["current"] == (r["profile"] == "O2") for r in rows) and sum(r["source"] is not None for r in rows) == 1
    history = net.forks.matrix_history(slug, "lerp")
    assert "O2 * / file [GCC 2.7.2 KMC -O2]: MATCH" in history and "claude-1" in history


@needs_tools
def test_a_match_under_another_profile_is_saved_with_its_recipe(fork):
    """The demo says its file is built with -O1 while the game was built with -O2: the C is right, the build isn't."""
    net, slug, agent = fork
    root = Path(net.project_row(slug)["root"])
    name = "checksum_save"
    said = root / "tools" / "current_profile.txt"
    said.write_text("O1")
    try:
        source = (root / "src" / "game.c").read_text()
        res = net.forks.matrix(agent, slug, name, _ref(name))
        assert res["matched"] and not res["committed"], res["text"]
        assert "MATRIX MATCH" in res["text"] and "compiled with O2 (its file is built with O1)" in res["text"]
        assert (root / "src" / "game.c").read_text() == source  # nothing in the tree changed
        fn = net.store.one("SELECT * FROM functions WHERE name=?", name)
        assert fn["status"] == "open" and json.loads(fn["meta"])["matrix"]["profile"] == "O2"
        assert fn["best_source"].strip() == _ref(name)
        note = net.store.one("SELECT text FROM notes WHERE function_id=? ORDER BY id DESC", fn["id"])["text"]
        assert note.startswith("MATRIX MATCH") and "verification before integration" in note
        saved = net.forks.matrix_matches(slug)
        assert [(m["function"], m["profile"], m["context"]) for m in saved] == [(name, "O2", "file")]
        assert saved[0]["source"].strip() == _ref(name) and saved[0]["source_path"] == "src/game.c"
        assert "already MATCHES under O2 / file" in net.get_function(name, slug, agent)
        # the same sources again: answered from the record, nothing compiled
        again = net.forks.matrix(agent, slug, name, _ref(name))
        assert again["repeat"] and again["matched"] and "nothing was compiled" in again["text"]
        assert net.store.one("SELECT COUNT(*) AS n FROM experiments WHERE function_id=?", fn["id"])["n"] == len(res["cells"])
        # agents aren't handed a function whose C is already right
        other, _ = net.local_agent("claude-2")
        for _ in range(12):
            got = net.claim_row(other, slug)
            if got is None:
                break
            assert got["name"] != name
            net.release(other, got["name"], "testing the queue", slug)
        status = net.forks.matrix_status(slug)
        assert status["configured"] and status["profiles"] == ["O2", "O1"] and status["matches"] == 1
        detail = net.function_detail(slug, name)["matrix"]
        assert detail["configured"] and detail["saved"]["context"] == "file" and len(detail["runs"]) == 1
        # a sync rebuilds every function's row from the project's report: the recipe has to survive it
        net.forks.sync(slug, log=lambda *_: None)
        assert json.loads(net.store.one("SELECT meta FROM functions WHERE id=?", fn["id"])["meta"])["matrix"]["profile"] == "O2"
    finally:
        said.unlink()


@needs_tools
def test_a_miss_that_is_different_code_gets_no_matrix(fork):
    net, slug, agent = fork
    name = "count_alive_entities"
    net.claim(agent, slug, name)
    res = net.attempt(agent, name, _ref(name).replace("return n;", "return n + 1;"), slug)
    assert not res["matched"] and "ran the compiler matrix" not in res["text"]
    assert net.store.one("SELECT COUNT(*) AS n FROM experiments WHERE function_id=(SELECT id FROM functions "
                         "WHERE name=?)", name)["n"] == 0
    # asked for, it runs: every cell is measured, none matches, the closest is shown with its diff
    out = net.forks.matrix(agent, slug, name, _ref(name).replace("return n;", "return n + 1;"), contexts="file,alone")
    assert not out["matched"] and "Best: " in out["text"] and "Closest (" in out["text"]
    assert {c["context"] for c in out["cells"]} <= {"file", "alone"} and all(c["diffs"] for c in out["cells"])
    net.release(agent, name, "testing", slug)
    with pytest.raises(NetworkError, match="no such profile"):
        net.forks.matrix(agent, slug, name, _ref(name), profiles="O3")
    with pytest.raises(NetworkError, match="assembly"):
        net.forks.matrix(agent, slug, name, 's32 count_alive_entities(void) { __asm__("nop"); return 0; }')


@needs_tools
def test_the_sweep_tries_what_has_c_and_skips_what_was_tried(fork):
    net, slug, _agent = fork
    name = "entity_collides"
    fn = net.store.one("SELECT * FROM functions WHERE name=?", name)
    if fn["status"] != "open":
        pytest.skip("entity_collides isn't open in this run")
    with net.store.tx() as db:  # an agent's best attempt: right C, left for the matrix
        db.execute("UPDATE functions SET best_source=?, best_score=100 WHERE id=?", (_ref(name), fn["id"]))
    net.forks.matrix_sweep(slug)
    job = net.forks._matrix_jobs[slug]
    for _ in range(600):
        if job["state"] != "running":
            break
        job["stop"].wait(0.1)
    assert job["state"] == "done" and job["tried"] >= 1 and job["committed"] >= 1, job
    assert net.store.one("SELECT status FROM functions WHERE id=?", fn["id"])["status"] == "matched"
    assert net.forks.matrix_status(slug)["sweep"]["done"] == job["total"]


# ------------------------------------------------------------------ what goes with a function

SENT = '''#include "common.h"
#include "extra.h"
/* helpers */
#define O16(o, x) (*(s16 *)((o) + (x)))
#define LONG(a, b) \\
    ((a) + \\
     (b))
extern s32 D_80070000;
typedef struct {
    s32 x;
    s32 y;
} Pair;
static inline s32 twice(s32 v) {
    return v * 2;
}
s32 target(s32 a) {
    return twice(a) + O16(a, 2);
}
'''
HAVE = '''#include "common.h"

extern s32 D_80070000;
#define O16(o, x) (*(s16 *)((o) + (x)))
'''


def _sent(source=SENT, have=HAVE):
    return forkutil._around(source, csource.find(source, "target"), have, "src/a.c")


def test_text_is_cut_into_whole_things():
    chunks = forkutil._chunks(SENT[:SENT.index("s32 target")])
    assert [forkutil._helper_kind(c) for c in chunks] == ["include", "include", "define", "define", "decl", "type", "helper"]
    assert chunks[3].count("\n") == 2 and chunks[5].endswith("} Pair;") and chunks[6].endswith("}")


def test_a_function_brings_its_macros_types_and_static_helpers():
    includes, before, after = _sent()
    assert includes == ['#include "extra.h"'] and after == []
    # the include, the declaration and the macro the file has already are left out; the rest is placed whole
    assert [forkutil._helper_kind(c) for c in before] == ["define", "type", "helper"]
    assert before[0].startswith("#define LONG") and "s32 y;" in before[1] and "return v * 2;" in before[2]
    assert _sent(have=HAVE + "\n".join(before))[1] == []  # sent again: nothing new


@pytest.mark.parametrize("extra", ["int g_table[2] = { 1, 2 };", "s32 other(s32 a) {\n    return a;\n}", "#if 0"])
def test_a_global_with_a_value_or_another_function_is_still_the_files_business(extra):
    with pytest.raises(NetworkError, match="with edit_file first"):
        _sent(extra + "\n" + SENT)


def test_a_macro_or_helper_sent_again_replaces_the_old_one():
    have = HAVE + "static inline s32 twice(s32 v) {\n    return v + v;\n}\n\ns32 target(s32 a);\n"
    _includes, before, _after = _sent(SENT.replace("((o) + (x)))", "((o) + (x) + 0))"), have)
    text, left = forkutil._merge_helpers(have, before, "src/a.c")
    assert text.count("#define O16") == 1 and "(x) + 0))" in text      # the macro's new body, in its old place
    assert text.count("twice(s32 v)") == 1 and "return v * 2;" in text and "return v + v;" not in text
    assert [forkutil._helper_kind(c) for c in left] == ["define", "type"]  # LONG and Pair are new: placed before it
    with pytest.raises(NetworkError, match="already defines `Pair`"):
        forkutil._merge_helpers(have + "typedef struct { s32 x; } Pair;\n", before, "src/a.c")


@needs_tools
def test_a_miss_is_remembered_with_what_it_needs_and_the_helper_is_replaced_not_doubled(fork):
    net, slug, agent = fork
    root = Path(net.project_row(slug)["root"])
    name = "entity_default_hp"
    net.claim(agent, slug, name)
    helper = "#define HP_BONUS 3\nstatic s32 hp_extra(s32 v) {\n    return v + HP_BONUS;\n}\n"
    wrong = _ref(name).replace("return ", "return hp_extra(0) + ", 1)
    assert wrong != _ref(name)
    res = net.attempt(agent, name, helper + wrong, slug)
    assert res["score"] and not res["matched"], res["text"]
    text = (root / "src" / "game.c").read_text()
    assert text.count("#define HP_BONUS 3") == 1 and text.index("hp_extra(s32 v)") < text.index(name + "(")
    best = net.store.one("SELECT best_source FROM functions WHERE name=?", name)["best_source"]
    assert best.startswith("#define HP_BONUS 3") and "static s32 hp_extra" in best and best.rstrip().endswith("}")
    again = net.attempt(agent, name, helper.replace("HP_BONUS 3", "HP_BONUS 4") + wrong, slug)
    assert again["score"], again["text"]
    text = (root / "src" / "game.c").read_text()
    assert text.count("#define HP_BONUS") == 1 and "#define HP_BONUS 4" in text and text.count("hp_extra(s32 v)") == 1
    # the remembered attempt stands on its own: the matrix compiles it from the record
    out = net.forks.matrix(agent, slug, name, best, contexts="file,alone")
    assert all(c["compiled"] for c in out["cells"]), out["text"]
    net.release(agent, name, "testing", slug)


@needs_tools
def test_the_permuter_compiles_a_round_in_one_build_and_keeps_what_matches(fork, monkeypatch):
    """The rewrites themselves are permuter.py's; here one of them is the right code, so the run has to find it,
    put it through the matrix and commit it."""
    net, slug, agent = fork
    root = Path(net.project_row(slug)["root"])
    name = "count_alive_entities"
    good = _ref(name)
    near = good.replace("return n;", "return n + 1;")
    first = net.forks.matrix(agent, slug, name, near, again=True)
    assert not first["matched"]
    made = iter([near.replace("n + 1", "n + 2"), "not C at all {", good])
    monkeypatch.setattr(forkmatrix, "mutate", lambda source, rng: next(made, None))
    builds = len(list((root / ".decomp-wtf").glob("matrix/*")))
    res = net.forks.matrix_permute(agent, slug, name, seconds=30)
    assert res["matched"] and res["committed"], res["text"]
    assert res["variants"] == 2 and res["improved"] == 1 and res["after"] == 0 and res["before"] > 0
    assert "Permuter: 2 rewrites" in res["text"] and "MATCH!" in res["text"]
    assert net.store.one("SELECT status FROM functions WHERE name=?", name)["status"] == "matched"
    assert good.split("{", 1)[1].strip() in (root / "src" / "game.c").read_text()
    assert len(list((root / ".decomp-wtf").glob("matrix/*"))) == builds  # nothing left behind
    # nothing closer: said so, and nothing recorded as progress
    with pytest.raises(NetworkError):
        net.forks.matrix_permute(agent, slug, "no_such_function")
    status = net.forks.matrix_status(slug)
    assert status["permute"] is None
    net.forks.matrix_permute_sweep(slug, seconds=1)
    job = net.forks._permute_jobs[slug]
    for _ in range(600):
        if job["state"] != "running":
            break
        job["stop"].wait(0.1)
    assert job["state"] == "done" and net.forks.matrix_status(slug)["permute"]["done"] == job["total"]


@needs_tools
def test_the_projects_own_c_is_the_brief_and_what_the_matrix_compiles(fork):
    """A project that keeps functional C for unmatched functions: an agent starts from it, and the matrix (so the
    sweep too) compiles it for a function with no C in its file."""
    net, slug, agent = fork
    root = Path(net.project_row(slug)["root"])
    name = "game_tick"
    fn = net.store.one("SELECT * FROM functions WHERE name=?", name)
    if fn["status"] != "open" or fn["best_source"]:
        pytest.skip("game_tick isn't open and untouched in this run")
    fp = net.forks._fork_project(net.project_row(slug))
    assert net.forks.project_seed(fp, fn, fp.unit(fn["unit"])) is None  # the project names no seeds
    manifest = json.loads((root / ".decomp.wtf.json").read_text())
    manifest["seeds"] = ["../outside/{symbol}.c", "equiv/{target}/{symbol}.c"]
    (root / ".decomp.wtf.json").write_text(json.dumps(manifest))
    (root / "equiv" / "game").mkdir(parents=True)
    (root / "equiv" / "game" / f"{name}.c").write_text('#include "game.h"\n\n' + _ref(name) + "\n")
    try:
        rel, text = net.forks.project_seed(fp, fn, fp.unit(fn["unit"]))
        assert rel == f"equiv/game/{name}.c" and _ref(name) in text
        brief = net.get_function(name, slug, agent)
        assert "The project's own C for this function (equiv/game/game_tick.c)" in brief and "Machine draft" not in brief
        res = net.forks.matrix(agent, slug, name)
        assert res["matched"] and res["committed"], res["text"]
        assert net.store.one("SELECT status FROM functions WHERE name=?", name)["status"] == "matched"
    finally:
        manifest.pop("seeds")
        (root / ".decomp.wtf.json").write_text(json.dumps(manifest))


@needs_tools
def test_a_project_can_name_who_writes_the_rewrites(fork):
    """With "permute" in the manifest's matrix, each round's rewrites come from the project's command (a real
    project puts decomp-permuter behind it; here it hands back the right code, or says it can't read the C)."""
    net, slug, agent = fork
    root = Path(net.project_row(slug)["root"])
    name = "entity_update_physics"
    fn = net.store.one("SELECT * FROM functions WHERE name=?", name)
    if fn["status"] != "open":
        pytest.skip("entity_update_physics isn't open in this run")
    good = _ref(name)
    near = good.replace("{", "{\n    volatile s32 unused_slot = 0;", 1)
    manifest = json.loads((root / ".decomp.wtf.json").read_text())
    before = net.forks.matrix_config(net.forks._fork_project(net.project_row(slug)))["digest"]
    manifest["matrix"]["permute"] = ["python", "tools/rewrites.py", "{job}"]
    (root / ".decomp.wtf.json").write_text(json.dumps(manifest))
    script = root / "tools" / "rewrites.py"
    try:
        cfg = net.forks.matrix_config(net.forks._fork_project(net.project_row(slug)))
        assert cfg["permute"] and cfg["digest"] == before  # naming a permuter leaves the record valid
        first = net.forks.matrix(agent, slug, name, near)
        if first["matched"]:
            pytest.skip("the near miss compiles to the same code here")
        # it can't read the C: said so, and the built-in rewrites take over
        script.write_text('import json, sys\nfrom pathlib import Path\njob = json.loads(Path(sys.argv[1]).read_text())\n'
                          'Path(job["out"]).write_text(json.dumps({"variants": [], "error": "ParseError: nope"}))\n')
        res = net.forks.matrix_permute(agent, slug, name, seconds=2)
        assert "the project's permuter gave none: ParseError: nope" in res["text"] or res["matched"], res["text"]
        if res["matched"]:
            return
        script.write_text('import json, sys\nfrom pathlib import Path\njob = json.loads(Path(sys.argv[1]).read_text())\n'
                          'assert job["function"] == "entity_update_physics" and job["count"] == 24\n'
                          'assert "entity_update_physics" in Path(job["source"]).read_text()\n'
                          f'Path(job["out"]).write_text(json.dumps({{"variants": [{good!r}, "junk"]}}))\n')
        res = net.forks.matrix_permute(agent, slug, name, seconds=20)
        assert res["matched"] and res["committed"] and res["variants"] == 1, res["text"]
        assert not list((root / ".decomp-wtf" / "matrix").iterdir())
    finally:
        manifest["matrix"].pop("permute")
        (root / ".decomp.wtf.json").write_text(json.dumps(manifest))
        script.unlink(missing_ok=True)
