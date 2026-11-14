"""Strict evidence for compiler-matrix matches, independent of objdiff's similarity settings.

This checks a function's object bytes outside relocated fields and the identity of what every relocation refers to.
It does not claim that a partly reconstructed project links to the original image. Unsupported or unreadable objects
cannot verify a match.

A reference to a place inside the function itself is compared by where it lands, not by what the place is called:
the original's object names it after its address (`j L8001A22C`, from the disassembly), the compiler names it after
its own bookkeeping (`LM12`, or the function plus an offset), and both are the same instruction. Comparing those
names rejected 428 of the 1,420 functions a PlayStation project's byte-identical build already proves. A reference
to anything outside the function has to name the same symbol with the same addend.
"""

from __future__ import annotations

import hashlib
import json
from io import BytesIO
from pathlib import Path

from .. import coffobj, elfobj

VERSION = 2  # invalidate cached experiments whenever the verification contract changes
LOCAL = "<in this function>"


def _inside(data: bytes, symbol: str, fn: elfobj.FunctionObj) -> dict[str, int]:
    """The names an ELF object gives to places inside one function (the function's own name among them), with
    their offsets from its start."""
    if fn.format != "elf":
        return {}
    from elftools.elf.elffile import ELFFile

    elf = ELFFile(BytesIO(data))
    found = elfobj._function_symbols(elf).get(symbol)
    symtab = elfobj.symbol_table(elf)
    if found is None or symtab is None:
        return {}
    shndx, start, size = found
    return {s.name: s["st_value"] - start for s in symtab.iter_symbols()
            if s.name and s["st_shndx"] == shndx and start <= s["st_value"] < start + size}


def _local_targets(inside: dict[str, int], fn: elfobj.FunctionObj) -> list[tuple]:
    """A function's relocation identities, with references into the function itself resolved to (offset of the
    field, type, LOCAL, offset they point at). ELF MIPS jumps only: other references keep their symbol and addend."""
    if fn.arch != "mips":
        return sorted(r.key() for r in fn.relocs)
    out = []
    for r in fn.relocs:
        if r.type == 4 and r.symbol in inside:  # R_MIPS_26: the field is a word index added to the symbol
            out.append((r.offset, r.type, LOCAL, inside[r.symbol] + (r.addend << 2)))
        else:
            out.append(r.key())
    return sorted(out)


def verify(target: Path | None, base: Path | None, symbol: str, renamed: list | tuple = ()) -> dict:
    """`renamed`: objdiff's (original, yours) pairs of differently named references. Those that only name places
    inside the function are dropped; what is left is kept as evidence["references"]."""
    evidence: dict = {"version": VERSION, "byte_exact": False, "byte_diffs": None,
                      "scope": "function object bytes and relocation identities"}
    try:
        objects, blobs = [], []
        for path in (target, base):
            if path is None:
                evidence["reason"] = "object unavailable"
                return evidence
            data = path.read_bytes()
            extract = coffobj.extract_function if coffobj.is_coff(data) else elfobj.extract_function
            objects.append(extract(data, symbol))
            blobs.append(data)
        a, b = objects
        if a is None or b is None or not a.code or not b.code:
            evidence["reason"] = "function bytes unavailable"
            return evidence
        evidence.update(target_size=len(a.code), base_size=len(b.code),
                        target_sha256=hashlib.sha256(a.code).hexdigest(), base_sha256=hashlib.sha256(b.code).hexdigest())
        # relocated fields hold addends, which the identities below compare; every other byte is compared as it is
        code_a, code_b = a.normalized(), b.normalized()
        evidence["byte_diffs"] = sum(x != y for x, y in zip(code_a, code_b, strict=False)) + abs(len(code_a) - len(code_b))
        same_format = (a.arch, a.big_endian, a.format) == (b.arch, b.big_endian, b.format)
        inside_a, inside_b = _inside(blobs[0], symbol, a), _inside(blobs[1], symbol, b)
        same_refs = _local_targets(inside_a, a) == _local_targets(inside_b, b)
        # a place inside the function, named differently on each side: proven by the identities, or local on both
        evidence["references"] = [list(pair) for pair in renamed
                                  if not (pair[0] in inside_a and (same_refs or pair[1] in inside_b))]
        evidence["relocations_equal"] = same_refs
        evidence["byte_exact"] = same_format and code_a == code_b and same_refs
        evidence["reason"] = ("verified" if evidence["byte_exact"] else "object architecture or format differs" if not same_format
                              else "function bytes differ" if code_a != code_b else "relocation identities differ")
    except Exception as exc:  # noqa: BLE001 a malformed/unsupported object is unverified, never a match
        evidence["reason"] = f"byte verification unavailable: {type(exc).__name__}"
    return evidence


def read_evidence(value: str | None) -> dict:
    try:
        data = json.loads(value or "{}")
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}
