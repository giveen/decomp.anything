"""Trying a fork's drafts without an agent.

The autopilot builds and measures every open unit's drafts, smallest first, and keeps what matches. A fork builds a file
at a time, so fewer workers run at once than for a ROM project (see autopilot.py for the matching version)."""

# pyright: reportAttributeAccessIssue=false
# A type checker reads one part at a time, so a self.<name> owned by another part (or by the engine itself) looks
# unknown. ForkEngine is one class at runtime; tests/test_fork_parts.py proves every name resolves there.


from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from contextlib import suppress

from .. import toolchains
from ..forks import drafts
from ..forks.project import ForkProject
from ..network import Agent, Network, NetworkError

NOT_C_AGENTS, NOT_C_MODELS = 3, 2  # agents, of different models, who must agree a function is no C's


def _model_of(agent: str) -> str:
    """The model behind a helper's name: claude-code-3 and claude-code-7 are one model, codex-1 another."""
    return re.sub(r"[-_ ]*\d+$", "", agent or "") or agent


class ForkAutoMixin:
    def autopilot_prepare(self, slug: str) -> list[int]:
        """The open functions autopilot tries in a game started from scratch, smallest first. Raises NetworkError
        when it can't run here."""
        proj, fp, cfg = self.get(slug)
        if cfg.get("origin") != "scratch":
            raise NetworkError("autopilot works on games started from scratch: a public decomp's headers don't have "
                               "what machine drafts need, and its easy functions are done already")
        if not cfg.get("ready"):
            raise NetworkError("finish setting the project up first")
        if not drafts.supported(cfg.get("platform") or "") or toolchains.installed("m2c") is None:
            raise NetworkError("autopilot needs m2c, the draft decompiler: install it in Setup")
        if drafts.add_m2c_macros(fp.root / "include" / "common.h"):  # projects made before autopilot ran on them
            self._commit(proj, fp, "include/common.h", "Add the macros machine drafts use\n\nWritten by decomp.wtf's autopilot.")
        rows = self.net.store.all("SELECT id FROM functions WHERE project_id=? AND status='open' ORDER BY n_insns, id",
                                  proj["id"])
        return [r["id"] for r in rows]

    def autopilot_one(self, agent: Agent, proj, fid: int) -> str:
        """match | near | far | fail | skip, for one function's machine draft, built and diffed like an agent's."""
        now = time.time()
        with self.net.store.tx() as db:
            self.net._expire_claims(db)
            fn = db.execute("SELECT * FROM functions WHERE id=?", (fid,)).fetchone()
            if fn is None or fn["status"] != "open" or db.execute("SELECT 1 FROM claims WHERE function_id=?", (fid,)).fetchone():
                return "skip"
            db.execute("INSERT INTO claims VALUES(?,?,?,?)", (fid, agent.id, now, now + 300))
        try:
            _, fp, cfg = self.get(proj["slug"])
            unit = fp.unit(fn["unit"])
            if not unit.source:
                return "skip"
            meta = self._meta(fn)
            hint = (unit.scratch or {}).get("compiler", "") or json.dumps(fp.needs(cfg.get("version")))
            text = drafts.draft(fp.root, cfg.get("platform") or "", unit.name, meta.get("symbol") or fn["name"],
                                unit.source, compiler_hint=hint, valid=True)
            _, span = self._find(text, self._names(meta, fn)) if text else (None, None)
            if span is None:
                return "fail"
            try:
                current = (fp.root / unit.source).read_text(errors="replace", encoding="utf-8")
            except OSError:
                current = ""
            code = "/* machine draft (m2c), tried by autopilot */\n" + drafts.self_contained(text or "", span, current)
            res = self.attempt(agent, proj["slug"], fn["name"], code, keep=False)
            if res["matched"]:
                return "match"
            if res["score"] is None:
                why = [ln for ln in res["text"].splitlines() if re.search(r"error|Error", ln) and "ninja" not in ln]
                logging.getLogger("decomp.wtf").info("autopilot: %s's draft didn't build: %s", fn["name"],
                                                     " | ".join(why[:4])[:600] or res["text"][-300:])
            return "fail" if res["score"] is None else "near" if (res.get("percent") or 0) >= 90 else "far"
        except (NetworkError, OSError, subprocess.SubprocessError) as e:
            logging.getLogger("decomp.wtf").info("autopilot: %s not tried: %s", fn["name"], str(e)[:500])
            return "fail"
        finally:
            with self.net.store.tx() as db:
                db.execute("DELETE FROM claims WHERE function_id=? AND agent_id=?", (fid, agent.id))

    def _insert_in_order(self, proj, fp: ForkProject, unit, fn, text: str, code: str) -> str:
        """Place a new function where it was in the original: after the closest earlier function present in the
        file, else before the closest later one, else at the end."""
        code = code.strip("\n") + "\n"
        entry = self._unit_index(proj, fp, unit)
        sym = self._meta(fn).get("symbol")
        order = entry["order"] if entry else []
        if sym in order:
            rows = {self._meta(r).get("symbol"): r for r in self.net.store.all(
                "SELECT name, meta FROM functions WHERE project_id=? AND unit=?", proj["id"], unit.name)}
            k = order.index(sym)
            for other in reversed(order[:k]):
                if other in rows:
                    _, span = self._find(text, self._names(self._meta(rows[other]), rows[other]))
                    if span:
                        return text[:span[1]].rstrip("\n") + "\n\n" + code + "\n" + text[span[1]:].lstrip("\n")
            for other in order[k + 1:]:
                if other in rows:
                    _, span = self._find(text, self._names(self._meta(rows[other]), rows[other]))
                    if span:
                        return text[:span[0]] + code + "\n" + text[span[0]:]
        return text.rstrip("\n") + "\n\n" + code

    def vote_not_c(self, agent: Agent, slug: str, function: str, why: str) -> str:
        """An agent's verdict that no C produces this function. Once NOT_C_AGENTS agents of NOT_C_MODELS different
        models agree it is retired (status unmatchable, their reasons kept): out of the queue for good, until the
        user gives it back. One agent alone can't take a function away, and two sessions of one model don't
        outvote another model."""
        fn = self._fn(slug, function)
        if fn["status"] == "matched":
            raise NetworkError(f"{fn['name']} is matched: C does produce it")
        why = " ".join((why or "").split())
        if len(why) < 15:
            raise NetworkError("say what in the assembly no C produces (an instruction, a register use, a call into "
                               "the middle of something): the next agents read it")
        meta = self._meta(fn)
        votes = [v for v in meta.get("not_c_votes") or [] if v.get("agent") != agent.name]
        votes.append({"agent": agent.name, "model": _model_of(agent.name), "why": why[:300], "at": time.time()})
        agents, models = {v["agent"] for v in votes}, {v["model"] for v in votes}
        retire = fn["status"] != "unmatchable" and len(agents) >= NOT_C_AGENTS and len(models) >= NOT_C_MODELS
        meta["not_c_votes"] = votes[-12:]
        with self.net.store.tx() as db:
            if retire:
                reasons = list(dict.fromkeys(v["why"] for v in votes))[:3]
                meta["unmatchable"] = f"{len(agents)} agents of {len(models)} models agree no C produces it: " + " | ".join(reasons)
                db.execute("UPDATE functions SET status='unmatchable', meta=?, changed_at=? WHERE id=?",
                           (json.dumps(meta), time.time(), fn["id"]))
                db.execute("DELETE FROM claims WHERE function_id=? AND agent_id<>?", (fn["id"], agent.id))
                self.net._event(db, proj_id := fn["project_id"], agent.id, "retire", fn["name"])
                db.execute("INSERT INTO notes(project_id,function_id,agent_id,text,created_at) VALUES(?,?,?,?,?)",
                           (proj_id, fn["id"], agent.id, f"retired: {meta['unmatchable']}", time.time()))
            else:
                db.execute("UPDATE functions SET meta=? WHERE id=?", (json.dumps(meta), fn["id"]))
        if retire:
            return f"{fn['name']} is retired: {len(agents)} agents of {len(models)} models agree no C produces it."
        return (f"Your verdict is recorded ({len(agents)} of {NOT_C_AGENTS} agents, {len(models)} of {NOT_C_MODELS} "
                "models so far): the function stays in the queue until enough agents agree.")

    def give_back(self, slug: str, function: str) -> dict:
        """The user's word over the agents': a retired function goes back to the queue, its verdicts cleared."""
        fn = self._fn(slug, function)
        meta = self._meta(fn)
        meta.pop("unmatchable", None)
        meta.pop("not_c_votes", None)
        with self.net.store.tx() as db:
            db.execute("UPDATE functions SET status=CASE WHEN status='unmatchable' THEN 'open' ELSE status END, meta=?, "
                       "changed_at=? WHERE id=?", (json.dumps(meta), time.time(), fn["id"]))
            self.net._event(db, fn["project_id"], None, "note", f"{fn['name']} given back to the agents")
        return {"ok": True}

    def release(self, agent: Agent, slug: str, function: str, notes: str) -> str:
        fn = self._fn(slug, function)
        if (link := self.net.links.get(slug)) is not None:
            link.call("/collab/release", agent, function=fn["name"], notes=notes)
        latest = self.net.store.one("SELECT score FROM attempts WHERE function_id=? AND agent_id=? ORDER BY id DESC LIMIT 1",
                                    fn["id"], agent.id)
        if (fn["best_source"] and fn["status"] != "matched" and latest and latest["score"] is not None
                and fn["best_score"] is not None and latest["score"] > fn["best_score"]):
            with suppress(NetworkError):  # leave the file at its best state for the next agent
                self.attempt(agent, slug, function, fn["best_source"])
        return Network.release(self.net, agent, function, notes, slug)

