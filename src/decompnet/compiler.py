"""Sandboxed compilation of untrusted agent-submitted C.

Agents never get to run their own compiler for anything that counts: every score and
every match is produced here, on the coordinator, with the project's pinned toolchain.
"""

from __future__ import annotations

import collections
import hashlib
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from . import wsl

MAX_SOURCE_BYTES = 64 * 1024
# Per-compile resource caps (address space, CPU seconds, output file size) and a global
# concurrency cap, so a hostile submission can't take the host down with it.
LIMITS = ["--as=2147483648", "--cpu=30", "--fsize=67108864"]
_SLOTS = threading.BoundedSemaphore(max(2, (os.cpu_count() or 4) - 1))

_CACHE_SHARDS_N = 16
_CACHE_SHARDS: list[collections.OrderedDict[tuple, CompileResult]] = [collections.OrderedDict() for _ in range(_CACHE_SHARDS_N)]
_CACHE_LOCKS: list[threading.Lock] = [threading.Lock() for _ in range(_CACHE_SHARDS_N)]
_CACHE_PER_SHARD = 64

_SLOT_COUNT = max(2, (os.cpu_count() or 4) - 1)
_SLOT_QUEUE: queue.SimpleQueue[int] = queue.SimpleQueue()
for _i in range(_SLOT_COUNT):
    _SLOT_QUEUE.put(_i)

_WORKSPACES_ROOT: Path | None = None
_SLOT_LAST_CTX: dict[int, str] = {}
_SLOT_LAST_INCLUDES: dict[int, tuple[str, ...]] = {}


def _workspace_root() -> Path:
    global _WORKSPACES_ROOT
    if _WORKSPACES_ROOT is None:
        cand = (Path(f"/dev/shm/decompnet-{os.getuid()}") if Path("/dev/shm").is_dir()
                else Path(tempfile.gettempdir()) / f"decompnet-{os.getuid()}")
        cand.mkdir(parents=True, exist_ok=True)
        _WORKSPACES_ROOT = cand
    return _WORKSPACES_ROOT


def _cache_lookup(key: tuple) -> CompileResult | None:
    h = hash(key) % _CACHE_SHARDS_N
    with _CACHE_LOCKS[h]:
        shard = _CACHE_SHARDS[h]
        if key in shard:
            res = shard[key]
            shard.move_to_end(key)
            return CompileResult(res.ok, res.obj, res.log)
    return None


def _cache_store(key: tuple, res: CompileResult) -> None:
    h = hash(key) % _CACHE_SHARDS_N
    with _CACHE_LOCKS[h]:
        shard = _CACHE_SHARDS[h]
        shard[key] = res
        if len(shard) > _CACHE_PER_SHARD:
            shard.popitem(last=False)

_COMMENTS_AND_STRINGS = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', re.DOTALL)
_FORBIDDEN = [
    (re.compile(r"\b(__)?asm(__)?\b"), "inline assembly is not allowed: matches must come from C"),
    (re.compile(r"\b(GLOBAL_ASM|INCLUDE_ASM|INCLUDE_RODATA|NON_MATCHING)\b"), "asm-include macros are not allowed"),
    (re.compile(r"\.incbin\b"), ".incbin is not allowed"),
    (re.compile(r"__attribute__\s*\(\(\s*(section|naked)\b"), "section/naked attributes are not allowed"),
    (re.compile(r"(#\s*pragma|\b_Pragma)\b"), "pragmas are not allowed (ask the project admin to whitelist one)"),
    (re.compile(r"#\s*include\s*<"), "system includes are not allowed; everything you need is in context.h"),
]
_INCLUDE = re.compile(r'#\s*include\s*"([^"]+)"')


class SourceRejected(ValueError):
    pass


def check_source(source: str) -> None:
    if len(source.encode()) > MAX_SOURCE_BYTES:
        raise SourceRejected(f"source too large (max {MAX_SOURCE_BYTES} bytes)")
    spliced = re.sub(r"\\\r?\n", "", source)
    cleaned = re.sub(r'//[^\n]*|/\*.*?\*/', " ", spliced, flags=re.DOTALL)
    for inc in _INCLUDE.findall(cleaned):
        if inc.startswith(("/", "\\")) or ":" in inc or ".." in inc.replace("\\", "/").split("/"):
            raise SourceRejected(f"include path not allowed: {inc}")
    code = _COMMENTS_AND_STRINGS.sub(" ", spliced)
    for pattern, why in _FORBIDDEN:
        if pattern.search(code):
            raise SourceRejected(why)


@dataclass
class CompilerConfig:
    cmd: list[str]
    timeout: float = 15.0
    sandbox: str = "auto"  # auto | bwrap | docker | none
    image: str | None = None  # for sandbox = "docker"
    ro_paths: tuple[str, ...] = ()
    include_dirs: tuple[str, ...] = ()  # absolute paths of extra (static) header dirs
    runtime32: str | None = None  # 32-bit x86 C runtime for old i386 compiler binaries (see toolchains)
    wsl: bool = False  # a Linux toolchain on Windows: runs inside WSL 2, `cmd` holds WSL paths (see wsl.py)


@dataclass
class CompileResult:
    ok: bool
    obj: bytes | None
    log: str


def _sandbox_mode(cfg: CompilerConfig) -> str:
    if cfg.wsl:
        return "wsl"
    if cfg.sandbox != "auto":
        return cfg.sandbox
    return "bwrap" if shutil.which("bwrap") else "none"


def _root(cfg: CompilerConfig, work: Path) -> str:
    """How the compiler sees the work folder (in WSL it works on a copy, see wsl.run)."""
    mode = _sandbox_mode(cfg)
    return "/work" if mode in ("bwrap", "docker") else wsl.stage_dir() if mode == "wsl" else str(work)


def _wrap(cfg: CompilerConfig, work: Path, argv: list[str]) -> list[str]:
    mode = _sandbox_mode(cfg)
    if mode in ("none", "bwrap") and shutil.which("prlimit"):
        argv = ["prlimit", *LIMITS, "--", *argv]
    if mode == "none":
        return argv
    if mode == "docker":
        if not cfg.image:
            raise RuntimeError("sandbox = 'docker' needs compiler.image")
        return ["docker", "run", "--rm", "--network", "none", "--memory", "512m", "--pids-limit", "64",
                "-v", f"{work}:/work", "-w", "/work", cfg.image, *argv]
    if mode == "bwrap":
        # /tmp first, so read-only mounts of paths that live under /tmp are not hidden by it.
        cmd = ["bwrap", "--tmpfs", "/tmp", "--ro-bind", "/usr", "/usr", "--symlink", "usr/lib64", "/lib64",
               "--symlink", "usr/bin", "/bin", "--proc", "/proc", "--dev", "/dev"]
        if cfg.runtime32:
            # i386 binaries hardcode /lib/ld-linux.so.2; give them a private /lib with the pinned loader,
            # and point the loader at the pinned 32-bit libraries. 64-bit tools ignore those libraries.
            cmd += ["--dir", "/lib", "--ro-bind", f"{cfg.runtime32}/ld-linux.so.2", "/lib/ld-linux.so.2",
                    "--ro-bind", cfg.runtime32, "/opt/rt32", "--setenv", "LD_LIBRARY_PATH", "/opt/rt32"]
        else:
            cmd += ["--symlink", "usr/lib", "/lib"]
        for p in cfg.ro_paths:
            cmd += ["--ro-bind", p, p]
        cmd += ["--bind", str(work), "/work", "--chdir", "/work", "--unshare-all", "--die-with-parent", "--new-session"]
        return [*cmd, "--", *argv]
    raise RuntimeError(f"unknown sandbox mode {mode!r}")


def _exec(cfg: CompilerConfig, work: Path, argv: list[str], timeout: float, cwd: Path | None = None, **stage):
    """`stage`: in WSL, run on a copy in WSL's own file system (wsl.run's outputs, inputs and stage)."""
    if _sandbox_mode(cfg) == "wsl":
        return wsl.run(argv, cwd or work, timeout, **stage)
    # in the work folder, never in the app's own: KMC GCC's assembler (binutils 2.6) can't read its input when the
    # current directory is / ("Bad file descriptor"), which it is for decomp.wtf started by a service or in Docker
    return subprocess.run(_wrap(cfg, work, argv), cwd=cwd or work, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def run_in(cfg: CompilerConfig, work: Path, argv: list[str], timeout: float = 300, *,
           inputs: list[str] | None = None, outputs: list[str] | None = None) -> subprocess.CompletedProcess:
    """Run a toolchain command inside `work` (the sandbox's /work), e.g. to build a whole exported project.
    Paths in `argv` must be relative to `work`. In WSL, naming the files it reads (`inputs`) and writes (`outputs`)
    lets old 32-bit compilers work on a copy in WSL's own file system, which they need (see wsl.run)."""
    return _exec(cfg, Path(work), argv, timeout, cwd=Path(work),
                 **({} if outputs is None else {"inputs": inputs, "outputs": outputs}))


def clear_compile_cache() -> None:
    for h in range(_CACHE_SHARDS_N):
        with _CACHE_LOCKS[h]:
            _CACHE_SHARDS[h].clear()


def compile_source(cfg: CompilerConfig, context_header: str, source: str, trusted: bool = False) -> CompileResult:
    if not trusted:
        try:
            check_source(source)
        except SourceRejected as e:
            return CompileResult(False, None, f"rejected: {e}")

    cache_key = (
        tuple(cfg.cmd),
        cfg.include_dirs,
        hashlib.sha256((context_header + "\0" + source).encode()).digest(),
    )
    cached = _cache_lookup(cache_key)
    if cached is not None:
        return cached

    slot_id = _SLOT_QUEUE.get()
    try:
        work = _workspace_root() / f"slot_{slot_id}"
        work.mkdir(parents=True, exist_ok=True)
        inc = work / "include"
        inc.mkdir(exist_ok=True)

        if _SLOT_LAST_INCLUDES.get(slot_id) != cfg.include_dirs:
            for old in inc.iterdir():
                if old.is_dir():
                    shutil.rmtree(old)
                else:
                    old.unlink()
            for d in cfg.include_dirs:
                shutil.copytree(d, inc, dirs_exist_ok=True)
            _SLOT_LAST_INCLUDES[slot_id] = cfg.include_dirs

        # LF on every platform: to old GCCs a backslash before CRLF doesn't continue a line
        (inc / "context.h").write_text(context_header, encoding="utf-8", newline="\n")

        (work / "func.c").write_text('#include "context.h"\n#line 1 "func.c"\n' + source + "\n", encoding="utf-8",
                                     newline="\n")

        obj = work / "func.o"
        if obj.exists():
            obj.unlink()

        root = _root(cfg, work)
        subst = {"{src}": f"{root}/func.c", "{obj}": f"{root}/func.o", "{include}": f"{root}/include"}

        def expand(arg: str) -> str:
            # placeholders may be joined to flags (/Fo{obj}, -I{include}) or stand alone (-o {obj})
            for key, value in subst.items():
                arg = arg.replace(key, value)
            return arg

        argv = [expand(a) for a in cfg.cmd]
        try:
            with _SLOTS:
                proc = _exec(cfg, work, argv, cfg.timeout,
                             **({"outputs": ["func.o"], "stage": root} if _sandbox_mode(cfg) == "wsl" else {}))
        except subprocess.TimeoutExpired:
            return CompileResult(False, None, f"compiler timed out after {cfg.timeout}s")
        log = (proc.stdout + proc.stderr).replace(root + "/", "")
        if proc.returncode != 0 or not obj.exists():
            res = CompileResult(False, None, log[-4000:] or f"compiler exited with {proc.returncode}")
        else:
            res = CompileResult(True, obj.read_bytes(), log[-2000:])
    finally:
        _SLOT_QUEUE.put(slot_id)

    _cache_store(cache_key, res)
    return res
