"""The intake report: what the import found, in the words the next agent needs.

An import knows the compiler, the debug symbols, the libraries and the subsystems it saw, and an agent that starts on a
function needs that as one readable brief (intake.md) instead of twenty separate calls. Importing and exporting debug
symbols (PDB, DWARF, map, symbol_addrs), repairing a split where a symbol starts inside a merged function, and version
comparison live here too, next to the report that quotes them.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .. import toolchains
from ..network import NetworkError
from ..rom import bincompare, debuginfo, library, pe
from ..rom.symbols import AUTO_NAME, IDENT, Symbol
from .platform import (
    X86_PLATFORMS,
    source_language,
)

# The names a splitter generates on its own (splat, FUN_/loc_/j_ from a disassembler): the report counts how many are
# still unnamed, because that is what an agent has to work through before the report says much.
_AUTO_FUNCTION = re.compile(r"^(?:sub|func|FUN|loc|D|STR|unk|j|off)_[0-9A-Fa-f]{4,}$")

# What imported APIs and strings usually mean. Hints, not proof: the report says which functions use them.
SUBSYSTEM_HINTS = (
    ("render", ("d3d", "dxgi", "opengl", "vulkan", "glbind", "glclear", "glgen", "gltex", "glvertex",
                "shader", "texture", "sprite", "render", "gdi", "bitblt", "drawtext", "font")),
    ("audio", ("dsound", "xaudio", "openal", "fmod", "audio", "sound", "waveout", "mixer", "music")),
    ("input", ("dinput", "xinput", "getkeystate", "getasynckeystate", "joystick", "keyboard", "mouse")),
    ("network", ("ws2", "winsock", "socket", "connect", "recv", "send", "http", "curl", "inet")),
    ("file/registry", ("createfile", "readfile", "writefile", "fopen", "fclose", "findfirst", "findnext",
                       "deletefile", "movefile", "copyfile", "regopen", "regquery", "regset", "registry")),
    ("ui", ("user32", "messagebox", "createwindow", "dialogbox", "getdlgitem", "setwindowtext", "menu")),
    ("memory", ("malloc", "free", "realloc", "calloc", "heapalloc", "virtualalloc", "virtualfree",
                "memcpy", "memset", "memmove")),
    ("threading", ("createthread", "thread", "mutex", "criticalsection", "waitforsingle", "sleep")),
    ("crypto", ("crypt", "aes", "sha1", "sha256", "md5", "rsa", "ssl", "tls")),
)


class IntakeMixin:
    def apply_intake(self, slug: str, *, notes: str = "", symbols: str = "", types: str = "",
                     refs: str = "", trace: str = "", libraries: str = "", log=print) -> dict:
        """Apply the optional starting context from the import screen. A bad optional file is reported, never
        allowed to fail the import itself."""
        proj = self._project(slug)
        analysis = self.is_analysis(proj)
        warnings: list[str] = []
        applied: dict[str, object] = {}

        text = notes.strip()[:4000]
        if refs.strip():
            text = (text + ("\n" if text else "") + "Reference source folder: " + refs.strip())[:4000]
        if text:
            self.set_instructions(slug, text)
            applied["instructions"] = True

        if symbols.strip() and not analysis:
            try:
                applied["symbols"] = self.import_debug_info(slug, symbols.strip(), log=log)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"symbols: {e}")
        if types.strip() and not analysis:
            try:
                path = Path(types.strip()).expanduser()
                if not path.is_absolute():
                    path = Path(proj["root"]) / path
                body = path.read_text(encoding="utf-8", errors="replace")
                if not body.strip():
                    raise ValueError("empty type file")
                agent, _ = self.local_agent("intake")
                res = self.edit_context(agent, slug, "", "\n/* Types supplied at import: " +
                                        path.name.replace("*/", "") + " */\n" + body,
                                        f"intake types from {path.name}")
                if res.startswith("✅"):
                    applied["types"] = True
                else:
                    warnings.append("types: " + res)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"types: {e}")
        if trace.strip() and not analysis:
            try:
                applied["trace"] = self.import_trace(slug, trace.strip(), log=log)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"trace: {e}")
        if libraries.strip() and not analysis:
            names = [n.strip() for n in re.split(r"[,;\s]+", libraries) if n.strip()]
            n = self.classify_library(slug, names=names, log=log)
            applied["libraries"] = n
        self._syms.pop(proj["id"], None)
        if warnings:
            log("starting context: " + "; ".join(warnings))
        return {"applied": applied, "warnings": warnings}

    def intake_report(self, slug: str) -> dict:
        """What the import already knows, in one place: identity, compiler, debug names, code, data, imports,
        strings, trace and the questions still open. Written to INTAKE.md so agents read it instead of rediscovering."""
        proj = self._project(slug)
        cfg = self.cfg(proj)
        kind = cfg.get("kind") or "rom"
        rom = cfg.get("rom") or {}
        compiler = cfg.get("compiler") or {}
        debug = cfg.get("debug") or {}
        target = cfg.get("target") or {}
        now = time.time()
        if kind == "analysis":
            agg = self.store.one("SELECT COUNT(*) AS total, SUM(status='matched') AS matched, "
                                 "SUM(size) AS bytes FROM functions WHERE project_id=?", proj["id"])
            counts = cfg.get("counts") or {}
            segments = cfg.get("segments") or []
            issues = []
            if not agg["total"]:
                issues.append("no procedures were listed: add tasks or open the target with another REA tool")
            summary = (f"{agg['total'] or 0} work items · {agg['matched'] or 0} analyzed · "
                       f"{target.get('format') or 'file'} {target.get('architecture') or ''}").strip()
            lines = [f"# Intake report: {proj['name']}", "", f"Generated {time.strftime('%Y-%m-%d %H:%M:%S')} "
                     f"for project `{proj['slug']}`.", "", "## Target",
                     f"- file: `{target.get('path') or ''}`",
                     f"- format: {target.get('format') or 'file'} · architecture: {target.get('architecture') or 'unknown'}",
                     f"- provider: {cfg.get('provider') or 'auto'}",
                     f"- work items: {agg['total'] or 0} · analyzed: {agg['matched'] or 0} · "
                     f"bytes: {agg['bytes'] or 0}"]
            counts = cfg.get("counts") or {}
            if counts:
                lines.append("- REA inventory: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
            if segments:
                lines += ["", "## Segments"]
                lines += [f"- `{s.get('name') or '?'}` at 0x{int(s.get('vram') or 0):X}" for s in segments[:30]]
            lines += ["", "## How to use this", "REA reads this target. Claim work items, use rea_tool for a deeper "
                      "look, and save findings with complete_function.", ""]
            if issues:
                lines += ["## What is still unknown", *[f"- {i}" for i in issues], ""]
            return {"slug": slug, "kind": kind, "title": proj["name"], "generated_at": now, "summary": summary,
                    "markdown": "\n".join(lines), "path": str(Path(proj["root"]) / "INTAKE.md")}
        if kind == "fork":
            return {"slug": slug, "kind": kind, "title": proj["name"], "generated_at": now,
                    "summary": "a fork already has its project's source and symbols", "markdown": "", "path": ""}
        agg = self.store.one(
            "SELECT COUNT(*) AS total, SUM(status='library') AS library, SUM(status='matched') AS matched, "
            "SUM(size) AS bytes, SUM(n_insns) AS insns, SUM(hits) AS hits, SUM(hits>0) AS traced "
            "FROM functions WHERE project_id=?", proj["id"])
        funcs = self.store.all("SELECT name, vram, size, n_insns, status FROM functions WHERE project_id=? "
                               "ORDER BY n_insns DESC", proj["id"])
        syms = self.store.all("SELECT name, address, size, kind, preview FROM symbols WHERE project_id=?", proj["id"])
        named = [f for f in funcs if not _AUTO_FUNCTION.match(f["name"]) and f["status"] != "library"]
        imports = []
        seen_imports = set()
        for sym in syms:
            if not sym["name"].startswith("__imp_"):
                continue
            name = sym["name"][6:]
            if name and name not in seen_imports:
                seen_imports.add(name)
                imports.append(name)
        strings = [s for s in syms if s["kind"] == "string" or s["name"].startswith("STR_")]
        strings.sort(key=lambda s: (s["size"] or 0), reverse=True)
        data = [s for s in syms if s["kind"] != "func" and s["kind"] != "string"
                and not s["name"].startswith(("STR_", "__imp_"))]
        data.sort(key=lambda s: (s["size"] or 0), reverse=True)
        entry = rom.get("entrypoint") or 0
        entry_rows = [f for f in funcs if entry and f["vram"] == entry]
        sections = cfg.get("segments") or []
        issues = []
        tc = toolchains.get(compiler.get("toolchain") or "")
        if kind == "rom" and not (agg["total"] or 0):
            issues.append("no functions were found")
        if tc and not toolchains.installed(tc.id):
            issues.append(f"{tc.name} is not installed, so nothing can be compiled yet (Compilers & tools)")
        if kind == "rom" and cfg.get("platform") in X86_PLATFORMS and not debug:
            issues.append("no debug information was shipped: names start as sub_/FUNC_ and types are recovered "
                          "from code, strings, imports and agents")
        if cfg.get("packed"):
            issues.append("the program looks packed (UPX): unpack it before trusting function boundaries")
        if rom.get("matchable") is False:
            issues.append(f"{rom.get('build_label') or 'this language'} cannot be rebuilt as C/C++; inspect and annotate it instead")
        if not (agg["traced"] or 0):
            issues.append("no runtime trace: hot functions and call order are unknown")
        if funcs and len(named) < max(1, len(funcs) // 20):
            issues.append("almost every function still has an automatic name")
        summary = (f"{agg['total'] or 0} functions · {agg['library'] or 0} library · {len(data)} data · "
                   f"{len(strings)} strings · {len(imports)} imports" +
                   (f" · {agg['traced']} traced ({agg['hits']} hits)" if agg["traced"] else ""))
        markdown = self._intake_markdown(proj, cfg, rom, compiler, debug, target, agg, funcs, named, imports,
                                         strings, data, entry_rows, sections, issues)
        markdown += self._xref_markdown(proj, funcs, syms)
        markdown += self._cxx_markdown(syms)
        groups, unclassified = self._subsystem_groups(proj, funcs, syms)
        if groups or unclassified:
            markdown += self._subsystem_markdown(groups, unclassified)
        markdown += ("## How to use this\n"
                     "This report is generated from the import, not from guesses. Read it before claiming functions; "
                     "use `read_file` for source files, `get_context` for context.h, `search_facts` for a name, "
                     "string, API or address, and `list_subsystems` for a starting area. The target is the final "
                     "judge: compile and diff.\n")
        return {"slug": slug, "kind": kind, "title": proj["name"], "generated_at": now, "summary": summary,
                "markdown": markdown, "path": str(Path(proj["root"]) / "INTAKE.md")}

    def search_facts(self, slug: str, query: str, limit: int = 40) -> str:
        """Search the import's index: function names, symbols, strings, imported APIs and addresses, with the
        callers of every symbol. One tool call replaces a name search followed by xrefs on each hit."""
        proj = self._project(slug)
        q = (query or "").strip()
        if not q:
            return "Give a name, part of a string, an API, or an address (0x140001000)."
        limit = max(1, min(int(limit or 40), 200))
        pid = proj["id"]
        low = q.lower()
        functions = self.store.all(
            "SELECT name, vram, size, n_insns, status FROM functions WHERE project_id=? "
            "AND instr(lower(name), ?) > 0 ORDER BY status='open', n_insns DESC LIMIT ?", pid, low, limit)
        symbols = self.store.all(
            "SELECT name, address, size, kind, preview FROM symbols WHERE project_id=? "
            "AND (instr(lower(name), ?) > 0 OR instr(lower(coalesce(preview,'')), ?) > 0) LIMIT ?",
            pid, low, low, limit)
        try:
            graph = self.xrefs(proj)
        except Exception:  # noqa: BLE001
            graph = {"called_by": {}, "calls": {}, "used_by": {}, "uses": {}}
        by_addr = {int(f["vram"]): f["name"] for f in self.store.all(
            "SELECT name, vram FROM functions WHERE project_id=? AND vram IS NOT NULL", pid)}
        lines = [f'Facts matching "{q}"']
        if functions:
            lines.append(f"Functions ({len(functions)}):")
            for r in functions:
                callers = len(graph["called_by"].get(int(r["vram"]), ())) if r["vram"] is not None else 0
                callees = len(graph["calls"].get(int(r["vram"]), ())) if r["vram"] is not None else 0
                lines.append(f"- `{r['name']}` at 0x{int(r['vram'] or 0):X} — {r['status']}, "
                             f"{r['n_insns']} instructions, {r['size']} bytes, {callers} callers, {callees} callees")
        if symbols:
            lines.append(f"Symbols ({len(symbols)}):")
            for r in symbols:
                users = graph["used_by"].get(int(r["address"] or 0), ())
                who = ", ".join(f"`{by_addr[a]}`" for a in sorted(users)[:8] if a in by_addr)
                if len(users) > 8:
                    who += f" +{len(users)-8}"
                detail = f" ({r['kind']}, {r['size']} bytes" + (f": {r['preview']}" if r["preview"] else "") + ")"
                lines.append(f"- `{r['name']}` at 0x{int(r['address'] or 0):X}{detail}"
                             + (f" — used by {who}" if who else ""))
        # An address is often what an agent has, not a name.
        try:
            addr = int(q, 0)
        except ValueError:
            addr = None
        if addr is not None:
            fn = self.store.one("SELECT name, vram, size, n_insns, status FROM functions WHERE project_id=? "
                                "AND vram<=? AND vram+coalesce(size,0)>? ORDER BY vram DESC LIMIT 1", pid, addr, addr)
            if fn:
                lines.append(f"Address 0x{addr:X}: inside `{fn['name']}` at 0x{int(fn['vram'] or 0):X} "
                             f"({fn['status']}, {fn['n_insns']} instructions)")
            sym = self.store.one("SELECT name, address, size, kind, preview FROM symbols WHERE project_id=? "
                                 "AND address=? LIMIT 1", pid, addr)
            if sym:
                lines.append(f"Address 0x{addr:X}: `{sym['name']}` ({sym['kind']}, {sym['size']} bytes)"
                             + (f": {sym['preview']}" if sym["preview"] else ""))
        if len(lines) == 1:
            lines.append("Nothing matched. Try a shorter fragment, an API name, a string, or an address.")
        return "\n".join(lines)

    def _subsystem_groups(self, proj, funcs=None, syms=None):
        """Function vram -> primary subsystem, scored by the imported APIs and strings it references, plus the
        number of unclassified functions. Hints for the first split, not a claim about every function."""
        pid = proj["id"]
        funcs = [f for f in (funcs if funcs is not None else self.store.all(
            "SELECT name, vram, size, n_insns, status FROM functions WHERE project_id=?", pid))
            if f["status"] != "library"]
        syms = syms if syms is not None else self.store.all(
            "SELECT name, address, size, kind, preview FROM symbols WHERE project_id=?", pid)
        by_addr = {int(f["vram"]): f for f in funcs if f["vram"] is not None}
        sym_at = {}
        for s in syms:
            sym_at[int(s["address"] or 0)] = s
        try:
            graph = self.xrefs(proj)
        except Exception:  # noqa: BLE001
            return {}, set()
        scores = {}
        def hint(label):
            text = str(label or "").lower()
            for tag, words in SUBSYSTEM_HINTS:
                if any(w in text for w in words):
                    return tag
            return None
        for caller, addrs in graph["uses"].items():
            if caller not in by_addr:
                continue
            for addr in addrs:
                s = sym_at.get(int(addr))
                if not s:
                    continue
                label = s["name"][6:] if s["name"].startswith("__imp_") else (
                    s["preview"] if s["kind"] == "string" and s["preview"] else s["name"])
                tag = hint(label)
                if tag:
                    scores.setdefault(caller, {}).setdefault(tag, 0)
                    scores[caller][tag] += 1
        for caller, callees in graph["calls"].items():
            if caller not in by_addr:
                continue
            for callee in callees:
                other = by_addr.get(int(callee))
                if not other:
                    continue
                tag = hint(other["name"])
                if tag:
                    scores.setdefault(caller, {}).setdefault(tag, 0)
                    scores[caller][tag] += 1
        groups = {}
        for vram, tags in scores.items():
            tag = max(tags, key=tags.get)
            groups.setdefault(tag, []).append(by_addr[vram])
        unclassified = [f for f in funcs if int(f["vram"] or 0) not in scores]
        return groups, unclassified

    def subsystem_text(self, slug: str, limit: int = 12) -> str:
        """The subsystem map as text for an agent: where to start looking, not an authoritative taxonomy."""
        proj = self._project(slug)
        if self.is_analysis(proj):
            return "Subsystem hints are built from imports and strings in a matching project; analysis projects use REA."
        groups, unclassified = self._subsystem_groups(proj)
        if not groups and not unclassified:
            return "No functions to group yet."
        lines = ["Subsystem hints (from imported APIs and strings; verify before relying on one):"]
        for tag, rows in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:max(1, int(limit or 12))]:
            rows = sorted(rows, key=lambda f: (f["n_insns"] or 0), reverse=True)
            lines.append(f"### {tag} — {len(rows)} functions")
            lines += [f"- `{f['name']}` at 0x{int(f['vram'] or 0):X} ({f['n_insns']} instructions, {f['status']})"
                      for f in rows[:10]]
            if len(rows) > 10:
                lines.append(f"- … {len(rows)-10} more")
        if unclassified:
            lines.append(f"### unclassified — {len(unclassified)} functions")
            lines += [f"- `{f['name']}` at 0x{int(f['vram'] or 0):X}" for f in unclassified[:10]]
            if len(unclassified) > 10:
                lines.append(f"- … {len(unclassified)-10} more")
        return "\n".join(lines)

    def _cxx_markdown(self, syms) -> str:
        """C++ vtables and RTTI that survived as symbols; says plainly that stripped binaries need fingerprints."""
        v = sorted((s for s in syms if str(s["name"]).startswith(("??_7", "_ZTV"))),
                   key=lambda s: int(s["address"] or 0))
        r = sorted((s for s in syms if str(s["name"]).startswith(("??_R", "_ZTI", "_ZTS", ".?AV"))),
                   key=lambda s: int(s["address"] or 0))
        if not v and not r:
            return ""
        lines = ["## C++ vtables and RTTI", "Present as symbols; use them to recover classes and virtual calls."]
        if v:
            lines += [f"- vtable: `{s['name']}` at 0x{int(s['address'] or 0):X} ({s['size']} bytes)" for s in v[:30]]
            if len(v) > 30:
                lines.append(f"- … {len(v)-30} more vtables")
        if r:
            lines += [f"- RTTI: `{s['name']}` at 0x{int(s['address'] or 0):X}" for s in r[:30]]
            if len(r) > 30:
                lines.append(f"- … {len(r)-30} more RTTI records")
        lines.append("")
        return "\n".join(lines)

    def _xref_markdown(self, proj, funcs, syms) -> str:
        """The names agents would otherwise look up one by one: most-called functions, and which functions use
        each imported API, string and data object."""
        try:
            graph = self.xrefs(proj)
        except Exception:  # noqa: BLE001
            return ""
        by_addr = {int(f["vram"]): f["name"] for f in funcs if f["vram"] is not None}
        def labels(addrs, n=6):
            names = [by_addr.get(int(a), f"0x{int(a):X}") for a in sorted(addrs) if int(a) in by_addr]
            return ", ".join(f"`{x}`" for x in names[:n]) + (f" +{len(names)-n}" if len(names) > n else "")
        called = sorted(((len(v), int(k), by_addr.get(int(k), f"0x{int(k):X}")) for k, v in graph["called_by"].items()),
                        reverse=True)[:15]
        imports = []
        strings = []
        data = []
        for s in syms:
            addr = int(s["address"] or 0)
            users = graph["used_by"].get(addr)
            if not users:
                continue
            name = s["name"]
            if name.startswith("__imp_"):
                imports.append((len(users), name[6:], users))
            elif s["kind"] == "string" or name.startswith("STR_"):
                strings.append((len(users), s["preview"] or name, users))
            elif s["kind"] != "func":
                data.append((len(users), name, users))
        imports.sort(key=lambda r: r[0], reverse=True)
        strings.sort(key=lambda r: r[0], reverse=True)
        data.sort(key=lambda r: r[0], reverse=True)
        lines = []
        if called:
            lines += ["## Most-called functions", *[f"- `{name}` — {n} caller{'s' if n != 1 else ''}"
                                                    for n, _addr, name in called], ""]
        if imports:
            lines += ["## Imported APIs and their callers", *[f"- `{name}` — {n} function{'s' if n != 1 else ''}: "
                                                               f"{labels(users)}" for n, name, users in imports[:30]], ""]
        if strings:
            lines += ["## Strings and their callers", *[f"- \"{str(text)[:100]}\" — {n} function{'s' if n != 1 else ''}: "
                                                         f"{labels(users)}" for n, text, users in strings[:25]], ""]
        if data:
            lines += ["## Data objects and their callers", *[f"- `{name}` — {n} function{'s' if n != 1 else ''}: "
                                                              f"{labels(users)}" for n, name, users in data[:20]], ""]
        return "\n".join(lines)

    @staticmethod
    def _subsystem_markdown(groups, unclassified) -> str:
        lines = ["## Subsystem hints", "From imported APIs and strings; a starting point, not a proof."]
        for tag, rows in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:12]:
            rows = sorted(rows, key=lambda f: (f["n_insns"] or 0), reverse=True)
            lines.append(f"### {tag} — {len(rows)} functions")
            lines += [f"- `{f['name']}` at 0x{int(f['vram'] or 0):X} ({f['n_insns']} instructions, {f['status']})"
                      for f in rows[:8]]
            if len(rows) > 8:
                lines.append(f"- … {len(rows)-8} more")
        if unclassified:
            lines.append(f"### unclassified — {len(unclassified)} functions")
            lines += [f"- `{f['name']}` at 0x{int(f['vram'] or 0):X}" for f in unclassified[:8]]
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _intake_markdown(proj, cfg, rom, compiler, debug, target, agg, funcs, named, imports, strings, data,
                         entry_rows, sections, issues) -> str:
        """A readable report, not JSON: the same facts an analyst would write on the first page."""
        def size(n):
            n = int(n or 0)
            return f"{n // (1 << 20)} MiB" if n >= 1 << 20 else f"{n / 1024:.1f} KiB" if n >= 1024 else f"{n} B"
        def esc(s):
            return str(s or "").replace("|", "\\|").replace("\n", " ")[:160]
        lines = [f"# Intake report: {proj['name']}", "",
                 f"Generated {time.strftime('%Y-%m-%d %H:%M:%S')} for project `{proj['slug']}`.", ""]
        if target:
            lines += ["## Target", f"- file: `{target.get('path') or ''}`",
                      f"- format: {target.get('format') or 'file'} · architecture: {target.get('architecture') or 'unknown'}",
                      f"- provider: {cfg.get('provider') or 'auto'}", ""]
            counts = cfg.get("counts") or {}
            if counts:
                lines += ["- REA inventory: " + ", ".join(f"{k} {v}" for k, v in counts.items())]
        else:
            lines += ["## Identity", f"- file: `{rom.get('file') or ''}`",
                      f"- platform: {cfg.get('platform') or 'unknown'} · architecture: {proj['arch']} · "
                      f"{'big endian' if proj['big_endian'] else 'little endian'}",
                      f"- title: {rom.get('title') or proj['name']} · game code: {rom.get('game_code') or 'none'} · "
                      f"region: {rom.get('region') or 'unknown'}",
                      f"- size: {size(rom.get('size'))} · SHA-1: `{rom.get('sha1') or ''}`",
                      f"- entry point: 0x{int(rom.get('entrypoint') or 0):X}" +
                      (f" · image base: 0x{int(rom.get('image_base') or 0):X}" if rom.get("image_base") else ""), ""]
        tc_name = ""
        if compiler:
            tc = toolchains.get(compiler.get("toolchain") or "")
            tc_name = tc.name if tc else (compiler.get("toolchain") or "")
            flags = " ".join(compiler.get("flags") or [])
            lines += ["## Compiler", f"- toolchain: {tc_name or 'none'}",
                      f"- source language: {rom.get('language_guess') or source_language(compiler.get('toolchain') or '')}",
                      f"- detected: {rom.get('build_label') or rom.get('compiler_guess') or 'unknown'}"
                      + (f" · version {rom.get('compiler_version')}" if rom.get("compiler_version") else ""),
                      f"- flags: `{flags or '(none)'}`"]
            if rom.get("suggested_flags"):
                lines.append(f"- inferred flags: `{' '.join(rom['suggested_flags'])}`")
            evidence = rom.get("flag_evidence") or []
            if evidence:
                lines.append("- evidence: " + "; ".join(str(e) for e in evidence[:6]))
            if tc and not toolchains.installed(tc.id):
                lines.append("- **not installed**: install it in Compilers & tools to compile candidates")
            lines.append("")
        if debug:
            lines += ["## Debug information", f"- source: `{debug.get('source') or ''}`",
                      f"- names: {debug.get('symbols', 0)} · types: {debug.get('types', 0)}"]
            if cfg.get("context", {}).get("file") or debug.get("header"):
                lines.append("- the debug header is part of context.h (unless it failed to compile)")
            lines.append("")
        lines += ["## Code", f"- functions: {agg['total'] or 0} ({agg['library'] or 0} library/CRT, "
                  f"{agg['matched'] or 0} matched) · {size(agg['bytes'])} · {agg['insns'] or 0} instructions",
                  f"- named: {len(named)} · automatic names: {max(0, (agg['total'] or 0) - (agg['library'] or 0) - len(named))}",
                  f"- data objects: {len(data)} · strings: {len(strings)} · imports: {len(imports)}",
                  f"- trace: {agg['traced'] or 0} functions, {agg['hits'] or 0} hits" +
                  ("" if agg["traced"] else " (none imported)"), ""]
        if entry_rows:
            lines += ["## Entry point", *[f"- `{r['name']}` at 0x{int(r['vram'] or 0):X} ({r['n_insns']} instructions)"
                                          for r in entry_rows[:6]], ""]
        if named:
            lines += ["## Named functions (first 20)", *[f"- `{r['name']}` at 0x{int(r['vram'] or 0):X} — "
                                                       f"{r['size']} bytes, {r['n_insns']} instructions, {r['status']}"
                                                       for r in named[:20]], ""]
        if funcs:
            lines += ["## Largest functions", "| address | function | bytes | instructions | status |",
                      "|---|---|---:|---:|---|",
                      *[f"| 0x{int(r['vram'] or 0):X} | `{esc(r['name'])}` | {r['size']} | {r['n_insns']} | {r['status']} |"
                        for r in funcs[:15]], ""]
        if imports:
            lines += ["## Imported APIs (first 40)", *[f"- `{esc(n)}`" for n in imports[:40]], ""]
        if strings:
            lines += ["## Strings (largest 20)", *[f"- 0x{int(s['address'] or 0):X} ({s['size']} B): {esc(s['preview'])}"
                                                  for s in strings[:20] if s["preview"]], ""]
        if data:
            lines += ["## Data objects (largest 15)", "| address | name | bytes | kind | preview |",
                      "|---|---|---:|---|---|",
                      *[f"| 0x{int(s['address'] or 0):X} | `{esc(s['name'])}` | {s['size']} | {s['kind']} | {esc(s['preview'])} |"
                        for s in data[:15]], ""]
        if sections:
            lines += ["## Sections", *[f"- `{s.get('name') or '?'}` at 0x{int(s.get('vram') or 0):X} "
                                       f"(file 0x{int(s.get('rom_start') or 0):X}..0x{int(s.get('rom_end') or 0):X})"
                                       for s in sections[:30]], ""]
        if issues:
            lines += ["## What is still unknown", *[f"- {i}" for i in issues], ""]
        return "\n".join(lines)

    def write_intake(self, slug: str, log=print) -> dict:
        """Write INTAKE.md beside the project and remember it, so every agent can read the import's findings."""
        proj = self._project(slug)
        report = self.intake_report(slug)
        if not report["markdown"] or self.cfg(proj).get("kind") == "fork":
            return report
        path = Path(proj["root"]) / "INTAKE.md"
        path.write_text(report["markdown"], encoding="utf-8")
        self._update_cfg(slug, lambda cfg: cfg.__setitem__("intake", {"file": "INTAKE.md", "generated_at": report["generated_at"]}))
        report["path"] = str(path)
        log(f"intake report: {path.name} ({report['summary']})")
        return report

    def intake_line(self, slug: str) -> str:
        """The one line every agent prompt and brief gets, pointing at the report instead of rediscovering it."""
        proj = self._project(slug)
        cfg = self.cfg(proj)
        if not cfg.get("intake"):
            return ""
        path = Path(proj["root"]) / (cfg["intake"].get("file") or "INTAKE.md")
        return (f"Intake report (facts already extracted at import; read it first with read_file): {path}. "
                f"It lists the target, compiler and flags, debug names/types, function and data inventory, imports, "
                f"strings and what is still unknown.")

    def import_debug_info(self, slug: str, path: str = "", log=print) -> dict:
        """Merge a PDB/DWARF/MAP/symbol_addrs file into an existing project: names replace automatic ones,
        data names are added, and types are appended to the shared header (regression-tested when it has matches)."""
        proj = self._project(slug)
        cfg = self.cfg(proj)
        if cfg.get("kind") == "fork":
            raise NetworkError("debug symbol import is for ROM and PC projects; a fork already has its project's symbols")
        root = Path(proj["root"])
        target = root / (cfg.get("rom", {}).get("program") or cfg.get("rom", {}).get("file") or "")
        image = None
        data = None
        if cfg.get("platform") in X86_PLATFORMS and target.is_file():
            data = target.read_bytes()
            from ..rom import elfexe
            image = (elfexe if cfg.get("platform") == "linux" else pe).read(data, target.name)
        if path.strip():
            p = Path(path.strip()).expanduser()
            if not p.is_absolute():
                p = root / p
            if not p.is_file():
                raise NetworkError(f"no debug file at {p}")
            info = debuginfo.read(p, image=image)
        else:
            info = debuginfo.discover(target, data, image=image)
            if info is None:
                raise NetworkError(f"no PDB, DWARF, MAP or symbol_addrs.txt found for {target}")
        if not info.symbols and not info.functions and not info.header:
            raise NetworkError(f"{info.source} has no symbols or types this reader understands")

        rows = self.store.all("SELECT * FROM functions WHERE project_id=? AND vram IS NOT NULL", proj["id"])
        by_addr = {int(r["vram"]): r for r in rows if r["vram"]}
        symbols = self.store.all("SELECT * FROM symbols WHERE project_id=?", proj["id"])
        taken = {s["name"] for s in symbols} | {r["name"] for r in rows}
        splits = 0
        if self.is_rom(proj) and self.cfg(proj).get("platform") not in X86_PLATFORMS:
            splits = self._split_missed_boundaries(proj, info, log=log)
            if splits:
                rows = self.store.all("SELECT * FROM functions WHERE project_id=? AND vram IS NOT NULL", proj["id"])
                by_addr = {int(r["vram"]): r for r in rows if r["vram"]}
                taken = {s["name"] for s in self.store.all("SELECT name FROM symbols WHERE project_id=?", proj["id"])} \
                    | {r["name"] for r in rows}
        # PDB/DWARF have explicit functions; a MAP or symbol_addrs.txt only has names. A name at a split
        # function's address is a function name there, whatever the file called it.
        candidates: dict[tuple[int, str], object] = {}
        for meta in info.functions:
            if meta.name:
                candidates.setdefault((meta.address, meta.name), meta)
        for sym in info.symbols:
            if sym.kind == "func" or sym.address in by_addr:
                candidates.setdefault((sym.address, sym.name), debuginfo.DebugFunction(sym.name, sym.address, sym.size))
        renames: list[tuple[str, str]] = []
        for (address, name), _meta in sorted(candidates.items()):
            row = by_addr.get(address)
            if row is None or not name or not IDENT.match(name) or name in taken:
                continue
            if row["name"] == name:
                continue
            if not (AUTO_NAME.match(row["name"]) or row["name"].startswith("sub_")):
                continue  # a name a person chose wins over a debug symbol
            renames.append((row["name"], name))
            taken.discard(row["name"])
            taken.add(name)

        added = 0
        now = time.time()
        with self.store.tx() as db:
            for old, new in renames:
                row = db.execute("SELECT id, asm, matched_source, best_source FROM functions WHERE project_id=? AND name=?",
                                 (proj["id"], old)).fetchone()
                if row is None:
                    continue
                db.execute("UPDATE functions SET name=?, changed_at=? WHERE id=?", (new, now, row["id"]))
                asm = (row["asm"] or "").replace(f"glabel {old}", f"glabel {new}").replace(f"endlabel {old}", f"endlabel {new}")
                db.execute("UPDATE functions SET asm=? WHERE id=?", (asm, row["id"]))
                word = re.compile(rf"\b{re.escape(old)}\b")
                for col in ("matched_source", "best_source"):
                    if row[col]:
                        db.execute(f"UPDATE functions SET {col}=? WHERE id=?", (word.sub(new, row[col]), row["id"]))
                db.execute("UPDATE symbols SET name=?, changed_at=? WHERE project_id=? AND name=?",
                           (new, now, proj["id"], old))
            for sym in info.symbols:
                if not sym.name or not IDENT.match(sym.name):
                    continue
                row = db.execute("SELECT address FROM symbols WHERE project_id=? AND name=?",
                                 (proj["id"], sym.name)).fetchone()
                if row is not None:
                    if row["address"] != sym.address:
                        continue  # don't steal an existing name for another address
                    db.execute("UPDATE symbols SET size=?, kind=?, preview=?, changed_at=? WHERE project_id=? AND name=?",
                               (sym.size, sym.kind, sym.preview or None, now, proj["id"], sym.name))
                else:
                    db.execute("INSERT INTO symbols(project_id,name,address,size,kind,preview,changed_at) "
                               "VALUES(?,?,?,?,?,?,?)", (proj["id"], sym.name, sym.address, sym.size, sym.kind,
                                                         sym.preview or None, now))
                    added += 1
            self._event(db, proj["id"], None, "debug", f"{info.source}: {len(renames)} renamed, {added} new symbols")
        self._syms.pop(proj["id"], None)
        self._xrefs.pop(proj["id"], None)
        self._ops.pop(proj["id"], None)
        self.classify_library(slug, log=log)

        types_added = False
        warning = ""
        if info.header:
            ctx = self._context(proj["id"])
            if info.header.strip() not in (ctx["header"] if ctx else ""):
                agent, _ = self.local_agent("debuginfo")
                try:
                    res = self.edit_context(agent, slug, "", "\n/* Debug information imported from " +
                                            info.source.replace("*/", "") + " */\n" + info.header,
                                            "import debug types")
                    types_added = res.startswith("✅")
                    if not types_added:
                        warning = res
                except NetworkError as e:
                    warning = str(e)
        self._update_cfg(slug, lambda c: c.__setitem__("debug", {
            "source": info.source, "symbols": len(info.symbols), "types": len(info.types),
            "header": info.header, "imported_at": now}))
        log(f"{info.source}: {len(renames)} functions renamed, {added} symbols added, "
            f"{len(info.types)} types{' imported' if types_added else ''}")
        return {"source": info.source, "renamed": len(renames), "split": splits, "symbols": added,
                "types": len(info.types), "types_added": types_added, "warning": warning}

    def _split_missed_boundaries(self, proj, info, log=print) -> int:
        """Cut an automatic function where a debug-symbol function starts inside it.

        A function that never returns runs into the next one in the split (exit() into clamp_s32, a thunk into its
        target). The symbol file knows the real boundary, so the merged function is split there and its name is
        imported after, instead of being dropped because no work item starts at that address."""
        agent, _ = self.local_agent("debuginfo")
        starts = sorted({int(f.address) for f in info.functions if f.name and IDENT.match(f.name)}
                        | {int(s.address) for s in info.symbols if s.kind == "func" and IDENT.match(s.name)})
        splits = 0
        for address in starts:
            row = self.store.one(
                "SELECT * FROM functions WHERE project_id=? AND vram < ? AND COALESCE(vram, 0) + COALESCE(size, 0) > ? "
                "AND status='open' ORDER BY vram LIMIT 1", proj["id"], address, address)
            if row is None or not AUTO_NAME.match(row["name"]):
                continue
            try:
                text = self.split_function(agent, proj["slug"], row["name"], f"0x{address:08X}")
            except NetworkError as e:
                log(f"couldn't split {row['name']} at 0x{address:08X}: {e}")
                continue
            if text.startswith("✅"):
                splits += 1
        if splits:
            log(f"{info.source}: split {splits} merged function(s) at symbol boundaries")
        return splits

    def export_debug_info(self, slug: str) -> dict:
        """Write portable symbol and type files into the project folder."""
        proj = self._project(slug)
        cfg = self.cfg(proj)
        root = Path(proj["root"])
        rows = self.store.all("SELECT name,address,size,kind FROM symbols WHERE project_id=? ORDER BY address", proj["id"])
        syms = [Symbol(r["name"], int(r["address"]), int(r["size"] or 0), r["kind"] or "data") for r in rows]
        sym_path = root / "symbol_addrs.txt"
        sym_path.write_text(debuginfo.symbol_addrs_text(syms), encoding="utf-8")
        debug = cfg.get("debug") or {}
        header = debug.get("header") or "/* No debug types were imported for this project. */\n"
        type_path = root / "debug_types.h"
        type_path.write_text(header, encoding="utf-8")
        json_path = root / "debug_symbols.json"
        json_path.write_text(debuginfo.symbols_json(syms), encoding="utf-8")
        return {"symbols": len(syms), "types": debug.get("types", 0), "symbol_addrs": str(sym_path),
                "types_header": str(type_path), "json": str(json_path)}

    def classify_library(self, slug: str, log=print, names=()) -> int:
        """Mark open functions whose names are CRT/runtime/library code, including projects imported before this.
        Matched or hand-written functions are never touched. `names` are libraries the user already knows."""
        proj = self._project(slug)
        rows = self.store.all("SELECT id, name, meta FROM functions WHERE project_id=? AND status='open'",
                              proj["id"])
        found = []
        wanted = {n.lower() for n in names if n}
        for row in rows:
            kind = library.kind(row["name"])
            if not kind and wanted and any(w in row["name"].lower() for w in wanted):
                kind = "known library"
            if kind:
                meta = json.loads(row["meta"] or "{}")
                meta["library"] = kind
                found.append((json.dumps(meta), row["id"]))
        if found:
            with self.store.tx() as db:
                db.executemany("UPDATE functions SET status='library', meta=? WHERE id=? AND status='open'", found)
                db.execute("INSERT INTO events(project_id,agent_id,kind,detail,created_at) VALUES(?,?,?,?,?)",
                           (proj["id"], None, "library", f"{len(found)} library/CRT functions skipped", time.time()))
            log(f"{len(found)} library/CRT functions marked and skipped")
        return len(found)

    def compare_version(self, slug: str, path: str) -> dict:
        """Compare the project's own target with another build (a patch, demo or port)."""
        proj = self._project(slug)
        cfg = self.cfg(proj)
        if cfg.get("kind") == "fork":
            raise NetworkError("compare versions on a ROM or PC project, not on a fork")
        if cfg.get("platform") not in X86_PLATFORMS:
            raise NetworkError("version comparison currently covers Windows and Linux x86 programs")
        root = Path(proj["root"])
        target = root / (cfg.get("rom", {}).get("program") or cfg.get("rom", {}).get("file") or "")
        if not target.is_file():
            raise NetworkError(f"the project's target is missing: {target}")
        other = Path(path).expanduser()
        if not other.is_absolute():
            other = root / other
        if not other.is_file():
            raise NetworkError(f"no program at {other}")
        if other.resolve() == target.resolve():
            raise NetworkError("that's the project's own target; choose another version")
        names = {int(r["vram"]): r["name"] for r in self.store.all(
            "SELECT name, vram FROM functions WHERE project_id=? AND vram IS NOT NULL", proj["id"])}
        return bincompare.compare_paths(target, other, old_names=names)
