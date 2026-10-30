"""Source isolation and externally visible definitions used by the export planners."""

from __future__ import annotations

from io import BytesIO
from itertools import pairwise

from elftools.elf.elffile import ELFFile

from .. import coffobj, elfobj
from ..forks import csource


def isolated_source(source: str, name: str, functions: list[str]) -> str | None:
    """Keep the selected definition and shared declarations; never guess at overlapping definitions."""
    target = csource.find(source, name)
    if target is None:
        return None
    spans = []
    for other in functions:
        if other == name:
            continue
        span = csource.find(source, other)
        if span is None or (span[0] < target[1] and target[0] < span[1]):
            return None
        spans.append(span)
    spans.sort()
    if any(left[1] > right[0] for left, right in pairwise(spans)):
        return None
    for start, end in reversed(spans):
        source = source[:start] + "\n" + source[end:]
    return source


def definitions(data: bytes, targets: set[str]) -> tuple[set[str], list[str]]:
    """Link-conflicting public symbols and C definition names; locals and coalescible helpers are excluded.

    Selected functions still participate when emitted as weak/COMDAT, so another submission cannot silently
    supply a different implementation of a matched function. Object-format readers own symbol decoding.
    """
    if coffobj.is_coff(data):
        arch, sections, symbols = coffobj._read(data)
        functions = coffobj.list_functions(data)
        public = {s.name for s in symbols if s.name and 0 < s.section <= len(sections)
                  and s.storage == coffobj.IMAGE_SYM_CLASS_EXTERNAL
                  and sections[s.section - 1].comdat_selection not in (2, 5)}  # ANY / ASSOCIATIVE, not NODUPLICATES
        # The matcher accepts x86 cdecl/stdcall decoration; the source parser needs the actual C spelling.
        names = [n.removeprefix("_").split("@", 1)[0] if arch == "x86" else n for n in functions]
        target_keys = {coffobj._key(t) for t in targets}
        public.update(raw for raw in functions if coffobj._key(raw) in target_keys)
        return public, names
    elf = ELFFile(BytesIO(data))
    table = elf.get_section_by_name(".symtab")
    public = {s.name for s in table.iter_symbols() if s.name and isinstance(s["st_shndx"], int)
              and s["st_shndx"] > 0 and s["st_info"]["bind"] == "STB_GLOBAL"} if table is not None else set()
    functions = elfobj.list_functions(data)
    public.update(set(functions) & targets)
    return public, functions
