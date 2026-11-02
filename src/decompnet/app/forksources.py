"""Source files for code that has no file yet: the scaffold a fork starts with.

A decomp's units come from its objdiff.json; code the fork doesn't have a file for is still code that has to live
somewhere. `start_files` creates those files, `split_file` carves one unit out of another when the boundaries are wrong,
and `uncovered` says what is still homeless. The tree is committed after each change."""

# pyright: reportAttributeAccessIssue=false
# A type checker reads one part at a time, so a self.<name> owned by another part (or by the engine itself) looks
# unknown. ForkEngine is one class at runtime; tests/test_fork_parts.py proves every name resolves there.

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

from .. import elfobj
from ..forks import dtksplits
from ..forks.objdiff import ObjdiffError
from ..forks.project import STATE, ForkError, ForkProject
from ..network import Agent, NetworkError
from .forkutil import (
    _tail,
)


class ForkSourceMixin:
    def _modules(self, fp: ForkProject, cfg: dict) -> dict:
        if fp.kind() not in ("dtk", "manifest") or not fp.linkable():
            raise NetworkError("giving code source files works on dtk-template projects (GameCube, Wii)")
        version = cfg.get("version") or (fp.versions() or [""])[0]
        if not version:  # config/<version>/config.yml is where the modules are listed
            raise NetworkError("this project has no version to read its config from")
        try:
            return dtksplits.modules(fp.root, version)
        except (OSError, ValueError) as e:
            raise NetworkError(f"can't read the project's config for {version}: {e}") from None

    @staticmethod
    def _report_on_disk(fp: ForkProject) -> dict:
        """objdiff's last report for a project, or {} when it hasn't measured one yet."""
        try:
            return json.loads((fp.root / STATE / "report.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _text_extent(unit, syms: dict) -> tuple[int, int] | None:
        """(start, end) in .text of an auto unit's code: its first symbol's address, its object's .text size."""
        from io import BytesIO

        from elftools.elf.elffile import ELFFile

        try:
            elf = ELFFile(BytesIO(unit.target.read_bytes()))
        except (OSError, AttributeError):
            return None
        sec = elf.get_section_by_name(".text")
        tab = elfobj.symbol_table(elf)
        if sec is None or tab is None or not sec["sh_size"]:
            return None
        idx = next(i for i, x in enumerate(elf.iter_sections()) if x.name == ".text")
        first = min(
            ((x["st_value"], x.name) for x in tab.iter_symbols() if x["st_shndx"] == idx and x.name and x["st_info"]["type"] == "STT_FUNC"),
            default=None,
        )
        if first is None or first[1] not in syms or syms[first[1]][0] != ".text":
            return None
        start = syms[first[1]][1] - first[0]
        return start, start + sec["sh_size"]

    def code_without_files(self, slug: str, rep: dict | None = None) -> dict:
        """What start_files would do: {"create": [source paths of listed files that don't exist],
        "split": [(module, source path, start, end, functions)]}."""
        _proj, fp, cfg = self.get(slug)
        mods = self._modules(fp, cfg)
        src = fp.src_dir()
        rep = rep if rep is not None else self._report_on_disk(fp)  # the measure just taken, or the one on disk
        rep_fns = {u["name"]: len(u.get("functions", [])) for u in rep.get("units", [])}
        cpp = {
            u["name"]
            for u in rep.get("units", [])  # code with C++ names goes in a C++ file
            if any((f.get("metadata") or {}).get("demangled_name") not in (None, f["name"]) for f in u.get("functions", []))
        }
        create, split, taken = [], [], set()
        listed = fp.link_states()
        for u in fp.units():
            module, _, rest = u.name.partition("/")
            if u.source or module not in mods:
                continue
            if not u.auto:
                for f in dtksplits.Splits.parse(mods[module].splits.read_text(encoding="utf-8")).files:
                    if str(Path(f.name).with_suffix("")) == rest and f"{src}/{f.name}" in listed:
                        create.append(f"{src}/{f.name}")
                continue
            if not rest.endswith("_text") or not rep_fns.get(u.name, 1):
                continue
            extent = self._text_extent(u, dtksplits.symbols(mods[module].symbols))
            if extent is None:
                continue
            folder = "auto" if module == "main" else f"REL/{module}"
            ext = ".cpp" if u.name in cpp else ".c"
            name = f"{folder}/text_{extent[0]:08X}{ext}"
            while name in taken or (fp.root / src / name).exists() or f"{src}/{name}" in listed:
                name = name[: -len(ext)] + "_" + ext
            taken.add(name)
            split.append((module, name, extent[0], extent[1], rep_fns.get(u.name, 0)))
        return {"create": sorted(set(create)), "split": sorted(split)}

    def start_files(self, slug: str, log=print, agent: Agent | None = None) -> dict:
        """Give all code a source file: create files configure.py lists, split off a file for every stretch of
        code no file covers. Verified by the game's checksum, committed, measured."""
        proj, fp, cfg = self.get(slug)
        mods = self._modules(fp, cfg)
        src = fp.src_dir()
        conf = fp.root / "configure.py"
        with self._tree(proj).exclusive(), self._build_locks[proj["id"]]:
            fp.build_all()
            plan = self.code_without_files(slug, fp.report())
            if not plan["create"] and not plan["split"]:
                return {"created": [], "split": [], "functions": 0}
            saved = {conf: conf.read_text(encoding="utf-8")}
            made: list[Path] = []
            try:
                for path in plan["create"]:
                    p = fp.root / path
                    if not p.exists():
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_text("", encoding="utf-8")
                        made.append(p)
                by_module: dict[str, list] = defaultdict(list)
                for item in plan["split"]:
                    by_module[item[0]].append(item)
                text = saved[conf]
                for module, items in by_module.items():
                    sp_path = mods[module].splits
                    saved.setdefault(sp_path, sp_path.read_text(encoding="utf-8"))
                    sp = dtksplits.Splits.parse(saved[sp_path])
                    for _, name, start, end, _n in items:
                        sp.files.append(dtksplits.FileSplit(name, {".text": (start, end)}))
                        p = fp.root / src / name
                        p.parent.mkdir(parents=True, exist_ok=True)
                        common = '#include "common.h"\n' if (fp.root / "include" / "common.h").is_file() else ""
                        p.write_text(
                            f"// Code of {module} that had no source file yet: decomp.wtf gave it this one.\n"
                            "// split_file divides it into the game's real files (file_status shows its range).\n" + common,
                            encoding="utf-8",
                        )
                        made.append(p)
                    sp_path.write_text(sp.render(), encoding="utf-8")
                    text = dtksplits.add_objects(text, module, [name for _, name, _, _, _ in items])
                conf.write_text(text, encoding="utf-8")
                log(f"giving {len(plan['split'])} stretch(es) of code a source file; the build splits the game again")
                ok, out = fp.verify()
                if not ok:
                    raise ForkError(f"the game didn't build identically with the new files:\n{_tail(out)}")
            except (ForkError, OSError, ValueError) as e:
                for path, old in saved.items():
                    path.write_text(old, encoding="utf-8")
                for p in made:
                    p.unlink(missing_ok=True)
                fp.verify()
                raise NetworkError(f"nothing was changed: {e}") from None
            fp.build_all()
            rep = fp.report()
            paths = [str(p.relative_to(fp.root)) for p in [*saved, *made]]
            n = sum(item[4] for item in plan["split"])
            self._commit_paths(
                proj,
                fp,
                paths,
                f"Give code without a source file one ({len(made)} files, {n} functions)\n\n"
                "Each stretch of code no file covered gets a file of its own, so it can be decompiled; "
                "the game still builds byte for byte.\n\nSplit by decomp.wtf.",
            )
            self._scores = {k: v for k, v in self._scores.items() if k[0] != proj["id"]}
            self.sync(slug, log=lambda *_: None, rep=rep)
            with self.net.store.tx() as db:
                self.net._event(db, proj["id"], agent.id if agent else None, "files", f"{len(made)} new source files for {n} functions")
        log(f"{len(made)} new source files; {n} more functions can be decompiled")
        return {"created": plan["create"], "split": [i[1] for i in plan["split"]], "functions": n}

    def split_file(self, agent: Agent | None, slug: str, path: str, at: str, new_name: str = "", data: str = "") -> str:
        """Divide a source file in two at a function: it and everything after it in the file's code go to a new file
        (with their C), and so do the parts of the data sections named in `data` (".rodata:0x88 .data:0x28": where the
        new file's part of each section starts). Kept only if the game still builds byte for byte and every function
        that matched still matches."""
        if agent is not None and (link := self.net.links.get(slug)) is not None:  # the host splits it; this copy follows
            text = link.call("/collab/split_file", agent, path=path, at=at, new_name=new_name, data=data)
            if text.startswith("✅"):
                link.sync()
            return text
        proj, fp, cfg = self.get(slug)
        mods = self._modules(fp, cfg)
        src = fp.src_dir()
        rel = path.strip().lstrip("./")
        if not fp.units_for(rel):
            try:  # a function: its file
                rel = fp.unit(self._fn(slug, path)["unit"]).source or ""
            except (NetworkError, ForkError):
                raise NetworkError(f"{path} isn't a source file (or a function) of this project") from None
        name = rel[len(src) + 1 :] if rel.startswith(src + "/") else rel
        owners = []
        for module, mod in mods.items():
            sp = dtksplits.Splits.parse(mod.splits.read_text(encoding="utf-8"))
            if any(f.name == name for f in sp.files):
                owners.append((module, mod, sp))
        if len(owners) != 1:
            raise NetworkError(
                f"{rel} is {'not in any' if not owners else 'shared by several'} module's splits; split_file divides a file one module owns"
            )
        module, mod, sp = owners[0]
        if fp.link_states().get(rel, ("",))[0] != "NonMatching":
            raise NetworkError(f"{rel} is linked (or not in configure.py): a linked file is finished, don't split it")
        f = next(x for x in sp.files if x.name == name)
        if ".text" not in f.sections:
            raise NetworkError(f"{rel} has no code to split")
        syms = dtksplits.symbols(mod.symbols)
        lo, hi = f.sections[".text"]
        where = at.strip()
        if where in syms and syms[where][0] == ".text":
            addr = syms[where][1]
        elif re.fullmatch(r"(?:0x)?[0-9A-Fa-f]+", where):
            addr = int(where, 16)
        else:
            try:
                fn_row = self._fn(slug, at)
                addr = syms[self._meta(fn_row).get("symbol")][1]
            except (NetworkError, KeyError):
                raise NetworkError(f"`{at}` isn't a function of {rel}: give its name or .text offset") from None
        starts = sorted(a for sec, a, _ in syms.values() if sec == ".text" and lo < a < hi)
        if addr not in starts:
            raise NetworkError(
                f"`{at}` (0x{addr:X}) isn't the start of a function in {rel} (after its first one); its code is .text 0x{lo:X}-0x{hi:X}"
            )
        ext = Path(name).suffix or ".c"
        new = new_name.strip().lstrip("./") or f"{Path(name).parent.as_posix()}/text_{addr:08X}{ext}"
        new = new[len(src) + 1 :] if new.startswith(src + "/") else new
        if not fp.safe_path(f"{src}/{new}", write=True) or (fp.root / src / new).exists() or any(x.name == new for x in sp.files):
            raise NetworkError(f"{src}/{new} exists already: pick another new_name")
        moved_sections = {".text": (addr, hi)}
        kept = dict(f.sections, **{".text": (lo, addr)})
        for part in data.replace(",", " ").split():
            sec, _, start = part.partition(":")
            if sec not in f.sections or sec == ".text":
                raise NetworkError(f"{rel} has no {sec} part to split (it has {', '.join(f.sections)})")
            a, b = f.sections[sec]
            cut = int(start, 16)
            if not a <= cut <= b:
                raise NetworkError(f"{sec}:{start} is outside {rel}'s {sec} (0x{a:X}-0x{b:X})")
            moved_sections[sec] = (cut, b)
            kept[sec] = (a, cut)
        kept = {k: v for k, v in kept.items() if v[1] > v[0]}

        old_path, new_path = fp.root / src / name, fp.root / src / new
        conf = fp.root / "configure.py"
        unit = fp.units_for(rel)[0]
        rows = self.net.store.all("SELECT id, name, meta FROM functions WHERE project_id=? AND unit=?", proj["id"], unit.name)
        with self._tree(proj).exclusive():  # no other build runs meanwhile
            text = old_path.read_text(errors="replace", encoding="utf-8") if old_path.exists() else ""
            spans = []
            for r in rows:
                sym = self._meta(r).get("symbol")
                if sym in syms and addr <= syms[sym][1] < hi:
                    _, span = self._find(text, self._names(self._meta(r), r))
                    if span:
                        spans.append((syms[sym][1], span))
            first_def = min(
                (sp_[0] for r in rows for sp_ in [self._find(text, self._names(self._meta(r), r))[1]] if sp_), default=len(text)
            )
            preamble = text[:first_def].rstrip() + "\n" if text[:first_def].strip() else ""
            moved_code = [text[a:b].strip("\n") for _, (a, b) in sorted(spans)]
            rest = text
            for _, (a, b) in sorted(spans, key=lambda x: -x[1][0]):
                rest = rest[:a].rstrip("\n") + "\n\n" + rest[b:].lstrip("\n")
            scores_before = self._baseline(proj, fp, unit, text)[0] or {}
            saved = {conf: conf.read_text(encoding="utf-8"), mod.splits: mod.splits.read_text(encoding="utf-8"), old_path: text}
            protected = {n for n, pct in scores_before.items() if pct is not None and pct >= 100}
            try:
                f.sections = kept
                sp.files.append(dtksplits.FileSplit(new, moved_sections))
                mod.splits.write_text(sp.render(), encoding="utf-8")
                ctext = saved[conf]
                states = dtksplits.libs(ctext)
                entry = next(((s0, e0) for lib in states for p_, s0, e0 in lib.objects if p_ == name), None)
                if entry is None:
                    raise ForkError(f"{name} isn't in configure.py")
                old_entry = ctext[entry[0] : entry[1]]
                new_entry = re.sub(
                    r'^(Object\(\s*)\w+(\s*,\s*)"[^"]+"', lambda mm: f'{mm.group(1)}NonMatching{mm.group(2)}"{new}"', old_entry, count=1
                )
                indent = dtksplits._indent_of(ctext, entry[0])
                conf.write_text(ctext[: entry[1]] + f",\n{indent}{new_entry}" + ctext[entry[1] :], encoding="utf-8")
                new_path.parent.mkdir(parents=True, exist_ok=True)
                new_path.write_text(
                    preamble + ("\n" if preamble else "") + "\n\n".join(moved_code) + ("\n" if moved_code else ""), encoding="utf-8"
                )
                old_path.write_text(rest, encoding="utf-8")
                ok, out = fp.verify()
                if not ok:
                    raise ForkError(f"the game didn't build identically with the files divided there:\n{_tail(out)}")
                after_units = [u for u in fp.units() if u.source in (rel, f"{src}/{new}")]
                now: dict[str, float | None] = {}
                for u in after_units:
                    built, log_ = fp.build_unit(u)
                    if not built:
                        raise ForkError(
                            f"{u.source} doesn't compile after the split (move the declarations it needs with "
                            f"edit_file first):\n{_tail(log_)}"
                        )
                    now.update(fp.diff(u, None).unit_scores)
                lost = sorted(n for n in protected if (now.get(n) or 0) < 100)
                if lost:
                    raise ForkError(f"these functions would stop matching: {', '.join(lost[:8])}")
            except (ForkError, ObjdiffError, OSError, ValueError) as e:
                for p_, old in saved.items():
                    p_.write_text(old, encoding="utf-8")
                new_path.unlink(missing_ok=True)
                fp.verify()
                for u in fp.units_for(rel):
                    fp.build_unit(u)
                raise NetworkError(f"not split, nothing changed: {e}") from None
            fp.build_all()
            rep = fp.report()
            self._commit_paths(
                proj,
                fp,
                [str(p_.relative_to(fp.root)) for p_ in saved] + [str(new_path.relative_to(fp.root))],
                f"Split {Path(name).name} at 0x{addr:X}: {Path(new).name}\n\n"
                f"Divided by {agent.name if agent else 'decomp.wtf'} with decomp.wtf; the game still builds "
                "byte for byte.",
            )
            self._scores = {k: v for k, v in self._scores.items() if k[0] != proj["id"]}
            self.sync(slug, log=lambda *_: None, rep=rep)
            with self.net.store.tx() as db:
                self.net._event(db, proj["id"], agent.id if agent else None, "split", f"{rel} → {src}/{new}")
        moved = ", ".join(f"{k} 0x{a:X}-0x{b:X}" for k, (a, b) in moved_sections.items())
        return (
            f"✅ {rel} is divided: {src}/{new} now holds {moved} ({len(moved_code)} function definitions moved with "
            "it); the game still builds byte for byte and every match still matches."
        )

    def _commit_paths(self, proj, fp: ForkProject, paths: list[str], message: str) -> None:
        with self._git_locks[proj["id"]]:
            fp.git("add", "--", *paths)
            fp.git("commit", "-q", "-m", message, "--", *paths)

    @staticmethod
    def _count_uncovered(rep: dict) -> dict:
        """Code no source file covers (dtk's auto_..._text units): agents can't reach it until it has a file."""
        autos = [
            u
            for u in rep.get("units", [])
            if (u.get("metadata") or {}).get("auto_generated") and u["name"].endswith("_text") and u.get("functions")
        ]
        return {"stretches": len(autos), "functions": sum(len(u["functions"]) for u in autos)}

    def uncovered(self, slug: str) -> dict | None:
        """{"stretches", "functions"} without a source file, from the last measurement (a project measured by an
        older decomp.wtf: from its saved report)."""
        _proj, fp, cfg = self.get(slug)
        if "uncovered" in cfg:
            return cfg["uncovered"]
        try:
            counted = self._count_uncovered(json.loads((fp.root / STATE / "report.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None
        self.net._update_cfg(slug, lambda c: c.setdefault("uncovered", counted))
        return counted
