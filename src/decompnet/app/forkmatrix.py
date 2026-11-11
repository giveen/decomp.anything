"""The compiler matrix: one function's C under every compiler profile and translation-unit context a project has.

A project that was built with more than one compiler (PSY-Q games mix GCC 2.7.2 and 2.8.1, with and without the
library profile) has functions whose C is right and whose file is compiled the wrong way, or sits in the wrong
translation unit. The project's build picks one compiler per file and agents can't change it, so such a function
reads as a near miss forever and gets rewritten. Here the same C is compiled under each profile the project
declares and each context tucontext.py makes, every object is measured against the original, and the result is
kept per function: what matched, what came closest, and what kind of difference is left (compilerdiff.py).

The project says how to compile, in its `.decomp.wtf.json`:

    "matrix": {
      "build": ["./run.sh", "python3", "tools/matrix_build.py", "{job}"],
      "profiles": [{"name": "2.7.2-psx", "compiler": "GCC 2.7.2 PSX", "flags": "-O2 -G8"}, "2.8.1-psx"],
      "contexts": ["file", "alone", "neighbors", "extern", "tentative"],   (optional: all of them)
      "auto": true                                                         (optional: after a compiler-like miss)
    }

`build` is run once per matrix, in the project folder, with `{job}` replaced by the path of a JSON file:
{"function", "symbol", "unit", "source_path", "result", "builds": [{"id", "profile", "context", "source", "object"}]}.
It compiles each build's `source` with its `profile` into `object` (paths relative to the project) and may write
`result`: {"current": the profile `source_path` is built with, "builds": {id: {"ok", "log"}}}. Sources and objects
live in the project's .decomp-wtf folder: nothing in its tree or its build is touched.

A match under the file's own profile and context is an ordinary match: it goes through `attempt` and is committed.
A match anywhere else can't be written into the tree by an agent (it needs the file's compiler changed, or the
function moved to its own file): it is saved with its recipe (profile, context, source) on the function, in its
notes and in the experiment history, for whoever maintains the build.
"""

# pyright: reportAttributeAccessIssue=false
# A type checker reads one part at a time, so a self.<name> owned by another part (or by the engine itself) looks
# unknown. ForkEngine is one class at runtime; tests/test_fork_parts.py proves every name resolves there.

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..forks import compilerdiff, csource, objdiff, tucontext
from ..forks.objdiff import ObjdiffError
from ..forks.project import STATE, ForkError, ForkProject
from ..network import Agent, NetworkError
from ..scoring import percent_text
from .forkutil import CHEAT, _around, _strip_comments, _tail, scaled

log = logging.getLogger("decomp.wtf")
NEAR_PERCENT = 80.0  # a miss at least this close gets the matrix on its own, when its differences are compiler-like
def _errors(text: str, lines: int = 14) -> str:
    """A compiler's output with its errors first: old GCCs warn about every implicit declaration, and the one line
    that says why nothing was built is somewhere among them."""
    rows = text.strip().splitlines()
    real = [r for r in rows if "warning" not in r and "previous" not in r]
    return "\n".join((real or rows)[-lines:])[-1500:]


def _is_mips(obj: Path | None) -> bool:
    """Whether an object is MIPS code: the kinds of difference compilerdiff reads are MIPS's."""
    try:
        with open(obj, "rb") as f:  # type: ignore[arg-type]  # None raises TypeError, caught below
            head = f.read(20)
    except (OSError, TypeError):
        return False
    return len(head) == 20 and head[:4] == b"\x7fELF" and int.from_bytes(
        head[18:20], "little" if head[5] == 1 else "big") == 8


HOW = ("this project declares no compiler matrix. Its maintainer adds a \"matrix\" section to .decomp.wtf.json "
       "(a build command and the compiler profiles): guide(\"matrix\") has the format")


class ForkMatrixMixin:
    # ------------------------------------------------------------------ what the project declares

    def matrix_config(self, fp: ForkProject) -> dict | None:
        """The project's matrix: {"build": [...], "profiles": [{"name", "compiler", "flags"}], "contexts": [...],
        "auto": bool, "timeout": seconds, "digest"}; None when it declares none (or a broken one)."""
        try:
            raw = (fp.manifest or {}).get("matrix")
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or not isinstance(raw.get("build"), list) or not raw["build"]:
            return None
        profiles = []
        for p in raw.get("profiles") or []:
            if isinstance(p, str) and p.strip():
                profiles.append({"name": p.strip(), "compiler": "", "flags": ""})
            elif isinstance(p, dict) and str(p.get("name") or "").strip():
                profiles.append({"name": str(p["name"]).strip(), "compiler": str(p.get("compiler") or ""),
                                 "flags": str(p.get("flags") or "")})
        if not profiles or not any("{job}" in str(a) for a in raw["build"]):
            return None
        wanted = [c for c in raw.get("contexts") or list(tucontext.CONTEXTS) if c in tucontext.CONTEXTS]
        cfg = {"build": [str(a) for a in raw["build"]], "profiles": profiles[:32],
               "contexts": wanted or list(tucontext.CONTEXTS), "auto": bool(raw.get("auto", True)),
               "timeout": max(30, min(int(raw.get("timeout") or 900), 3600))}
        cfg["digest"] = hashlib.sha1(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:12]
        return cfg

    def _matrix_lock(self, proj) -> threading.Lock:
        with self._mlock:
            return self._matrix_locks[proj["id"]]

    # ------------------------------------------------------------------ one function through the matrix

    def matrix(self, agent: Agent, slug: str, function: str, source: str = "", profiles: str = "", contexts: str = "",
               again: bool = False, commit: bool = True, terse: bool = False) -> dict:
        """Compile `source` (default: the function as it is in its file, else its best attempt) under the
        project's profiles and contexts. {"text", "matched", "committed", "cells", "repeat"}. `commit`: whether a
        match is kept (committed, or saved with its recipe). `terse`: the answer follows a miss's own diff."""
        proj, fp, _cfg = self.get(slug)
        mcfg = self.matrix_config(fp)
        if mcfg is None:
            raise NetworkError(HOW)
        fn = self._fn(slug, function)
        meta = self._meta(fn)
        unit = fp.unit(fn["unit"])
        if not unit.source or unit.target is None or not unit.target.exists():
            raise NetworkError(f"unit {unit.name} has no source file or no original object to compare with")
        names = self._names(meta, fn)
        symbol = meta.get("symbol") or fn["name"]
        with self._unit_lock(proj, unit.name):
            try:
                have = (fp.root / unit.source).read_text(errors="replace", encoding="utf-8")
            except OSError:
                have = ""
        if not source.strip():
            _, span = self._find(have, names)
            current = have[span[0]:span[1]] if span else ""
            source = current if current and not csource.is_stub(current) else (fn["best_source"] or "")
            if not source.strip():
                raise NetworkError(f"{fn['name']} has no C yet (in {unit.source} or as a best attempt): send its "
                                   "definition as `source`")
        if len(source) > 64 * 1024:
            raise NetworkError("source too large")
        if CHEAT.search(_strip_comments(source)):
            raise NetworkError("inline assembly, asm functions and asm includes aren't allowed: a match has to come from C/C++")
        _, span = self._find(source, names)
        if span is None or csource.is_stub(source[span[0]:span[1]]):
            raise NetworkError(f"send the complete definition of {names[0]} (with its body)")
        code = source[span[0]:span[1]]
        includes, before_lines, after_lines = _around(source, span, have, unit.source)
        text = self._placed(proj, fp, unit, fn, names, have, code, includes, before_lines, after_lines, "")

        want_profiles = [p.strip() for p in profiles.split(",") if p.strip()]
        plist = [p for p in mcfg["profiles"] if not want_profiles or p["name"] in want_profiles]
        if not plist:
            raise NetworkError("no such profile; this project has: " + ", ".join(p["name"] for p in mcfg["profiles"]))
        want_contexts = [c.strip() for c in contexts.split(",") if c.strip()] or mcfg["contexts"]
        unknown = [c for c in want_contexts if c not in tucontext.CONTEXTS]
        if unknown:
            raise NetworkError(f"no such context {unknown[0]!r}; there are: " + ", ".join(tucontext.CONTEXTS))
        texts = tucontext.contexts(text, names[0], want_contexts)
        source_hash = hashlib.sha256(code.encode()).hexdigest()
        input_hash = hashlib.sha256(json.dumps([mcfg["digest"], [p["name"] for p in plist], sorted(texts.items())])
                                    .encode()).hexdigest()
        if not again:
            done = self.net.store.all("SELECT * FROM experiments WHERE function_id=? AND input_hash=? ORDER BY id",
                                      fn["id"], input_hash)
            if done:
                cells = [self._cell_of(r) for r in done]
                said = ("(These exact sources were already tried under this matrix; nothing was compiled. "
                        "again=true compiles anyway.)\n" + self._matrix_text(fn, mcfg, cells, source_hash, None))
                hits = [c for c in cells if c["matched"]]
                kept = False
                if hits and commit and fn["status"] != "matched" and not isinstance(meta.get("matrix"), dict):
                    # matched before and not kept (the tree was reset since, or it was only measured): keep it now
                    was = next((c["profile"] for c in cells if c["current"]), None)
                    kept, more = self._matrix_keep(agent, slug, proj, fn, source, hits, was)
                    said += more
                return {"text": said, "matched": bool(hits), "committed": kept, "cells": cells, "repeat": True}

        run = uuid.uuid4().hex[:12]
        started = time.time()
        with self._matrix_lock(proj):
            cells, current = self._matrix_build(fp, mcfg, plist, texts, fn, unit, symbol, run)
        took = time.time() - started
        best = self._best_cell(cells, current)
        with self.net.store.tx() as db:
            for c in cells:
                db.execute(
                    "INSERT INTO experiments(project_id, function_id, agent_id, run, config, input_hash, source_hash, "
                    "source, profile, compiler, flags, context, current, compiled, score, percent, diffs, matched, "
                    "classes, error, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (proj["id"], fn["id"], agent.id, run, mcfg["digest"], input_hash, source_hash,
                     source if c is best else None, c["profile"], c["compiler"], c["flags"], c["context"],
                     int(c["profile"] == current), int(c["compiled"]), c["score"], c["percent"], c["diffs"],
                     int(c["matched"]), c["classes"].to_json() if c["classes"] else None, c["error"], started))
        out = [self._matrix_text(fn, mcfg, cells, source_hash, current, took)]
        hits = [c for c in cells if c["matched"]]
        committed = False
        if hits and commit and fn["status"] != "matched":
            committed, said = self._matrix_keep(agent, slug, proj, fn, source, hits, current)
            out.append(said)
        elif best is not None and not best["matched"] and best.get("rows"):
            # after a miss the agent has just read its file's own diff: show a diff only when another cell is closer
            if not terse or best["context"] != "file" or best["profile"] != current:
                out.append(f"\nClosest ({best['profile']} / {best['context']}):\n" + objdiff.FunctionDiff(
                    symbol, best["percent"], best["rows"], 0, {}, [], {}).render(max_rows=36))
            if best["classes"] and best["classes"].total:
                out.append(best["classes"].note("done"))
        for c in cells:
            c.pop("rows", None)
        return {"text": "\n".join(out), "matched": bool(hits), "committed": committed, "cells": cells, "repeat": False}

    def _matrix_build(self, fp: ForkProject, mcfg: dict, plist: list[dict], texts: dict[str, str], fn, unit,
                      symbol: str, run: str) -> tuple[list[dict], str | None]:
        """Write the sources, run the project's matrix build, measure every object. (cells, the file's profile)."""
        work = fp.root / STATE / "matrix" / run
        work.mkdir(parents=True, exist_ok=True)
        rel = lambda p: Path(p).relative_to(fp.root).as_posix()  # noqa: E731
        suffix = Path(unit.source).suffix or ".c"
        try:
            builds, cells = [], []
            for ctx, body in texts.items():
                src = work / f"{ctx}{suffix}"
                src.write_text(body, encoding="utf-8", newline="\n")
                for p in plist:
                    k = len(builds)
                    builds.append({"id": f"b{k}", "profile": p["name"], "context": ctx, "source": rel(src),
                                   "object": rel(work / f"b{k}.o")})
                    cells.append({"profile": p["name"], "compiler": p["compiler"], "flags": p["flags"], "context": ctx,
                                  "compiled": False, "score": None, "percent": None, "diffs": None, "matched": False,
                                  "classes": None, "error": None})
            job = work / "job.json"
            job.write_text(json.dumps({"function": fn["name"], "symbol": symbol, "unit": unit.name,
                                       "source_path": unit.source, "result": rel(work / "result.json"),
                                       "builds": builds}, indent=1), encoding="utf-8", newline="\n")
            cmd = [sys.executable if a == "python" else a.replace("{job}", rel(job)) for a in mcfg["build"]]
            ok, tail = fp.run(cmd, log=lambda *_: None, timeout=mcfg["timeout"])
            try:
                result = json.loads((work / "result.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                result = {}
            if not isinstance(result, dict):
                result = {}
            told, now = result.get("builds"), result.get("current")
            said: dict = told if isinstance(told, dict) else {}
            current = now if isinstance(now, str) else None
            options = fp.diff_options(unit)
            mips = _is_mips(unit.target)

            def measure(k: int) -> None:
                b, c = builds[k], cells[k]
                obj = fp.root / b["object"]
                entry = said.get(b["id"])
                one: dict = entry if isinstance(entry, dict) else {}
                if not obj.is_file() or one.get("ok") is False:
                    c["error"] = _errors(str(one.get("log") or "").strip() or (tail if not ok else "") or
                                         "the matrix build made no object for this profile")
                    return
                c["compiled"] = True
                try:
                    d = objdiff.diff(unit.target, obj, symbol, cwd=fp.root, options=options)
                except (ObjdiffError, ForkError, OSError) as e:
                    c["error"] = f"objdiff: {e}"[:600]
                    return
                if d.match_percent is None:
                    c["error"] = "the function isn't in the compiled object"
                    return
                c["percent"] = d.match_percent
                c["matched"] = d.matched
                c["diffs"] = 0 if d.matched else max(1, sum(1 for m, _, _ in d.rows if m.strip()), len(d.literals))
                c["score"] = (0 if d.matched else max(1, len(d.literals)) if d.literals
                              else scaled(d.match_percent, fn["n_insns"]))
                c["classes"] = compilerdiff.classify(d.rows) if mips else None
                c["rows"] = d.rows

            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(measure, range(len(builds))))
            return cells, current
        finally:
            shutil.rmtree(work, ignore_errors=True)

    @staticmethod
    def _best_cell(cells: list[dict], current: str | None) -> dict | None:
        """The cell to remember: a match (the file's own profile and context first), else the lowest score."""
        order = list(tucontext.CONTEXTS)
        scored = [c for c in cells if c["score"] is not None]
        if not scored:
            return None
        return min(scored, key=lambda c: (not c["matched"], c["score"], c["profile"] != current,
                                          order.index(c["context"])))

    def _matrix_keep(self, agent: Agent, slug: str, proj, fn, source: str, hits: list[dict],
                     current: str | None) -> tuple[bool, str]:
        """A match: committed when the file's own build makes it, saved with its recipe otherwise."""
        in_file = [c for c in hits if c["context"] == "file" and (current is None or c["profile"] == current)]
        if in_file:
            try:
                res = self.attempt(agent, slug, fn["name"], source, keep=False)
            except NetworkError as e:
                res = {"matched": False, "text": str(e)}
            if res.get("matched"):
                return True, "\n" + res["text"]
            if current is not None:  # the file's own profile matched here and its build says otherwise: no recipe
                return False, ("\n⚠️ The matrix matched under the file's own profile, but the project's build of "
                               f"{fn['unit']} didn't confirm it, so nothing was saved:\n{_tail(res['text'], 8)}")
        order = list(tucontext.CONTEXTS)
        hit = min(hits, key=lambda c: (c["profile"] != current, order.index(c["context"])))
        others = [f"{c['profile']} / {c['context']}" for c in hits if c is not hit]
        recipe = {"profile": hit["profile"], "context": hit["context"], "at": time.time(),
                  "source_hash": hashlib.sha256(source.encode()).hexdigest()}
        change = []
        if current is not None and hit["profile"] != current:
            change.append(f"compiled with {hit['profile']} (its file is built with {current})")
        if hit["context"] != "file":
            change.append(f"in the `{hit['context']}` context ({tucontext.CONTEXTS[hit['context']]})")
        how = " and ".join(change) or f"under {hit['profile']} / {hit['context']}"
        note = (f"MATRIX MATCH: this C is byte-identical to the original when {how}. The C is right; the build "
                f"needs the change (the file's compiler, or this function in its own file). Don't rewrite it."
                + (f" Also matches: {', '.join(others[:6])}." if others else ""))
        with self.net.store.tx() as db:
            row = db.execute("SELECT meta, best_score FROM functions WHERE id=?", (fn["id"],)).fetchone()
            meta = json.loads(row["meta"]) if row["meta"] else {}
            meta["matrix"] = recipe
            db.execute("UPDATE functions SET meta=?, best_source=?, best_agent_id=? WHERE id=?",
                       (json.dumps(meta), source, agent.id, fn["id"]))
            db.execute("INSERT INTO notes(project_id, function_id, agent_id, text, created_at) VALUES(?,?,?,?,?)",
                       (proj["id"], fn["id"], agent.id, note, time.time()))
            self.net._event(db, proj["id"], agent.id, "matrix", fn["name"])
        return False, (f"\n✅ MATRIX MATCH: {fn['name']} is byte-identical to the original when {how}.\n"
                       "Your C is right; what differs is how the project builds this file, which agents can't change. "
                       "The source and its recipe are saved on the function (notes and experiment history) for the "
                       "project's maintainer. Don't rewrite it: release_function, then claim the next one.")

    @staticmethod
    def _cell_of(r) -> dict:
        return {"profile": r["profile"], "compiler": r["compiler"] or "", "flags": r["flags"] or "",
                "context": r["context"], "compiled": bool(r["compiled"]), "score": r["score"], "percent": r["percent"],
                "diffs": r["diffs"], "matched": bool(r["matched"]), "error": r["error"],
                "classes": compilerdiff.from_json(r["classes"]) if r["classes"] else None,
                "current": bool(r["current"])}

    def _matrix_text(self, fn, mcfg: dict, cells: list[dict], source_hash: str, current: str | None,
                     took: float | None = None) -> str:
        """The matrix as a table: profiles down, contexts across, differing instructions in each cell."""
        if current is None:
            current = next((c["profile"] for c in cells if c.get("current")), None)
        ctxs = [c for c in tucontext.CONTEXTS if any(x["context"] == c for x in cells)]
        profs = list(dict.fromkeys(c["profile"] for c in cells))
        at = {(c["profile"], c["context"]): c for c in cells}
        width = max(len(p) for p in profs) + 3

        def show(c: dict | None) -> str:
            if c is None:
                return "·"
            if c["matched"]:
                return "MATCH"
            if not c["compiled"]:
                return "error"
            return "-" if c["diffs"] is None else str(c["diffs"])

        lines = [f"Compiler matrix for {fn['name']} (source {source_hash[:8]}): {len(profs)} profile(s) x "
                 f"{len(ctxs)} context(s), {len(cells)} builds" + (f" in {took:.1f}s" if took is not None else ""),
                 " " * width + "".join(f"{c:<11}" for c in ctxs)]
        for p in profs:
            label = p + (" *" if p == current else "")
            lines.append(f"{label:<{width}}" + "".join(f"{show(at.get((p, c))):<11}" for c in ctxs))
        legend = "cells: differing instructions (MATCH = byte-identical; error = didn't compile; - = function missing)"
        if current is not None:
            legend += f"; * = the profile {fn['unit']} is built with"
        lines.append(legend)
        skipped = [c for c in mcfg["contexts"] if c not in ctxs]
        if skipped:
            lines.append(f"not built (the same text as another context, or not asked for): {', '.join(skipped)}")
        best = self._best_cell(cells, current)
        if best is None:
            err = next((c["error"] for c in cells if c["error"]), "")
            lines.append("Nothing compiled." + (f" First error:\n{err}" if err else ""))
        elif best["matched"]:
            for ctx in ctxs:
                hits = [c["profile"] for c in cells if c["matched"] and c["context"] == ctx]
                if hits:
                    lines.append(f"Matches in `{ctx}`: " + ", ".join(hits))
        else:
            kinds = best["classes"].summary() if best["classes"] else ""
            lines.append(f"Best: {best['profile']} / {best['context']}: {best['diffs']} differing instruction(s), "
                         f"{percent_text(best['percent'])}" + (f" ({kinds})" if kinds else ""))
        return "\n".join(lines)

    # ------------------------------------------------------------------ after an ordinary compile

    def matrix_after_miss(self, agent: Agent, slug: str, function: str, source: str, rows: list,
                          percent: float | None) -> str:
        """What to add to a miss's answer: the kinds of difference left, and the matrix itself when they are all
        compiler-like and the project declares one."""
        try:
            _proj, fp, _cfg = self.get(slug)
            mips = _is_mips(fp.unit(self._fn(slug, function)["unit"]).target)
            mcfg = self.matrix_config(fp)
        except (NetworkError, OSError):
            return ""
        classes = compilerdiff.classify(rows) if mips else compilerdiff.Classes()
        if classes.verdict not in ("compiler", "maybe", "mixed"):
            return ""
        if mcfg is None:
            return "\n\n" + classes.note("none")
        if (classes.verdict != "compiler" or not mcfg["auto"] or (percent or 0) < NEAR_PERCENT
                or slug in self.net.links):
            return "\n\n" + classes.note("ask")
        try:
            res = self.matrix(agent, slug, function, source, terse=True)
        except NetworkError as e:
            return f"\n\n{classes.note('ask')}\n(compiler_matrix couldn't run just now: {e})"
        return (f"\n\nEvery difference left is of a kind a compiler makes ({classes.summary()}), so decomp.anything "
                f"tried your C under the project's other compilers and translation units:\n{res['text']}")

    # ------------------------------------------------------------------ history

    def matrix_runs(self, slug: str, function: str, limit: int = 12) -> list[dict]:
        """A function's experiments, newest run first: [{"run", "at", "agent", "source_hash", "cells": [...]}]."""
        fn = self._fn(slug, function)
        rows = self.net.store.all(
            "SELECT e.*, a.name AS agent FROM experiments e LEFT JOIN agents a ON a.id=e.agent_id WHERE "
            "e.function_id=? AND e.run IN (SELECT run FROM experiments WHERE function_id=? GROUP BY run "
            "ORDER BY MAX(id) DESC LIMIT ?) ORDER BY e.id", fn["id"], fn["id"], max(1, min(int(limit), 100)))
        runs: dict[str, dict] = {}
        for r in rows:
            run = runs.setdefault(r["run"], {"run": r["run"], "at": r["created_at"], "agent": r["agent"],
                                             "source_hash": r["source_hash"], "config": r["config"], "cells": []})
            cell = self._cell_of(r)
            cell["kinds"] = cell["classes"].summary() if cell["classes"] else ""
            cell["verdict"] = cell["classes"].verdict if cell["classes"] else None
            del cell["classes"]
            run["cells"].append(cell)
        return sorted(runs.values(), key=lambda x: -x["at"])

    def matrix_history(self, slug: str, function: str, limit: int = 8) -> str:
        fn = self._fn(slug, function)
        runs = self.matrix_runs(slug, function, limit)
        if not runs:
            return f"No compiler-matrix experiments for {fn['name']} yet: compiler_matrix(function) runs one."
        out = [f"Experiment history of {fn['name']} ({len(runs)} run(s), newest first)"]
        for run in runs:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(run["at"]))
            out.append(f"\n{when}, by {run['agent'] or '?'}, source {run['source_hash'][:8]}:")
            for c in sorted(run["cells"], key=lambda c: (c["score"] is None, c["score"] or 0)):
                state = ("MATCH" if c["matched"] else f"didn't compile: {(c['error'] or '').splitlines()[-1][:80]}"
                         if not c["compiled"] else c["error"] if c["diffs"] is None else
                         f"{c['diffs']} differing, {percent_text(c['percent'])}" + (f" ({c['kinds']})" if c["kinds"] else ""))
                what = " ".join(x for x in (c["compiler"], c["flags"]) if x)
                out.append(f"  {c['profile']}{' *' if c['current'] else ''} / {c['context']}"
                           + (f" [{what}]" if what else "") + f": {state}")
        return "\n".join(out)

    def matrix_matches(self, slug: str) -> list[dict]:
        """Every open function with a saved matrix match: its recipe and source, for whoever maintains the build."""
        proj, _fp, _cfg = self.get(slug)
        out = []
        for r in self.net.store.all("SELECT id, name, unit, status, best_source, meta FROM functions WHERE "
                                    "project_id=? AND status='open' AND meta LIKE ?", proj["id"], '%"matrix"%'):
            m = self._meta(r).get("matrix")
            if not isinstance(m, dict):
                continue
            row = self.net.store.one("SELECT source FROM experiments WHERE function_id=? AND matched=1 AND source "
                                     "IS NOT NULL AND profile=? AND context=? ORDER BY id DESC LIMIT 1",
                                     r["id"], m.get("profile"), m.get("context"))
            out.append({"function": r["name"], "symbol": self._meta(r).get("symbol") or r["name"], "unit": r["unit"],
                        "source_path": self._meta(r).get("source"), "profile": m.get("profile"),
                        "context": m.get("context"), "at": m.get("at"),
                        "source": (row["source"] if row else None) or r["best_source"]})
        return out

    def matrix_brief(self, fn, fp: ForkProject) -> list[str]:
        """For a function's brief: what the matrix already knows about it."""
        mcfg = self.matrix_config(fp)
        saved = self._meta(fn).get("matrix")
        if isinstance(saved, dict) and fn["status"] != "matched":
            return ["", f"### Compiler matrix: the best attempt already MATCHES under {saved.get('profile')} / "
                        f"{saved.get('context')}",
                    "Its C is right; the project's build has to change (the file's compiler, or this function in its "
                    "own file), which agents can't do. Don't rewrite it: release_function and claim another."]
        if mcfg is None:
            return []
        last = self.net.store.one("SELECT run, source_hash FROM experiments WHERE function_id=? ORDER BY id DESC LIMIT 1",
                                  fn["id"])
        if last is None:
            return ["", f"Compiler matrix: {len(mcfg['profiles'])} profiles x {len(mcfg['contexts'])} contexts; when "
                        "only compiler-like differences are left, compiler_matrix(function) tests your C under all of "
                        "them instead of another rewrite."]
        cells = [self._cell_of(r) for r in self.net.store.all("SELECT * FROM experiments WHERE run=? ORDER BY id",
                                                              last["run"])]
        return ["", "### Compiler matrix (last run)", "```", self._matrix_text(fn, mcfg, cells, last["source_hash"], None),
                "```", "A source the matrix already tried isn't worth compiling under other profiles again."]

    # ------------------------------------------------------------------ every near miss, in the background

    def matrix_status(self, slug: str) -> dict:
        _proj, fp, _cfg = self.get(slug)
        mcfg = self.matrix_config(fp)
        job = self._matrix_jobs.get(slug)
        return {"configured": mcfg is not None,
                "profiles": [p["name"] for p in mcfg["profiles"]] if mcfg else [],
                "contexts": mcfg["contexts"] if mcfg else [],
                "sweep": {k: v for k, v in job.items() if k != "stop"} if job else None,
                "matches": len(self.matrix_matches(slug)) if mcfg else 0}

    def matrix_sweep(self, slug: str, limit: int = 0) -> dict:
        """Put every open function that has C (in its file, or as a best attempt) through the matrix, closest
        first. Sources already tried are skipped, so it can be run again after agents worked."""
        proj, fp, _cfg = self.get(slug)
        if self.matrix_config(fp) is None:
            raise NetworkError(HOW)
        job = self._matrix_jobs.get(slug)
        if job is not None and job["state"] == "running":
            return self.matrix_status(slug)
        job = self._matrix_jobs[slug] = {"state": "running", "total": 0, "done": 0, "tried": 0, "matched": 0,
                                         "committed": 0, "skipped": 0, "failed": 0, "current": "",
                                         "started_at": time.time(), "finished_at": None, "stop": threading.Event()}
        threading.Thread(target=self._matrix_sweep, args=(slug, proj["id"], job, int(limit or 0)), daemon=True,
                         name=f"matrix-{slug}").start()
        return self.matrix_status(slug)

    def matrix_stop(self, slug: str) -> dict:
        job = self._matrix_jobs.get(slug)
        if job is not None:
            job["stop"].set()
        return self.matrix_status(slug)

    def _matrix_sweep(self, slug: str, project_id: int, job: dict, limit: int) -> None:
        try:
            agent, _ = self.net.local_agent("matrix")
            rows = self.net.store.all(
                "SELECT name FROM functions WHERE project_id=? AND status='open' AND COALESCE(meta,'') NOT LIKE ? "
                "ORDER BY best_score IS NULL, CAST(best_score AS REAL) / MAX(n_insns, 1), n_insns",
                project_id, '%"matrix"%')
            if limit > 0:
                rows = rows[:limit]
            job["total"] = len(rows)
            for r in rows:
                if job["stop"].is_set():
                    break
                job["current"] = r["name"]
                try:
                    res = self.matrix(agent, slug, r["name"])
                except NetworkError:  # no C yet, no source file: nothing to try
                    job["skipped"] += 1
                except Exception as e:  # noqa: BLE001 one odd function must not stop the rest
                    log.info("matrix: %s: %s", r["name"], e)
                    job["failed"] += 1
                else:
                    if res["repeat"]:
                        job["skipped"] += 1
                    else:
                        job["tried"] += 1
                        job["matched"] += bool(res["matched"])
                        job["committed"] += bool(res["committed"])
                job["done"] += 1
        finally:
            job["state"] = "stopped" if job["stop"].is_set() else "done"
            job["current"] = ""
            job["finished_at"] = time.time()
