"""REA's analysis tools, for decomp.wtf agents on PC targets and for the RE console.

REA (https://rea.tools) is a separate MCP server that inspects native binaries with Ghidra, IDA,
Hopper or Rizin (function inventories, pseudocode, cross references, strings, call graphs), and
Android, .NET, JavaScript/Electron, web and firmware targets. This module speaks MCP to ``rea mcp``
over stdio. Agents get a small read-only allowlist (`ALLOWED`); the console, driven by the user, can
call any tool. Nothing here is required for matching: without REA, PC programs still import and are
judged byte for byte.

Setup (optional): the console's REA settings (saved in rea.json next to the database), or
``DECOMP_REA`` set to the command (``DECOMP_REA="npx -y rea-agents@6.7.0 mcp"``), or ``rea`` on PATH,
or REA installed from Compilers & tools, or an ``rea`` server registered with Claude Code
(~/.claude.json), or npx. ``DECOMP_REA=off`` disables it.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import toolchains

PACKAGE = "rea-agents@6.7.0"  # the version tested with decomp.anything
PROVIDERS = ("ghidra", "hopper", "ida", "rizin")
CONFIG: Path | None = None  # rea.json, set by the app at start
_catalog_lock = threading.Lock()
_SECRET = re.compile(r"key|token|secret|password|auth|cookie|credential", re.IGNORECASE)

# Only read-only analysis. Nothing that opens files by arbitrary path after the initial target,
# executes code, writes annotations, or touches the network.
ALLOWED = {
    "open_binary", "binary_overview", "list_procedures", "analyze_function", "procedure_pseudo_code",
    "procedure_assembly", "procedure_info", "procedure_callers", "procedure_callees", "procedure_references",
    "xrefs", "search_procedures", "search_strings", "read_bytes", "list_segments", "list_names", "close_binary",
}
# Analysis projects never reopen or close the target under an agent; everything else here only reads it.
ANALYSIS_ALLOWED = (ALLOWED - {"open_binary", "close_binary"}) | {
    "list_procedures", "list_strings", "inspect_android_package", "search_android_classes", "inspect_android_class",
    "inspect_android_method", "inspect_managed_artifact", "inspect_managed_members", "inspect_managed_native_boundaries",
    "analyze_javascript_application", "inspect_analysis_view", "inspect_firmware_regions", "inspect_artifact",
    "inspect_pe_resources", "inspect_macho", "inspect_plist", "inspect_signature", "inspect_binary_layout",
    "inspect_native_data_type", "inspect_native_instruction", "inspect_address_context", "inspect_native_api",
    # native graph work: caller/callee graphs, literal and def-use traces, several functions decompiled in one
    # call, and masked byte search. All read the open target and write nothing.
    "batch_decompile", "get_call_graph", "trace_call_path", "trace_feature", "trace_native_values", "search_bytes",
    # a static trace through the application graph the project was opened with; it opens nothing and runs nothing
    "trace_application_feature",
    "binary_session", "get_evidence_bundle",
}
# What a native analysis provider looks for in the environment before REA will use it. Ghidra installed from
# Compilers & tools used to be invisible to REA unless the operator also typed GHIDRA_INSTALL_DIR by hand, so a
# pinned, sha256-verified Ghidra sat on disk while every native tool reported `not_configured` -- measured on a
# machine that had it. We seed what we installed ourselves; a value the operator set always wins.
#
# REA's own policy is that it never downloads an engine during an analysis operation, so an engine it supports
# stays missing unless something installs it -- and an engine we installed stays invisible unless we name it. The
# second half of that sentence is the whole point of this table: variable -> (toolchain id, shape), where shape is
# what REA expects to read: an install directory, a bin directory, a JAR, an interpreter, or a command in a venv.
OWN_PROVIDER_ENV = {"ghidra": ("GHIDRA_INSTALL_DIR", "ghidra")}
OWN_ENGINES = {
    "GHIDRA_INSTALL_DIR": ("ghidra", "dir"),
    "REA_WABT_BIN_DIRECTORY": ("wabt", "bindir"),
    "REA_JADX_MCP_JAR": ("jadx-mcp", "jar"),
    "REA_PWNTOOLS_PYTHON": ("pwntools", "venvpython"),
    "REA_MITMDUMP_COMMAND": ("mitmproxy", "venvbin:mitmdump"),
    "REA_APKTOOL_COMMAND": ("apktool", "path:bin/apktool"),
    "REA_ADB_PATH": ("adb", "path:platform-tools/adb"),
}

# The same engines as capability lanes, for the console: a missing Android decompiler should read as "install
# jadx-mcp", not as "this tool cannot do APKs".
ENGINE_LANES = {
    "ghidra": ("Ghidra", "native decompilation for almost any CPU"),
    "wabt": ("WABT", "WebAssembly modules: wasm2wat, wasm-decompile"),
    "jadx-mcp": ("jadx-headless-mcp", "Android APK code: Java and Kotlin read from DEX"),
    "pwntools": ("pwntools", "ELF segment and stack-canary layout"),
    "mitmproxy": ("mitmproxy", "live HTTPS capture for browser and native clients"),
    "apktool": ("apktool", "Android APK resources and AndroidManifest"),
    "adb": ("adb", "a connected Android device: install, pull, logcat"),
}

# Tools the host already provides. Finding one and saying nothing would repeat the Ghidra lesson in a different
# suit: REA reads its own environment, so a pcap reader that exists in /usr/bin is invisible until named.
SYSTEM_ENGINES = {
    "sigrok-cli": "REA_SIGROK_COMMAND",
    "tshark": "REA_TSHARK_COMMAND",
    "capinfos": "REA_CAPINFOS_COMMAND",
    "gdb": "REA_GDB_COMMAND",
}

SYSTEM_LANES = {
    "sigrok-cli": "decoded logic-analyser traces (UART, SPI, I2C)",
    "tshark": "reading pcap and pcapng captures",
    "capinfos": "a capture's shape before it is opened",
    "gdb": "observing a live native target instead of reading it statically",
}

# Engines we will never download. Commercial software on a coordinator is the operator's own licensing decision,
# so the useful thing left is to say what to run and which variable points REA at it.
FOREIGN_ENGINES = {
    "jeb": ("JEB", "commercial Android decompiler",
            "REA_JEB_MCP_URL", "JEB is commercial: run it yourself and point REA_JEB_MCP_URL at its MCP server."),
    "ida": ("IDA Pro", "native disassembly and decompilation through its MCP adapter",
            "REA_IDA_MCP_CONFIG", "IDA Pro needs its own MCP adapter (mrexodia/ida-pro-mcp); "
                                  "decomp.anything cannot install it."),
}

# Four of REA's *_PRLIMIT_COMMAND variables make util-linux a quiet prerequisite of whole lanes. Absent, the
# symptom is a lane refusing to start a child process, which reads as a REA bug rather than a missing package.
PRLIMIT_VAR = "prlimit (util-linux)"


def _windows() -> bool:
    """In a function so a POSIX test run can still check what we would choose on Windows."""
    return os.name == "nt"


def own_system(tid: str) -> str | None:
    """A tool the host provides, at the path we found it. Naming it is not a claim of ownership."""
    try:
        found = toolchains.system_binary(toolchains.get(tid))
    except (toolchains.ToolchainError, OSError):
        return None
    return str(found) if found else None

# Providers we cannot supply: they are configured outside decomp.anything, and saying so beats a silent absence.
FOREIGN_PROVIDERS = {
    "hopper": ("HOPPER_LAUNCHER_PATH", "Hopper is macOS: install it there and point HOPPER_LAUNCHER_PATH at it."),
    "ida": ("", "IDA Pro needs its own MCP adapter registered (mrexodia/ida-pro-mcp); decomp.anything cannot install it."),
}


def own_install(tid: str) -> str | None:
    """An analysis tool decomp.anything installed itself, at the path REA would want."""
    return own_engine(tid, "dir")


def own_engine(tid: str, shape: str) -> str | None:
    """The path REA would want for an engine we installed, or None while it is missing or its layout is wrong.
    A half-install is never offered as ready: an unusable path is worse than an obvious absence."""
    try:
        if toolchains.installed(tid) is None:
            return None
        home = toolchains.path(tid)
    except (toolchains.ToolchainError, OSError):
        return None
    if shape == "dir":
        # Ghidra's own layout is the proof, and it is exactly what REA's diagnostics look for
        if (home / "support" / "analyzeHeadless").exists() or (home / "support" / "analyzeHeadless.bat").exists():
            return str(home)
        return None
    if shape == "bindir":
        return str(home / "bin") if (home / "bin").is_dir() else None
    if shape == "jar":
        jars = sorted(home.glob("*.jar"))
        return str(jars[0]) if jars else None
    if shape == "venvpython":
        exe = toolchains.venv_bin(home / "venv", "python")
        return str(exe) if exe.exists() else None
    if shape.startswith("venvbin:"):  # a console script inside our own venv, e.g. mitmdump
        exe = toolchains.venv_bin(home / "venv", shape.split(":", 1)[1])
        return str(exe) if exe.exists() else None
    if shape.startswith("path:"):  # a file inside our install: a wrapper we wrote, or adb inside platform-tools
        rel = shape.split(":", 1)[1]
        # on Windows the .bat wrapper and adb.exe both sit next to the extension-less file we also placed
        names = [rel + ".exe", rel + ".bat", rel] if _windows() else [rel, rel + ".exe"]
        for name in names:
            if (home / name).exists():
                return str(home / name)
        return None
    return None


def provider_env() -> dict:
    """Environment additions for engines we installed. Empty when we have nothing to offer or the operator set
    the variable for the whole session, which still wins."""
    out = {}
    for var, (tid, shape) in OWN_ENGINES.items():
        if os.environ.get(var):
            continue
        found = own_engine(tid, shape)
        if found:
            out[var] = found
    for tid, var in SYSTEM_ENGINES.items():
        if os.environ.get(var):
            continue
        found = own_system(tid)
        if found:
            out[var] = found
    return out


def engines() -> list[dict]:
    """Each engine we can install, what it opens, and whether REA can use it right now."""
    rows = []
    for tid, (name, capability) in ENGINE_LANES.items():
        var, shape = next((k, v[1]) for k, v in OWN_ENGINES.items() if v[0] == tid)
        path_ = own_engine(tid, shape)
        rows.append({"tool": tid, "name": name, "capability": capability, "variable": var, "ready": path_ is not None,
                     "path": path_, "set_by": "decomp.anything" if path_ else ("operator" if os.environ.get(var) else None),
                     "fix": None if path_ or os.environ.get(var) else f"decompnet toolchain install {tid}"})
    for tid, capability in SYSTEM_LANES.items():
        path_ = own_system(tid)
        rows.append({"tool": tid, "name": tid, "capability": capability, "variable": SYSTEM_ENGINES[tid],
                     "ready": path_ is not None, "path": path_, "set_by": "host" if path_ else None,
                     "fix": None if path_ else f"install {tid} with your package manager: we will point REA at it"})
    if sys.platform.startswith("linux"):  # util-linux is a Linux thing; on Windows there is nothing to install
        limit = shutil.which("prlimit")
        rows.append({"tool": "prlimit", "name": PRLIMIT_VAR, "variable": None,
                     "capability": "resource limits around the firmware, sigrok, tshark and JavaScript lanes (Linux)",
                     "ready": limit is not None, "path": limit, "set_by": "host" if limit else None,
                     "fix": None if limit else "install util-linux: several REA lanes will not start a child without it"})
    for tid, (name, capability, var, why) in FOREIGN_ENGINES.items():
        set_by = "operator" if os.environ.get(var) else None
        rows.append({"tool": tid, "name": name, "capability": capability, "variable": var, "ready": set_by is not None,
                     "path": os.environ.get(var) or None, "set_by": set_by, "fix": None if set_by else why})
    return rows


def readiness() -> list[dict]:
    """Why each native provider can or cannot be used on this machine, and what would change it. Shown in the RE
    console: an analysis tool that is quietly absent reads as 'this tool cannot do native code'."""
    saved = {}
    if CONFIG is not None and CONFIG.exists():
        try:
            saved = json.loads(CONFIG.read_text(encoding="utf-8")).get("env") or {}
        except (OSError, ValueError):
            saved = {}
    rows = []
    for provider in PROVIDERS:
        var, tid = OWN_PROVIDER_ENV.get(provider, FOREIGN_PROVIDERS.get(provider, ("", "")))
        we_supply = provider in OWN_PROVIDER_ENV and own_engine(tid, "dir") is not None
        set_by = "decomp.anything" if we_supply and not os.environ.get(var) else (
            "operator" if os.environ.get(var) else ("saved settings" if var in saved else None))
        if provider == "rizin":  # no variable: REA finds it on PATH
            found, set_by = (shutil.which("rizin") is not None), ("PATH" if shutil.which("rizin") else None)
        else:
            found = bool(set_by)
        row = {"provider": provider, "ready": bool(found), "variable": var or None, "set_by": set_by}
        if not found:
            if provider in FOREIGN_PROVIDERS:
                row["fix"] = FOREIGN_PROVIDERS[provider][1]
            elif provider == "rizin":
                row["fix"] = "install rizin (Compilers & tools, or your package manager) and restart the server."
            else:
                row["fix"] = (f"install it from Compilers & tools (`decompnet toolchain install {provider}`); "
                              "decomp.anything will then point REA at it by itself.")
        rows.append(row)
    return rows


NATIVE_ALLOWED = (ALLOWED - {"open_binary", "close_binary"}) | {
    "binary_session", "batch_decompile", "get_call_graph", "trace_call_path", "trace_feature",
    "trace_native_values", "search_bytes", "inspect_native_api", "inspect_native_data_type",
    "inspect_address_context", "inspect_analysis_view", "get_evidence_bundle",
}


class ReaError(RuntimeError):
    pass


def _claude_code() -> dict | None:
    """An `rea` MCP server the user registered with Claude Code: the same command and environment."""
    try:
        data = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    servers = [data.get("mcpServers") or {}, *((p or {}).get("mcpServers") or {} for p in (data.get("projects") or {}).values())]
    for found in servers:
        rea = found.get("rea") if isinstance(found, dict) else None
        if isinstance(rea, dict) and rea.get("command"):
            env = {k: str(v) for k, v in (rea.get("env") or {}).items()}
            return {"command": [str(rea["command"]), *map(str, rea.get("args") or [])], "env": env}
    return None


def _installed_toolchain() -> dict | None:
    """REA installed from Compilers & tools (toolchains.install("rea")), with the environment an existing
    Claude Code registration already has (GHIDRA_INSTALL_DIR, JAVA_HOME...)."""
    try:
        if toolchains.installed("rea") is not None:
            env = (_claude_code() or {}).get("env") or {}
            return {"command": [str(toolchains.binary("rea")), "mcp"], "env": env}
    except (toolchains.ToolchainError, OSError, ValueError):
        return None
    return None


def settings() -> dict:
    """How REA starts, with the analysis providers' variables it needs filled in. An operator's own
    value -- in the console, in DECOMP_REA's process environment, or in a Claude Code registration -- always wins
    over the one we would supply."""
    res = _settings()
    if res.get("command") and res.get("source") != "off":
        res["env"] = {**provider_env(), **(res.get("env") or {})}
    return res


def _settings() -> dict:
    """How REA starts: {command, env, provider, source}. Saved console settings first, then DECOMP_REA,
    `rea` on PATH, REA from Compilers & tools, Claude Code's registration, and npx with the tested version."""
    saved: dict = {}
    if CONFIG is not None and CONFIG.exists():
        try:
            saved = json.loads(CONFIG.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
    out = {"command": None, "env": {}, "provider": saved.get("provider", "ghidra"), "source": None}
    env = os.environ.get("DECOMP_REA", "").strip()
    if env.lower() in ("off", "0", "false", "no"):
        return out | {"source": "off"}
    if saved.get("command"):
        return out | {"command": list(saved["command"]), "env": dict(saved.get("env") or {}), "source": "saved"}
    if env:
        return out | {"command": shlex.split(env), "source": "DECOMP_REA"}
    found = shutil.which("rea") or shutil.which("rea-agents")
    if found:
        return out | {"command": [found, "mcp"], "source": "PATH"}
    installed = _installed_toolchain()
    if installed:
        return out | installed | {"source": "installed"}
    claude = _claude_code()
    if claude:
        return out | claude | {"source": "Claude Code"}
    if shutil.which("npx"):
        return out | {"command": ["npx", "-y", PACKAGE, "mcp"], "source": "npx"}
    return out


def save(command: list[str] | str, env: dict | None = None, provider: str = "ghidra") -> dict:
    """Keep the console's REA settings (used by the agents' bridge too)."""
    if CONFIG is None:
        raise ReaError("decomp.wtf has no settings folder")
    argv = shlex.split(command) if isinstance(command, str) else [str(a) for a in command]
    if provider and provider not in PROVIDERS:
        raise ReaError(f"provider must be one of {', '.join(PROVIDERS)}")
    clean = {str(k).strip(): str(v) for k, v in (env or {}).items() if str(k).strip()}
    CONFIG.write_text(json.dumps({"command": argv, "env": clean, "provider": provider}, indent=1), encoding="utf-8")
    return settings()


def public(cfg: dict | None = None) -> dict:
    """Settings safe to show in the page: values of variables that look like secrets are hidden."""
    cfg = cfg or settings()
    return {**cfg, "env": {k: ("•••" if _SECRET.search(k) else v) for k, v in cfg["env"].items()}}


def command() -> list[str] | None:
    """How to start REA's MCP server, or None when it isn't configured on this machine."""
    return settings()["command"]


def unwrap(result):
    """REA wraps every answer in an evidence record; the answer itself is its normalized_result."""
    if isinstance(result, dict) and "normalized_result" in result and "operation" in result:
        return result["normalized_result"]
    return result


def _error(text: str) -> str:
    """REA's error JSON as a sentence: the message, and what to do about it when it says."""
    try:
        err = json.loads(text).get("error") or {}
    except (ValueError, AttributeError):
        return text.strip()
    message = str(err.get("message") or text).strip()
    action = str((err.get("remediation") or {}).get("action") or "").strip()
    return message if not action or action == message else f"{message} {action}"


class Client:
    """A minimal MCP client: initialize, then tools/call by name."""

    # the tool catalog is cached on the client for CACHE_TTL; declared so the attribute exists as a type, not just
    # as something setattr'd onto an instance somewhere below
    _agent_catalog: tuple[float, list[dict], Any]

    def __init__(self, argv: list[str] | None = None, timeout: float = 600, env: dict | None = None,
                 provider: str | None = None):
        cfg = settings() if argv is None else {"command": argv, "env": env or {}, "provider": provider}
        argv = cfg["command"]
        if not argv:
            raise ReaError("REA isn't set up: set its command in the RE console's settings (or install rea-agents)")
        extra = {**cfg.get("env", {}), **(env or {})}
        if cfg.get("provider") and "REA_ANALYSIS_PROVIDER" not in extra:
            extra["REA_ANALYSIS_PROVIDER"] = cfg["provider"]
        self.argv, self.timeout = argv, timeout
        self._id = 0
        self._lock = threading.Lock()
        self._lines: queue.Queue[str | None] = queue.Queue()
        try:
            self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                         text=True, encoding="utf-8", errors="replace", bufsize=1,
                                         env={**os.environ, **extra, "NO_COLOR": "1"})
        except OSError as e:
            raise ReaError(f"REA couldn't start ({' '.join(argv)}): {e}") from e
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self._initialize()
        except BaseException:
            self.close()
            raise

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if line:
                self._lines.put(line)
        self._lines.put(None)

    def _send(self, message: dict) -> None:
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self.proc.stdin.flush()
        except OSError as e:
            raise ReaError(f"REA exited: {e}") from e

    def _request(self, method: str, params: dict | None = None, timeout: float | None = None) -> dict:
        with self._lock:
            self._id += 1
            ident = self._id
            self._send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}})
            deadline = time.time() + (timeout or self.timeout)
            while True:
                left = deadline - time.time()
                if left <= 0:
                    raise ReaError(f"REA did not answer {method} in {timeout or self.timeout:.0f}s")
                try:
                    line = self._lines.get(timeout=left)
                except queue.Empty:
                    raise ReaError(f"REA did not answer {method} in {timeout or self.timeout:.0f}s") from None
                if line is None:
                    self._lines.put(None)
                    raise ReaError("REA exited")
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("id") == ident and "method" not in msg:
                    if "error" in msg:
                        raise ReaError(str(msg["error"].get("message") or msg["error"]))
                    return msg.get("result") or {}
                if "method" in msg and "id" in msg:  # a server request (ping, roots): answer minimally
                    self._send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})

    def _initialize(self) -> None:
        result = self._request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                              "clientInfo": {"name": "decomp.wtf", "version": "1"}}, timeout=30)
        info = result.get("serverInfo") if isinstance(result, dict) else None
        self.server_info = {k: info[k] for k in ("name", "version") if isinstance(info.get(k), str)} \
            if isinstance(info, dict) else {}
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def call(self, tool: str, arguments: dict | None = None, timeout: float | None = None, *,
             any_tool: bool = False, envelope: bool = False) -> Any:
        """A tool's answer (its normalized_result). `any_tool`: the console, past the agents' allowlist;
        `envelope`: the whole evidence record (provider, confidence, limitations) instead of the answer.

        The shape is whatever the tool returned: REA answers in JSON of its own choosing, and each tool picks its
        own, so this is deliberately `Any` rather than a dict that would be wrong half the time."""
        if tool not in ALLOWED and not any_tool:
            raise ReaError(f"REA tool {tool!r} is not on decomp.wtf's allowlist")
        result = self._request("tools/call", {"name": tool, "arguments": arguments or {}}, timeout=timeout)
        if result.get("isError"):
            text = " ".join(c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict))
            raise ReaError(_error(text) or f"REA's {tool} failed")
        if "structuredContent" in result:
            value = result["structuredContent"]
        else:
            text = "".join(c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict))
            try:
                value = json.loads(text)
            except ValueError:
                value = text
        return value if envelope else unwrap(value)

    def tools(self) -> list[dict]:
        """REA's whole catalog: name, title, description, input schema and hints, for every tool."""
        out, cursor = [], None
        for _ in range(50):
            page = self._request("tools/list", {"cursor": cursor} if cursor else {}, timeout=60)
            out += page.get("tools") or []
            cursor = page.get("nextCursor")
            if not cursor:
                break
        return out

    def alive(self) -> bool:
        return self.proc.poll() is None

    def close(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            with contextlib.suppress(OSError):
                self.proc.kill()
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                self.proc.wait(timeout=5)

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ------------------------------------------------------------------ what the decomp tools use

_clients: dict[str, Client] = {}
_opened: dict[str, str] = {}
_cache: dict[tuple[str, int], str] = {}
_binary_locks: dict[str, threading.Lock] = {}
_binary_locks_lock = threading.Lock()


def _binary_lock(key: str) -> threading.Lock:
    with _binary_locks_lock:
        lock = _binary_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _binary_locks[key] = lock
        return lock


def _client(binary: Path) -> Client:
    key = str(binary.resolve())
    client = _clients.get(key)
    if client is None or client.proc.poll() is not None:
        client = Client()
        _clients[key] = client
    if _opened.get(key) != key:
        client.call("open_binary", {"path": key}, timeout=900)
        _opened[key] = key
    return client


def _procedure(fn, base: int = 0) -> str:
    """The procedure string REA wants: the project's address plus its image base (Ghidra loads a file at 0x100000 and up)."""
    return f"0x{int(fn['vram']) + int(base or 0):X}"


def draft(binary: Path, fn, timeout: float = 60) -> str | None:
    """REA/Ghidra pseudocode as a starting draft, if REA answers in time. Never blocks the claim loop
    for long: a cold Ghidra import takes minutes, and agents can call inspect_binary themselves."""
    if os.environ.get("DECOMP_REA_DRAFTS", "").lower() not in ("1", "on", "yes", "true"):
        return None  # a cold Ghidra import takes minutes; agents ask for a dossier with inspect_binary instead
    bpath = Path(binary).resolve()
    key = (str(bpath), int(fn["vram"]))
    if key in _cache:
        return _cache[key]
    if command() is None:
        return None
    try:
        with _binary_lock(str(bpath)):
            text = _client(bpath).call("procedure_pseudo_code", {"procedure": _procedure(fn)},
                                      timeout=timeout)
    except (ReaError, OSError):
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    result = ("/* draft from REA's decompiler (Ghidra) via MCP: a starting point, not a match */\n"
              + text.strip() + "\n")
    _cache[key] = result
    return result


def inspect(binary: Path, fn, symbols, what: str = "", timeout: float = 900) -> str:
    """A function dossier from REA for the inspect_binary tool: pseudocode, signature, callers, references."""
    what = (what or "all").lower()
    parts: list[str] = []
    bpath = Path(binary).resolve()
    with _binary_lock(str(bpath)):
        client = _client(bpath)
        proc = _procedure(fn)
        if what in ("all", "info"):
            dossier = client.call("analyze_function", {"procedure": proc, "detail": "complete"}, timeout=timeout)
            if isinstance(dossier, dict):
                info = dossier.get("procedure") or dossier.get("function") or {}
                if isinstance(info, dict):
                    sig = info.get("signature") or info.get("name")
                    if sig:
                        parts.append(f"signature: {sig}")
        if what in ("all", "pseudocode", "code"):
            code = client.call("procedure_pseudo_code", {"procedure": proc}, timeout=timeout)
            if isinstance(code, str) and code.strip():
                parts += ["", "REA pseudocode (Ghidra; a draft, not the original source):", "```c", code.strip(), "```"]
        if what in ("all", "assembly"):
            asm = client.call("procedure_assembly", {"procedure": proc}, timeout=timeout)
            if isinstance(asm, str) and asm.strip():
                parts += ["", "REA assembly:", "```asm", asm.strip(), "```"]
        if what in ("all", "callers"):
            callers = client.call("procedure_callers", {"procedure": proc}, timeout=timeout)
            if callers:
                parts += ["", "callers: " + _addr_list(callers, symbols)]
        if what in ("all", "callees"):
            callees = client.call("procedure_callees", {"procedure": proc}, timeout=timeout)
            if callees:
                parts += ["", "callees: " + _addr_list(callees, symbols)]
        if what in ("all", "references"):
            refs = client.call("procedure_references", {"procedure": proc, "direction": "outgoing"}, timeout=timeout)
            if refs:
                parts += ["", "references: " + _addr_list(refs, symbols)]
    return "\n".join(parts) or f"REA returned nothing for {what or 'this function'}."


def _parse_addr(addr) -> int | None:
    if addr is None:
        return None
    try:
        return int(addr, 0) if isinstance(addr, str) else int(addr)
    except (TypeError, ValueError):
        return None


def search(binary: Path, query: str, symbols, timeout: float = 900) -> str:
    """Names and strings in the binary, through REA: the fastest way to find what a game calls."""
    bpath = Path(binary).resolve()
    with _binary_lock(str(bpath)):
        client = _client(bpath)
        rows = []
        try:
            found = client.call("search_strings", {"pattern": query}, timeout=timeout)
            for row in (found if isinstance(found, list) else [])[:40]:
                value = row.get("value") or row.get("text") if isinstance(row, dict) else row
                addr = _parse_addr(row.get("address") if isinstance(row, dict) else None)
                rows.append(f"- string {symbols.label(addr) if addr is not None else ''}: {str(value)[:160]}")
        except (ReaError, OSError):
            pass
        try:
            found = client.call("search_procedures", {"pattern": query}, timeout=timeout)
            for row in (found if isinstance(found, list) else [])[:40]:
                addr_raw, value = (row.get("address"), row.get("value") or row.get("name")) if isinstance(row, dict) else (None, row)
                addr = _parse_addr(addr_raw)
                rows.append(f"- procedure {symbols.label(addr) if addr is not None else ''}: {value}")
        except (ReaError, OSError):
            pass
    return "\n".join(rows) or f"REA found no string or procedure matching {query!r}."


def _dict(value) -> dict:
    """REA's JSON is loose: a field that should be an object arrives as null, a list or a string often enough that
    every reader needs the same guard. Saying it once keeps the guards honest instead of half-applied."""
    return value if isinstance(value, dict) else {}


def _addr_list(value, symbols) -> str:
    rows = value if isinstance(value, list) else value.get("addresses") or value.get("references") or []
    out = []
    for row in rows[:40]:
        addr = row.get("address") if isinstance(row, dict) else row
        if addr is None:
            continue
        try:
            at = int(addr, 0) if isinstance(addr, str) else int(addr)
        except (TypeError, ValueError):
            continue
        out.append(symbols.label(at))
    return ", ".join(out) or str(value)[:500]


atexit.register(lambda: close_all())


_analysis_clients: dict[tuple, Client] = {}
_analysis_results: dict[tuple, object] = {}
_analysis_records: dict[tuple, object] = {}  # the whole evidence record, for tools that take it inline
_analysis_lock = threading.Lock()


def _target_key(target: dict, provider: str = "") -> tuple:
    import json

    return (str(target.get("path")), str(target.get("tool") or "open_binary"),
            json.dumps(target.get("arguments") or {}, sort_keys=True), provider or "")


def open_analysis(target: dict, provider: str = "") -> tuple[Client, object]:
    """The REA process for an analysis project's target, opened with the tool the user picked, and its answer."""
    key = _target_key(target, provider)
    with _analysis_lock:
        client = _analysis_clients.get(key)
        if client is None or not client.alive():
            client = Client(provider=provider or None, timeout=1800)
            tool = str(target.get("tool") or "open_binary")
            args = dict(target.get("arguments") or {})
            arg = "input_path" if tool == "analyze_javascript_application" else "path"
            if tool != "inspect_artifact":
                args.setdefault(arg, str(target["path"]))
            if tool == "open_binary" and provider and "provider_id" not in args:
                args["provider_id"] = provider
            try:
                record = client.call(tool, args, any_tool=True, timeout=1800, envelope=True)
                _analysis_records[key] = record
                _analysis_results[key] = unwrap(record)
            except BaseException:
                client.close()
                raise
            _analysis_clients[key] = client
        return client, _analysis_results.get(key)

def analysis_client(target: dict, provider: str = "") -> Client:
    return open_analysis(target, provider)[0]


def agent_client(target: dict, provider: str = "", *, native: bool = False, connect: bool = False) -> Client | None:
    """Reuse the project's session; discovery alone does not start a cold native import."""
    if os.environ.get("DECOMP_REA", "").strip().lower() in ("off", "0", "false", "no"):
        raise ReaError("REA is disabled by DECOMP_REA; use the available project skills and findings")
    if native:
        key = str(Path(target["path"]).resolve())
        client = _clients.get(key)
        if connect:
            with _binary_lock(key):
                return _client(Path(key))
    else:
        client = _analysis_clients.get(_target_key(target, provider))
        if connect:
            return analysis_client(target, provider)
    return client if client is not None and client.alive() else None


def agent_catalog(client: Client, allowed: set[str], refresh: bool = False) -> dict:
    """Actual schemas and session availability, restricted to the agent's read-only tools."""
    with _catalog_lock:
        cached = getattr(client, "_agent_catalog", None)
        if refresh or cached is None or time.monotonic() - cached[0] > 300:
            specs = client.tools()
            availability = {}
            if any(s.get("name") == "binary_session" for s in specs):
                try:
                    session = client.call("binary_session", {}, any_tool=True)
                    if isinstance(session, dict):
                        session = session.get("result", session)
                        availability = session.get("tool_availability", {}) if isinstance(session, dict) else {}
                except ReaError as e:
                    availability = {"_error": str(e)}
            client._agent_catalog = (time.monotonic(), specs, availability)
        _, specs, availability = client._agent_catalog
    if isinstance(availability, list):
        availability = {a.get("name", a.get("tool")): a for a in availability if isinstance(a, dict)}
    availability = availability if isinstance(availability, dict) else {}
    tools = []
    for spec in specs:
        name = spec.get("name")
        if name not in allowed:
            continue
        state = availability.get(name, {})
        state = state if isinstance(state, dict) else {"available": state} if isinstance(state, bool) else {}
        tools.append({"name": name, "description": spec.get("description", ""),
                      "inputSchema": spec.get("inputSchema") or {}, "available": state.get("available"),
                      "availability": state})
    return {"status": "connected", "tools": tools,
            "availability_note": availability.get("_error") or
            ("Unreported availability is unknown; an advertised tool may still need a provider." if not availability else "")}


def evidence_metadata(record) -> dict:
    """Keep the provenance and limits that agents need when saving findings or following retained evidence."""
    if not isinstance(record, dict) or "normalized_result" not in record:
        return {}
    return {k: record[k] for k in ("evidence_id", "operation", "provider", "confidence", "authority", "limitations",
                                 "subject", "retention", "analysis_profile") if k in record}


def analysis_result(target: dict, provider: str = "") -> object:
    return open_analysis(target, provider)[1]


def analysis_evidence(target: dict, provider: str = "") -> object | None:
    """The evidence record the target was opened with (what REA's graph tools take as their input)."""
    return _analysis_records.get(_target_key(target, provider))


def trace_feature(target: dict, provider: str, seed: dict, direction: str = "both", timeout: float = 1800) -> object:
    """Trace one literal seed through the JavaScript application graph the project was opened with.

    REA keeps the opening evidence in the session, so the trace usually references it by id; a server that
    doesn't retain it gets the complete record instead. The answer is the whole evidence record.
    """
    client = analysis_client(target, provider)
    record = analysis_evidence(target, provider)
    if not isinstance(record, dict) or not record:
        raise ReaError("REA has no application graph for this project; reopen the target with "
                       "analyze_javascript_application first")
    args: dict = {"seed": seed, "direction": direction}
    if record.get("evidence_id"):
        args["application"] = {"kind": "retained-evidence", "evidence_id": record["evidence_id"]}
        try:
            return client.call("trace_application_feature", args, any_tool=True, envelope=True, timeout=timeout)
        except ReaError as e:
            # Some servers don't retain opening evidence across calls; them, and only them, get the record inline.
            if not any(word in str(e).lower() for word in ("evidence", "retain", "missing")):
                raise
    args["application"] = record
    return client.call("trace_application_feature", args, any_tool=True, envelope=True, timeout=timeout)


def close_all() -> None:
    with _binary_locks_lock:
        for client in _clients.values():
            client.close()
        _clients.clear()
        _opened.clear()
        _binary_locks.clear()
    with _analysis_lock:
        for client in _analysis_clients.values():
            client.close()
        _analysis_clients.clear()
        _analysis_results.clear()
        _analysis_records.clear()
def pseudo(binary: Path, fn, timeout: float = 600, *, base: int = 0) -> str | None:
    """REA's decompiler output for one function, whatever the environment says (the autopilot asked for it)."""
    if command() is None:
        return None
    bpath = Path(binary).resolve()
    try:
        with _binary_lock(str(bpath)):
            text = _client(bpath).call("procedure_pseudo_code", {"procedure": _procedure(fn, base)}, timeout=timeout)
    except (ReaError, OSError):
        return None
    return text.strip() if isinstance(text, str) and text.strip() else None


def dossier(binary: Path, fn, timeout: float = 900, *, base: int = 0) -> dict | None:
    """REA's reading of one function as data: signature, calling convention, callees, the strings and names it refers to.

    `detail: complete` is what carries them (the default answer is a summary view); a provider that doesn't know the
    option is asked again without it. One call per function: callers keep the result, because a Ghidra call is slow.
    Raises ReaError when REA answers with an error, so the caller can say why instead of pretending there was nothing."""
    if command() is None:
        return None
    bpath = Path(binary).resolve()
    proc = _procedure(fn, base)
    try:
        with _binary_lock(str(bpath)):
            client = _client(bpath)
            record = client.call("analyze_function", {"procedure": proc, "detail": "complete"}, timeout=timeout)
            if not isinstance(record, dict) or not (record.get("procedure") or record.get("callees")):
                record = client.call("analyze_function", {"procedure": proc}, timeout=timeout)
    except OSError:
        return None
    if not isinstance(record, dict):
        return None
    info = _dict(record.get("procedure"))
    native = _dict(record.get("native_api"))

    def named(*keys) -> list[str]:
        out: list[str] = []
        for key in keys:
            for row in record.get(key) or ():
                if isinstance(row, dict):
                    entry = _dict(row)
                    text = entry.get("value") or entry.get("name") or _dict(entry.get("symbol")).get("name") or ""
                else:
                    text = row
                text = str(text or "").strip()
                if text and text not in out:
                    out.append(text)
        return out

    return {"name": str(info.get("name") or ""), "signature": str(info.get("signature") or ""),
            "calling_convention": str(native.get("calling_convention") or ""),
            "callees": named("callees", "unresolved_calls"), "callers": named("callers"),
            "strings": named("referenced_strings", "referenced_names", "strings"),
            "provider": "REA", "confidence": "observed", "at": time.time()}


