"""Linking a fork, file by file, and saying how far it got.

objdiff knows which functions of a unit match; a file is done when all of them do, and a link is what turns a tree of
object files into a program to check against the original. Failures are remembered so the page can say why a file is
still open instead of retrying it forever."""

# pyright: reportAttributeAccessIssue=false
# A type checker reads one part at a time, so a self.<name> owned by another part (or by the engine itself) looks
# unknown. ForkEngine is one class at runtime; tests/test_fork_parts.py proves every name resolves there.


from __future__ import annotations

import json
import threading
from collections import defaultdict
from contextlib import suppress
from pathlib import Path

from ..forks import datadiff, dtksplits
from ..forks.project import ForkError, ForkProject
from ..network import Agent, NetworkError
from .forkutil import (
    _tail,
)


class ForkLinkMixin:
    @staticmethod
    def _finished(unit: dict) -> bool:
        m = unit.get("measures") or {}
        tc, td = int(m.get("total_code") or 0), int(m.get("total_data") or 0)
        return bool(tc or td) and int(m.get("matched_code") or 0) == tc and int(m.get("matched_data") or 0) == td

    def link_candidates(self, proj, fp: ForkProject, rep: dict) -> list[str]:
        """Source files not linked yet whose every unit matches in code and data (and loads the original's constants)."""
        states = fp.link_states()
        units: dict[str, list[dict]] = defaultdict(list)
        for u in rep.get("units", []):
            src = (u.get("metadata") or {}).get("source_path")
            if src:
                units[src].append(u)
        return sorted(src for src, us in units.items() if states.get(src, ("",))[0] == "NonMatching"
                      and all(self._finished(u) and not self._held(proj, u["name"]) for u in us))

    def link(self, slug: str, log=print, agent: Agent | None = None) -> dict:
        """Link every finished file into the game, keeping only what the project's checksum check accepts."""
        proj, fp, cfg = self.get(slug)
        if not fp.linkable():
            return {"linked": [], "failed": {}, "note": "this project has no link step decomp.wtf knows (dtk-template)"}
        with self._tree(proj).exclusive(), self._build_locks[proj["id"]]:
            fp.build_all()
            rep = fp.report()
            todo = self.link_candidates(proj, fp, rep)
            if not todo:
                return {"linked": [], "failed": {}}
            conf = fp.root / "configure.py"
            original = conf.read_text(errors="replace", encoding="utf-8")
            ok, out = fp.verify()
            if not ok:
                return {"linked": [], "failed": {}, "error": "the project's checksum check fails before linking "
                        f"anything, so nothing was linked:\n{_tail(out)}"}

            def build_with(sources: list[str]) -> tuple[bool, str]:
                conf.write_text(fp.marked_matching(original, sources, cfg.get("version")), encoding="utf-8")
                return fp.verify()

            log(f"linking {len(todo)} finished file(s)")
            linked: list[str] = []
            failed: dict[str, str] = {}
            ok, out = build_with(todo)
            if ok:
                linked = todo
            else:  # one at a time, so one file that doesn't link keeps none of the others out
                for src in todo[:40]:
                    ok, out = build_with([*linked, src])
                    if ok:
                        linked.append(src)
                    else:
                        failed[src] = _tail(out)
                ok, out = build_with(linked)
                if not ok:
                    linked = []
            self._remember_link_failures(proj, linked, failed)
            if not linked:
                conf.write_text(original, encoding="utf-8")
                fp.verify()
                return {"linked": [], "failed": failed}
            names = ", ".join(Path(s).name for s in linked[:6]) + (f" and {len(linked) - 6} more" if len(linked) > 6 else "")
            who = f"\n\nLinked by decomp.wtf after {agent.name}'s match." if agent else "\n\nLinked by decomp.wtf."
            self._commit(proj, fp, "configure.py", f"Link {names}\n\nEvery function and all data match, and the game "
                                                   f"still builds byte for byte from the C.{who}")
            with self.net.store.tx() as db:
                for src in linked:
                    self.net._event(db, proj["id"], agent.id if agent else None, "link", src)
            self._scores = {k: v for k, v in self._scores.items() if k[0] != proj["id"]}
            self.sync(slug, log=lambda *_: None, rep=fp.report())
        log(f"linked {len(linked)} file(s)" + (f"; {len(failed)} didn't link" if failed else ""))
        return {"linked": linked, "failed": failed}

    def _verify(self, proj, fp: ForkProject) -> tuple[bool, str]:
        with self._build_locks[proj["id"]]:
            return fp.verify()

    def _file_done(self, slug: str, proj, fp: ForkProject, unit, scores: dict, agent: Agent | None) -> str:
        """When every function of a file matches: link it (in the background), or say what its data still needs."""
        if (unit.complete or not unit.source or not fp.linkable() or not scores
                or any(v is None or v < 100 for v in scores.values())):
            return ""
        data = self._data_status(unit)
        self.link_soon(slug, agent)  # the checksum decides; the comparison below is what to fix if it fails
        if not data:
            return (f"\n🔗 Every function and all data in {unit.source} match: decomp.wtf is linking the file into the "
                    "game now (it stays linked if the game still builds byte for byte).")
        return (f"\n🔗 Every function in {unit.source} matches. To link the file into the game (the goal: the game "
                f"built from this C), its data must match too. Fix it with edit_file, then file_status to check:\n{data}")

    def _data_status(self, unit, max_rows: int = 40) -> str:
        """The file's data sections that differ from the original's ("" when all match)."""
        if unit.target is None or not unit.target.exists():
            return ""
        try:
            return datadiff.render(datadiff.compare(unit.target, unit.base), max_rows)
        except Exception as e:  # noqa: BLE001 an object pyelftools can't read: say so rather than fail the tool
            return f"(couldn't compare the data: {e})"

    def file_status(self, slug: str, path: str) -> str:
        """A source file on its way to being linked: its functions, its data next to the original's, and why the last
        link attempt failed."""
        proj, fp, cfg = self.get(slug)
        rel = path.strip().lstrip("./")
        units = fp.units_for(rel)
        if not units:  # a function name: its file
            try:
                fn = self._fn(slug, path)
                units, rel = [fp.unit(fn["unit"])], fp.unit(fn["unit"]).source or ""
            except (NetworkError, ForkError):
                raise NetworkError(f"{path} isn't a source file of a unit (or a function) in this project") from None
        state = fp.link_states().get(rel, ("",))[0]
        lines = [f"## {rel}"]
        lines.append("🔗 LINKED: the game is built from this file's C and checked byte for byte." if state == "Matching"
                     else f"not linked yet ({state or 'not in configure.py'})" if fp.linkable()
                     else "this project has no link step decomp.wtf knows")
        for u in units:
            rows = self.net.store.all("SELECT name, status, n_insns FROM functions WHERE project_id=? AND unit=? "
                                      "ORDER BY vram, name", proj["id"], u.name)
            left = [r for r in rows if r["status"] != "matched"]
            lines.append(f"unit {u.name}: " + ("no functions (data only)" if not rows else
                         f"{len(rows) - len(left)}/{len(rows)} functions match")
                         + (": still to do " + ", ".join(f"{r['name']} ({r['n_insns']} insns)" for r in left[:12])
                            if left else ""))
            if state != "Matching":
                data = self._data_status(u)
                lines.append(f"data: {'matches' if not data else 'differs'}" + (f"\n{data}" if data else ""))
        try:
            mods = self._modules(fp, cfg)
            src = fp.src_dir()
            name = rel[len(src) + 1:] if rel.startswith(src + "/") else rel
            for module, mod in mods.items():
                for f in dtksplits.Splits.parse(mod.splits.read_text(encoding="utf-8")).files:
                    if f.name == name:
                        lines.append(f"{module} sections: " + ", ".join(f"{k} 0x{a:X}-0x{b:X}" for k, (a, b) in f.sections.items())
                                     + " (split_file divides it)")
        except (NetworkError, OSError):
            pass
        why = (cfg.get("link_failed") or {}).get(rel)
        if why and state != "Matching":
            lines += ["", "The last try to link it failed (the game didn't build identically):", why]
        return "\n".join(lines)

    def _remember_link_failures(self, proj, linked: list[str], failed: dict[str, str]) -> None:
        cfg = json.loads(self.net.store.one("SELECT config FROM projects WHERE id=?", proj["id"])["config"])
        known = cfg.get("link_failed") or {}
        for src in linked:
            known.pop(src, None)
        known.update(failed)
        cfg["link_failed"] = known
        self._save_cfg(proj, cfg)

    def link_soon(self, slug: str, agent: Agent | None = None) -> None:
        """Link in the background (one run per project at a time; another request while it runs runs it again)."""
        with self._mlock:
            state = self._linking.setdefault(slug, {"running": False, "again": False})
            if state["running"]:
                state["again"] = True
                return
            state["running"] = True

        def run() -> None:
            while True:
                with suppress(Exception):  # a background tidy-up: the next match tries again
                    self.link(slug, log=lambda *_: None, agent=agent)
                with self._mlock:
                    if not state["again"]:
                        state["running"] = False
                        return
                    state["again"] = False

        threading.Thread(target=run, daemon=True, name=f"link-{slug}").start()

