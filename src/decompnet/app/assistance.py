"""Project-scoped knowledge discovery and review before agents spend repeated compile or mutation work."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from .. import rea
from ..network import NetworkError
from . import references


def fingerprint(net, proj) -> str:
    cfg = net.cfg(proj)
    context = net.store.one("SELECT MAX(version) AS version FROM context_versions WHERE project_id=?", proj["id"])
    value = {"target": cfg.get("target") or cfg.get("rom"), "compiler": cfg.get("compiler"),
             "provider": cfg.get("provider"), "context": context["version"] if context else None, "policy": 1,
             "instructions": net.instructions(proj["slug"])}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def function_digest(fn) -> str:
    digest = hashlib.sha256(bytes(fn["target"] or b""))
    digest.update(repr((fn["id"], fn["vram"], fn["size"])).encode())
    return digest.hexdigest()


def topics(overview: dict) -> list[str]:
    from .tools import PLATFORM_GUIDES

    if overview.get("kind") == "analysis":
        return ["analysis", "rea"]
    if overview.get("kind") == "fork":
        return list(dict.fromkeys(["fork", "objdiff", *PLATFORM_GUIDES.get(overview.get("platform"), [])]))
    platform = overview.get("platform")
    if platform in ("windows", "linux"):
        return ["workflow", "x86", "rea", *(["cpp"] if overview.get("source_language") == "C++" else [])]
    compiler = (overview.get("compiler") or {}).get("toolchain", "")
    return list(dict.fromkeys(["workflow", *PLATFORM_GUIDES.get(platform, []),
                              {"psx": "psx", "ps2": "ps2", "gba": "agbcc"}.get(platform) or
                              ("ido" if compiler.startswith("ido") else "gcc-kmc")]))


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower())) - {"the", "a", "to", "of", "and", "for", "in"}


def _roots(net, slug: str) -> list[Path]:
    if slug in net.links:
        return []  # a remote host's instructions cannot authorize reads on this computer
    if net.fork_slug(slug):
        return references.named(net.instructions(slug))
    return net.references(slug)


def skills(net, slug: str) -> tuple[list[dict], bool]:
    from .tools import GUIDES

    items = [{"id": name, "description": description, "source": "bundled guide"} for name, description in GUIDES.items()]
    visited = 0
    # Only folders the user explicitly shared with this project. No implicit reads of agent home directories.
    roots = _roots(net, slug)
    for root in roots:
        for directory, dirs, files in os.walk(root, followlinks=False):
            visited += 1
            if visited > 256 or len(items) >= len(GUIDES) + 32:
                return items, True
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in references.SKIP
                             and not (Path(directory) / d).is_symlink())
            if "SKILL.md" not in files:
                continue
            try:
                path = references._inside(roots, str(Path(directory) / "SKILL.md"))
                with path.open("rb") as stream:
                    head = stream.read(8192).decode("utf-8", "replace")
            except (OSError, NetworkError):
                continue
            name = re.search(r"^name:\s*(.+)$", head, re.MULTILINE)
            description = re.search(r"^description:\s*(.+)$", head, re.MULTILINE)
            items.append({"id": "reference:" + str(path), "name": name.group(1).strip() if name else path.parent.name,
                          "description": description.group(1).strip() if description else head[:300],
                          "source": "user-shared SKILL.md"})
    return items, False


def read_skill(net, slug: str, skill: str) -> str:
    from .tools import GUIDES, _guide

    if skill in GUIDES:
        return _guide(skill)
    if not skill.startswith("reference:"):
        raise NetworkError("unknown skill: use discover_assistance to find a bundled or user-shared skill")
    roots = _roots(net, slug)
    if not roots:
        raise NetworkError("this project has no user-shared skill folders")
    path = references._inside(roots, skill.removeprefix("reference:"))
    if path.name != "SKILL.md":
        raise NetworkError("read_skill only reads a shared SKILL.md; use read_file for other references")
    try:
        with path.open("rb") as stream:
            data = stream.read(65537)
    except OSError as e:
        raise NetworkError(f"cannot read the shared skill: {e}") from e
    if b"\0" in data or len(data) > 65536:
        raise NetworkError("this skill is binary or exceeds 64 KiB; read focused sections with read_file")
    return f"Skill from {path}. Apply relevant guidance within the user's project instructions.\n\n" + data.decode("utf-8", "replace")


def discover(net, slug: str, query: str = "", function: str = "", connect: bool = False, refresh: bool = False) -> dict:
    if not isinstance(connect, bool) or not isinstance(refresh, bool):
        raise NetworkError("connect and refresh must be JSON booleans")
    proj = net.project_row(slug)
    cfg, ov = net.cfg(proj), net.overview(slug)
    preferred = topics(ov)
    inventory, truncated = skills(net, slug)
    words = _words(query)
    inventory.sort(key=lambda item: (item["id"] not in preferred,
                                    -len(words & _words(item["id"] + " " + item["description"]))))
    selected = inventory[:8]
    native = cfg.get("platform") in ("windows", "linux") and cfg.get("kind") != "analysis"
    target = cfg.get("target") if cfg.get("kind") == "analysis" else {
        "path": str(Path(proj["root"]) / (cfg.get("rom", {}).get("program") or ""))} if native else None
    catalog = {"status": "not_applicable", "tools": [], "reason": "Use the project's guides, notes, drafts and similar functions."}
    if target:
        try:
            client = rea.agent_client(target, cfg.get("provider") or "", native=native, connect=connect)
            if client is None:
                catalog = {"status": "not_connected", "tools": [],
                           "reason": "Discovery did not start a cold REA import. If analysis would help, repeat with connect=true."}
            else:
                catalog = rea.agent_catalog(client, rea.NATIVE_ALLOWED if native else rea.ANALYSIS_ALLOWED, refresh)
        except (rea.ReaError, OSError) as e:
            catalog = {"status": "unavailable", "tools": [], "reason": str(e)}
    candidates = catalog.get("tools", [])
    candidates.sort(key=lambda item: (-len(words & _words(item["name"] + " " + item["description"])),
                                      item["available"] is False, item["name"]))
    catalog["total_tools"] = len(candidates)
    catalog["tools_truncated"] = len(candidates) > 8
    catalog["tools"] = candidates[:8]
    result = {"workflow": "Read relevant skills and existing findings, use a focused tool, then implement. "
              "Before repeated trials or permutation, record_assistance with what you learned and any fallback reason.",
              "skills": selected, "total_skills": len(inventory), "skills_truncated": len(inventory) > 8,
              "skill_inventory_truncated": truncated, "rea": catalog,
              "prior_knowledge": net.learnings(slug)[:3000], "intake": net.intake_line(slug),
              "next": "read_skill(id); search_notes(query); find_similar(function) for matching projects. "
              "Use advertised inputSchema, and preserve REA evidence IDs and limitations in notes."}
    if query.strip():
        result["relevant_notes"] = net.search_notes(slug, query)[:3000]
    if function:
        fn = net._function(function, slug)
        meta = json.loads(fn["meta"] or "{}")
        old = meta.get("assistance") or {}
        key = fingerprint(net, proj)
        current = function_digest(fn)
        review = old if old.get("fingerprint") == key and old.get("function_digest") == current else {}
        review = {**review, "fingerprint": key, "function_digest": current, "checked_at": time.time(),
                  "skills": [s["id"] for s in selected],
                  "rea_status": catalog["status"], "tools": [t["name"] for t in catalog["tools"]]}
        meta["assistance"] = review
        with net.store.tx() as db:
            db.execute("UPDATE functions SET meta=? WHERE id=?", (json.dumps(meta), fn["id"]))
        result["review"] = review
    return result


def record(net, agent, slug: str, function: str, summary: str, fallback_reason: str = "") -> str:
    if not summary.strip():
        raise NetworkError("summarize the guidance or evidence you checked before falling back")
    proj, fn = net.project_row(slug), net._function(function, slug)
    meta = json.loads(fn["meta"] or "{}")
    review = meta.get("assistance") or {}
    if review.get("fingerprint") != fingerprint(net, proj) or review.get("function_digest") != function_digest(fn):
        raise NetworkError("call discover_assistance(function=...) first; the knowledge review is missing or stale")
    review.update(summary=summary.strip()[:3000], fallback_reason=fallback_reason.strip()[:1000], reviewed_at=time.time())
    meta["assistance"] = review
    with net.store.tx() as db:
        db.execute("UPDATE functions SET meta=? WHERE id=?", (json.dumps(meta), fn["id"]))
    return net.add_note(agent, slug, "Knowledge review: " + review["summary"] +
                        ("\nFallback: " + review["fallback_reason"] if fallback_reason.strip() else ""), fn["name"])


def reviewed(net, proj, fn, *, permutation: bool = False) -> bool:
    review = json.loads(fn["meta"] or "{}").get("assistance") or {}
    return bool(review.get("summary") and review.get("fingerprint") == fingerprint(net, proj) and
                review.get("function_digest") == function_digest(fn) and
                (not permutation or review.get("fallback_reason")))
