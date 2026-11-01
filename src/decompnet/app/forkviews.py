"""What the page is shown for a fork: its overview, a function's diff and detail.

The fork's own objdiff is the judge, so these read the last measurement instead of running one: a page that opened a file
to find out how it stands would compile it, and the page polls."""

# pyright: reportAttributeAccessIssue=false
# A type checker reads one part at a time, so a self.<name> owned by another part (or by the engine itself) looks
# unknown. ForkEngine is one class at runtime; tests/test_fork_parts.py proves every name resolves there.


from __future__ import annotations

from ..forks import catalog
from ..forks.objdiff import ObjdiffError
from ..forks.project import ForkError
from .forkutil import (
    _floor_pct,
)


class ForkViewMixin:
    def overview(self, slug: str) -> dict:
        proj, fp, cfg = self.get(slug)
        agg = self.net.store.one(
            "SELECT COUNT(*) AS total, SUM(status='matched') AS matched, SUM(size) AS bytes, "
            "SUM(CASE WHEN status='matched' THEN size ELSE 0 END) AS matched_bytes, SUM(attempts) AS attempts "
            "FROM functions WHERE project_id=?", proj["id"])
        m = cfg.get("measures") or {}
        need = fp.needs(cfg.get("version")) if not cfg.get("ready") else None
        return {
            "slug": slug, "title": proj["name"], "description": proj["description"], "kind": "fork",
            "platform": cfg.get("platform"), "platform_name": catalog.PLATFORMS.get(cfg.get("platform"), cfg.get("platform") or ""),
            "repo_url": cfg.get("repo_url"), "fork_kind": cfg.get("fork_kind"), "version": cfg.get("version"),
            "origin": cfg.get("origin"), "compiler_name": cfg.get("compiler"),
            "versions": fp.versions(), "ready": bool(cfg.get("ready")), "needs": need,
            "rom": {"game_code": cfg.get("version") or "", "region": catalog.PLATFORMS.get(cfg.get("platform"), ""),
                    "title": proj["name"]},
            "compiler": {"toolchain": "project", "name": "Project's own build", "flags": [], "installed": True,
                         "proprietary": False, "supported": True},
            "compilers": [],
            "functions": agg["total"] or 0, "matched": agg["matched"] or 0, "bytes": agg["bytes"] or 0,
            "matched_bytes": agg["matched_bytes"] or 0, "attempts": agg["attempts"] or 0,
            "context_version": 0, "folder": proj["root"], "room": self.net._room_info(slug),
            "measures": m, "uncovered": self.uncovered(slug) if cfg.get("ready") and fp.linkable() else None,
            "rebuild": (fp.manifest or {}).get("rebuild") if cfg.get("origin") == "scratch" else None,
        }

    def function_diff(self, slug: str, name: str) -> dict:
        """objdiff's diff of the function as it is in the source tree now."""
        _proj, fp, _cfg = self.get(slug)
        fn = self._fn(slug, name)
        meta = self._meta(fn)
        try:
            d = fp.diff(fp.unit(fn["unit"]), meta.get("symbol") or fn["name"])
        except (ObjdiffError, ForkError) as e:
            return {"kind": None, "rows": None, "error": f"objdiff: {e}"}
        if d.match_percent is None:
            return {"kind": None, "rows": None}
        problems = [f"a loaded constant differs: {m.describe()}" for m in d.literals]
        problems += [f"refers to `{mine}` where the original refers to `{orig}`" for orig, mine in d.renamed]
        return {"kind": "matched" if fn["status"] == "matched" else "latest", "rows": [list(r) for r in d.rows],
                "score": None, "percent": _floor_pct(d.match_percent, d.matched), "problems": problems}

    def function_detail(self, slug: str, name: str) -> dict:
        _proj, fp, _cfg = self.get(slug)
        fn = self._fn(slug, name)
        meta = self._meta(fn)
        unit = fp.unit(fn["unit"])
        asm, current, pct = "", None, None
        try:
            d = fp.diff(unit, meta.get("symbol") or fn["name"])
            asm, pct = d.target_asm(), (None if d.match_percent is None else _floor_pct(d.match_percent, d.matched))
        except (ObjdiffError, ForkError) as e:
            asm = f"(objdiff: {e})"
        if unit.source and (fp.root / unit.source).exists():
            text = (fp.root / unit.source).read_text(errors="replace", encoding="utf-8")
            _, span = self._find(text, self._names(meta, fn))
            current = text[span[0]:span[1]] if span else None
        attempts = self.net.store.all("SELECT t.score, t.created_at, a.name AS agent FROM attempts t LEFT JOIN agents a "
                                      "ON a.id=t.agent_id WHERE t.function_id=? ORDER BY t.id", fn["id"])
        notes = self.net.store.all("SELECT n.text, n.created_at, a.name AS agent FROM notes n LEFT JOIN agents a "
                                   "ON a.id=n.agent_id WHERE n.function_id=? ORDER BY n.id DESC LIMIT 10", fn["id"])
        return {"name": fn["name"], "vram": fn["vram"] or 0, "rom_offset": 0, "size": fn["size"], "insns": fn["n_insns"],
                "status": fn["status"], "best": fn["best_score"], "asm": asm, "unit": fn["unit"],
                "source": current or fn["best_source"], "source_kind": "matched" if fn["status"] == "matched" else
                ("latest" if current else "best" if fn["best_source"] else None),
                "match_percent": pct, "scores": [{"score": a["score"], "t": a["created_at"], "agent": a["agent"]} for a in attempts],
                "notes": [dict(n) for n in notes], "calls": [], "called_by": []}

