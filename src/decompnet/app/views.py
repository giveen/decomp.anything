"""What the page is shown: the overview, the map, the live view, a function's diff.

These read the database and the project config and say what they found, so the page, an agent's project_info and the
intake report show the same numbers. Two write: recheck_project re-detects a target imported before this version could,
and _backfill_detection fills in what an older config never recorded.
"""

# pyright: reportAttributeAccessIssue=false
# Each part is read on its own, so every self.<name> that lives in another part (or in Network) looks unknown to a
# type checker. RomNetwork is one class at runtime, and tests/test_engine_parts.py proves every name resolves there.


from __future__ import annotations

import contextlib
import hashlib
import json
import time
from pathlib import Path

from .. import scoring, toolchains, tracefile
from ..network import NetworkError
from ..rom import buildinfo, detect, pe
from .platform import (
    PLATFORM_COMPILERS,
    SHORT_NAMES,
    X86_PLATFORMS,
    _linux_flags,
    arch_toolchain,
    cxx_toolchain,
    source_ext,
    source_language,
)


class ViewsMixin:
    def rea_project(self, path: str) -> dict | None:
        """The decomp.anything project whose target is this file, with its functions by address.

        The RE console opens any file, and REA reports it at its own image base (a Linux ELF comes back at 0x100000 and
        up), while a project's functions are at the file's real addresses. So the console rebases with the base its own
        segments give it, and here every address is the project's."""
        if not path:
            return None
        wanted = Path(path).expanduser()
        try:
            wanted = wanted.resolve()
        except OSError:
            return None
        for row in self.store.all("SELECT slug, name, root, config FROM projects ORDER BY id DESC"):
            cfg = self.cfg(row)
            name = (cfg.get("rom") or {}).get("program") or (cfg.get("rom") or {}).get("file")
            if not name:
                continue
            try:
                if Path(row["root"], name).resolve() != wanted:
                    continue
            except OSError:
                continue
            proj = self._project(row["slug"])
            rows = self.store.all("SELECT name, vram, size, status FROM functions WHERE project_id=? AND vram IS NOT NULL "
                                 "ORDER BY vram", proj["id"])
            return {"slug": proj["slug"], "title": proj["name"], "kind": cfg.get("kind") or "rom",
                    "platform": cfg.get("platform") or "",
                    "functions": [[int(r["vram"]), r["name"], r["status"], int(r["size"] or 0)] for r in rows]}
        return None

    def overview(self, slug: str, *, backfill: bool = False) -> dict:
        proj = self._project(slug)
        last = self.store.one("SELECT MAX(created_at) AS t FROM events WHERE project_id=?", proj["id"])["t"]
        cfg = self.cfg(proj)
        # matches by the helper agents the app runs (their spend is what `spend` counts)
        helpers = self.store.one(
            "SELECT COUNT(*) AS n FROM functions f JOIN agents a ON a.id=f.matched_agent_id WHERE f.project_id=? "
            "AND f.status='matched' AND a.origin='local' AND a.name NOT IN ('you','autopilot')", proj["id"])["n"]
        twins = self.twins.jobs.get(proj["slug"])
        return self._overview(slug, proj, backfill) | {"last_activity": last, "autopilot": self.autopilot_status(slug),
                                             "permuter": self.permuter.status(proj["slug"]),
                                             "twins": None if twins is None else {
                                                 k: getattr(twins, k) for k in ("state", "pairs", "tried", "matched", "asm")},
                                             "helper_matches": helpers,
                                             "spend": cfg.get("spend"), "instructions": cfg.get("instructions") or "",
                                             "priority": cfg.get("priority", [])}

    def _repair_pc_toolchain(self, proj, cfg) -> None:
        """A PC project must compile for the target's architecture. Older imports and manual picks can name
        the x86-64 variant for a 32-bit target; fix that before anything compiles."""
        if cfg.get("platform") not in X86_PLATFORMS or not cfg.get("arch"):
            return
        old = cfg.get("compiler", {}).get("toolchain") or ""
        try:
            toolchains.get(old)
        except toolchains.ToolchainError:
            return
        fixed = arch_toolchain(old, cfg["arch"])
        if (cfg.get("rom") or {}).get("language_guess") == "C++" and source_language(fixed) != "C++":
            fixed = cxx_toolchain(fixed)
        if fixed == old:
            return
        cfg["compiler"]["toolchain"] = fixed
        with self.store.tx() as db:
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))
            self._event(db, proj["id"], None, "compiler", f"{fixed} for this {cfg['arch']} target")

    def _backfill_detection(self, proj, cfg, force: bool = False) -> dict:
        """Old PC projects stored only a rough .comment guess; read the target once and keep the details.
        `force` re-reads it even when every fact is cached: the target or the detector's knowledge can change."""
        rom = cfg["rom"]
        name = rom.get("program") or rom.get("file")
        if not name:
            return rom
        path = Path(proj["root"]) / name
        try:
            if not path.is_file() or path.stat().st_size > 256 * 1024 * 1024:
                return rom
            picked = detect.identify(path, name)
        except Exception:  # noqa: BLE001
            return rom
        if picked.platform not in X86_PLATFORMS:
            return rom
        try:
            from ..rom import elfexe
            image = (elfexe if picked.platform == "linux" else pe).read(picked.data, picked.filename)
        except Exception:  # noqa: BLE001
            return rom
        changed = False
        for key in ("language_guess", "compiler_version", "build_label", "matchable", "compiler_guess"):
            value = picked.info.get(key)
            if value is not None and rom.get(key) != value:
                rom[key] = value
                changed = True
        old_tc = cfg["compiler"]["toolchain"]
        tc_id = arch_toolchain(old_tc, image.arch)
        if picked.info.get("language_guess") == "C++" and source_language(tc_id) != "C++":
            tc_id = cxx_toolchain(tc_id)
        if tc_id != old_tc:
            cfg["compiler"]["toolchain"] = tc_id
            changed = True
        if cfg.get("arch") != image.arch:
            cfg["arch"] = image.arch
            changed = True
        want_style = "msvc" if tc_id.startswith(("clang-cl", "msvc")) else "gnu"
        if force or not rom.get("suggested_flags") or rom.get("flag_style") != want_style:
            try:
                tc = toolchains.get(tc_id)
                base = cfg["compiler"].get("flags") or tc.flags
                suggestion = buildinfo.infer_flags(
                    picked.data, compiler="MSVC" if want_style == "msvc" else "GNU",
                    cxx=picked.info.get("language_guess") == "C++", base_flags=base, symbols=image.symbols,
                    section_names=[s.name for s in image.sections])
                rom["suggested_flags"], rom["flag_evidence"] = suggestion["flags"], suggestion["evidence"]
                rom["flag_style"] = suggestion["style"]
                changed = True
            except Exception:  # noqa: BLE001
                pass
        if changed:
            cfg["rom"] = rom
            with self.store.tx() as db:
                db.execute("UPDATE projects SET config=?, arch=? WHERE id=?",
                           (json.dumps(cfg), cfg.get("arch") or proj["arch"], proj["id"]))
        self.classify_library(proj["slug"], log=lambda *_: None)
        return rom

    def recheck_project(self, slug: str) -> dict:
        """Re-read the target and refresh the facts agents work from: detected compiler/version/language,
        inferred flags, library classification and INTAKE.md. Verified work is never touched, and the
        compiler isn't switched silently (Check matches is how a switch is justified)."""
        proj = self._project(slug)
        if self.fork_slug(slug):
            raise NetworkError("a fork builds from its own source: its facts come from the project")
        if self.is_analysis(proj):
            raise NetworkError("an analysis project's facts come from its REA provider, not from a build")
        cfg = self.cfg(proj)
        rom = cfg.get("rom") or {}
        target = rom.get("program") or rom.get("file")
        if cfg.get("platform") in X86_PLATFORMS and (not target or not (Path(proj["root"]) / target).is_file()):
            raise NetworkError(f"the project's target is missing ({Path(proj['root']) / (target or '')}): "
                               "put it back, then re-check")
        before = self._facts(cfg)
        lib_before = self.store.one("SELECT COUNT(*) AS n FROM functions WHERE project_id=? AND status='library'",
                                    proj["id"])["n"]
        if cfg.get("platform") in X86_PLATFORMS:
            self._backfill_detection(proj, cfg, force=True)
        else:
            self.classify_library(slug, log=lambda *_: None)
        cfg = self.cfg(self._project(slug))
        lib_after = self.store.one("SELECT COUNT(*) AS n FROM functions WHERE project_id=? AND status='library'",
                                   proj["id"])["n"]
        rom = cfg.get("rom") or {}
        after = self._facts(cfg)
        changed = [f"{label}: {old} → {new}" if old else f"{label}: {new}"
                   for (label, old), (_same, new) in zip(before, after, strict=True) if old != new]
        if lib_after != lib_before:
            changed.append(f"library/CRT functions: {lib_before} → {lib_after}")
        rea_counts: dict = {}
        try:  # the same pass an import runs: memory regions, strings, what REA sees that the split doesn't
            rea_counts = self.rea_inventory(slug, log=lambda *_: None)
        except Exception:  # noqa: BLE001 - a re-check must not fail because REA is missing or mid-restart
            rea_counts = {}
        if rea_counts.get("added"):
            changed.append(f"REA: +{rea_counts.get('strings', 0)} strings, {rea_counts.get('regions', 0)} memory "
                           f"regions, {rea_counts.get('functions', 0)} procedures the split doesn't cover")
        report = self.write_intake(slug, log=lambda *_: None)
        agent, _ = self.local_agent("recheck")
        now = time.time()
        with self.store.tx() as db:
            self._event(db, proj["id"], agent.id, "recheck", "; ".join(changed) or "facts unchanged")
            if changed:  # a note only when agents have something new to act on
                db.execute("INSERT INTO notes(project_id,function_id,agent_id,text,created_at) VALUES(?,?,?,?,?)",
                           (proj["id"], None, agent.id, f"Re-checked the target: {'; '.join(changed)}. "
                            "The refreshed facts are in INTAKE.md.", now))
        tc = toolchains.get(cfg["compiler"]["toolchain"])
        return {"changed": changed, "detected": {"label": rom.get("build_label") or "",
                                                  "compiler": rom.get("compiler_guess") or "",
                                                  "version": rom.get("compiler_version") or "",
                                                  "language": rom.get("language_guess") or ""},
                "compiler": {"toolchain": tc.id, "name": SHORT_NAMES.get(tc.id, tc.name)},
                "flags": rom.get("suggested_flags") or [], "library": lib_after - lib_before,
                "intake": report.get("path") or "", "functions": self.store.one(
                    "SELECT COUNT(*) AS n FROM functions WHERE project_id=? AND status NOT IN ('library', 'unmatchable')",
                    proj["id"])["n"]}

    @staticmethod
    def _facts(cfg) -> list[tuple[str, str]]:
        """(label, value) rows a re-check compares, in a fixed order."""
        rom, comp = cfg.get("rom") or {}, cfg.get("compiler") or {}
        return [("compiler", comp.get("toolchain") or ""),
                ("architecture", cfg.get("arch") or ""),
                ("language", rom.get("language_guess") or ""),
                ("detected build", rom.get("build_label") or rom.get("compiler_guess") or ""),
                ("compiler version", rom.get("compiler_version") or ""),
                ("suggested flags", " ".join(rom.get("suggested_flags") or []))]

    def _overview(self, slug: str, proj, backfill: bool = False) -> dict:
        if (fork := self.fork_slug(slug)):
            return self.forks.overview(fork)
        if self.is_analysis(proj):
            return self._analysis_overview(proj)
        cfg = self.cfg(proj)
        if cfg.get("platform") in X86_PLATFORMS:
            self._repair_pc_toolchain(proj, cfg)
        agg = self.store.one(
            "SELECT COUNT(*) AS total, SUM(status='matched') AS matched, SUM(size) AS bytes, "
            "SUM(CASE WHEN status='matched' THEN size ELSE 0 END) AS matched_bytes, SUM(attempts) AS attempts, "
            "SUM(hits>0) AS traced, SUM(hits) AS hits, COALESCE(SUM(status='reconstructed'), 0) AS reconstructed "
            "FROM functions WHERE project_id=? AND status NOT IN ('library', 'unmatchable')", proj["id"])
        lib = self.store.one("SELECT COUNT(*) AS n FROM functions WHERE project_id=? AND status='library'",
                             proj["id"])["n"]
        retired = self.store.one("SELECT COUNT(*) AS n FROM functions WHERE project_id=? AND status='unmatchable'",
                                 proj["id"])["n"]
        rom = cfg["rom"]
        detected = None
        if cfg.get("platform") in X86_PLATFORMS:
            want_style = "msvc" if cfg["compiler"]["toolchain"].startswith(("clang-cl", "msvc")) else "gnu"
            try:
                tool_arch = toolchains.get(cfg["compiler"]["toolchain"]).arch
            except toolchains.ToolchainError:
                tool_arch = ""
            wrong_arch = bool(tool_arch and cfg.get("arch") and tool_arch != cfg["arch"])
            if backfill and (not rom.get("build_label") or not rom.get("suggested_flags") or
                             rom.get("flag_style") != want_style or wrong_arch or (
                    rom.get("language_guess") == "C++" and source_language(cfg["compiler"]["toolchain"]) != "C++")):
                rom = self._backfill_detection(proj, cfg)
            language = rom.get("language_guess") or source_language(cfg["compiler"]["toolchain"])
            guess = rom.get("compiler_guess") or ""
            label = rom.get("build_label") or buildinfo.label_from_guess(guess, language)
            detected = {"language": language, "compiler": guess, "version": rom.get("compiler_version") or "",
                        "label": label, "matchable": rom.get("matchable", True)}
        tc = toolchains.get(cfg["compiler"]["toolchain"])
        return {
            "slug": proj["slug"], "title": proj["name"], "description": proj["description"],
            "platform": cfg.get("platform", "n64"), "platform_name": detect.PLATFORMS.get(cfg.get("platform", "n64"), ""),
            "compilers": PLATFORM_COMPILERS.get(cfg.get("platform", "n64"), []),
            "rom": {k: rom.get(k) for k in ("title", "game_code", "region", "size", "sha1", "byte_order", "compiler_guess")},
            "detected": (detected or {}) | ({"suggested_flags": rom.get("suggested_flags") or [],
                                            "flag_evidence": rom.get("flag_evidence") or []} if detected else {}),
            "compiler": {"toolchain": tc.id, "name": SHORT_NAMES.get(tc.id, tc.name), "flags": cfg["compiler"]["flags"],
                         "installed": toolchains.installed(tc.id) is not None, "proprietary": tc.proprietary,
                         "supported": tc.supported, "system": tc.system, "wsl": tc.via_wsl,
                         "command": tc.cmd[0] if tc.cmd else "",
                         "download": tc.download if toolchains.current_platform().startswith("windows") else "",
                         "why": None if tc.supported else toolchains.unsupported_cpu()},
            "source_language": source_language(tc.id), "source_ext": source_ext(tc.id),
            "functions": agg["total"] or 0, "matched": agg["matched"] or 0, "bytes": agg["bytes"] or 0,
            "matched_bytes": agg["matched_bytes"] or 0, "attempts": agg["attempts"] or 0, "library": lib,
            "unmatchable": retired,
            "traced": agg["traced"] or 0, "hits": agg["hits"] or 0,
            "data": self.store.one("SELECT COUNT(*) AS n FROM symbols WHERE project_id=? AND kind!='func'",
                                    proj["id"])["n"],
            "context_version": self._context(proj["id"])["version"],
            "folder": proj["root"],
            "export": cfg.get("export"),
            "reconstruction": {"folder": "recovered", "files": agg["reconstructed"] or 0,
                               "recovered": agg["matched"] or 0, "matches": agg["matched"] or 0,
                               "reconstructed": agg["reconstructed"] or 0,
                               "pending": max(0, (agg["total"] or 0) - (agg["matched"] or 0)
                                              - (agg["reconstructed"] or 0))},
            "exportable": cfg.get("platform", "n64") in ("n64", "psx", "ps2", "gba", *X86_PLATFORMS),
            "debug": cfg.get("debug"),
            "binary": cfg.get("rom", {}).get("program") if cfg.get("platform") in X86_PLATFORMS else None,
            # what REA added to this project (an import or Re-check): the memory map, and what it reported
            "segments": cfg.get("segments") or [],
            "rea": cfg.get("rea") or {},
            "room": self._room_info(proj["slug"]),
        }

    def _analysis_overview(self, proj) -> dict:
        cfg = self.cfg(proj)
        target = cfg.get("target") or {}
        agg = self.store.one(
            "SELECT COUNT(*) AS total, SUM(status='matched') AS matched, SUM(size) AS bytes, "
            "SUM(CASE WHEN status='matched' THEN size ELSE 0 END) AS matched_bytes, "
            "SUM(attempts) AS attempts, SUM(abandons) AS releases FROM functions WHERE project_id=?", proj["id"])
        notes = self.store.one("SELECT COUNT(*) AS n FROM notes WHERE project_id=?", proj["id"])["n"]
        fmt = target.get("format") or "target"
        return {
            "slug": proj["slug"], "title": proj["name"], "description": proj["description"],
            "kind": "analysis", "platform": "rea",
            "platform_name": (fmt.upper() if fmt and fmt != "target" else "Binary"),
            "compilers": [],
            "rom": {"title": proj["name"], "game_code": "", "region": "", "size": target.get("size"),
                    "sha1": target.get("sha256"), "byte_order": "", "compiler_guess": fmt},
            "compiler": {"toolchain": "rea", "name": "REA", "flags": [], "installed": True,
                         "proprietary": False, "supported": True, "wsl": False, "why": None},
            "analysis": {"target": target, "counts": cfg.get("counts") or {}, "segments": cfg.get("segments") or [],
                         "provider": cfg.get("provider") or "", "notes": notes,
                         "artifact": cfg.get("artifact") or {}, "children": cfg.get("children") or [],
                         "features": [self._feature_summary(f) for f in cfg.get("features") or []
                                      if isinstance(f, dict)],
                         "features_traceable": target.get("tool") == "analyze_javascript_application",
                         "reconstruction": cfg.get("reconstruction") or self._adopt_binary_tier(proj, cfg)},
            "entries": (cfg.get("artifact") or {}).get("entries"),
            "files": (cfg.get("artifact") or {}).get("files"),
            "source": cfg.get("source") or None,
            "functions": agg["total"] or 0, "matched": agg["matched"] or 0, "bytes": agg["bytes"] or 0,
            "matched_bytes": agg["matched_bytes"] or 0, "attempts": agg["attempts"] or 0,
            "releases": agg["releases"] or 0, "findings": notes,
            "context_version": 1,
            "folder": proj["root"],
            "export": None, "exportable": False,
            "binary": target.get("path"),
            "room": self._room_info(proj["slug"]),
        }

    def _room_info(self, slug: str) -> dict | None:
        if slug in self.links:
            link = self.links[slug]
            now = time.time()
            return {"role": "guest", "host": link.url, "member": link.member, "error": link.error,
                    "members": link.members, "syncing": link.syncing,
                    "connected": link.syncing or now - link.last_ok < 15,
                    # how the sync is going: the page shows the last one and counts down to the next attempt
                    "last_sync": (now - link.last_ok) if link.last_ok else None,
                    "sync_ms": link.sync_ms, "failures": link.failures,
                    "retry_in": max(0.0, link.next_try - now) if link.failures else 0.0,
                    "layout": link.layout}
        if slug in self.rooms:
            return {"role": "host", "members": self.rooms[slug].members()}
        return None

    def import_trace(self, slug: str, path: str, log=print) -> dict:
        """Fold a debugger/emulator coverage file into the project: every hit becomes a function's hit count."""
        proj = self._project(slug)
        trace = tracefile.read(path)
        cfg = self.cfg(proj)
        image_base = int(cfg.get("rom", {}).get("image_base") or 0)
        rows = self.store.all("SELECT id, name, vram, size FROM functions WHERE project_id=? AND vram IS NOT NULL",
                              proj["id"])
        by_name = {r["name"]: r for r in rows}
        by_addr = {int(r["vram"]): r for r in rows}
        starts = sorted(by_addr)
        import bisect
        def find(addr: int):
            if addr in by_addr:
                return by_addr[addr]
            i = bisect.bisect_right(starts, addr) - 1
            if i >= 0:
                r = by_addr[starts[i]]
                if addr < int(r["vram"]) + int(r["size"] or 0):
                    return r
            return None
        hits: dict[int, int] = {}
        unmatched = 0
        for hit in trace.hits:
            row = by_name.get(hit.name) if hit.name else None
            if row is None and hit.address is not None:
                candidates = [hit.address]
                if image_base:
                    if hit.address < image_base:
                        candidates.append(hit.address + image_base)
                    else:
                        candidates.append(hit.address - image_base)
                for addr in candidates:
                    row = find(addr)
                    if row is not None:
                        break
            if row is None:
                unmatched += 1
                continue
            hits[row["id"]] = hits.get(row["id"], 0) + hit.count
        now = time.time()
        if hits:
            with self.store.tx() as db:
                db.executemany("UPDATE functions SET hits=hits+?, trace_at=? WHERE id=?",
                               [(n, now, fid) for fid, n in hits.items()])
                db.execute("INSERT INTO events(project_id,agent_id,kind,detail,created_at) VALUES(?,?,?,?,?)",
                           (proj["id"], None, "trace", f"{sum(hits.values())} hits in {len(hits)} functions from {trace.source}",
                            now))
        top = sorted(((self.store.one("SELECT name, vram, hits FROM functions WHERE id=?", fid), n) for fid, n in hits.items()),
                     key=lambda x: -x[1])[:10]
        log(f"trace {trace.source}: {sum(hits.values())} hits in {len(hits)} functions, {unmatched} unmatched")
        return {"source": trace.source, "hits": sum(hits.values()), "functions": len(hits), "unmatched": unmatched,
                "skipped": trace.skipped,
                "top": [{"name": r["name"], "vram": int(r["vram"]), "hits": n} for r, n in top]}

    def hot_functions(self, slug: str, limit: int = 40) -> list[dict]:
        proj = self._project(slug)
        rows = self.store.all(
            "SELECT name, vram, size, hits, trace_at FROM functions WHERE project_id=? AND hits>0 "
            "ORDER BY hits DESC, name LIMIT ?", proj["id"], max(1, min(int(limit), 500)))
        return [{"name": r["name"], "vram": int(r["vram"] or 0), "size": int(r["size"] or 0),
                 "hits": int(r["hits"] or 0), "trace_at": r["trace_at"]} for r in rows]

    def data_symbols(self, slug: str, query: str = "", limit: int = 500) -> list[dict]:
        """Data and string symbols the target already names, as a readable inventory (not code work items)."""
        proj = self._project(slug)
        rows = self.store.all(
            "SELECT name, address, size, kind, preview FROM symbols WHERE project_id=? AND kind!='func' "
            "ORDER BY address, name", proj["id"])
        q = query.strip().lower()
        out = []
        for i, r in enumerate(rows):
            if q and q not in (r["name"] or "").lower() and q not in (r["preview"] or "").lower():
                continue
            size = int(r["size"] or 0)
            if not size and i + 1 < len(rows):
                size = max(0, int(rows[i + 1]["address"] or 0) - int(r["address"] or 0))
            out.append({"name": r["name"], "address": int(r["address"] or 0), "size": size,
                        "kind": r["kind"] or "data", "preview": r["preview"] or ""})
            if len(out) >= max(1, min(int(limit), 5000)):
                break
        return out

    def function_map(self, slug: str) -> list[dict]:
        proj = self._project(slug)
        rows = self.store.all(
            "SELECT f.name, f.vram, f.size, f.n_insns, f.status, f.meta, f.best_score, f.attempts, f.hits, f.unit, f.matched_at, "
            "a.name AS agent, m.name AS matched_by FROM functions f LEFT JOIN claims c ON c.function_id=f.id "
            "LEFT JOIN agents a ON a.id=c.agent_id LEFT JOIN agents m ON m.id=f.matched_agent_id "
            "WHERE f.project_id=? ORDER BY f.vram, f.unit, f.id", proj["id"])
        cfg = self.cfg(proj)
        known = {row["slug"] for row in self.store.all("SELECT slug FROM projects")}
        children = {str(c.get("entry")): c.get("slug") for c in cfg.get("children") or []
                    if isinstance(c, dict) and c.get("slug") in known}
        link = self.links.get(slug)
        # who works on what in the room: by address, or by name in a git project (its functions have no addresses)
        key = (lambda r: r["name"]) if link is not None and link.fork else (lambda r: r["vram"])
        remote = {key(c): "@" + c["agent"] for c in (link.claims if link is not None else [])}
        out = []
        for r in rows:
            meta = json.loads(r["meta"] or "{}")
            status = "library" if r["status"] == "library" else (
                r["status"] if r["status"] in ("matched", "asm", "unmatchable") else
                "working" if (r["agent"] or key(r) in remote) else "open")
            out.append({"name": r["name"], "vram": r["vram"], "size": r["size"], "insns": r["n_insns"],
                        "unit": r["unit"], "kind": meta.get("kind") or "", "path": meta.get("path") or "",
                        "child": children.get(meta.get("path")) if meta.get("path") else None, "status": status,
                        "library": meta.get("library"),
                        "best": r["best_score"], "attempts": r["attempts"], "hits": int(r["hits"] or 0),
                        "agent": r["agent"] or remote.get(key(r)), "matched_at": r["matched_at"],
                        "matched_by": r["matched_by"]})
        return out

    def map_signature(self, slug: str) -> str:
        """What the function map shows, summed up: it changes when a function is added, matched, tried or claimed.
        A few sums over indexed rows, so the GUI can ask "anything new?" every few seconds without fetching tens of
        thousands of functions (renames and splits come with events, which refetch anyway)."""
        proj = self._project(slug)
        f = self.store.one("SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS hi, COALESCE(SUM(attempts), 0) AS at, "
                           "COALESCE(SUM(status='matched'), 0) AS m, COALESCE(SUM(best_score), 0) AS bs, "
                           "COALESCE(SUM(status='unmatchable'), 0) AS u, COALESCE(MAX(matched_at), 0) AS ma "
                           "FROM functions WHERE project_id=?", proj["id"])
        c = self.store.one("SELECT COUNT(*) AS n, COALESCE(SUM(c.function_id * 31 + c.agent_id), 0) AS k FROM claims c "
                           "JOIN functions f ON f.id=c.function_id WHERE f.project_id=?", proj["id"])
        link = self.links.get(slug)
        remote = sorted(str(x.get("vram") or x.get("name")) + x.get("agent", "") for x in (link.claims if link else []))
        return (f"{f['n']}.{f['hi']}.{f['at']}.{f['m']}.{f['bs']}.{f['u']}.{f['ma']}.{c['n']}.{c['k']}."
                f"{hash(tuple(remote)) & 0xFFFFFF}")

    def function_diff(self, slug: str, name: str, which: str = "latest") -> dict:
        """The target next to an attempt, instruction by instruction, for the GUI: the matched C, else the latest
        (or best) attempt, compiled and linked like the judge does. Cached, so watching an agent costs nothing."""
        if (fork := self.fork_slug(slug)):
            return self.forks.function_diff(fork, name)
        fn = self._function(name, slug)
        proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        latest = self.store.one("SELECT source FROM attempts WHERE function_id=? AND source IS NOT NULL "
                                "ORDER BY id DESC LIMIT 1", fn["id"])
        picks = [("matched", fn["matched_source"]), ("best", fn["best_source"]),
                 ("latest", latest["source"] if latest else None)]
        if which == "latest":
            picks = [picks[0], picks[2], picks[1]]
        kind, source = next(((k, v) for k, v in picks if v), (None, None))
        if source is None:
            return {"kind": None, "rows": None}
        ctx = self._context(proj["id"], fn["unit"])
        key = (fn["id"], hashlib.sha1(source.encode()).hexdigest(), ctx["version"], proj["config"])
        with self._diffs_lock:
            if key in self._diffs:
                return self._diffs[key] | {"kind": kind}
        result = self._compile(proj, ctx["header"], source)
        if not result.ok:
            out = {"rows": None, "error": result.log.strip()[-3000:] or "it didn't compile", "source": source}
        else:
            cand, verdict, problems = self._evaluate(fn, proj, result.obj)
            if cand is None:
                out = {"rows": None, "error": f"the object has no function named `{fn['name']}`", "source": source}
            else:
                d = scoring.diff(self._target_obj(fn, proj), cand)
                if verdict is not None:
                    d = scoring.DiffResult(0 if verdict else max(1, d.score), d.max_score, d.rows, verdict)
                out = {"rows": [list(r) for r in d.rows], "score": d.score,
                       "percent": 100.0 if d.bytes_match else min(99.9, int(d.percent * 10) / 10),
                       "problems": problems, "source": source}
        with self._diffs_lock:
            if len(self._diffs) > 128:
                with contextlib.suppress(RuntimeError, StopIteration):
                    self._diffs.pop(next(iter(self._diffs)), None)
            self._diffs[key] = out
        return out | {"kind": kind}

    def function_detail(self, slug: str, name: str) -> dict:
        if (fork := self.fork_slug(slug)):
            return self.forks.function_detail(fork, name)
        fn = self._function(name, slug)
        proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        if self.is_analysis(proj):
            notes = self.store.all("SELECT n.text, n.created_at, a.name AS agent FROM notes n LEFT JOIN agents a "
                                   "ON a.id=n.agent_id WHERE n.function_id=? ORDER BY n.id DESC LIMIT 10", fn["id"])
            cfg = self.cfg(proj)
            meta = json.loads(fn["meta"] or "{}")
            return {"name": fn["name"], "vram": fn["vram"], "rom_offset": 0, "size": fn["size"],
                    "insns": 0, "status": fn["status"], "best": fn["best_score"], "asm": "",
                    "source": ((fn["best_source"] if meta.get("reconstructed") else None)
                               or fn["matched_source"] or fn["best_source"]),
                    "source_kind": ("reconstructed" if meta.get("reconstructed") else
                                    "analysis" if fn["matched_source"] else None),
                    "scores": [], "notes": [dict(n) for n in notes], "calls": [], "called_by": [],
                    "kind": meta.get("kind") or "procedure", "address": meta.get("address") or 0,
                    "path": meta.get("path") or "", "origin": meta.get("origin") or "",
                    "verified": bool(meta.get("verified")), "reconstructed": bool(meta.get("reconstructed")),
                    "map": meta.get("map") or "",
                    "prefill": self._analysis_prefill_view(cfg, proj, meta),
                    "prefillable": self._analysis_prefillable(cfg, meta)}
        attempts = self.store.all(
            "SELECT t.score, t.created_at, a.name AS agent FROM attempts t LEFT JOIN agents a ON a.id=t.agent_id "
            "WHERE t.function_id=? ORDER BY t.id", fn["id"])
        latest = self.store.one("SELECT source, score FROM attempts WHERE function_id=? AND source IS NOT NULL "
                                "ORDER BY id DESC LIMIT 1", fn["id"])
        notes = self.store.all("SELECT n.text, n.created_at, a.name AS agent FROM notes n LEFT JOIN agents a "
                               "ON a.id=n.agent_id WHERE n.function_id=? ORDER BY n.id DESC LIMIT 10", fn["id"])
        x = self.xrefs(proj)
        syms = self.symbols(proj["id"])
        if not (fn["matched_source"] or fn["best_source"] or latest) and fn["seed"] is None:
            with self._fn_seed_lock(fn["id"]):
                self._seed_for(fn, proj)
            fn = self._function(fn["name"], slug)
        return {
            "name": fn["name"], "vram": fn["vram"], "rom_offset": fn["rom_offset"], "size": fn["size"],
            "insns": fn["n_insns"], "status": fn["status"], "best": fn["best_score"],
            "asm": self._asm_text(fn, proj),
            "source": fn["matched_source"] or fn["best_source"] or (latest["source"] if latest else None) or fn["seed"],
            "source_kind": ("reconstructed" if fn["status"] == "reconstructed" and fn["best_source"] else
                            "matched" if fn["matched_source"] else "best" if fn["best_source"] else
                            "latest" if latest else "draft" if fn["seed"] else None),
            "scores": [{"score": a["score"], "t": a["created_at"], "agent": a["agent"]} for a in attempts],
            "notes": [dict(n) for n in notes],
            "calls": [syms.label(a) for a in sorted(x["calls"].get(fn["vram"], ()))],
            "called_by": [syms.label(a) for a in sorted(x["called_by"].get(fn["vram"], ()))],
        }

    def stats(self, slug: str) -> dict:
        """Who did what and how fast: the overview's numbers (asked every few seconds, not every second)."""
        proj = self._project(slug)
        pid = proj["id"]
        now = time.time()
        if self.is_analysis(proj):
            rows = self.store.all(
                "SELECT a.name AS agent, COUNT(n.id) AS compiles, MAX(n.created_at) AS last FROM notes n "
                "JOIN agents a ON a.id=n.agent_id WHERE n.project_id=? GROUP BY a.id", pid)
            matched = {r["agent"]: r["n"] for r in self.store.all(
                "SELECT a.name AS agent, COUNT(*) AS n FROM functions f JOIN agents a ON a.id=f.matched_agent_id "
                "WHERE f.project_id=? AND f.status='matched' GROUP BY a.id", pid)}
            board = [{"agent": r["agent"], "compiles": r["compiles"], "matched": matched.pop(r["agent"], 0), "last": r["last"]}
                     for r in rows]
            board += [{"agent": a, "compiles": 0, "matched": n, "last": None} for a, n in matched.items()]
            board.sort(key=lambda r: (-r["matched"], -r["compiles"]))
            per_hour = [0] * 24
            for r in self.store.all("SELECT matched_at FROM functions WHERE project_id=? AND status='matched' "
                                    "AND matched_at > ?", pid, now - 86400):
                per_hour[23 - min(23, max(0, int((now - r["matched_at"]) // 3600)))] += 1
            notes = self.store.one("SELECT COUNT(*) AS n FROM notes WHERE project_id=? AND created_at > ?",
                                   pid, now - 3600)["n"]
            return {"agents": board[:24], "matches_per_hour": per_hour, "compiles_last_hour": notes}
        rows = self.store.all(
            "SELECT a.name AS agent, COUNT(t.id) AS compiles, MAX(t.created_at) AS last FROM attempts t "
            "JOIN functions f ON f.id=t.function_id JOIN agents a ON a.id=t.agent_id WHERE f.project_id=? GROUP BY a.id", pid)
        matched = {r["agent"]: r["n"] for r in self.store.all(
            "SELECT a.name AS agent, COUNT(*) AS n FROM functions f JOIN agents a ON a.id=f.matched_agent_id "
            "WHERE f.project_id=? AND f.status='matched' GROUP BY a.id", pid)}
        board = [{"agent": r["agent"], "compiles": r["compiles"], "matched": matched.pop(r["agent"], 0), "last": r["last"]}
                 for r in rows]
        board += [{"agent": a, "compiles": 0, "matched": n, "last": None} for a, n in matched.items()]
        board.sort(key=lambda r: (-r["matched"], -r["compiles"]))
        per_hour = [0] * 24
        for r in self.store.all("SELECT matched_at FROM functions WHERE project_id=? AND status='matched' AND matched_at > ?",
                                pid, now - 86400):
            per_hour[23 - min(23, max(0, int((now - r["matched_at"]) // 3600)))] += 1  # (one matched since `now`: this hour)
        compiles = self.store.one("SELECT COUNT(*) AS n FROM attempts t JOIN functions f ON f.id=t.function_id "
                                  "WHERE f.project_id=? AND t.created_at > ?", pid, now - 3600)["n"]
        return {"agents": board[:24], "matches_per_hour": per_hour, "compiles_last_hour": compiles}

    def live(self, slug: str, since_event: int = 0, since_attempt: int = 0) -> dict:
        proj = self._project(slug)
        now = time.monotonic()
        if now - self._last_expire_time >= 15.0:
            self._last_expire_time = now
            with self.store.tx() as db:
                self._expire_claims(db)
        events = self.store.all(
            "SELECT e.id, e.kind, e.detail, e.created_at, a.name AS agent FROM events e LEFT JOIN agents a "
            "ON a.id=e.agent_id WHERE e.project_id=? AND e.id>? ORDER BY e.id DESC LIMIT 60", proj["id"], since_event)
        attempts = self.store.all(
            "SELECT t.id, t.score, t.compiled, t.created_at, f.name AS function, f.n_insns, a.name AS agent "
            "FROM attempts t JOIN functions f ON f.id=t.function_id LEFT JOIN agents a ON a.id=t.agent_id "
            "WHERE f.project_id=? AND t.id>? ORDER BY t.id DESC LIMIT 80", proj["id"], since_attempt)
        claims = self.store.all(
            "SELECT f.name, f.n_insns, f.best_score, f.attempts, a.name AS agent, c.claimed_at FROM claims c "
            "JOIN functions f ON f.id=c.function_id JOIN agents a ON a.id=c.agent_id WHERE f.project_id=?", proj["id"])
        claims = [dict(c) for c in claims]
        if slug in self.links:
            for c in self.links[slug].claims:
                row = self.store.one("SELECT name, n_insns, best_score, attempts FROM functions WHERE project_id=? AND vram=?",
                                     proj["id"], c["vram"])
                if row:
                    claims.append(dict(row) | {"agent": "@" + c["agent"], "claimed_at": None})
        return {"events": [dict(e) for e in reversed(events)], "attempts": [dict(t) for t in reversed(attempts)],
                "claims": claims, "overview": self.overview(slug)}

    def preview_compiler(self, slug: str, toolchain: str, flags: list[str] | None = None) -> dict:
        """What a different compiler would do to the functions already matched: how many still compile to the
        same bytes, without changing the project. Switching compilers can be the right move (MSVC code vs GCC),
        and this is the evidence for it, rather than finding out after the old matches stop matching."""
        proj = self._project(slug)
        if self.fork_slug(slug):
            raise NetworkError("a fork builds with the project's own compilers and flags")
        if self.is_analysis(proj):
            raise NetworkError("an analysis project doesn't compile")
        cfg = self.cfg(proj)
        toolchain = arch_toolchain(toolchain, cfg.get("arch") or "")
        try:
            tc = toolchains.get(toolchain)
        except toolchains.ToolchainError as e:
            raise NetworkError(str(e)) from None
        cand = {"toolchain": tc.id, "flags": list(flags or tc.flags), "timeout": 20}
        preview_cfg = json.loads(proj["config"])
        preview_cfg["compiler"] = cand
        fake = dict(proj)
        fake["config"] = json.dumps(preview_cfg)
        rows = self.store.all("SELECT * FROM functions WHERE project_id=? AND status='matched' "
                              "AND matched_source IS NOT NULL ORDER BY n_insns", proj["id"])
        installed = toolchains.installed(tc.id) is not None
        if rows and not installed:
            return {"toolchain": tc.id, "name": SHORT_NAMES.get(tc.id, tc.name), "flags": cand["flags"],
                    "installed": False, "total": len(rows), "checked": 0, "kept": 0, "broke": 0, "failed": 0,
                    "sampled": False, "broke_names": [], "failed_names": []}
        step = max(1, len(rows) // 300)
        sample = rows[::step]
        kept, broke, failed = [], [], []
        for fn in sample:
            ctx = self._context(proj["id"], fn["unit"])
            result = self._compile(fake, ctx["header"], fn["matched_source"])
            if not result.ok:
                failed.append(fn["name"])
                continue
            candidate, verdict, _ = self._evaluate(fn, fake, result.obj)
            if candidate is None:
                failed.append(fn["name"])
                continue
            d = scoring.diff(self._target_obj(fn, fake), candidate)
            if verdict is not None:
                d = scoring.DiffResult(0 if verdict else max(1, d.score), d.max_score, d.rows, verdict)
            (kept if d.bytes_match else broke).append(fn["name"])
        return {"toolchain": tc.id, "name": SHORT_NAMES.get(tc.id, tc.name), "flags": cand["flags"],
                "installed": installed, "total": len(rows), "checked": len(sample),
                "kept": len(kept), "broke": len(broke), "failed": len(failed), "sampled": step > 1,
                "broke_names": broke[:8], "failed_names": failed[:5]}

    def set_compiler(self, slug: str, toolchain: str, flags: list[str] | None = None) -> None:
        proj = self._project(slug)
        if self.is_analysis(proj):
            raise NetworkError("an analysis project doesn't compile: its agents read the target with REA")
        if self.fork_slug(slug):
            raise NetworkError("a fork builds with the project's own compilers and flags")
        cfg = self.cfg(proj)
        toolchain = arch_toolchain(toolchain, cfg.get("arch") or "")
        tc = toolchains.get(toolchain)
        if not flags and cfg.get("platform") == "linux":
            flags = _linux_flags(tc.flags, cfg.get("rom", {}).get("pie", False))
        cfg["compiler"] = {"toolchain": tc.id, "flags": flags or tc.flags, "timeout": 20}
        with self.store.tx() as db:
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))
            db.execute("UPDATE functions SET seed=NULL WHERE project_id=? AND status='open'", (proj["id"],))
            self._event(db, proj["id"], None, "compiler", f"{tc.name} {' '.join(cfg['compiler']['flags'])}")
