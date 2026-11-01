"""What REA knows about a function, kept where the agents read it.

Ghidra's reading of a function - its signature, the convention it was built with, what it calls, the strings and globals
it names - says more than the split alone can, and it is slow to ask for. So it is fetched once per function and kept in
that function's meta; the brief, the `type_hint` tool, the mismatch hints and search all read the stored copy.

Nothing here blocks a claim: a function without facts gets a brief without them, and `type_hint` fetches on demand. Every
line REA contributes is labelled as its reading, because it is inference over a decompiler's output, not proof.
"""

# pyright: reportAttributeAccessIssue=false
# A type checker reads one part of RomNetwork at a time, so a helper owned by another part looks unknown here. The class is
# assembled at runtime and tests/test_engine_parts.py checks that every name resolves.

from __future__ import annotations

import json
import time
from pathlib import Path

from .. import rea
from ..network import NetworkError
from ..rom.symbols import AUTO_NAME
from .platform import X86_PLATFORMS

# REA names that say nothing a page can use: its placeholder for a function it could not name. (No empty string here:
# every name starts with "" and that would skip them all.)
_PLACEHOLDER = ("FUN_", "thunk_FUN_", "sub_", "<EXTERNAL>")
# Names the split or Ghidra generates: a function called one of these has no name yet.
_GENERATED = ("func_", "FUN_", "sub_", "thunk_FUN_", "fn_", "unknown_")


def _meta(row) -> dict:
    """A function's meta as a dict: text this database wrote, with a default instead of an exception."""
    try:
        value = json.loads(row["meta"] or "{}")
    except (ValueError, KeyError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


class ReaFactsMixin:
    # ------------------------------------------------------------------ the file, and one function's facts

    def rea_file(self, proj) -> Path | None:
        """The program REA would open for this project, when it has one and REA is set up."""
        if self.is_analysis(proj) or self.fork_slug(proj["slug"]):
            return None
        cfg = self.cfg(proj)
        if cfg.get("platform") not in X86_PLATFORMS or rea.command() is None:
            return None
        name = (cfg.get("rom") or {}).get("program") or (cfg.get("rom") or {}).get("file")
        path = Path(proj["root"]) / name if name else None
        return path if path is not None and path.is_file() else None

    def rea_facts(self, slug: str, function: str, *, refresh: bool = False, timeout: float = 300) -> dict | None:
        """REA's reading of one function, from its meta when it is there (one Ghidra call per function, ever)."""
        proj = self._project(slug)
        fn = self._function(function, slug)
        stored = _meta(fn).get("rea")
        if not refresh and isinstance(stored, dict) and (stored.get("signature") or stored.get("callees")):
            return stored
        target = self.rea_file(proj)
        if target is None:
            return None
        base = int((self.cfg(proj).get("rea") or {}).get("base") or 0)  # REA's image base, from the import or re-check
        try:
            facts = rea.dossier(target, fn, timeout=timeout, base=base)
        except rea.ReaError as e:  # keep why, so type_hint explains itself instead of saying "nothing"
            with self.store.tx() as db:
                fresh = db.execute("SELECT meta FROM functions WHERE id=?", (fn["id"],)).fetchone()
                current = _meta(fresh) if fresh is not None else {}
                current["rea"] = {"error": str(e)[:300], "at": time.time()}
                db.execute("UPDATE functions SET meta=? WHERE id=?", (json.dumps(current), fn["id"]))
            return None
        if facts is None:
            return None
        with self.store.tx() as db:
            fresh = db.execute("SELECT meta FROM functions WHERE id=?", (fn["id"],)).fetchone()
            current = _meta(fresh) if fresh is not None else {}
            current["rea"] = facts
            db.execute("UPDATE functions SET meta=? WHERE id=?", (json.dumps(current), fn["id"]))
        return facts

    def rea_warm(self, slug: str, limit: int = 25, log=print) -> int:
        """Fetch facts for the open functions that don't have them yet, smallest first (agents start there).

        Bounded on purpose: this runs during an import or a re-check, not while an agent waits."""
        proj = self._project(slug)
        if self.rea_file(proj) is None:
            return 0
        rows = self.store.all(
            "SELECT name, meta FROM functions WHERE project_id=? AND status='open' ORDER BY n_insns ASC, vram ASC", proj["id"]
        )
        done = 0
        for row in rows:
            if isinstance(_meta(row).get("rea"), dict):
                continue
            if done >= limit:
                break
            if self.rea_facts(slug, row["name"], timeout=120) is not None:
                done += 1
        if done:
            log(f"REA: read {done} function(s) in detail (signature, callees, references)")
        return done

    # ------------------------------------------------------------------ names REA resolved, and the strings it names

    def rea_name(self, proj, address: int) -> str:
        """The best name for an address: the project's own, else one REA resolved, else nothing.

        A project's name wins even when it is an automatic one: renaming is the project's decision, and the proposals in
        the config are only used where nothing better exists."""
        name, _ = self.symbols(proj["id"]).name_at(address)
        if name and not AUTO_NAME.match(name):
            return name
        proposal = (self.cfg(proj).get("rea") or {}).get("names") or {}
        return str(proposal.get(f"0x{address:X}", "")) or ""

    def adopt_rea_names(self, agent, slug: str) -> str:
        """Rename the functions that have only an automatic name to the name REA resolved, one commit at a time."""
        proj = self._project(slug)
        proposals = (self.cfg(proj) or {}).get("rea", {}).get("names") or {}
        if not proposals:
            return "REA hasn't resolved any names for this project yet: run Re-check first."
        rows = {
            int(r["vram"]): r["name"]
            for r in self.store.all("SELECT name, vram FROM functions WHERE project_id=? AND vram IS NOT NULL", proj["id"])
        }
        renamed, skipped = 0, []
        for hex_address, new in sorted(proposals.items(), key=lambda kv: int(kv[0], 16)):
            try:
                address = int(hex_address, 16)
            except ValueError:
                continue
            old = rows.get(address)
            generated = bool(old) and (AUTO_NAME.match(old) or old.startswith(_GENERATED))
            if not generated or not str(new).strip() or str(new).startswith(_PLACEHOLDER):
                continue
            try:
                self.rename_symbol(agent, slug, old, new, "named from REA")
                renamed += 1
            except NetworkError as e:  # a name already taken, a claimed function: skip and say so
                skipped.append(f"{old} → {new}: {e}")
        if renamed:
            lines = [f"Renamed {renamed} function(s) to the names REA resolved."]
        elif skipped:
            lines = ["No function was renamed: their names are references the project's code already uses.", ""]
        else:
            lines = ["Nothing to rename: no function here has only an automatic name and a REA name."]
        lines += [*skipped[:10]] if skipped else []
        lines += ["", "The source tree keeps its names: this only changes what agents and the page call them."]
        return "\n".join(lines)

    def rea_brief_lines(self, proj, fn) -> list[str]:
        """The REA block of a function's brief, plus the strings and globals its xrefs reach. Never calls Ghidra."""
        lines: list[str] = []
        touches = self._touches(proj, fn)
        if touches:
            lines += ["", "### Strings and globals it reaches", ", ".join(touches[:12])]
        facts = _meta(fn).get("rea")
        if isinstance(facts, dict) and facts.get("error"):
            return [*lines, "", f"REA couldn't read this function: {facts['error']}"]
        if not isinstance(facts, dict):
            if self.rea_file(proj) is not None:
                lines += ["", "REA has read nothing here yet: `type_hint(function)` fetches its signature and callees."]
            return lines
        bits = [f"signature: `{facts['signature']}`"] if facts.get("signature") else []
        if facts.get("calling_convention"):
            bits.append(f"convention: {facts['calling_convention']}")
        lines += ["", "### REA says (Ghidra; its reading, not proof)", *bits]
        if facts.get("callees"):
            lines.append("calls: " + ", ".join(facts["callees"][:12]))
        if facts.get("strings"):
            lines.append("names: " + ", ".join(facts["strings"][:10]))
        return lines

    def _touches(self, proj, fn) -> list[str]:
        """The named strings and globals this function's code reaches, from the xrefs the project already has."""
        if not fn["vram"]:
            return []
        syms = self.symbols(proj["id"])
        used = sorted((self.xrefs(proj).get("uses") or {}).get(fn["vram"]) or ())
        named = {
            r["name"]: r
            for r in self.store.all(
                "SELECT name, kind, preview FROM symbols WHERE project_id=? AND kind IN ('string', 'data', 'jtbl', 'bss')", proj["id"]
            )
        }
        out: list[str] = []
        for address in used[:40]:
            label = syms.label(address)
            row = named.get(syms.name_at(address)[0])
            if row is None:  # not a named string or global: the label alone says nothing an agent can use
                continue
            if row["preview"]:
                label = f"{label} ({str(row['preview'])[:40]})"
            if label not in out:
                out.append(label)
        return out
