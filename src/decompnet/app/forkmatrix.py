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

`build` runs per batch (a learned recipe first, then the remainder if needed), with `{job}` replaced by a JSON path:
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
import os
import random
import shutil
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..forks import compilerdiff, csource, matrixverify, objdiff, tucontext
from ..forks.objdiff import ObjdiffError
from ..forks.project import STATE, ForkError, ForkProject
from ..network import Agent, NetworkError
from ..permuter import mutate
from ..scoring import percent_text
from .forkutil import CHEAT, _around, _strip_comments, _tail, scaled

log = logging.getLogger("decomp.wtf")
PERMUTE_SECONDS = 45  # per function per turn
PERMUTE_BATCH = 24  # rewrites compiled by one run of the matrix build
PERMUTE_NEAR = 0.10  # a near miss: at most this much of the function differs in its closest cell
PERMUTE_TURNS = 3  # turns per function, per run of the app, before it's left to agents
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

    def project_seed(self, fp: ForkProject, fn, unit) -> tuple[str, str] | None:
        """(path, text) of the C the project itself has for a function that isn't matched yet: code that behaves
        like the original without compiling to its bytes (a functional rewrite, checked some other way). A project
        names where it keeps them in `.decomp.wtf.json`: "seeds": ["equiv/{target}/{symbol}.c"], with {symbol}, {unit}
        (the unit's name) and {target} (the unit's name up to its first slash). It is where an agent starts instead
        of a machine draft, and what the matrix compiles for a function that has no C in its file yet."""
        try:
            templates = (fp.manifest or {}).get("seeds")
        except (OSError, ValueError):
            return None
        if isinstance(templates, str):
            templates = [templates]
        if not isinstance(templates, list):
            return None
        meta = self._meta(fn)
        symbol = meta.get("symbol") or fn["name"]
        for template in templates[:8]:
            rel = (str(template).replace("{symbol}", symbol).replace("{unit}", unit.name)
                   .replace("{target}", unit.name.split("/", 1)[0]))
            try:
                path = fp.safe_path(rel)
                if not path.is_file() or path.stat().st_size > 64 * 1024:
                    continue
                text = path.read_text(errors="replace", encoding="utf-8")
            except (ForkError, OSError):
                continue
            _, span = self._find(text, self._names(meta, fn))
            if span is not None and not csource.is_stub(text[span[0]:span[1]]) and not CHEAT.search(_strip_comments(text)):
                return rel, text
        return None

    def _matrix_lock(self, proj) -> threading.Lock:
        with self._mlock:
            return self._matrix_locks[proj["id"]]

    def _matrix_learning(self, proj, fn, mcfg: dict, source_path: str, target_hash: str) -> list[dict]:
        """One vote per other function and recipe, from its latest verified experiment in this file/config/target."""
        rows = self.net.store.all(
            "SELECT e.*, f.name FROM experiments e JOIN functions f ON f.id=e.function_id WHERE e.id IN "
            "(SELECT MAX(id) FROM experiments WHERE project_id=? AND source_path=? AND config=? AND target_hash=? "
            "GROUP BY function_id, profile, context) AND f.project_id=? AND f.unit=? AND f.id<>? "
            "AND e.compiled=1 AND e.matched=1 ORDER BY e.id",
            proj["id"], source_path, mcfg["digest"], target_hash, proj["id"], fn["unit"], fn["id"])
        votes: dict[tuple[str, str], dict] = {}
        for row in rows:
            evidence = matrixverify.read_evidence(row["evidence"])
            if evidence.get("version") != matrixverify.VERSION or evidence.get("byte_exact") is not True:
                continue
            key = row["profile"], row["context"]
            vote = votes.setdefault(key, {"profile": key[0], "context": key[1], "matches": 0, "functions": []})
            vote["matches"] += 1
            if len(vote["functions"]) < 5:
                vote["functions"].append(row["name"])
        return sorted(votes.values(), key=lambda v: (-v["matches"], v["profile"], v["context"]))

    # ------------------------------------------------------------------ one function through the matrix

    def matrix(self, agent: Agent, slug: str, function: str, source: str = "", profiles: str = "", contexts: str = "",
               again: bool = False, commit: bool = True, terse: bool = False, exhaustive: bool = False) -> dict:
        """Compile `source` (default: the function as it is in its file, else its best attempt) under the
        project's profiles and contexts, trying a verified same-file recipe first unless `exhaustive` is set.
        {"text", "matched", "committed", "cells", "repeat"}. `commit`: whether a
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
            source = current if current and not csource.is_stub(current) else (
                fn["best_source"] or (self.project_seed(fp, fn, unit) or ("", ""))[1])
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
        target_hash = hashlib.sha256(unit.target.read_bytes()).hexdigest()
        input_hash = hashlib.sha256(json.dumps([matrixverify.VERSION, mcfg["digest"], target_hash, exhaustive,
                                               [p["name"] for p in plist], sorted(texts.items())])
                                    .encode()).hexdigest()
        if not again:
            done = self.net.store.all("SELECT * FROM experiments WHERE function_id=? AND run=(SELECT run FROM experiments "
                                      "WHERE function_id=? AND input_hash=? ORDER BY id DESC LIMIT 1) ORDER BY id",
                                      fn["id"], fn["id"], input_hash)
            if done:
                cells = [self._cell_of(r) for r in done]
                said = ("(These exact sources were already tried under this matrix; nothing was compiled. "
                        "again=true compiles anyway.)\n" + self._matrix_text(fn, mcfg, cells, source_hash, None))
                hits = [c for c in cells if c["matched"]]
                kept = False
                saved = meta.get("matrix") or {}
                if hits and commit and fn["status"] != "matched" and saved.get("verified") != matrixverify.VERSION:
                    # matched before and not kept (the tree was reset since, or it was only measured): keep it now
                    was = self._matrix_current(cells)
                    kept, more = self._matrix_keep(agent, slug, proj, fn, source, hits, was)
                    said += more
                return {"text": said, "matched": bool(hits), "committed": kept, "cells": cells, "repeat": True,
                        "source": source}

        run = uuid.uuid4().hex[:12]
        started = time.time()
        pairs = [(p["name"], ctx) for ctx in texts for p in plist]
        learning = [v for v in self._matrix_learning(proj, fn, mcfg, unit.source, target_hash)
                    if (v["profile"], v["context"]) in pairs]
        with self._matrix_lock(proj):
            if learning and not exhaustive:
                first = learning[0]
                preferred = (first["profile"], first["context"])
                # with it, every profile on the file as it is: a match the file's own build makes has to be seen,
                # or an ordinary match would be saved as one that needs the build changed
                early = [pair for pair in pairs if pair == preferred or pair[1] == "file"]
                cells, current = self._matrix_build(fp, mcfg, plist, texts, fn, unit, symbol, run, early)
                next(c for c in cells if (c["profile"], c["context"]) == preferred)["evidence"]["learning"] = first
                remaining = [pair for pair in pairs if pair not in early]
                if not any(c["matched"] for c in cells) and remaining:
                    rest, rest_current = self._matrix_build(fp, mcfg, plist, texts, fn, unit, symbol, run + "-rest", remaining)
                    cells.extend(rest)
                    if current and rest_current and current != rest_current:
                        raise NetworkError("the current compiler profile changed between batches; run the matrix again")
                    current = rest_current or current
            else:
                cells, current = self._matrix_build(fp, mcfg, plist, texts, fn, unit, symbol, run)
        if hashlib.sha256(unit.target.read_bytes()).hexdigest() != target_hash:
            raise NetworkError("the original object changed during the experiment; run the matrix again")
        for c in cells:
            c["current"] = c["profile"] == current
            c["evidence"]["planned_builds"] = len(pairs)
            c["evidence"]["current_profile"] = current
        took = time.time() - started
        best = self._best_cell(cells, current)
        with self.net.store.tx() as db:
            for c in cells:
                db.execute(
                    "INSERT INTO experiments(project_id, function_id, agent_id, run, config, input_hash, source_hash, "
                    "source, profile, compiler, flags, context, current, compiled, score, percent, diffs, matched, "
                    "classes, error, created_at, source_path, target_hash, evidence) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (proj["id"], fn["id"], agent.id, run, mcfg["digest"], input_hash, source_hash,
                     source if c is best else None, c["profile"], c["compiler"], c["flags"], c["context"],
                     int(c["profile"] == current), int(c["compiled"]), c["score"], c["percent"], c["diffs"],
                     int(c["matched"]), c["classes"].to_json() if c["classes"] else None, c["error"], started,
                     unit.source, target_hash, json.dumps(c["evidence"])))
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
        return {"text": "\n".join(out), "matched": bool(hits), "committed": committed, "cells": cells, "repeat": False,
                "source": source}

    def _matrix_build(self, fp: ForkProject, mcfg: dict, plist: list[dict], texts: dict[str, str], fn, unit,
                      symbol: str, run: str, pairs: list[tuple[str, str]] | None = None) -> tuple[list[dict], str | None]:
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
                    if pairs is not None and (p["name"], ctx) not in pairs:
                        continue
                    k = len(builds)
                    builds.append({"id": f"b{k}", "profile": p["name"], "context": ctx, "source": rel(src),
                                   "object": rel(work / f"b{k}.o")})
                    cells.append({"profile": p["name"], "compiler": p["compiler"], "flags": p["flags"], "context": ctx,
                                  "compiled": False, "score": None, "percent": None, "diffs": None, "matched": False,
                                  "classes": None, "error": None, "evidence": {"version": matrixverify.VERSION,
                                                                             "byte_exact": False}})
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
                c["evidence"] = matrixverify.verify(unit.target, obj, symbol, d.renamed)
                c["evidence"]["literal_diffs"] = len(d.literals)
                c["evidence"].setdefault("references", [list(pair) for pair in d.renamed])
                c["evidence"]["literal_details"] = [m.describe() for m in d.literals[:6]]
                c["matched"] = d.matched and c["evidence"]["byte_exact"] and not c["evidence"]["references"]
                c["diffs"] = sum(1 for m, _, _ in d.rows if m.strip())
                c["score"] = 0 if c["matched"] else max(1, len(d.literals), scaled(d.match_percent, fn["n_insns"]))
                c["classes"] = compilerdiff.classify(d.rows) if mips else None
                c["rows"] = d.rows

            with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 4)) as pool:
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
        hits = [c for c in hits if c["matched"] and c.get("evidence", {}).get("byte_exact") is True]
        if not hits:
            return False, "\nNo byte-verified match to save."
        in_file = [c for c in hits if c["context"] == "file" and (current is None or c["profile"] == current)]
        if in_file:
            try:
                res = self.attempt(agent, slug, fn["name"], source, keep=False, require_bytes=True)
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
                  "source_hash": hashlib.sha256(source.encode()).hexdigest(), "verified": matrixverify.VERSION}
        change = []
        if current is not None and hit["profile"] != current:
            change.append(f"compiled with {hit['profile']} (its file is built with {current})")
        if hit["context"] != "file":
            change.append(f"in the `{hit['context']}` context ({tucontext.CONTEXTS[hit['context']]})")
        how = " and ".join(change) or f"under {hit['profile']} / {hit['context']}"
        note = (f"MATRIX MATCH: this function's object bytes and relocation identities are verified when {how}. "
                "The project's normal build still needs verification before integration. Preserve this source and recipe."
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
        return False, (f"\n✅ MATRIX MATCH: {fn['name']} has byte-identical function code and matching relocations when {how}.\n"
                       "The project's normal build still needs verification before integration. "
                       "The source and its recipe are saved on the function (notes and experiment history) for the "
                       "project's maintainer. Preserve this result: release_function, then claim the next one.")

    @staticmethod
    def _matrix_current(cells: list[dict]) -> str | None:
        # An early match may omit the current profile entirely: retain the adapter's answer in the history.
        return next((c.get("evidence", {}).get("current_profile") for c in cells
                     if c.get("evidence", {}).get("current_profile")),
                    next((c["profile"] for c in cells if c.get("current")), None))

    @staticmethod
    def _cell_of(r) -> dict:
        evidence = matrixverify.read_evidence(r["evidence"])
        return {"profile": r["profile"], "compiler": r["compiler"] or "", "flags": r["flags"] or "",
                "context": r["context"], "compiled": bool(r["compiled"]), "score": r["score"], "percent": r["percent"],
                "diffs": r["diffs"], "matched": bool(r["matched"] and evidence.get("byte_exact") is True
                                                    and evidence.get("version") == matrixverify.VERSION), "error": r["error"],
                "classes": compilerdiff.from_json(r["classes"]) if r["classes"] else None,
                "current": bool(r["current"]), "evidence": evidence}

    def _matrix_evidence(self, cells: list[dict], current: str | None) -> list[str]:
        """Measured improvement and verification limits, also consumed by history and the browser."""
        best = self._best_cell(cells, current)
        lines = []
        for c in cells:
            learned = c.get("evidence", {}).get("learning")
            if learned:
                lines.append(f"Tried {c['profile']} / {c['context']} first: {learned['matches']} other function(s) "
                             "in this file verified that recipe (one vote per function).")
        planned = max((c.get("evidence", {}).get("planned_builds", len(cells)) for c in cells), default=0)
        if planned > len(cells):
            lines.append(f"Verified early match: built {len(cells)} of {planned} configurations; "
                         "exhaustive=true measures the complete matrix.")
        if best is None:
            return lines
        baseline = next((c for c in cells if c["context"] == "file" and c["profile"] == current
                         and c["diffs"] is not None), None)
        if baseline is not None and baseline is not best and baseline["diffs"] > 0 and best["diffs"] is not None:
            reduction = 100 * (baseline["diffs"] - best["diffs"]) / baseline["diffs"]
            if reduction > 0:
                lines.append(f"Alternative configuration {best['profile']} / {best['context']} eliminated {reduction:.1f}% "
                             f"of differing instruction rows ({baseline['diffs']} -> {best['diffs']}) relative to "
                             f"{current} / file, with the same submitted C.")
        elif baseline is None:
            lines.append("No measured file/current-profile baseline; no reduction percentage is inferred.")
        evidence = best.get("evidence", {})
        byte_diffs = evidence.get("byte_diffs")
        lines.append("Byte verification: " + str(evidence.get("reason") or "unavailable in this historical record")
                     + (f" ({byte_diffs} differing byte(s))" if byte_diffs is not None else "") + ".")
        if evidence.get("literal_diffs"):
            lines.append(f"Remaining constant differences: {evidence['literal_diffs']}. "
                         + "; ".join(evidence.get("literal_details", [])))
        if evidence.get("references"):
            lines.append("Remaining reference differences: " + "; ".join(f"{a} -> {b}" for a, b in evidence["references"]))
        if not best["matched"]:
            classes = best.get("classes")
            counts = classes.counts if classes else best.get("counts", {})
            if counts:
                lines.append("Remaining observed patterns: " + ", ".join(
                    f"{compilerdiff.KINDS[k][0]} x{n}" for k, n in counts.items()) + ".")
            lines.append("NOT A MATCH: fewer differences do not prove correct C or a compiler cause. "
                         "Review the remaining code, constants and references; byte-for-byte verification is required.")
        return lines

    def _matrix_text(self, fn, mcfg: dict, cells: list[dict], source_hash: str, current: str | None,
                     took: float | None = None) -> str:
        """The matrix as a table: profiles down, contexts across, differing instructions in each cell."""
        if current is None:
            current = self._matrix_current(cells)
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
            return "-" if c["diffs"] is None else "verify" if c["diffs"] == 0 else str(c["diffs"])

        lines = [f"Compiler matrix for {fn['name']} (source {source_hash[:8]}): {len(profs)} profile(s) x "
                 f"{len(ctxs)} context(s), {len(cells)} builds" + (f" in {took:.1f}s" if took is not None else ""),
                 " " * width + "".join(f"{c:<11}" for c in ctxs)]
        for p in profs:
            label = p + (" *" if p == current else "")
            lines.append(f"{label:<{width}}" + "".join(f"{show(at.get((p, c))):<11}" for c in ctxs))
        legend = ("cells: differing instruction rows (MATCH = verified bytes and relocations; verify = no row differences, "
                  "verification incomplete/failed; error = didn't compile; - = function missing)")
        if current is not None:
            legend += f"; * = the profile {fn['unit']} is built with"
        lines.append(legend)
        skipped = [c for c in mcfg["contexts"] if c not in ctxs]
        if skipped:
            lines.append(f"not built (duplicate text, not requested, or stopped after a verified match): {', '.join(skipped)}")
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
        lines.extend(self._matrix_evidence(cells, current))
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
        return (f"\n\nObserved patterns suggest testing compiler/context hypotheses ({classes.summary()}); "
                f"they do not rule out a source error. Testing the same C:\n{res['text']}")

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
            cell["counts"] = cell["classes"].counts if cell["classes"] else {}
            cell["examples"] = cell["classes"].examples if cell["classes"] else {}
            del cell["classes"]
            run["cells"].append(cell)
        for run in runs.values():
            current = self._matrix_current(run["cells"])
            run["evidence_text"] = "\n".join(self._matrix_evidence(run["cells"], current))
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
            out.append(run["evidence_text"])
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
            if not isinstance(m, dict) or m.get("verified") != matrixverify.VERSION:
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
            if saved.get("verified") != matrixverify.VERSION:
                return ["", "Historical matrix recipe has no strict byte evidence; run compiler_matrix again to verify it."]
            return ["", f"### Compiler matrix: the best attempt already MATCHES under {saved.get('profile')} / "
                        f"{saved.get('context')}",
                    "Function object bytes and relocations verified; the normal project build still needs verification. "
                    "Preserve the recipe: release_function and claim another."]
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
                "```", "Use exhaustive=true to measure all requested configurations; again=true reruns unchanged inputs."]

    # ------------------------------------------------------------------ every near miss, in the background

    def matrix_status(self, slug: str) -> dict:
        _proj, fp, _cfg = self.get(slug)
        mcfg = self.matrix_config(fp)
        job = self._matrix_jobs.get(slug)
        return {"configured": mcfg is not None,
                "profiles": [p["name"] for p in mcfg["profiles"]] if mcfg else [],
                "contexts": mcfg["contexts"] if mcfg else [],
                "sweep": {k: v for k, v in job.items() if k != "stop"} if job else None,
                "permute": ({k: v for k, v in self._permute_jobs[slug].items() if k != "stop"}
                            if slug in self._permute_jobs else None),
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
                "SELECT name FROM functions WHERE project_id=? AND status='open' "
                "AND COALESCE(json_extract(NULLIF(meta,''), '$.matrix.verified'),0)<>? "
                "ORDER BY best_score IS NULL, CAST(best_score AS REAL) / MAX(n_insns, 1), n_insns",
                project_id, matrixverify.VERSION)
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

    # ------------------------------------------------------------------ rewrites of a near miss, a batch at a time

    def matrix_permute(self, agent: Agent, slug: str, function: str, seconds: float = PERMUTE_SECONDS,
                       stop: threading.Event | None = None) -> dict:
        """Try rewrites of a near miss that keep its meaning (statement and declaration order, a cast, `register`,
        a loop's form: permuter.py) under the profile and context where it came closest. Each round's variants are
        compiled by one run of the project's matrix build, so a round costs about what one compile does.
        {"text", "matched", "committed", "variants", "improved", "before", "after"}."""
        proj, fp, _cfg = self.get(slug)
        mcfg = self.matrix_config(fp)
        if mcfg is None:
            raise NetworkError(HOW)
        fn = self._fn(slug, function)
        unit = fp.unit(fn["unit"])
        names = self._names(self._meta(fn), fn)
        symbol = self._meta(fn).get("symbol") or fn["name"]
        # start from the closest source the matrix has measured for this function, else from its C as it is
        closest = self.net.store.one("SELECT source FROM experiments WHERE function_id=? AND source IS NOT NULL AND "
                                     "score IS NOT NULL AND config=? ORDER BY score, id DESC LIMIT 1", fn["id"], mcfg["digest"])
        first = None
        if closest is not None:
            try:
                first = self.matrix(agent, slug, function, closest["source"])
            except NetworkError:  # that source no longer fits the file (a helper it used was renamed): start over
                first = None
        if first is None:
            first = self.matrix(agent, slug, function)
        base = {"variants": 0, "improved": 0, "before": None, "after": None}
        if first["matched"]:
            return {**base, "text": first["text"], "matched": True, "committed": first["committed"]}
        current = self._matrix_current(first["cells"])
        cell = self._best_cell(first["cells"], current)
        if cell is None:
            raise NetworkError(f"{fn['name']}'s C doesn't compile under any profile, so there is nothing to permute:\n"
                               + first["text"][-800:])
        source = first["source"]
        _, span = self._find(source, names)
        head, code, tail = source[:span[0]], source[span[0]:span[1]], source[span[1]:]
        with self._unit_lock(proj, unit.name):
            have = (fp.root / unit.source).read_text(errors="replace", encoding="utf-8")
        includes, before_lines, after_lines = _around(source, span, have, unit.source)
        profile = next(p for p in mcfg["profiles"] if p["name"] == cell["profile"])

        def body(variant: str) -> str:
            text = self._placed(proj, fp, unit, fn, names, have, variant, includes, before_lines, after_lines, "")
            made = tucontext.contexts(text, names[0], ["file", cell["context"]])
            return made.get(cell["context"], made["file"])  # a context that changes nothing is the file's text

        rng = random.Random(f"{fn['id']}-{hashlib.sha256(code.encode()).hexdigest()}-{self._permute_turns.get(fn['id'], 0)}")
        self._permute_turns[fn["id"]] = self._permute_turns.get(fn["id"], 0) + 1
        best = cur = cell["score"]
        best_code = cur_code = code
        seen = {code}
        variants = improved = 0
        deadline = time.monotonic() + max(1.0, float(seconds))
        while time.monotonic() < deadline and best > 0 and not (stop is not None and stop.is_set()):
            cands: list[str] = []
            for _ in range(PERMUTE_BATCH * 4):
                cand = mutate(cur_code if rng.random() < 0.85 else best_code, rng)
                if cand is not None and cand not in seen and self._find(cand, names)[1] is not None:
                    seen.add(cand)
                    cands.append(cand)
                    if len(cands) >= PERMUTE_BATCH:
                        break
            if not cands:
                break  # every rewrite this source allows has been tried
            texts = {f"v{i}": body(c) for i, c in enumerate(cands)}
            with self._matrix_lock(proj):
                measured, _ = self._matrix_build(fp, mcfg, [profile], texts, fn, unit, symbol,
                                                 f"permute-{uuid.uuid4().hex[:8]}")
            variants += len(cands)
            for cand, got in zip(cands, measured, strict=True):
                score = got["score"]
                if score is None:
                    continue
                if score < best:
                    best, best_code = score, cand
                    improved += 1
                if score <= cur or rng.random() < 0.02:  # mostly downhill, now and then sideways off a plateau
                    cur, cur_code = score, cand
            if best == 0:
                break
        out = {"variants": variants, "improved": improved, "before": cell["score"], "after": best}
        where = f"{cell['profile']} / {cell['context']}"
        if best >= cell["score"]:
            return {**out, "matched": False, "committed": False,
                    "text": f"Permuter: {variants} rewrites of {fn['name']} under {where}, none closer than score "
                            f"{cell['score']}. What is left is not a matter of statement order, declarations or casts."}
        # the best rewrite goes through the whole matrix: that records it, verifies it and keeps a match
        final = self.matrix(agent, slug, function, head + best_code + tail)
        if not final["matched"] and cell["context"] == "file" and cell["profile"] == current:
            self.net._record(fn, agent, best, (head.strip() + "\n\n" if head.strip() else "") + best_code, 0,
                             compiled=True, via="permuter")  # agents start from it
        return {**out, "matched": final["matched"], "committed": final["committed"],
                "text": f"Permuter: {variants} rewrites of {fn['name']} under {where}; score {cell['score']} -> {best}"
                        + (" (a match)" if best == 0 else "") + f".\n{final['text']}"
                        + ("" if final["matched"] else "\n\nThe closest rewrite:\n```c\n" + best_code.strip() + "\n```")}

    def matrix_permute_sweep(self, slug: str, seconds: float = PERMUTE_SECONDS, near: float = PERMUTE_NEAR,
                             limit: int = 0) -> dict:
        """Permute every open near miss the matrix has measured (its closest cell differs in at most `near` of the
        function), closest first, a few turns each."""
        proj, fp, _cfg = self.get(slug)
        if self.matrix_config(fp) is None:
            raise NetworkError(HOW)
        job = self._permute_jobs.get(slug)
        if job is not None and job["state"] == "running":
            return self.matrix_status(slug)
        job = self._permute_jobs[slug] = {"state": "running", "total": 0, "done": 0, "variants": 0, "improved": 0,
                                          "matched": 0, "committed": 0, "failed": 0, "current": "",
                                          "started_at": time.time(), "finished_at": None, "stop": threading.Event()}
        threading.Thread(target=self._matrix_permute_sweep, args=(slug, proj["id"], job, float(seconds), float(near),
                                                                 int(limit or 0)), daemon=True,
                         name=f"matrix-permute-{slug}").start()
        return self.matrix_status(slug)

    def matrix_permute_stop(self, slug: str) -> dict:
        job = self._permute_jobs.get(slug)
        if job is not None:
            job["stop"].set()
        return self.matrix_status(slug)

    def _matrix_permute_sweep(self, slug: str, project_id: int, job: dict, seconds: float, near: float,
                              limit: int) -> None:
        try:
            agent, _ = self.net.local_agent("permuter")
            rows = self.net.store.all(
                "SELECT f.id, f.name, MIN(e.score) AS s FROM experiments e JOIN functions f ON f.id=e.function_id "
                "WHERE f.project_id=? AND f.status='open' AND e.score > 0 "
                "AND COALESCE(json_extract(NULLIF(f.meta,''), '$.matrix.verified'),0)<>? "
                "AND f.id NOT IN (SELECT function_id FROM experiments WHERE matched=1 AND project_id=?) "
                "GROUP BY f.id HAVING s <= MAX(f.n_insns, 1) * 100 * ? ORDER BY s * 1.0 / MAX(f.n_insns, 1), f.n_insns",
                project_id, matrixverify.VERSION, project_id, near)
            rows = [r for r in rows if self._permute_turns.get(r["id"], 0) < PERMUTE_TURNS]
            if limit > 0:
                rows = rows[:limit]
            job["total"] = len(rows)
            for r in rows:
                if job["stop"].is_set():
                    break
                job["current"] = r["name"]
                try:
                    res = self.matrix_permute(agent, slug, r["name"], seconds, job["stop"])
                except NetworkError:
                    job["failed"] += 1
                except Exception as e:  # noqa: BLE001 one odd function must not stop the rest
                    log.info("matrix permuter: %s: %s", r["name"], e)
                    job["failed"] += 1
                else:
                    job["variants"] += res["variants"]
                    job["improved"] += bool(res["improved"])
                    job["matched"] += bool(res["matched"])
                    job["committed"] += bool(res["committed"])
                job["done"] += 1
        finally:
            job["state"] = "stopped" if job["stop"].is_set() else "done"
            job["current"] = ""
            job["finished_at"] = time.time()
