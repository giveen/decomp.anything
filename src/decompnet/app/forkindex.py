"""The look-alike index: which functions of a fork have the same machine code.

Identical code is free to match: when one function matches, its twin can be tried with the names swapped, and the judge
decides. The index keeps each unit's instruction shapes (by mtime, so it survives a restart) and the text helpers here
give an agent the twin's declaration and types to go with it."""

# pyright: reportAttributeAccessIssue=false
# A type checker reads one part at a time, so a self.<name> owned by another part (or by the engine itself) looks
# unknown. ForkEngine is one class at runtime; tests/test_fork_parts.py proves every name resolves there.


from __future__ import annotations

import json
import re
import threading
from contextlib import suppress

from .. import similarity
from ..forks import csource, drafts
from ..forks.objdiff import ObjdiffError
from ..forks.project import STATE, ForkError, ForkProject
from ..network import NetworkError
from .forkutil import (
    ASM_DEF,
    INCLUDE_LINE,
    SOURCE_INCLUDE,
    _file_scope_lines,
    _headers_defining,
    _pragmas_around,
    _type_blocks,
    objdiff_diff,
)


class ForkIndexMixin:
    def _index_of(self, proj, fp: ForkProject) -> dict:
        with self._ilock:
            if proj["id"] not in self._index:
                p = fp.root / STATE / "index.json"
                try:
                    self._index[proj["id"]] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
                except (ValueError, OSError):
                    self._index[proj["id"]] = {}
            return self._index[proj["id"]]

    def _save_index(self, proj, fp: ForkProject) -> None:
        with self._ilock:
            snapshot = dict(self._index.get(proj["id"], {}))
        with suppress(OSError):
            (fp.root / STATE / "index.json").write_text(json.dumps(snapshot), encoding="utf-8")

    def _remember(self, proj, fp: ForkProject, unit, d) -> None:
        if unit.target is None or not unit.target.exists():
            return
        entry = {"mtime": unit.target.stat().st_mtime, "order": d.order, "ops": d.ops}
        with self._ilock:
            idx = self._index_of(proj, fp)
            idx[unit.name] = entry

    def _unit_index(self, proj, fp: ForkProject, unit, save: bool = True) -> dict | None:
        """Target order and mnemonics of a unit's functions, from its target object only (never races a build)."""
        idx = self._index_of(proj, fp)
        if unit.target is None or not unit.target.exists():
            return None
        hit = idx.get(unit.name)
        if hit and hit.get("mtime") == unit.target.stat().st_mtime:
            return hit
        row = self.net.store.one("SELECT meta FROM functions WHERE project_id=? AND unit=? LIMIT 1", proj["id"], unit.name)
        if row is None:
            return None
        try:
            d = objdiff_diff(fp, unit, self._meta(row).get("symbol"), target_only=True)
        except (ObjdiffError, ForkError):
            return None
        self._remember(proj, fp, unit, d)
        if save:
            self._save_index(proj, fp)
        return idx.get(unit.name)

    def start_indexing(self, slug: str) -> None:
        """Index every unit in the background so find_similar covers the whole project."""
        proj, fp, cfg = self.get(slug)
        with self._ilock:
            if proj["id"] in self._indexing or not cfg.get("ready"):
                return
            self._indexing.add(proj["id"])

        def run():
            try:
                for n, unit in enumerate(fp.units()):
                    if self.net.store.one("SELECT 1 FROM projects WHERE id=?", proj["id"]) is None:
                        return
                    self._unit_index(proj, fp, unit, save=False)
                    if n % 50 == 49:
                        self._save_index(proj, fp)
                self._save_index(proj, fp)
            finally:
                with self._ilock:
                    self._indexing.discard(proj["id"])
        threading.Thread(target=run, daemon=True, name=f"index-{slug}").start()

    def similar(self, proj, fp: ForkProject, fn, top: int = 5) -> list[tuple[dict, float]]:
        """Functions that look like `fn`, best first: [(row, similarity)]."""
        meta = self._meta(fn)
        unit = fp.unit(fn["unit"])
        own = self._unit_index(proj, fp, unit)
        if not own or meta.get("symbol") not in own["ops"]:
            return []
        target = own["ops"][meta["symbol"]]
        cands = {}
        with self._ilock:
            entries = list(self._index_of(proj, fp).items())
        for uname, entry in entries:
            for sym, ops in entry["ops"].items():
                if (uname, sym) != (fn["unit"], meta.get("symbol")):
                    cands[f"{uname}\0{sym}"] = ops
        out = []
        rows = {(r["unit"], self._meta(r).get("symbol")): r for r in self.net.store.all(
            "SELECT id, name, unit, status, best_score, meta, n_insns FROM functions WHERE project_id=?", proj["id"])}
        for key, sim in similarity.rank(target, cands, top=top * 2):
            uname, sym = key.split("\0", 1)
            if (uname, sym) in rows:
                out.append((rows[(uname, sym)], sim))
        out.sort(key=lambda x: (x[0]["status"] != "matched", -x[1]))
        return out[:top]

    def find_similar(self, slug: str, function: str, limit: int = 5) -> str:
        proj, fp, _cfg = self.get(slug)
        fn = self._fn(slug, function)
        hits = self.similar(proj, fp, fn, top=max(1, min(int(limit), 15)))
        covered = len(self._index_of(proj, fp))
        total = len(fp.units())
        note = f"(index covers {covered} of {total} source files{'; still indexing' if proj['id'] in self._indexing else ''})"
        if not hits:
            return f"No look-alikes found for {fn['name']} {note}."
        lines = [f"Functions that look like {fn['name']} {note}:"]
        for r, sim in hits:
            state = "matched" if r["status"] == "matched" else f"best {r['best_score']}" if r["best_score"] is not None else "not started"
            lines.append(f"- {r['name']}  {sim * 100:.0f}% similar  {state}  ({r['unit']}, {r['n_insns']} insns)")
        best = next((r for r, _ in hits if r["status"] == "matched"), None)
        if best is not None:
            src = self._current_source(fp, best)
            if src:
                lines += ["", f"Source of {best['name']} (matched):", "```c", src.strip()[:4000], "```"]
        return "\n".join(lines)

    def _current_source(self, fp: ForkProject, fn) -> str | None:
        """fn's definition as its file has it now, also when the file gets it by including another source file."""
        unit = fp.unit(fn["unit"])
        if not unit.source or not (fp.root / unit.source).exists():
            return None
        names = self._names(self._meta(fn), fn)
        todo, seen = [fp.root / unit.source], set()
        while todo and len(seen) < 6:
            path = todo.pop(0)
            if path in seen:
                continue
            seen.add(path)
            try:
                text = path.read_text(errors="replace", encoding="utf-8")
            except OSError:
                continue
            _, span = self._find(text, names)
            if span:
                return text[span[0]:span[1]]
            for inc in SOURCE_INCLUDE.findall(text):
                todo += [p for p in (fp.root / inc, fp.root / "src" / inc, path.parent / inc) if p.is_file()][:1]
        return None

    def twin_source(self, proj, a, b, names: dict[str, str], includes: bool = False) -> str | None:
        """a's matched definition, from its source file, written for its twin b: names swapped (see app/twins.py),
        with the includes and declarations it relies on from a's file that b's file doesn't have (compile_and_diff
        places those in b's file). None for a definition in assembly (then b is assembly too)."""
        from . import twins

        fp = self._fork_project(proj)
        ua, ub = fp.unit(a["unit"]), fp.unit(b["unit"])
        if not ua.source or not ub.source:
            return None
        try:
            text_a = (fp.root / ua.source).read_text(errors="replace", encoding="utf-8")
            text_b = (fp.root / ub.source).read_text(errors="replace", encoding="utf-8") if (fp.root / ub.source).exists() else ""
        except OSError:
            return None
        _, span = self._find(text_a, self._names(self._meta(a), a))
        if span is None or ASM_DEF.match(text_a[span[0]:span[1]]):
            return None
        code = twins.apply(text_a[span[0]:span[1]], names)
        used = set(re.findall(r"\b\w+\b", code))
        lines_b = {ln.strip() for ln in text_b.splitlines()}
        clean_b = csource._blank(text_b)
        declared_b = {m.group(1) for ln in text_b.splitlines() if drafts._DECL.match(ln.strip())
                      for m in [re.search(r"(\w+)\s*(?:\(|\[|;)", ln)] if m}
        head, seen = [], set()
        if includes:  # only the headers that define a name b's file doesn't know (more can clash with b's own)
            wanted = {n for n in used if not re.search(rf"\b{re.escape(n)}\b", clean_b)}
            head += _headers_defining(fp, ua.source, [ln for ln in _file_scope_lines(text_a)
                                                      if INCLUDE_LINE.match(ln) and ln not in lines_b], wanted)
        for line in _file_scope_lines(text_a[:span[0]] + "\n" + text_a[span[1]:]):
            if INCLUDE_LINE.match(line):
                continue
            if not drafts._DECL.match(line) or line.startswith("#"):
                continue
            renamed = twins.apply(line, names)
            name = re.search(r"(\w+)\s*(?:\(|\[|;)", renamed)
            if name and name.group(1) in used and name.group(1) not in declared_b and name.group(1) not in seen \
                    and renamed not in lines_b and not re.search(rf"\b{re.escape(name.group(1))}\b", clean_b):
                seen.add(name.group(1))
                head.append(renamed)
        if self._find(code, self._names(self._meta(b), b))[1] is None:
            return None  # a's definition isn't written under its symbol (a macro, another spelling): can't rename it
        return "\n".join(head) + ("\n" if head else "") + code

    def c_linkage(self, proj, a, b, source: str) -> str | None:
        """a's code made into b's, given C linkage (`extern "C"` on its definition and prototypes) when it goes
        from a C file into a C++ one: there its names would be mangled (fn_1_BB34 becomes fn_1_BB34__Fis), so
        the build has no fn_1_BB34 at all (0%). None when that isn't the case."""
        fp = self._fork_project(proj)
        src_a, src_b = fp.unit(a["unit"]).source or "", fp.unit(b["unit"]).source or ""
        cpp = (".cpp", ".cp", ".cc", ".cxx")
        sym = self._meta(b).get("symbol") or b["name"]
        if not src_b.endswith(cpp) or src_a.endswith(cpp) or "__" in sym.strip("_"):
            return None
        _, span = self._find(source, self._names(self._meta(b), b))
        if span is None or re.match(r"\s*(?:static|inline)\b", source[span[0]:span[1]]):
            return None
        head = []
        for line in source[:span[0]].splitlines():
            if drafts._DECL.match(line.strip()) and "(" in line and 'extern "C"' not in line:
                line = 'extern "C" ' + re.sub(r"^\s*extern\s+", "", line.strip())
            head.append(line)
        return "".join(line + "\n" for line in head) + 'extern "C" ' + source[span[0]:]

    def twin_declaration(self, proj, a, b, missing: str, names: dict[str, str]) -> str | None:
        """A declaration of `missing`, a name the code made from a's for b uses where b's file doesn't declare it yet
        (the compiler's "undefined identifier"): b's file's own (its prototype, or the head of its definition, both
        may come further down), else a's file's for the same name, renamed. Data only as extern: b's file keeps the
        data it defines."""
        from . import twins

        fp = self._fork_project(proj)
        ua, ub = fp.unit(a["unit"]), fp.unit(b["unit"])
        try:
            text_a = (fp.root / ua.source).read_text(errors="replace", encoding="utf-8")
            text_b = (fp.root / ub.source).read_text(errors="replace", encoding="utf-8") if (fp.root / ub.source).exists() else ""
        except (OSError, TypeError):
            return None
        a_name = {v: k for k, v in names.items()}.get(missing, missing)

        def declared(text: str, name: str) -> str | None:
            for line in _file_scope_lines(text):
                if line.startswith("#") or not re.search(rf"(?<![\w:]){re.escape(name)}\s*[(\[;=]", line):
                    continue
                if "(" in line.split("=", 1)[0] and line.endswith(";"):
                    return line  # a prototype
                if drafts._DECL.match(line) or "=" in line:  # data: refer to it, never define it again
                    left = re.sub(r"\bstatic\s+", "", line.split("=", 1)[0].rstrip(" ;"))
                    return left + ";" if left.startswith("extern") else f"extern {left};"
            span = csource.find(text, name)
            if span is not None:
                head = text[span[0]:span[1]].split("{", 1)[0].strip()
                if "(" in head:
                    return head + ";"
            return None

        if (own := declared(text_b, missing)) is not None:
            return own
        theirs = declared(text_a, a_name)
        if theirs is None:
            return None
        return re.sub(r"^(?:static\s+|inline\s+)+", "", twins.apply(theirs, names))  # b's file doesn't define it

    def twin_types(self, proj, a, b, source: str) -> str:
        """The file-scope type definitions (struct, union, enum, typedef) of a's file that `source` (a's code made
        into b's) relies on and b's file doesn't have, in a's order: for code that uses a type a's file defines."""
        fp = self._fork_project(proj)
        ua, ub = fp.unit(a["unit"]), fp.unit(b["unit"])
        try:
            text_a = (fp.root / ua.source).read_text(errors="replace", encoding="utf-8")
            text_b = (fp.root / ub.source).read_text(errors="replace", encoding="utf-8") if (fp.root / ub.source).exists() else ""
        except (OSError, TypeError):
            return ""
        blocks = _type_blocks(text_a)
        clean_b = csource._blank(text_b)
        want, out = set(re.findall(r"\b\w+\b", source)), []
        for _ in range(4):  # types used by the types taken
            added = False
            for names, block in blocks:
                if block in out or not names & want or any(re.search(rf"\b{re.escape(n)}\b", clean_b) for n in names):
                    continue
                out.append(block)
                want |= set(re.findall(r"\b\w+\b", block))
                added = True
            if not added:
                break
        return "\n".join(b for _, b in blocks if b in out)

    def one_definition(self, slug: str, source: str, name: str, others: list[str]) -> str:
        """A source holding several functions' definitions, with the definitions of `others` (and the #pragma lines
        right around them) taken out: what compile_and_diff for `name` alone gets."""
        for other in others:
            try:
                fn = self._fn(slug, other)
            except NetworkError:
                continue
            _, span = self._find(source, self._names(self._meta(fn), fn))
            if span:
                lo, hi = _pragmas_around(source, *span)
                source = source[:lo] + source[hi:]
        return source

    def is_asm(self, proj, fn) -> bool:
        """Whether a matched function's definition in its file is hand-written assembly (an asm function)."""
        src = self._current_source(self._fork_project(proj), fn)
        return bool(src and ASM_DEF.match(src))

    def mark_asm(self, fn, twin_name: str | None = None, reason: str | None = None) -> None:
        """fn's code is a matched assembly function's (runtime helpers each module links its own copy of), or code
        no compiler makes (`reason`: see handwritten.py): agents can't write it in C, so it's out of their way."""
        meta = self._meta(fn)
        if twin_name:
            meta["asm_twin"] = twin_name
        if reason:
            meta["asm_code"] = reason
        with self.net.store.tx() as db:
            db.execute("UPDATE functions SET status='asm', meta=? WHERE id=? AND status='open'", (json.dumps(meta), fn["id"]))

