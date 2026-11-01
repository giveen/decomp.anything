"""What REA contributes to an agent's work: the brief's REA block, type hints, miss hints and search.

The facts come from `analyze_function` and are kept on the function (one Ghidra call per function, ever). These tests use
the fake REA from tests/test_rea.py, so they run without Ghidra and assert the wiring rather than the decompiler.
"""

import json

import pytest

from decompnet.app import tools
from decompnet.app.engine import RomNetwork

from .test_rea import fake  # noqa: F401 - the fixture that puts the fake REA on DECOMP_REA


@pytest.fixture
def pc(tmp_path, monkeypatch, fake):  # noqa: F811
    """A PC project whose target is a file on disk: the shape REA-aware helpers expect."""
    monkeypatch.setenv("DECOMP_REA", " ".join(fake))
    home = tmp_path / "app"
    home.mkdir()
    net = RomNetwork(home / "app.db", home)
    root = tmp_path / "project"
    root.mkdir()
    (root / "demo.bin").write_bytes(b"\x7fELF" + b"\0" * 60)
    cfg = {"kind": "rom", "platform": "linux", "arch": "x86_64", "provider": "",
           "rom": {"title": "demo", "program": "demo.bin", "file": "demo.bin"}}
    with net.store.tx() as db:
        pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                         ("demo", "demo", "a PC program", "x86_64", 0, str(root), json.dumps(cfg))).lastrowid
        db.execute("INSERT INTO functions(project_id,name,size,n_insns,unit,vram,meta,status) VALUES(?,?,?,?,?,?,?,?)",
                   (pid, "FUN_00101000", 64, 12, ".text", 0x1000, json.dumps({"symbol": "FUN_00101000"}), "open"))
        db.execute("INSERT INTO symbols(project_id,name,address,size,kind,preview) VALUES(?,?,?,?,?,?)",
                   (pid, "FUN_00101000", 0x1000, 64, "func", None))  # what an import writes for every function
        db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                   "VALUES(?,?,?,?,?,?,?)", (pid, 1, "/* context.h */", None, "basic types", 0.0, ""))
    net.project_row = net.project_row  # (the row is read from the store like any project)
    agent = net.local_agent("tester")[0]
    return net, agent, "demo", "FUN_00101000", pid


def test_facts_are_fetched_once_and_kept_on_the_function(pc):
    net, _agent, slug, name, _pid = pc
    facts = net.rea_facts(slug, name)
    assert facts["signature"] == "int f(void)" and facts["calling_convention"] == "cdecl"
    assert "helper" in facts["callees"] and "hello world" in facts["strings"]
    stored = json.loads(net._function(name, slug)["meta"])["rea"]
    assert stored["signature"] == facts["signature"]  # kept, so the next reader doesn't call Ghidra again


def test_the_brief_says_what_rea_says(pc):
    net, _agent, slug, name, _pid = pc
    net.rea_facts(slug, name)
    lines = net.rea_brief_lines(net.project_row(slug), net._function(name, slug))
    text = "\n".join(lines)
    assert "REA says" in text and "int f(void)" in text and "cdecl" in text
    assert "helper" in text and "hello world" in text


def test_type_hint_reads_like_a_prototype(pc):
    net, _agent, slug, name, _pid = pc
    text = tools._type_hint(net, slug, name)
    assert "int f(void)" in text and "cdecl" in text and "helper" in text
    assert "inference, not proof" in text


def test_a_miss_is_explained_with_what_rea_saw(pc):
    net, _agent, slug, name, _pid = pc
    net.rea_facts(slug, name)
    text = tools._rea_hints(net, slug, name, "int FUN_00101000(void) { return 0; }", "❌ did not match")
    assert "REA's signature: `int f(void)`" in text
    assert "REA says it calls helper" in text  # the agent's source mentions neither
    assert "hello world" in text


def test_search_finds_what_rea_named(pc):
    net, _agent, slug, name, _pid = pc
    net.rea_facts(slug, name)
    found = net.search_facts(slug, "helper")
    assert "REA named" in found and name in found


def test_a_real_name_wins_over_what_rea_proposed(pc):
    net, _agent, slug, _name, pid = pc
    with net.store.tx() as db:  # the project's own name, and a REA proposal for the same address
        db.execute("INSERT INTO symbols(project_id,name,address,size,kind,preview) VALUES(?,?,?,?,?,?)",
                   (pid, "save_game", 0x2000, 32, "func", None))
        row = db.execute("SELECT config FROM projects WHERE id=?", (pid,)).fetchone()
        cfg = json.loads(row["config"])
        cfg["rea"] = {"names": {"0x2000": "FUN_00200000", "0x3000": "load_game"}}
        db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), pid))
    proj = net.project_row(slug)
    assert net.rea_name(proj, 0x2000) == "save_game"   # the project decided
    assert net.rea_name(proj, 0x3000) == "load_game"   # only REA has a name for it
    assert net.rea_name(proj, 0x4000) == ""            # nobody knows it


def test_adopt_rea_names_renames_only_automatic_names(pc):
    net, agent, slug, _name, pid = pc
    with net.store.tx() as db:
        row = db.execute("SELECT config FROM projects WHERE id=?", (pid,)).fetchone()
        cfg = json.loads(row["config"])
        cfg["rea"] = {"names": {"0x1000": "save_game"}}
        db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), pid))
    assert "Renamed 1 function" in net.adopt_rea_names(agent, slug)
    assert net._function("save_game", slug)["name"] == "save_game"


def test_the_autopilot_says_pc_drafts_come_from_rea(pc, monkeypatch):
    """Without REA a PC project gets no drafts (twins still run); the panel says why instead of failing silently."""
    net, _agent, slug, _name, _pid = pc
    monkeypatch.setattr(type(net), "rea_file", lambda self, proj: None)  # REA not set up for this project
    state = net.start_autopilot(slug)
    assert "drafts for x86 programs come from REA" in state["no_drafts"]
    net.stop_autopilot(slug)
