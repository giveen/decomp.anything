from __future__ import annotations

import time
from pathlib import Path

from decompnet import coffobj
from decompnet.app.server import Jobs, Settings
from decompnet.forks.csource import find
from decompnet.forks.drafts import self_contained
from decompnet.rom import n64


def test_coffobj_string_exceeding_256_bytes():
    # Construct a string table with a symbol longer than 256 bytes
    long_name = "A" * 300
    table = b"\x00" * 4 + b"short\x00" + long_name.encode("ascii") + b"\x00"
    offset = 4 + len(b"short\x00")
    assert coffobj._string(table, offset) == long_name


def test_n64_guess_compiler_continues_past_data_words():
    # Create ROM bytes with an unimplemented instruction/data word at the start,
    # followed by several valid IDO branches
    # Unimplemented/data word (e.g. 0x0000003f which is reserved/invalid or non-standard in rabbitizer)
    data_word = (0x03FFFFFF).to_bytes(4, "big")
    # beq $zero, $zero (IDO branch): 0x10000001
    branch = (0x10000001).to_bytes(4, "big")
    rom = b"\x00" * 0x1000 + data_word * 5 + branch * 10
    assert n64.guess_compiler(rom, start=0x1000, limit=0x100) == "IDO"


def test_csource_macro_without_semicolon_does_not_consume_next_function():
    # If a file has a macro invocation without a semicolon followed by a real function,
    # find should not mistake the macro for a function definition with the next
    # function's body.
    src = """
MACRO_CALL(foo, bar)

int actual_func(int x) {
    return x + 1;
}
"""
    assert find(src, "MACRO_CALL") is None
    actual = find(src, "actual_func")
    assert actual is not None
    assert src[actual[0]:actual[1]].strip().startswith("int actual_func(int x)")


def test_drafts_self_contained_handles_function_pointers():
    # m2c draft with a global function pointer declaration
    draft_text = """
s32 (*D_80100200)(void *, s32);
void f(void) {
    D_80100200(0, 1);
}
"""
    span = (draft_text.find("void f(void) {"), len(draft_text))
    # If file_text contains s32, it should NOT drop D_80100200
    file_text = "typedef int s32;\n"
    res = self_contained(draft_text, span, file_text=file_text)
    # Must declare extern s32 (*D_80100200)(void *, s32); inside f
    assert "extern s32 (*D_80100200)(void *, s32);" in res


def test_settings_atomic_save(tmp_path: Path):
    s_path = tmp_path / "settings.json"
    s = Settings(s_path)
    s.save_endpoint({"name": "test", "provider": "openai", "base_url": "http://localhost:8000"})
    s.save()
    assert s_path.exists()
    s2 = Settings(s_path)
    eps = s2.endpoints()
    assert len(eps) == 1
    assert eps[0]["name"] == "test"


def test_jobs_prune():
    jobs = Jobs()
    # Add a completed job that finished in the past
    jid = jobs.start("test", lambda log: "ok")
    # Wait for completion
    jobs.done[jid].wait(timeout=2.0)
    assert jobs.jobs[jid]["state"] == "done"
    # Artificially age the job
    jobs.jobs[jid]["t"] = time.time() - 700
    # Starting a new job triggers _prune()
    jobs.start("test2", lambda log: "ok2")
    assert jid not in jobs.jobs
    assert jid not in jobs.done
