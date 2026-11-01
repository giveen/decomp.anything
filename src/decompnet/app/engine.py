"""The local engine: ROM projects on top of the coordination core.

Network (claims, attempts, best attempts, notes, a regression-tested shared header) already does the bookkeeping
several agents need to work on one codebase. A ROM project adds:

- targets taken straight from the ROM (splat), matched by linking the candidate at the function's address
  (rom.match), so a match is byte-identical code;
- a symbol table, so agents can name things (rename_symbol) without breaking matches;
- tools for what ROMs need: split a function the disassembler merged, read data, see who calls what.

Everything stays on this computer.

This module is what every part of a project shares: opening a project (project_row, cfg), the ROM and its functions,
the symbol table, the target a function is judged against, and the smallest agent tools (drafts, notes, budget, the
chat agent). Everything else is a mixin beside it, by the job it does: creation imports, contents and prefill carry
what an analysis agent reads, sourcefiles holds recovered and reconstructed code, intake reports what an import found,
romedit changes a split, roomops applies what a room's members did, autopilot tries drafts, views answer the page. They
are one class at runtime, so a tool in one part reaches a helper in another with a plain self. call. Each method that
overrides a Network one has exactly one owner here: super() from it still lands on Network (see
tests/test_engine_parts.py).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import struct
import threading
import time
from pathlib import Path

from .. import rea, seeds, similarity
from ..elfobj import FunctionObj
from ..network import Agent, Network, NetworkError, _hash_token
from ..rom import match, split
from ..rom.symbols import ADDRESS, AUTO_TOKEN, Symbol, SymbolTable
from .autopilot import AutopilotMixin
from .contents import ContentsMixin
from .creation import CreationMixin
from .features import FeaturesMixin
from .intake import IntakeMixin
from .platform import (
    BASE_HEADER,
    COMPILERS,
    MW_VERSIONS,
    PLATFORM_STATUS,
    SHORT_NAMES,
    TOOLS,
    X86_PLATFORMS,
    source_language,
)
from .prefill import PrefillMixin
from .reafacts import ReaFactsMixin
from .romedit import RomEditMixin
from .roomops import RoomOpsMixin
from .sourcefiles import SourceFilesMixin
from .views import ViewsMixin

__all__ = [
    "BASE_HEADER",
    "COMPILERS",
    "MW_VERSIONS",
    "PLATFORM_STATUS",
    "SHORT_NAMES",
    "TOOLS",
    "RomNetwork",
]


def _stored(text: str | None, what: str, default: str = "{}"):
    """A JSON column (a project's config, a function's meta or relocs) as the value it holds.

    Text this database wrote is trusted, so the normal path is a plain parse; a row that does not parse is reported as
    the project's problem, with what was being read, instead of as a JSONDecodeError from inside an agent tool."""
    try:
        return json.loads(text or default)
    except ValueError as e:
        raise NetworkError(f"{what} is not valid JSON: {e}") from None


class RomNetwork(
    CreationMixin,
    ContentsMixin,
    PrefillMixin,
    FeaturesMixin,
    SourceFilesMixin,
    IntakeMixin,
    RomEditMixin,
    RoomOpsMixin,
    AutopilotMixin,
    ReaFactsMixin,
    ViewsMixin,
    Network,
):
    def __init__(self, db_path: str | Path, workspace: Path, claim_ttl: int = 30 * 60, agent_secret: str | None = None):
        super().__init__(db_path, claim_ttl=claim_ttl)
        self.workspace = Path(workspace)
        # Stable for the install (CLI: .decomp/agent.secret): a helper's MCP bridge must keep working when the
        # app restarts. Tests and tools without a secret get a per-process one.
        self._agent_secret = agent_secret or secrets.token_urlsafe(32)
        self._syms: dict[int, SymbolTable] = {}
        self._sym_override: dict[int, SymbolTable] = {}
        self._roms: dict[int, tuple[bytes, list[split.Segment]]] = {}
        self._xrefs: dict[int, dict] = {}
        self._binary_tier_seen: set[int] = set()  # analysis projects whose Sources tier was worked out in this process
        # analysis project + work item -> rendered batch_decompile prefill, so viewing a survey or task again
        # doesn't decompile the same functions again in this process
        self._analysis_prefill_cache: dict[tuple, dict] = {}
        # analysis project + work item -> event: the batches being decompiled right now, so a second request (the
        # drawer's button while the import's background job runs) waits for that one instead of decompiling again
        self._analysis_prefill_running: dict[tuple, threading.Event] = {}
        self._analysis_prefill_lock = threading.Lock()
        self._seed_locks: dict[int, threading.Lock] = {}
        self._seed_locks_lock = threading.Lock()
        self._last_expire_time: float = 0.0
        # ponytail: one archive extraction/import at a time. It is user-triggered and rare; this closes the
        # same-entry race between two tabs without a per-entry lock map.
        self._extract_lock = threading.Lock()
        self.links: dict = {}  # slug -> collab.Link, for projects joined from a friend's room
        self.rooms: dict = {}  # slug -> collab.Room, for projects this computer shares
        self._remote_ids: dict[str, int] = {}
        self._ops: dict[int, dict] = {}  # project id -> look-alike index
        self._diffs: dict[tuple, dict] = {}  # (function, source, context, compiler) -> diff for the GUI
        self._diffs_lock = threading.Lock()
        self.autopilots: dict[str, dict] = {}  # slug -> progress of trying every function's machine draft
        from .forkengine import ForkEngine  # forks build on this class's claims, attempts and live view
        from .permute import Permuter
        from .twins import Twins

        self.forks = ForkEngine(self)
        self.twins = Twins(self)
        self.permuter = Permuter(self)

    def _require_budget(self, agent: Agent) -> None:
        """Your machine, your agent: no compile budget."""

    @staticmethod
    def cfg(proj) -> dict:
        return _stored(proj["config"], "a project's config")

    def is_rom(self, proj) -> bool:
        return self.cfg(proj).get("kind") == "rom"

    def is_analysis(self, proj) -> bool:
        return self.cfg(proj).get("kind") == "analysis"

    def project_row(self, slug: str):
        return self._project(slug)

    def fork_slug(self, project: str | None) -> str | None:
        """The slug when `project` is a fork of a public decomp, else None."""
        try:
            proj = self._project(project)
        except NetworkError:
            return None
        return proj["slug"] if self.cfg(proj).get("kind") == "fork" else None

    def _rom_only(self, project: str | None, what: str) -> None:
        if self.fork_slug(project):
            raise NetworkError(
                f"{what} is for ROM projects. In a fork, the project's own headers and source files are "
                "the shared context: use read_file, search_code and edit_file"
            )

    def symbols(self, project_id: int) -> SymbolTable:
        if project_id in self._sym_override:
            return self._sym_override[project_id]
        if project_id not in self._syms:
            rows = self.store.all("SELECT name, address, size, kind FROM symbols WHERE project_id=?", project_id)
            self._syms[project_id] = SymbolTable([Symbol(r["name"], r["address"], r["size"], r["kind"]) for r in rows])
        return self._syms[project_id]

    def _rom(self, proj) -> tuple[bytes, list[split.Segment]]:
        if proj["id"] not in self._roms:
            cfg = self.cfg(proj)
            rom = (Path(proj["root"]) / cfg["rom"]["file"]).read_bytes()
            segs = [split.Segment(**s) for s in cfg["segments"]]
            self._roms[proj["id"]] = (rom, segs)
        return self._roms[proj["id"]]

    def reader(self, proj):
        rom, segs = self._rom(proj)

        def read(addr: int, n: int) -> bytes | None:
            for seg in segs:
                if seg.contains(addr, n):
                    at = seg.rom_start + addr - seg.vram
                    return rom[at : at + n]
            return None

        return read

    def _function(self, name: str, project: str | None):
        """Functions can also be named by address (0x80001234), handy for agents reading assembly, and the way
        agents write names they read elsewhere (see _loose_functions)."""
        m = ADDRESS.match(name.strip()) if name else None
        if m:
            proj = self._project(project)
            row = self.store.one("SELECT * FROM functions WHERE project_id=? AND vram=?", proj["id"], int(m.group(1), 16))
            if row is not None:
                return row
        try:
            return super()._function(name, project)
        except NetworkError as e:
            rows = self._loose_functions(name, project) if name else []
            if len(rows) == 1:
                return rows[0]
            if rows:
                raise NetworkError(self._ambiguous(name, rows)) from None
            near = self._near_names(name, project) if name else []
            if near:
                raise NetworkError(f"{e}. Did you mean: {', '.join(near)}?") from None
            raise

    def _near_names(self, name: str, project: str | None, n: int = 5) -> list[str]:
        """Function names close to one that doesn't exist (a typo, another spelling of the symbol)."""
        import difflib

        try:
            proj = self._project(project)
        except NetworkError:
            return []
        sym = name.strip().strip("`'\"").split("@", 1)[0].split("(", 1)[0].strip()
        if len(sym) < 3:
            return []
        rows = self.store.all(
            "SELECT name FROM functions WHERE project_id=? AND (name LIKE ? OR name LIKE ?) LIMIT 4000",
            proj["id"],
            f"%{sym[-6:]}%",
            f"{sym[:4]}%",
        )
        names = [r["name"] for r in rows]
        return difflib.get_close_matches(sym, names, n=n, cutoff=0.6) or [x for x in names if sym in x][:n]

    def _loose_functions(self, name: str, project: str | None) -> list:
        """Functions an agent may mean by a name that isn't stored as such: the plain symbol of a function whose
        name repeats across a fork's files (stored as symbol@unit), symbol@unit for one that doesn't repeat,
        Class::method, a name with its parameter list or in quotes."""
        try:
            proj = self._project(project)
        except NetworkError:
            return []
        text = name.strip().strip("`'\"").strip()
        sym, _, unit = text.partition("@")
        unit, _, nth = unit.partition("#")
        sym = sym.split("(", 1)[0].strip()
        if not sym:
            return []
        rows = self.store.all(
            "SELECT * FROM functions WHERE project_id=? AND (name=? OR substr(name,1,?)=? "
            "OR json_extract(meta,'$.symbol')=? OR substr(json_extract(meta,'$.demangled'),1,?)=?) ORDER BY name",
            proj["id"],
            sym,
            len(sym) + 1,
            sym + "@",
            sym,
            len(sym) + 1,
            sym + "(",
        )
        if unit:
            rows = (
                [r for r in rows if r["unit"] == unit]
                or [r for r in rows if r["unit"].endswith(unit)]
                or [r for r in rows if r["unit"].startswith(unit)]
                or [r for r in rows if unit in r["unit"]]
            )
        if nth.isdigit() and len(rows) > 1:
            rows = [r for r in rows if (_stored(r["meta"], "a function's meta").get("nth", 0) + 1) == int(nth)] or rows
        return [r for r in rows if r["status"] != "nosource"] or rows  # ones in files without source can't be worked on

    @staticmethod
    def _ambiguous(name: str, rows: list) -> str:
        listing = "\n".join(f"- {r['name']} ({r['status']}, {r['n_insns']} instructions)" for r in rows[:12])
        more = f"\n... and {len(rows) - 12} more" if len(rows) > 12 else ""
        return f"{name!r} is the name of {len(rows)} functions in different files; pass the full name:\n{listing}{more}"

    def resolve_function(self, agent: Agent | None, project: str | None, name: str) -> str:
        """The stored name for what an agent calls a function: when a plain name fits several, the one the agent
        holds a claim on."""
        try:
            return self._function(name, project)["name"]
        except NetworkError:
            rows = self._loose_functions(name, project) if name else []
            held = (
                self.store.all("SELECT f.id, f.unit FROM claims c JOIN functions f ON f.id=c.function_id WHERE c.agent_id=?", agent.id)
                if agent
                else []
            )
            if held and len(rows) > 1:  # one it holds, else the one in the file (or module) it works in
                ids, units = {h["id"] for h in held}, {h["unit"] for h in held}
                modules = {u.split("/", 1)[0] for u in units}
                for pick in (
                    [r for r in rows if r["id"] in ids],
                    [r for r in rows if r["unit"] in units],
                    [r for r in rows if r["unit"].split("/", 1)[0] in modules],
                ):
                    if len(pick) == 1:
                        return pick[0]["name"]
            raise

    def _rom_target(self, fn) -> match.RomTarget:
        proj = self.store.one("SELECT config FROM projects WHERE id=?", fn["project_id"])
        if proj is None:  # the function outlived its project (a delete in another tab): judge nothing
            raise NetworkError(f"{fn['name']} belongs to a project that no longer exists")
        cfg = _stored(proj["config"], f"the config of {fn['name']}'s project")
        return match.RomTarget(
            fn["name"],
            fn["vram"],
            fn["target"],
            match.RomTarget.relocs_from_json(_stored(fn["relocs"], f"the relocations of {fn['name']}", "[]")),
            cfg.get("big_endian", True),
            cfg.get("gp", 0),
        )

    def _make_target(self, proj, rf: split.RomFunction, syms: SymbolTable) -> match.RomTarget:
        cfg = self.cfg(proj)
        return match.target_from_split(rf, syms.resolve, cfg.get("big_endian", True), cfg.get("gp", 0))

    def _x86_target(self, fn, proj):
        from ..rom import match_x86

        cfg = self.cfg(proj)
        return match_x86.X86Target(
            fn["name"],
            cfg.get("arch", "x86_64"),
            fn["target"],
            match_x86.X86Target.relocs_from_json(_stored(fn["relocs"], f"the relocations of {fn['name']}", "[]")),
        )

    def _target_obj(self, fn, proj):
        if not self.is_rom(proj):
            return super()._target_obj(fn, proj)
        if self.cfg(proj).get("platform") in X86_PLATFORMS:
            from ..rom import match_x86

            return match_x86.target_obj(self._x86_target(fn, proj), self.symbols(proj["id"]))
        if self.cfg(proj).get("platform") == "gba":
            return FunctionObj(fn["name"], "thumb", False, fn["target"], [])
        return match.canonical_target(self._rom_target(fn), self.symbols(proj["id"]))

    def _evaluate(self, fn, proj, obj: bytes, symbols=None):
        if not self.is_rom(proj):
            return super()._evaluate(fn, proj, obj, symbols=symbols)
        syms = symbols if symbols is not None else self.symbols(proj["id"])
        if self.cfg(proj).get("platform") in X86_PLATFORMS:
            from ..rom import match_x86

            res = match_x86.link(obj, fn["name"], self._x86_target(fn, proj), syms)
            return res.candidate, res.exact if res.candidate is not None else None, res.problems
        if self.cfg(proj).get("platform") == "gba":
            from ..rom import match_arm

            res = match_arm.link(obj, fn["name"], match_arm.ArmTarget(fn["name"], fn["vram"], fn["target"]), syms, self.reader(proj))
            return res.candidate, res.exact if res.candidate is not None else None, res.problems
        res = match.link(obj, fn["name"], self._rom_target(fn), syms, self.reader(proj))
        return res.candidate, res.exact if res.candidate is not None else None, res.problems

    def _twin_lines(self, proj, fn) -> list[str]:
        """For a brief: the matched function with the same code, and its C with this one's names."""
        try:
            twin_id = self.twins.twin_of(proj, fn["id"])
        except Exception:  # noqa: BLE001
            return []
        if twin_id is None:
            return []
        a = self.store.one("SELECT * FROM functions WHERE id=?", twin_id)
        if a is None:  # its twin was deleted since the lookup
            return []
        source = self.twins.source_for(proj, a, fn)
        out = ["", f"### Identical code: {a['name']} is the same machine code and already matches"]
        if source and len(source) < 6000:
            out += ["Its C with this function's names (try it first):", "```c", source.strip(), "```"]
        return out

    def rename_tokens(self, proj, text: str) -> str:
        """Show automatic names under their current names (func_80001234 -> deflate)."""
        syms = self.symbols(proj["id"])
        return AUTO_TOKEN.sub(lambda m: syms.label(int(m.group(0)[-8:], 16)), text)

    def _asm_text(self, fn, proj) -> str:
        if not self.is_rom(proj):
            return super()._asm_text(fn, proj)
        return self.rename_tokens(proj, fn["asm"])

    @staticmethod
    def _asm_source(fn: split.RomFunction) -> str:
        out = [f"glabel {fn.name}"]
        for ln in fn.lines:
            if ln.vram in fn.labels and ln.vram != fn.vram:
                out.append(f".L{ln.vram:08X}:")
            out.append(f"/* {ln.rom:X} {ln.vram:08X} {ln.word:08X} */  {ln.mnemonic:<10} {ln.operands}".rstrip())
        out.append(f"endlabel {fn.name}")
        return "\n".join(out)

    @staticmethod
    def _parse_asm(name: str, text: str, segment: str) -> split.RomFunction:
        lines, labels = [], set()
        for raw in text.splitlines():
            m = split._INSN.match(raw)
            if m:
                rom, vram, word, mnem, ops = m.groups()
                lines.append(split.AsmLine(int(rom, 16), int(vram, 16), int(word, 16), mnem, ops))
                continue
            m = split._LABEL.match(raw)
            if m:
                labels.add(int(m.group(1).lstrip(".L"), 16))
        return split.RomFunction(name, lines[0].vram, lines[0].rom, segment, lines, Path(), labels)

    def _rea_seed(self, fn, proj) -> str | None:
        """A decompiler draft for Windows targets, through REA when it is set up (see rea.py)."""
        if fn["seed"] is not None:
            return fn["seed"]

        text = rea.draft(Path(proj["root"]) / self.cfg(proj)["rom"]["program"], fn)
        if text:
            with self.store.tx() as db:
                db.execute("UPDATE functions SET seed=? WHERE id=?", (text, fn["id"]))
        return text

    def _fn_seed_lock(self, fn_id: int) -> threading.Lock:
        with self._seed_locks_lock:
            lock = self._seed_locks.get(fn_id)
            if lock is None:
                lock = threading.Lock()
                self._seed_locks[fn_id] = lock
            return lock

    def _seed_for(self, fn, proj) -> str | None:
        """m2c draft, made the first time someone opens the function (imports stay fast)."""
        if self.cfg(proj).get("platform") in X86_PLATFORMS:
            return self._rea_seed(fn, proj)
        if fn["seed"] is not None or not seeds.available():
            return fn["seed"]
        tc = self.cfg(proj)["compiler"]["toolchain"]
        asm = self._asm_text(fn, proj)
        data_asm = None
        if "jtbl_" in fn["asm"]:
            data_asm = "\n".join(self._jtbl_asm(proj, fn))
        ctx = self._context(proj["id"], fn["unit"])
        draft = seeds.generate_from_asm(asm, fn["name"], tc, ctx["header"] if ctx else "", data_asm)
        text = f"/* machine-generated by m2c: a starting point, not a match */\n{draft}" if draft else ""
        with self.store.tx() as db:
            db.execute("UPDATE functions SET seed=? WHERE id=?", (text, fn["id"]))
        return text

    def _jtbl_asm(self, proj, fn) -> list[str]:
        out = [".section .rodata"]
        read = self.reader(proj)
        syms = self.symbols(proj["id"])
        for m in sorted(set(re.findall(r"jtbl_([0-9A-F]{8})", fn["asm"]))):
            addr = int(m, 16)
            sym = syms.by_addr.get(addr)
            size = sym.size if sym and sym.size else 0
            data = read(addr, size) if size else None
            if not data:
                continue
            out.append(f"dlabel {syms.label(addr)}")
            fmt = ">I" if self.cfg(proj).get("big_endian", True) else "<I"
            for (w,) in struct.iter_unpack(fmt, data[: len(data) // 4 * 4]):
                out.append(f".word .L{w:08X}" if fn["vram"] <= w < fn["vram"] + fn["size"] else f".word 0x{w:08X}")
        return out

    def get_context(self, project: str | None, function: str | None = None) -> str:
        self._rom_only(project, "get_context")
        return super().get_context(project, function)

    def _brief(self, fn, agent, header):
        if self.cfg(self.store.one("SELECT config FROM projects WHERE id=?", fn["project_id"])).get("kind") == "fork":
            return self.forks.brief(fn, agent, header)
        proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        if proj is None:
            raise NetworkError(f"{fn['name']} belongs to a project that no longer exists")
        if self.is_rom(proj) and fn["status"] != "matched" and not fn["best_source"] and fn["seed"] is None:
            with self._fn_seed_lock(fn["id"]):
                self._seed_for(self._function(fn["name"], proj["slug"]), proj)
            fn = self._function(fn["name"], proj["slug"])
        text = super()._brief(fn, agent, header)
        if header == "CLAIMED" and fn["status"] == "open" and not self.is_analysis(proj):
            text += (
                "\n\nIf it can't be made byte-exact — the toolchain can't build this code, or the compiler it was "
                "built with is gone — write_source(function, source) keeps a reconstructed implementation in "
                "recovered/. It is labelled unverified and is never counted as a match."
            )
        return text

    def xrefs(self, proj) -> dict:
        """calls / called_by / uses / used_by, all keyed by address."""
        if proj["id"] in self._xrefs:
            return self._xrefs[proj["id"]]
        funcs = self.store.all("SELECT name, vram, size, relocs FROM functions WHERE project_id=?", proj["id"])
        starts = {f["vram"] for f in funcs}
        calls, called_by, uses, used_by = {}, {}, {}, {}
        # Calls to something not known as a function are not data: R_MIPS_26, or 10, a Thumb call (GBA). On x86
        # those numbers are RIP-relative and absolute data references, so nothing is left out there.
        not_data = () if self.cfg(proj).get("platform") in X86_PLATFORMS else (match.R_MIPS_26, 10)
        for f in funcs:
            for row in _stored(f["relocs"], f"the relocations of {f['name']}", "[]"):
                _, rtype, addr = row[0], row[1], row[2]
                if f["vram"] <= addr < f["vram"] + f["size"]:
                    continue
                if addr in starts:
                    calls.setdefault(f["vram"], set()).add(addr)
                    called_by.setdefault(addr, set()).add(f["vram"])
                elif rtype not in not_data:
                    uses.setdefault(f["vram"], set()).add(addr)
                    used_by.setdefault(addr, set()).add(f["vram"])
        self._xrefs[proj["id"]] = {"calls": calls, "called_by": called_by, "uses": uses, "used_by": used_by}
        return self._xrefs[proj["id"]]

    def _extra_brief(self, fn, proj) -> list[str]:
        if not self.is_rom(proj):
            return []
        syms = self.symbols(proj["id"])
        x = self.xrefs(proj)
        lines = [""]
        if facts := self.intake_line(proj["slug"]):
            lines += [facts, ""]
        lines += ["### Where it lives", f"address 0x{fn['vram']:08X}, ROM offset 0x{fn['rom_offset']:X}, segment {fn['unit']}"]
        prev = {r["name"]: r for r in self.store.all("SELECT name, kind, size, preview FROM symbols WHERE project_id=?", proj["id"])}
        calls = sorted(x["calls"].get(fn["vram"], ()))
        callers = sorted(x["called_by"].get(fn["vram"], ()))
        if calls:
            call_labels = []
            for a in calls[:30]:
                cname, _ = syms.name_at(a)
                sr = prev.get(cname)
                lbl = syms.label(a)
                if sr and sr["preview"] and sr["kind"] in ("fn", "func"):
                    lbl = f"{lbl} /* {sr['preview']} */"
                call_labels.append(lbl)
            lines.append("calls: " + ", ".join(call_labels))
        if callers:
            lines.append("called by: " + ", ".join(syms.label(a) for a in callers[:30]))
        used = sorted(x["uses"].get(fn["vram"], ()))
        if used:
            lines += ["", "### Data it uses (declare what you need as extern in your source, or in context.h)"]
            for a in used[:40]:
                name, _off = syms.name_at(a)
                r = prev.get(name)
                desc = f"{r['kind']}, {r['size']} bytes" if r else "unknown size"
                if r and r["preview"]:
                    desc += f": {r['preview']}"
                lines.append(f"- {syms.label(a)} (0x{a:08X}; {desc})")
        twin = self._twin_lines(proj, fn) if fn["status"] != "matched" else []
        lines += twin
        alike = [] if twin else [(r, sim) for r, sim in self.similar(proj, fn, top=3) if r["status"] == "matched"]
        if alike:
            lines += [
                "",
                "### Looks like (already matched; read with get_function)",
                ", ".join(f"{r['name']} ({sim * 100:.0f}% similar)" for r, sim in alike),
            ]
            r, sim = alike[0]
            src = self.store.one("SELECT matched_source FROM functions WHERE project_id=? AND name=?", proj["id"], r["name"])
            if sim >= 0.8 and src and src["matched_source"] and len(src["matched_source"]) <= 3000:
                # nearly the same code: its matching source is the best starting point there is (saves a tool call)
                lang = source_language(self.cfg(proj)["compiler"]["toolchain"])
                lines += [
                    f"Matching {lang} of {r['name']} ({sim * 100:.0f}% similar): start from this",
                    f"```{'cpp' if lang == 'C++' else 'c'}",
                    src["matched_source"].strip(),
                    "```",
                ]
        if roots := self.references(proj["slug"]):
            from . import references

            hits = references.search(roots, rf"0x0*{fn['vram']:x}\b")
            if hits != "no matches":
                lines += ["", "### In the reference folders (read_file / search_code for more)", *hits.splitlines()[:12]]
        lines += self.rea_brief_lines(proj, fn)
        lines += [
            "",
            "Names: use the names shown here (or rename_symbol to give an address a real name). "
            "Your code is linked at this function's address and must reproduce the ROM bytes exactly.",
        ]
        return lines

    def _rom_ops(self, proj) -> dict[tuple[int, int], list[str]]:
        """(vram, size) -> mnemonics of every function, cached (renames keep addresses; splits change sizes)."""
        cache = self._ops.setdefault(proj["id"], {})
        rows = self.store.all("SELECT vram, size, target FROM functions WHERE project_id=? AND target IS NOT NULL", proj["id"])
        live = set()
        for r in rows:
            key = (r["vram"], r["size"])
            live.add(key)
            if key not in cache:
                arch = proj["arch"]
                if arch == "arm":
                    cache[key] = similarity.thumb_mnemonics(r["target"], r["vram"] or 0)
                elif arch == "ppc":
                    cache[key] = similarity.ppc_mnemonics(r["target"], bool(proj["big_endian"]), r["vram"] or 0)
                elif arch in ("x86", "x86_64"):
                    cache[key] = similarity.x86_mnemonics(r["target"], arch == "x86_64", r["vram"] or 0)
                else:
                    cache[key] = similarity.mips_mnemonics(r["target"], bool(proj["big_endian"]), r["vram"] or 0)
        for key in set(cache) - live:
            del cache[key]
        return cache

    def similar(self, proj, fn, top: int = 5) -> list[tuple[dict, float]]:
        """Functions whose instructions look like `fn`'s, matched ones first: [(row, similarity)]."""
        if fn["target"] is None:
            return []
        ops = self._rom_ops(proj)
        me = (fn["vram"], fn["size"])
        if me not in ops:
            return []
        cands = {f"{v}:{n}": o for (v, n), o in ops.items() if (v, n) != me}
        rows = {
            (r["vram"], r["size"]): r
            for r in self.store.all("SELECT name, vram, size, n_insns, status, best_score FROM functions WHERE project_id=?", proj["id"])
        }
        out = []
        for key, sim in similarity.rank(ops[me], cands, top=top * 2):
            v, n = (int(x) for x in key.split(":"))
            if (v, n) in rows:
                out.append((rows[(v, n)], sim))
        out.sort(key=lambda x: (x[0]["status"] != "matched", -x[1]))
        return out[:top]

    def find_similar(self, project: str | None, function: str, limit: int = 5) -> str:
        if fork := self.fork_slug(project):
            return self.forks.find_similar(fork, function, limit)
        proj = self._project(project)
        fn = self._function(function, proj["slug"])
        hits = self.similar(proj, fn, top=max(1, min(int(limit), 15)))
        if not hits:
            return f"No look-alikes found for {fn['name']}."
        lines = [f"Functions that look like {fn['name']}:"]
        for r, sim in hits:
            state = "matched" if r["status"] == "matched" else f"best {r['best_score']}" if r["best_score"] is not None else "not started"
            lines.append(f"- {r['name']}  {sim * 100:.0f}% similar  {state}  ({r['n_insns']} insns, 0x{r['vram']:08X})")
        best = next((r for r, _ in hits if r["status"] == "matched"), None)
        if best is not None:
            src = self.store.one("SELECT matched_source FROM functions WHERE project_id=? AND name=?", proj["id"], best["name"])
            if src and src["matched_source"]:
                lang = source_language(self.cfg(proj)["compiler"]["toolchain"])
                lines += [
                    "",
                    f"Matching {lang} of {best['name']}:",
                    f"```{'cpp' if lang == 'C++' else 'c'}",
                    src["matched_source"].strip()[:4000],
                    "```",
                ]
        return "\n".join(lines)

    def export_rom(self, slug: str, log=print) -> dict:
        """Write a rebuildable source tree (the base for ports and mods) and prove it rebuilds the game."""
        if self.is_analysis(self._project(slug)):
            raise NetworkError("an analysis project has no source tree to export: its findings are notes on its work items")
        if self.fork_slug(slug):  # a fork's work is already in its source tree, committed
            return {"path": self._project(slug)["root"], "identical": None}
        from ..rom.export import export_rom
        from ..rom.export_gba import export_gba

        proj = self._project(slug)
        if self.cfg(proj).get("platform") in X86_PLATFORMS:
            from ..rom.export_pc import export_pc

            res = export_pc(self, slug, Path(proj["root"]).parent / f"{Path(proj['root']).name}-export", log=log)
            with self.store.tx() as db:
                cfg = _stored(
                    db.execute("SELECT config FROM projects WHERE id=?", (proj["id"],)).fetchone()["config"],
                    f"the config of {proj['slug']}",
                )
                cfg["export"] = {
                    "t": time.time(),
                    "path": str(res["path"]),
                    "identical": None,
                    "matched": res.get("matched"),
                    "functions": res.get("functions"),
                    "kind": "pc",
                }
                db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))
            return res
        export = export_gba if self.cfg(proj).get("platform") == "gba" else export_rom
        res = export(self, slug, Path(proj["root"]) / "source", log=log)
        with self.store.tx() as db:  # shown on the project page: what's in the tree and whether it rebuilds
            cfg = _stored(
                db.execute("SELECT config FROM projects WHERE id=?", (proj["id"],)).fetchone()["config"], f"the config of {proj['slug']}"
            )
            cfg["export"] = {
                "t": time.time(),
                "path": str(res["path"]),
                "identical": res.get("identical"),
                "matched": res.get("matched"),
                "functions": res.get("functions"),
                "fallback": len(res.get("fallback") or []),
            }
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))
        return res

    def _update_cfg(self, slug: str, change) -> dict:
        """Change a project's config in one transaction (change(cfg) edits it in place)."""
        proj = self._project(slug)
        with self.store.tx() as db:
            cfg = _stored(
                db.execute("SELECT config FROM projects WHERE id=?", (proj["id"],)).fetchone()["config"], f"the config of {proj['slug']}"
            )
            change(cfg)
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))
        return cfg

    def add_spend(self, slug: str, usd: float, tokens: int) -> None:
        if usd or tokens:

            def change(cfg):
                sp = cfg.setdefault("spend", {"usd": 0.0, "tokens": 0})
                sp["usd"] = round(sp.get("usd", 0) + usd, 6)
                sp["tokens"] = sp.get("tokens", 0) + tokens

            self._update_cfg(slug, change)

    def spend(self, slug: str) -> dict | None:
        return self.cfg(self._project(slug)).get("spend")

    def set_instructions(self, slug: str, text: str) -> None:
        """What the user wants agents to know or focus on, given to every agent on this project."""
        self._update_cfg(slug, lambda cfg: cfg.__setitem__("instructions", text.strip()[:4000]))

    def instructions(self, slug: str) -> str:
        return self.cfg(self._project(slug)).get("instructions") or ""

    def references(self, slug: str) -> list[Path]:
        """Folders named in the user's instructions that the project's agents may read (ROM projects; not in a
        room, whose instructions could come from someone else's computer)."""
        from . import references

        if self.fork_slug(slug) or self.links.get(slug) is not None:
            return []
        return references.named(self.instructions(slug))

    def set_priority(self, slug: str, name: str, on: bool = True) -> list[str]:
        """Functions the user wants done next: automatic claims take these first."""
        fn = self._function(name, slug)

        def change(cfg):
            items = [n for n in cfg.get("priority", []) if n != fn["name"]]
            cfg["priority"] = ([fn["name"], *items]) if on else items

        return self._update_cfg(slug, change)["priority"]

    def claim_row(self, agent: Agent, project: str | None = None, function: str | None = None, strategy: str = "easiest"):
        if function is None and project and not self.fork_slug(project):
            proj = self._project(project)
            for name in self.cfg(proj).get("priority", []):
                row = self.store.one("SELECT f.id, f.status FROM functions f WHERE f.project_id=? AND f.name=?", proj["id"], name)
                held = self.store.one("SELECT 1 FROM claims WHERE agent_id=?", agent.id)
                if (
                    row
                    and row["status"] == "open"
                    and not held
                    and not self.store.one("SELECT 1 FROM claims WHERE function_id=?", row["id"])
                ):
                    function = name
                    break
        self._yield_autopilot(function, project)
        return Network.claim_row(self, agent, project, function, strategy)

    def chat_agent(self, base: str) -> tuple[Agent, str]:
        """An identity for an agent the user talks to (Claude Code, Codex, ... connected with `decomp mcp`): `base`,
        or base_2, base_3... when another session of the same agent is active right now (never base-2: that's the
        name of a helper session, see local_agent)."""
        base = re.sub(r"[^A-Za-z0-9_.-]+", "-", base).strip("-")[:30] or "chat"
        for n in range(1, 100):
            name = base if n == 1 else f"{base}_{n}"
            row = self.store.one("SELECT id, origin, last_seen FROM agents WHERE name=?", name)
            if row is None or (row["origin"] == "chat" and (row["last_seen"] or 0) < time.time() - 600):
                break
        else:
            raise NetworkError("too many chat sessions at once")
        if row is None:
            token = self.register_agent(name, "you", trusted=True, origin="chat")
        else:
            token = "dn_" + secrets.token_urlsafe(24)
            with self.store.tx() as db:
                db.execute("UPDATE agents SET token_hash=?, last_seen=? WHERE id=?", (_hash_token(token), time.time(), row["id"]))
        return self.authenticate(token), token

    def is_chat(self, agent: Agent) -> bool:
        row = self.store.one("SELECT origin FROM agents WHERE id=?", agent.id)
        return bool(row) and row["origin"] == "chat"

    def _local_token(self, name: str) -> str:
        digest = hmac.new(self._agent_secret.encode(), f"local:{name}".encode(), hashlib.sha256).digest()
        return "dn_" + base64.urlsafe_b64encode(digest).decode().rstrip("=")

    def local_agent(self, name: str) -> tuple[Agent, str]:
        """An agent identity for one runner on this machine (created on first use). A name an agent the user talks
        to had before 0.10.4 (chat identities were numbered like helper sessions) becomes the runner's: a helper
        never gets the chat agent's tools, and the chat agent never takes over the runner's identity.

        The token is derived from the install's agent secret, so it survives app restarts: a helper's MCP bridge
        that reconnects after a restart still authenticates, instead of being treated as an unknown agent."""
        token = self._local_token(name)
        row = self.store.one("SELECT id FROM agents WHERE name=?", name)
        if row is not None:
            with self.store.tx() as db:
                db.execute(
                    "UPDATE agents SET token_hash=?, origin=CASE origin WHEN 'chat' THEN 'local' ELSE origin END WHERE id=?",
                    (_hash_token(token), row["id"]),
                )
        else:
            with self.store.tx() as db:
                db.execute(
                    "INSERT INTO agents(name,owner,token_hash,created_at,trusted,origin) VALUES(?,?,?,?,1,?)",
                    (name, "you", _hash_token(token), time.time(), "local"),
                )
        return self.authenticate(token), token
