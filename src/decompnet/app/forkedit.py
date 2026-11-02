"""Reading and editing a fork's sources: what an agent does before it attempts a function.

read_file, read_data and search are how an agent looks around a tree it didn't write; edit_file changes one file and
commits it. A shared header is edited from both sides, so `_edit_shared` keeps the change and merges what another
agent added meanwhile."""

# pyright: reportAttributeAccessIssue=false
# A type checker reads one part at a time, so a self.<name> owned by another part (or by the engine itself) looks
# unknown. ForkEngine is one class at runtime; tests/test_fork_parts.py proves every name resolves there.


from __future__ import annotations

import json
import os
import re
import struct
from collections import defaultdict
from pathlib import Path

from .. import elfobj
from ..forks.objdiff import ObjdiffError
from ..forks.project import EDITABLE, STATE, ForkError, ForkProject
from ..network import Agent, NetworkError
from ..textedit import replace_once
from .forkutil import (
    CHEAT,
    _matched_in,
    _strip_comments,
    _tail,
)


class ForkEditMixin:
    def read_file(self, slug: str, path: str, start: int = 1, count: int = 200) -> str:
        _, fp, _ = self.get(slug)
        p = fp.root if path.strip().strip("/") in ("", ".") else fp.safe_path(path)
        if p.is_dir():  # a folder: what's in it (agents look around this way)
            hidden = {".git", "build", STATE, "orig", "__pycache__", "node_modules"}
            items = sorted((x for x in p.iterdir() if x.name not in hidden and not x.name.startswith(".")),
                           key=lambda x: (not x.is_dir(), x.name.lower()))
            listing = [f"  {x.name}/" if x.is_dir() else f"  {x.name}" for x in items[:300]]
            more = f"\n  ... and {len(items) - 300} more" if len(items) > 300 else ""
            return f"{path.strip() or '.'} is a folder ({len(items)} entries):\n" + "\n".join(listing) + more
        if not p.is_file():
            raise NetworkError(f"{path} isn't a file{self._did_you_mean(fp, path)}")
        with open(p, "rb") as f:
            head = f.read(8192)
        if b"\0" in head or p.stat().st_size > 4 * 1024 * 1024:
            raise NetworkError(f"{path} isn't a source or text file")  # never the user's game data
        lines = p.read_text(errors="replace", encoding="utf-8").splitlines()
        start = max(1, int(start))
        chunk = lines[start - 1:start - 1 + max(1, min(int(count), 600))]
        out = [f"{path} (lines {start}-{start + len(chunk) - 1} of {len(lines)})"]
        out += [f"{start + i:5d}  {line}" for i, line in enumerate(chunk)]
        return "\n".join(out)

    def read_data(self, agent: Agent | None, slug: str, what: str, length: int = 64) -> str:
        """A data symbol's bytes in the original game (from the target objects): a string, a table, a constant,
        with the symbols its words point to."""
        from elftools.elf.elffile import ELFFile
        from elftools.elf.relocation import RelocationSection

        proj, fp, _ = self.get(slug)
        name = what.strip().strip("`'\"")
        held = agent and self.net.store.one("SELECT f.unit FROM claims c JOIN functions f ON f.id=c.function_id "
                                            "WHERE c.agent_id=?", agent.id)
        units = [held["unit"]] if held else []
        units += [u for u in self._data_index(proj, fp).get(name, []) if u not in units]
        for uname in units:
            try:
                unit = fp.unit(uname)
            except ForkError:
                continue
            if unit.target is None or not unit.target.exists():
                continue
            with open(unit.target, "rb") as f:
                elf = ELFFile(f)
                symtab = elfobj.symbol_table(elf)
                sym = next((x for x in symtab.iter_symbols() if x.name == name and isinstance(x["st_shndx"], int)
                            and x["st_info"]["type"] != "STT_FUNC"), None) if symtab else None
                if sym is None:
                    continue
                sec = elf.get_section(sym["st_shndx"])
                head = f"{name} ({uname}, {sec.name}+0x{sym['st_value']:X}, {sym['st_size'] or '?'} bytes)"
                if sec["sh_type"] == "SHT_NOBITS":
                    return f"{head}: uninitialized data ({sec.name}), zeros at boot."
                n = max(4, min(sym["st_size"] or 512, int(length or 64), 512))
                data = sec.data()[sym["st_value"]:sym["st_value"] + n]
                if n < (sym["st_size"] or 0):
                    head += f"; the first {n} (length up to 512 shows more)"
                ptrs = []
                for rs in elf.iter_sections():
                    if isinstance(rs, RelocationSection) and rs["sh_info"] == sym["st_shndx"]:
                        table = elf.get_section(rs["sh_link"])
                        for r in rs.iter_relocations():
                            at = r["r_offset"] - sym["st_value"]
                            if 0 <= at < n:
                                t = table.get_symbol(r["r_info_sym"])
                                tname = elf.get_section(t["st_shndx"]).name if t["st_info"]["type"] == "STT_SECTION" else t.name
                                addend = r["r_addend"] if r.is_RELA() else 0
                                ptrs.append((at, tname + (f"+0x{addend:X}" if addend else "")))
                out = [head]
                for i in range(0, len(data), 16):
                    chunk = data[i:i + 16]
                    out.append(f"  +0x{i:03X}: {chunk.hex(' '):<47}  {''.join(chr(c) if 32 <= c < 127 else '.' for c in chunk)}")
                if ptrs:
                    out.append("pointers in it: " + ", ".join(f"+0x{at:X} -> {t}" for at, t in sorted(ptrs)[:24]))
                e = ">" if not elf.little_endian else "<"
                words = [w for (w,) in struct.iter_unpack(e + "I", data[:len(data) // 4 * 4])]
                floats = [struct.unpack(e + "f", struct.pack(e + "I", w))[0] for w in words[:8]]
                if words and any(1e-6 < abs(x) < 1e7 for x in floats):
                    out.append("as f32: " + ", ".join(f"{x:g}" for x in floats))
                text = data.split(b"\0", 1)[0]
                if len(text) >= 3 and all(32 <= c < 127 or c in (9, 10) for c in text):
                    out.append(f"as a string: {json.dumps(text.decode('ascii'))}")
                return "\n".join(out)
        raise NetworkError(f"no data symbol `{name}` in the game's objects (read_file and search_code find it in "
                           "the source; a function is shown by get_function)")

    def _data_index(self, proj, fp: ForkProject) -> dict[str, list[str]]:
        """Data symbol -> units whose target object defines it (built once per objdiff.json)."""
        from elftools.elf.elffile import ELFFile

        key = (proj["id"], (fp.root / "objdiff.json").stat().st_mtime_ns if (fp.root / "objdiff.json").exists() else 0)
        cached = getattr(self, "_data_idx", None)
        if cached and cached[0] == key:
            return cached[1]
        index: dict[str, list[str]] = defaultdict(list)
        for unit in fp.units():
            if unit.target is None or not unit.target.exists():
                continue
            try:
                with open(unit.target, "rb") as f:
                    symtab = elfobj.symbol_table(ELFFile(f))
                    for x in symtab.iter_symbols() if symtab else ():
                        if x.name and x["st_info"]["type"] in ("STT_OBJECT", "STT_NOTYPE") and isinstance(x["st_shndx"], int):
                            index[x.name].append(unit.name)
            except Exception:  # noqa: BLE001
                continue
        self._data_idx = (key, dict(index))
        return self._data_idx[1]

    @staticmethod
    def _did_you_mean(fp: ForkProject, path: str) -> str:
        """Files of the project with the name the agent guessed a path for."""
        name = Path(path).name
        if not name or not EDITABLE.search(name):
            return ""
        root = fp.root.resolve()
        found = []
        for hit in root.rglob(name):
            rel = hit.relative_to(root)
            if hit.is_file() and not {".git", "build", STATE} & set(rel.parts):
                found.append(rel.as_posix())
                if len(found) >= 5:
                    break
        return ("; did you mean " + " or ".join(found)) if found else ""

    def search(self, slug: str, pattern: str, glob: str = "") -> str:
        _, fp, _ = self.get(slug)
        try:
            rx = re.compile(pattern)
        except re.error:  # e.g. "func(": search for the text itself
            rx = re.compile(re.escape(pattern))
        root = fp.root.resolve()
        if glob:
            files = sorted(fp.root.glob(glob))
        else:
            files = sorted(p for ext in ("c", "cc", "cpp", "h", "hpp", "inc") for p in fp.root.rglob(f"*.{ext}"))
        hits = []
        for p in files:
            rp = p.resolve()
            if root not in rp.parents or not rp.is_file():
                continue
            rel = rp.relative_to(root)
            if {".git", "build", STATE} & set(rel.parts):
                continue
            for i, line in enumerate(rp.read_text(errors="replace", encoding="utf-8").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{rel.as_posix()}:{i}: {line.strip()[:160]}")
                    if len(hits) >= 80:
                        return "\n".join([*hits, "... (more matches; narrow the pattern)"])
        return "\n".join(hits) or "no matches"

    def edit_file(self, agent: Agent, slug: str, path: str, old: str, new: str, message: str) -> str:
        if (link := self.net.links.get(slug)) is not None:  # the host makes and checks it; this copy follows
            text = link.call("/collab/edit", agent, path=path, old=old, new=new, message=message)
            if text.startswith(("✅", "⚠️")):
                link.sync()
            return text
        proj, fp, _cfg = self.get(slug)
        p = fp.safe_path(path, write=True)
        if not message.strip():
            raise NetworkError("give a short message explaining the change")
        if CHEAT.search(_strip_comments(new)):
            raise NetworkError("inline assembly, asm functions and asm includes aren't allowed")
        rel = Path(os.path.relpath(p, fp.root)).as_posix()
        units = [u for u in fp.units_for(rel) if u.base is not None]
        if units:  # a unit's own source file: only that unit can change
            return self._edit_source(agent, proj, fp, p, rel, units, old, new, message)
        return self._edit_shared(agent, proj, fp, slug, p, rel, old, new, message)

    @staticmethod
    def _edited(p: Path, path: str, old: str, new: str) -> tuple[bool, str, str]:
        existed = p.exists()
        text = p.read_text(errors="replace", encoding="utf-8") if existed else ""
        if old:
            return existed, text, replace_once(text, old, new, path)
        return existed, text, text.rstrip("\n") + ("\n" if text else "") + new.strip("\n") + "\n"

    def _edit_source(self, agent: Agent, proj, fp: ForkProject, p: Path, rel: str, units: list, old: str, new: str,
                     message: str) -> str:
        """Edit a unit's source file: rebuild and measure just that unit, keep everything that matched before."""
        linked = any(u.complete for u in units) and fp.linkable()
        with self._tree(proj).exclusive() if linked else self._tree(proj).shared(), \
                self._units_locked(proj, [u.name for u in units]):
            existed, text, updated = self._edited(p, rel, old, new)
            bases = {u.name: self._baseline(proj, fp, u, text) for u in units}
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(updated, encoding="utf-8")

            def undo() -> None:
                if existed:
                    p.write_text(text, encoding="utf-8")
                else:
                    p.unlink(missing_ok=True)
                for u in units:
                    if self._build_unit(proj, fp, u)[0] and bases[u.name][0] is not None:
                        self._keep_scores(proj, u, text, bases[u.name][0])

            after: dict[str, dict | None] = {}
            for u in units:
                ok, log = self._build_unit(proj, fp, u)
                if not ok:
                    if bases[u.name][1]:
                        undo()
                        return f"❌ {rel} doesn't compile after your edit, so it was undone:\n{log[-2500:]}"
                    after[u.name] = None  # it didn't build before the edit either
                    continue
                try:
                    after[u.name] = fp.diff(u, None).unit_scores
                except (ObjdiffError, ForkError) as e:
                    undo()
                    return f"❌ objdiff couldn't measure {rel} after your edit, so it was undone: {e}"
            lost = []
            for u in units:
                before = bases[u.name][0] if bases[u.name][0] is not None else self._last_scores(proj, u)
                now = after[u.name] or {}
                lost += [n for n, pct in (before or {}).items() if pct is not None and pct >= 100 and (now.get(n) or 0) < 100]
            if lost:
                undo()
                return f"❌ REJECTED and undone: the edit breaks {len(lost)} matched function(s): {', '.join(sorted(lost)[:10])}"
            if linked and not (ok := self._verify(proj, fp))[0]:
                undo()
                return (f"❌ REJECTED and undone: {rel} is linked into the game, and with your edit the game no longer "
                        f"builds byte for byte:\n{_tail(ok[1])}")
            for u in units:
                if after[u.name] is not None:
                    self._keep_scores(proj, u, updated, after[u.name])
                    self._apply_scores(proj, u.name, after[u.name], agent=agent, text=updated)
            self._commit(proj, fp, rel, f"{message.strip()}\n\nEdited by {agent.name} with decomp.wtf.")
            with self.net.store.tx() as db:
                self.net._event(db, proj["id"], agent.id, "context", f"{rel}: {message.strip()[:120]}")
        if any(v is None for v in after.values()):
            return (f"⚠️ {rel} updated, but it still doesn't compile (it didn't before your edit either): "
                    f"fix it so its functions can be measured.")
        done = "".join(self._file_done(proj["slug"], proj, fp, u, after[u.name] or {}, agent) for u in units[:1])
        return f"✅ {rel} updated; every matched function still matches.{done}"

    def _edit_shared(self, agent: Agent, proj, fp: ForkProject, slug: str, p: Path, rel: str, old: str, new: str,
                     message: str) -> str:
        """Edit a header (or any file several units may use): rebuild and measure the whole project."""
        with self._tree(proj).exclusive():
            existed, text, updated = self._edited(p, rel, old, new)
            failed_before: list[str] = []
            self._build_all(proj, fp, failed=failed_before)  # what already doesn't build, so it isn't blamed on the edit
            game_ok = fp.linkable() and fp.verify()[0]
            broken_before = fp.failed_units(failed_before)
            protected = {(r["unit"], self._meta(r).get("symbol")) for r in self.net.store.all(
                "SELECT unit, meta FROM functions WHERE project_id=? AND status='matched'", proj["id"])}
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(updated, encoding="utf-8")

            def undo() -> None:
                if existed:
                    p.write_text(text, encoding="utf-8")
                else:
                    p.unlink(missing_ok=True)
                self._build_all(proj, fp)

            failed_after: list[str] = []
            _, log = self._build_all(proj, fp, failed=failed_after)
            broken_after = fp.failed_units(failed_after)
            newly = sorted(broken_after - broken_before)
            if not newly and game_ok and not (ok := fp.verify())[0]:
                undo()
                return (f"❌ after your edit the game no longer builds byte for byte from its linked files, so it was "
                        f"undone:\n{_tail(ok[1])}")
            if newly:
                undo()
                return (f"❌ after your edit {len(newly)} file(s) no longer compile ({', '.join(newly[:5])}), so it was "
                        f"undone. Build output:\n{log[-2500:]}")
            try:
                rep = fp.report()
            except (ObjdiffError, ForkError) as e:
                undo()
                return f"❌ the project couldn't be measured after your edit, so it was undone: {e}"
            lost = protected - _matched_in(rep, broken_after)
            if lost:  # was each of them really matched before the edit? (the database can be behind the tree)
                undo()
                try:
                    rep_before = fp.report()
                except (ObjdiffError, ForkError) as e:
                    return f"❌ the project couldn't be measured, so the edit was undone: {e}"
                truly = sorted(sym for unit, sym in lost & _matched_in(rep_before, broken_before))
                self.sync(slug, log=lambda *_: None, rep=rep_before, broken=broken_before)
                if truly:
                    return f"❌ REJECTED and undone: the edit breaks {len(truly)} matched function(s): {', '.join(truly[:10])}"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(updated, encoding="utf-8")
                self._build_all(proj, fp)
            self._scores = {k: v for k, v in self._scores.items() if k[0] != proj["id"]}
            self._commit(proj, fp, rel, f"{message.strip()}\n\nEdited by {agent.name} with decomp.wtf.")
            with self.net.store.tx() as db:
                self.net._event(db, proj["id"], agent.id, "context", f"{rel}: {message.strip()[:120]}")
            self.sync(slug, log=lambda *_: None, rep=rep, broken=broken_after, agent=agent)
        if fp.linkable() and self.link_candidates(proj, fp, rep):
            self.link_soon(slug, agent)
        return f"✅ {rel} updated; every matched function still matches."

