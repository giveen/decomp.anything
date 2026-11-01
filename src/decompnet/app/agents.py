"""The agents decomp.wtf runs: their sessions, and the manager that keeps them going.

An agent is a subprocess (a CLI like Claude Code) or an HTTP conversation (an OpenAI-compatible endpoint), started for one
project with decomp.anything's MCP server attached, so everything it does shows up in the app as it happens. The manager
owns their lifetime: starting them with the right prompt, restarting them when they exit, budgeting them, reading what
they print, and remembering them across a restart.

What the CLIs are and how they print live beside this module (agenttools, agentparse). The names the rest of the app asks
for are re-exported here, so `agents.<name>` keeps meaning what it did before the split.
"""

from __future__ import annotations

import collections
import contextlib
import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ..network import NetworkError
from . import tools
from .agentparse import _args_brief, clean_stderr, parse_line
from .agenttools import (
    _SIGNED_OUT,
    _THROTTLED,
    AGY_ALLOW,
    ALIASES,
    API_PRESETS,
    CLAUDE_MODELS,
    KINDS,
    PER_SESSION,
    WIN,
    Fatal,
    Kind,
    _codex_config,
    _config_model,
    _which,
    agy_settings,
    argv,
    detect,
    endpoint_entry,
    is_local,
    kind_id,
    major,
    models,
    no_credit,
    search_path,
    signin,
    version,
)
from .engine import RomNetwork

__all__ = [
    "AGY_ALLOW",
    "ALIASES",
    "API_PRESETS",
    "CLAUDE_MODELS",
    "KINDS",
    "PER_SESSION",
    "Fatal",
    "Kind",
    "Manager",
    "Session",
    "agy_settings",
    "api_tool_schemas",
    "argv",
    "clean_stderr",
    "context_window",
    "detect",
    "endpoint_entry",
    "fit",
    "is_local",
    "kind_id",
    "major",
    "models",
    "no_credit",
    "parse_line",
    "search_path",
    "signin",
    "version",
]


@dataclass
class Session:
    id: str
    kind: str
    label: str
    slug: str
    agent_name: str
    options: dict
    state: str = "starting"  # starting | working | waiting | stopped | error
    started_at: float = field(default_factory=time.time)
    runs: int = 0
    error: str = ""
    feed: collections.deque = field(default_factory=lambda: collections.deque(maxlen=400))
    seq: int = 0
    stop: threading.Event = field(default_factory=threading.Event)
    proc: subprocess.Popen | None = None
    http: httpx.Client | None = None  # an API agent's request in flight; closing it aborts the wait
    usd: float = 0.0  # what the agent's provider charged, as the agent reports it
    tokens: int = 0
    signed_out: bool = False  # the agent CLI isn't signed in: trying again won't help
    throttled: bool = False  # the last run ended on the provider's limit of requests at once: not a failure

    def push(self, kind: str, text: str) -> None:
        self.seq += 1
        self.feed.append({"seq": self.seq, "t": time.time(), "type": kind, "text": text[:4000]})

    def snapshot(self, since: int = 0) -> dict:
        return {"id": self.id, "kind": self.kind, "label": self.label, "slug": self.slug, "agent": self.agent_name,
                "state": self.state, "runs": self.runs, "error": self.error, "started_at": self.started_at,
                "model": self.options.get("model") or "", "usd": round(self.usd, 4), "tokens": self.tokens,
                "feed": [f for f in self.feed if f["seq"] > since]}

class Manager:
    def __init__(self, net: RomNetwork, home: Path, mcp_url: str):
        self.net, self.home, self.mcp_url = net, home, mcp_url
        self.sessions: dict[str, Session] = {}
        self.assignments: dict[int, str] = {}  # agent id -> project slug
        self._lock = threading.Lock()
        self._prep: dict[str, threading.Event] = {}  # slug -> giving its code source files before agents claim
        self.on_stop = lambda: None  # set by the server: close tunnels when the app quits
        self.persist = lambda running: None  # set by the server: remember helpers to resume after a restart

    # -- control

    def start(self, kind: str, slug: str, count: int = 1, options: dict | None = None) -> list[str]:
        options = dict(options or {})
        if kind_id(kind) != kind:
            kind = kind_id(kind)
            options.pop("model", None)  # a Gemini CLI model name may mean nothing to Antigravity: use its default
        if kind not in KINDS:
            raise ValueError(f"unknown agent kind {kind!r}")
        ids = []
        self._prepare(slug)
        with self._lock:
            for _ in range(max(1, min(int(count), 8))):
                n = 1 + sum(1 for s in self.sessions.values() if s.kind == kind)
                sid = f"{kind}-{n}"
                while sid in self.sessions:
                    n += 1
                    sid = f"{kind}-{n}"
                if kind == "api":  # numbered per endpoint: "gpu-box #2" says which machine it runs on
                    name = options.get("provider_name") or "API"
                    k = 1 + max((int(s.label.rsplit(" #", 1)[1]) for s in self.sessions.values() if s.kind == "api"
                                 and s.label.rsplit(" #", 1)[0] == name and s.label.rsplit(" #", 1)[-1].isdigit()), default=0)
                    label = f"{name} #{k}"
                else:
                    label = f"{KINDS[kind].name} #{n}"
                s = Session(sid, kind, label, slug, sid, dict(options))
                self.sessions[sid] = s
                threading.Thread(target=self._run, args=(s,), daemon=True, name=f"agent-{sid}").start()
                ids.append(sid)
        self.persist(self.running())
        return ids

    def _prepare(self, slug: str) -> None:
        """Before agents claim: free matches from identical code (twins, in the background: what they'd match is
        left for last meanwhile), and on a GameCube/Wii decomp where code has no source file yet, files for it
        (start_files, checked against the game's checksum), or they'd all share the few hard functions left."""
        try:
            self.net.twins.refresh(slug)
            self.net.permuter.start(slug)  # near misses agents leave, on spare CPU
        except Exception:  # noqa: BLE001 agents work without them
            pass
        with self._lock:
            if slug in self._prep:
                return
            try:
                if not self.net.fork_slug(slug):
                    return
                _, fp, _cfg = self.net.forks.get(slug)
                if not (self.net.forks.uncovered(slug) or {}).get("functions") or not fp.linkable():
                    return
            except Exception:  # noqa: BLE001
                return
            done = self._prep[slug] = threading.Event()

        def run() -> None:
            try:
                self.net.forks.start_files(slug, log=lambda *_: None)
            except Exception:  # noqa: BLE001 the agents still work on what has files
                pass
            finally:
                done.set()
                with self._lock:
                    self._prep.pop(slug, None)

        threading.Thread(target=run, daemon=True, name=f"start-files-{slug}").start()

    def running(self) -> list[dict]:
        """The helpers that should keep working: resumed by the server after the app restarts."""
        return [{"kind": s.kind, "slug": s.slug, "options": {k: v for k, v in s.options.items() if k != "api_key"}}
                for s in self.sessions.values() if s.state in ("starting", "working", "waiting") and not s.stop.is_set()]

    def stop(self, sid: str | None = None) -> None:
        """Stop one helper (for good) or all of them (the app is quitting: they resume next time)."""
        for s in list(self.sessions.values()):
            if sid in (None, s.id):
                s.stop.set()
                if s.proc and s.proc.poll() is None:
                    _terminate(s.proc)
                if s.http is not None:  # a local model can take minutes to answer: don't wait for it
                    with contextlib.suppress(Exception):
                        s.http.close()
                if s.state != "error":
                    s.state = "stopped"
        if sid is not None:
            self.persist(self.running())

    def stop_project(self, slug: str) -> None:
        """Stop all helpers working on a specific project."""
        for s in list(self.sessions.values()):
            if s.slug == slug:
                self.stop(s.id)

    def remove(self, sid: str) -> None:
        self.stop(sid)
        self.sessions.pop(sid, None)

    def snapshot(self, since: dict | None = None) -> list[dict]:
        since = since or {}
        return [s.snapshot(int(since.get(s.id, 0))) for s in self.sessions.values()]

    def project_of(self, agent_id: int) -> str | None:
        return self.assignments.get(agent_id)

    # -- loop

    def _progress(self, agent_id: int) -> int:
        """Compiles + claims + notes by this agent: a session that adds none of these made no progress."""
        row = self.net.store.one(
            "SELECT (SELECT COUNT(*) FROM attempts WHERE agent_id=?) + (SELECT COUNT(*) FROM events WHERE agent_id=?) AS n",
            agent_id, agent_id)
        return row["n"]

    def _left(self, slug: str) -> int:
        proj = self.net.project_row(slug)
        row = self.net.store.one("SELECT COUNT(*) AS n FROM functions WHERE project_id=? AND status='open'", proj["id"])
        return row["n"]

    def _account(self, s: Session, usage: dict) -> None:
        """Add what a session spent to the session and to the project (kept in its config across restarts)."""
        usd, tokens = float(usage.get("usd") or 0), int(usage.get("tokens") or 0)
        s.usd += usd
        s.tokens += tokens
        self.net.add_spend(s.slug, usd, tokens)

    def over_budget(self, slug: str) -> float | None:
        """The budget (USD) when the project's agents have spent it, else None."""
        cap = self.budget()
        spent = (self.net.spend(slug) or {}).get("usd", 0)
        return cap if cap and spent >= cap else None

    def budget(self) -> float:
        return 0.0  # set by the server from the settings

    def _run(self, s: Session) -> None:
        agent, token = self.net.local_agent(s.agent_name)
        self.assignments[agent.id] = s.slug
        workdir = self.home / "agents" / s.id
        workdir.mkdir(parents=True, exist_ok=True)
        failures = throttles = 0
        s.push("info", f"{s.label} starting on {s.slug}")
        if (prep := self._prep.get(s.slug)) is not None:
            s.push("info", "first giving the game's code without a source file one (about a minute)")
            prep.wait(900)
        twins = getattr(getattr(self.net, "twins", None), "jobs", {}).get(s.slug)
        if twins is not None and not twins.ordered.is_set():
            s.push("info", "first setting hand-written assembly aside and putting the functions in order "
                           "(seconds; a minute the first time)")
            twins.ordered.wait(120)
        while not s.stop.is_set():
            if self._left(s.slug) == 0:
                s.push("info", "nothing left to decompile here 🎉")
                break
            if (cap := self.over_budget(s.slug)) is not None:
                s.push("info", f"stopping: the agents on this project have spent the ${cap:.2f} budget (Setup)")
                break
            link = self.net.links.get(s.slug)
            if link is not None and (link.error or time.time() - link.last_ok > 30):
                # in a room whose host is away (or that hasn't let us in yet): wait, don't fail
                if s.state != "waiting" or not s.feed or "waiting for the room" not in s.feed[-1]["text"]:
                    s.push("info", f"waiting for the room's host: {link.error or 'not reachable right now'}")
                s.state = "waiting"
                with contextlib.suppress(Exception):
                    link.sync()
                s.stop.wait(10)
                continue
            s.state = "working"
            s.runs += 1
            t0 = time.time()
            before = self._progress(agent.id)
            try:
                ok = self._run_api(s, agent) if s.kind == "api" else self._run_cli(s, token, workdir)
            except Fatal as e:  # needs the user first: retrying can't help
                s.state, s.error = "error", str(e)
                break
            except Exception as e:  # noqa: BLE001 keep the runner alive; show what happened
                s.push("error", f"{type(e).__name__}: {e}")
                ok = False
            if ok and not s.stop.is_set() and self._progress(agent.id) == before:
                s.error = "the session ended without compiling anything (check the agent's output)"
                s.push("error", s.error)
                ok = False
            if s.stop.is_set():
                break
            if s.signed_out:
                s.state = "error"
                break
            if s.throttled and throttles < 10:  # the provider's limit, not the agent's failure: wait it out
                s.throttled, throttles = False, throttles + 1
                s.state = "waiting"
                s.push("info", "the model's provider says too many requests at once: trying again in a minute "
                               "(fewer agents at once on this account would wait less)")
                s.stop.wait(60)
                continue
            throttles = 0 if ok else throttles
            failures = 0 if ok else failures + 1
            if failures >= 3:
                s.state = "error"
                s.error = s.error or "the agent failed three times in a row"
                s.push("error", f"stopping: {s.error}. {signin(KINDS[s.kind])}".strip())
                break
            s.state = "waiting"
            s.stop.wait(3 if ok else min(60, 5 * failures + max(0, 10 - (time.time() - t0))))
        if s.state != "error":
            s.state = "stopped"
        if not s.stop.is_set():  # finished by itself (done, budget, errors): no need to resume it
            s.stop.set()
            self.persist(self.running())
        held_functions = self.net.store.all(
            "SELECT f.name FROM claims c JOIN functions f ON f.id=c.function_id WHERE c.agent_id=?", agent.id
        )
        link = self.net.links.get(s.slug)
        if link is not None:
            for held in held_functions:
                with contextlib.suppress(Exception):
                    link.call("/collab/release", agent, function=held["name"], notes="")
        with self.net.store.tx() as db:  # release any claim the agent still holds so others can take it
            db.execute("DELETE FROM claims WHERE agent_id=?", (agent.id,))

    def prompt(self, s: Session) -> str:
        text = self._prompt(s)
        # the know-how every session reads first, given up front (saves each session its first tool calls)
        ov = self.net.overview(s.slug)
        from . import assistance

        topics = assistance.topics(ov)
        text += "\n" + tools._assistance_hint()
        text += "\nKnow-how you'd otherwise read with guide() (more topics: guide()):\n\n" + "\n\n".join(
            tools._guide(t) for t in topics)
        if (learned := self.net.learnings(s.slug)):
            text += ("\nWhat agents learned on this project so far (compiler quirks, conventions; add yours with "
                     f"add_note and no function):\n{learned}\n")
        if (facts := self.net.intake_line(s.slug)):
            text += f"\n{facts}\n"
        if (wish := self.net.instructions(s.slug)):
            text += f"\nThe user's instructions for this project (they come first):\n{wish}\n"
        if (roots := self.net.references(s.slug)):
            from . import references

            text += f"\n{references.prompt_line(roots)}\n"
        return text

    def _prompt(self, s: Session) -> str:
        ov = self.net.overview(s.slug)
        if ov.get("kind") == "analysis":
            return tools.ANALYSIS_PROMPT.format(title=ov["title"], count=s.options.get("per_session", PER_SESSION),
                                                guides='guide("analysis")')
        if ov.get("kind") == "fork":
            guides = ["fork", "objdiff", *tools.PLATFORM_GUIDES.get(ov.get("platform") or "", [])]
            return tools.FORK_PROMPT.format(title=ov["title"], platform=ov.get("platform_name") or "its platform",
                                            count=s.options.get("per_session", PER_SESSION),
                                            guides=", ".join(f'guide("{g}")' for g in guides))
        platform = ov.get("platform") or ""
        language = ov.get("source_language") or "C"
        if platform in ("windows", "linux"):
            target = "program"
            guide = "x86" + (" and cpp" if language == "C++" else "")
            note = ("\nThe target's function names are C++ mangled names: write the class and signature that mangle to "
                    "them (or wrap a sub_<address> in extern \"C\"); the cpp guide has the ABI details.") \
                if language == "C++" else ""
        else:
            target = "game"
            guide = {"psx": "psx", "ps2": "ps2", "gba": "agbcc"}.get(platform) or (
                "ido" if ov["compiler"]["toolchain"].startswith("ido") else "gcc-kmc")
            note = ""
        detected = ov.get("detected") or {}
        if detected and not detected.get("matchable", True):
            note += (f"\nThis target was built with {detected['label']}: C/C++ cannot reproduce that compiler's output, "
                     "so matches will stay elusive. Use the tools to inspect and explain the program; don't claim a match.")
        return tools.WORK_PROMPT.format(title=ov["title"], count=s.options.get("per_session", PER_SESSION),
                                        compiler_guide=guide, language=language, target=target, language_note=note)

    def _command(self, s: Session, token: str, workdir: Path) -> tuple[list[str], dict, str]:
        """The agent's command line, environment and what goes in on stdin (the prompt)."""
        k = KINDS[s.kind]
        exe = _which(k.command)
        if not exe:
            raise RuntimeError(f"{k.name} is not installed")
        env = dict(os.environ)
        if WIN:
            env["PATH"] = search_path()  # the agent finds node (and what it needs) even when installed after the app
        # An agent that only loads the MCP servers in the user's own settings (Antigravity CLI since 1.1.3 ignores a
        # workspace's .agents/mcp_config.json) reaches decomp.anything through the `decomp mcp` the user connected it
        # with: that bridge works as this session's agent when it finds this, not as a new agent of the user's
        env["DECOMP_SESSION_TOKEN"] = token
        prompt = self.prompt(s)
        model = s.options.get("model") or ""
        if s.kind == "claude":
            cfg = workdir / "mcp.json"
            cfg.write_text(json.dumps({"mcpServers": {"decomp": {"type": "http", "url": self.mcp_url,
                                                                  "headers": {"Authorization": f"Bearer {token}"}}}}),
                           encoding="utf-8")
            cmd = [exe, "-p", "--output-format", "stream-json", "--verbose", "--mcp-config", str(cfg),
                   "--strict-mcp-config", "--allowedTools", "mcp__decomp", "--disallowedTools",
                   "Bash,Edit,Write,NotebookEdit,WebFetch,WebSearch"]
            if model:
                cmd += ["--model", model]
        elif s.kind == "codex":
            env["DECOMP_TOKEN"] = token
            # The worker's own server, under a name of its own: `-c` settings merge into the user's config, and
            # `decomp connect codex` gave that a stdio `decomp` entry (a url on top of it: "url is not supported for
            # stdio"). The user's MCP servers are switched off for the run, as --strict-mcp-config does for Claude.
            cmd = [exe, "exec", "--json", "--skip-git-repo-check", "--ephemeral", "-C", str(workdir), "-s", "read-only"]
            for name in (_codex_config().get("mcp_servers") or {}):
                if re.fullmatch(r"[A-Za-z0-9_-]+", name):
                    cmd += ["-c", f"mcp_servers.{name}.enabled=false"]
            cmd += ["-c", f'mcp_servers.decomp_agent={{url="{self.mcp_url}", bearer_token_env_var="DECOMP_TOKEN", '
                          'default_tools_approval_mode="approve", tool_timeout_sec=180}',
                    "-c", 'approval_policy="never"']
            if model:
                cmd += ["-m", model]
            cmd.append("-")  # the prompt comes from stdin
        elif s.kind == "antigravity":
            # the workspace's own MCP config; tool permission comes from the user's rule for decomp's tools (AGY_ALLOW)
            (workdir / ".agents").mkdir(exist_ok=True)
            (workdir / ".agents" / "mcp_config.json").write_text(json.dumps({"mcpServers": {"decomp": {
                "serverUrl": self.mcp_url, "headers": {"Authorization": f"Bearer {token}"}}}}), encoding="utf-8")
            # a session works through several functions: more than agy's default 5 minutes
            cmd = [exe, "--output-format", "stream-json", "--input-format", "stream-json", "--print-timeout", "2h"]
            if model:
                cmd += ["--model", model]
            prompt = json.dumps({"event": "user", "message": {"content": prompt}}) + "\n"
        elif s.kind == "opencode":
            # "deny" takes the tools out of the request, and OpenCode Zen's free models then refuse it ("can only be
            # used from within OpenCode"). "ask" keeps the usual tools, and `opencode run` rejects every ask itself.
            # (Zen's and Go's free models alike: opencode/... and opencode-go/...)
            zen = (model or _config_model("opencode") or "opencode/").split("/", 1)[0] in ("opencode", "opencode-go")
            never = "ask" if zen else "deny"
            server = {"type": "remote", "url": self.mcp_url, "headers": {"Authorization": f"Bearer {token}"}}
            v2 = major(version("opencode", exe)) >= 2
            if v2:
                # OpenCode 2 reaches MCP tools through one `execute` tool by default (Code Mode), where models wander
                # off to the user's other servers: decomp's tools are offered as themselves and `execute` is denied.
                cfg = {"mcp": {"servers": {"decomp": {**server, "oauth": False, "codemode": False}}},
                       "permissions": [{"action": "execute", "resource": "*", "effect": "deny"},
                                       {"action": "decomp_*", "resource": "*", "effect": "allow"},
                                       *({"action": a, "resource": "*", "effect": never} for a in ("edit", "shell", "webfetch"))]}
            else:
                cfg = {"mcp": {"decomp": {**server, "enabled": True}},
                       "permission": {"edit": never, "bash": never, "webfetch": never}}
            (workdir / "opencode.json").write_text(json.dumps({"$schema": "https://opencode.ai/config.json", **cfg}),
                                                   encoding="utf-8")
            # --print-logs: opencode's JSON error only says "Unexpected server error"; the log line says what failed.
            # (No --dir: it runs in workdir, and OpenCode before 1.4 stops at the flag. No --log-level: OpenCode 2
            # stops at "ERROR", earlier ones at "error"; clean_stderr keeps only the errors anyway.) OpenCode 2 runs
            # on a shared background service unless --standalone: then the server's own errors reach stderr too.
            cmd = [exe, "run", "--format", "json", "--print-logs", *(["--standalone"] if v2 else [])]
            if model:
                cmd += ["-m", model]
        else:
            raise RuntimeError(s.kind)
        return argv(cmd), env, prompt

    def _run_cli(self, s: Session, token: str, workdir: Path) -> bool:
        if s.kind == "antigravity" and s.runs == 1:
            from . import connect  # (connect imports this module)

            if not connect.status("antigravity"):  # decomp.anything's tools only reach it through the user's settings
                raise Fatal("Antigravity CLI only loads the MCP servers in your own settings: connect it to decomp.anything "
                            "once (Connect your agent, or `decomp connect antigravity`), then start the helpers again")
            allowed = connect.antigravity_allows()
            if not connect.allow_antigravity():
                raise Fatal("Antigravity's settings couldn't be updated to allow decomp.anything's tools: add "
                            f"\"{AGY_ALLOW}\" to \"permissions\": {{\"allow\": [...]}} in {agy_settings()}")
            if not allowed:
                s.push("info", "allowed decomp.anything's tools in Antigravity's settings")
        cmd, env, prompt = self._command(s, token, workdir)
        s.push("info", f"session {s.runs}: {KINDS[s.kind].name} started")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if WIN else 0
        proc = subprocess.Popen(cmd, cwd=workdir, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1,
                                creationflags=flags, start_new_session=not WIN)
        s.proc = proc
        errors: collections.deque = collections.deque(maxlen=20)

        def feed_prompt():
            try:
                proc.stdin.write(prompt)
                proc.stdin.close()  # end of input: the agent works on this one prompt, then exits
            except OSError:  # it exited before reading (stderr says why)
                pass

        def drain_err():
            for line in proc.stderr:
                if (text := clean_stderr(s.kind, line)) and (not errors or errors[-1] != text):
                    errors.append(text)
        threading.Thread(target=feed_prompt, daemon=True).start()
        threading.Thread(target=drain_err, daemon=True).start()
        saw_tool = False
        said: collections.deque = collections.deque(maxlen=20)
        state: dict = {}
        last_error = refused = ""
        for line in proc.stdout:
            for kind, text in parse_line(s.kind, line, state):
                if kind == "usage":
                    self._account(s, json.loads(text))
                    continue
                saw_tool |= kind == "tool"
                said.append(text)
                if kind == "error":
                    last_error = text
                    refused = refused or (text if text.startswith("Antigravity refused") else "")
                s.push(kind, text)
            if s.stop.is_set():
                _terminate(proc)
                break
        code = proc.wait()
        s.proc = None
        if refused and not s.stop.is_set():
            raise Fatal(refused)  # every retry would be refused the same way
        if code != 0 and not s.stop.is_set() and (why := no_credit(" ".join([*said, *errors]))):
            raise Fatal(why)  # every agent on this account fails the same way until it's topped up
        if code != 0 and not s.stop.is_set() and _THROTTLED.search(" ".join([*said, *errors])):
            s.throttled = True
        if code != 0 and not s.stop.is_set() and _SIGNED_OUT.search(" ".join([*said, *errors])):
            s.signed_out = True
            s.error = f"{KINDS[s.kind].name} isn't signed in on this computer. {signin(KINDS[s.kind])}"
            s.push("error", s.error)
            return False
        if code != 0 and not s.stop.is_set():
            tail = " · ".join(list(errors)[-3:]) or last_error or f"exited with code {code}"
            s.error = tail[:300]
            s.push("error", f"exited with code {code}: {tail[:600]}" if tail != f"exited with code {code}" else tail)
            return False
        if not saw_tool and not s.stop.is_set():
            s.error = "the agent never used the decomp tools" + (f": {list(errors)[-1]}" if errors else "")
            s.push("error", s.error)
            return False
        s.error = ""
        return True

    # -- built-in agent for OpenAI-compatible APIs

    def _run_api(self, s: Session, agent) -> bool:
        """The agent loop for any OpenAI-compatible API. The conversation is kept within the model's context
        (`context_tokens`, or what a local llama.cpp server says it has): older tool output is trimmed first, and
        when the server still says it's too long, trimmed harder and sent again."""
        base = (s.options.get("base_url") or "").rstrip("/")
        model = s.options.get("model") or ""
        if not base or not model:
            raise RuntimeError("set the API address and model first")
        headers = {"Content-Type": "application/json"}
        if s.options.get("api_key"):
            headers["Authorization"] = f"Bearer {s.options['api_key']}"
        try:
            analysis = self.net.is_analysis(self.net.project_row(s.slug))
        except NetworkError:
            analysis = False
        schema = api_tool_schemas(fork=bool(self.net.fork_slug(s.slug)), analysis=analysis)
        messages = [{"role": "system", "content": tools.INSTRUCTIONS},
                    {"role": "user", "content": self.prompt(s)}]
        used_tool = False
        client = httpx.Client(timeout=httpx.Timeout(300, connect=15))
        s.http = client  # stop() closes this, so a long local generation doesn't keep the session alive
        try:
            with client:
                window = context_window(client, base, s.options, s.stop)
                s.push("info", f"session {s.runs}: {model} via {base} ({window // 1000}K tokens of context)")
                budget = int(window * 0.7) - _tokens(schema)  # room for the reply and the tools' definitions
                for _step in range(int(s.options.get("max_steps", 120))):
                    if s.stop.is_set():
                        return True
                    for _attempt in range(4):
                        if s.stop.is_set():
                            return True
                        fit(messages, budget, trim_latest_tools=True)
                        r = _request_or_stop(s.stop, client, "post", f"{base}/chat/completions", headers=headers,
                                             json={"model": model, "messages": messages, "tools": schema,
                                                   "tool_choice": "auto"})
                        if r is None:
                            return True
                        if r.status_code == 200 or not _too_long(r):
                            break
                        budget = int(budget * 0.6)  # the server counts differently, or knows better: trim more
                    if r.status_code != 200:
                        if (why := no_credit(r.text)) or r.status_code == 402:
                            raise Fatal(why or no_credit("payment required"))
                        s.throttled = r.status_code == 429 or bool(_THROTTLED.search(r.text))
                        s.error = f"API error {r.status_code}: {r.text[:300]}"
                        if _too_long(r):
                            s.error = ("the model's context is too small for this work even after trimming: give it at "
                                       "least 16K tokens (llama.cpp: -c 16384; Ollama: OLLAMA_CONTEXT_LENGTH=16384; LM "
                                       "Studio: Context Length) and set it in Agents & models")
                        s.push("error", s.error)
                        return False
                    reply = r.json()
                    msg = reply["choices"][0]["message"]
                    usage = reply.get("usage") if isinstance(reply.get("usage"), dict) else {}
                    if usage.get("total_tokens"):
                        self._account(s, {"tokens": usage["total_tokens"]})
                    if msg.get("reasoning_content"):
                        s.push("think", msg["reasoning_content"][:2000])
                    if msg.get("content"):
                        s.push("text", msg["content"])
                    calls = msg.get("tool_calls") or []
                    messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")} | {"role": "assistant"})
                    if not calls:
                        return used_tool
                    for c in calls:
                        if s.stop.is_set():
                            return True
                        name = c["function"]["name"]
                        try:
                            args = json.loads(c["function"].get("arguments") or "{}")
                        except ValueError:
                            args = {}
                        used_tool = True
                        s.push("tool", f"{name}({_args_brief(args)})")
                        result = tools.call(self.net, agent, s.slug, name, args)
                        if s.stop.is_set():
                            return True
                        messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
                    if len(messages) > 80:  # keep the conversation small: summarize by restarting fresh
                        return True
            return used_tool
        except httpx.HTTPError:
            if s.stop.is_set():  # stop() closed the request: this isn't an agent failure
                return True
            raise
        finally:
            s.http = None

def _tokens(obj) -> int:
    """A cautious token estimate: code and assembly run about 3 characters a token."""
    return len(obj if isinstance(obj, str) else json.dumps(obj)) // 3 + 4

def _too_long(r) -> bool:
    """The server refused the request because it doesn't fit the model's context."""
    return r.status_code in (400, 413, 422, 500) and bool(re.search(
        r"context|too long|exceed|maximum.*tokens|n_ctx|prompt is too|reduce the length", r.text[:2000], re.IGNORECASE))

def fit(messages: list[dict], budget: int, *, trim_latest_tools: bool = False) -> int:
    """Shrink the conversation to `budget` tokens: older tool output goes first (a function's assembly and diffs
    from several steps ago), then older reasoning. The instructions, the task and the latest exchange stay whole.
    API requests may also bound an oversized latest tool response, labelled incomplete; task instructions stay whole.
    Returns the estimate after."""
    total = sum(_tokens(m) for m in messages)
    keep_from = max(2, len(messages) - 2)
    for kinds in (("tool",), ("assistant",)):
        for m in messages[2:keep_from]:
            if total <= budget:
                return total
            text = m.get("content")
            if m.get("role") in kinds and isinstance(text, str) and len(text) > 300:
                before = _tokens(m)
                lines = text.splitlines()
                prefix = lines[0] if lines else text[:240]
                if (len(lines) > 1 and len(prefix) + len(lines[1]) + 1 <= 240
                        and not lines[1].strip().startswith(("|", "TARGET", "markers:"))):
                    prefix = prefix + "\n" + lines[1]
                m["content"] = prefix[:240] + "\n[... trimmed to save context: call the tool again if you need this]"
                total -= before - _tokens(m)
    if trim_latest_tools and total > budget:
        for message in messages[2:]:
            text = message.get("content")
            if message.get("role") != "tool" or not isinstance(text, str) or len(text) <= 600:
                continue
            before = _tokens(message)
            size = max(300, len(text) - (total - budget) * 3 - 300)
            message["content"] = (text[:size // 2] + "\n[Incomplete tool preview: trimmed to fit context. "
                                  "Use a focused view or retained evidence; do not infer full coverage.]\n" + text[-size // 2:])
            total += _tokens(message) - before
            if total <= budget:
                break
    return total

def _request_or_stop(stop: threading.Event, client, method: str, url: str, **kwargs):
    """Run one HTTP request in a worker thread and return early when the session is stopped.

    Closing an httpx client does not interrupt a server that has not sent its headers yet (a local model can
    think for minutes before that). The session thread must return on Stop regardless; the daemon worker is
    abandoned, and closing the client still asks the transport to abort when it can."""
    if stop.is_set():
        return None
    box: dict = {}
    done = threading.Event()

    def call() -> None:
        try:
            box["response"] = getattr(client, method)(url, **kwargs)
        except BaseException as e:  # noqa: BLE001 handed to the waiting thread
            box["error"] = e
        finally:
            done.set()

    threading.Thread(target=call, daemon=True, name="agent-api-request").start()
    while not done.wait(0.25):
        if stop.is_set():
            with contextlib.suppress(Exception):
                client.close()
            return None
    if stop.is_set():
        return None
    if "error" in box:
        raise box["error"]
    return box.get("response")

def context_window(client, base: str, options: dict, stop: threading.Event | None = None) -> int:
    """The model's context in tokens: what the user set, else what a llama.cpp server reports (/props), else a
    guess (local servers often run small contexts)."""
    try:
        if int(options.get("context_tokens") or 0) >= 2048:
            return int(options["context_tokens"])
    except (TypeError, ValueError):
        pass
    if is_local(base):
        try:
            r = _request_or_stop(stop, client, "get", re.sub(r"/v1$", "", base) + "/props", timeout=5) if stop else client.get(
                re.sub(r"/v1$", "", base) + "/props", timeout=5)
            n = (r.json().get("default_generation_settings") or {}).get("n_ctx") if r is not None and r.status_code == 200 else None
            if isinstance(n, int) and n >= 2048:
                return n
        except (httpx.HTTPError, ValueError, AttributeError):
            pass
        return 16384
    return 128000

def api_tool_schemas(fork: bool = False, analysis: bool = False) -> list[dict]:
    """The same project-scoped schemas the MCP server advertises, without a second handwritten registry."""
    return tools.tool_schemas(fork, analysis)

def _terminate(proc: subprocess.Popen) -> None:
    try:
        if WIN:
            # the agent with everything it started (node starts opencode.exe, ...): terminate() would only end the
            # first process and leave the rest working, and spending, in the background
            res = subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=30)
            if res.returncode != 0 and proc.poll() is None:
                proc.terminate()
        else:
            os.killpg(proc.pid, 15)
    except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
        pass

