"""Working on a shared project: the host's side of a room.

A room's members keep working on their own copy. What they claim, attempt, match, note and change arrives here as an
event, and this is where it is applied to the host's database: claims, attempts, best attempts, notes, context, and the
layout of what each member has split and named. Every method here that overrides the plain Network version falls back
to it when the project is not in a room, so a project works alone and in company through the same calls.
"""

# pyright: reportAttributeAccessIssue=false
# Each part is read on its own, so every self.<name> that lives in another part (or in Network) looks unknown to a
# type checker. RomNetwork is one class at runtime, and tests/test_engine_parts.py proves every name resolves there.


from __future__ import annotations

import bisect
import contextlib
import json
import re
import secrets
import threading
import time

from ..network import Agent, NetworkError
from ..rom import split
from ..rom.symbols import Symbol
from .platform import (
    BATCH_INSNS,
)


class RoomOpsMixin:
    def link_project(self, slug: str, info: dict, client=None, start: bool = True):
        from .collab import Link

        proj = self._project(slug)
        cfg = self.cfg(proj)
        # A host's project is the room's truth: making it a member of a room (a host testing its own invite with its
        # own copy of the game) takes the room down. A member joins with its own project.
        if slug in self.rooms or (cfg.get("collab") or {}).get("open"):
            raise NetworkError(f"{slug} is set up as a room host on this computer: a host project doesn't join a room "
                               "as a member. Share it and let someone else join, or stop sharing it first.")
        cfg["link"] = info
        with self.store.tx() as db:
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))
        link = Link(self, slug, info["url"], info["room"], info["token"], info["member"], client=client)
        self.links[slug] = link
        if start:
            link.run()
        return link

    def restore_links(self) -> None:
        for p in self.store.all("SELECT slug, config FROM projects"):
            cfg = json.loads(p["config"])
            info = cfg.get("link")
            if not info:
                continue
            if (cfg.get("collab") or {}).get("open"):
                # An older build let a host join its own room; that membership is not one. The project stays the
                # room's host and the room can be shared again.
                cfg.pop("link", None)
                with self.store.tx() as db:
                    db.execute("UPDATE projects SET config=? WHERE slug=?", (json.dumps(cfg), p["slug"]))
                continue
            if p["slug"] not in self.links:
                self.link_project(p["slug"], info)

    def unlink(self, slug: str) -> None:
        link = self.links.pop(slug, None)
        if link is not None:
            link.stop()
            with contextlib.suppress(NetworkError):
                link._req("POST", "/collab/leave", {})
        proj = self._project(slug)
        cfg = self.cfg(proj)
        cfg.pop("link", None)
        with self.store.tx() as db:
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))

    def _link(self, project: str | None):
        if not self.links:
            return None
        try:
            return self.links.get(self._project(project)["slug"])
        except NetworkError:
            return None

    def claim(self, agent: Agent, project: str | None = None, function: str | None = None, strategy: str = "easiest") -> str:
        try:
            proj = self._project(project)
        except NetworkError:
            proj = None
        if proj is not None and self.is_analysis(proj):
            fn = self.claim_row(agent, proj["slug"], function, strategy)
            if fn is None:
                return f"Nothing left to claim in {proj['slug']} — every open item is analyzed or claimed. 🎉"
            return self._analysis_brief(fn, agent, header="CLAIMED")
        if (fork := self.fork_slug(project)):
            return self.forks.claim(agent, fork, function, strategy)
        link = self._link(project)
        if link is None:
            return super().claim(agent, project, function, strategy)
        if function:
            function = self._function(function, project)["name"]
        name = link.claim(agent, function, strategy)
        if name is None:
            return "Nothing left to claim in this room — every open function is matched or someone is on it. 🎉"
        link.sync()
        fn = self.claim_row(agent, project, name, strategy)
        return self._brief(fn, agent, header="CLAIMED")

    def claim_many(self, agent: Agent, project: str | None, count: int, strategy: str = "easiest") -> str:
        """Several small functions at once (count 2-6): the next one by `strategy` and, when it's small, its
        neighbours in the same file. Most small functions match on the first try, so an agent writes them all and
        judges them in one compile_and_diff: a fraction of the turns. Big functions come alone."""
        count = max(1, min(int(count or 1), 6))
        try:
            proj = self._project(project)
        except NetworkError:
            proj = None
        if proj is not None and self.is_analysis(proj):
            return self.claim(agent, project, None, strategy)
        if count == 1 or self._link(project) is not None:
            return self.claim(agent, project, None, strategy)
        proj = self._project(project)
        held = self.store.all("SELECT f.* FROM claims c JOIN functions f ON f.id=c.function_id WHERE c.agent_id=? "
                              "AND f.project_id=? ORDER BY f.vram, f.id", agent.id, proj["id"])
        if not held:
            text = self.claim(agent, project, None, strategy)  # the first, the usual way (with its "nothing left")
            held = self.store.all("SELECT f.* FROM claims c JOIN functions f ON f.id=c.function_id WHERE c.agent_id=? "
                                  "AND f.project_id=?", agent.id, proj["id"])
            if not held or held[0]["n_insns"] > BATCH_INSNS:
                return text
        first = held[0]
        rows = list(held)
        if first["n_insns"] <= BATCH_INSNS and len(rows) < count:
            rows += self._batch_more(agent, proj, first, count - len(rows))
        if len(rows) == 1:
            return self.claim(agent, project, None, strategy)
        names = [r["name"] for r in rows]
        fork = self.fork_slug(project)
        head = (f"## CLAIMED {len(rows)} small functions: {', '.join(names)}\n"
                f"Write all of them, then judge them in one call: compile_and_diff(function=\"{', '.join(names)}\", "
                f"source=<all {len(rows)} definitions>). Each is judged on its own and saved as soon as it matches; "
                "then fix what's left (one at a time is fine). release_function any you can't do.")
        briefs = [self.forks.brief(r, agent, "CLAIMED") if fork else self._brief(r, agent, "CLAIMED") for r in rows]
        return "\n\n---\n\n".join([head, *briefs])

    def _batch_more(self, agent: Agent, proj, first, n: int) -> list:
        """Up to n more small open functions next to `first` (same file, nearest address), claimed for the agent."""
        now = time.time()
        with self.store.tx() as db:
            self._expire_claims(db)
            rows = db.execute(
                "SELECT f.* FROM functions f WHERE f.project_id=? AND f.status='open' AND f.boost >= 0 AND f.n_insns <= ? "
                "AND f.abandons = 0 AND f.unit=? AND f.id NOT IN (SELECT function_id FROM claims) "
                "ORDER BY ABS(COALESCE(f.vram, 0) - ?), f.id LIMIT ?",
                (proj["id"], BATCH_INSNS, first["unit"], first["vram"] or 0, n)).fetchall()
            for r in rows:
                db.execute("INSERT INTO claims VALUES(?,?,?,?)", (r["id"], agent.id, now, now + self.claim_ttl))
                self._event(db, proj["id"], agent.id, "claim", r["name"])
        return rows

    def attempt_many(self, agent: Agent, names: list[str], source: str, project: str | None = None) -> str:
        """compile_and_diff for several functions in one source: each judged on its own (a fork's, with the other
        definitions taken out, as its file gets them one by one), in order."""
        fork = self.fork_slug(project)
        out, matched = [], 0
        for name in names:
            try:
                one = self.forks.one_definition(fork, source, name, [n for n in names if n != name]) if fork else source
                res = self.attempt(agent, name, one, project)
            except NetworkError as e:
                res = {"text": f"❌ {e}", "matched": False}
            matched += bool(res.get("matched"))
            out.append(f"### {name}\n{res['text']}")
        return f"{matched} of {len(names)} matched.\n\n" + "\n\n".join(out)

    def _yield_autopilot(self, function: str | None, project: str | None) -> None:
        """The autopilot holds a function only while its draft compiles: an agent that wants it waits for that."""
        if not function:
            return
        try:
            fid = self._function(function, project)["id"]
        except NetworkError:
            return
        for _ in range(100):
            held = self.store.one("SELECT a.name FROM claims c JOIN agents a ON a.id=c.agent_id WHERE c.function_id=?", fid)
            if not held or held["name"] != "autopilot":
                return
            time.sleep(0.2)

    def attempt(self, agent: Agent, function: str, source: str, project: str | None = None) -> dict:
        self._yield_autopilot(function, project)
        if (fork := self.fork_slug(project)):
            return self.forks.attempt(agent, fork, function, source)
        link = self._link(project)
        before = self._function(function, project)["best_score"] if link else None
        res = super().attempt(agent, function, source, project)
        if link is None or res["score"] is None:
            return res
        name = self._function(function, project)["name"]
        if res["matched"]:
            verdict = link.attempt(agent, name, source)
            if not verdict.get("matched"):
                res["text"] += ("\n⚠️ The room's host could not confirm this match on their copy (score "
                                f"{verdict.get('score')}). Check that both of you use the same compiler settings.")
            else:
                res["text"] += "\nShared with the room: the host confirmed the match."
        elif before is None or res["score"] < before:
            threading.Thread(target=lambda: self._report(link, agent, name, source, res["score"]), daemon=True).start()
        return res

    @staticmethod
    def _report(link, agent, name, source, score) -> None:
        with contextlib.suppress(NetworkError):
            link.call("/collab/best", agent, function=name, source=source, score=score)

    def apply_reported_best(self, slug: str, name: str, score: int, source: str, agent: Agent) -> None:
        """Host side: a room member's better attempt, as they report it (their own compile scored it)."""
        fn = self._function(name, slug)
        if fn["status"] != "open" or score <= 0 or (fn["best_score"] is not None and score >= fn["best_score"]):
            return
        self._record(fn, agent, score, source, self._must_context(fn["project_id"], fn["unit"])["version"], via="room")

    def release(self, agent: Agent, function: str, notes: str, project: str | None = None) -> str:
        if (fork := self.fork_slug(project)):
            return self.forks.release(agent, fork, function, notes)
        link = self._link(project)
        if link is not None:
            link.call("/collab/release", agent, function=self._function(function, project)["name"], notes=notes)
        return super().release(agent, function, notes, project)

    def edit_context(self, agent: Agent, project: str | None, old: str, new: str, message: str,
                     function: str | None = None) -> str:
        self._rom_only(project, "edit_context")
        link = self._link(project)
        if link is None:
            return super().edit_context(agent, project, old, new, message, function)
        text = link.call("/collab/context", agent, old=old, new=new, message=message, function=function)
        if text.startswith("✅"):
            link.sync()
        return text

    def add_note(self, agent: Agent, project: str | None, text: str, function: str | None = None) -> str:
        link = self._link(project)
        if link is None:
            return super().add_note(agent, project, text, function)
        name = self._function(function, project)["name"] if function else None
        return link.call("/collab/note", agent, text=text, function=name) or "Noted, and shared with the room."

    def _remote_agent(self, name: str) -> int:
        label = "@" + name
        if label in self._remote_ids:
            return self._remote_ids[label]
        row = self.store.one("SELECT id FROM agents WHERE name=?", label)
        if row is None:
            with self.store.tx() as db:
                aid = db.execute("INSERT INTO agents(name,owner,token_hash,created_at,trusted,origin) VALUES(?,?,?,?,1,?)",
                                 (label, "room", "remote:" + secrets.token_hex(16), time.time(), "remote")).lastrowid
        else:
            aid = row["id"]
        self._remote_ids[label] = aid
        return aid

    def remote_event(self, project_id: int, kind: str, detail: str, agent_name: str, created_at: float) -> None:
        aid = self._remote_agent(agent_name)
        with self.store.tx() as db:
            db.execute("INSERT INTO events(project_id,agent_id,kind,detail,created_at) VALUES(?,?,?,?,?)",
                       (project_id, aid, kind, detail, created_at))

    def apply_remote_match(self, slug: str, vram: int, source: str, agent_name: str) -> None:
        proj = self._project(slug)
        aid = self._remote_agent(agent_name)
        with self.store.tx() as db:
            row = db.execute("SELECT id, status FROM functions WHERE project_id=? AND vram=?", (proj["id"], vram)).fetchone()
            if row is None or row["status"] == "matched":
                return
            db.execute("UPDATE functions SET status='matched', matched_source=?, matched_agent_id=?, matched_at=?, "
                       "best_score=0, best_source=? WHERE id=?", (source, aid, time.time(), source, row["id"]))
            db.execute("DELETE FROM claims WHERE function_id=?", (row["id"],))

    def apply_remote_match_named(self, slug: str, name: str, agent_name: str) -> None:
        """A git project's room: who matched a function (the code itself arrives with the host's commits)."""
        proj = self._project(slug)
        aid = self._remote_agent(agent_name)
        with self.store.tx() as db:
            row = db.execute("SELECT id, status, matched_agent_id FROM functions WHERE project_id=? AND name=?",
                             (proj["id"], name)).fetchone()
            if row is None:
                return
            if row["status"] != "matched":
                db.execute("UPDATE functions SET status='matched', matched_agent_id=?, matched_at=? WHERE id=?",
                           (aid, time.time(), row["id"]))
            elif row["matched_agent_id"] is None:
                db.execute("UPDATE functions SET matched_agent_id=? WHERE id=?", (aid, row["id"]))
            db.execute("DELETE FROM claims WHERE function_id=?", (row["id"],))

    def apply_remote_note_named(self, slug: str, function: str | None, text: str, agent_name: str, created_at: float) -> None:
        proj = self._project(slug)
        row = self.store.one("SELECT id FROM functions WHERE project_id=? AND name=?", proj["id"], function) if function else None
        fid = row["id"] if row else None
        if self.store.one("SELECT 1 FROM notes WHERE project_id=? AND text=? AND function_id IS ?", proj["id"], text, fid):
            return
        aid = self._remote_agent(agent_name)
        with self.store.tx() as db:
            db.execute("INSERT INTO notes(project_id,function_id,agent_id,text,created_at) VALUES(?,?,?,?,?)",
                       (proj["id"], fid, aid, text, created_at))

    def apply_remote_best(self, slug: str, vram: int, score: int, source: str) -> None:
        proj = self._project(slug)
        with self.store.tx() as db:
            db.execute("UPDATE functions SET best_score=?, best_source=? WHERE project_id=? AND vram=? AND status='open' "
                       "AND (best_score IS NULL OR best_score > ?)", (score, source, proj["id"], vram, score))

    def apply_remote_note(self, slug: str, host_id: int, vram: int | None, text: str, agent_name: str, created_at: float) -> None:
        proj = self._project(slug)
        fid = None
        if vram is not None:
            row = self.store.one("SELECT id FROM functions WHERE project_id=? AND vram=?", proj["id"], vram)
            fid = row["id"] if row else None
        if self.store.one("SELECT 1 FROM notes WHERE project_id=? AND text=? AND function_id IS ?", proj["id"], text, fid):
            return
        aid = self._remote_agent(agent_name)
        with self.store.tx() as db:
            db.execute("INSERT INTO notes(project_id,function_id,agent_id,text,created_at) VALUES(?,?,?,?,?)",
                       (proj["id"], fid, aid, text, created_at))

    def apply_remote_context(self, slug: str, header: str, host_version: int) -> None:
        proj = self._project(slug)
        if self._must_context(proj["id"])["header"] == header:
            return
        with self.store.tx() as db:
            version = self._next_version(db, proj["id"])
            db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                       "VALUES(?,?,?,?,?,?,?)", (proj["id"], version, header, None, f"from the room (host v{host_version})",
                                                  time.time(), ""))

    def apply_remote_layout(self, slug: str, functions: list[dict], symbols: list[dict]) -> dict:
        """Mirror the room's splits and names. The host already regression-tested them on the same ROM.

        A big room sends every function and symbol on its first sync (and only what changed after it): tens of
        thousands of rows, most unchanged. The lookups below are indexed and the writes batched, so the first sync
        of a 70,000-function room lands in seconds instead of grinding through a scan per row.

        Returns what happened, for the page: splits applied, names applied or displaced, and the boundaries that
        couldn't be applied here because the two splits differ (that is work this member never gets)."""
        proj = self._project(slug)
        pid = proj["id"]
        applied = displaced = 0
        skipped = {"no_owner": 0, "matched": 0, "unsplittable": 0}
        sample: list[str] = []

        def skip(why: str, vram: int, name: str) -> None:
            skipped[why] += 1
            if len(sample) < 5:
                sample.append(f"0x{vram:08X} {name}")
        local = {r["vram"]: {"id": r["id"], "name": r["name"], "size": r["size"], "status": r["status"],
                             "unit": r["unit"], "vram": r["vram"]}
                 for r in self.store.all("SELECT id,name,size,status,unit,vram FROM functions "
                                         "WHERE project_id=? AND vram IS NOT NULL", pid)}
        starts = sorted(local)
        for f in sorted(functions, key=lambda f: f["vram"]):
            vram = int(f["vram"])
            if vram in local:
                continue
            i = bisect.bisect_right(starts, vram) - 1
            owner = local.get(starts[i]) if i >= 0 else None
            if owner is None or not (owner["vram"] < vram < owner["vram"] + owner["size"]):
                skip("no_owner", vram, f["name"])  # no function of the guest's split contains this boundary
                continue
            if owner["status"] == "matched":
                skip("matched", vram, f["name"])  # splitting it would throw the member's match away
                continue
            row = self.store.one("SELECT * FROM functions WHERE id=?", owner["id"])
            if row is None:
                skip("no_owner", vram, f["name"])
                continue
            rf = self._parse_asm(row["name"], row["asm"], row["unit"])
            try:
                head, tail = split.split_function(rf, vram, f["name"])
            except ValueError:
                skip("unsplittable", vram, f["name"])
                continue
            syms = self.symbols(pid)
            syms.add(Symbol(f["name"], vram, tail.size, "func"))
            th, tt = self._make_target(proj, head, syms), self._make_target(proj, tail, syms)
            with self.store.tx() as db:
                db.execute("UPDATE functions SET size=?, n_insns=?, target=?, relocs=?, asm=?, seed=NULL, "
                           "best_score=NULL, stuck_at=NULL WHERE id=?",
                           (head.size, len(head.lines), th.code, json.dumps(th.to_json()),
                                          self._asm_source(head), owner["id"]))
                # the head's name now covers only the head (its old best attempt was for the whole function)
                db.execute("UPDATE symbols SET size=? WHERE project_id=? AND name=?", (head.size, pid, owner["name"]))
                # the new function's id, so a later boundary that falls inside this tail splits it in turn (the
                # host's split is finer than the guest's here: several of its functions can sit in this one)
                tail_id = db.execute(
                    "INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,seed,unit,vram,rom_offset) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (pid, f["name"], tail.size, len(tail.lines), tt.code,
                                                      json.dumps(tt.to_json()), self._asm_source(tail), None,
                                                      owner["unit"], vram, tail.rom)).lastrowid
                db.execute("INSERT OR REPLACE INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)",
                           (pid, f["name"], vram, tail.size, "func"))
            owner["size"] = head.size
            local[vram] = {"id": tail_id, "name": f["name"], "size": tail.size, "status": "open",
                           "unit": owner["unit"], "vram": vram}
            bisect.insort(starts, vram)
            applied += 1
        self._xrefs.pop(pid, None)
        syms = self.symbols(pid)
        changes: list[tuple[str, dict]] = []
        for sym in symbols:
            current, off = syms.name_at(int(sym["address"]))
            if off == 0 and current == sym["name"]:
                continue
            changes.append((current if off == 0 else f"D_{int(sym['address']):08X}", sym))
        summary = {"applied": applied, "renamed": len(changes), "displaced": displaced,
                   "skipped": skipped, "sample": sample}
        if not changes:
            return summary

        def free_name(db, vram: int, taken: set[str]) -> str:
            """An address-derived name no function or symbol here has (a displaced one takes it back)."""
            base, name, n = f"func_{vram:08X}", f"func_{vram:08X}", 1
            while name in taken or db.execute("SELECT 1 FROM functions WHERE project_id=? AND name=?",
                                              (pid, name)).fetchone() is not None or db.execute(
                    "SELECT 1 FROM symbols WHERE project_id=? AND name=?", (pid, name)).fetchone() is not None:
                n += 1
                name = f"{base}_{n}"
            return name

        # The room's names can collide with this project's: a function here may already carry a name the room gives
        # another address (the two splits differed, or a name was guessed locally), and two names can swap. Free
        # every name this sync writes first, then write them all, so neither a UNIQUE constraint nor a swap loses a
        # row. A displaced function goes back to its address's name, which is what the room's copy of it will say.
        claimed = {sym["name"] for _old, sym in changes}
        with self.store.tx() as db:
            # step out of the way first: a function this sync renames may be holding another of the room's names
            for _old, sym in changes:
                db.execute("UPDATE functions SET name=char(1) || id WHERE project_id=? AND vram=?",
                           (pid, int(sym["address"])))
            for old in {old for old, _sym in changes} | claimed:
                db.execute("DELETE FROM symbols WHERE project_id=? AND name=?", (pid, old))
            for name in claimed:
                held = db.execute("SELECT id,vram,size FROM functions WHERE project_id=? AND name=?", (pid, name)).fetchall()
                for row in held:
                    fallback = free_name(db, int(row["vram"]), claimed)
                    db.execute("UPDATE functions SET name=? WHERE id=?", (fallback, row["id"]))
                    db.execute("INSERT INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)",
                               (pid, fallback, int(row["vram"]), row["size"] or 0, "func"))
                    displaced += 1
            for _old, sym in changes:
                db.execute("INSERT INTO symbols(project_id,name,address,size,kind) VALUES(?,?,?,?,?)",
                           (pid, sym["name"], int(sym["address"]), sym["size"], sym["kind"]))
                db.execute("UPDATE functions SET name=? WHERE project_id=? AND vram=?", (sym["name"], pid, int(sym["address"])))
        # Renamed symbols have to read the same in the findings already written here (matched and best sources).
        # One pass over each source, replacing every identifier the room renamed, instead of a pass per rename.
        renamed = {old: sym["name"] for old, sym in changes if old != sym["name"]}
        if renamed:
            rows = self.store.all("SELECT id, matched_source, best_source FROM functions WHERE project_id=? AND "
                                  "(matched_source IS NOT NULL OR best_source IS NOT NULL)", pid)
            updates = []
            for r in rows:
                matched = self._sub_names(r["matched_source"], renamed)
                best = self._sub_names(r["best_source"], renamed)
                if matched != r["matched_source"] or best != r["best_source"]:
                    updates.append((matched, best, r["id"]))
            if updates:
                with self.store.tx() as db:
                    db.executemany("UPDATE functions SET matched_source=?, best_source=? WHERE id=?", updates)
        self._syms.pop(pid, None)
        summary["displaced"] = displaced
        return summary

    @staticmethod
    def _sub_names(text: str | None, renamed: dict[str, str]) -> str | None:
        """`text` with every identifier the room renamed replaced (words only, so aa_b does not hit aa_bc)."""
        if not text:
            return text
        return re.sub(r"[A-Za-z_][A-Za-z0-9_]*", lambda m: renamed.get(m.group(0), m.group(0)), text)
