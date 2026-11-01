"""The agent CLIs on this computer: which are installed, what models they offer, and how to start one.

decomp.wtf drives the agents the user already has (Claude Code, Antigravity CLI, Codex, opencode) and any
OpenAI-compatible endpoint. This is the catalog side of that: KINDS and the endpoint presets, what each CLI is called and
where it lives (search_path, _which), what models it reports (models, _config_model, CLAUDE_MODELS), and the argv that
starts one with decomp.anything's MCP server attached (argv). It runs a CLI to ask it these things, and nothing here runs
an agent."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

WIN = sys.platform == "win32"

class Fatal(RuntimeError):
    """A helper can't work until the user changes something (shown as the helper's error, not retried)."""

@dataclass
class Kind:
    id: str
    name: str
    command: str  # executable, "" for the built-in agent
    vendor: str
    install: dict[str, str]  # os -> command
    signin: str
    docs: str

KINDS = {
    "claude": Kind("claude", "Claude Code", "claude", "Anthropic",
                   {"unix": "curl -fsSL https://claude.ai/install.sh | bash",
                    "windows": "irm https://claude.ai/install.ps1 | iex"},
                   "Open a terminal, run `claude` and sign in with /login.", "https://docs.claude.com/en/docs/claude-code"),
    "codex": Kind("codex", "Codex CLI", "codex", "OpenAI",
                  {"unix": "npm install -g @openai/codex", "windows": "npm install -g @openai/codex"},
                  "Run `codex login` once in a terminal.", "https://github.com/openai/codex"),
    "antigravity": Kind("antigravity", "Antigravity CLI", "agy", "Google",
                        {"unix": "curl -fsSL https://antigravity.google/cli/install.sh | bash",
                         "windows": "irm https://antigravity.google/cli/install.ps1 | iex"},
                        "Open a terminal and run `agy` once to sign in.", "https://antigravity.google/docs/cli"),
    "opencode": Kind("opencode", "opencode", "opencode", "any provider (DeepSeek, Qwen, Kimi, ...)",
                     {"unix": "curl -fsSL https://opencode.ai/install | bash", "windows": "npm install -g opencode-ai"},
                     "Run `opencode auth login` once to add a provider (DeepSeek, OpenRouter, ...).", "https://opencode.ai"),
    "api": Kind("api", "API key", "", "DeepSeek, OpenRouter, Ollama, any OpenAI-compatible API", {}, "",
                "https://api-docs.deepseek.com"),
}

API_PRESETS = {
    "deepseek": {"name": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat", "key": True},
    "openrouter": {"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "model": "deepseek/deepseek-chat", "key": True},
    "ollama": {"name": "Ollama (local)", "base_url": "http://127.0.0.1:11434/v1", "model": "qwen3-coder", "key": False},
    "lmstudio": {"name": "LM Studio (local)", "base_url": "http://127.0.0.1:1234/v1", "model": "local-model", "key": False},
    "llamacpp": {"name": "llama.cpp (local)", "base_url": "http://127.0.0.1:8080/v1", "model": "local", "key": False},
    "openai": {"name": "OpenAI", "base_url": "https://api.openai.com/v1", "model": "gpt-5", "key": True},
    "custom": {"name": "Custom", "base_url": "", "model": "", "key": True},
}

# Google retired Gemini CLI on 2026-06-18; Antigravity CLI (`agy`) replaced it. Helpers and launcher choices saved
# by older versions as "gemini" carry on as Antigravity.
ALIASES = {"gemini": "antigravity", "agy": "antigravity"}

# decomp.anything's tools are loaded up front in Antigravity (tools.ALWAYS_LOAD) and need no permission. Should a helper
# reach one through agy's generic MCP dispatcher instead, a headless run can't ask, and agy quietly refuses it:
# this rule in the user's agy settings allows decomp.anything's tools (and nothing else).
AGY_ALLOW = "mcp(decomp/*)"

# Functions per helper session: a fresh session starts with a small, clean context (each function's assembly,
# drafts and diffs would otherwise ride along into the next one): cheaper per call, and the model stays sharp.
PER_SESSION = 2

def kind_id(kind: str) -> str:
    return ALIASES.get(kind, kind)

def agy_settings() -> Path:
    return Path.home() / ".gemini" / "antigravity-cli" / "settings.json"

def endpoint_entry(e: dict) -> dict:
    """A saved API endpoint, with what's left out filled in from its provider (a name: the provider's, or for a
    custom server the address's host, so sessions say which machine they run on)."""
    provider = e.get("provider") if e.get("provider") in API_PRESETS else "custom"
    preset = API_PRESETS[provider]
    base = (e.get("base_url") or preset["base_url"]).strip()
    host = re.sub(r"^\w+://", "", base).split("/", 1)[0]
    moved = provider == "custom" or base.rstrip("/") != preset["base_url"].rstrip("/")  # Ollama on another box...
    out = {"id": str(e.get("id") or ""), "provider": provider, "base_url": base,
           "model": (e.get("model") or preset["model"]).strip(),
           "name": (e.get("name") or "").strip() or (host if moved and host else preset["name"]) or "API"}
    for k in ("api_key", "context_tokens", "max_sessions"):
        if e.get(k):
            out[k] = e[k]
    return out

def is_local(base: str, resolve: bool = True) -> bool:
    """An address on this computer or the local network (a llama.cpp box, Ollama on another machine): a private,
    loopback, link-local or Tailscale address, a name without dots or under .local/.lan/.internal/.home.arpa, or
    (`resolve`) a name that resolves to such addresses only."""
    import ipaddress
    import socket

    host = re.sub(r"^\w+://", "", base.strip()).split("/", 1)[0].rsplit("@", 1)[-1]
    host = host[1:].split("]", 1)[0] if host.startswith("[") else host.split(":", 1)[0] if host.count(":") == 1 else host
    if not host:
        return False

    def private(ip: str) -> bool:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return False
        tailscale = a.version == 4 and a in ipaddress.ip_network("100.64.0.0/10")
        return a.is_private or a.is_loopback or a.is_link_local or tailscale or a.is_unspecified

    if private(host):
        return True
    if "." not in host or re.search(r"\.(local|lan|home|internal|localdomain|home\.arpa)$", host, re.IGNORECASE) \
            or host.lower() == "localhost":
        return True
    if not resolve or re.fullmatch(r"[\d.]+|[0-9a-fA-F:]+", host):
        return False
    try:
        addrs = {i[4][0] for i in socket.getaddrinfo(host, None)}
    except (OSError, UnicodeError):
        return False
    return bool(addrs) and all(private(a) for a in addrs)

# what an agent CLI says when nobody signed in to it (Claude Code: "Not logged in · Please run /login")
_SIGNED_OUT = re.compile(r"not logged in|run /login|invalid api key|not authenticated|please (?:log|sign) ?in|"
                         r"auth(?:entication)? method", re.IGNORECASE)

# what a model's provider says when the account has no credit left (DeepSeek: "Insufficient Balance", OpenAI:
# "insufficient_quota", Anthropic: "credit balance is too low"): trying again can't help until it's topped up
_NO_CREDIT = re.compile(r"insufficient[ _](?:balance|credits?|funds|quota)|exceeded your current quota|"
                        r"credit balance is too low|payment required|out of credits?", re.IGNORECASE)

# and when it's asked too much at once (HTTP 429): it works again in a while
_THROTTLED = re.compile(r"too many requests|rate[ _-]?limit|concurrency limit|\b429\b|overloaded", re.IGNORECASE)

def no_credit(said: str) -> str | None:
    """The helper's error when its model's provider says the account is out of credit, else None."""
    if not (m := _NO_CREDIT.search(said)):
        return None
    return (f"the model's provider says the account is out of credit (\u201c{m.group(0)}\u201d): top it up, then "
            "start the agents again")

# Claude Code's aliases always point at Anthropic's latest model of each kind.
_versions: dict[str, tuple[float, str | None]] = {}
_models: dict[str, tuple[float, list[dict]]] = {}
CLAUDE_MODELS = [("fable", "Fable", "Anthropic's most capable model"), ("opus", "Opus", "very capable"),
                 ("sonnet", "Sonnet", "fast and capable, cheaper"), ("haiku", "Haiku", "fastest, cheapest")]

def _codex_config() -> dict:
    """The user's Codex settings (~/.codex/config.toml, or $CODEX_HOME's)."""
    import tomllib

    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        return tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}

def _config_model(kind: str) -> str | None:
    """The model the user set as default in the agent's own settings, if any."""
    home = Path.home()
    try:
        if kind == "claude":
            return json.loads((home / ".claude" / "settings.json").read_text(encoding="utf-8")).get("model") or None
        if kind == "codex":
            return _codex_config().get("model") or None
        if kind == "antigravity":
            return json.loads(agy_settings().read_text(encoding="utf-8")).get("model") or None
        if kind == "opencode":
            base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
            return json.loads((base / "opencode" / "opencode.json").read_text(encoding="utf-8")).get("model") or None
    except (OSError, ValueError, AttributeError):
        return None
    return None

def models(kind: str, path: str | None) -> list[dict]:
    """Models to offer for this agent: [{"id", "name", "note"}]. Claude Code's aliases; opencode's and Antigravity's
    own lists (what the user's sign-in offers); for Codex the user's default plus any model name they type."""
    if kind == "claude":
        return [{"id": i, "name": n, "note": note} for i, n, note in CLAUDE_MODELS]
    if kind not in ("opencode", "antigravity") or not path:
        return []
    cached = _models.get(kind)
    if cached and time.time() - cached[0] < 600:
        return cached[1]
    try:
        res = subprocess.run(argv([path, "models"]), capture_output=True, text=True, encoding="utf-8", errors="replace",
                             stdin=subprocess.DEVNULL, timeout=30)
        lines = [line.strip() for line in res.stdout.splitlines()]
    except (OSError, subprocess.TimeoutExpired):
        lines = []
    if kind == "opencode":
        rows = [r for r in lines if re.fullmatch(r"[\w.-]+/[\w.:/-]+", r)]
        out = [{"id": r, "name": r.split("/", 1)[1], "note": r.split("/", 1)[0]} for r in rows[:200]]
    else:  # agy: "<slug>\t<name>" per model
        rows = [r.split("\t", 1) for r in lines if re.fullmatch(r"[\w.:-]+\t.+", r)]
        out = [{"id": slug, "name": name.strip(), "note": slug.split("-", 1)[0]} for slug, name in rows[:200]]
    _models[kind] = (time.time(), out)
    return out

def search_path() -> str | None:
    """Where to look for agents and npm (None: this process's PATH). On Windows also the PATH set for the user right
    now, read from the registry: an agent or Node.js installed while the app runs is found without restarting it."""
    if not WIN:
        return None
    import winreg

    dirs = os.environ.get("PATH", "").split(os.pathsep)
    for root, key in ((winreg.HKEY_CURRENT_USER, "Environment"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(root, key) as k:
                dirs += [os.path.expandvars(d) for d in str(winreg.QueryValueEx(k, "Path")[0]).split(";")]
        except OSError:
            pass
    return os.pathsep.join(dict.fromkeys(d for d in dirs if d))

def _which(cmd: str) -> str | None:
    found = shutil.which(cmd, path=search_path())
    if found:
        return found
    home = Path.home()
    dirs = [home / ".local" / "bin", home / ".claude" / "local", home / ".opencode" / "bin", home / ".npm-global" / "bin"]
    if WIN and os.environ.get("APPDATA"):
        dirs.append(Path(os.environ["APPDATA"]) / "npm")  # npm's global installs on Windows
    if WIN and os.environ.get("LOCALAPPDATA"):
        dirs.append(Path(os.environ["LOCALAPPDATA"]) / "agy" / "bin")  # where Antigravity's installer puts agy
    # on Windows the file without an extension next to npm's foo.cmd is a shell script Windows can't run
    names = (f"{cmd}.exe", f"{cmd}.cmd") if WIN else (cmd, f"{cmd}.cmd", f"{cmd}.exe")
    for d in dirs:
        for name in names:
            if (d / name).exists():
                return str(d / name)
    return None

# npm's Windows launcher (cmd-shim) ends with: "%_prog%" [node options] "%dp0%\node_modules\...\cli.js" %*
_NPM_SHIM = re.compile(r'"%_prog%"\s+(?P<opts>[^"\r\n]*?)\s*"%~?dp0%?\\(?P<script>[^"\r\n]+)"\s+%\*', re.IGNORECASE)

def argv(cmd: list[str]) -> list[str]:
    """The command to start, as Windows needs it. npm installs agents there as .cmd scripts, which run through cmd.exe:
    it cuts an argument at its first line break (a prompt loses everything after its first line, and the flags after
    it with it) and treats & | < > in it as commands. Start node with the agent's script instead, as the .cmd would."""
    if not WIN or Path(cmd[0]).suffix.lower() not in (".cmd", ".bat"):
        return cmd
    shim = Path(cmd[0])
    try:
        m = _NPM_SHIM.search(shim.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        m = None
    if not m:
        return cmd
    script = shim.parent / m["script"]
    node = shim.parent / "node.exe"
    node = str(node) if node.exists() else shutil.which("node", path=search_path())
    if not node or not script.exists():
        return cmd
    return [node, *m["opts"].split(), str(script), *cmd[1:]]

def signin(k: Kind) -> str:
    """How to sign in to an agent, said for where decomp.anything runs (WSL/Docker: in decomp.anything's own terminal)."""
    from .. import hostenv

    if not k.signin or hostenv.runtime() == "native":
        return k.signin
    text = k.signin.removeprefix("Open a terminal, ").removeprefix("Open a terminal and ")
    return f"{hostenv.terminal_hint()}, then: {text[0].lower()}{text[1:]}"

def version(kind: str, path: str) -> str | None:
    """What the agent's `--version` says (cached for 5 minutes), or None."""
    cached = _versions.get(kind)
    if cached is None or time.time() - cached[0] > 300:
        try:
            res = subprocess.run(argv([path, "--version"]), capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=15)
            said = (res.stdout or res.stderr).strip()
            _versions[kind] = (time.time(), said.splitlines()[0][:60] if said else None)
        except (OSError, subprocess.TimeoutExpired):
            _versions[kind] = (time.time(), None)
    return _versions[kind][1]

def major(said: str | None) -> int:
    """The major version in what `--version` said ("opencode v2.0.32" -> 2), 0 when there's none."""
    m = re.search(r"(\d+)\.\d+", said or "")
    return int(m.group(1)) if m else 0

def detect() -> list[dict]:
    out = []
    for k in KINDS.values():
        row = {"id": k.id, "name": k.name, "vendor": k.vendor, "signin": signin(k), "docs": k.docs,
               "install": k.install.get("windows" if WIN else "unix", "")}
        if not k.command:
            row.update(installed=True, version=None, path=None)
        else:
            path = _which(k.command)
            row.update(installed=path is not None, path=path, version=version(k.id, path) if path else None)
            if path:
                row.update(models=models(k.id, path), default_model=_config_model(k.id))
        if row["install"].startswith("npm ") and not row["installed"] and not shutil.which("npm", path=search_path()):
            row["needs"] = "Node.js (https://nodejs.org) for npm"
        elif k.id == "claude" and WIN and not shutil.which("git", path=search_path()):
            row["needs"] = "Git for Windows (https://git-scm.com/downloads/win): Claude Code needs its Git Bash"
        out.append(row)
    return out

