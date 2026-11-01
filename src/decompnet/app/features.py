"""Analysis work items that are not procedures: features to trace, and tasks the user adds.

A feature is a question about the target ("loads a save file", "opens a socket") with the REA trace that answers it; a
task is a plain work item the user writes. Both are rows in the same table as functions, so claiming, notes and
status work for them the way they work for a procedure.
"""

# pyright: reportAttributeAccessIssue=false
# Each part is read on its own, so every self.<name> that lives in another part (or in Network) looks unknown to a
# type checker. RomNetwork is one class at runtime, and tests/test_engine_parts.py proves every name resolves there.


from __future__ import annotations

import json
import time
from pathlib import Path

from .. import rea
from ..network import Agent, NetworkError
from . import analysis


class FeaturesMixin:
    def analysis_cfg(self, slug: str) -> tuple:
        proj = self._project(slug)
        if not self.is_analysis(proj):
            raise NetworkError(f"{slug} isn't an analysis project")
        cfg = self.cfg(proj)
        return proj, cfg

    def add_analysis_task(self, agent: Agent | None, slug: str, name: str, brief: str = "") -> str:
        proj, _cfg = self.analysis_cfg(slug)
        name = analysis.safe_name(name)
        if self.store.one("SELECT 1 FROM functions WHERE project_id=? AND name=?", proj["id"], name):
            raise NetworkError(f"{name!r} is already a work item")
        with self.store.tx() as db:
            db.execute("INSERT INTO functions(project_id,name,size,n_insns,unit,vram,meta,status) VALUES(?,?,?,?,?,?,?,?)",
                       (proj["id"], name, 0, 0, "", 0, json.dumps({"kind": "task", "brief": brief.strip()}), "open"))
            self._event(db, proj["id"], agent.id if agent else None, "task", name)
        return name

    def add_feature(self, slug: str, kind: str, value: str, *, name: str = "", direction: str = "both",
                    match: str | None = None, case_sensitive: bool = False, log=print) -> dict:
        """Trace one literal seed through a JavaScript/Electron application graph and make the feature a group of
        work items: every module, channel or endpoint the trace reaches gets one, unless it already has one."""
        proj, cfg = self.analysis_cfg(slug)
        target = cfg.get("target") or {}
        if target.get("tool") != "analyze_javascript_application":
            raise NetworkError("feature tracing needs a JavaScript or Electron application project: this target "
                               "was opened with REA's " + (target.get("tool") or "binary analysis"))
        seed = analysis.feature_seed(kind, value, match, case_sensitive)
        direction = str(direction or "both").strip()
        if direction not in analysis.FEATURE_DIRECTIONS:
            raise NetworkError(f"direction must be one of {', '.join(analysis.FEATURE_DIRECTIONS)}")
        features = [f for f in cfg.get("features") or [] if isinstance(f, dict)]
        if len(features) >= analysis.MAX_FEATURES:
            raise NetworkError(f"this project already has {len(features)} features; remove one first")
        taken = {str(f.get("name") or "").casefold() for f in features}
        base = analysis.safe_name(name or str(seed["value"]))
        label, n = base, 2
        while label.casefold() in taken:
            label, n = f"{base}~{n}", n + 1

        log(f"tracing {analysis.feature_seed_text(seed)} through {Path(str(target.get('path'))).name} ({direction})…")
        try:
            record = rea.trace_feature(target, cfg.get("provider") or "", seed, direction)
        except rea.ReaError as e:
            raise NetworkError(str(e)) from e
        trace = rea.unwrap(record)
        graph = analysis.feature_graph(trace if isinstance(trace, dict) else {})
        matches = trace.get("seed_matches") if isinstance(trace, dict) else None
        coverage = trace.get("coverage") if isinstance(trace, dict) else None
        if not graph["nodes"] or not matches:
            why = " ".join(str(x) for x in ((trace or {}).get("limitations") or [])) if isinstance(trace, dict) else ""
            raise NetworkError(f"REA traced {analysis.feature_seed_text(seed)} to nothing in this application"
                               + (f": {why}" if why else "; try a shorter string or another seed"))
        log(f"{len(graph['nodes'])} nodes, {len(graph['edges'])} edges, {graph['paths']} paths")

        candidates = [node for node in graph["nodes"] if node.get("kind") in analysis.FEATURE_WORK_KINDS
                      and not analysis.feature_external(node)]
        if not candidates:
            candidates = [node for node in graph["nodes"] if node.get("name") and not analysis.feature_external(node)]
        priority = {kind: i for i, kind in enumerate(analysis.FEATURE_WORK_PRIORITY)}
        candidates.sort(key=lambda node: (priority.get(node["kind"], len(priority)), node.get("name") or ""))
        created = linked = grouped = 0
        now = time.time()
        unit = f"feature:{label}"
        with self.store.tx() as db:
            rows = db.execute("SELECT id, name, meta FROM functions WHERE project_id=?", (proj["id"],)).fetchall()
            names = {row["name"] for row in rows}
            by_name = {row["name"]: row for row in rows}
            by_path: dict[str, object] = {}
            for row in rows:
                meta = json.loads(row["meta"] or "{}")
                if meta.get("path"):
                    by_path.setdefault(str(meta["path"]), row)
            key_items: dict[str, str] = {}
            for node in candidates:
                key = str(node.get("path") or node.get("name") or "").casefold()
                if key and key in key_items:  # another graph role of the same file or channel
                    node["work_item"] = key_items[key]
                    grouped += 1
                    continue
                row: dict | None = (by_path.get(node["path"]) if node.get("path") else None) or \
                    by_name.get(node.get("label") or "") or by_name.get(node.get("name") or "")
                if row is not None:  # an existing procedure or archive entry: the feature points at it
                    node["work_item"] = row["name"]
                    if key:
                        key_items[key] = row["name"]
                    linked += 1
                    continue
                if created >= analysis.MAX_FEATURE_ITEMS:
                    node["work_item"] = ""
                    continue
                item = analysis.safe_name(node["name"])
                if item in names and node.get("kind"):
                    qualified = analysis.safe_name(f"{node['name']} ({node['kind']})")
                    if qualified not in names:
                        item = qualified
                stem, k = item, 2
                while item in names:
                    item, k = f"{stem}~{k}", k + 1
                names.add(item)
                meta = {"kind": "feature-node", "feature": label, "node_id": node["node_id"],
                        "node_kind": node["kind"], "path": node["path"], "seed": analysis.feature_seed_text(seed),
                        "state": node["state"], "authority": node["authority"]}
                db.execute(
                    "INSERT INTO functions(project_id,name,size,n_insns,unit,vram,meta,status) VALUES(?,?,?,?,?,?,?,?)",
                    (proj["id"], item, 0, 0, unit, 0, json.dumps(meta), "open"))
                node["work_item"] = item
                if key:
                    key_items[key] = item
                created += 1
            feature = {"name": label, "seed": seed, "direction": direction,
                       "trace_id": str(trace.get("trace_id") or "") if isinstance(trace, dict) else "",
                       "evidence_id": str(record.get("evidence_id") or "") if isinstance(record, dict) else "",
                       "coverage": coverage or {}, "summary": (trace.get("summary") or {}) if isinstance(trace, dict) else {},
                       "limitations": [str(x) for x in ((trace or {}).get("limitations") or [])][:20]
                       if isinstance(trace, dict) else [],
                       "nodes": graph["nodes"], "edges": graph["edges"], "paths": graph["paths"],
                       "truncated": graph["truncated"], "created_at": now,
                       "work_items": {"created": created, "linked": linked, "grouped": grouped}}
            cfg = json.loads(db.execute("SELECT config FROM projects WHERE id=?", (proj["id"],)).fetchone()["config"])
            cfg["features"] = [*features, feature]
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))
            self._event(db, proj["id"], None, "feature",
                        f"{label}: {len(graph['nodes'])} nodes, {created} new work items")
        return self._feature_summary(feature) | {"nodes": feature["nodes"], "edges": feature["edges"],
                                                 "limitations": feature["limitations"],
                                                 "work_items": feature["work_items"]}

    @staticmethod
    def _feature_summary(feature: dict) -> dict:
        nodes = feature.get("nodes") or []
        items = [n for n in nodes if n.get("work_item")]
        return {"name": feature.get("name") or "", "seed": feature.get("seed") or {},
                "direction": feature.get("direction") or "both", "trace_id": feature.get("trace_id") or "",
                "coverage": feature.get("coverage") or {}, "summary": feature.get("summary") or {},
                "created_at": feature.get("created_at"), "node_count": len(nodes),
                "edge_count": len(feature.get("edges") or []), "item_count": len(items),
                "truncated": bool(feature.get("truncated"))}

    def features(self, slug: str) -> dict:
        """The features traced in an analysis project, without their graphs (the page asks for one at a time)."""
        _proj, cfg = self.analysis_cfg(slug)
        return {"slug": slug, "traceable": (cfg.get("target") or {}).get("tool") == "analyze_javascript_application",
                "features": [self._feature_summary(f) for f in cfg.get("features") or [] if isinstance(f, dict)]}

    def feature(self, slug: str, name: str) -> dict:
        """One traced feature with its nodes and edges, and how each work item it points at is doing."""
        proj, cfg = self.analysis_cfg(slug)
        for feature in cfg.get("features") or []:
            if isinstance(feature, dict) and feature.get("name") == name:
                rows = {row["name"]: row for row in self.store.all(
                    "SELECT name, status, matched_source FROM functions WHERE project_id=?", proj["id"])}
                nodes = []
                for node in feature.get("nodes") or []:
                    row = rows.get(str(node.get("work_item") or ""))
                    nodes.append(node | {"status": row["status"] if row is not None else "",
                                         "reached_by": analysis.reached_by(feature.get("edges") or [],
                                                                          feature.get("nodes") or [],
                                                                          node.get("node_id") or "")})
                return self._feature_summary(feature) | {"nodes": nodes, "edges": feature.get("edges") or [],
                                                        "limitations": feature.get("limitations") or []}
        raise NetworkError(f"no feature named {name!r} in {slug}")

    def remove_feature(self, slug: str, name: str) -> dict:
        """Forget a feature and the work items it created that nobody has analyzed yet."""
        proj, cfg = self.analysis_cfg(slug)
        features = [f for f in cfg.get("features") or [] if isinstance(f, dict)]
        if not any(f.get("name") == name for f in features):
            raise NetworkError(f"no feature named {name!r} in {slug}")
        with self.store.tx() as db:
            rows = db.execute("SELECT id, name, meta, status FROM functions WHERE project_id=?", (proj["id"],)).fetchall()
            removed = 0
            for row in rows:
                meta = json.loads(row["meta"] or "{}")
                if meta.get("kind") == "feature-node" and meta.get("feature") == name and row["status"] != "matched":
                    db.execute("DELETE FROM claims WHERE function_id=?", (row["id"],))
                    db.execute("DELETE FROM notes WHERE function_id=?", (row["id"],))
                    db.execute("DELETE FROM functions WHERE id=?", (row["id"],))
                    removed += 1
            cfg = json.loads(db.execute("SELECT config FROM projects WHERE id=?", (proj["id"],)).fetchone()["config"])
            cfg["features"] = [f for f in features if f.get("name") != name]
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))
            self._event(db, proj["id"], None, "feature", f"removed {name} ({removed} open items)")
        return {"removed": name, "work_items": removed}
