"""Tests for agent solution velocity, compilation caching, and decompilation performance optimizations."""

from __future__ import annotations

import json
import sys

from decompnet import compiler, scoring
from decompnet.app import agents, assistance, tools
from decompnet.app.engine import RomNetwork
from decompnet.compiler import CompilerConfig, compile_source
from decompnet.elfobj import FunctionObj
from decompnet.network import Network
from decompnet.rom import library
from decompnet.seeds import _context_for_m2c


def test_function_obj_caching_and_exact_diff_short_circuit():
    code = b"\x90\x90\x90\xc3"
    t = FunctionObj("dummy", "x86", False, code, [])
    assert t._decomp_insns is None
    assert t._fingerprint is None

    # First fingerprint call computes and caches
    fp1 = t.fingerprint()
    assert t._fingerprint == fp1
    assert t.fingerprint() == fp1

    # First disassemble call computes and caches
    insns1 = scoring.disassemble(t)
    assert t._decomp_insns is not None
    assert len(insns1) == 4
    assert scoring.disassemble(t) is insns1

    # Exact diff short circuit
    c = FunctionObj("dummy", "x86", False, code, [])
    d = scoring.diff(t, c)
    assert d.bytes_match is True
    assert d.score == 0
    assert len(d.rows) == 4
    assert all(m == " " for m, _, _ in d.rows)


def test_seeds_context_sanitization_for_pycparser():
    raw_header = """
#ifndef HEADER_H
#define HEADER_H
#include <stdio.h>

enum EntityType;

struct Player {
    int hp;
    int max_hp;
} __attribute__((aligned(16))) __attribute__((packed));

void __attribute__((noinline)) update_player(struct Player *p);
int __extension__ compute(int __restrict__ val);
void do_asm(void) {
    __asm__("nop");
}

#endif
"""
    cleaned = _context_for_m2c(raw_header)
    assert "#ifndef" not in cleaned
    assert "#define" not in cleaned
    assert "#include" not in cleaned
    assert "__attribute__" not in cleaned
    assert "enum EntityType;" not in cleaned
    assert "__extension__" not in cleaned
    assert "__restrict__" not in cleaned
    assert "__asm__" not in cleaned
    assert "struct Player {" in cleaned
    assert "int hp;" in cleaned


def test_library_sdk_and_runtime_recognition():
    # N64 Libultra
    assert library.kind("osCreateThread") == "SDK library"
    assert library.kind("guPerspective") == "SDK library"
    assert library.kind("__osSetSR") == "SDK library"
    assert library.kind("bzero") == "SDK library"

    # PS1 Psy-Q
    assert library.kind("DrawSync") == "SDK library"
    assert library.kind("SpuInit") == "SDK library"
    assert library.kind("CdRead") == "SDK library"

    # GBA AGB Lib / Sound
    assert library.kind("AgbSysCall") == "SDK library"
    assert library.kind("m4aSoundInit") == "SDK library"
    assert library.kind("MPlayOpen") == "SDK library"

    # Libgcc softfloat / runtime
    assert library.kind("__divsi3") == "compiler runtime"
    assert library.kind("__muldi3") == "compiler runtime"

    # User code / game code should stay open (None)
    assert library.kind("ostrich_run") is None
    assert library.kind("pad_input") is None
    assert library.kind("player_update") is None
    assert library.kind("sub_140001020") is None
    assert library.kind("func_80001000") is None


def test_compiler_source_cache(tmp_path):
    compiler.clear_compile_cache()
    # Dummy compiler config that calls python to echo an object
    py_script = tmp_path / "mock_cc.py"
    py_script.write_text(
        "import sys, pathlib\n"
        "out = pathlib.Path(sys.argv[1])\n"
        "out.write_bytes(b'\\x7fELF\\x01\\x01\\x01\\x00' + b'\\x00' * 50)\n"
    )
    cfg = CompilerConfig(
        cmd=[sys.executable, str(py_script), "{obj}"],
        sandbox="none",
    )
    header = "typedef int s32;"
    source = "s32 foo(void) { return 42; }"

    res1 = compile_source(cfg, header, source, trusted=True)
    assert res1.ok
    assert res1.obj is not None

    # Repeated call should hit cache immediately
    res2 = compile_source(cfg, header, source, trusted=True)
    assert res2.ok
    assert res2.obj == res1.obj

    # Different source should not hit cache
    source2 = "s32 bar(void) { return 99; }"
    res3 = compile_source(cfg, header, source2, trusted=True)
    assert res3.ok

    compiler.clear_compile_cache()


def test_fit_preserves_attempt_header():
    messages = [
        {"role": "system", "content": "You are a decompiling agent."},
        {"role": "user", "content": "Decompile func_80001000."},
        {
            "role": "tool",
            "content": (
                "score 120 (improved by +15 pts vs previous attempt) (85.2% match) — best so far 100.\n"
                "Target 40 insns, yours 40.\n"
                "   TARGET | YOURS\n"
                + "   lw $a0, 0($sp) | lw $a0, 4($sp)\n" * 50
            ),
        },
        {"role": "assistant", "content": "Let me adjust the stack variable."},
        {"role": "user", "content": "Next step."},
    ]
    budget = 100
    agents.fit(messages, budget)
    tool_content = messages[2]["content"]
    assert "score 120 (improved by +15 pts vs previous attempt)" in tool_content
    assert "Target 40 insns, yours 40." in tool_content
    assert "trimmed" in tool_content


def test_network_attempt_deltas_and_duplicate_warning(tmp_path, monkeypatch):
    net = Network(tmp_path / "app.db")
    with net.store.tx() as db:
        db.execute(
            "INSERT INTO projects(id, slug, name, description, arch, big_endian, root, config) "
            "VALUES(1, 'demo', 'demo', 'desc', 'x86', 0, ?, '{}')",
            (str(tmp_path),),
        )
        db.execute(
            "INSERT INTO functions(id, project_id, name, size, n_insns, target, relocs, asm, unit, vram, rom_offset, status, best_score) "
            "VALUES(1, 1, 'f', 4, 1, ?, '[]', 'nop', 'main', 0, 0, 'open', 100)",
            (b"\x90\x90\x90\xc3",),
        )
        db.execute("INSERT INTO context_versions(project_id, version, header, created_at) VALUES(1, 1, '', 0)")

    agent = net.authenticate(net.register_agent("test-agent"))

    # Mock compile and evaluate to simulate attempts
    cand_obj1 = FunctionObj("f", "x86", False, b"\x90\x90\x90\x90", [])
    cand_obj2 = FunctionObj("f", "x86", False, b"\x90\x90\x90\x90", [])
    cand_obj3 = FunctionObj("f", "x86", False, b"\x90\x90\x90\xc3", [])

    call_count = 0

    def mock_eval(fn, proj, obj):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return cand_obj1, False, []
        if call_count == 2:
            return cand_obj2, False, []
        return cand_obj3, True, []

    monkeypatch.setattr(net, "_compile", lambda *args: compiler.CompileResult(True, b"fake_o", ""))
    monkeypatch.setattr(net, "_evaluate", mock_eval)

    # First attempt: score non-zero
    res1 = net.attempt(agent, "f", "int f() { return 1; }")
    assert not res1["matched"]
    assert "score" in res1["text"]

    # Second attempt: same source -> duplicate warning + same score delta
    res2 = net.attempt(agent, "f", "int f() { return 1; }")
    assert "⚠️ Identical source to your previous attempt" in res2["text"]
    assert "same score as previous attempt" in res2["text"]

    # Third attempt: matching code
    res3 = net.attempt(agent, "f", "int f() { return 0; }")
    assert res3["matched"]
    assert "✅ MATCH!" in res3["text"]


def test_tools_compile_guidance_when_assistance_discovered(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    net = RomNetwork(tmp_path / "app.db", tmp_path / "home")
    agent, _ = net.local_agent("test-agent")
    cfg = {"kind": "rom", "platform": "windows", "compiler": {"toolchain": "mingw-x86_64", "flags": []}}
    with net.store.tx() as db:
        pid = db.execute(
            "INSERT INTO projects(slug, name, arch, big_endian, root, config) "
            "VALUES('demo', 'demo', 'x86_64', 0, ?, ?)",
            (str(root), json.dumps(cfg)),
        ).lastrowid
        db.execute(
            "INSERT INTO functions(project_id, name, size, n_insns, target, relocs, asm, unit, vram, rom_offset, status, attempts) "
            "VALUES(?, 'f', 4, 1, ?, '[]', 'nop', 'main', 0, 0, 'open', 2)",
            (pid, b"\x90\x90\x90\xc3"),
        )
        db.execute("INSERT INTO context_versions(project_id, version, header, created_at) VALUES(?, 1, '', 0)", (pid,))

    proj = net.project_row("demo")
    fn = net._function("f", "demo")

    # If assistance NOT discovered yet: prompts discover_assistance
    msg1 = tools._compile(net, agent, "demo", "f", "source")
    assert "Knowledge review needed for f" in msg1
    assert "discover_assistance" in msg1

    # If assistance WAS discovered: prompts directly to record_assistance
    meta = {
        "assistance": {
            "fingerprint": assistance.fingerprint(net, proj),
            "function_digest": assistance.function_digest(fn),
        }
    }
    with net.store.tx() as db:
        db.execute("UPDATE functions SET meta=? WHERE id=?", (json.dumps(meta), fn["id"]))

    msg2 = tools._compile(net, agent, "demo", "f", "source")
    assert "Knowledge review needed for f" in msg2
    assert "record_assistance(function='f'" in msg2
