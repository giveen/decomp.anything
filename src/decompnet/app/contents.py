# pyright: reportAttributeAccessIssue=false
# Each part is read on its own, so every self.<name> that lives in another part (or in Network) looks unknown to a
# type checker. RomNetwork is one class at runtime, and tests/test_engine_parts.py proves every name resolves there.
"""Reading an artifact: what it is made of, and what can be asked about one piece of it.

analysis_summary is the survey an analysis project starts from (entry points, imports, strings, subsystems) and
analysis_contents walks an archive or the binary itself. The rest pass a question through to REA and report what it
answered, with what it would not say. Nothing here writes to the target.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
import time
from pathlib import Path

from .. import rea
from ..network import Agent, NetworkError, _ago
from . import analysis
from .platform import (
    X86_PLATFORMS,
)


class ContentsMixin:
    def analysis_tool(self, slug: str, tool: str, arguments: dict | None = None) -> str:
        """Run one read-only REA tool on an analysis project's target."""
        _, cfg = self.analysis_cfg(slug)
        if tool not in rea.ANALYSIS_ALLOWED:
            raise NetworkError(f"REA tool {tool!r} isn't available to agents; allowed: "
                               f"{', '.join(sorted(rea.ANALYSIS_ALLOWED))}")
        try:
            client = rea.analysis_client(cfg["target"], cfg.get("provider") or "")
            record = client.call(tool, arguments or {}, timeout=1800, any_tool=True, envelope=True)
        except rea.ReaError as e:
            raise NetworkError(str(e)) from e
        return analysis.pretty({"evidence": rea.evidence_metadata(record), "result": rea.unwrap(record)})

    def agent_rea_tool(self, slug: str, tool: str, arguments: dict | None = None) -> str:
        proj = self.project_row(slug)
        if self.is_analysis(proj):
            return self.analysis_tool(slug, tool, arguments)
        cfg = self.cfg(proj)
        if cfg.get("platform") not in X86_PLATFORMS or self.fork_slug(slug):
            raise NetworkError("direct REA tools need an analysis or Windows/Linux matching project")
        if tool not in rea.NATIVE_ALLOWED:
            raise NetworkError(f"REA tool {tool!r} isn't available to agents")
        target = {"path": str(Path(proj["root"]) / cfg["rom"]["program"])}
        try:
            client = rea.agent_client(target, native=True, connect=True)
            if client is None:  # REA isn't set up for this program (see the RE console's settings)
                raise NetworkError("REA isn't set up for this program: check the RE console's settings, then try again")
            record = client.call(tool, arguments or {}, timeout=1800, any_tool=True, envelope=True)
        except rea.ReaError as e:
            raise NetworkError(str(e)) from e
        return analysis.pretty({"evidence": rea.evidence_metadata(record), "result": rea.unwrap(record)})

    def discover_assistance(self, slug: str, query: str = "", function: str = "", connect: bool = False,
                            refresh: bool = False) -> str:
        from . import assistance

        return json.dumps(assistance.discover(self, slug, query, function, connect, refresh), ensure_ascii=False)

    def read_skill(self, slug: str, skill: str) -> str:
        from . import assistance

        return assistance.read_skill(self, slug, skill)

    def record_assistance(self, agent: Agent, slug: str, function: str, summary: str, fallback_reason: str = "") -> str:
        from . import assistance

        return assistance.record(self, agent, slug, function, summary, fallback_reason)

    def analysis_search(self, slug: str, query: str) -> str:
        _, cfg = self.analysis_cfg(slug)
        try:
            client = rea.analysis_client(cfg["target"], cfg.get("provider") or "")
        except rea.ReaError as e:
            raise NetworkError(str(e)) from e
        rows: list[str] = []
        try:
            found = client.call("search_strings", {"pattern": query}, timeout=900)
            if isinstance(found, list):
                rows += [f"- string {analysis.summarize([r])}" for r in found[:40]]
        except rea.ReaError:
            pass
        try:
            found = client.call("search_procedures", {"pattern": query}, timeout=900)
            if isinstance(found, list):
                rows += [f"- procedure {analysis.summarize([r])}" for r in found[:40]]
        except rea.ReaError:
            pass
        return "\n".join(rows) or f"REA found no string or procedure matching {query!r}."

    def analysis_read_entry(self, slug: str, entry: str, offset: int = 0, length: int = 256) -> str:
        """Read one file inside an archive project, without extracting it to disk."""
        _, cfg = self.analysis_cfg(slug)
        if not cfg.get("artifact"):
            raise NetworkError("this analysis project isn't an archive")
        data = analysis.read_archive_entry((cfg.get("target") or {}).get("path") or "", entry,
                                           max(0, int(offset)), max(1, min(int(length), 8192)))
        if data is None:
            raise NetworkError(f"no entry {entry!r} in this archive (or it couldn't be read)")
        return f"{entry} + 0x{max(0, int(offset)):X} ({len(data)} bytes)\n" + analysis.preview_bytes(data, len(data))

    def analysis_summary(self, slug: str) -> dict:
        """Write SUMMARY.md: the target, every analyzed work item's finding, and what is still open."""
        proj, cfg = self.analysis_cfg(slug)
        target = cfg.get("target") or {}
        artifact = cfg.get("artifact") or {}
        rows = self.store.all(
            "SELECT f.name, f.size, f.status, f.meta, f.matched_source, f.matched_at, a.name AS agent "
            "FROM functions f LEFT JOIN agents a ON a.id=f.matched_agent_id "
            "WHERE f.project_id=? ORDER BY f.status='matched' DESC, f.unit, f.name", proj["id"])
        analyzed = [r for r in rows if r["status"] == "matched"]
        pending = [r for r in rows if r["status"] != "matched"]
        notes = self.store.all(
            "SELECT n.text, n.created_at, a.name AS agent FROM notes n LEFT JOIN agents a ON a.id=n.agent_id "
            "WHERE n.project_id=? AND n.function_id IS NULL ORDER BY n.id DESC", proj["id"])
        now = time.time()
        findings = f"{len(analyzed)} finding" + ("" if len(analyzed) == 1 else "s")
        summary = (f"{len(analyzed)}/{len(rows)} work items analyzed · {findings}"
                   + (f" · {artifact.get('entries')} archive entries" if artifact.get("entries") else ""))
        lines = [f"# Analysis summary: {proj['name']}", "",
                 f"Generated {time.strftime('%Y-%m-%d %H:%M:%S')} for project `{proj['slug']}`.", "",
                 "## Target",
                 f"- file: `{target.get('path') or ''}`",
                 f"- format: {target.get('format') or 'file'} · kind: {target.get('kind') or 'target'}",
                 f"- provider: {cfg.get('provider') or 'auto'}",
                 f"- progress: {summary}"]
        if artifact.get("entries"):
            dirs, files = artifact.get("directories") or 0, artifact.get("files") or 0
            lines.append(f"- archive: {artifact.get('entries')} entries · {files} file{'' if files == 1 else 's'} · "
                         f"{dirs} director{'y' if dirs == 1 else 'ies'}")
        source_rows = [(row, json.loads(row["meta"] or "{}")) for row in rows
                       if json.loads(row["meta"] or "{}").get("kind") in ("recovered-source", "reconstruction")
                       or json.loads(row["meta"] or "{}").get("reconstructed")]
        if source_rows:
            tier = self._reconstruction_tier(cfg)
            verified = sum(1 for _row, meta in source_rows if meta.get("verified"))
            reconstructed = sum(1 for row, meta in source_rows if row["status"] == "matched" and not meta.get("verified"))
            to_go = sum(1 for row, _meta in source_rows if row["status"] != "matched")
            lines += ["", "## Sources", f"- folder: `{Path(proj['root']) / 'recovered'}`"]
            if tier == "binary":
                lines += [f"- {reconstructed} reconstructed by agents (unverified): this target was analyzed, not "
                          "matched, so nothing here is byte-exact",
                          f"- {max(0, len(rows) - reconstructed)} work items without recovered code"]
            else:
                lines += [f"- {verified} "
                          f"{'extracted from the archive' if tier == 'archive' else 'recovered from source maps'} · "
                          f"{reconstructed} reconstructed by agents (unverified) · {to_go} still to reconstruct"]
            for row, meta in source_rows:
                path = meta.get("path") or row["name"]
                if meta.get("verified"):
                    state = ("extracted from the archive, byte for byte" if meta.get("origin") == "archive"
                             else "recovered from its source map")
                else:
                    state = ("reconstructed by an agent (unverified)" if row["status"] == "matched"
                             else "still to reconstruct")
                lines.append(f"- `{path}` — {state}")
            lines.append("")
        features = [f for f in cfg.get("features") or [] if isinstance(f, dict)]
        if features:
            by_name = {row["name"]: row for row in rows}
            lines += ["", "## Features"]
            for feature in features:
                coverage = feature.get("coverage") or {}
                traced = feature.get("summary") or {}
                lines += [f"### {feature.get('name')}",
                          f"- seed: {analysis.feature_seed_text(feature.get('seed'))} "
                          f"({feature.get('direction') or 'both'})",
                          f"- coverage: {coverage.get('status') or 'unknown'} · "
                          f"{traced.get('traced_nodes', len(feature.get('nodes') or []))} nodes · "
                          f"{traced.get('terminal_paths', feature.get('paths') or 0)} paths"]
                if feature.get("truncated"):
                    lines.append("- the trace was larger than this project stores; the work items below are the first "
                                 "ones reached")
                seen_items: set[str] = set()  # several graph roles share one work item
                for node in feature.get("nodes") or []:
                    item = str(node.get("work_item") or "")
                    if not item or item in seen_items:
                        continue
                    seen_items.add(item)
                    row = by_name.get(item)
                    if row is None:
                        continue
                    label = node.get("path") or node.get("label") or node.get("name")
                    if row["status"] == "matched":
                        finding = " ".join((row["matched_source"] or "").split())
                        lines.append(f"- `{label}` ({node.get('kind')}): {finding[:400]}"
                                     + ("…" if len(finding) > 400 else ""))
                    else:
                        lines.append(f"- `{label}` ({node.get('kind')}): {row['status']}")
                for limit in (feature.get("limitations") or [])[:6]:
                    lines.append(f"- limit: {limit}")
                lines.append("")
        lines += ["", "## Findings"]
        if not analyzed:
            lines += ["Nothing has been analyzed yet.", ""]
        for row in analyzed:
            meta = json.loads(row["meta"] or "{}")
            path = meta.get("path") or row["name"]
            if meta.get("kind") in ("recovered-source", "reconstruction"):
                state = "recovered from its source map (verified)" if meta.get("verified") else \
                    "reconstructed by an agent (unverified)"
                lines += [f"### `{path}`", f"- {row['size']} bytes · {state}", ""]
                continue
            lines += [f"### `{path}`"]
            facts = [f"{row['size']} bytes"]
            if meta.get("artifact_format"):
                facts.append(str(meta["artifact_format"]))
            if meta.get("sha256"):
                facts.append(f"sha256 {meta['sha256']}")
            lines.append("- " + " · ".join(facts))
            when = _ago(row["matched_at"]) if row["matched_at"] else "earlier"
            lines.append(f"- analyzed by {row['agent'] or 'an agent'} {when}")
            body = (row["matched_source"] or "").strip()
            if not body and meta.get("reconstructed"):
                body = f"reconstructed code: `{meta.get('path')}` (unverified)"
            lines += ["", body or "(no finding text)", ""]
        if pending:
            lines += ["## Still open"]
            for row in pending:
                meta = json.loads(row["meta"] or "{}")
                label = meta.get("path") or row["name"]
                lines.append(f"- `{label}` — {row['status']}")
            lines.append("")
        if notes:
            lines += ["## Project notes"]
            for note in notes:
                lines.append(f"- {note['text'].strip()}" + (f" — {note['agent']}" if note["agent"] else ""))
            lines.append("")
        children = cfg.get("children") or []
        if children:
            lines += ["## Nested targets"]
            for child in children:
                if not isinstance(child, dict) or not child.get("slug"):
                    continue
                try:
                    cproj = self._project(child["slug"])
                except NetworkError:
                    lines.append(f"- `{child.get('entry')}` → `{child['slug']}` (missing)")
                    continue
                cagg = self.store.one(
                    "SELECT COUNT(*) AS total, SUM(status='matched') AS matched FROM functions WHERE project_id=?",
                    cproj["id"])
                cpath = Path(cproj["root"]) / "SUMMARY.md"
                lines += [f"### `{child.get('entry')}` → `{child['slug']}`",
                          f"- target: {cproj['name']} · {cagg['matched'] or 0}/{cagg['total'] or 0} analyzed",
                          f"- summary: {cpath}"]
                for crow in self.store.all(
                        "SELECT name, meta, matched_source FROM functions WHERE project_id=? AND status='matched' "
                        "ORDER BY name", cproj["id"]):
                    cmeta = json.loads(crow["meta"] or "{}")
                    label = cmeta.get("path") or crow["name"]
                    finding = " ".join((crow["matched_source"] or "").split())
                    lines.append(f"- `{label}`: {finding[:300]}" + ("…" if len(finding) > 300 else ""))
                lines.append("")
        while lines and lines[-1] == "":
            lines.pop()
        lines += ["", "## What this means",
                  "The findings above are agent-written observations, not verified original source. Read the target "
                  "again with REA when a claim matters, and add a task for anything still unanswered."]
        markdown = "\n".join(lines).rstrip() + "\n"
        path = Path(proj["root"]) / "SUMMARY.md"
        path.write_text(markdown, encoding="utf-8")
        self._update_cfg(slug, lambda c: c.__setitem__("summary", {"file": "SUMMARY.md", "generated_at": now}))
        return {"slug": slug, "kind": "analysis", "title": proj["name"], "generated_at": now,
                "summary": summary, "complete": bool(rows) and not pending, "children": len(children),
                "markdown": markdown, "path": str(path)}

    def _artifact_entry_row(self, proj, entry: str):
        """The work item for one archive entry: an artifact entry, or the source item recovered from it."""
        for row in self.store.all("SELECT * FROM functions WHERE project_id=?", proj["id"]):
            meta = json.loads(row["meta"] or "{}")
            if meta.get("path") == entry and (meta.get("kind") == "artifact-entry" or meta.get("entry_type")):
                return row, meta
        return None, {}

    def analysis_contents(self, slug: str) -> dict:
        """The archive's files as a tree for the project page, including which entries already have child targets."""
        proj, cfg = self.analysis_cfg(slug)
        artifact = cfg.get("artifact") or {}
        known = {row["slug"] for row in self.store.all("SELECT slug FROM projects")}
        children = {str(c.get("entry")): c for c in cfg.get("children") or []
                    if isinstance(c, dict) and c.get("slug") in known}
        root = {"name": proj["name"], "path": "", "kind": "directory", "children": {}}
        for row in self.store.all("SELECT name, size, meta FROM functions WHERE project_id=?", proj["id"]):
            meta = json.loads(row["meta"] or "{}")
            if meta.get("kind") != "artifact-entry" and not meta.get("entry_type"):
                continue  # recovered/reconstructed entries keep their facts and stay in the tree
            path = str(meta.get("path") or row["name"])
            parts = [p for p in path.split("/") if p not in ("", ".")]
            if not parts:
                continue
            node = root
            for i, part in enumerate(parts[:-1]):
                node = node["children"].setdefault(part, {"name": part, "path": "/".join(parts[:i + 1]),
                                                          "kind": "directory", "children": {}})
            child = children.get(path)
            node["children"][parts[-1]] = {
                "name": parts[-1], "path": path, "kind": "file", "size": row["size"],
                "entry_type": meta.get("entry_type") or "unknown", "entry_format": meta.get("entry_format") or "",
                "tool": meta.get("tool") or "", "analyzable": bool(meta.get("analyzable")),
                "sha256": meta.get("sha256") or "", "child": child.get("slug") if child else None,
            }

        def clean(node):
            if node["kind"] == "file":
                return node
            kids = sorted(node["children"].values(), key=lambda n: (n["kind"] != "directory", n["name"].lower()))
            return {"name": node["name"], "path": node["path"], "kind": "directory",
                    "children": [clean(k) for k in kids]}

        return {"slug": slug, "title": proj["name"], "format": artifact.get("format") or "archive",
                "entries": artifact.get("entries") or 0, "files": artifact.get("files") or 0,
                "nodes": [clean(n) for n in sorted(root["children"].values(),
                                                  key=lambda n: (n["kind"] != "directory", n["name"].lower()))]}

    def analyze_archive_entry(self, slug: str, entry: str, log=print) -> dict:
        """Extract one archive entry and open it as a linked analysis project. Never extracts the whole archive."""
        with self._extract_lock:
            return self._open_archive_entry(slug, entry, log)

    def _open_archive_entry(self, slug: str, entry: str, log=print) -> dict:
        proj, cfg = self.analysis_cfg(slug)
        artifact = cfg.get("artifact") or {}
        if not artifact:
            raise NetworkError("this analysis project isn't an archive")
        depth = int((cfg.get("source") or {}).get("depth") or 0)
        if depth >= analysis.MAX_NESTING:
            raise NetworkError(f"nested targets stop at {analysis.MAX_NESTING} levels; open this entry separately")
        row, meta = self._artifact_entry_row(proj, entry)
        if row is None:
            raise NetworkError(f"no file named {entry!r} in this archive project")
        tool = str(meta.get("tool") or "")
        if not meta.get("analyzable"):
            data = analysis.read_archive_entry((cfg.get("target") or {}).get("path") or "", entry, 0, 16)
            info = analysis.classify_entry(entry, data)
            tool = str(info.get("tool") or "")
            if tool:
                meta = {**meta, **info}
        if not tool:
            raise NetworkError(f"{entry} isn't a target type this app can open yet")

        children = []
        for child in cfg.get("children") or []:
            if not isinstance(child, dict) or not child.get("slug"):
                continue
            try:
                self._project(child["slug"])
            except NetworkError:
                continue  # the child was removed: forget it and offer to open the entry again
            children.append(child)
        if len(children) != len(cfg.get("children") or []):
            cfg["children"] = children
            self._update_cfg(slug, lambda c: c.__setitem__("children", children))
        for child in children:
            if child.get("entry") == entry:
                log(f"{entry} is already open as {child['slug']}")
                return {"slug": child["slug"], "entry": entry, "existing": True}

        sha = str(meta.get("sha256") or "")
        target_path = Path((cfg.get("target") or {}).get("path") or "")
        if not target_path.is_file():
            raise NetworkError(f"the archive is gone: {target_path}")
        folder = Path(proj["root"]) / "extracted" / (sha[:16] or hashlib.sha256(entry.encode()).hexdigest()[:16])
        name = Path(entry).name or "entry.bin"
        dest = folder / name
        if not dest.exists():
            size = int(meta.get("size") or 0)
            extract_root = Path(proj["root"]) / "extracted"
            extracted = sum(p.stat().st_size for p in extract_root.rglob("*") if p.is_file()) if extract_root.exists() else 0
            if extracted + size > analysis.MAX_EXTRACTED_BYTES:
                raise NetworkError(f"this project already has {extracted // (1024 * 1024)} MiB of extracted files; "
                                   f"the limit is {analysis.MAX_EXTRACTED_BYTES // (1024 * 1024)} MiB. "
                                   f"Delete old files in {extract_root} to free space")
            if shutil.disk_usage(proj["root"]).free < size + (64 * 1024 * 1024):
                raise NetworkError("not enough free disk space for this entry")
            total, digest = analysis.extract_archive_entry(target_path, entry, dest)
            if extracted + total > analysis.MAX_EXTRACTED_BYTES:
                dest.unlink(missing_ok=True)
                raise NetworkError(f"this project already has {extracted // (1024 * 1024)} MiB of extracted files; "
                                   f"the limit is {analysis.MAX_EXTRACTED_BYTES // (1024 * 1024)} MiB. "
                                   f"Delete old files in {extract_root} to free space")
            if sha and digest != sha:
                dest.unlink(missing_ok=True)
                raise NetworkError(f"{entry} changed while it was being extracted (hash mismatch)")
            log(f"extracted {entry} ({total} bytes)")

        tool = str(meta.get("tool") or tool)
        child = self.import_analysis(dest, tool=tool, provider=cfg.get("provider") or "", confirmed=True,
                                     copy_target=True, log=log)
        source = {"kind": "artifact-entry", "parent": proj["slug"], "parent_title": proj["name"],
                  "entry": entry, "entry_sha256": sha, "archive": target_path.name,
                  "archive_sha256": (cfg.get("target") or {}).get("sha256") or "", "tool": tool,
                  "depth": depth + 1}
        self._update_cfg(child, lambda c: c.__setitem__("source", source))
        child_row = self.project_row(child)

        def remember(c):
            kids = c.setdefault("children", [])
            if not any(isinstance(k, dict) and k.get("slug") == child for k in kids):
                kids.append({"slug": child, "title": child_row["name"], "entry": entry, "entry_sha256": sha,
                             "tool": tool})
        self._update_cfg(slug, remember)
        with self.store.tx() as db:
            self._event(db, proj["id"], None, "entry", f"{entry} → {child}")
        with contextlib.suppress(Exception):
            self.analysis_summary(slug)
        log(f"{entry} opened as {child}")
        return {"slug": child, "entry": entry, "existing": False, "tool": tool}

    def analysis_read_data(self, slug: str, what: str, length: int = 64) -> str:
        _, cfg = self.analysis_cfg(slug)
        address = what
        try:
            fn = self._function(what, slug)
            address = f"0x{int(fn['vram']):X}"
        except NetworkError:
            pass
        try:
            value = rea.analysis_client(cfg["target"], cfg.get("provider") or "").call(
                "read_bytes", {"address": address, "length": max(1, min(int(length), 512))}, timeout=900)
        except rea.ReaError as e:
            raise NetworkError(str(e)) from e
        return analysis.pretty(value)
