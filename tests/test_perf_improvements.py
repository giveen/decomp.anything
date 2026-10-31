from __future__ import annotations

import io
import json
import struct
import tarfile
import zipfile
from pathlib import Path

from decompnet import coffobj, elfobj, rea
from decompnet.app import analysis, sources
from decompnet.app.engine import RomNetwork


def test_sources_asar_cache(tmp_path: Path):
    asar_file = tmp_path / "app.asar"
    header_dict = {"files": {"main.js": {"offset": "0", "size": 10}}}
    header_json = json.dumps(header_dict).encode("utf-8")
    header_block = struct.pack("<II", len(header_json), len(header_json)) + header_json
    full_asar = struct.pack("<II", 4, len(header_block)) + header_block + b"0123456789"
    asar_file.write_bytes(full_asar)

    # Clear cache to start
    sources._ASAR_CACHE.clear()

    idx1 = sources._asar_index(asar_file)
    assert "main.js" in idx1
    assert len(sources._ASAR_CACHE) == 1

    # Second read reuses cached dict
    idx2 = sources._asar_index(asar_file)
    assert idx2 is idx1


def test_trim_code_trailing_pad_optimization():
    # Return code immediately if there is no trailing pad
    # x86: nop (0x90), ret (0xC3)
    code_no_pad = b"\x90\x90\xc3"
    assert coffobj.trim_code(code_no_pad, "x86") == code_no_pad

    # Code with trailing padding: ret (0xC3) followed by 0x90 padding
    code_with_pad = b"\x90\xc3\x90\x90\xcc\x00"
    trimmed = coffobj.trim_code(code_with_pad, "x86")
    assert trimmed == b"\x90\xc3"


def test_read_entry_bytes_reusing_archive(tmp_path: Path):
    zip_path = tmp_path / "test.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("test.c", "int foo() { return 42; }")
        z.writestr("other.c", "int bar() { return 1; }")

    # Read with path
    data1 = analysis.read_entry_bytes(zip_path, "test.c")
    assert data1 == b"int foo() { return 42; }"

    # Read with open archive passed
    with zipfile.ZipFile(zip_path) as arc:
        data2 = analysis.read_entry_bytes(zip_path, "test.c", archive=arc)
        data3 = analysis.read_entry_bytes(zip_path, "other.c", archive=arc)
        assert data2 == b"int foo() { return 42; }"
        assert data3 == b"int bar() { return 1; }"

    # Test tarfile as well
    tar_path = tmp_path / "test.tar"
    with tarfile.open(tar_path, "w") as t:
        content = b"void tar_func() {}"
        ti = tarfile.TarInfo("func.c")
        ti.size = len(content)
        t.addfile(ti, io.BytesIO(content))

    with tarfile.open(tar_path) as arc:
        tar_data = analysis.read_entry_bytes(tar_path, "func.c", archive=arc)
        assert tar_data == content


def test_rea_binary_lock():
    lock1 = rea._binary_lock("/path/to/bin1")
    lock2 = rea._binary_lock("/path/to/bin2")
    lock1_again = rea._binary_lock("/path/to/bin1")

    assert lock1 is lock1_again
    assert lock1 is not lock2


def test_engine_fn_seed_lock(tmp_path: Path):
    engine = RomNetwork(tmp_path / "state.db", tmp_path / "workspace")
    l1 = engine._fn_seed_lock(101)
    l2 = engine._fn_seed_lock(102)
    l1_again = engine._fn_seed_lock(101)

    assert l1 is l1_again
    assert l1 is not l2


def test_elfobj_get_symbols_by_section_caching():
    class DummySymbol(dict):
        def __init__(self, name, **kwargs):
            super().__init__(**kwargs)
            self.name = name

    class DummySymtab:
        def __init__(self, symbols):
            self.symbols = symbols
            self.calls = 0

        def iter_symbols(self):
            self.calls += 1
            return list(self.symbols)

    s1 = DummySymbol(
        name="foo",
        st_shndx=1,
        st_info={"type": "STT_FUNC", "bind": "STB_GLOBAL"},
        st_value=0x100,
        st_size=0x20,
    )
    s2 = DummySymbol(
        name="bar",
        st_shndx=1,
        st_info={"type": "STT_FUNC", "bind": "STB_LOCAL"},
        st_value=0x120,
        st_size=0x10,
    )
    symtab = DummySymtab([s1, s2])

    # First call builds cache and queries iter_symbols
    by_sec = elfobj._get_symbols_by_section(symtab)
    assert 1 in by_sec
    assert len(by_sec[1]) == 2
    assert symtab.calls == 1

    # Second call should use cache without calling iter_symbols again
    by_sec2 = elfobj._get_symbols_by_section(symtab)
    assert by_sec2 is by_sec
    assert symtab.calls == 1
