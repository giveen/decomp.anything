"""The small pieces every part of the fork engine shares.

Text surgery on a fork's sources (the twin helpers take a function's C and put another function's names in it, which is
how a look-alike is tried), the tree lock, and the objdiff call that measures a unit. Nothing here knows about projects
or agents: it is the layer the parts in forkcreate/forklink/forkindex/... are written on.
"""

from __future__ import annotations

import hashlib
import re
import threading
from contextlib import contextmanager

from ..forks import csource, drafts
from ..forks.project import ForkProject
from ..network import NetworkError

REPO = re.compile(r"^(https://(github\.com|gitlab\.com|codeberg\.org|git\.[\w.-]+)/[\w.-]+/[\w.-]+?)(\.git)?/?$")

# A match has to come from C/C++: no inline assembly, asm functions, asm includes or embedded binaries.
CHEAT = re.compile(r"\b(__asm__|__asm|asm|_asm)\b|INCLUDE_ASM|GLOBAL_ASM|INCLUDE_RODATA|\.incbin"
                    r"|#\s*include\s*[\"<][^\">]*\.(s|S|asm|bin|inc\.c)[\">]")

# `register int x asm("r4");` binds a variable to a register: an accepted matching technique (pret's GBA
# projects use it), not assembly code, so it doesn't count as inline asm.
REGVAR = re.compile(r"\b(?:asm|__asm__|__asm)\s*\(\s*\"\$?[a-z]{1,2}\d{0,2}\"\s*\)", re.IGNORECASE)

def _strip_comments(code: str) -> str:
    return REGVAR.sub(" ", re.sub(r"//[^\n]*|/\*.*?\*/", " ", code, flags=re.DOTALL))

def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "fork"

def objdiff_diff(fp: ForkProject, unit, symbol: str, target_only: bool = False):
    from ..forks import objdiff

    if not target_only:
        return fp.diff(unit, symbol)
    return objdiff.diff(unit.target, None, symbol, cwd=fp.root, options=fp.diff_options(unit))

def scaled(fuzzy: float | None, n_insns: int) -> int:
    """objdiff's match percentage as a score on the engine's scale (0 = matched)."""
    if fuzzy is None:
        return n_insns * 100
    return 0 if fuzzy >= 100 else max(1, round((1 - fuzzy / 100) * n_insns * 100))

class TreeLock:
    """Work on different source files runs side by side; a change any file may depend on (a header) runs alone.
    A thread that holds it exclusively may take it again either way."""

    def __init__(self):
        self._cond = threading.Condition()
        self._readers = 0
        self._writer: int | None = None
        self._waiting = 0
        self._mine = threading.local()

    @contextmanager
    def shared(self):
        me = threading.get_ident()
        depth = getattr(self._mine, "depth", 0)
        if self._writer == me or depth:
            self._mine.depth = depth + 1
            try:
                yield
            finally:
                self._mine.depth -= 1
            return
        with self._cond:
            while self._writer is not None or self._waiting:
                self._cond.wait()
            self._readers += 1
        self._mine.depth = 1
        try:
            yield
        finally:
            self._mine.depth = 0
            with self._cond:
                self._readers -= 1
                self._cond.notify_all()

    @contextmanager
    def exclusive(self):
        me = threading.get_ident()
        if self._writer == me:
            yield
            return
        if getattr(self._mine, "depth", 0):
            raise RuntimeError("a shared holder can't take the tree exclusively")
        with self._cond:
            self._waiting += 1
            while self._writer is not None or self._readers:
                self._cond.wait()
            self._waiting -= 1
            self._writer = me
        try:
            yield
        finally:
            with self._cond:
                self._writer = None
                self._cond.notify_all()

def _tail(log: str, lines: int = 14) -> str:
    """The end of a build log, where the reason is."""
    return "\n".join(log.strip().splitlines()[-lines:])

def _floor_pct(pct: float, matched: bool) -> float:
    """For display: 100 only for a match (99.96 or code whose constants differ reads 99.9)."""
    return 100.0 if matched else min(99.9, int(pct * 10) / 10)

def _digest(code: str) -> str:
    """A function's text, whitespace aside."""
    return hashlib.sha1(re.sub(r"\s+", " ", code).strip().encode()).hexdigest()

def _matched_in(rep: dict, broken: set[str] | frozenset[str] = frozenset()) -> set[tuple[str, str]]:
    """(unit, function) pairs a progress report counts as matched; units that don't build are measured from an old
    object, so nothing in them counts."""
    return {(u["name"], f["name"]) for u in rep.get("units", []) if u["name"] not in broken
            for f in u.get("functions", []) if (f.get("fuzzy_match_percent") or 0) >= 100}

INCLUDE_LINE = re.compile(r'^#\s*include\s*"[^"]+"\s*$')

ASM_KEYS = ("asm_twin", "asm_code")  # meta saying a function is hand-written assembly (kept by sync)

ASM_DEF = re.compile(r"\s*(?:(?:static|inline|extern)\s+)*asm\b")

# a source file that includes another source file: Mario Party 6's REL runtime.c files are one line,
# #include "src/Runtime.PPCEABI.H/runtime.c" (its asm helpers, which every module links a copy of)
SOURCE_INCLUDE = re.compile(r'^\s*#\s*include\s*"([^"]+\.(?:c|cc|cp|cpp))"', re.MULTILINE)

PRAGMA = re.compile(r"^#\s*pragma\b")

def _around(source: str, span: tuple[int, int], file_text: str, rel: str) -> tuple[list[str], list[str], list[str]]:
    """What an agent sends around the one definition, as (includes, lines before it, lines after it): project
    includes, prototypes and extern declarations, `#pragma` lines (push/pop wrappers). Lines the file has already
    are left out (a wrapper's pragmas aren't: they belong to this function). Anything else (a struct, a global,
    another function) isn't placed: edit_file is for that, and this says so."""
    have = {ln.strip() for ln in file_text.splitlines()}
    clean = csource._blank(source)

    def lines(lo: int, hi: int) -> list[str]:
        return [real.strip() for real, c in zip(source[lo:hi].splitlines(), clean[lo:hi].splitlines(), strict=False) if c.strip()]

    includes, before, after, other = [], [], [], []
    for ln in lines(0, span[0]):
        if INCLUDE_LINE.match(ln):
            if ln not in have:
                includes.append(ln)
        elif PRAGMA.match(ln):
            before.append(ln)
        elif drafts._DECL.match(ln):
            if ln not in have:
                before.append(ln)
        elif ln not in have:
            other.append(ln)
    for ln in lines(span[1], len(source)):
        if PRAGMA.match(ln):
            after.append(ln)
        elif ln not in have:
            other.append(ln)
    if other:
        listing = "\n".join(other[:6]) + ("\n..." if len(other) > 6 else "")
        raise NetworkError(f"send the function definition, plus any #include \"...\", prototypes, extern declarations "
                           f"or #pragma lines it needs (those are placed for you). These lines aren't any of that and "
                           f"aren't in {rel}:\n{listing}\nPut structs, globals and helper functions in with edit_file first")
    return includes, before, after

def _headers_defining(fp: ForkProject, rel: str, include_lines: list[str], names: set[str]) -> list[str]:
    """Of a file's #include "..." lines, those whose header defines one of `names` (a type, macro or function),
    looked up where the project's builds look: include/, the file's own folder, src/, the project root."""
    out = []
    for line in include_lines:
        m = re.search(r'"([^"]+)"', line)
        if not m:
            continue
        for base in (fp.root / "include", (fp.root / rel).parent, fp.root / "src", fp.root):
            h = base / m.group(1)
            if not h.is_file():
                continue
            try:
                text = csource._blank(h.read_text(errors="replace", encoding="utf-8"))
            except OSError:
                break
            if any(re.search(rf"(?:struct|union|enum)\s+{re.escape(n)}\b|\}}\s*{re.escape(n)}\s*;|#\s*define\s+{re.escape(n)}\b|"
                             rf"\b{re.escape(n)}\s*\(", text) for n in names):
                out.append(line)
            break
    return out

def _type_blocks(text: str) -> list[tuple[set[str], str]]:
    """The file-scope type definitions of a C file: (the names they define, their text)."""
    clean = csource._blank(text)
    out, i = [], 0
    pattern = re.compile(r"^[ \t]*(typedef\b|struct\s+\w+\s*\{|union\s+\w+\s*\{|enum\s*\w*\s*\{)", re.MULTILINE)
    depth_at = []
    depth = 0
    for _, c in enumerate(clean):  # depth before each character
        depth_at.append(depth)
        depth += (c == "{") - (c == "}")
    for m in pattern.finditer(clean):
        if m.start() < i or depth_at[m.start()] != 0:
            continue
        j, d = m.start(), 0
        while j < len(clean):  # to the `;` that ends it at depth 0
            c = clean[j]
            d += (c == "{") - (c == "}")
            if c == ";" and d == 0:
                break
            j += 1
        block = text[m.start():j + 1]
        names = set(re.findall(r"(?:struct|union|enum)\s+(\w+)", block[:block.find("{")] if "{" in block else block))
        tail = block[block.rfind("}") + 1:] if "}" in block else block
        names |= {n for n in re.findall(r"\b([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*[;,)]", tail)
                  if n not in ("typedef", "struct", "union", "enum", "const", "volatile")}
        if names:
            out.append((names, block.strip()))
        i = j + 1
    return out

def _file_scope_lines(text: str) -> list[str]:
    """The lines of a C file outside every function body and struct (brace depth 0), stripped."""
    clean, depth, out = csource._blank(text), 0, []
    for real, c in zip(text.splitlines(), clean.splitlines(), strict=False):
        if depth == 0 and c.strip() and "{" not in c and "}" not in c:
            out.append(real.strip())
        depth = max(0, depth + c.count("{") - c.count("}"))
    return out

def _pragmas_around(text: str, lo: int, hi: int) -> tuple[int, int]:
    """Widen a definition's span over the #pragma lines right above and below it (its wrapper)."""
    start = lo
    while start > 0:
        prev = text.rfind("\n", 0, start - 1) + 1
        line = text[prev:start].strip()
        if not PRAGMA.match(line):
            break
        start = prev
    end = hi
    while end < len(text):
        nl = text.find("\n", end)
        nl = len(text) if nl < 0 else nl
        line = text[end:nl].strip()
        if line and not PRAGMA.match(line):
            break
        if not line and end != hi:
            break
        end = nl + 1 if nl < len(text) else nl
    return start, end

def _with_includes(text: str, includes: list[str], prelude: str = "") -> str:
    """The file with new #include lines after its last one (or at the top), and `prelude` (type definitions)
    after those."""
    if not includes and not prelude:
        return text
    last = None
    for m in re.finditer(r"^[ \t]*#\s*include\b[^\n]*\n", text, re.MULTILINE):
        last = m
    block = "".join(ln + "\n" for ln in includes) + (prelude.rstrip("\n") + "\n\n" if prelude else "")
    return text[:last.end()] + block + text[last.end():] if last else block + text

def _renamed_note(d) -> str:
    """objdiff's rules accept a reference to a differently named symbol; the game's link doesn't."""
    if not d.renamed:
        return ""
    pairs = ", ".join(f"`{mine}` (the original uses `{orig}`)" for orig, mine in d.renamed[:5])
    return (f"\n⚠️ Some references use other names than the original: {pairs}. objdiff counts the match, but the "
            "game links by name: use the original's names (or rename the symbol everywhere) so the code links.")
