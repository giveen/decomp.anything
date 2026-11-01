"""Trying every open function's machine draft without an agent.

Identical code is tried first, because a twin costs nothing; then each open function's draft decompiler output is
compiled and judged, smallest first, and whatever matches is saved before any agent spends a token on it. A draft that
compiles becomes the function's best attempt, so an agent starts from it. A draft is made again only when what it was
made against changed, which is why "Run again" after a context or compiler change costs a fraction of the first run.
"""

# pyright: reportAttributeAccessIssue=false
# Each part is read on its own, so every self.<name> that lives in another part (or in Network) looks unknown to a
# type checker. RomNetwork is one class at runtime, and tests/test_engine_parts.py proves every name resolves there.


from __future__ import annotations

import json
import logging
import threading
import time

from .. import rea, scoring, seeds
from ..network import Agent, NetworkError
from .platform import (
    X86_PLATFORMS,
)


class AutopilotMixin:
    def start_autopilot(self, slug: str, workers: int = 3) -> dict:
        """Try every open function's machine draft (m2c, compilable form) through the judge, smallest first, in the
        background. Whatever already matches is saved before any agent spends a token on it; drafts that compile
        become the function's best attempt, so agents start from them. Agents and autopilot never collide: a
        function is held only while its draft compiles, and claimed ones are skipped."""
        state = self.autopilots.get(slug)
        if state and state["state"] == "running":
            return state
        if self.is_analysis(self._project(slug)):
            raise NetworkError("autopilot is for matching projects: an analysis project's agents read the target with REA")
        # first the twins (identical code to something matched: every project), then machine drafts where m2c reads
        # the code and the project's headers are fit for them (ROM projects; games started from scratch)
        try:
            if (fork := self.fork_slug(slug)):  # a GameCube, Wii or DS game started from scratch: its own build judges
                ids = self.forks.autopilot_prepare(fork)
                try_one, workers = self.forks.autopilot_one, min(workers, 2)  # each try builds a file: fewer at once
            else:
                proj = self._project(slug)
                if self.cfg(proj).get("platform") in X86_PLATFORMS:
                    # REA's decompiler drafts PC programs; m2c can't read x86. Without REA only twins are possible.
                    if self.rea_file(proj) is None:
                        raise NetworkError("autopilot's drafts for x86 programs come from REA (m2c can't read them): set it "
                                           "up in the RE console; until then it matches identical code only")
                else:
                    if not seeds.available():
                        raise NetworkError("autopilot's drafts need m2c, the draft decompiler: install it in Setup")
                    tc = self.cfg(proj)["compiler"]["toolchain"]
                    if not seeds.M2C_TARGETS.get(tc):
                        raise NetworkError("m2c can't read this project's code (no draft decompiler for it yet)")
                ids = [r["id"] for r in self.store.all(
                    "SELECT id FROM functions WHERE project_id=? AND status='open' AND boost >= 0 "
                    "ORDER BY (boost > 0) DESC, boost DESC, n_insns ASC, vram ASC", proj["id"]
                )]
                try_one = self._autopilot_one
            no_drafts = ""
        except NetworkError as e:
            ids, try_one, no_drafts = [], None, str(e)
        state = {"state": "running", "total": len(ids), "done": 0, "tried": 0, "matched": 0, "near": 0, "far": 0,
                 "failed": 0, "skipped": 0, "cached": 0, "twins": 0, "reasons": [], "phase": "twins",
                 "no_drafts": no_drafts, "started_at": time.time(), "stop": threading.Event()}
        self.autopilots[slug] = state
        threading.Thread(target=self._autopilot_run, args=(slug, ids, state, workers, try_one),
                         daemon=True, name=f"autopilot-{slug}").start()
        return state

    def stop_autopilot(self, slug: str) -> None:
        if (state := self.autopilots.get(slug)) and state["state"] == "running":
            state["stop"].set()

    def autopilot_status(self, slug: str) -> dict | None:
        state = self.autopilots.get(slug)
        if state:
            return {k: v for k, v in state.items() if k != "stop"}
        last = self.cfg(self._project(slug)).get("autopilot")  # the last run, from before the app restarted
        return {"state": "done", **last} if last else None

    def _autopilot_run(self, slug: str, ids: list[int], state: dict, workers: int, try_one=None) -> None:
        from concurrent.futures import ThreadPoolExecutor

        agent, _ = self.local_agent("autopilot")
        proj = self._project(slug)
        try_one = try_one or self._autopilot_one
        try:  # identical code first: free, and it takes those functions off the drafts' list
            if slug not in self.links:  # (a room member's matches come from the host)
                job = self.twins.full_pass(slug)
                state["twins"] = job.matched
                state["matched"] += job.matched
        except Exception as e:  # noqa: BLE001
            logging.getLogger("decomp.wtf").info("autopilot twins: %s", e)
        state["phase"] = "drafts"
        ids = [fid for fid in ids if (r := self.store.one("SELECT status FROM functions WHERE id=?", fid)) and r["status"] == "open"]
        state["total"] = len(ids)

        counts = threading.Lock()  # workers count into one state: a lost increment is a wrong panel

        def one(fid: int) -> None:
            if state["stop"].is_set():
                return
            try:
                res = try_one(agent, proj, fid)
            except Exception as e:  # noqa: BLE001 one odd function must not stop the rest
                res = {"outcome": "fail", "why": f"#{fid}: {str(e)[:120]}"}
            outcome, why = (res, "") if isinstance(res, str) else (res.get("outcome") or "fail", res.get("why") or "")
            with counts:
                state["done"] += 1
                if outcome in ("match", "near", "far", "cached", "skip"):
                    state[{"match": "matched", "skip": "skipped"}.get(outcome, outcome)] += 1
                else:
                    outcome = "fail"  # (anything unexpected counts as one, never as a draft tried)
                    state["failed"] += 1
                if outcome == "fail" and why and len(state["reasons"]) < 8:
                    state["reasons"].append(why)
                if outcome not in ("skip", "cached"):  # a draft was actually made and judged
                    state["tried"] += 1

        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(one, ids))
        finally:
            state["state"] = "stopped" if state["stop"].is_set() else "done"
            state["finished_at"] = time.time()
            with self.store.tx() as db:
                cfg = json.loads(db.execute("SELECT config FROM projects WHERE id=?", (proj["id"],)).fetchone()["config"])
                cfg["autopilot"] = {k: state.get(k) for k in ("total", "done", "tried", "matched", "near", "far",
                                                              "failed", "skipped", "cached", "twins", "reasons",
                                                              "no_drafts", "started_at", "finished_at")}
                db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))
            drafts = state["matched"] - state.get("twins", 0)
            if drafts:  # (twins announce their own)
                with self.store.tx() as db:
                    self._event(db, proj["id"], agent.id, "autopilot",
                                f"{drafts} of {state['tried']} machine drafts matched")

    def on_match(self, proj, function_id: int, agent: Agent | None = None) -> None:
        """Its twins get its code, at once. (Not for the full twins pass's own matches while it runs: it tries
        those twins itself.)"""
        if proj is None:
            return
        if agent is not None and agent.name == "autopilot" and self.twins._full_running(proj["slug"]):
            return
        self.twins.after_match(proj, function_id)

    def try_quietly(self, agent, proj, fn, source: str) -> dict:
        """Judge one candidate for a function nobody holds (autopilot's tries): held only while it compiles,
        recorded as an attempt like any other. {"matched", "score"}."""
        now = time.time()
        with self.store.tx() as db:
            self._expire_claims(db)
            if db.execute("SELECT 1 FROM claims WHERE function_id=?", (fn["id"],)).fetchone():
                raise NetworkError(f"{fn['name']} is claimed")
            db.execute("INSERT INTO claims VALUES(?,?,?,?)", (fn["id"], agent.id, now, now + 300))
        try:
            ctx = self._context(proj["id"], fn["unit"])
            result = self._compile(proj, ctx["header"], source)
            if not result.ok:
                return {"matched": False, "score": None}
            cand, verdict, _ = self._evaluate(fn, proj, result.obj)
            if cand is None:
                return {"matched": False, "score": None}
            d = scoring.diff(self._target_obj(fn, proj), cand)
            if verdict is not None:
                d = scoring.DiffResult(0 if verdict else max(1, d.score), d.max_score, d.rows, verdict)
            self._record(fn, agent, d.score, source, ctx["version"], matched=d.bytes_match)
            return {"matched": d.bytes_match, "score": d.score}
        finally:
            with self.store.tx() as db:
                db.execute("DELETE FROM claims WHERE function_id=? AND agent_id=?", (fn["id"], agent.id))

    def _compiler_key(self, proj) -> str:
        """What a draft is built with. Switching the compiler or its flags can change the verdict, so a draft tried
        before that is tried again (see draft_tries)."""
        compiler = self.cfg(proj).get("compiler") or {}
        return f"{compiler.get('toolchain') or ''} {' '.join(compiler.get('flags') or [])}".strip()

    def _autopilot_one(self, agent, proj, fid: int) -> dict:
        """One function's m2c draft through the judge: {"outcome": match | near | far | fail | skip | cached,
        "why": a short reason for the panel}. A draft is made and compiled again only when what it's made against
        changed (the context version, or the compiler and flags): "Run again" then costs what actually improved,
        not every open function's draft a second time."""
        # PC programs are drafted by REA's decompiler (m2c can't read x86); without REA there is nothing to try
        pc = self.cfg(proj).get("platform") in X86_PLATFORMS
        if pc and self.rea_file(proj) is None:
            return {"outcome": "skip", "why": "no machine-draft decompiler for x86: set REA up in the RE console"}
        fn = self.store.one("SELECT * FROM functions WHERE id=?", fid)
        if fn is None or fn["status"] != "open":
            return {"outcome": "skip", "why": "no longer open"}
        compiler = self._compiler_key(proj)
        ctx = self._context(proj["id"], fn["unit"])
        done = self.store.one("SELECT outcome, why FROM draft_tries WHERE function_id=? AND context_version=? "
                              "AND compiler=?", fid, ctx["version"], compiler)
        if done is not None:  # nothing it depends on changed since: the same draft gives the same verdict
            return {"outcome": "cached", "why": done["why"] or done["outcome"]}
        now = time.time()
        with self.store.tx() as db:
            self._expire_claims(db)
            fn = db.execute("SELECT * FROM functions WHERE id=?", (fid,)).fetchone()
            if fn is None or fn["status"] != "open" or db.execute("SELECT 1 FROM claims WHERE function_id=?", (fid,)).fetchone():
                return {"outcome": "skip", "why": "claimed by an agent"}
            db.execute("INSERT INTO claims VALUES(?,?,?,?)", (fid, agent.id, now, now + 300))
        outcome, why = "fail", f"{fn['name']}: the draft went wrong"
        try:
            self._seed_for(fn, proj)  # the readable draft, for the agents that come after
            source = ""
            if pc:  # REA's pseudocode, straight from Ghidra
                target = self.rea_file(proj)
                base = int((self.cfg(proj).get("rea") or {}).get("base") or 0)
                text = rea.pseudo(target, fn, base=base) if target is not None else None
                if text:
                    source = f"/* machine draft (REA, Ghidra), tried by autopilot */\n{text}\n"
                else:
                    why = f"{fn['name']}: REA made no draft"
            else:  # m2c, from the assembly
                data_asm = "\n".join(self._jtbl_asm(proj, fn)) if "jtbl_" in (fn["asm"] or "") else None
                draft = seeds.generate_from_asm(self._asm_text(fn, proj), fn["name"],
                                                self.cfg(proj)["compiler"]["toolchain"], ctx["header"], data_asm,
                                                valid=True)
                if not draft:
                    why = f"{fn['name']}: m2c made no draft"
                elif "M2C_ERROR" in draft or draft.count("goto ") > 10:
                    outcome = "far"
                    why = f"{fn['name']}: draft has errors/excessive gotos"
                else:
                    source = f"/* machine draft (m2c), tried by autopilot */\n{seeds.M2C_MACROS}\n{draft}\n"
            if source:
                result = self._compile(proj, ctx["header"], source)
                if not result.ok:
                    why = f"{fn['name']}: the draft didn't compile"
                else:
                    cand, verdict, _ = self._evaluate(fn, proj, result.obj)
                    if cand is None:
                        why = f"{fn['name']}: the object it built has no {fn['name']}"
                    else:
                        d = scoring.diff(self._target_obj(fn, proj), cand)
                        if verdict is not None:
                            d = scoring.DiffResult(0 if verdict else max(1, d.score), d.max_score, d.rows, verdict)
                        self._record(fn, agent, d.score, source, ctx["version"], matched=d.bytes_match)
                        outcome = "match" if d.bytes_match else "near" if d.percent >= 90 else "far"
                        why = "" if d.bytes_match else f"{fn['name']}: {d.percent:.0f}% there"
        except Exception as e:  # noqa: BLE001 one odd function is reported, not left to stop the run
            outcome, why = "fail", f"{fn['name']}: {str(e)[:120]}"
        finally:
            with self.store.tx() as db:
                db.execute("DELETE FROM claims WHERE function_id=? AND agent_id=?", (fid, agent.id))
        if outcome == "fail":
            logging.getLogger("decomp.wtf").info("autopilot: %s", why)
        with self.store.tx() as db:  # what was tried, so the next run can leave an unchanged draft alone
            db.execute("INSERT OR REPLACE INTO draft_tries(function_id,project_id,context_version,compiler,outcome,why,at) "
                       "VALUES(?,?,?,?,?,?,?)", (fid, proj["id"], ctx["version"], compiler, outcome, why, time.time()))
        return {"outcome": outcome, "why": why}
