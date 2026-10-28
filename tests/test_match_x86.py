"""An exact PC match must validate relocation destinations, including embedded COFF addends."""

import shutil
import struct
import subprocess

import pytest

from decompnet import coffobj
from decompnet.rom import match_x86
from decompnet.rom.symbols import Symbol, SymbolTable


def _coff(code, arch, field, rtype, symbol="arr"):
    """One real COFF section, a function symbol, and an external relocation target."""
    relptr = 60 + len(code)
    symptr = relptr + 10
    header = struct.pack("<HHIIIHH", 0x8664 if arch == "x86_64" else 0x14C, 1, 0, symptr, 2, 0, 0)
    section = struct.pack("<8sIIIIIIHHI", b".text", 0, 0, len(code), 60, relptr, 0, 1, 0, 0x60000020)
    reloc = struct.pack("<IIH", field, 1, rtype)
    symbols = struct.pack("<8sIhHBB", b"get", 0, 1, 0x20, 2, 0)
    symbols += struct.pack("<8sIhHBB", symbol.encode(), 0, 0, 0, 2, 0)
    return header + section + code + reloc + symbols + struct.pack("<I", 4)


@pytest.mark.parametrize("arch,rtype,prefix,field,target_type", [
    ("x86_64", 4, b"\x8b\x05", 2, 4),  # RIP-relative load
    ("x86", 6, b"\xa1", 1, 6),  # absolute load
    ("x86_64", 2, b"\x8b\x04\x25", 3, 10),  # COFF ADDR32, not ELF PC32
    ("x86_64", 1, b"\x48\xb8", 2, 1),  # 64-bit absolute address
])
@pytest.mark.parametrize("addend", [-4, 0, 4])
def test_coff_relocation_addends(arch, rtype, prefix, field, target_type, addend):
    base, pc = 0x402000, 0x401000
    width = 8 if rtype == 1 else 4
    mask = (1 << (width * 8)) - 1
    code = prefix + (addend & mask).to_bytes(width, "little") + b"\xc3"
    obj = _coff(code, arch, field, rtype)
    parsed = coffobj.extract_function(obj, "get")
    assert parsed is not None
    assert parsed.relocs[0].addend & 0xFFFFFFFF == addend & 0xFFFFFFFF
    for original_addend in (0, addend):
        dest = base + original_addend
        value = dest - (pc + field + 4) if target_type == 4 else dest
        original = prefix + (value & mask).to_bytes(width, "little") + b"\xc3"
        target = match_x86.X86Target("get", arch, original, [(field, target_type, dest, field + width)])
        result = match_x86.link(obj, "get", target, SymbolTable([Symbol("arr", base)]))
        assert result.exact == (original_addend == addend), result.problems


def test_coff_section_offset_is_relative_to_the_resolved_symbol():
    # The relocation names .data+4; arr itself starts at .data+4, so the named addend is zero.
    code = bytes.fromhex("8b 05 04 00 00 00 c3")
    body = b"\0" * 8
    relptr = 100 + len(code) + len(body)
    header = struct.pack("<HHIIIHH", 0x8664, 2, 0, relptr + 10, 3, 0, 0)
    text = struct.pack("<8sIIIIIIHHI", b".text", 0, 0, len(code), 100, relptr, 0, 1, 0, 0x60000020)
    data = struct.pack("<8sIIIIIIHHI", b".data", 0, 0, len(body), 100 + len(code), 0, 0, 0, 0, 0xC0000040)
    reloc = struct.pack("<IIH", 2, 1, 4)
    symbols = struct.pack("<8sIhHBB", b"get", 0, 1, 0x20, 2, 0)
    symbols += struct.pack("<8sIhHBB", b".data", 0, 2, 0, 3, 0)
    symbols += struct.pack("<8sIhHBB", b"arr", 4, 2, 0, 2, 0)
    obj = header + text + data + code + body + reloc + symbols + struct.pack("<I", 4)
    parsed = coffobj.extract_function(obj, "get")
    assert parsed is not None
    assert parsed.relocs[0].symbol == "arr" and parsed.relocs[0].addend == 0
    target = match_x86.X86Target("get", "x86_64", bytes.fromhex("8b 05 fe 0f 00 00 c3"), [(2, 4, 0x402004, 6)])
    assert match_x86.link(obj, "get", target, SymbolTable([Symbol("arr", 0x402004)])).exact


def test_coff_rel32_bias_for_an_instruction_with_a_trailing_immediate():
    # mov DWORD PTR [rip + arr + 4], 7: REL32_4 accounts for the immediate after the displacement.
    code = bytes.fromhex("c7 05 04 00 00 00 07 00 00 00 c3")
    original = bytes.fromhex("c7 05 fa 0f 00 00 07 00 00 00 c3")
    target = match_x86.X86Target("get", "x86_64", original, [(2, 4, 0x402004, 10)])
    symbols = SymbolTable([Symbol("arr", 0x402000)])
    assert match_x86.link(_coff(code, "x86_64", 2, 8), "get", target, symbols).exact
    assert not match_x86.link(_coff(code, "x86_64", 2, 4), "get", target, symbols).exact


def test_coff_unknown_symbol_cannot_match_a_zero_displacement():
    code = bytes.fromhex("e9 00 00 00 00")
    target = match_x86.X86Target("get", "x86_64", code, [(1, 4, 0x401005, 5)])
    symbols = SymbolTable([Symbol("right", 0x401005, kind="func")])
    result = match_x86.link(_coff(code, "x86_64", 1, 4, "wrong"), "get", target, symbols)
    assert not result.exact
    assert any("`wrong` is not a known symbol" in p for p in result.problems)


@pytest.mark.skipif(shutil.which("gcc") is None, reason="needs gcc")
def test_elf_unknown_symbol_cannot_match_a_zero_displacement(tmp_path):
    source, obj = tmp_path / "call.c", tmp_path / "call.o"
    source.write_text("extern int wrong(void); int get(void) { return wrong(); }\n")
    subprocess.run(["gcc", "-O2", "-fcf-protection=none", "-fno-pie", "-c", str(source), "-o", str(obj)], check=True)
    code = bytes.fromhex("e9 00 00 00 00")
    target = match_x86.X86Target("get", "x86_64", code, [(1, 4, 0x401005, 5)])
    result = match_x86.link(obj.read_bytes(), "get", target, SymbolTable([Symbol("right", 0x401005)]))
    assert not result.exact
    assert any("`wrong` is not a known symbol" in p for p in result.problems)
    # Resolving the symbol to the actual destination establishes a valid match.
    assert match_x86.link(obj.read_bytes(), "get", target, SymbolTable([Symbol("wrong", 0x401005)])).exact


@pytest.mark.parametrize("cc", ["x86_64-w64-mingw32-gcc", "i686-w64-mingw32-gcc"])
def test_compiled_array_accesses_must_use_the_original_index(tmp_path, cc):
    if shutil.which(cc) is None:
        pytest.skip(f"needs {cc}")
    from decompnet.rom import pe

    flags = ["-O2", "-ffreestanding", "-fno-asynchronous-unwind-tables", "-fno-unwind-tables", "-fno-stack-protector"]
    source, exe = tmp_path / "original.c", tmp_path / "original.exe"
    source.write_text("int arr[4]; int get(void) { return arr[0]; }\n")
    entry = "get" if cc.startswith("x86_64") else "_get"
    subprocess.run([cc, *flags, "-nostdlib", f"-Wl,--entry,{entry}", str(source), "-o", str(exe)], check=True)
    image = pe.read(exe.read_bytes())
    fn = next(f for f in image.functions if f.name in ("get", "_get"))
    target = match_x86.X86Target("get", image.arch, fn.code, fn.refs)
    for index in (0, 1):
        candidate, obj = tmp_path / "candidate.c", tmp_path / "candidate.o"
        candidate.write_text(f"int arr[4]; int get(void) {{ return arr[{index}]; }}\n")
        subprocess.run([cc, *flags, "-c", str(candidate), "-o", str(obj)], check=True)
        result = match_x86.link(obj.read_bytes(), "get", target, SymbolTable(image.symbols))
        assert result.exact == (index == 0), result.problems
