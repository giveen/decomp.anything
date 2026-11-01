"""The starting point an analysis work item gets before an agent reads it.

A procedure no one has decompiled yet has no code for an agent to argue with, so the first agent to claim it gets a
batch of decompiler drafts: entry points and the largest functions for the survey item, the addresses a task names for
a task. A batch is decompiled once and kept (in this process, and in the project's config for a restart), because
decompiling the same function twice costs minutes. _analysis_brief is the same evidence as a work item's brief, and
complete_analysis is what an agent writes back.
"""

# pyright: reportAttributeAccessIssue=false
# Each part is read on its own, so every self.<name> that lives in another part (or in Network) looks unknown to a
# type checker. RomNetwork is one class at runtime, and tests/test_engine_parts.py proves every name resolves there.


from __future__ import annotations

import contextlib
import hashlib
import json
import re
import threading
import time

from .. import rea
from ..network import Agent, NetworkError, _ago
from . import analysis

# Analysis projects: work items with no address of their own (the survey item, a task) get a batch-decompiled
# starting point. Entry points first, then the largest functions, at most this many.
ANALYSIS_PREFILL_LIMIT = 4

ANALYSIS_PREFILL_CACHE_ITEMS = 16  # persisted per project: recent survey/task batches, for a restart to reuse

ANALYSIS_START_NAMES = re.compile(r"^(?:main|entry|_start|start|WinMain|wmain|DllMain|JNI_OnLoad)$", re.IGNORECASE)


class PrefillMixin:
    def _analysis_start_addresses(self, proj, client=None, cfg: dict | None = None) -> list[str]:
        """Functions a survey (or a task with no address) should start from: entry points, then the largest.
        A survey exists because the import stored no procedure inventory, so ask REA once more before giving up."""
        rows = self.store.all("SELECT name, vram, size FROM functions WHERE project_id=? AND vram>0", proj["id"])
        if not rows and client is not None:
            try:
                found = analysis.procedures(client.call("list_procedures", {}, timeout=300, any_tool=True),
                                            (cfg or {}).get("segments") or [])
            except rea.ReaError:
                found = []
            rows = [{"name": row["name"], "vram": row["address"], "size": row["size"]} for row in found]
        if not rows:
            return []
        starts = {int(row["vram"]) for row in rows if ANALYSIS_START_NAMES.match(str(row["name"] or ""))}
        start_rows = sorted((row for row in rows if int(row["vram"]) in starts), key=lambda row: int(row["vram"]))
        rest = sorted((row for row in rows if int(row["vram"]) not in starts), key=lambda row: -(row["size"] or 0))
        out: list[str] = []
        seen: set[int] = set()
        for row in [*start_rows, *rest]:
            address = int(row["vram"])
            if address in seen:
                continue
            seen.add(address)
            out.append(f"0x{address:X}")
            if len(out) >= ANALYSIS_PREFILL_LIMIT:
                break
        return out

    @staticmethod
    def _analysis_prefill_key(meta: dict, cfg: dict, kind: str) -> str:
        """A compact key for one work item's starting batch: the target, provider and item it was built for."""
        target = cfg.get("target") or {}
        raw = json.dumps([str(target.get("path") or ""), str(target.get("sha256") or ""),
                          cfg.get("provider") or "", kind,
                          str(meta.get("brief") or "") if kind == "task" else ""])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _persisted_prefill(cfg: dict, key: str) -> dict | None:
        cache = cfg.get("prefill")
        stored = cache.get(key) if isinstance(cache, dict) else None
        if not isinstance(stored, dict):
            return None
        items = stored.get("items")
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            return None
        try:
            more = max(0, int(stored.get("more") or 0))
        except (TypeError, ValueError):
            more = 0
        return {"heading": str(stored.get("heading") or "Starting functions"),
                "note": str(stored.get("note") or ""),
                "items": [dict(item) for item in items], "more": more}

    def _cached_prefill(self, proj, cfg: dict, key: str) -> dict | None:
        """One starting batch from this process or the project; None when it has never been built."""
        with self._analysis_prefill_lock:
            payload = self._analysis_prefill_cache.get((proj["id"], key))
        if payload is None:
            payload = self._persisted_prefill(cfg, key)
            if payload is not None:
                with self._analysis_prefill_lock:
                    self._analysis_prefill_cache[(proj["id"], key)] = payload
        return payload

    @staticmethod
    def _prefill_lines(payload: dict) -> list[str]:
        heading = str(payload.get("heading") or "Starting functions")
        note = str(payload.get("note") or "")
        return ["", f"## {heading}", note, "",
                analysis.batch_text(payload.get("items"), int(payload.get("more") or 0))]

    def _remember_prefill(self, proj, key: str, payload: dict) -> None:
        """Keep one batch with the project (so a restart does not decompile it again) and in this process."""
        def change(cfg):
            cache = cfg.get("prefill")
            if not isinstance(cache, dict):
                cache = {}
            cache[key] = {**payload, "at": time.time()}
            if len(cache) > ANALYSIS_PREFILL_CACHE_ITEMS:
                def saved(item):
                    value = cache.get(item)
                    return float(value.get("at") or 0) if isinstance(value, dict) else 0.0
                for old in sorted(cache, key=saved)[:len(cache) - ANALYSIS_PREFILL_CACHE_ITEMS]:
                    cache.pop(old, None)
            cfg["prefill"] = cache
        with contextlib.suppress(Exception):  # a cache write must never fail the claim or the view
            self._update_cfg(proj["slug"], change)

    @staticmethod
    def _analysis_prefillable(cfg: dict, meta: dict) -> bool:
        """Whether REA can decompile a starting batch for this work item."""
        return (str(meta.get("kind") or "") in ("survey", "task")
                and (cfg.get("target") or {}).get("tool") == "open_binary")

    def _analysis_prefill(self, cfg: dict, proj, meta: dict, *, strict: bool = False, log=None) -> list[str]:
        """Batch-decompiled starting functions for a work item that names no address of its own: the survey item
        gets the target's entry points and largest functions, a task the addresses its brief names. Read-only,
        and never fatal: the item is still claimable when REA can't decompile the batch."""
        kind = str(meta.get("kind") or "")
        if kind not in ("survey", "task"):
            return []
        target = cfg.get("target") or {}
        if target.get("tool") != "open_binary":
            return []  # Android, .NET, JavaScript, firmware and archive targets have no native provider
        named: list[int] = []
        for found in re.findall(r"0x[0-9a-fA-F]+", str(meta.get("brief") or "")):
            address = int(found, 16)
            if address and address not in named:
                named.append(address)
        if kind == "task" and named:
            heading = "Functions named in the task"
            note = ("The addresses the brief names, decompiled in one batch. Each is a draft, not the original "
                    "source; check it against the assembly before you rely on it.")
        else:
            heading = "Starting functions"
            note = ("The target's entry points and largest functions, decompiled in one batch. Each is a draft, "
                    "not the original source; check it against the assembly before you rely on it.")
        key = self._analysis_prefill_key(meta, cfg, kind)
        slot = (proj["id"], key)
        while True:
            payload = self._cached_prefill(proj, cfg, key)
            if payload is not None:
                return self._prefill_lines(payload)
            with self._analysis_prefill_lock:
                event = self._analysis_prefill_running.get(slot)
                if event is None:
                    event = self._analysis_prefill_running[slot] = threading.Event()
                    break
            # another request (the auto job or a drawer click) is building this exact batch: its result lands in
            # the cache before it wakes us, so looping re-checks instead of running batch_decompile twice
            if log:
                log("waiting for another request to finish decompiling the same starting functions…")
            event.wait()
        try:
            try:
                client = rea.analysis_client(target, cfg.get("provider") or "")
                addresses = ([f"0x{address:X}" for address in named[:ANALYSIS_PREFILL_LIMIT]]
                             if kind == "task" and named else self._analysis_start_addresses(proj, client, cfg))
                if not addresses:
                    if strict:
                        raise NetworkError("REA listed no procedures for this item to start from")
                    return []
                if log:
                    log(f"decompiling {len(addresses)} function{'s' if len(addresses) != 1 else ''}: "
                        + ", ".join(addresses))
                value = client.call("batch_decompile", {"addresses": addresses}, timeout=1800, any_tool=True)
            except rea.ReaError as e:
                if strict:
                    raise NetworkError(f"REA couldn't decompile the starting functions: {e}") from e
                return ["", f"## {heading}", f"REA couldn't decompile them: {e}"]
            view = analysis.batch_view(value)
            if not view["items"]:
                if strict:
                    raise NetworkError("REA returned no starting functions for this item")
                return []
            if log:
                log(f"REA decompiled {len(view['items'])} starting function{'s' if len(view['items']) != 1 else ''}.")
            payload = {"heading": heading, "note": note, **view}
            with self._analysis_prefill_lock:
                self._analysis_prefill_cache[slot] = payload
            self._remember_prefill(proj, key, payload)
            return self._prefill_lines(payload)
        finally:
            # wake the waiters only after the cache has the payload (or after a failure they'll see as no cache)
            with self._analysis_prefill_lock:
                if self._analysis_prefill_running.get(slot) is event:
                    self._analysis_prefill_running.pop(slot, None)
            event.set()

    def _analysis_prefill_view(self, cfg: dict, proj, meta: dict) -> dict | None:
        """A survey's or task's cached starting functions for the project page, without decompiling anything."""
        if not self._analysis_prefillable(cfg, meta):
            return None
        payload = self._cached_prefill(proj, cfg, self._analysis_prefill_key(meta, cfg, str(meta.get("kind") or "")))
        if payload is None:
            return None
        return {"heading": payload.get("heading") or "Starting functions",
                "note": payload.get("note") or "",
                "items": payload.get("items") or [],
                "more": int(payload.get("more") or 0)}

    def analysis_prefill(self, slug: str, name: str, log=print) -> dict:
        """Build (or reuse) the starting functions the project page shows for a survey or a task."""
        proj, cfg = self.analysis_cfg(slug)
        fn = self._function(name, slug)
        meta = json.loads(fn["meta"] or "{}")
        if not self._analysis_prefillable(cfg, meta):
            raise NetworkError("starting functions are for the survey item and tasks on native targets")
        if (cached := self._analysis_prefill_view(cfg, proj, meta)) is not None:
            log(f"using the starting functions already cached for {name}.")
            return cached
        log(f"decompiling starting functions for {name}…")
        self._analysis_prefill(cfg, proj, meta, strict=True, log=log)
        view = self._analysis_prefill_view(cfg, proj, meta)
        if view is None:
            raise NetworkError("REA returned no starting functions for this item")
        return view

    def prefill_survey(self, slug: str, log=print) -> dict | None:
        """Build the survey item's starting batch, for the background job an import starts.

        An import that stored no procedure inventory leaves one survey item; building its batch now means the
        project page opens on starting functions instead of the button. A project without that item (or a target
        whose starting functions aren't native procedures) has nothing to build: that is a skip, not a failure.
        """
        proj, cfg = self.analysis_cfg(slug)
        row = self.store.one("SELECT meta FROM functions WHERE project_id=? AND name='survey'", proj["id"])
        if row is None:
            return None
        meta = json.loads(row["meta"] or "{}")
        if not self._analysis_prefillable(cfg, meta):
            return None
        log("the import is done: decompiling the survey's starting functions in the background")
        return self.analysis_prefill(slug, "survey", log=log)

    def _analysis_brief(self, fn, agent: Agent | None, header: str) -> str:
        proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        cfg = self.cfg(proj)
        target = cfg.get("target") or {}
        meta = json.loads(fn["meta"] or "{}")
        claim = self.store.one("SELECT * FROM claims WHERE function_id=?", fn["id"])
        lines = [f"## {header}: {proj['slug']}/{fn['name']}",
                 f"target: {target.get('path')}",
                 f"{target.get('format') or 'file'} · {target.get('architecture') or 'unknown'} · "
                 f"{target.get('kind') or 'analysis target'}"]
        if meta.get("address"):
            lines.append(f"address: 0x{int(meta['address']):X}")
        if claim:
            who = "you" if agent and claim["agent_id"] == agent.id else self._agent_name(claim["agent_id"])
            lines.append(f"claimed by {who}, lease expires in {int(claim['expires_at'] - time.time()) // 60} min")
        if meta.get("kind") == "feature-node":
            feature = next((f for f in cfg.get("features") or []
                            if isinstance(f, dict) and f.get("name") == meta.get("feature")), None)
            if feature:
                nodes = feature.get("nodes") or []
                node = next((n for n in nodes if n.get("node_id") == meta.get("node_id")), {})
                lines += ["", f"feature {feature['name']}: {analysis.feature_seed_text(feature.get('seed'))} "
                              f"({feature.get('direction') or 'both'})",
                          f"this node: {node.get('kind') or meta.get('node_kind') or 'application node'}"
                          + (f" at {node['path']}" if node.get("path") else "")]
                reached = analysis.reached_by(feature.get("edges") or [], nodes, str(meta.get("node_id") or ""))
                if reached:
                    lines.append(f"reached from: {reached}")
                if feature.get("limitations"):
                    lines.append("trace limits: " + "; ".join(str(x) for x in feature["limitations"][:4]))
                lines += ["", "Report what this node contributes to the feature: what it does, what it exchanges "
                              "with its neighbours, and anything the trace leaves open. Static reachability is not "
                              "proof it runs."]
        if fn["status"] == "matched":
            finding = (fn["matched_source"] or "").strip()
            lines += [f"ANALYZED by {self._agent_name(fn['matched_agent_id'])} {_ago(fn['matched_at'])}:",
                      finding or "(no finding text: the reconstructed code is in recovered/)"]
        if meta.get("kind") == "task" and meta.get("brief"):
            lines += ["", "The user's task:", meta["brief"]]
        if meta.get("kind") == "artifact-entry":
            path = str(meta.get("path") or fn["name"])
            lines += ["", f"archive entry: {path}", f"size: {fn['size']} bytes"]
            if meta.get("sha256"):
                lines.append(f"sha256: {meta['sha256']}")
            if meta.get("executable"):
                lines.append("executable: yes")
            if meta.get("encrypted"):
                lines.append("encrypted: yes")
            data = analysis.read_archive_entry(target.get("path") or "", path, 0, 4096)
            lines += ["", "entry preview (bounded):", "```", analysis.preview_bytes(data), "```"]
            lines += ["", "Read more with read_artifact_entry(entry, offset, length); the archive inventory is "
                          "available through rea_tool(\"inspect_artifact\", {})."]
        elif meta.get("kind") in ("recovered-source", "reconstruction"):
            path = str(meta.get("path") or fn["name"])
            archive = meta.get("origin") == "archive" or bool(meta.get("entry_type"))
            if meta.get("kind") == "recovered-source":
                source = ("source: extracted byte for byte from the archive "
                          f"({meta.get('entry_format') or 'original file'})" if archive
                          else f"source map: {meta.get('map') or 'inline'}")
                lines += ["", f"recovered file: {path}", f"size: {fn['size']} bytes", source,
                          "This is the original file, not a reconstruction: it does not need writing. Read it, "
                          "name what it does in a note, and complete it as a finding."]
            elif archive:
                lines += ["", f"archive entry to reconstruct: {path}",
                          f"entry format: {meta.get('entry_format') or meta.get('module_kind') or 'binary'}",
                          f"size: {fn['size']} bytes"]
                data = analysis.read_archive_entry(target.get("path") or "", path, 0, 4096)
                lines += ["", "entry preview (bounded):", "```", analysis.preview_bytes(data), "```",
                          "", "Its source did not ship as text. Read more with read_artifact_entry(entry, offset, "
                          "length), check the archive inventory with rea_tool(\"inspect_artifact\", {}), then save "
                          f"the recovered source with write_source(\"{fn['name']}\", <source>). It is labelled "
                          "reconstructed and unverified; never call it original source."]
            else:
                lines += ["", f"module to reconstruct: {path}",
                          f"module kind: {meta.get('module_kind') or 'javascript module'}",
                          f"No source map covers this module. Reconstruct it as JavaScript from the graph, the "
                          f"bundle and its neighbours, then save it with write_source(\"{fn['name']}\", <source>). "
                          "It will be marked reconstructed and unverified; never call it original source."]
        elif meta.get("kind") == "feature-node":
            # The feature block above is this item's brief; a JavaScript application has no binary provider to ask.
            pass
        elif target.get("tool") == "analyze_javascript_application":
            # A JavaScript graph has no binary provider to ask for an overview or a function dossier.
            pass
        else:
            try:
                client = rea.analysis_client(target, cfg.get("provider") or "")
                if meta.get("kind") == "procedure" and meta.get("address"):
                    dossier = client.call("analyze_function", {"procedure": f"0x{int(meta['address']):X}"}, timeout=1800)
                    lines += ["", analysis.dossier(dossier)]
                else:
                    ov = client.call("binary_overview", {}, timeout=900)
                    segs = analysis.clean_segments(client.call("list_segments", {}, timeout=900))
                    lines += ["", "REA overview:", analysis.pretty(ov)]
                    if segs:
                        lines += ["", "segments:", analysis.pretty(segs)]
            except rea.ReaError as e:
                lines += ["", f"REA couldn't read this item: {e}", "Try another view with rea_tool."]
            if meta.get("reconstructed") and fn["best_source"]:
                lines += ["", f"### Reconstructed code (unverified, saved in {meta.get('path') or 'recovered/'}): "
                              "not original source", "```", fn["best_source"].strip(), "```"]
            elif meta.get("kind") in ("procedure", "task"):
                lines += ["", "No byte-exact reference exists for this target. Once the evidence supports it, "
                              f"write_source(\"{fn['name']}\", <source>) saves the recovered implementation in "
                              "recovered/: labelled reconstructed and unverified, never original source. Say what "
                              "you based it on in a note."]
        lines += self._analysis_prefill(cfg, proj, meta)
        notes = self.store.all(
            "SELECT n.text, n.created_at, a.name FROM notes n LEFT JOIN agents a ON a.id=n.agent_id "
            "WHERE n.function_id=? ORDER BY n.id DESC LIMIT 8", fn["id"])
        if notes:
            lines += ["", "### Notes from previous agents"] + [
                f"- ({n['name']}, {_ago(n['created_at'])}) {n['text']}" for n in notes]
        pnotes = self.store.all(
            "SELECT n.text, a.name FROM notes n LEFT JOIN agents a ON a.id=n.agent_id "
            "WHERE n.project_id=? AND n.function_id IS NULL ORDER BY n.id DESC LIMIT 5", proj["id"])
        if pnotes:
            lines += ["", "### Project-wide notes"] + [f"- ({n['name']}) {n['text']}" for n in pnotes]
        if header == "CLAIMED":
            if meta.get("kind") == "reconstruction":
                lines += ["", f"Next: reconstruct {meta.get('path') or fn['name']} as JavaScript and save it with "
                              f"write_source(\"{fn['name']}\", source). That writes the file, marks it analyzed and "
                              "labels it unverified. Release it with notes if it needs someone else."]
            elif meta.get("kind") == "artifact-entry":
                lines += ["", "Next: read_artifact_entry(entry, offset, length) for more of this file, write what "
                              "matters with add_note, then complete_function(function, summary). Release it with "
                              "notes if it needs someone else."]
            else:
                lines += ["", "Next: investigate with rea_tool (search_strings, search_procedures, xrefs, list_segments…), "
                              "write what matters with add_note, then complete_function(function, summary). "
                              "Release it with notes if it needs someone else."]
        return "\n".join(lines)

    def complete_analysis(self, agent: Agent, project: str | None, function: str, summary: str) -> str:
        fn = self._function(function, project)
        proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        if not self.is_analysis(proj):
            raise NetworkError("complete_function is for analysis projects; compile_and_diff matches a game")
        cfg = self.cfg(proj)
        summary = summary.strip()
        if not summary:
            raise NetworkError("write the finding first: what this item does, what it calls, what matters")
        if fn["status"] == "matched":
            raise NetworkError(f"{fn['name']} is already analyzed")
        if json.loads(fn["meta"] or "{}").get("kind") == "reconstruction":
            raise NetworkError(f"{fn['name']} is a source module: write it with write_source(name, source) so the "
                               "reconstructed file is saved with the finding")
        now = time.time()
        with self.store.tx() as db:
            claim = db.execute("SELECT agent_id FROM claims WHERE function_id=?", (fn["id"],)).fetchone()
            if claim is None or claim["agent_id"] != agent.id:
                raise NetworkError(f"you don't hold a claim on {fn['name']}")
            db.execute("UPDATE functions SET status='matched', matched_source=?, matched_agent_id=?, matched_at=?, "
                       "best_score=0, best_source=? WHERE id=?", (summary, agent.id, now, summary, fn["id"]))
            db.execute("DELETE FROM claims WHERE function_id=?", (fn["id"],))
            db.execute("INSERT INTO notes(project_id,function_id,agent_id,text,created_at) VALUES(?,?,?,?,?)",
                       (proj["id"], fn["id"], agent.id, summary, now))
            self._event(db, proj["id"], agent.id, "analyze", fn["name"])
        done = self.store.one("SELECT COUNT(*) AS n FROM functions WHERE project_id=? AND status!='matched'",
                              proj["id"])["n"] == 0
        if done:
            with contextlib.suppress(Exception):
                self.analysis_summary(proj["slug"])
        parent = (cfg.get("source") or {}).get("parent")
        if parent:
            with contextlib.suppress(Exception):
                self.analysis_summary(parent)
        return (f"✅ {fn['name']} is analyzed and saved. Your finding is on it and in the project's notes. "
                "Call claim_function for the next item, or add_note about the target as a whole."
                + (" The project summary is in SUMMARY.md." if done else ""))
