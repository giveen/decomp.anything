"""The host's side of a room: who is in it, what they may do, and the API they talk to.

A room is a project its owner shares. The host keeps the truth (claims, attempts, notes, the layout of what each member
split) and serves it to members over a small Starlette app of its own; a member sends what they did, the host applies it
(see roomops.py) and sends back what changed. Nothing here touches the GUI or the agents' endpoint: the room server is
its own port, reached directly or through a tunnel (collabtunnel.py)."""

from __future__ import annotations

import json
import re
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from ..network import Agent, NetworkError, _hash_token
from ..rom.symbols import AUTO_NAME
from . import forkroom
from .collabwire import LAYOUT_OVERLAP, MAX_BODY, NAME, _hash


class Room:
    """Host-side state for one shared project."""

    def __init__(self, net, slug: str):
        self.net, self.slug = net, slug
        self.proj = net.project_row(slug)
        self.fork = bool(net.fork_slug(slug))  # a git source tree (forkroom.py)
        self._hits: dict[str, deque] = defaultdict(lambda: deque(maxlen=900))
        self._members: tuple[float, list] = (0.0, [])  # cached: every member polls, the list changes slowly

    @property
    def settings(self) -> dict:
        """visibility: private (an invite link is needed) or public (the room's address is enough); approve: the
        host lets each new member in."""
        c = self.cfg.get("collab") or {}
        return {"visibility": c.get("visibility", "private"), "approve": bool(c.get("approve", False))}

    def configure(self, visibility: str | None = None, approve: bool | None = None) -> dict:
        cfg = self.cfg
        c = cfg.setdefault("collab", {})
        if visibility is not None:
            if visibility not in ("private", "public"):
                raise NetworkError("visibility is private or public")
            c["visibility"] = visibility
        if approve is not None:
            c["approve"] = bool(approve)
        with self.net.store.tx() as db:
            db.execute("UPDATE projects SET config=? WHERE slug=?", (json.dumps(cfg), self.slug))
        return self.settings

    def pending(self) -> list[dict]:
        return [dict(r) for r in self.net.store.all(
            "SELECT name, created_at FROM agents WHERE origin=? AND owner IS NULL ORDER BY created_at", self.origin + ":pending")]

    def approve(self, name: str, ok: bool = True) -> None:
        with self.net.store.tx() as db:
            row = db.execute("SELECT id FROM agents WHERE name=? AND origin=?", (name, self.origin + ":pending")).fetchone()
            if row is None:
                raise NetworkError(f"nobody called {name} is waiting")
            if ok:
                db.execute("UPDATE agents SET origin=? WHERE id=?", (self.origin, row["id"]))
                self.net._event(db, self.proj["id"], None, "collab", f"{name} joined")
            else:
                db.execute("UPDATE agents SET origin=?, token_hash=? WHERE id=?",
                           (self.origin + ":declined", "declined:" + secrets.token_hex(16), row["id"]))
        self._members = (0.0, [])

    @property
    def cfg(self) -> dict:
        return json.loads(self.net.project_row(self.slug)["config"])

    def open_invite(self, keep: bool = False, **where) -> str:
        """Open the room and return its invite code: a new one, or with `keep` the current one (the room reopening
        after the app restarted, so links people already have keep working). `where`: how it's shared
        (mode="tunnel" or mode="address" with address and port), remembered for reopening."""
        cfg = self.cfg
        old = cfg.get("collab") or {}
        code = old.get("code") if keep and old.get("code") else secrets.token_urlsafe(16)
        cfg["collab"] = {**old, **where, "invite": _hash(code), "code": code, "open": True,
                         "since": old.get("since") or time.time()}
        with self.net.store.tx() as db:
            db.execute("UPDATE projects SET config=? WHERE slug=?", (json.dumps(cfg), self.slug))
        return code

    def remove(self, name: str) -> None:
        """Take a member out of the room: their token stops working and their claims are freed."""
        with self.net.store.tx() as db:
            row = db.execute("SELECT id FROM agents WHERE name=? AND origin=? AND owner IS NULL", (name, self.origin)).fetchone()
            if row is None:
                raise NetworkError(f"{name} isn't a member of this room")
            db.execute("UPDATE agents SET token_hash=? WHERE id=?", ("removed:" + secrets.token_hex(16), row["id"]))
            db.execute("DELETE FROM claims WHERE agent_id IN (SELECT id FROM agents WHERE owner=? AND origin=?)",
                       (name, self.origin))
            db.execute("UPDATE agents SET origin=? WHERE id=?", (self.origin + ":removed", row["id"]))
            self.net._event(db, self.proj["id"], None, "collab", f"{name} was removed from the room")
        self._members = (0.0, [])

    def close(self) -> None:
        cfg = self.cfg
        if "collab" in cfg:
            cfg["collab"]["open"] = False
            with self.net.store.tx() as db:
                db.execute("UPDATE projects SET config=? WHERE slug=?", (json.dumps(cfg), self.slug))

    def invite_ok(self, code: str) -> bool:
        c = self.cfg.get("collab") or {}
        if not c.get("open"):
            return False
        return c.get("visibility") == "public" or secrets.compare_digest(c.get("invite", ""), _hash(code or ""))

    @property
    def origin(self) -> str:
        return f"collab:{self.slug}"

    def members(self) -> list[dict]:
        at, cached = self._members
        if time.time() - at < 3:
            return cached
        out = self._members_now()
        self._members = (time.time(), out)
        return out

    def _members_now(self) -> list[dict]:
        rows = self.net.store.all("SELECT id, name, created_at, last_seen FROM agents WHERE origin=? AND owner IS NULL",
                                  self.origin)
        out = []
        for r in rows:
            agents = self.net.store.all("SELECT id, name FROM agents WHERE origin=? AND owner=?", self.origin, r["name"])
            ids = [a["id"] for a in agents] or [-1]
            matched = self.net.store.one(
                f"SELECT COUNT(*) AS n FROM functions WHERE matched_agent_id IN ({','.join('?' * len(ids))})", *ids)["n"]
            working = [c["name"] for c in self.net.store.all(
                f"SELECT f.name FROM claims c JOIN functions f ON f.id=c.function_id WHERE c.agent_id IN "
                f"({','.join('?' * len(ids))})", *ids)]
            out.append({"name": r["name"], "joined": r["created_at"], "last_seen": r["last_seen"],
                        "online": bool(r["last_seen"]) and time.time() - r["last_seen"] < 30,
                        "agents": [a["name"].split(".", 1)[1] for a in agents], "matched": matched, "working": working})
        return out

    def join(self, name: str, sha1: str) -> tuple[str, str]:
        """New member: (token, the name they got; a taken name gets a number)."""
        if not NAME.match(name or ""):
            raise NetworkError("pick a name of up to 24 letters, digits, - or _")
        cfg = self.cfg
        want = forkroom.project_identity(cfg)
        if want and sha1 != want:
            what = (f"{cfg['rom']['title']}, {cfg['rom']['game_code']}" if not self.fork
                    else f"{self.proj['name']}, {cfg.get('version') or 'the same version'}")
            raise NetworkError("that isn't the same game: everyone in a room needs their own copy of exactly this "
                               f"game ({what}, same version)")
        base, n = name, 2
        while self.net.store.one("SELECT 1 FROM agents WHERE lower(name)=lower(?)", name):
            name, n = f"{base}{n}", n + 1
        token = "dn_" + secrets.token_urlsafe(24)
        waiting = self.settings["approve"]
        with self.net.store.tx() as db:
            db.execute("INSERT INTO agents(name,owner,token_hash,created_at,trusted,origin) VALUES(?,?,?,?,1,?)",
                       (name, None, _hash_token(token), time.time(), self.origin + (":pending" if waiting else "")))
            if waiting:
                self.net._event(db, self.proj["id"], None, "collab", f"{name} asks to join")
            else:
                self.net._event(db, self.proj["id"], None, "collab", f"{name} joined")
        self._members = (0.0, [])
        return token, name

    def member(self, request: Request) -> Agent:
        token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        agent = self.net.authenticate(token or None)
        row = self.net.store.one("SELECT origin, owner FROM agents WHERE id=?", agent.id)
        if row["origin"] == self.origin + ":pending":
            raise NetworkError("waiting for the host to let you in")
        if row["origin"] != self.origin or row["owner"] is not None:
            raise NetworkError("not a member of this room")
        hits = self._hits[agent.name]
        now = time.time()
        hits.append(now)
        if len(hits) == hits.maxlen and now - hits[0] < 60:
            raise NetworkError("slow down")
        return agent

    def agent_for(self, member: Agent, label: str) -> Agent:
        """The host-side identity of one of a member's agents, e.g. alex.claude-1."""
        label = re.sub(r"[^A-Za-z0-9_-]", "", str(label or "agent"))[:24] or "agent"
        name = f"{member.name}.{label}"
        row = self.net.store.one("SELECT id FROM agents WHERE name=?", name)
        if row is None:
            with self.net.store.tx() as db:
                aid = db.execute("INSERT INTO agents(name,owner,token_hash,created_at,trusted,origin) VALUES(?,?,?,?,1,?)",
                                 (name, member.name, "remote:" + secrets.token_hex(16), time.time(), self.origin)).lastrowid
            return Agent(aid, name, member.name, True)
        return Agent(row["id"], name, member.name, True)

    def state(self, e: int, a: int, ctx_version: int, member: Agent, layout_at: float = 0.0) -> dict:
        """What changed since the guest's cursors: work product only, never ROM-derived data."""
        if self.fork:
            return self._fork_state(e, member)
        pid = self.proj["id"]
        net = self.net
        with net.store.tx() as db:
            net._expire_claims(db)
        events = net.store.all("SELECT e.id, e.kind, e.detail, e.created_at, a.name AS agent FROM events e LEFT JOIN agents a "
                               "ON a.id=e.agent_id WHERE e.project_id=? AND e.id>? ORDER BY e.id LIMIT 500", pid, e)
        max_e = events[-1]["id"] if events else e
        kinds = {ev["kind"] for ev in events}
        names = {ev["detail"] for ev in events if ev["kind"] == "match"}
        out: dict = {"e": max_e, "events": [dict(ev) for ev in events]}
        first = e == 0
        if first or names:
            rows = net.store.all("SELECT f.name, f.vram, f.matched_source, a.name AS agent FROM functions f LEFT JOIN agents a "
                                 "ON a.id=f.matched_agent_id WHERE f.project_id=? AND f.status='matched'", pid)
            out["matched"] = [dict(r) for r in rows if first or r["name"] in names]
        best = net.store.all("SELECT f.name, f.vram, f.best_score, f.best_source, MAX(t.id) AS last FROM attempts t "
                             "JOIN functions f ON f.id=t.function_id WHERE f.project_id=? AND t.id>? AND f.status='open' "
                             "AND f.best_source IS NOT NULL GROUP BY f.id", pid, a)
        out["best"] = [{"name": r["name"], "vram": r["vram"], "score": r["best_score"], "source": r["best_source"]} for r in best]
        last = net.store.one("SELECT MAX(t.id) AS m FROM attempts t JOIN functions f ON f.id=t.function_id WHERE f.project_id=?", pid)
        out["a"] = max(a, last["m"] or 0)
        # The split and its names: everything on a member's first sync, then only the rows that changed since it
        # last saw them. A rename used to re-send every function and symbol of the room for one new name (17 MB in
        # a 70,000-function room), which is what made a busy room feel like it was never synced.
        cutoff = time.time()
        if layout_at <= 0:
            out["symbols"] = [dict(r) for r in net.store.all(
                "SELECT name, address, size, kind FROM symbols WHERE project_id=?", pid) if not AUTO_NAME.match(r["name"])]
            out["functions"] = [dict(r) for r in net.store.all(
                "SELECT name, vram, size FROM functions WHERE project_id=? ORDER BY vram", pid)]
        else:
            since = layout_at - LAYOUT_OVERLAP
            changed_functions = net.store.all(
                "SELECT name, vram, size FROM functions WHERE project_id=? AND changed_at>? ORDER BY vram", pid, since)
            changed_symbols = net.store.all(
                "SELECT name, address, size, kind FROM symbols WHERE project_id=? AND changed_at>? ORDER BY name",
                pid, since)
            if changed_functions:
                out["functions"] = [dict(r) for r in changed_functions]
            if changed_symbols:
                out["symbols"] = [dict(r) for r in changed_symbols]
        out["l"] = cutoff
        ctx = net._must_context(pid)
        if ctx["version"] != ctx_version:
            out["context"] = {"version": ctx["version"], "header": ctx["header"]}
        if first or kinds & {"note"}:
            out["notes"] = [dict(r) for r in net.store.all(
                "SELECT n.id, n.text, n.created_at, f.vram, a.name AS agent FROM notes n LEFT JOIN functions f ON f.id=n.function_id "
                "LEFT JOIN agents a ON a.id=n.agent_id WHERE n.project_id=? ORDER BY n.id", pid)]
        out["claims"] = [dict(r) for r in net.store.all(
            "SELECT f.name, f.vram, a.name AS agent FROM claims c JOIN functions f ON f.id=c.function_id JOIN agents a "
            "ON a.id=c.agent_id WHERE f.project_id=?", pid)]
        out["members"] = self.members()
        return out

    def _fork_state(self, e: int, member: Agent) -> dict:
        """A git project's room: events, matches (by name), claims and notes; the tree itself travels as bundles
        (GET /collab/bundle) when `head` moves."""
        pid, net = self.proj["id"], self.net
        with net.store.tx() as db:
            net._expire_claims(db)
        events = net.store.all("SELECT e.id, e.kind, e.detail, e.created_at, a.name AS agent FROM events e LEFT JOIN agents a "
                               "ON a.id=e.agent_id WHERE e.project_id=? AND e.id>? ORDER BY e.id LIMIT 500", pid, e)
        out: dict = {"e": events[-1]["id"] if events else e, "events": [dict(ev) for ev in events]}
        names = {ev["detail"] for ev in events if ev["kind"] == "match"}
        if e == 0 or names:
            rows = net.store.all("SELECT f.name, a.name AS agent FROM functions f LEFT JOIN agents a ON a.id=f.matched_agent_id "
                                 "WHERE f.project_id=? AND f.status='matched'", pid)
            out["matched"] = [dict(r) for r in rows if e == 0 or r["name"] in names]
        if e == 0 or any(ev["kind"] == "note" for ev in events):
            out["notes"] = [dict(r) for r in net.store.all(
                "SELECT n.id, n.text, n.created_at, f.name AS function, a.name AS agent FROM notes n LEFT JOIN functions f "
                "ON f.id=n.function_id LEFT JOIN agents a ON a.id=n.agent_id WHERE n.project_id=? ORDER BY n.id", pid)]
        out["claims"] = [dict(r) for r in net.store.all(
            "SELECT f.name, f.vram, a.name AS agent FROM claims c JOIN functions f ON f.id=c.function_id JOIN agents a "
            "ON a.id=c.agent_id WHERE f.project_id=?", pid)]
        out["head"] = forkroom.head(Path(self.proj["root"]))
        out["branch"] = forkroom.branch(Path(self.proj["root"]))
        out["members"] = self.members()
        return out

def host_app(net, rooms: dict[str, Room]) -> Starlette:
    """The public side of a shared project, served through the tunnel."""

    def room_for(request: Request) -> Room:
        slug = request.query_params.get("room") or request.headers.get("x-room", "")
        if not slug and len(rooms) == 1:  # a public room's bare address
            slug = next(iter(rooms))
        room = rooms.get(slug)
        if room is None:
            raise NetworkError("this room is closed")
        return room

    async def body(request: Request) -> dict:
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            raise NetworkError("request too large")
        try:
            data = await request.json()
        except ValueError:
            raise NetworkError("invalid request") from None
        if not isinstance(data, dict):
            raise NetworkError("invalid request")
        return data

    def endpoint(fn, auth: bool = True):
        async def handler(request: Request):
            import anyio

            try:
                room = room_for(request)
                data = await body(request) if request.method == "POST" else dict(request.query_params)
                member = await anyio.to_thread.run_sync(room.member, request) if auth else None
                result = await anyio.to_thread.run_sync(lambda: fn(room, member, data))
                return JSONResponse(result)
            except NetworkError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
        return handler

    def hello(room, member, q):
        if not room.invite_ok(q.get("invite", "")):
            raise NetworkError("this invite link is no longer valid")
        cfg = room.cfg
        ov = net.overview(room.slug)
        if room.fork:
            return {"room": room.slug, **forkroom.hello(net, room.slug), "functions": ov["functions"],
                    "matched": ov["matched"], "members": len(room.members()), **room.settings}
        return {"room": room.slug, "kind": "rom", "title": cfg["rom"]["title"], "game_code": cfg["rom"]["game_code"],
                "region": cfg["rom"]["region"], "sha1": cfg["rom"]["sha1"], "compiler": cfg["compiler"],
                "functions": ov["functions"], "matched": ov["matched"], "members": len(room.members()),
                **room.settings}

    def join(room, member, d):
        if not room.invite_ok(str(d.get("invite", ""))):
            raise NetworkError("this invite link is no longer valid")
        token, name = room.join(str(d.get("name", "")), str(d.get("sha1", "")))
        return {"token": token, "room": room.slug, "name": name, "waiting": room.settings["approve"]}

    def whoami(room, member, q):
        return {"name": member.name, "room": room.slug}

    def state(room, member, q):
        try:
            layout_at = float(q.get("l") or 0)
        except ValueError:
            layout_at = 0.0
        return room.state(int(q.get("e") or 0), int(q.get("a") or 0), int(q.get("v") or 0), member, layout_at)

    def claim(room, member, d):
        agent = room.agent_for(member, d.get("agent"))
        fn = net.claim_row(agent, room.slug, d.get("function") or None, str(d.get("strategy") or "easiest"))
        return {"function": fn["name"] if fn else None, "vram": fn["vram"] if fn else None}

    def attempt(room, member, d):
        agent = room.agent_for(member, d.get("agent"))
        res = net.attempt(agent, str(d.get("function", "")), str(d.get("source", "")), room.slug)
        return {"score": res["score"], "matched": res["matched"]}

    def best(room, member, d):
        """A member's attempt got better: kept as the function's best so far, as reported (no compile here: the host
        only spends its CPU on verifying matches)."""
        agent = room.agent_for(member, d.get("agent"))
        net.apply_reported_best(room.slug, str(d.get("function", "")), int(d.get("score", 0)), str(d.get("source", ""))[:65536], agent)
        return {"ok": True}

    def release(room, member, d):
        agent = room.agent_for(member, d.get("agent"))
        return {"text": net.release(agent, str(d.get("function", "")), str(d.get("notes", "")), room.slug)}

    def context(room, member, d):
        agent = room.agent_for(member, d.get("agent"))
        return {"text": net.edit_context(agent, room.slug, str(d.get("old", "")), str(d.get("new", "")),
                                         str(d.get("message", "")), d.get("function") or None)}

    def rename(room, member, d):
        agent = room.agent_for(member, d.get("agent"))
        return {"text": net.rename_symbol(agent, room.slug, str(d.get("old", "")), str(d.get("new", "")),
                                          str(d.get("message", "")))}

    def split(room, member, d):
        agent = room.agent_for(member, d.get("agent"))
        return {"text": net.split_function(agent, room.slug, str(d.get("function", "")), str(d.get("at", "")),
                                           str(d.get("new_name", "")))}

    def note(room, member, d):
        agent = room.agent_for(member, d.get("agent"))
        return {"text": net.add_note(agent, room.slug, str(d.get("text", "")), d.get("function") or None)}

    def edit(room, member, d):
        """A member's edit of a git project's file (a header, a new or shared source file): made and checked here."""
        if not room.fork:
            raise NetworkError("this room's project has no files to edit")
        agent = room.agent_for(member, d.get("agent"))
        return {"text": net.forks.edit_file(agent, room.slug, str(d.get("path", "")), str(d.get("old", "")),
                                            str(d.get("new", "")), str(d.get("message", "")))}

    def split_file(room, member, d):
        if not room.fork:
            raise NetworkError("this room's project has no files to split")
        agent = room.agent_for(member, d.get("agent"))
        return {"text": net.forks.split_file(agent, room.slug, str(d.get("path", "")), str(d.get("at", "")),
                                             str(d.get("new_name", "")), str(d.get("data", "")))}

    async def get_bundle(request: Request):
        """The host's commits a member doesn't have yet (a git bundle: tracked files only, never the game)."""
        import anyio

        try:
            room = room_for(request)
            await anyio.to_thread.run_sync(room.member, request)
            if not room.fork:
                raise NetworkError("this room's project isn't a git tree")
            data = await anyio.to_thread.run_sync(lambda: forkroom.bundle(Path(room.proj["root"]),
                                                                           request.query_params.get("have", "")))
        except NetworkError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return Response(data, media_type="application/octet-stream", status_code=200 if data else 204)

    def leave(room, member, d):
        with net.store.tx() as db:
            db.execute("DELETE FROM claims WHERE agent_id IN (SELECT id FROM agents WHERE owner=? AND origin=?)",
                       (member.name, room.origin))
        return {"ok": True}

    async def landing(request: Request):
        return HTMLResponse("<!doctype html><meta charset=utf-8><title>decomp.wtf room</title>"
                            "<body style='font:16px system-ui;max-width:36em;margin:4rem auto;padding:0 1rem'>"
                            "<h1>decomp.wtf room</h1><p>This is an invite to decompile a game together. Open "
                            "<b>decomp.wtf</b>, choose <b>Join a friend</b>, and paste this page's address. "
                            "You'll need your own copy of the game.</p><p>Don't have the app? "
                            "<a href='https://decomp.wtf'>decomp.wtf</a></p>")

    routes = [
        Route("/", landing), Route("/join/{code}", landing),
        Route("/collab/hello", endpoint(hello, auth=False)),
        Route("/collab/join", endpoint(join, auth=False), methods=["POST"]),
        Route("/collab/state", endpoint(state)),
        Route("/collab/whoami", endpoint(whoami)),
        Route("/collab/claim", endpoint(claim), methods=["POST"]),
        Route("/collab/attempt", endpoint(attempt), methods=["POST"]),
        Route("/collab/best", endpoint(best), methods=["POST"]),
        Route("/collab/release", endpoint(release), methods=["POST"]),
        Route("/collab/context", endpoint(context), methods=["POST"]),
        Route("/collab/rename", endpoint(rename), methods=["POST"]),
        Route("/collab/split", endpoint(split), methods=["POST"]),
        Route("/collab/note", endpoint(note), methods=["POST"]),
        Route("/collab/leave", endpoint(leave), methods=["POST"]),
        Route("/collab/edit", endpoint(edit), methods=["POST"]),
        Route("/collab/split_file", endpoint(split_file), methods=["POST"]),
        Route("/collab/bundle", get_bundle),
    ]
    return Starlette(routes=routes)

