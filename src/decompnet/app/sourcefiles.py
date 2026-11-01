"""The code a project has: recovered, reconstructed, and how much is still missing.

Recovered code is what the shipped artifact itself carries (a source map's sourcesContent, a file inside an archive);
reconstructed code is what an agent wrote, and it is never counted as proof. write_source is the only way in, and it
labels what it saves as unverified.
"""

# pyright: reportAttributeAccessIssue=false
# Each part is read on its own, so every self.<name> that lives in another part (or in Network) looks unknown to a
# type checker. RomNetwork is one class at runtime, and tests/test_engine_parts.py proves every name resolves there.


from __future__ import annotations

import json
import posixpath
import time
import zipfile
from pathlib import Path

from ..network import Agent, NetworkError
from . import analysis, sources
from .platform import (
    source_ext,
)


class SourceFilesMixin:
    def _source_rows(self, proj) -> list[tuple]:
        """Rows that carry code: a reconstruction project's recovered/reconstructed files, and any work item an
        agent wrote code for with write_source (a matching project reconstructs one function at a time)."""
        rows = []
        for row in self.store.all("SELECT * FROM functions WHERE project_id=? ORDER BY name", proj["id"]):
            meta = json.loads(row["meta"] or "{}")
            if meta.get("kind") in ("recovered-source", "reconstruction") or meta.get("reconstructed"):
                rows.append((row, meta))
        return rows

    @staticmethod
    def _source_state(row, meta, *, analysis: bool) -> str:
        """verified, reconstructed or pending: what this row's code is worth in this kind of project.

        In a reconstruction project a source map is the proof; in a matching project the proof is byte-exact
        equality with its own copy of the target."""
        if analysis:
            if meta.get("verified"):
                return "verified"
            return "reconstructed" if row["status"] == "matched" else "pending"
        if row["status"] == "matched":
            return "verified"
        return "reconstructed" if meta.get("reconstructed") else "pending"

    @staticmethod
    def _reconstruction_tier(cfg) -> str:
        """Where an analysis project's code comes from: source maps, an archive, or REA's analysis of a binary."""
        target = cfg.get("target") or {}
        if target.get("tool") == "analyze_javascript_application":
            return "source-map"
        if cfg.get("artifact"):
            return "archive"
        return "binary"

    def _analysis_source_files(self, proj) -> tuple[list[dict], dict]:
        """An analysis project's source rows, each with the tier its code is worth (verified/reconstructed/pending)."""
        files: list[dict] = []
        counts = {"recovered": 0, "reconstructed": 0, "pending": 0}
        for row, meta in self._source_rows(proj):
            state = self._source_state(row, meta, analysis=True)
            counts["recovered" if state == "verified" else state] += 1
            files.append({"path": str(meta.get("path") or row["name"]), "name": row["name"],
                          "status": row["status"], "size": row["size"],
                          "verified": bool(meta.get("verified")), "state": state,
                          "origin": str(meta.get("origin") or ""), "map": str(meta.get("map") or ""),
                          "agent": str(meta.get("agent") or ""),
                          "module_kind": str(meta.get("module_kind") or "")})
        return files, counts

    def _reconstruction_state(self, proj) -> dict:
        """The project config's reconstruction summary, tier-aware: a binary has no byte-exact tier, so its
        pending count is the work items still without recovered code."""
        cfg = self.cfg(proj)
        tier = self._reconstruction_tier(cfg)
        files, counts = self._analysis_source_files(proj)
        total, pending = len(files), counts["pending"]
        if tier == "binary":
            total = self.store.one("SELECT COUNT(*) AS n FROM functions WHERE project_id=?", proj["id"])["n"]
            pending = max(0, total - counts["reconstructed"])
        return {"folder": "recovered", "tier": tier, "files": len(files), "recovered": counts["recovered"],
                "reconstructed": counts["reconstructed"], "pending": pending, "total": total}

    def _adopt_binary_tier(self, proj, cfg) -> dict | None:
        """A binary imported before binaries had a Sources tier has no summary in its config: work it out once,
        keep it, and the page shows the tier from then on."""
        if cfg.get("reconstruction") or self._reconstruction_tier(cfg) != "binary":
            return None
        if proj["id"] in self._binary_tier_seen:
            return None
        self._binary_tier_seen.add(proj["id"])
        state = self._reconstruction_state(proj)
        with self.store.tx() as db:
            row = db.execute("SELECT config FROM projects WHERE id=?", (proj["id"],)).fetchone()
            fresh = json.loads(row["config"]) if row is not None else cfg
            if not fresh.get("reconstruction"):
                fresh["reconstruction"] = state
                db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(fresh), proj["id"]))
        return state

    def sources(self, slug: str) -> dict:
        """The code a project has: what is verified, what agents reconstructed, and what is still without code."""
        proj = self._project(slug)
        if not self.is_analysis(proj):
            return self._rom_sources(proj)
        cfg = self.cfg(proj)
        tier = self._reconstruction_tier(cfg)
        files, counts = self._analysis_source_files(proj)
        rec = cfg.get("reconstruction") or {}
        counts.update({"files": len(files), "total": len(files), "maps": rec.get("maps") or 0,
                       "truncated": bool(rec.get("truncated"))})
        if tier == "binary":  # nothing is byte-exact here: a work item either has reconstructed code or doesn't
            total = self.store.one("SELECT COUNT(*) AS n FROM functions WHERE project_id=?", proj["id"])["n"]
            counts.update({"total": total, "pending": max(0, total - counts["reconstructed"])})
        return {"slug": slug, "kind": "analysis", "tier": tier,
                "folder": str(Path(proj["root"]) / "recovered"), "files": files, "counts": counts}

    def _rom_sources(self, proj) -> dict:
        """A matching project's code: functions matched byte for byte, and the ones agents reconstructed."""
        counts = self._rom_source_counts(proj["id"])
        files = []
        for row, meta in self._source_rows(proj):
            if row["status"] == "matched" or not meta.get("reconstructed"):
                continue  # verified code lives in the project's own source tree, not in recovered/
            files.append({"path": str(meta.get("path") or row["name"]), "name": row["name"],
                          "status": row["status"], "size": row["size"], "verified": False,
                          "state": "reconstructed", "origin": str(meta.get("origin") or ""), "map": "",
                          "agent": str(meta.get("agent") or ""),
                          "module_kind": str(meta.get("module_kind") or "")})
        return {"slug": proj["slug"], "kind": "rom", "tier": "match",
                "folder": str(Path(proj["root"]) / "recovered"), "files": files,
                "counts": {**counts, "files": len(files), "reconstructed": len(files),
                           "maps": 0, "truncated": False}}

    def source_file(self, slug: str, path: str) -> dict:
        """One recovered or reconstructed source file, as it stands."""
        proj = self._project(slug)
        analysis = self.is_analysis(proj)
        wanted = sources.clean_path(path)
        for row, meta in self._source_rows(proj):
            if row["name"] == path or sources.clean_path(str(meta.get("path") or "")) == wanted:
                code = (row["best_source"] if meta.get("reconstructed") else None) or \
                    row["matched_source"] or row["best_source"] or ""
                return {"path": str(meta.get("path") or row["name"]), "name": row["name"],
                        "status": row["status"], "source": code,
                        "verified": bool(meta.get("verified")) or (not analysis and row["status"] == "matched"),
                        "state": self._source_state(row, meta, analysis=analysis),
                        "origin": str(meta.get("origin") or ""), "map": str(meta.get("map") or ""),
                        "agent": str(meta.get("agent") or ""), "size": row["size"]}
        raise NetworkError(f"no source file named {path!r} in {slug}")

    def write_source(self, agent: Agent, project: str | None, function: str, source: str) -> str:
        """Save code an agent reconstructed: written into recovered/, labelled unverified, never counted as a match.

        A reconstruction project's item is a whole module. In a matching project it is one function: the code is
        kept as an unverified reconstruction, and a later byte-exact compile_and_diff match replaces it."""
        fn = self._function(function, project)
        proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        if self.fork_slug(proj["slug"]):
            raise NetworkError("this project has its own source tree: write the function into its files and run "
                               "compile_and_diff, so the result is verified")
        reconstruction_project = self.is_analysis(proj)
        meta = json.loads(fn["meta"] or "{}")
        if reconstruction_project and meta.get("kind") not in (
                "recovered-source", "reconstruction", "procedure", "task", "feature-node"):
            raise NetworkError(f"{fn['name']} is not a work item this project can reconstruct")
        if not reconstruction_project and fn["status"] == "matched":
            raise NetworkError(f"{fn['name']} already matches the original byte for byte: there is nothing to reconstruct")
        if reconstruction_project:
            path = sources.clean_path(str(meta.get("path") or ""))
            if path and meta.get("entry_type"):  # a compiled archive entry: the source file sits beside it
                stem, ext = posixpath.splitext(path)
                recovered_ext = analysis.COMPILED_SOURCE_EXTS.get(ext.lower())
                if recovered_ext:
                    path = sources.clean_path(stem + recovered_ext)
            if not path:  # a procedure or task of an analyzed binary: one file per work item
                folder = {"task": "tasks", "feature-node": "features"}.get(meta.get("kind"), "functions")
                ext = analysis.recovered_ext(self.cfg(proj).get("target") or {})
                path = sources.clean_path(posixpath.join(folder, f"{fn['name']}{ext}"))
            meta["path"] = path
        else:
            path = sources.clean_path(str(meta.get("path") or ""))
            if not path:
                cfg = self.cfg(proj)
                unit = sources.clean_path(str(fn["unit"] or "")) or "unmatched"
                ext = source_ext(str((cfg.get("compiler") or {}).get("toolchain") or ""))
                path = sources.clean_path(posixpath.join(unit, f"{fn['name']}{ext}"))
        if not path:
            raise NetworkError(f"{fn['name']} has no safe path in the recovered tree")
        source = source.strip("\n") + "\n"
        if not source.strip():
            raise NetworkError("write the code first")
        if len(source.encode("utf-8")) > sources.MAX_SOURCE_BYTES:
            raise NetworkError(f"that source is larger than {sources.MAX_SOURCE_BYTES // (1024 * 1024)} MiB")
        claim = self.store.one("SELECT agent_id FROM claims WHERE function_id=?", fn["id"])
        if claim is None or claim["agent_id"] != agent.id:
            raise NetworkError(f"you don't hold a claim on {fn['name']}")
        folder = Path(proj["root"]) / "recovered"
        out = folder / path
        if folder.resolve() not in out.resolve().parents:
            raise NetworkError("that path would leave the recovered folder")
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + ".part")
        tmp.write_text(source, encoding="utf-8")
        tmp.replace(out)
        now = time.time()
        module = reconstruction_project and meta.get("kind") in ("recovered-source", "reconstruction")
        with self.store.tx() as db:
            meta.update({"origin": "agent", "verified": False, "agent": agent.name})
            if module:  # a whole module: the code is the item's result (and its finding)
                db.execute("UPDATE functions SET status='matched', matched_source=?, best_source=?, best_score=0, "
                           "matched_agent_id=?, matched_at=?, attempts=attempts+1, meta=? WHERE id=?",
                           (source, source, agent.id, now, json.dumps(meta), fn["id"]))
            elif reconstruction_project:  # a procedure or task of an analyzed binary: keep any finding it already has
                meta["reconstructed"] = True
                db.execute("UPDATE functions SET status='matched', best_source=?, best_score=0, "
                           "matched_agent_id=COALESCE(matched_agent_id, ?), matched_at=COALESCE(matched_at, ?), "
                           "attempts=attempts+1, meta=? WHERE id=?",
                           (source, agent.id, now, json.dumps(meta), fn["id"]))
            else:
                meta.update({"path": path, "reconstructed": True})
                db.execute("UPDATE functions SET status='reconstructed', best_source=?, meta=?, attempts=attempts+1 "
                           "WHERE id=?", (source, json.dumps(meta), fn["id"]))
            db.execute("DELETE FROM claims WHERE function_id=?", (fn["id"],))
            cfg = json.loads(db.execute("SELECT config FROM projects WHERE id=?", (proj["id"],)).fetchone()["config"])
            if reconstruction_project:
                rec = cfg.setdefault("reconstruction", {"folder": "recovered", "maps": 0, "truncated": False})
                rec.update(self._reconstruction_state(proj))
            else:
                counts = self._rom_source_counts(proj["id"])
                cfg["reconstruction"] = {"folder": "recovered", **counts}
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))
            self._event(db, proj["id"], agent.id, "reconstruct", f"{path} ({len(source)} bytes, unverified)")
        if reconstruction_project:
            return (f"✅ {path} is saved in recovered/ and the work item is marked analyzed. It is reconstructed, not "
                    "verified against the original: cite what you based it on in a note, and say so in any summary. "
                    "Claim the next item, or export the tree.")
        return (f"✅ {path} is saved in recovered/. It is reconstructed, not verified: compile_and_diff is the test, "
                "and a byte-exact match replaces it. Say so wherever you report on it.")

    def _rom_source_counts(self, project_id: int) -> dict:
        """A matching project's code, counted the way its Sources panel shows it."""
        agg = self.store.one(
            "SELECT COUNT(*) AS total, COALESCE(SUM(status='matched'), 0) AS matched, "
            "COALESCE(SUM(status='reconstructed'), 0) AS reconstructed FROM functions "
            "WHERE project_id=? AND status NOT IN ('library', 'unmatchable')", project_id)
        total, matched = agg["total"] or 0, agg["matched"] or 0
        return {"files": agg["reconstructed"], "recovered": matched, "reconstructed": agg["reconstructed"],
                "pending": max(0, total - matched - agg["reconstructed"]), "total": total, "matches": matched}

    def export_sources(self, slug: str, log=print) -> dict:
        """Write every reconstructed file into recovered/ (and a zip of it) with an explicit verification report:
        what is verified, what is reconstructed, and what is still without code."""
        proj = self._project(slug)
        if self.fork_slug(slug):
            raise NetworkError("this project has its own source tree: its code is committed there, not recovered "
                               "into recovered/")
        analysis = self.is_analysis(proj)
        rows = [(row, meta) for row, meta in self._source_rows(proj)]
        if not analysis:
            rows = [(row, meta) for row, meta in rows if row["status"] != "matched" and meta.get("reconstructed")]
        if not rows:
            raise NetworkError("this project has no recovered or reconstructed code yet: claim a work item and save "
                               "its source with write_source first")
        folder = Path(proj["root"]) / "recovered"
        folder.mkdir(parents=True, exist_ok=True)
        total = 0
        entries: list[tuple[str, str]] = []
        for row, meta in rows:
            code = row["matched_source"] or row["best_source"]
            if not code:
                continue
            path = sources.clean_path(str(meta.get("path") or row["name"]))
            if not path:
                continue
            total += len(code.encode("utf-8"))
            if total > 256 * 1024 * 1024:
                raise NetworkError("the recovered tree is larger than 256 MiB; export it in parts")
            out = folder / path
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(code, encoding="utf-8")
            entries.append((path, code))
        tier = self._reconstruction_tier(self.cfg(proj))
        if analysis:
            verified = sum(1 for _row, meta in rows if meta.get("verified"))
            unverified = sum(1 for row, meta in rows if row["status"] == "matched" and not meta.get("verified"))
            pending = sum(1 for row, _meta in rows if row["status"] != "matched")
            if tier == "binary":
                total = self.store.one("SELECT COUNT(*) AS n FROM functions WHERE project_id=?", proj["id"])["n"]
                pending = max(0, total - unverified)  # every work item without reconstructed code
                report = [f"# Recovered sources: {proj['name']}", "",
                          "- verified: none (this target was analyzed, not matched: reconstructed code is unverified)",
                          f"- reconstructed by agents (unverified): {unverified}",
                          f"- still without code: {pending}", ""]
            else:
                archive = any(meta.get("origin") == "archive" for _row, meta in rows)
                report = [f"# Recovered sources: {proj['name']}", "",
                          f"- verified ({'extracted from the archive, byte for byte' if archive else 'from source maps'}): "
                          f"{verified}",
                          f"- reconstructed by agents (unverified): {unverified}",
                          f"- still to reconstruct: {pending}", ""]
        else:
            counts = self._rom_source_counts(proj["id"])
            verified, unverified, pending = counts["recovered"], len(entries), counts["pending"]
            report = [f"# Recovered sources: {proj['name']}", "",
                      f"- verified (matched byte for byte, in the project's own source tree): {verified}",
                      f"- reconstructed by agents (unverified, in this tree): {unverified}",
                      f"- still without code: {pending}", ""]
        for row, meta in rows:
            path = sources.clean_path(str(meta.get("path") or row["name"]))
            if not path:
                continue
            if analysis:
                if meta.get("verified"):
                    state = ("extracted from the archive, byte for byte" if meta.get("origin") == "archive"
                             else "recovered from a source map")
                else:
                    state = ("reconstructed by an agent (unverified)" if row["status"] == "matched"
                             else "still to reconstruct")
            else:
                state = "reconstructed by an agent (unverified)"
            who = f" by {meta['agent']}" if meta.get("agent") else ""
            report.append(f"- `{path}` — {state}{who}")
        if analysis and tier == "binary":
            # A binary has no source map to be complete against: name the work items still without code.
            have = {row["id"] for row, _meta in rows}
            missing = [(row, json.loads(row["meta"] or "{}")) for row in self.store.all(
                "SELECT id, name, meta FROM functions WHERE project_id=? ORDER BY name LIMIT 500", proj["id"])
                if row["id"] not in have][:200]
            if missing:
                report += ["", "## Still without code (first 200)", ""]
                report += [f"- `{row['name']}`" + (f" at 0x{int(meta['address']):X}" if meta.get("address") else "")
                           for row, meta in missing]
        if not analysis:
            missing = self.store.all("SELECT name, vram FROM functions WHERE project_id=? AND status='open' "
                                     "ORDER BY vram, name LIMIT 200", proj["id"])
            if missing:
                report += ["", "## Still without code (first 200)", ""]
                report += [f"- `{m['name']}`" + (f" at 0x{m['vram']:08X}" if m["vram"] else "") for m in missing]
        report_md = "\n".join(report) + "\n"
        (folder / "RECONSTRUCTION.md").write_text(report_md, encoding="utf-8")
        zip_path = Path(proj["root"]) / f"{proj['slug']}-sources.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for path, code in entries:
                zf.writestr(path, code)
            zf.writestr("RECONSTRUCTION.md", report_md)
        log(f"{len(entries)} source files in {folder} and {zip_path}")
        return {"files": len(entries), "verified": verified, "unverified": unverified, "pending": pending,
                "folder": str(folder), "zip": str(zip_path)}
