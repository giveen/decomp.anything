"""What an agent does on a fork: claim a function, get a brief, try code, keep what matches.

A fork's judge is its own build plus objdiff, so an attempt compiles, links and measures before it is kept or thrown
away. Claims and holds keep two agents off one function; a room's claims come from the host (see roomops.py)."""

# pyright: reportAttributeAccessIssue=false
# A type checker reads one part at a time, so a self.<name> owned by another part (or by the engine itself) looks
# unknown. ForkEngine is one class at runtime; tests/test_fork_parts.py proves every name resolves there.


from __future__ import annotations

import json
import threading
import time
from contextlib import suppress
from pathlib import Path

from ..forks import catalog, csource, drafts, ghidra, matrixverify
from ..forks.objdiff import ObjdiffError
from ..forks.project import STATE, ForkError, ForkProject
from ..network import Agent, NetworkError
from ..scoring import percent_text
from .forkutil import (
    CHEAT,
    PRAGMA,
    _around,
    _digest,
    _pragmas_around,
    _renamed_note,
    _strip_comments,
    _tail,
    _with_includes,
    scaled,
)


class ForkAgentMixin:
    def info(self, slug: str) -> str:
        proj, fp, cfg = self.get(slug)
        m = cfg.get("measures") or {}
        units = fp.units() if cfg.get("ready") else []
        scratch = next((u.scratch for u in units if u.scratch), None) or {}
        platform = catalog.PLATFORMS.get(cfg.get("platform"), cfg.get("platform") or "unknown platform")
        return (f"{proj['name']}: a fork of {cfg['repo_url']} ({platform}{', ' + cfg['version'] if cfg.get('version') else ''})\n"
                f"progress: {m.get('matched_functions', '?')}/{m.get('total_functions', '?')} functions, "
                f"{float(m.get('matched_code_percent', 0) or 0):.2f}% of code, in {len(units)} source files\n"
                f"compiler: {scratch.get('compiler', 'set by the project build')} {scratch.get('c_flags', '')}\n"
                "You work in the project's real source tree. claim_function, then compile_and_diff writes your function "
                "into its source file, builds that file with the project's build system and diffs it with objdiff. "
                "read_file and search_code show how the codebase is written; edit_file changes headers and file-level "
                "declarations (undone if it breaks any match). Guides: guide()."
                + "".join(f"\nThe project has notes for contributors: read_file(\"{n}\") before you start."
                          for n in ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md") if (fp.root / n).exists())[:400])

    def _fn(self, slug: str, name: str):
        return self.net._function(name, slug)

    @staticmethod
    def _names(meta: dict, fn) -> list[str]:
        """Ways the function may be spelled in source: full C++ name, Class::method, then the plain symbol."""
        out = []
        dem = (meta.get("demangled") or "").split("(", 1)[0].strip()
        if dem:
            dem = dem.split(" ")[-1] if " " in dem and "operator" not in dem else dem
            out.append(dem)
            parts = dem.split("::")
            if len(parts) > 2:
                out.append("::".join(parts[-2:]))
            if len(parts) > 1:
                out.append(parts[-1])
        out.append(meta.get("symbol") or fn["name"].split("@")[0])
        return list(dict.fromkeys(out))

    def _find(self, text: str, names: list[str]):
        for n in names:
            span = csource.find(text, n)
            if span:
                return n, span
        return None, None

    def _held(self, proj, unit: str, text: str | None = None) -> set[str]:
        """Functions of a unit whose source is still the very text judged to load the wrong constants: objdiff's
        progress rules count them, decomp.wtf doesn't (see forks/literals.py)."""
        rows = self.net.store.all("SELECT name, meta FROM functions WHERE project_id=? AND unit=? AND meta LIKE ?",
                                  proj["id"], unit, '%"held"%')
        if not rows:
            return set()
        if text is None:
            try:
                u = self._fork_project(proj).unit(unit)
                text = (Path(proj["root"]) / u.source).read_text(errors="replace", encoding="utf-8") if u.source else ""
            except (ForkError, OSError):
                return set()
        out = set()
        for r in rows:
            meta = self._meta(r)
            _, span = self._find(text, self._names(meta, r))
            if span and _digest(text[span[0]:span[1]]) == meta.get("held"):
                out.add(meta.get("symbol"))
        return out

    def _hold(self, fn, code: str | None) -> None:
        """Remember (code) or forget (None) that this text of the function loads constants the original doesn't."""
        meta = self._meta(fn)
        if code is None and "held" not in meta:
            return
        if code is None:
            meta.pop("held", None)
        else:
            meta["held"] = _digest(code)
        with self.net.store.tx() as db:
            db.execute("UPDATE functions SET meta=? WHERE id=?", (json.dumps(meta), fn["id"]))

    def _apply_scores(self, proj, unit: str, scores: dict[str, float | None], skip: int | None = None,
                      agent: Agent | None = None, text: str | None = None) -> None:
        """Bring every function of a unit up to date with a fresh diff (cheaper than a full report). A function
        that matches now is a match like any other, whichever change made it: `agent` (whose edit it was) is
        credited, and `text` (the unit's source) gives the matching code."""
        rows = self.net.store.all("SELECT id, name, status, n_insns, best_score, best_source, meta FROM functions "
                                  "WHERE project_id=? AND unit=?", proj["id"], unit)
        held = self._held(proj, unit, text)
        with self.net.store.tx() as db:
            for r in rows:
                sym = self._meta(r).get("symbol")
                if r["id"] == skip or sym not in scores or r["status"] == "nosource":
                    continue
                pct = scores[sym]
                if pct is not None and pct >= 100 and sym not in held:
                    if r["status"] != "matched":
                        self._matched(db, proj, r, agent, text)
                elif r["status"] == "matched":
                    db.execute("UPDATE functions SET status='open' WHERE id=?", (r["id"],))
                elif pct is not None and not r["best_source"]:
                    db.execute("UPDATE functions SET best_score=? WHERE id=?", (scaled(pct, r["n_insns"]), r["id"]))

    def _matched(self, db, proj, r, agent: Agent | None, text: str | None) -> None:
        """Record a function that matches now (outside compile_and_diff, which records its own)."""
        source = None
        if text:
            _, span = self._find(text, self._names(self._meta(r), r))
            source = text[span[0]:span[1]] if span else None
        db.execute("UPDATE functions SET status='matched', best_score=0, matched_at=?, matched_agent_id=?, "
                   "matched_source=COALESCE(?, matched_source, best_source) WHERE id=?",
                   (time.time(), agent.id if agent else None, source, r["id"]))
        db.execute("DELETE FROM claims WHERE function_id=?", (r["id"],))
        self.net._event(db, proj["id"], agent.id if agent else None, "match", r["name"])
        self.net.on_match(proj, r["id"], agent)

    def brief(self, fn, agent: Agent | None, header: str) -> str:
        proj = self.net.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        _, fp, cfg = self.get(proj["slug"])
        meta = self._meta(fn)
        unit = fp.unit(fn["unit"])
        lines = [f"## {header}: {fn['name']}  ({proj['name']})", f"unit {unit.name}, {fn['size']} bytes"]
        if meta.get("demangled"):
            lines.append(f"C++ name: {meta['demangled']}")
        if unit.scratch:
            lines.append(f"compiler: {unit.scratch.get('compiler', '?')} {unit.scratch.get('c_flags', '')}")
        src_text = None
        if unit.source:
            lines.append(f"source file: {unit.source}")
            p = fp.root / unit.source
            src_text = p.read_text(errors="replace", encoding="utf-8") if p.exists() else None
        else:
            lines.append("this unit has no source file yet, so it can't be decompiled from here")
        try:
            d = fp.diff(unit, meta.get("symbol") or fn["name"])
            pct = ("not in your build yet" if d.match_percent is None else
                   "every instruction matches, but loaded constants differ" if d.literals else
                   f"{percent_text(d.match_percent)} match")
            asm = d.target_asm().splitlines()
            if len(asm) > 500:
                asm = [*asm[:500], f"... ({len(asm) - 500} more instructions)"]
            lines += [f"current state: {pct}", "", "### Target assembly", "```asm", *asm, "```"]
            if d.literals:
                lines += ["", d.literals_note()]
            elif d.match_percent is not None and not d.matched:
                lines += ["", "### Diff against the current build", d.render()]
        except (ObjdiffError, ForkError) as e:
            lines.append(f"(objdiff couldn't diff this function: {e})")
        span = self._find(src_text, self._names(meta, fn))[1] if src_text else None
        current = src_text[span[0]:span[1]] if src_text and span else None
        if current is not None:
            lines += ["", "### Current source", "```c", current.strip(), "```"]
        else:
            lines += ["", f"### Not in {unit.source or 'the source'} yet: compile_and_diff adds it at the end."]
        if fn["best_source"] and fn["status"] != "matched":
            lines += ["", f"### Best attempt so far (score {fn['best_score']})", "```c", fn["best_source"].strip(), "```"]
        elif fn["status"] != "matched" and (current is None or csource.is_stub(current)):
            seed = self._draft(proj, fp, cfg, fn, unit)
            if seed:
                lines += ["", "### Machine draft: a starting point, often wrong in types and details", "```c",
                          seed.strip(), "```"]
        twin = self._twin_brief(proj, fn) if fn["status"] != "matched" else []
        lines += twin
        try:
            alike = [] if twin else [(r, sim) for r, sim in self.similar(proj, fp, fn, top=3) if r["status"] == "matched"]
        except (ObjdiffError, ForkError):
            alike = []
        if alike:
            r, sim = alike[0]
            src = self._current_source(fp, r)
            lines += ["", f"### Looks like {r['name']} ({sim * 100:.0f}% similar, already matched)"]
            if src and len(src) < 3000:
                lines += ["```c", src.strip(), "```"]
            if len(alike) > 1:
                lines.append("also similar and matched: " + ", ".join(f"{x['name']} ({s * 100:.0f}%)" for x, s in alike[1:]))
        done = self.net.store.all("SELECT name FROM functions WHERE project_id=? AND unit=? AND status='matched' "
                                  "AND id != ? ORDER BY n_insns DESC LIMIT 12", fn["project_id"], fn["unit"], fn["id"])
        if done:
            lines += ["", "Already matched in this file (read them for the codebase's style): "
                      + ", ".join(r["name"] for r in done)]
        why = (cfg.get("link_failed") or {}).get(unit.source or "")
        if why and not unit.complete:
            lines += ["", f"### {unit.source} didn't link yet (file_status shows its data)", why]
        notes = self.net.store.all("SELECT n.text, a.name FROM notes n LEFT JOIN agents a ON a.id=n.agent_id "
                                   "WHERE n.function_id=? ORDER BY n.id DESC LIMIT 6", fn["id"])
        if notes:
            lines += ["", "### Notes"] + [f"- ({n['name']}) {n['text']}" for n in notes]
        lines += self.matrix_brief(fn, fp)
        lines += ["", "Send compile_and_diff the complete function definition (same name and signature as the original), "
                      "with any #include \"...\", prototypes or #pragma lines it needs. Structs, globals and helper "
                      "functions go in with edit_file."]
        return "\n".join(lines)

    def _twin_brief(self, proj, fn) -> list[str]:
        """For a brief: the matched function with the same code (relocations aside), and its code made into this
        one's when the names can be swapped."""
        try:
            twin_id = self.net.twins.twin_of(proj, fn["id"])
        except Exception:  # noqa: BLE001
            return []
        if twin_id is None:
            return []
        a = self.net.store.one("SELECT * FROM functions WHERE id=?", twin_id)
        try:
            source = self.net.twins.source_for(proj, a, fn, includes=True)
        except (NetworkError, ForkError, OSError, ValueError):
            source = None
        out = ["", f"### Identical code: {a['name']} ({a['unit']}) is the same machine code and already matches"]
        if source and len(source) < 6000:
            why = self.net.twins.miss_why(fn["id"])
            out += ["Its code with this function's names: start from it. "
                    + (f"decomp.wtf tried it as is: {why}. Fix that (this file lacks something the other one has: a "
                       "type, a declaration, an include, or declares a callee differently)." if why else
                       "If it doesn't build or match as is, this file lacks something that one has (a type, a "
                       "declaration, an include)."), "```c", source.strip(), "```"]
        elif self.is_asm(proj, a):
            out.append("It's hand-written assembly there, so this one is too: release it with a note saying so.")
        else:
            src = self._current_source(self._fork_project(proj), a)
            if src and len(src) < 4000:
                out += ["Its source (its names differ from this one's: use the ones in this function's assembly):",
                        "```c", src.strip(), "```"]
        return out

    def _draft(self, proj, fp: ForkProject, cfg: dict, fn, unit) -> str | None:
        """m2c draft from the project's assembly for this function, cached in the database."""
        if fn["seed"] is not None:
            return fn["seed"] or None
        hint = (unit.scratch or {}).get("compiler", "") or json.dumps(fp.needs(cfg.get("version")))
        meta = self._meta(fn)
        try:
            text = drafts.draft(fp.root, cfg.get("platform") or "", unit.name, meta.get("symbol") or fn["name"],
                                unit.source, compiler_hint=hint)
            text = text and "/* m2c */\n" + text
            if not text and unit.target is not None and unit.target.exists() and ghidra.available():
                found = ghidra.decompile_object(unit.target, fp.root / STATE / "ghidra")
                names = [meta.get("symbol") or fn["name"], *self._names(meta, fn)]
                body = next((found[n] for n in names if n in found), None)
                text = body and "/* Ghidra */\n" + body.strip()
        except OSError:
            text = None
        with self.net.store.tx() as db:
            db.execute("UPDATE functions SET seed=? WHERE id=?", (text or "", fn["id"]))
        return text

    def claim(self, agent: Agent, slug: str, function: str | None = None, strategy: str = "easiest") -> str:
        if (link := self.net.links.get(slug)) is not None:
            return self._claim_in_room(link, agent, slug, function, strategy)
        fn = self.net.claim_row(agent, slug, function, strategy)
        if fn is None:
            left = (self.get(slug)[2].get("uncovered") or {}).get("functions")
            return ("Nothing left to claim: every function with a source file matches or someone is on it. 🎉"
                    + (f"\n{left} more functions are in code no source file covers yet. The user can give them files "
                       "(the project's overview in decomp.wtf: Give them source files); then there's more to claim."
                       if left else ""))
        return self.brief(fn, agent, "CLAIMED")

    def _claim_in_room(self, link, agent: Agent, slug: str, function: str | None, strategy: str) -> str:
        """A member of a room: the host hands out functions (nobody does one twice); the brief comes from this copy."""
        name = link.claim(agent, self._fn(slug, function)["name"] if function else None, strategy)
        if name is None:
            return "Nothing left to claim in this room: every function with a source file matches or someone is on it. 🎉"
        try:
            fn = self.net.claim_row(agent, slug, name, strategy)
        except NetworkError:  # the host has a newer tree (new files, a split): follow it, then claim here
            link.sync()
            fn = self.net.claim_row(agent, slug, name, strategy)
        return self.brief(fn, agent, "CLAIMED")

    def get_function(self, agent: Agent | None, slug: str, function: str) -> str:
        return self.brief(self._fn(slug, function), agent, "VIEW")

    def list_functions(self, slug: str, status: str = "open", limit: int = 40) -> str:
        proj, _, _ = self.get(slug)
        where = {"open": "status='open'", "matched": "status='matched'", "all": "1",
                 "stuck": "stuck_at IS NOT NULL AND status='open'"}.get(status)
        if where is None:
            raise NetworkError("status must be open, matched, all or stuck")
        rows = self.net.store.all(f"SELECT name, unit, n_insns, status, best_score FROM functions WHERE project_id=? "
                                  f"AND {where} ORDER BY n_insns LIMIT ?", proj["id"], max(1, min(int(limit), 200)))
        out = [f"{r['name']:<40} {r['unit']:<36} {r['n_insns']:>5} insns  "
               f"{'matched' if r['status'] == 'matched' else 'best ' + str(r['best_score']) if r['best_score'] is not None else 'new'}"
               for r in rows]
        return "\n".join(out) or "none"

    def attempt(self, agent: Agent, slug: str, function: str, source: str, keep: bool = True, prelude: str = "",
                quiet: int | None = None, require_bytes: bool = False) -> dict:
        """`keep`: whether a version that doesn't match stays in the source file (an agent's work in progress) or
        is undone and kept only as the function's best attempt (autopilot's drafts). `prelude`: (decomp.wtf's own
        tries) type definitions the code needs, placed after the file's includes. `quiet`: (the permuter's
        variants) recorded only when better than this score. A try that isn't kept holds the function only while
        it builds (twins' tries come unclaimed: a 30-minute claim each kept agents off them, then counted as given up)."""
        held = None if keep else self._hold_for_try(slug, function, agent)
        try:
            res = self._attempt(agent, slug, function, source, keep, prelude, quiet, require_bytes)
        finally:
            if held is not None:
                with self.net.store.tx() as db:
                    db.execute("DELETE FROM claims WHERE function_id=? AND agent_id=?", (held, agent.id))
        link = self.net.links.get(slug)
        if link is None or res["score"] is None:
            return res
        name = self._fn(slug, function)["name"]
        if res["matched"]:  # the host builds it in its own tree, against its own copy, and commits it for everyone
            try:
                verdict = link.attempt(agent, name, source)
            except NetworkError as e:
                res["text"] += f"\n⚠️ Not shared with the room yet: {e}"
                return res
            if verdict.get("matched"):
                res["text"] += "\nShared with the room: the host confirmed the match."
                threading.Thread(target=self._follow, args=(link,), daemon=True).start()
            else:
                res["text"] += ("\n⚠️ The room's host could not confirm this match in its tree (score "
                                f"{verdict.get('score')}): it may have changed since; this copy follows the host's.")
        else:
            fn = self._fn(slug, function)
            if fn["best_score"] is None or res["score"] <= fn["best_score"]:
                threading.Thread(target=self._report_best, args=(link, agent, name, source, res["score"]), daemon=True).start()
        return res

    def _hold_for_try(self, slug: str, function: str, agent: Agent) -> int | None:
        """Claim an open function nobody holds for one quick try; its id, or None (held already, or matched)."""
        fn = self._fn(slug, function)
        now = time.time()
        with self.net.store.tx() as db:
            self.net._expire_claims(db)
            if fn["status"] != "open" or db.execute("SELECT 1 FROM claims WHERE function_id=?", (fn["id"],)).fetchone():
                return None
            db.execute("INSERT INTO claims VALUES(?,?,?,?)", (fn["id"], agent.id, now, now + 300))
        return fn["id"]

    @staticmethod
    def _follow(link) -> None:
        with suppress(NetworkError):
            link.sync()

    @staticmethod
    def _report_best(link, agent: Agent, name: str, source: str, score: int) -> None:
        with suppress(NetworkError):
            link.call("/collab/best", agent, function=name, source=source, score=score)

    def _placed(self, proj, fp: ForkProject, unit, fn, names: list[str], before: str, code: str, includes: list[str],
                before_lines: list[str], after_lines: list[str], prelude: str) -> str:
        """The source file's text (`before`) with the function's new definition in place of the old one or of its
        stub, or where it was in the original when the file doesn't have it yet."""
        _hit, old_span = self._find(before, names)
        pre, post = "".join(ln + "\n" for ln in before_lines), "".join(ln + "\n" for ln in after_lines)
        if old_span:
            lo, hi = old_span
            if any(PRAGMA.match(ln) for ln in before_lines + after_lines):  # a new wrapper replaces the old one
                lo, hi = _pragmas_around(before, lo, hi)
            after = before[:lo] + pre + code.rstrip("\n") + "\n" + post + before[hi:]
        else:
            after = self._insert_in_order(proj, fp, unit, fn, before, pre + code.rstrip("\n") + "\n" + post)
        return _with_includes(after, includes, prelude)

    def _attempt(self, agent: Agent, slug: str, function: str, source: str, keep: bool = True, prelude: str = "",
                 quiet: int | None = None, require_bytes: bool = False) -> dict:
        proj, fp, _cfg = self.get(slug)
        fn = self._fn(slug, function)
        meta = self._meta(fn)
        if len(source) > 64 * 1024:
            raise NetworkError("source too large")
        if CHEAT.search(_strip_comments(source)):
            raise NetworkError("inline assembly, asm functions and asm includes aren't allowed: a match has to come from C/C++")
        names = self._names(meta, fn)
        _, span = self._find(source, names)
        if span is None:
            raise NetworkError(f"send the complete definition of {names[0]} (with its body)")
        code = source[span[0]:span[1]]
        unit = fp.unit(fn["unit"])
        if not unit.source:
            raise NetworkError(f"unit {unit.name} has no source file, so there's nowhere to write it")
        try:
            have = (fp.root / unit.source).read_text(errors="replace", encoding="utf-8")
        except OSError:
            have = ""
        includes, before_lines, after_lines = _around(source, span, have, unit.source)
        with self.net.store.tx() as db:  # claim bookkeeping, as for ROM projects
            self.net._expire_claims(db)
            claim = db.execute("SELECT agent_id FROM claims WHERE function_id=?", (fn["id"],)).fetchone()
            if claim is not None and claim["agent_id"] != agent.id:
                raise NetworkError(f"{fn['name']} is claimed by {self.net._agent_name(claim['agent_id'])}")
            now = time.time()
            if claim is None and fn["status"] != "matched":
                db.execute("INSERT INTO claims VALUES(?,?,?,?)", (fn["id"], agent.id, now, now + self.net.claim_ttl))
            elif claim is not None:
                db.execute("UPDATE claims SET expires_at=? WHERE function_id=?", (now + self.net.claim_ttl, fn["id"]))
        path = fp.safe_path(unit.source, write=True)
        symbol = meta.get("symbol") or fn["name"]
        linked = unit.complete and fp.linkable()  # the game is built from this file: changes must keep it identical
        with self._tree(proj).exclusive() if linked else self._tree(proj).shared(), self._unit_lock(proj, unit.name):
            before = path.read_text(errors="replace", encoding="utf-8") if path.exists() else ""
            # What matches in the file as it is now is what this change must keep (not the database's idea of it,
            # which a change outside decomp.wtf or a different measurement can leave behind)
            scores, builds = self._baseline(proj, fp, unit, before)
            held = self._held(proj, unit.name, before)
            if scores is not None:
                self._apply_scores(proj, unit.name, scores, text=before, skip=fn["id"] if require_bytes else None)
                protected = {n for n, pct in scores.items()
                             if n != symbol and n not in held and pct is not None and pct >= 100}
                was_matched = (scores.get(symbol) or 0) >= 100 and symbol not in held
            else:
                protected = {n for n, pct in (self._last_scores(proj, unit) or {}).items()
                             if n != symbol and n not in held and pct is not None and pct >= 100}
                was_matched = fn["status"] == "matched"
            after = self._placed(proj, fp, unit, fn, names, before, code, includes, before_lines, after_lines, prelude)
            path.write_text(after, encoding="utf-8")

            def undo() -> None:
                path.write_text(before, encoding="utf-8")
                if self._build_unit(proj, fp, unit)[0] and scores is not None:
                    self._keep_scores(proj, unit, before, scores)

            ok, log = self._build_unit(proj, fp, unit)
            if not ok:
                undo()
                if quiet is None:
                    self.net._record(fn, agent, None, code, 0)
                why = "" if builds else ("\n(The file didn't build before your change either, so the error may be in "
                                         "other code of the file: fix that with edit_file.)")
                return {"text": f"❌ BUILD ERROR (your change was undone){why}\n{log[-3000:]}", "score": None,
                        "matched": False}
            try:
                d = fp.diff(unit, symbol)
            except (ObjdiffError, ForkError) as e:
                undo()
                return {"text": f"❌ objdiff couldn't compare (your change was undone): {e}", "score": None, "matched": False}
            if require_bytes and d.matched:
                evidence = matrixverify.verify(unit.target, unit.base, symbol, d.renamed)
                if evidence["byte_exact"] is not True or evidence.get("references"):
                    undo()
                    return {"text": "Byte verification failed in the normal build; your change was undone: "
                                    + evidence["reason"], "score": 1, "matched": False}
            broken = sorted(n for n in protected if n and (d.unit_scores.get(n) or 0) < 100)
            if broken:
                undo()
                if quiet is None:
                    self.net._record(fn, agent, None, code, 0, compiled=True)
                return {"text": f"❌ REJECTED and undone: your change breaks matched function(s) {', '.join(broken[:8])}. "
                                "Check you replaced the right definition (overloads?) and didn't change shared code.",
                        "score": None, "matched": False}
            if linked and d.matched and not (ok := self._verify(proj, fp))[0]:
                undo()
                if quiet is None:
                    self.net._record(fn, agent, None, code, 0, compiled=True)
                return {"text": f"❌ REJECTED and undone: {unit.source} is linked into the game, and with your change "
                                f"the game no longer builds byte for byte:\n{_tail(ok[1])}", "score": None,
                        "matched": False}
            pct = d.match_percent if d.match_percent is not None else 0.0
            score = max(1, len(d.literals)) if d.literals else scaled(pct, fn["n_insns"])
            matched = d.matched
            if not matched and not keep and not was_matched:  # tried, not kept: the file stays as it was
                undo()
                if quiet is None or score < quiet:
                    self.net._record(fn, agent, score, code, 0, compiled=True)
                return {"text": f"{percent_text(pct)} match (score {score}); not kept in {unit.source}", "score": score,
                        "matched": False, "percent": pct}
            self._keep_scores(proj, unit, after, d.unit_scores)
            self._hold(fn, code if d.literals else None)
            if was_matched:  # a rewrite of a matched function must keep matching
                if not matched:
                    undo()
                    why = "loads other constants" if d.literals else f"is {percent_text(pct)}"
                    return {"text": f"❌ {fn['name']} already matches; your version {why}, so the matching one was "
                                    "kept. Call claim_function for a function that still needs work.",
                            "score": score, "matched": False}
                self._commit(proj, fp, unit.source, f"Tidy {meta.get('demangled') or symbol} (still matches)\n\n"
                                                    f"Edited by {agent.name} with decomp.wtf.")
                return {"text": f"✅ {fn['name']} still matches; your tidier version is committed.{_renamed_note(d)}",
                        "score": 0, "matched": True}
            self.net._record(fn, agent, score, code, 0, matched=matched)
            self._apply_scores(proj, fn["unit"], d.unit_scores, skip=fn["id"], agent=agent, text=after)
            self._remember(proj, fp, unit, d)
            if matched:
                self._commit(proj, fp, unit.source, f"Match {meta.get('demangled') or symbol}\n\n"
                                                    f"Decompiled by {agent.name} with decomp.wtf.")
                return {"text": f"✅ MATCH! {fn['name']} is 100% identical to the original. Committed to git in the "
                                f"project. Call claim_function for the next one.{_renamed_note(d)}"
                                f"{self._file_done(slug, proj, fp, unit, d.unit_scores, agent)}", "score": 0,
                        "matched": True}
        stall = self.net.stall_note(fn["id"], agent.id)
        if d.literals:
            return {"text": f"score {score} (0 = identical). {d.literals_note()}{stall}", "score": score, "matched": False}
        # observed patterns and, when warranted, a controlled compiler/context experiment (forkmatrix.py)
        more = self.matrix_after_miss(agent, slug, fn["name"], source, d.rows, pct) if keep and quiet is None else ""
        return {"text": f"{percent_text(pct)} match (score {score}; 0 = identical).\n{d.render()}{more}{stall}",
                "score": score, "matched": False}

