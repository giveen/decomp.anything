"""Importing a target: an artifact into an analysis project, a ROM into a matching one.

An artifact (an executable, a disc image, a JavaScript application) becomes an analysis project whose work items come
from REA's description of it; a ROM is split into functions by splat, so a candidate can be linked at the function's
own address. The import detects the platform and compiler and records what it guessed in the project config, so a
wrong guess is fixed in one place instead of in every step that follows it.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import shutil
import tarfile
import time
import zipfile
from pathlib import Path

from .. import rea, toolchains
from ..network import NetworkError
from ..rom import buildinfo, debuginfo, detect, library, match, pe, split
from ..rom.symbols import IDENT, Symbol, SymbolTable
from . import analysis, sources
from .platform import (
    BASE_HEADER,
    DEFAULT_TOOLCHAIN,
    GP_NOTE,
    LINUX_NOTE,
    WINDOWS_NOTE,
    X86_PLATFORMS,
    _linux_flags,
    arch_toolchain,
    cxx_toolchain,
    source_language,
)


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:40] or "rom"

def _preview(data: bytes | None, kind: str) -> str:
    if not data:
        return ""
    text = data.split(b"\0", 1)[0]
    if len(text) >= 3 and all(32 <= c < 127 or c in (9, 10) for c in text):
        return json.dumps(text.decode("ascii"))
    return data[:16].hex(" ")


class CreationMixin:
    def import_analysis(self, path: str | Path, *, tool: str = "open_binary", arguments: dict | None = None,
                        provider: str = "", confirmed: bool = False, copy_target: bool = False,
                        log=print) -> str:
        """Open anything with REA as a project: procedures become work items, and agents read them in parallel."""
        if not confirmed:
            raise NetworkError("confirm that you are authorized to analyze this target")
        target_path = Path(path).expanduser().resolve()
        tool = tool or "open_binary"
        if not target_path.exists():
            raise NetworkError(f"nothing at {target_path}")
        if target_path.is_dir() and tool != "analyze_javascript_application":
            raise NetworkError("a folder can only be opened as a JavaScript or Electron application")
        if target_path.is_dir() and copy_target:
            raise NetworkError("a folder is analyzed where it is; it isn't copied into the project")
        if not target_path.is_file() and not target_path.is_dir():
            raise NetworkError(f"{target_path} is not a file or a folder")
        if analysis.is_archive(target_path) and target_path.stat().st_size > analysis.MAX_ARCHIVE_INPUT:
            raise NetworkError(f"that archive is larger than {analysis.MAX_ARCHIVE_INPUT // (1024 ** 3)} GiB; "
                               "extract the part you need and analyze that instead")
        target = {"path": str(target_path), "tool": tool, "arguments": arguments or {}}
        for row in self.store.all("SELECT slug, config FROM projects"):
            cfg = json.loads(row["config"])
            old = cfg.get("target") or {}
            if cfg.get("kind") == "analysis" and old.get("path") == target["path"] and old.get("tool") == target["tool"]:
                log(f"already imported as {row['slug']}")
                return row["slug"]

        log(f"opening {target_path.name} with REA…")
        base = _slugify(target_path.stem or "analysis")
        slug, n = base, 2
        while self.store.one("SELECT 1 FROM projects WHERE slug=?", slug) or (self.workspace / slug).exists():
            slug, n = f"{base}-{n}", n + 1
        workdir = self.workspace / slug
        workdir.mkdir(parents=True)
        if copy_target:
            kept = workdir / target_path.name
            shutil.copy2(target_path, kept)
            target_path = kept
            target["path"] = str(target_path)
        try:
            attempts = [target["tool"]]
            if target["tool"] == "open_binary" and target_path.is_file():
                suffix = target_path.suffix.lower()
                # REA opens most of these with open_binary too (an .asar is an archive, an .apk is a ZIP), so the
                # inspector that actually reads the format has to come first; open_binary is the fallback.
                extra = ("inspect_firmware_regions" if suffix in (".bin", ".img", ".fw", ".rom")
                         else "analyze_javascript_application" if suffix == ".asar"  # REA reads an .asar, not one .js file
                         else "inspect_android_package" if suffix == ".apk"
                         else "inspect_managed_artifact" if suffix in (".dll", ".exe")
                         else None)
                if extra:
                    attempts = [extra, "open_binary"]
            error = None
            for tool in attempts:
                try:
                    target["tool"] = tool
                    client, opened = rea.open_analysis(target, provider)
                    break
                except rea.ReaError as e:
                    error = e
            else:
                raise error or rea.ReaError("REA couldn't open this target")
            info = analysis.opened_target(opened, str(target_path))
            recovered = {"files": {}, "maps": 0, "skipped": 0, "truncated": False}
            source_plan: list[dict] = []
            if target["tool"] == "analyze_javascript_application":
                recovered = sources.recover(target_path)
                source_plan = sources.plan(sources.application_modules(opened), recovered["files"])
            inventory = None
            if analysis.is_archive(target_path):
                try:
                    inventory = analysis.artifact_inventory(client.call(
                        "inspect_artifact", {}, timeout=analysis.INSPECT_TIMEOUT, any_tool=True))
                except rea.ReaError as e:
                    log(f"archive inventory failed: {e}")
                    inventory = None
                if inventory is not None:
                    analysis.validate_inventory(inventory)
            try:
                overview = client.call("binary_overview", {}, timeout=900)
            except rea.ReaError:
                overview = {}
            try:
                segments = analysis.clean_segments(client.call("list_segments", {}, timeout=900))
            except rea.ReaError:
                segments = []
            try:
                items = analysis.procedures(client.call("list_procedures", {}, timeout=900), segments)
            except rea.ReaError:
                items = []
            entries = analysis.artifact_entries(inventory) if inventory is not None else []
            file_count = len(entries)
            for item in entries:
                if item.get("entry_type") == "unknown":
                    try:
                        head = analysis.read_archive_entry(target_path, item["path"], 0, 16)
                        item.update(analysis.classify_entry(item["path"], head))
                    except (OSError, ValueError):
                        pass
            archive_sources: list[dict] = []
            if entries and analysis.is_archive(target_path):
                entries, archive_sources = self._archive_sources(target_path, entries, log)
            if entries or archive_sources:
                items = [*entries, *archive_sources]
            if source_plan or recovered["files"]:
                seen_paths: set[str] = set()
                source_items: list[dict] = []
                for row in source_plan:
                    path = str(row["path"])
                    if not path or path in seen_paths:
                        continue
                    seen_paths.add(path)
                    source_items.append({
                        "name": path, "path": path, "unit": posixpath.dirname(path),
                        "kind": "recovered-source" if row.get("source") is not None else "reconstruction",
                        "size": len(row["source"].encode("utf-8")) if row.get("source") is not None else 0,
                        "code": row.get("source"), "map": row.get("map") or "",
                        "node_id": row.get("node_id") or "", "module_kind": row.get("kind") or ""})
                for path, rec in sorted(recovered["files"].items()):
                    if path in seen_paths:
                        continue
                    seen_paths.add(path)
                    source_items.append({"name": path, "path": path, "unit": posixpath.dirname(path),
                                         "kind": "recovered-source", "size": len(rec["source"].encode("utf-8")),
                                         "code": rec["source"], "map": rec.get("map") or "",
                                         "node_id": "", "module_kind": ""})
                items = source_items
                done = sum(1 for i in items if i["kind"] == "recovered-source")
                log(f"{done} source files recovered from {recovered['maps']} source map(s); "
                    f"{len(items) - done} module(s) to reconstruct")
        except BaseException:
            shutil.rmtree(workdir, ignore_errors=True)
            raise
        counts = analysis.overview_counts(overview)
        if counts.get("segments") is None:
            counts["segments"] = len(segments)
        if inventory is not None:
            occurrences = inventory.get("occurrences") or []
            counts["entries"] = len(occurrences)
            counts["files"] = file_count
            counts["directories"] = sum(1 for o in occurrences if isinstance(o, dict) and o.get("entry_kind") == "directory")
        recovered_count = sum(1 for item in items if item.get("kind") == "recovered-source")
        pending_count = sum(1 for item in items if item.get("kind") == "reconstruction")
        if recovered_count or pending_count:
            counts["sources"] = recovered_count + pending_count
            counts["recovered"] = recovered_count
            counts["reconstruction"] = pending_count
        if counts.get("procedures") is None or inventory is not None:
            counts["procedures"] = len(items)
        if not items:  # anything without a procedure inventory still gets one survey item
            items = [{"name": "survey", "address": 0, "size": 0, "unit": ""}]
            log("no procedures listed: the project starts with one survey task; add more tasks in the project")
        # Every target gets a Sources tier. A binary has no recovered files at all: its tier starts empty and its
        # pending count is the work items still without reconstructed code.
        tier = ("source-map" if target["tool"] == "analyze_javascript_application"
                else "archive" if inventory is not None else "binary")
        reconstruction = ({"folder": "recovered", "files": recovered_count + pending_count,
                           "recovered": recovered_count, "reconstructed": 0, "pending": pending_count,
                           "maps": recovered["maps"], "truncated": recovered["truncated"], "tier": tier}
                          if recovered_count or pending_count or tier == "binary" else None)
        if reconstruction is not None and tier == "binary":
            reconstruction.update({"files": 0, "total": len(items), "pending": max(0, len(items) - recovered_count)})
        if inventory is not None:
            log(f"{info.get('format') or 'archive'}: {file_count} files from "
                f"{counts.get('entries', file_count)} entries")
        else:
            log(f"{info.get('format') or 'target'} {info.get('architecture') or ''}: {len(items)} work items")

        cfg = {
            "kind": "analysis", "platform": "rea", "target": info | {"tool": target["tool"],
                                                                      "arguments": target["arguments"]},
            "provider": provider or "", "counts": counts, "segments": segments,
            "artifact": ({"format": info.get("format") or "", "entries": counts.get("entries", 0),
                          "files": counts.get("files", 0), "directories": counts.get("directories", 0),
                          "inspection_id": inventory.get("inspection_id") if inventory else ""}
                         if inventory is not None else None),
            "legal": {"basis": "authorized", "license": "user's own use",
                      "statement": "opened by the user, who confirmed they are authorized to analyze it",
                      "confirmed_at": time.time()},
            "context": {"file": None},
            "reconstruction": reconstruction,
        }
        work_items = f"{len(items)} work item" + ("" if len(items) == 1 else "s")
        description = (f"{info.get('format') or 'archive'} · {counts.get('entries')} entries · "
                       f"{recovered_count} recovered · {pending_count} to reconstruct"
                       if inventory is not None and (recovered_count or pending_count) else
                       f"{info.get('format') or 'target'} · {counts.get('entries')} entries · {work_items}"
                       if inventory is not None and entries else
                       f"{info.get('format') or 'target'} · {recovered_count} recovered · {pending_count} to reconstruct"
                       if recovered_count or pending_count else
                       f"{info.get('format') or 'target'} · {info.get('architecture') or 'unknown'} · {work_items}")
        now = time.time()
        names: set[str] = set()
        with self.store.tx() as db:
            pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) "
                             "VALUES(?,?,?,?,?,?,?)",
                             (slug, target_path.name, description, info.get("architecture") or "unknown", 0,
                              str(workdir), json.dumps(cfg))).lastrowid
            for item in items:
                name = analysis.safe_name(item["name"], item.get("address"))
                if name in names:  # an archive can repeat a basename in two directories
                    base, n = name, 2
                    while name in names:
                        name, n = f"{base}~{n}", n + 1
                names.add(name)
                if item.get("kind") == "artifact-entry":
                    meta = {"kind": "artifact-entry", "address": 0, **{k: item.get(k) for k in (
                        "path", "entry_kind", "artifact_kind", "artifact_format", "sha256", "compressed_size",
                        "executable", "encrypted", "entry_type", "entry_format", "tool", "analyzable")}}
                elif item.get("kind") in ("recovered-source", "reconstruction"):
                    meta = {"kind": item["kind"], "path": item["path"], "address": 0,
                            "origin": item.get("origin") or
                            ("source-map" if item["kind"] == "recovered-source" else "bundle"),
                            "verified": item["kind"] == "recovered-source", "map": item.get("map") or "",
                            "node_id": item.get("node_id") or "", "module_kind": item.get("module_kind") or ""}
                    if item.get("entry_type"):  # an archive entry: keep its facts for Contents and child targets
                        meta.update({"entry_type": item["entry_type"], "entry_format": item.get("entry_format") or "",
                                     "entry_kind": "file", "sha256": item.get("sha256") or "",
                                     "tool": item.get("tool") or "", "analyzable": bool(item.get("analyzable"))})
                else:
                    meta = {"kind": "survey" if name == "survey" and not item.get("address") else "procedure",
                            "address": item.get("address") or 0}
                status = "matched" if item.get("kind") == "recovered-source" else "open"
                db.execute(
                    "INSERT INTO functions(project_id,name,size,n_insns,unit,vram,meta,status) VALUES(?,?,?,?,?,?,?,?)",
                    (pid, name, item.get("size") or 0, 0, item.get("unit") or "", item.get("address") or 0,
                     json.dumps(meta), status))
                if item.get("kind") == "recovered-source" and item.get("code"):
                    db.execute("UPDATE functions SET matched_source=?, best_source=?, best_score=0, matched_at=? "
                               "WHERE project_id=? AND name=?", (item["code"], item["code"], now, pid, name))
            db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                       "VALUES(?,?,?,?,?,?,?)", (pid, 1, f"// REA analysis target: {target_path}\n", None,
                                                  "analysis target", now, ""))
            self._event(db, pid, None, "import", f"{len(items)} work items from {target_path.name}")
        if recovered_count:
            folder = workdir / "recovered"
            written = 0
            for item in items:
                code, path = item.get("code"), str(item.get("path") or "")
                if not code or not path:
                    continue
                rel = analysis.safe_rel_path(path)
                if not rel:  # an archive entry called '../x': it stays a work item, nothing is written outside
                    log(f"not writing recovered/{path}: that path climbs out of the project")
                    continue
                out = folder / rel
                try:
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_text(code, encoding="utf-8")
                    written += 1
                except OSError as e:
                    log(f"couldn't write recovered/{path}: {e}")
            log(f"{written} recovered source files in {folder}")
        return slug

    @staticmethod
    def _archive_sources(target_path: Path, entries: list[dict], log=print) -> tuple[list[dict], list[dict]]:
        """Split an archive's regular files into code that comes back exactly (text source, verified) and code whose
        source didn't ship (compiled entries to reconstruct). Everything else stays a plain work item."""
        kept: list[dict] = []
        sources: list[dict] = []
        budget = analysis.MAX_RECOVERED_BYTES
        archive = None
        try:
            if zipfile.is_zipfile(target_path):
                archive = zipfile.ZipFile(target_path)
            elif tarfile.is_tarfile(target_path):
                archive = tarfile.open(target_path)  # noqa: SIM115
        except (OSError, tarfile.TarError, zipfile.BadZipFile):
            archive = None

        try:
            for item in entries:
                path = str(item.get("path") or "")
                if item.get("encrypted"):  # locked entry: it stays a work item, its code can't be recovered here
                    kept.append(item)
                    continue
                language, compiled = analysis.source_language(path), analysis.compiled_language(path)
                if language:
                    if int(item.get("size") or 0) > min(budget, analysis.MAX_SOURCE_BYTES):
                        kept.append(item)  # too large to recover here: it stays a work item in the archive
                        continue
                    text = analysis.decode_source(analysis.read_entry_bytes(target_path, path, archive=archive))
                    if text is not None and len(text.encode("utf-8")) <= budget:
                        budget -= len(text.encode("utf-8"))
                        sources.append({**item, "kind": "recovered-source", "code": text, "origin": "archive",
                                        "module_kind": language, "entry_type": "source",
                                        "entry_format": language, "map": f"archive entry {path}"})
                    elif text is not None or item.get("tool"):
                        kept.append(item)  # over the budget, or a program this app can open as its own target
                    else:  # not readable as text: the source has to be reconstructed
                        sources.append({**item, "kind": "reconstruction", "origin": "archive", "module_kind": language,
                                        "entry_type": "source", "entry_format": language})
                    continue
                if compiled:
                    sources.append({**item, "kind": "reconstruction", "origin": "archive", "module_kind": compiled,
                                    "entry_type": "compiled", "entry_format": compiled})
                    continue
                kept.append(item)
        finally:
            if archive is not None:
                archive.close()
        if sources:
            done = sum(1 for source in sources if source["kind"] == "recovered-source")
            log(f"{done} source file(s) recovered from the archive, {len(sources) - done} to reconstruct")
        return kept, sources

    def import_rom(self, data: bytes | Path, *, toolchain: str | None = None, flags: list[str] | None = None,
                   confirmed: bool = False, name: str = "", log=print) -> str:
        """Import a game the user owns (ROM bytes, or a file such as a disc image): split it, store every function
        as a target. Returns the slug."""
        if not confirmed:
            raise NetworkError("confirm that this is your own legally obtained copy of the game")
        source = Path(data).expanduser().resolve() if isinstance(data, (str, Path)) else None
        digest = None
        if source is not None:
            try:
                h = hashlib.sha1()
                with source.open("rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                digest = h.hexdigest()
                log(f"checking {source.name} ({source.stat().st_size // 1024} KiB)")
            except OSError:
                digest = None
        elif isinstance(data, (bytes, bytearray)):
            digest = hashlib.sha1(data).hexdigest()
        if digest is not None:
            for p in self.store.all("SELECT slug, config FROM projects"):
                if json.loads(p["config"]).get("rom", {}).get("sha1") == digest:
                    log(f"already imported as {p['slug']}")
                    return p["slug"]
        project_file = self._community_layout(data, name)
        if project_file is not None:  # a dtk-template or ds-decomp project, worked on like a fork
            compiler = toolchain if toolchain and toolchain.startswith(("GC/", "Wii/", "DS/")) else None
            return self.forks.create_scratch(project_file, compiler=compiler, log=log)
        log(f"finding functions in {source.name if source is not None else (name or 'the file')}…")
        picked = detect.identify(data, name)
        rom, info = picked.data, picked.info
        for p in self.store.all("SELECT slug, config FROM projects"):
            if json.loads(p["config"]).get("rom", {}).get("sha1") == info["sha1"]:
                log(f"already imported as {p['slug']}")
                return p["slug"]
        base = _slugify(info["title"])
        slug, n = base, 2
        while self.store.one("SELECT 1 FROM projects WHERE slug=?", slug) or (self.workspace / slug).exists():
            slug, n = f"{base}-{n}", n + 1
        workdir = self.workspace / slug
        workdir.mkdir(parents=True)
        (workdir / picked.filename).write_bytes(rom)
        size = f"{info['size'] // (1024 * 1024)} MiB" if info["size"] >= 1 << 20 else f"{info['size'] // 1024} KiB"
        log(f"{detect.PLATFORMS[picked.platform]}: {info['title']} ({info['game_code'] or 'no game code'}, "
            f"{info['region']}), {size}, looks built with {info.get('build_label') or info['compiler_guess']}")
        if picked.platform == "gba":
            return self._import_gba(picked, slug, workdir, toolchain, flags, log)
        if picked.platform in X86_PLATFORMS:
            return self._import_windows(picked, slug, workdir, toolchain, flags, log, source=source)
        if picked.overlays:
            log(f"{len(picked.overlays)} overlay{'s' if len(picked.overlays) != 1 else ''} on the disc: "
                + ", ".join(f"{o[0]} at 0x{o[2]:08X}" for o in picked.overlays[:6]) + (", ..." if len(picked.overlays) > 6 else ""))
        res = split.split_rom(workdir, picked.filename, log=log, overlays=picked.overlays)
        if res.image and res.image != picked.filename:  # splat works on a flat image made from the program
            rom = (workdir / res.image).read_bytes()

        toolchain = toolchain or DEFAULT_TOOLCHAIN.get(res.compiler, DEFAULT_TOOLCHAIN[info["compiler_guess"]])
        tc = toolchains.get(toolchain)
        default_flags = tc.flags
        if picked.platform in ("psx", "ps2"):
            default_flags = [f for f in tc.flags if not f.startswith("-G")] + [f"-G{8 if res.gp else 0}"]
        cfg = {
            "kind": "rom", "platform": picked.platform, "big_endian": picked.big_endian, "gp": res.gp,
            "rom": {**info, "file": res.image or picked.filename, "program": picked.filename},
            "segments": [vars(s) for s in res.segments],
            "compiler": {"toolchain": toolchain, "flags": flags or default_flags, "timeout": 20},
            "legal": {"basis": "personal-copy", "license": "personal use only",
                      "statement": "imported by the owner of this copy of the game, who confirmed it was legally obtained",
                      "confirmed_at": time.time()},
            "context": {"file": None},
        }
        symbols = SymbolTable()
        for name, addr in res.named.items():
            symbols.add(Symbol(name, addr, 0, "func"), reindex=False)
        for fn in res.functions:
            symbols.add(Symbol(fn.name, fn.vram, fn.size, "func"), reindex=False)
        rom_bytes, segs = rom, res.segments

        def read(addr, n):
            for seg in segs:
                if seg.contains(addr, n):
                    at = seg.rom_start + addr - seg.vram
                    return rom_bytes[at:at + n]
            return None

        data_rows = []
        for d in res.data:
            if d.name in symbols.by_name:
                continue
            symbols.add(Symbol(d.name, d.vram, d.size, d.kind), reindex=False)
            data_rows.append((d.name, d.vram, d.size, d.kind, _preview(read(d.vram, min(d.size, 48)), d.kind) if d.kind != "bss" else ""))
        symbols._reindex()

        now = time.time()
        with self.store.tx() as db:
            pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                             (slug, info["title"], f"{info['game_code']} · {info['region']} · {len(res.functions)} functions",
                              "mips", int(picked.big_endian), str(workdir), json.dumps(cfg))).lastrowid
            for fn in res.functions:
                target = match.target_from_split(fn, symbols.resolve, picked.big_endian, res.gp)
                db.execute(
                    "INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,seed,unit,vram,rom_offset) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, fn.name, fn.size, len(fn.lines), target.code, json.dumps(target.to_json()),
                     self._asm_source(fn), None, fn.segment, fn.vram, fn.rom))
            for s in symbols.by_name.values():
                preview = next((r[4] for r in data_rows if r[0] == s.name), None)
                db.execute("INSERT INTO symbols(project_id,name,address,size,kind,preview) VALUES(?,?,?,?,?,?)",
                           (pid, s.name, s.address, s.size, s.kind, preview))
            db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                       "VALUES(?,?,?,?,?,?,?)", (pid, 1, BASE_HEADER.format(title=info["title"]) + (GP_NOTE if res.gp else ""),
                                                  None, "basic types", now, ""))
            self._event(db, pid, None, "import", f"{len(res.functions)} functions from {info['title']}")
        log(f"ready: {len(res.functions)} functions to decompile")
        return slug

    def _import_gba(self, picked, slug: str, workdir: Path, toolchain: str | None, flags: list[str] | None, log) -> str:
        """Game Boy Advance: functions found by luvdis (plus a pass for code nothing calls), judged by the ARM matcher."""
        from ..rom import gba

        info = picked.info
        fns = gba.split(workdir / picked.filename, workdir, log=log)
        toolchain = toolchain or "agbcc"
        tc = toolchains.get(toolchain)
        cfg = {
            "kind": "rom", "platform": "gba", "big_endian": False, "gp": 0, "arch": "thumb",
            "rom": {**info, "file": picked.filename, "program": picked.filename},
            "segments": [{"name": "rom", "rom_start": 0, "rom_end": info["size"], "vram": gba.BASE}],
            "compiler": {"toolchain": toolchain, "flags": flags or tc.flags, "timeout": 20},
            "legal": {"basis": "personal-copy", "license": "personal use only",
                      "statement": "imported by the owner of this copy of the game, who confirmed it was legally obtained",
                      "confirmed_at": time.time()},
            "context": {"file": None},
        }
        now = time.time()
        with self.store.tx() as db:
            pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                             (slug, info["title"], f"{info['game_code']} · {info['region']} · {len(fns)} functions",
                              "arm", 0, str(workdir), json.dumps(cfg))).lastrowid
            names: set[str] = set()
            for f in fns:
                name = f.asm_only or f.name  # known hand-written helpers keep their real name and stay assembly
                if name in names:  # a game can link a helper twice (Spyro Orange has two of _call_via_r3)
                    name = f"{name}_{f.vram:08X}"
                names.add(name)
                db.execute(
                    "INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,seed,unit,vram,rom_offset,status) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, name, len(f.code), f.n_insns, f.code, json.dumps([list(r) for r in f.refs or []]),
                     f.asm.replace(f.name, name), None, "rom", f.vram, f.rom, "asm" if f.asm_only else "open"))
                db.execute("INSERT INTO symbols(project_id,name,address,size,kind,preview) VALUES(?,?,?,?,?,?)",
                           (pid, name, f.vram, len(f.code), "func", None))
            db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                       "VALUES(?,?,?,?,?,?,?)", (pid, 1, BASE_HEADER.format(title=info["title"]), None, "basic types", now, ""))
            self._event(db, pid, None, "import", f"{len(fns)} functions from {info['title']}")
        log(f"ready: {len(fns)} functions to decompile")
        return slug

    @staticmethod
    def _merge_debug(image, debug) -> None:
        """Put the names a PDB/DWARF/MAP already knows onto the split functions, without inventing new work items."""
        by_addr = {f.vram: f for f in image.functions}
        names = {s.name for s in image.symbols}
        for meta in debug.functions:
            fn = by_addr.get(meta.address)
            if fn is None or not meta.name or not IDENT.match(meta.name):
                continue
            if fn.name == meta.name:
                continue
            if meta.name in names:
                continue  # another function or data already owns that name
            old = fn.name
            fn.name = meta.name
            fn.asm = (fn.asm.replace(f"glabel {old}", f"glabel {meta.name}")
                      .replace(f"endlabel {old}", f"endlabel {meta.name}"))
            for sym in image.symbols:
                if sym.address == meta.address and sym.name == old:
                    sym.name, sym.kind = meta.name, "func"
                    if meta.size:
                        sym.size = meta.size
            names.discard(old)
            names.add(meta.name)
        by_name = {s.name: s for s in image.symbols}
        for sym in debug.symbols:
            if sym.name not in by_name:
                image.symbols.append(sym)
                by_name[sym.name] = sym
            elif sym.kind == "func" and by_name[sym.name].kind != "func":
                by_name[sym.name].kind = "func"

    def _import_windows(self, picked, slug: str, workdir: Path, toolchain: str | None, flags: list[str] | None,
                        log, source: Path | None = None) -> str:
        """Windows and Linux programs: functions from .pdata/.eh_frame/exports/symbols/call targets, matched
        against a compiled object by the x86 matcher. The target is the program's own image, so every function's
        bytes and addresses are final."""
        from ..rom import elfexe

        linux = picked.platform == "linux"
        image = getattr(picked, "image", None) or (elfexe if linux else pe).read(picked.data, picked.filename)
        info = picked.info  # the name the user picked (not the canonical game.exe the copy is stored as)
        debug = None
        try:
            debug = debuginfo.discover(source or (workdir / picked.filename), picked.data, image=image)
        except (debuginfo.DebugInfoError, OSError, ValueError) as e:
            log(f"debug information not imported: {e}")
        if debug is not None:
            self._merge_debug(image, debug)
            log(f"debug information from {debug.source}: {len(debug.symbols)} names, {len(debug.types)} types")
        packed = not linux and (b"UPX!" in picked.data[:0x1000] or any(s.name.upper().startswith("UPX") for s in image.sections))
        if packed:
            log("this looks UPX-packed: unpack it first (UPX is in Compilers & tools), then import the unpacked program")
        suffix = "x86_64" if image.arch == "x86_64" else "x86"
        cxx = buildinfo.looks_cxx(image.symbols)
        requested = toolchain
        if toolchain:
            toolchain = arch_toolchain(toolchain, image.arch)
            if cxx and source_language(toolchain) != "C++":
                toolchain = cxx_toolchain(toolchain)
            if toolchain != requested:
                log(f"using {toolchain} for this {image.arch} target (not {requested})")
        if linux:
            # .comment names the compiler; C++ programs call into libstdc++, whose names are mangled (_Z...).
            family = "clang" if info.get("language_guess") == "Rust" or "clang" in info["compiler_guess"].lower() else "gcc"
            driver = {"gcc": "gxx", "clang": "clangxx"}[family] if cxx else family
            toolchain = toolchain or f"{driver}-linux-{suffix}"
        elif not toolchain:
            # MSVC's ABI is matched by clang-cl; MinGW's by MinGW's C++ driver.
            msvc = info["compiler_guess"] == "MSVC" or any(s.name.startswith("?") for s in image.symbols)
            toolchain = f"{'clang-clxx' if msvc else 'mingwxx'}-{suffix}" if cxx else f"mingw-{suffix}"
        tc = toolchains.get(toolchain)
        base_flags = flags or (_linux_flags(tc.flags, info.get("pie", False)) if linux else tc.flags)
        suggestion = buildinfo.infer_flags(
            picked.data, compiler="MSVC" if toolchain.startswith(("clang-cl", "msvc")) else "GNU", cxx=cxx,
            base_flags=base_flags, symbols=image.symbols, section_names=[s.name for s in image.sections])
        info["suggested_flags"] = suggestion["flags"]
        info["flag_evidence"] = suggestion["evidence"]
        info["flag_style"] = suggestion["style"]
        if not flags and suggestion["flags"] != list(tc.flags):
            flags = suggestion["flags"]
            info["flags_inferred"] = True
            log("build flags inferred: " + " ".join(flags))
        log(f"detected {info.get('build_label') or info['compiler_guess']}; matching with {tc.name}")
        if info.get("matchable") is False:
            log(f"{info.get('build_label') or 'this language'} can't be rebuilt from C/C++: inspect and annotate it, "
                f"but functions won't match the original compiler output")
        if toolchains.installed(toolchain) is None:
            log(f"{tc.name} isn't on this computer: functions import and can be inspected, but compiling needs it "
                f"(install it, or set another [compiler] for the project)")
        if linux and not flags:
            flags = base_flags
        cfg = {
            "kind": "rom", "platform": picked.platform, "arch": image.arch, "big_endian": False, "gp": 0,
            "packed": bool(packed),
            "rom": {**info, "file": picked.filename, "program": picked.filename},
            "segments": image.segments,
            "compiler": {"toolchain": toolchain, "flags": flags or tc.flags, "timeout": 20},
            "legal": {"basis": "personal-copy", "license": "personal use only",
                      "statement": "imported by the owner of this copy of the game, who confirmed it was legally obtained",
                      "confirmed_at": time.time()},
            "context": {"file": None},
        }
        if debug is not None and (debug.header or debug.functions):
            cfg["debug"] = {"source": debug.source, "symbols": len(debug.symbols), "types": len(debug.types),
                            "header": debug.header, "imported_at": time.time()}
        names: set[str] = set()
        now = time.time()
        header = BASE_HEADER.format(title=info["title"]) + (LINUX_NOTE if linux else WINDOWS_NOTE)
        if debug is not None and debug.header:
            debug_header = header + "\n/* Debug information imported from " + debug.source.replace("*/", "") + " */\n" + debug.header
            try:
                from ..compiler import compile_source
                from ..project import compiler_config
                if not compile_source(compiler_config(workdir, cfg), debug_header, "").ok:
                    log("debug types don't compile with this project's compiler: keeping them out of the shared header")
                else:
                    header = debug_header
            except Exception as e:  # noqa: BLE001
                log(f"debug types not added to the shared header: {e}")
        with self.store.tx() as db:
            pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                             (slug, info["title"], f"{image.arch} · {len(image.functions)} functions · "
                                                   f"{info['compiler_guess']}", image.arch, 0, str(workdir),
                              json.dumps(cfg))).lastrowid
            libs = 0
            for f in image.functions:
                name = f.name
                if name in names:
                    name = f"{name}_{f.vram:08X}"
                names.add(name)
                lib = library.kind(f.name)
                libs += bool(lib)
                db.execute(
                    "INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,seed,unit,vram,rom_offset,status,meta) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, name, len(f.code), f.n_insns, f.code, json.dumps([list(r) for r in f.refs]),
                     f.asm.replace(f.name, name), None, f.section, f.vram, f.rom,
                     "library" if lib else "open", json.dumps({"library": lib}) if lib else None))
            for sym in image.symbols:
                db.execute("INSERT OR REPLACE INTO symbols(project_id,name,address,size,kind,preview) "
                           "VALUES(?,?,?,?,?,?)", (pid, sym.name, sym.address, sym.size, sym.kind, sym.preview or None))
            db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                       "VALUES(?,?,?,?,?,?,?)", (pid, 1, header, None, "basic types", now, ""))
            self._event(db, pid, None, "import", f"{len(image.functions)} functions from {info['title']}")
        log(f"ready: {len(image.functions)} functions to decompile" + (f" ({libs} library/CRT functions skipped)" if libs else ""))
        return slug

    def _community_layout(self, data: bytes | Path, name: str = "") -> Path | None:
        """The file, when the game starts as a community-layout project: a GameCube/Wii program (DOL) or disc image
        (dtk-template), or a DS ROM (ds-decomp). Bytes are saved to a file first."""
        from ..forks import scratch_dsd, scratch_dtk

        if isinstance(data, (str, Path)):
            with Path(data).open("rb") as fh:
                head = fh.read(0x440)
        else:
            head = bytes(data[:0x440])
        small = Path(data).read_bytes() if isinstance(data, (str, Path)) and Path(data).stat().st_size < 64 << 20 else data
        nds = scratch_dsd.is_nds(head)
        if not nds and scratch_dtk.disc_kind(head) is None and not (isinstance(small, (bytes, bytearray))
                                                                   and scratch_dtk.is_dol(small)):
            return None
        if isinstance(data, (str, Path)):
            return Path(data)
        uploads = self.workspace / ".decomp" / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        path = uploads / (Path(name).name or ("game.nds" if nds else "main.dol" if scratch_dtk.is_dol(data) else "game.iso"))
        path.write_bytes(data)
        return path
