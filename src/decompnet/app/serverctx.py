"""The local server's plumbing: what every route group shares.

The page and its API are registered by several modules (routes_pages, routes_projects, ...). They all need the same
things: the engine, the agent manager, the settings, the background jobs, the RE console, and the helpers every route
starts with (guard the request, read its body, run the slow part off the event loop, turn an error into JSON). Those live
here, so a route group imports them instead of closing over build().

Jobs and Settings are the app's own files (logs/decomp.log, settings.json); the GUI and the tests read them here.
"""

from __future__ import annotations

import contextlib
import json
import logging
import logging.handlers
import os
import secrets
import subprocess
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from importlib import resources
from pathlib import Path

import anyio
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse

from .. import hostenv, rea, toolchains
from ..network import NetworkError
from ..rom.n64 import RomError
from . import agents, updates

COOKIE = "decomp_live"
MAX_DISC = 16 * 1024 * 1024 * 1024  # dual-layer DVD and then some



def _analyze_instead(error: Exception) -> bool:
    """A file this app can't split as a game or match as a program: REA can still open it as a project."""
    text = str(error)
    return any(phrase in text for phrase in (
        "isn't a ROM, disc or game program", "isn't for x86/x86-64 Linux", "isn't for x86/x86-64 matching",
        "too big for a ROM and isn't a disc image"))


def file_log(path: Path) -> logging.Logger:
    """decomp.anything's own log (logs/decomp.log in its data folder): every job, what it said, and every error with its
    traceback, so a failed import can be reported with the whole story."""
    logger = logging.getLogger("decomp.anything")
    if not any(getattr(h, "baseFilename", None) == str(path) for h in logger.handlers):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(path, maxBytes=2 << 20, backupCount=2, encoding="utf-8")
        except OSError:
            return logger
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


def _tool_of(cmd) -> str:
    """The program a command ran (past a systemd-run wrapper), by name."""
    argv = [str(a) for a in cmd] if isinstance(cmd, (list, tuple)) else str(cmd).split()
    if "--" in argv and Path(argv[0]).name.startswith("systemd-run"):
        argv = argv[argv.index("--") + 1:]
    return Path(argv[0]).name if argv else "a tool"


def job_error(e: BaseException, log_path: Path | None = None) -> str:
    """What to tell the user when a job fails: why, in words, for every kind of failure."""
    if isinstance(e, subprocess.TimeoutExpired):
        mins = max(1, round((e.timeout or 0) / 60))
        return (f"{_tool_of(e.cmd)} took longer than {mins} minute{'s' if mins != 1 else ''} and was stopped. "
                "A very big game can need more: please report it, with the log")
    if isinstance(e, subprocess.CalledProcessError):
        said = e.stderr or e.output or ""  # what the command said, so the error shows why
        said = (said.decode("utf-8", "replace") if isinstance(said, bytes) else said).strip()
        return f"{_tool_of(e.cmd)} failed ({e.returncode})" + (f": {said[-400:]}" if said else "")
    if isinstance(e, MemoryError):
        return "this computer ran out of memory: close other programs and try again"
    if isinstance(e, (NetworkError, RomError, toolchains.ToolchainError, ValueError, RuntimeError, OSError)):
        return str(e) or type(e).__name__
    where = f" (the log: {hostenv.shown_path(log_path)})" if log_path else ""
    return (f"something went wrong inside decomp.anything ({type(e).__name__}: {e}). That's a bug: please report it with "
            f"the log{where}")


class Jobs:
    """Background work the GUI can follow: imports and installs. A job always ends: done, or an error that says why
    (never `running` forever because of an error nobody caught). Everything goes to the log file too."""

    def __init__(self, log_path: Path | None = None):
        self.jobs: dict[str, dict] = {}
        # jid -> event set when the job ends. Kept beside `jobs`, not inside it: job dicts are JSON to the GUI.
        self.done: dict[str, threading.Event] = {}
        self.log_path = log_path
        self.logger = file_log(log_path) if log_path else logging.getLogger("decomp.anything")

    def _prune(self) -> None:
        now = time.time()
        for jid, job in list(self.jobs.items()):
            ttl = 300 if job.get("kind") == "rea" else 600
            if job.get("state") in ("done", "error") and now - job.get("t", 0) > ttl:
                self.jobs.pop(jid, None)
                self.done.pop(jid, None)

    def start(self, kind: str, fn) -> str:
        self._prune()
        jid = uuid.uuid4().hex[:12]
        event = threading.Event()  # async watchers wait on this instead of polling the dict every 0.5s
        job = {"id": jid, "kind": kind, "state": "running", "log": [], "result": None, "error": None, "t": time.time()}
        self.jobs[jid] = job
        self.done[jid] = event
        logger = self.logger
        logger.info("job %s %s started", kind, jid)

        def log(line: str) -> None:
            job["log"].append(str(line))
            logger.info("[%s %s] %s", kind, jid[:6], line)

        def run():
            try:
                job["result"] = fn(log)
                job["state"] = "done"
                logger.info("job %s %s done in %.0fs", kind, jid, time.time() - job["t"])
            except BaseException as e:  # a job must end, whatever went wrong
                if getattr(e, "details", None):  # what the tool said, in full
                    job["trace"] = str(e.details)[-6000:]
                elif not isinstance(e, (NetworkError, RomError, ValueError)):
                    job["trace"] = traceback.format_exc()[-4000:]
                job["error"] = job_error(e, self.log_path)
                job["state"] = "error"  # last: whoever sees the state sees the rest
                logger.error("job %s %s failed: %s\n%s", kind, jid, job["error"], traceback.format_exc())
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
            finally:
                event.set()  # state is already final here, so a waiter always reads the real outcome
        threading.Thread(target=run, daemon=True, name=f"job-{kind}").start()
        return jid


class Settings:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}
        if not isinstance(self.data.get("endpoints"), list):  # one saved "api" before 0.10.1: the first endpoint
            old = self.data.get("api") if isinstance(self.data.get("api"), dict) else {}
            self.data["endpoints"] = [agents.endpoint_entry(old | {"id": "e1"})] if old.get("base_url") or old.get("provider") else []

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(f".tmp.{os.getpid()}.{threading.get_ident()}")
            tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            with contextlib.suppress(OSError):
                os.chmod(tmp, 0o600)
            tmp.replace(self.path)

    # -- API endpoints: OpenAI-compatible servers for the built-in agent, each with its own address, model and key

    def endpoints(self) -> list[dict]:
        with self._lock:
            return [dict(e) for e in self.data.get("endpoints") or [] if isinstance(e, dict) and e.get("id")]

    def endpoint(self, eid: str) -> dict | None:
        return next((e for e in self.endpoints() if e["id"] == eid), None)

    def save_endpoint(self, data: dict) -> dict:
        """Add an endpoint (no id) or change one. A key is only replaced by a new one typed in (the masked one the
        GUI shows comes back unchanged)."""
        numbers = {}
        for k in ("context_tokens", "max_sessions"):  # the model's context in tokens (empty = find out); a box's limit
            if k in data:
                try:
                    numbers[k] = int(str(data[k] or "").strip() or 0)
                except ValueError:
                    raise ValueError("the context size is a number of tokens, like 16384" if k == "context_tokens"
                                     else "the session limit is a number, like 1") from None
        with self._lock:
            eid = str(data.get("id") or "")
            current = next((e for e in self.data.get("endpoints") or [] if isinstance(e, dict) and e.get("id") == eid), None)
            if current is None:  # ids are never reused: a session resumed later can't end up on another machine
                ids = {e["id"] for e in self.data.get("endpoints") or [] if isinstance(e, dict) and e.get("id")}
                n = max(int(self.data.get("endpoint_seq") or 0), len(ids)) + 1
                while f"e{n}" in ids:
                    n += 1
                self.data["endpoint_seq"] = n
                current = {"id": f"e{n}"}
                self.data.setdefault("endpoints", []).append(current)
            for k in ("name", "provider", "base_url", "model"):
                if k in data:
                    current[k] = str(data[k] or "").strip()
            for k, n in numbers.items():
                if n:
                    current[k] = max(2048, n) if k == "context_tokens" else max(1, min(n, 32))
                else:
                    current.pop(k, None)
            if data.get("api_key") and "•" not in str(data["api_key"]):
                current["api_key"] = str(data["api_key"]).strip()
            if data.get("clear_key"):
                current.pop("api_key", None)
            current.update(agents.endpoint_entry(current))
            return dict(current)

    def remove_endpoint(self, eid: str) -> None:
        with self._lock:
            self.data["endpoints"] = [e for e in self.data.get("endpoints") or [] if isinstance(e, dict) and e.get("id") != eid]

    def public(self) -> dict:
        def masked(e: dict) -> dict:
            out = {k: v for k, v in e.items() if k != "api_key"}
            if e.get("api_key"):
                out["api_key"], out["has_key"] = "•" * 8 + e["api_key"][-4:], True
            return out

        eps = [masked(e) for e in self.endpoints()]
        return {"endpoints": eps, "api": eps[0] if eps else {},
                "per_session": self.data.get("per_session", agents.PER_SESSION), "last_project": self.data.get("last_project"),
                "autopilot": self.data.get("autopilot", True), "budget_usd": self.data.get("budget_usd") or 0,
                "auto_update": bool(self.data.get("auto_update")), "check_updates": self.data.get("check_updates", True) is not False,
                "auto_update_rea": bool(self.data.get("auto_update_rea")),
                "launcher": _launcher(self.data.get("launcher") or {}), "recent_models": self.data.get("recent_models") or {}}


def _session_options(entry: dict) -> dict:
    """What a built-in agent session needs from its endpoint."""
    out = {"endpoint": entry.get("id") or "", "provider": entry["provider"], "base_url": entry["base_url"],
           "model": entry["model"], "provider_name": entry["name"]}
    return out | {k: entry[k] for k in ("api_key", "context_tokens") if entry.get(k)}


def _spread(settings: Settings, manager, picked: list[str], count: int, given: dict) -> list[tuple[dict, int]]:
    """(endpoint, sessions) for a start of `count` built-in agent sessions: spread over the endpoints picked, one
    at a time in turn, each up to its own limit of sessions at once (a small box serves one request at a time).
    A start without endpoints (an older GUI, or an address typed in) uses the first saved one, with what it
    changes; a saved key only ever goes to its own endpoint's address."""
    if not picked:
        first = settings.endpoints()[0] if settings.endpoints() else {}
        typed = {k: v for k, v in given.items() if k in ("provider", "base_url", "model", "context_tokens") and v}
        entry = agents.endpoint_entry(first | typed)
        if first and entry["base_url"].rstrip("/") != first["base_url"].rstrip("/"):
            entry.pop("api_key", None)
        if not entry.get("base_url") or not entry.get("model"):
            raise ValueError("add an API endpoint first (Agents & models): its address and model")
        return [(entry, count)]
    eps = [settings.endpoint(e) for e in picked]
    if any(e is None for e in eps):
        raise ValueError("an endpoint you picked isn't saved any more: pick again")
    busy = {e["id"]: sum(1 for x in manager.sessions.values() if x.kind == "api" and x.options.get("endpoint") == e["id"]
                         and x.state in ("starting", "working", "waiting") and not x.stop.is_set()) for e in eps}
    plan = {e["id"]: 0 for e in eps}
    for _ in range(count):
        room = [e for e in eps if not e.get("max_sessions") or busy[e["id"]] + plan[e["id"]] < int(e["max_sessions"])]
        if not room:
            break
        pick = min(room, key=lambda e: (busy[e["id"]] + plan[e["id"]], eps.index(e)))
        plan[pick["id"]] += 1
    if not any(plan.values()):
        raise ValueError("every endpoint you picked already runs as many sessions as you allowed it")
    return [(e, plan[e["id"]]) for e in eps if plan[e["id"]]]


def _launcher(saved: dict) -> dict:
    """The last launcher choice; one saved for Gemini CLI (retired) comes back as Antigravity with its default model."""
    if saved.get("kind") and agents.kind_id(saved["kind"]) != saved["kind"]:
        return {**saved, "kind": agents.kind_id(saved["kind"]), "model": ""}
    return saved


def _static(name: str) -> bytes | None:
    if ".." in name or name.startswith(("/", "\\")):
        return None
    try:
        return resources.files("decompnet.app").joinpath("static", name).read_bytes()
    except (FileNotFoundError, IsADirectoryError, OSError):
        return None


class ServerContext:
    """Everything a route group needs from the server, built once by `server.build()`.

    A few members are made after the context (the MCP app the routes register on, the two update checkers, the update
    thread's entry point); they are declared here so a route group can use them and a reader can find them."""

    mcp: MCPServer  # build(): tools.build_mcp, before the routes register
    updater: updates.Updater  # build(): they outlive a request and restart through before_restart
    rea_updater: updates.Updater
    watch_updates: Callable[[], None]  # routes_updates: the thread build() starts

    def __init__(self, net, home: Path, port: int, secret: str, manager, jobs: Jobs, settings: Settings, console):
        self.net = net
        self.home = home
        self.port = port
        self.secret = secret
        self.manager = manager
        self.jobs = jobs
        self.settings = settings
        self.console = console
        self.mcp_url = f"http://127.0.0.1:{port}/mcp"
        self.build_id = ""
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    # --- the helpers every route starts with ------------------------------------------------------------------

    def guard(self, request: Request, write: bool = False):
        """None when the request may run (same host, the page's cookie, the page's write header); else the refusal."""
        if request.headers.get("host") not in self.allowed_hosts:
            return JSONResponse({"error": "bad host"}, status_code=403)
        if not secrets.compare_digest(request.cookies.get(COOKIE, ""), self.secret):
            return JSONResponse({"error": "open decomp.anything from the app or the `decomp` command"}, status_code=401)
        if write and request.headers.get("x-decomp") != "1":
            return JSONResponse({"error": "missing header"}, status_code=403)
        return None

    async def body(self, request: Request) -> dict:
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    async def call(self, fn, *args):
        """Something slow (a build, an import) without blocking the event loop."""
        return await anyio.to_thread.run_sync(fn, *args)

    def fail(self, e: Exception, code: int = 400) -> JSONResponse:
        return JSONResponse({"error": str(e)}, status_code=code)

    def with_project(self, fn):
        """A route that works on one project: the slug from the body or the query, and its errors as JSON."""
        async def handler(request: Request):
            if (bad := self.guard(request, write=request.method == "POST")):
                return bad
            data = await self.body(request) if request.method == "POST" else {}
            slug = data.get("slug") or request.query_params.get("slug", "")
            try:
                return JSONResponse(await self.call(lambda: fn(slug, request, data)))
            except (NetworkError, ValueError, toolchains.ToolchainError, OSError) as e:
                return self.fail(e)
        return handler

    # --- who is asking, and what several route groups start -----------------------------------------------------

    def agent_of(self, ctx):
        """The agent behind an MCP request, and the project it works on."""
        headers = ctx.headers or {}
        auth = headers.get("authorization") or headers.get("Authorization") or ""
        agent = self.net.authenticate(auth.removeprefix("Bearer ").strip() or None)
        slug = self.manager.project_of(agent.id) or self.settings.data.get("last_project")
        if not slug:
            raise NetworkError("no project is open in decomp.anything; open a ROM in the app first")
        return agent, slug

    def autopilot(self, slug: str) -> None:
        """Free matches first, in the background, when the user wants that (the default): a game started from
        scratch, on any platform, as soon as it's ready. A public decomp, or one not set up yet, says no."""
        if self.settings.data.get("autopilot", True):
            with contextlib.suppress(NetworkError):
                self.net.start_autopilot(slug)

    def before_restart(self) -> None:  # what quitting does: helpers stop now and resume in the new version
        self.manager.stop()
        self.manager.on_stop()
        self.console.stop()
        rea.close_all()
