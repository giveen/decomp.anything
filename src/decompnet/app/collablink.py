"""A member's side of a room: join, then keep the two copies in step.

A member works on their own project; the link sends what they claim, attempt, match and note to the host, pulls what
everyone else did, and applies it (see roomops.py). A host that is away is waited out, not treated as an error: the
member's agents keep their place and their work is sent when it comes back."""

from __future__ import annotations

import contextlib
import json
import re
import shutil
import threading
import time
from pathlib import Path

import httpx

from ..network import Agent, NetworkError
from . import forkroom
from .collabtunnel import parse_invite
from .collabwire import BACKOFF_MAX, SYNC_EVERY


class Link:
    """The guest's connection to a host's room."""

    def __init__(self, net, slug: str, url: str, room: str, token: str, member: str, client: httpx.Client | None = None):
        self.net, self.slug, self.url, self.room, self.token, self.member = net, slug, url.rstrip("/"), room, token, member
        self.client = client or httpx.Client(timeout=httpx.Timeout(60, connect=15))
        cfg = json.loads(net.project_row(slug)["config"])
        info = cfg.get("link") if isinstance(cfg.get("link"), dict) else {}
        saved = info.get("cursor") if isinstance(info.get("cursor"), dict) else {}
        # where this member got to last time: restarting the app then syncs only what it missed, not the whole room
        self.cursor = {"e": int(saved.get("e") or 0), "a": int(saved.get("a") or 0),
                       "v": int(saved.get("v") or 0), "l": float(saved.get("l") or 0.0)}
        self.claims: list[dict] = []
        self.members: list[dict] = []
        self.error = ""
        self.last_ok = 0.0
        self.syncing = False  # a sync is being applied: the room answered, so it isn't offline
        self.sync_ms = 0
        self.failures = 0     # syncs in a row that failed (the page counts down to the next try)
        self.last_try = 0.0
        self.next_try = 0.0
        self.layout: dict | None = None  # what the last layout apply could and couldn't do here (see engine)
        self._saved_cursor = 0.0
        self._last_saved = dict(self.cursor)  # what the project has: an idle room writes nothing
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.fork = bool(net.fork_slug(slug))  # a git source tree: follows the host's commits (forkroom.py)
        self.head = info.get("head", "")

    # -- transport

    def _req(self, method: str, path: str, data: dict | None = None) -> dict:
        headers = {"authorization": f"Bearer {self.token}", "x-room": self.room}
        try:
            if method == "GET":
                r = self.client.get(f"{self.url}{path}", params={**(data or {}), "room": self.room}, headers=headers)
            else:
                r = self.client.post(f"{self.url}{path}?room={self.room}", json=data or {}, headers=headers)
        except httpx.HTTPError as e:
            self.error = f"can't reach the host ({type(e).__name__})"
            raise NetworkError(f"the room's host isn't reachable right now: {self.error}") from None
        out = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200:
            self.error = out.get("error") or f"host answered {r.status_code}"
            raise NetworkError(self.error)
        self.error, self.last_ok = "", time.time()
        return out

    def _raw(self, path: str, params: dict) -> bytes:
        headers = {"authorization": f"Bearer {self.token}", "x-room": self.room}
        try:
            r = self.client.get(f"{self.url}{path}", params={**params, "room": self.room}, headers=headers,
                                timeout=httpx.Timeout(600, connect=15))
        except httpx.HTTPError as e:
            self.error = f"can't reach the host ({type(e).__name__})"
            raise NetworkError(f"the room's host isn't reachable right now: {self.error}") from None
        if r.status_code == 204:
            return b""
        if r.status_code != 200:
            raise NetworkError((r.json() if r.headers.get("content-type", "").startswith("application/json")
                                else {}).get("error") or f"host answered {r.status_code}")
        self.error, self.last_ok = "", time.time()
        return r.content

    # -- actions, called by RomNetwork for a linked project

    def claim(self, agent: Agent, function: str | None, strategy: str) -> str | None:
        return self._req("POST", "/collab/claim", {"agent": agent.name, "function": function, "strategy": strategy})["function"]

    def attempt(self, agent: Agent, function: str, source: str) -> dict:
        return self._req("POST", "/collab/attempt", {"agent": agent.name, "function": function, "source": source})

    def call(self, route: str, agent: Agent, /, **data) -> str:
        return self._req("POST", route, {"agent": agent.name, **data}).get("text", "")

    # -- mirroring the host's work into the local project

    def sync(self) -> None:
        with self._lock:
            self.syncing = True
            try:
                st = self._req("GET", "/collab/state", self.cursor)
                # the room answered: applying the work (a big room's first sync) is not a lost connection
                self.last_ok = time.time()
                self.apply(st)
                with contextlib.suppress(Exception):  # the work is applied; an unsaved cursor only costs a re-sync
                    self._save_cursor()
            finally:
                self.syncing = False

    def _save_cursor(self, force: bool = False) -> None:
        """Remember where this member got to (throttled: a sync every 2 s shouldn't write the project each time)."""
        now = time.time()
        if self.cursor == self._last_saved:
            return
        if not force and now - self._saved_cursor < 10:
            return
        self._saved_cursor = now

        def change(cfg):  # one transaction, so a rename or an export saving the config at the same time isn't lost
            info = cfg.get("link") if isinstance(cfg.get("link"), dict) else {}
            info["cursor"] = dict(self.cursor)
            cfg["link"] = info
        self.net._update_cfg(self.slug, change)
        self._last_saved = dict(self.cursor)

    def apply(self, st: dict) -> None:
        if self.fork:
            return self._apply_fork(st)
        net, slug = self.net, self.slug
        proj = net.project_row(slug)
        pid = proj["id"]
        mine = f"{self.member}."
        if st.get("symbols") is not None or st.get("functions") is not None:
            report = net.apply_remote_layout(slug, st.get("functions") or [], st.get("symbols") or [])
            self.layout = self._merge_layout(self.layout, report)
        if st.get("context"):
            net.apply_remote_context(slug, st["context"]["header"], st["context"]["version"])
            self.cursor["v"] = st["context"]["version"]
        for m in st.get("matched") or []:
            net.apply_remote_match(slug, m["vram"], m["matched_source"], m["agent"] or "host")
        for b in st.get("best") or []:
            net.apply_remote_best(slug, b["vram"], b["score"], b["source"])
        for n in st.get("notes") or []:
            net.apply_remote_note(slug, n["id"], n["vram"], n["text"], n["agent"] or "host", n["created_at"])
        for ev in st.get("events") or []:
            kinds = ("match", "claim", "release", "context", "rename", "split", "collab")
            if ev["kind"] in kinds and not (ev["agent"] or "").startswith(mine):
                net.remote_event(pid, ev["kind"], ev["detail"], ev["agent"] or "host", ev["created_at"])
        self.claims = [c for c in st.get("claims") or [] if not (c["agent"] or "").startswith(mine)]
        self.members = st.get("members") or []
        self.cursor["e"], self.cursor["a"] = st.get("e", self.cursor["e"]), st.get("a", self.cursor["a"])
        self.cursor["l"] = float(st.get("l") or self.cursor["l"])

    @staticmethod
    def _merge_layout(old: dict | None, new: dict) -> dict:
        """What to show in the page. A delta that applied nothing new doesn't prove an earlier gap is gone, so the
        earlier gaps stay until the room sends another split (the splits only ever move closer to each other)."""
        if old and not sum(new.get("skipped", {}).values()):
            return old
        return new

    def _apply_fork(self, st: dict) -> None:
        """A git project's room: follow the host's commits, then mirror matches, notes, events, claims."""
        net, slug = self.net, self.slug
        pid = net.project_row(slug)["id"]
        mine = f"{self.member}."
        if st.get("head") and st["head"] != self.head:
            self.pull(st.get("branch") or "")
        for m in st.get("matched") or []:
            net.apply_remote_match_named(slug, m["name"], m["agent"] or "host")
        for n in st.get("notes") or []:
            net.apply_remote_note_named(slug, n["function"], n["text"], n["agent"] or "host", n["created_at"])
        for ev in st.get("events") or []:
            if ev["kind"] in ("match", "claim", "release", "context", "split", "collab", "link") \
                    and not (ev["agent"] or "").startswith(mine):
                net.remote_event(pid, ev["kind"], ev["detail"], ev["agent"] or "host", ev["created_at"])
        self.claims = [c for c in st.get("claims") or [] if not (c["agent"] or "").startswith(mine)]
        self.members = st.get("members") or []
        self.cursor["e"] = st.get("e", self.cursor["e"])

    def pull(self, ref: str) -> bool:
        """Bring the host's new commits into this tree (while no agent here is building in it), rebuild what
        changed and measure again. Returns whether anything came."""
        forks = self.net.forks
        proj, fp, _ = forks.get(self.slug)
        data = self._raw("/collab/bundle", {"have": self.head})
        if data:
            with forks._tree(proj).exclusive():
                tip = forkroom.pull(fp.root, data, ref or forkroom.branch(fp.root))
                forks._build_all(proj, fp)
        else:
            tip = self._req("GET", "/collab/state", {"e": self.cursor["e"]}).get("head") or self.head
        self.head = tip
        forkroom.save_head(self.net, self.slug, tip)
        if data:
            forks.sync(self.slug, log=lambda *_: None)
        return bool(data)

    def run(self, every: float = SYNC_EVERY) -> None:
        def loop():
            while not self._stop.is_set():
                self._stop.wait(self._tick(every))
        threading.Thread(target=loop, daemon=True, name=f"link-{self.slug}").start()

    def _tick(self, every: float = SYNC_EVERY) -> float:
        """One sync attempt: record how long it took and how it went, and return the wait before the next one.

        A room that keeps failing is retried less and less often (up to a minute) instead of every two seconds, and
        the page counts down to the next try with the reason the last one failed."""
        started = time.time()
        try:
            self.sync()
            self.failures = 0
            wait = every
        except NetworkError as e:
            self.failures += 1
            self.error = self.error or str(e)
            wait = min(BACKOFF_MAX, every * 2 ** min(self.failures, 5))
        except Exception as e:  # noqa: BLE001 keep syncing; show the problem in the GUI
            self.failures += 1
            self.error = f"{type(e).__name__}: {e}"
            wait = min(BACKOFF_MAX, every * 2 ** min(self.failures, 5))
        self.sync_ms = int((time.time() - started) * 1000)
        self.last_try = time.time()
        self.next_try = self.last_try + wait
        return wait

    def stop(self) -> None:
        self._stop.set()
        with contextlib.suppress(Exception):  # a project that is already gone has nowhere to save to
            self._save_cursor(force=True)

def _hello(http: httpx.Client, link: str) -> tuple[str, str, dict]:
    url, code = parse_invite(link)
    code = code.split("#", 1)[0]
    room_hint = link.split("#room=", 1)[1] if "#room=" in link else ""
    try:
        r = http.get(f"{url}/collab/hello", params={"invite": code, "room": room_hint})
    except httpx.HTTPError:
        raise NetworkError("can't reach that room: is the host's decomp.wtf open and sharing?") from None
    hello = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200:
        raise NetworkError(hello.get("error", "the invite didn't work"))
    return url, code, hello

def _hosting(net, room: str, sha1: str | None) -> str | None:
    """The project on this computer that is a room host for the game being joined, if any: either the room in the
    link (same slug), or the same game's project shared as a host. Joining with it would make the host project a
    member of its own room, and the room would show as offline (and stop being hosted) from then on."""
    if room in net.rooms:
        return room
    if not sha1:
        return None
    for p in net.store.all("SELECT slug, config FROM projects"):
        cfg = json.loads(p["config"])
        if (cfg.get("collab") or {}).get("open") and forkroom.project_identity(cfg) == sha1:
            return p["slug"]
    return None

def _linked_project(net, sha1: str | None, room: str):
    for p in net.store.all("SELECT slug, config FROM projects"):
        cfg = json.loads(p["config"])
        if forkroom.project_identity(cfg) == sha1 and (cfg.get("link") or {}).get("room") == room:
            return p["slug"], cfg["link"]
    return None, None

def reconnect(net, slug: str, link: str, log=print, client: httpx.Client | None = None, start: bool = True) -> str:
    """Guest: the host restarted and shared a new link. Reconnect this project as the same member if the host still
    knows us, or join again with the invite."""
    http = client or httpx.Client(timeout=httpx.Timeout(60, connect=15))
    url, code, hello = _hello(http, link)
    proj = net.project_row(slug)
    cfg = json.loads(proj["config"])
    if (cfg.get("collab") or {}).get("open"):
        raise NetworkError(f"{slug} is set up to host a room: stop sharing it before joining another room, so the "
                           "two rooms don't mix.")
    if forkroom.project_identity(cfg) != hello.get("sha1"):
        raise NetworkError(f"that link is for {hello['title']}, a different game than this project")
    info = dict(cfg.get("link") or {})
    if info.get("token"):
        r = http.get(f"{url}/collab/whoami", params={"room": hello["room"]},
                     headers={"authorization": f"Bearer {info['token']}", "x-room": hello["room"]})
        if r.status_code == 200:
            old = net.links.pop(slug, None)
            if old is not None:
                old.stop()
            net.link_project(slug, info | {"url": url, "room": hello["room"], "member": r.json()["name"]},
                             client=client, start=start)
            log(f"reconnected to {hello['title']} as {r.json()['name']}")
            return slug
    name = info.get("member") or "friend"
    r = http.post(f"{url}/collab/join?room={hello['room']}", json={"invite": code, "name": name, "sha1": hello["sha1"]})
    joined = r.json()
    if r.status_code != 200:
        raise NetworkError(joined.get("error", "couldn't join"))
    old = net.links.pop(slug, None)
    if old is not None:
        old.stop()
    net.link_project(slug, {"url": url, "room": hello["room"], "token": joined["token"], "member": joined["name"],
                            "host_title": hello["title"], "head": info.get("head", "")}, client=client, start=start)
    log(f"joined {hello['title']} again as {joined['name']}")
    return slug

def join_room(net, link: str, name: str, rom: bytes | Path, log=print, client: httpx.Client | None = None,
              start: bool = True) -> str:
    """Guest: check the invite, check the ROM is the same game, join, import it locally, link. Returns the slug.
    If this game is already linked to that room here, reconnect instead (no ROM needed). `rom`: the game's bytes or
    the file (a GameCube or Wii disc is gigabytes: never read whole)."""
    from ..rom import detect

    http = client or httpx.Client(timeout=httpx.Timeout(60, connect=15))
    url, code, hello = _hello(http, link)
    hosted = _hosting(net, hello["room"], hello.get("sha1"))
    if hosted:
        raise NetworkError("this computer is hosting a room for this game: a host can't join a room for it from the "
                           "same project. Send the invite to a friend, test it from another computer, or stop sharing "
                           "this room before joining another.")
    slug, _info = _linked_project(net, hello["sha1"], hello["room"])
    if slug:
        return reconnect(net, slug, link, log=log, client=client, start=start)
    if not rom:
        raise NetworkError("pick your own copy of the game to join")
    if hello.get("kind") == "fork":
        return _join_fork(net, http, url, code, hello, name, rom, log, client, start)
    if isinstance(rom, Path):
        rom = rom.read_bytes()
    picked = detect.identify(rom)
    if picked.info["sha1"] != hello["sha1"]:
        raise NetworkError(f"this ROM isn't the same as the host's: the room is for {hello['title']} "
                           f"({hello['game_code']}, {hello['region']}), and every member needs their own copy of exactly that version")
    log(f"joining {hello['title']} ({hello['functions']} functions, {hello['matched']} matched)")
    r = http.post(f"{url}/collab/join?room={hello['room']}", json={"invite": code, "name": name, "sha1": hello["sha1"]})
    joined = r.json()
    if r.status_code != 200:
        raise NetworkError(joined.get("error", "couldn't join"))
    slug = net.import_rom(rom, toolchain=hello["compiler"]["toolchain"], flags=hello["compiler"]["flags"],
                          confirmed=True, log=log)
    net.link_project(slug, {"url": url, "room": hello["room"], "token": joined["token"], "member": joined["name"],
                            "host_title": hello["title"]}, client=client, start=start)
    log(f"linked as {joined['name']}: your agents now work with the room")
    return slug

def _join_fork(net, http: httpx.Client, url: str, code: str, hello: dict, name: str, game: bytes | Path, log,
               client: httpx.Client | None, start: bool) -> str:
    """Join a git project's room: the host's tree (forkroom.clone), the member's own copy of the game placed where
    the project wants it and built with the member's own tools, then linked."""
    import tempfile

    from ..forks.catalog import PLATFORMS

    tmp = None
    if not isinstance(game, Path):
        with tempfile.NamedTemporaryFile(prefix="decomp-join-", suffix=".game", delete=False) as fh:
            fh.write(game)  # closed here; delete=False keeps the bytes for the join, the cleanup unlinks it
        tmp = fh.name
        game = Path(tmp)
    try:
        if hello.get("sha1") and forkroom.game_identity(game) != hello["sha1"]:
            raise NetworkError(f"this isn't the same game as the host's: the room is for {hello['title']} "
                               f"({PLATFORMS.get(hello.get('platform'), hello.get('platform') or '')}, "
                               f"{hello.get('version') or 'the same version'}), and every member needs their own copy of "
                               "exactly that version")
        log(f"joining {hello['title']} ({hello['functions']} functions, {hello['matched']} matched)")
        r = http.post(f"{url}/collab/join?room={hello['room']}", json={"invite": code, "name": name, "sha1": hello.get("sha1") or ""})
        joined = r.json()
        if r.status_code != 200:
            raise NetworkError(joined.get("error", "couldn't join"))
        headers = {"authorization": f"Bearer {joined['token']}", "x-room": hello["room"]}

        def get_bundle(have: str) -> bytes:
            res = http.get(f"{url}/collab/bundle", params={"have": have, "room": hello["room"]}, headers=headers,
                           timeout=httpx.Timeout(600, connect=15))
            if res.status_code == 204:
                return b""
            if res.status_code != 200:
                raise NetworkError((res.json() if res.headers.get("content-type", "").startswith("application/json")
                                    else {}).get("error") or "the host couldn't send the project")
            return res.content

        slug = net.forks._free_slug(re.sub(r"[^a-z0-9]+", "-", hello["title"].lower()).strip("-")[:40] or "room")
        dest = net.workspace / slug
        try:
            forkroom.clone(dest, hello, get_bundle, log=log)
            forkroom.register_member(net, slug, dest, hello, hello["title"])
        except BaseException:
            shutil.rmtree(dest, ignore_errors=True)
            raise
        info = {"url": url, "room": hello["room"], "token": joined["token"], "member": joined["name"],
                "host_title": hello["title"], "head": forkroom.head(dest)}
        net.link_project(slug, info, client=client, start=False)  # its agents wait for the room from the start
        log("building the project with your copy of the game and your tools")
        net.forks.setup(slug, game, game.name if tmp is None else "", log=log, move=tmp is not None)
        if start:
            net.links[slug].run()
        log(f"linked as {joined['name']}: your agents now work with the room")
        return slug
    finally:
        if tmp is not None:
            Path(tmp).unlink(missing_ok=True)

