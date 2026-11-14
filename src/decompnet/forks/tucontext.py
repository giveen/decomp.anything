"""One function under different translation units, made from its source file's text.

What a compiler emits for a function depends on more than the function: which other functions the file holds, and
how the file declares the globals it uses (a global the file defines is small data and read through $gp; an `extern`
one is read by its full address). Projects find the original's file boundaries by trying such variants. This makes
them from the text alone, so they can be compiled side by side without touching the project's sources or build:

- file       the source file as it is, with the function in it (what compile_and_diff builds)
- alone      a dedicated translation unit: every other function is only a prototype
- neighbors  the function with the one before and the one after it; the rest are prototypes
- extern     alone, and the globals the file defines on one line are declared `extern` instead
- tentative  alone, and the file's one-line `extern` globals are defined (`extern` dropped) instead

`inline` functions always stay: they are part of the code of whoever calls them. Everything else at file scope
(includes, types, macros, prototypes) is kept as it is, in the same order.
"""

from __future__ import annotations

import re

from . import csource

CONTEXTS = {
    "file": "the source file as it is",
    "alone": "a dedicated translation unit: the function alone, the others as prototypes",
    "neighbors": "the function with the one before and after it in the file",
    "extern": "alone, with the file's own globals declared extern",
    "tentative": "alone, with the file's extern globals defined in the file",
}

_NAME_BEFORE_PAREN = re.compile(r"((?:[\w~]+::)*[\w~]+)\s*$")
_KEYWORDS = {"if", "for", "while", "switch", "return", "sizeof", "do", "else"}
_INLINE = re.compile(r"\b(inline|__inline__|__inline)\b")
# one-line file-scope data: `extern T name[..];` and `T name[..] = value;` (no parentheses: not a prototype or a
# function pointer, which keep their declaration either way)
_EXTERN_DATA = re.compile(r"^(?P<indent>[ \t]*)extern[ \t]+(?P<rest>[^;()=\n]+;[ \t]*(?://[^\n]*|/\*.*?\*/[ \t]*)?)$")
_DATA = re.compile(r"^(?P<indent>[ \t]*)(?!extern\b|typedef\b|static\b|return\b|goto\b|case\b|#)"
                   r"(?P<decl>(?:(?:const|volatile|unsigned|signed|struct|union|enum)[ \t]+)*[A-Za-z_]\w*[ \t*]+"
                   r"[A-Za-z_]\w*[ \t]*(?:\[[^\]\n]*\][ \t]*)*)(?:=[^;{}\n]*)?;[ \t]*(?://[^\n]*|/\*.*?\*/[ \t]*)?$")


def definitions(text: str) -> list[tuple[str, int, int]]:
    """The file-scope function definitions of a C file, in order: (name, start, end)."""
    clean = csource._blank(text)
    out: list[tuple[str, int, int]] = []
    depth, i, end_of_last = 0, 0, 0
    while i < len(clean):
        c = clean[i]
        if c == "{":
            if depth == 0 and i >= end_of_last:
                j = i - 1
                while j >= 0 and clean[j].isspace():
                    j -= 1
                if j >= 0 and clean[j] == ")":  # a body: walk back over the parameter list to the name
                    k, d = j, 0
                    while k >= 0:
                        d += (clean[k] == ")") - (clean[k] == "(")
                        if d == 0:
                            break
                        k -= 1
                    m = _NAME_BEFORE_PAREN.search(clean[:k]) if k > 0 else None
                    if m and m.group(1) not in _KEYWORDS:
                        span = csource.find(text, m.group(1))
                        if span and span[0] <= i < span[1] and not csource.is_stub(text[span[0]:span[1]]):
                            out.append((m.group(1), span[0], span[1]))
                            end_of_last = span[1]
                            i = span[1]
                            continue
            depth += 1
        elif c == "}":
            depth = max(0, depth - 1)
        i += 1
    return out


def prototype(definition: str) -> str:
    """A definition's declaration: everything up to its body, ended with a semicolon."""
    clean = csource._blank(definition)
    head = definition[:clean.index("{")] if "{" in clean else definition
    return " ".join(head.split()).rstrip() + ";\n"


def _keep_only(text: str, keep: set[str]) -> str:
    """The file with every function not in `keep` (and not inline) turned into its prototype."""
    out, at = [], 0
    for name, start, end in definitions(text):
        body = text[start:end]
        head = body[:csource._blank(body).index("{")]
        if name in keep or _INLINE.search(csource._blank(head)):
            continue
        out.append(text[at:start])
        out.append(prototype(body))
        at = end
    out.append(text[at:])
    return "".join(out)


def _file_scope_lines(text: str) -> list[tuple[int, int]]:
    """(start, end) of each line that is wholly at file scope (brace depth 0 before and after it)."""
    clean, depth, at, out = csource._blank(text), 0, 0, []
    for line in clean.splitlines(keepends=True):
        opens, closes = line.count("{"), line.count("}")
        if depth == 0 and not opens and not closes:
            out.append((at, at + len(line.rstrip("\n"))))
        depth = max(0, depth + opens - closes)
        at += len(line)
    return out


def _redeclare(text: str, to_extern: bool) -> str:
    """The file's one-line globals as extern declarations (to_extern), or its extern ones as definitions."""
    pieces, at = [], 0
    for start, end in _file_scope_lines(text):
        line = text[start:end]
        if to_extern:
            m = _DATA.match(line)
            new = f"{m.group('indent')}extern {m.group('decl').rstrip()};" if m else None
        else:
            m = _EXTERN_DATA.match(line)
            new = f"{m.group('indent')}{m.group('rest')}" if m else None
        if new is not None and new != line:
            pieces.append(text[at:start])
            pieces.append(new)
            at = end
    pieces.append(text[at:])
    return "".join(pieces)


def contexts(text: str, name: str, wanted: list[str] | None = None) -> dict[str, str]:
    """The source file's text under each translation-unit context, for the function `name` (which is in `text`).
    A context whose text would be the same as an earlier one's is left out: it would compile to the same object."""
    names = [n for n in (wanted or list(CONTEXTS)) if n in CONTEXTS]
    defs = definitions(text)
    order = [n for n, _, _ in defs]
    simple = name.split("::")[-1]
    me = next((n for n in order if n == name or n.split("::")[-1] == simple), None)
    out: dict[str, str] = {}
    seen: set[str] = set()

    def add(key: str, value: str) -> None:
        if key in names and value not in seen:
            seen.add(value)
            out[key] = value

    add("file", text)
    if me is None:
        return out
    alone = _keep_only(text, {me})
    add("alone", alone)
    k = order.index(me)
    add("neighbors", _keep_only(text, set(order[max(0, k - 1):k + 2])))
    add("extern", _redeclare(alone, to_extern=True))
    add("tentative", _redeclare(alone, to_extern=False))
    return out
