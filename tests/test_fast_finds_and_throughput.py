"""Tests for compiler RAM workspace pooling, permuter mutations, parallel permuter batches,
PPC/x86 disassembly similarity, and autopilot velocity improvements."""

from __future__ import annotations

import random
from unittest.mock import MagicMock

from decompnet import compiler, permuter, similarity
from decompnet.app.permute import Permuter, State


def test_compiler_workspace_and_slot_queue():
    ws_root = compiler._workspace_root()
    assert ws_root.exists()
    assert ws_root.is_dir()
    assert not compiler._SLOT_QUEUE.empty()
    slot_id = compiler._SLOT_QUEUE.get()
    try:
        assert isinstance(slot_id, int)
        assert slot_id >= 0
    finally:
        compiler._SLOT_QUEUE.put(slot_id)


def test_compiler_cache_sharding():
    assert len(compiler._CACHE_SHARDS) == compiler._CACHE_SHARDS_N
    assert len(compiler._CACHE_LOCKS) == compiler._CACHE_SHARDS_N

    key = ("test_tc", "s32", "int f(void) { return 1; }", ("-O2",))
    res = compiler.CompileResult(True, b"fake_obj", "log")
    compiler._cache_store(key, res)

    retrieved = compiler._cache_lookup(key)
    assert retrieved is not None
    assert retrieved.ok is True
    assert retrieved.obj == b"fake_obj"


def test_permuter_mutate_ifelse_to_ternary():
    lines = [
        "int abs_val(int x) {",
        "    if (x > 0) res = x; else res = -x;",
        "    return res;",
        "}",
    ]
    rng = random.Random(42)
    mutated = permuter._mutate_ifelse_to_ternary(lines, rng)
    assert mutated is not None
    assert "?" in mutated[1] and ":" in mutated[1]

    # Test reverse direction (ternary to if-else)
    back = permuter._mutate_ifelse_to_ternary(mutated, rng)
    assert back is not None
    assert "if (" in back[1] and "else" in back[1]


def test_permuter_mutate_while_and_dowhile():
    lines = [
        "void loop(int n) {",
        "    while (n > 0) {",
        "        n--;",
        "    }",
        "}",
    ]
    rng = random.Random(42)
    mutated = permuter._mutate_while_and_dowhile(lines, rng)
    assert mutated is not None
    assert "do {" in mutated[1]
    assert "} while (n > 0);" in mutated[3]


def test_permuter_mutate_array_vs_pointer():
    lines = [
        "int get(int *a, int i) {",
        "    return a[i];",
        "}",
    ]
    rng = random.Random(42)
    mutated = permuter._mutate_array_vs_pointer(lines, rng)
    assert mutated is not None
    assert "*(a + i)" in mutated[1]

    # Test reverse pointer deref to array indexing
    back = permuter._mutate_array_vs_pointer(mutated, rng)
    assert back is not None
    assert "a[i]" in back[1]


def test_permuter_mutate_with_new_types():
    source = """int compute(int x) {
    s16 count = 10;
    u8 flag = 1;
    float scale = 1.0f;
    return count + flag;
}"""
    rng = random.Random(123)
    variations = set()
    for _ in range(50):
        m = permuter.mutate(source, rng)
        if m is not None and m != source:
            variations.add(m)
    assert len(variations) > 0


def test_similarity_exact_equality_short_circuit():
    a = ("addi", "addiu", "lw", "sw", "jr", "nop")
    assert similarity.score(a, a) == 1.0


def test_similarity_ppc_mnemonics():
    # PowerPC instructions: 0x38600001 (li r3, 1), 0x4e800020 (blr)
    ppc_bytes = bytes.fromhex("386000014e800020")
    mnemonics = similarity.ppc_mnemonics(ppc_bytes, big_endian=True)
    assert len(mnemonics) == 2
    assert mnemonics[0] in ("li", "addi")
    assert mnemonics[1] == "blr"


def test_similarity_x86_mnemonics():
    # x86 instructions: 31 c0 (xor eax, eax), c3 (ret)
    x86_bytes = bytes.fromhex("31c0c3")
    mnemonics = similarity.x86_mnemonics(x86_bytes, is_64=False)
    assert len(mnemonics) == 2
    assert mnemonics[0] == "xor"
    assert mnemonics[1] == "ret"


def test_permuter_batch_execution(monkeypatch):
    from decompnet.app import assistance

    monkeypatch.setattr(assistance, "reviewed", lambda *args, **kwargs: True)
    mock_net = MagicMock()
    mock_net.project_row.return_value = {"id": 1, "slug": "test_game", "config": "{}"}
    mock_net._function.return_value = {
        "id": 10,
        "name": "func_1",
        "best_source": "int func_1(void) {\n    int a = 1;\n    int b = 2;\n    return a + b;\n}\n",
        "best_score": 10,
        "unit": "main",
        "meta": None,
    }
    mock_net.fork_slug.return_value = None
    mock_tx = MagicMock()
    mock_db = MagicMock()
    mock_db.execute.return_value.fetchone.return_value = None
    mock_tx.__enter__.return_value = mock_db
    mock_net.store.tx.return_value = mock_tx

    p = Permuter(mock_net)
    agent = MagicMock()
    agent.id = "agent-1"
    st = State()
    st.stop.set()  # Stop immediately after one check so loop terminates cleanly

    p._permute(agent, {"slug": "test_game"}, {"id": 10, "name": "func_1"}, st)
    assert st.tried == 1
    assert st.current == "func_1"
