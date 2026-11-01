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

