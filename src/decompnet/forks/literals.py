"""The constants a matched function loads, compared with the original's.

objdiff's progress rules (functionRelocDiffs: none) ignore what a relocation points at, so the compiler's @91 can
stand for the original's lbl_802C2BD0: names and places in a partly decompiled file differ, values don't. But the
same rule would count a function that loads 1.2 where the original loads 1.1 as matched. Where both objects hold
the data an instruction loads (a float in .sdata2, a double in .rodata, a string), the values are compared here.
Data outside an object (an extern) can't be checked, and isn't.

A switch's jump table is data too, and the same rule lets one pass whose `case` labels sit on the wrong blocks: the
instructions match, the table sends a case elsewhere, and only the linked game shows it. Where both objects hold
the table, each entry is compared by where it lands in the function (the original names the place after its address,
the compiler after its own bookkeeping), and a difference says which case to move.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from elftools.elf.elffile import ELFFile
from elftools.elf.relocation import RelocationSection

from ..elfobj import _function_symbols, _section_target
from ..rom.match import _raw_relocs

# Bytes a load or store moves, by mnemonic (PowerPC, MIPS). Anything else (an address computed with addi or lui)
# is only compared when both sides point at a C string.
_WIDTH = {
    **dict.fromkeys(("lfd", "lfdu", "lfdx", "lfdux", "stfd", "stfdu", "ldc1", "sdc1", "ld", "sd", "ldl", "ldr"), 8),
    **dict.fromkeys(("lfs", "lfsu", "lfsx", "stfs", "stfsu", "lwz", "lwzu", "lwzx", "stw", "stwu",
                    "lwc1", "swc1", "lw", "lwu", "sw", "l.s", "s.s"), 4),
    **dict.fromkeys(("lhz", "lhzu", "lha", "lhau", "sth", "sthu", "lh", "lhu", "sh"), 2),
    **dict.fromkeys(("lbz", "lbzu", "stb", "stbu", "lb", "lbu", "sb"), 1),
}
_ADDRESS = {"addi", "addic", "addiu", "daddiu", "ori", "la", "li"}


@dataclass
class Mismatch:
    offset: int  # in the function
    mnemonic: str
    original: str  # the original's label
    theirs: bytes
    yours: bytes
    big_endian: bool = True

    def describe(self) -> str:
        return (f"+0x{self.offset:X} {self.mnemonic}: the original's {self.original} holds "
                f"{_show(self.theirs, self.big_endian)}, yours {_show(self.yours, self.big_endian)}")


@dataclass
class JumpTable:
    offset: int  # of the instruction that addresses the table, in the function
    mnemonic: str
    original: str  # the original's label for the table
    theirs: list[int]  # where each entry lands, as an offset in the function
    yours: list[int]

    def describe(self) -> str:
        if len(self.theirs) != len(self.yours):
            return (f"+0x{self.offset:X} {self.mnemonic}: the original's jump table {self.original} has {len(self.theirs)} "
                    f"entries, yours {len(self.yours)}: the switch's lowest or highest case differs")
        wrong = [k for k, (a, b) in enumerate(zip(self.theirs, self.yours, strict=True)) if a != b]
        cases = ", ".join(f"entry {k} goes to +0x{self.yours[k]:X}, the original's to +0x{self.theirs[k]:X}"
                          for k in wrong[:4])
        more = f" (and {len(wrong) - 4} more)" if len(wrong) > 4 else ""
        return (f"+0x{self.offset:X} {self.mnemonic}: jump table {self.original}: {cases}{more}. The code matches: "
                "move those `case` labels (entry k is case lowest+k) to the blocks the original sends them to")


def _show(data: bytes, big: bool = True) -> str:
    e = ">" if big else "<"
    if len(data) == 8:
        return f"{struct.unpack(e + 'd', data)[0]!r} (0x{data.hex().upper()})"
    if len(data) == 4:
        return f"{struct.unpack(e + 'f', data)[0]!r} (0x{data.hex().upper()})"
    if data.endswith(b"\0") and all(32 <= c < 127 or c in (9, 10, 13) for c in data[:-1]):
        return '"' + data[:-1].decode().replace("\n", "\\n") + '"'
    return "0x" + data.hex().upper()


class _Object:
    def __init__(self, path: Path):
        self.elf = ELFFile(BytesIO(path.read_bytes()))
        self.big = not self.elf.little_endian
        self.machine = self.elf["e_machine"]

    def refs(self, name: str) -> tuple[int, dict[int, tuple[str, bytes | None, int, int]]] | None:
        """(function size, {instruction offset: (label, the referenced section's bytes or None, offset in it,
        that section's index)})."""
        found = _function_symbols(self.elf).get(name)
        if found is None:
            return None
        shndx, start, size = found
        self.function = (shndx, start, size)
        code = self.elf.get_section(shndx).data()[start:start + size]
        raw = _raw_relocs(self.elf, shndx, start, size, code)
        out: dict[int, tuple[str, bytes | None, int, int]] = {}
        for k, r in enumerate(raw):
            residual = r["value"] if r["rela"] else _section_target("mips", raw, k)
            if residual is None:
                continue
            sym = r["symtab"].get_symbol(r["symidx"])
            target = sym["st_shndx"]
            if not isinstance(target, int) or target == 0:
                out[r["off"] & ~3] = (sym.name, None, 0, 0)  # outside this object
                continue
            sec = self.elf.get_section(target)
            section_sym = sym["st_info"]["type"] == "STT_SECTION"
            data = None if sec["sh_type"] == "SHT_NOBITS" or sec["sh_flags"] & 0x4 else sec.data()
            label = sec.name if section_sym else sym.name
            out[r["off"] & ~3] = (label, data, residual + (0 if section_sym else sym["st_value"]), target)
        return size, out

    def table(self, section: int, at: int) -> list[int]:
        """Where each entry of a jump table at `at` in `section` lands, as offsets in the function last read by
        `refs`: consecutive 32-bit words relocated into that function. Empty when there's no such table there."""
        fshndx, fstart, fsize = self.function
        words: dict[int, int] = {}
        for rsec in self.elf.iter_sections():
            if not isinstance(rsec, RelocationSection) or rsec["sh_info"] != section:
                continue
            symtab = self.elf.get_section(rsec["sh_link"])
            data = self.elf.get_section(section).data()
            for rel in rsec.iter_relocations():
                if rel["r_info_type"] != 2:  # R_MIPS_32: a word holding an address
                    continue
                sym = symtab.get_symbol(rel["r_info_sym"])
                if sym["st_shndx"] != fshndx:
                    continue
                off = rel["r_offset"]
                addend = rel["r_addend"] if rel.is_RELA() else int.from_bytes(
                    data[off:off + 4], "big" if self.big else "little")
                base = 0 if sym["st_info"]["type"] == "STT_SECTION" else sym["st_value"]
                words[off] = base + addend
        out, off = [], at
        while off in words and fstart <= words[off] < fstart + fsize:
            out.append(words[off] - fstart)
            off += 4
        return out


def _string(data: bytes, at: int) -> bytes | None:
    end = data.find(b"\0", at, at + 512)
    if end < 0:
        return None
    s = data[at:end + 1]
    return s if all(32 <= c < 127 or c in (9, 10, 13) for c in s[:-1]) else None


def mismatches(target: Path, base: Path, symbol: str, insns: list[tuple[int, str]]) -> list[Mismatch | JumpTable]:
    """Loaded constants that differ, for a function whose instructions match (`insns`: the original's (offset in
    the function, mnemonic), in order)."""
    try:
        left, right = _Object(target), _Object(base)
    except (OSError, ValueError):
        return []
    if left.machine != right.machine or left.machine not in ("EM_PPC", "EM_MIPS"):
        return []
    try:
        a, b = left.refs(symbol), right.refs(symbol)
    except Exception:  # noqa: BLE001 an unusual object: no verdict rather than a wrong one
        return []
    if a is None or b is None:
        return []
    out: list[Mismatch | JumpTable] = []
    for at, mnemonic in insns:
        ra, rb = a[1].get(at), b[1].get(at)
        if not ra or not rb or ra[1] is None or rb[1] is None:
            continue
        (label, da, oa, sa), (_, db, ob, sb) = ra, rb
        if da is None or db is None or oa is None or ob is None:  # one side has no datum behind this reference
            continue
        if left.machine == "EM_MIPS" and (mnemonic in _ADDRESS or mnemonic == "lw"):
            try:
                ta, tb = left.table(sa, oa), right.table(sb, ob)
            except Exception:  # noqa: BLE001 an unusual object: no verdict rather than a wrong one
                ta = tb = []
            if ta or tb:  # a jump table on either side: its words are addresses, not constants to compare
                if ta and tb and ta != tb:
                    out.append(JumpTable(at, mnemonic, label, ta, tb))
                continue
        width = _WIDTH.get(mnemonic)
        if width:
            theirs, yours = da[oa:oa + width], db[ob:ob + width]
            if len(theirs) != width or len(yours) != width:
                continue
        elif mnemonic in _ADDRESS:
            theirs, yours = _string(da, oa), _string(db, ob)
            if theirs is None or yours is None:
                continue
        else:
            continue
        if theirs != yours:
            out.append(Mismatch(at, mnemonic, label, theirs, yours, left.big))
    return out
