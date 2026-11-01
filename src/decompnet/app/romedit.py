"""The tools that change a ROM project's shape: split, merge, rename, retire.

splat splits a ROM into functions by pattern; where it merged two functions, or missed one an agent found, the split is
fixed here and the listing rewritten, so the next link still happens at the function's own address. A rename moves a
name and not an address, so a match survives it: that is why agents can name things as they learn them. On PC targets
the same tools read and patch the program's bytes instead.
"""

from __future__ import annotations

import json
import re
import struct
import time
from pathlib import Path

from ..network import Agent, NetworkError
from ..rom import pe, split
from ..rom.symbols import ADDRESS, AUTO_NAME, IDENT, Symbol, SymbolTable
from .platform import (
    X86_PLATFORMS,
)


class RomEditMixin:
    def list_functions(self, project: str | None, status: str = "open", limit: int = 40) -> str:
        if (fork := self.fork_slug(project)):
            return self.forks.list_functions(fork, status, limit)
        proj = self._project(project)
        analysis = self.is_analysis(proj)
        where = "" if status == "all" else " AND f.status=?"
        args = [proj["id"]] + ([] if status == "all" else [status])
        rows = self.store.all(
            f"SELECT f.name, f.vram, f.n_insns, f.status, f.best_score, a.name AS claimed_by FROM functions f "
            f"LEFT JOIN claims c ON c.function_id=f.id LEFT JOIN agents a ON a.id=c.agent_id "
            f"WHERE f.project_id=?{where} ORDER BY f.vram LIMIT ?", *args, limit)
        if not rows:
            return f"No {status} {'work items' if analysis else 'functions'}."
        if analysis:
            return "\n".join(f"- {r['name']}" + (f" @0x{r['vram']:08X}" if r["vram"] else " (task)")
                             + f": {r['status']}" + (f", claimed by {r['claimed_by']}" if r["claimed_by"] else "")
                             for r in rows)
        return "\n".join(f"- {r['name']} @0x{r['vram']:08X}: {r['n_insns']} insns, {r['status']}"
                         + (f", best score {r['best_score']}" if r["best_score"] is not None else "")
                         + (f", claimed by {r['claimed_by']}" if r["claimed_by"] else "") for r in rows)

    def get_function(self, function: str, project: str | None = None, agent: Agent | None = None) -> str:
        fn = self._function(function, project)
        proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        if self.is_analysis(proj):
            return self._analysis_brief(fn, agent, "VIEW")
        return super().get_function(function, project, agent)

    def inspect_binary(self, project: str | None, function: str, what: str = "") -> str:
        """Windows targets: REA's dossier for a function (pseudocode, signature, callers), or decomp.wtf's own
        assembly and cross references when REA isn't installed. Analysis projects: the work item's REA dossier."""
        fn = self._function(function, project)
        proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        if self.is_analysis(proj):
            return self._analysis_brief(fn, None, "VIEW")
        cfg = self.cfg(proj)
        if cfg.get("platform") not in X86_PLATFORMS:
            raise NetworkError("inspect_binary is for Windows and Linux programs: use get_function for this one")
        binary = Path(proj["root"]) / cfg["rom"]["program"]
        syms = self.symbols(proj["id"])
        try:
            from .. import rea

            text = rea.inspect(binary, fn, syms, what)
        except Exception as e:  # noqa: BLE001 REA is optional: never turn a missing analyzer into a failed claim
            text = f"(REA is not available: {e})"
        x = self.xrefs(proj)
        lines = [text, "", "### decomp.wtf's view", self._asm_text(fn, proj)]
        calls = sorted(x["calls"].get(fn["vram"], ()))
        callers = sorted(x["called_by"].get(fn["vram"], ()))
        if calls:
            lines.append("calls: " + ", ".join(syms.label(a) for a in calls[:30]))
        if callers:
            lines.append("called by: " + ", ".join(syms.label(a) for a in callers[:30]))
        return "\n".join(lines)

    def search_binary(self, project: str | None, query: str) -> str:
        """Strings and procedure names in a Windows target: REA's inventories when available, else what
        decomp.wtf knows (symbol and function names). Analysis projects: REA's inventories."""
        proj = self._project(project)
        if self.is_analysis(proj):
            return self.analysis_search(proj["slug"], query)
        cfg = self.cfg(proj)
        if cfg.get("platform") not in X86_PLATFORMS:
            raise NetworkError("search_binary is for Windows and Linux programs")
        try:
            from .. import rea

            return rea.search(Path(proj["root"]) / cfg["rom"]["program"], query, self.symbols(proj["id"]))
        except Exception as e:  # noqa: BLE001 REA is optional
            syms = self.symbols(proj["id"])
            rows = [f"- symbol {s.name} at 0x{s.address:08X}" for s in syms.by_name.values() if query.lower() in s.name.lower()]
            funcs = self.store.all("SELECT name, vram FROM functions WHERE project_id=? AND name LIKE ? LIMIT 40",
                                   proj["id"], f"%{query}%")
            rows += [f"- function {r['name']} at 0x{r['vram']:08X}" for r in funcs]
            return (f"(REA is not available: {e})\n" + "\n".join(rows[:60])) if rows else \
                f"(REA is not available: {e}) decomp.wtf knows no symbol matching {query!r}."

    def read_data(self, project: str | None, what: str, length: int = 64) -> str:
        proj = self._project(project)
        if self.is_analysis(proj):
            return self.analysis_read_data(proj["slug"], what, length)
        self._rom_only(project, "read_data")
        syms = self.symbols(proj["id"])
        m = ADDRESS.match(what.strip())
        addr = int(m.group(1), 16) if m else syms.resolve(what.strip())
        if addr is None:
            raise NetworkError(f"`{what}` is not an address or a known symbol")
        length = max(4, min(int(length), 512))
        data = self.reader(proj)(addr, length)
        head = f"{syms.label(addr)} at 0x{addr:08X}"
        if data is None:
            sym = syms.by_addr.get(addr)
            if sym and sym.kind == "bss":
                return f"{head}: uninitialized data (.bss), {sym.size} bytes; it starts as zeros at boot."
            return f"{head}: not in a loaded part of the ROM (maybe .bss, or a segment that isn't split yet)."
        out = [head]
        for i in range(0, len(data), 16):
            chunk = data[i:i + 16]
            text = "".join(chr(c) if 32 <= c < 127 else "." for c in chunk)
            out.append(f"  {addr + i:08X}: {chunk.hex(' '):<47}  {text}")
        e = ">" if self.cfg(proj).get("big_endian", True) else "<"
        words = [w for (w,) in struct.iter_unpack(e + "I", data[:len(data) // 4 * 4])]
        ptrs = [f"+0x{4 * i:X}: {syms.label(w)}" for i, w in enumerate(words) if 0x80000400 <= w < 0x80800000]
        if ptrs:
            out.append("words that look like addresses: " + ", ".join(ptrs[:16]))
        floats = [struct.unpack(e + "f", struct.pack(e + "I", w))[0] for w in words[:8]]
        if any(1e-6 < abs(f) < 1e7 for f in floats):
            out.append("as f32: " + ", ".join(f"{f:g}" for f in floats))
        return "\n".join(out)

    def split_function(self, agent: Agent, project: str | None, function: str, at: str, new_name: str = "") -> str:
        self._rom_only(project, "split_function")
        link = self._link(project)
        if link is not None:
            name = self._function(function, project)["name"]
            text = link.call("/collab/split", agent, function=name, at=at, new_name=new_name)
            if text.startswith("✅"):
                link.sync()
            return text
        fn = self._function(function, project)
        proj = self.store.one("SELECT * FROM projects WHERE id=?", fn["project_id"])
        if not self.is_rom(proj):
            raise NetworkError("split_function is for ROM projects")
        if self.cfg(proj).get("platform") in X86_PLATFORMS:
            raise NetworkError("split_function isn't needed for PC programs: function boundaries come from the "
                               "executable (.pdata or .eh_frame, its symbol table and call targets)")
        if fn["status"] == "matched":
            raise NetworkError(f"{fn['name']} is matched; it can't be split")
        m = ADDRESS.match(str(at).strip())
        if not m:
            raise NetworkError("`at` must be an address like 0x80001234")
        addr = int(m.group(1), 16)
        new_name = (new_name or f"func_{addr:08X}").strip()
        syms = self.symbols(proj["id"])
        if not IDENT.match(new_name) or new_name in syms.by_name:
            raise NetworkError(f"`{new_name}` is not a free C identifier")
        if self.cfg(proj).get("platform") == "gba":
            return self._split_gba(agent, proj, fn, addr, new_name, syms)
        rf = self._parse_asm(fn["name"], fn["asm"], fn["unit"])
        try:
            head, tail = split.split_function(rf, addr, new_name)
        except ValueError as e:
            raise NetworkError(str(e)) from None
        syms.add(Symbol(new_name, addr, tail.size, "func"))
        old = syms.by_name.get(fn["name"])
        if old is not None:
            old.size = head.size
        th, tt = self._make_target(proj, head, syms), self._make_target(proj, tail, syms)
        now = time.time()
        with self.store.tx() as db:
            db.execute("UPDATE functions SET size=?, n_insns=?, target=?, relocs=?, asm=?, seed=NULL, best_score=NULL, "
                       "changed_at=? WHERE id=?", (head.size, len(head.lines), th.code, json.dumps(th.to_json()),
                                                    self._asm_source(head), now, fn["id"]))
            db.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,seed,unit,vram,rom_offset,"
                       "changed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (proj["id"], new_name, tail.size, len(tail.lines), tt.code, json.dumps(tt.to_json()),
                        self._asm_source(tail), None, fn["unit"], addr, tail.rom, now))
            db.execute("UPDATE symbols SET size=?, changed_at=? WHERE project_id=? AND name=?",
                       (head.size, now, proj["id"], fn["name"]))
            db.execute("INSERT OR REPLACE INTO symbols(project_id,name,address,size,kind,changed_at) "
                       "VALUES(?,?,?,?,?,?)", (proj["id"], new_name, addr, tail.size, "func", now))
            self._event(db, proj["id"], agent.id, "split", f"{fn['name']} / {new_name}")
        self._xrefs.pop(proj["id"], None)
        return (f"✅ split: {fn['name']} is now {len(head.lines)} instructions; {new_name} at 0x{addr:08X} has "
                f"{len(tail.lines)}. Continue with {fn['name']} (get_function shows its new assembly).")

    def _last_mnemonic(self, proj, fn) -> str:
        md = pe._md(self.cfg(proj).get("arch", "x86"))
        md.detail = True
        md.skipdata = True
        last = ""
        for ins in md.disasm(fn["target"], 0):
            if ins.id:
                last = ins.mnemonic
        return last

    def _merge_rows(self, agent: Agent, proj, prev, frag) -> int:
        """Make `frag` (the contiguous piece right after `prev`) part of `prev`. Returns the new byte size."""
        cfg = self.cfg(proj)
        code = bytes(prev["target"]) + bytes(frag["target"])
        lo = min(s["vram"] for s in cfg["segments"])
        hi = max(s["vram"] + s["rom_end"] - s["rom_start"] for s in cfg["segments"])
        md = pe._md(cfg.get("arch", "x86"))
        md.detail = True
        md.skipdata = True
        lines, count, refs = pe._decode(code, prev["vram"], cfg.get("arch", "x86"), lo, hi, False, md)
        asm = "\n".join([f"glabel {prev['name']}", *lines, f"endlabel {prev['name']}"])
        now = time.time()
        with self.store.tx() as db:
            # The fragment's notes are about the same bytes: keep them on the function that now owns them.
            db.execute("UPDATE notes SET function_id=? WHERE function_id=?", (prev["id"], frag["id"]))
            db.execute("DELETE FROM attempts WHERE function_id=?", (frag["id"],))
            db.execute("DELETE FROM claims WHERE function_id=?", (frag["id"],))
            db.execute("DELETE FROM functions WHERE id=?", (frag["id"],))
            db.execute("UPDATE functions SET size=?, n_insns=?, target=?, relocs=?, asm=?, seed=NULL, best_score=NULL, "
                       "best_source=NULL, best_agent_id=NULL, changed_at=? WHERE id=?",
                       (len(code), count, code, json.dumps([[r[0], r[1], r[2], r[3]] for r in refs]), asm, now,
                        prev["id"]))
            db.execute("UPDATE symbols SET size=?, changed_at=? WHERE project_id=? AND address=?",
                       (len(code), now, proj["id"], prev["vram"]))
            db.execute("INSERT OR REPLACE INTO symbols(project_id,name,address,size,kind,changed_at) "
                       "VALUES(?,?,?,?,?,?)", (proj["id"], prev["name"], prev["vram"], len(code), "func", now))
            db.execute("DELETE FROM symbols WHERE project_id=? AND name=?", (proj["id"], frag["name"]))
            self._event(db, proj["id"], agent.id, "merge", f"{frag['name']} → {prev['name']}")
        self._xrefs.pop(proj["id"], None)
        self._ops.pop(proj["id"], None)
        return len(code)

    def merge_function(self, agent: Agent, project: str | None, function: str, into: str = "") -> str:
        """Put a fragment back into the function it belongs to (the reverse of split_function)."""
        self._rom_only(project, "merge_function")
        proj = self._project(project)
        if self.cfg(proj).get("platform") not in X86_PLATFORMS:
            raise NetworkError("merge_function is for PC programs: their 32-bit function boundaries are found by "
                               "following the code, and an old import could have cut one into pieces")
        fn = self._function(function, project)
        prev = self._function(into, project) if into else self.store.one(
            "SELECT * FROM functions WHERE project_id=? AND unit=? AND vram<? ORDER BY vram DESC LIMIT 1",
            (proj["id"], fn["unit"], fn["vram"]))
        if prev is None:
            raise NetworkError(f"there is no function before {fn['name']} to merge it into")
        if prev["id"] == fn["id"]:
            raise NetworkError("a function can't be merged into itself")
        if prev["unit"] != fn["unit"] or (prev["vram"] or 0) + (prev["size"] or 0) != fn["vram"]:
            raise NetworkError(f"{fn['name']} doesn't directly follow {prev['name']} in the same section: "
                               f"pass into= the function it is a piece of")
        for row in (prev, fn):
            if row["status"] == "matched":
                raise NetworkError(f"{row['name']} is matched; merging would move a verified boundary")
            if row["status"] != "open":
                raise NetworkError(f"{row['name']} is {row['status']}, not open")
        held = self.store.one("SELECT agent_id FROM claims WHERE function_id=?", fn["id"])
        if held is not None and held["agent_id"] != agent.id:
            raise NetworkError(f"{fn['name']} is claimed by {self._agent_name(held['agent_id'])}")
        size = self._merge_rows(agent, proj, prev, fn)
        return (f"✅ merged: {fn['name']} is now part of {prev['name']} ({len(bytes(prev['target']))} + "
                f"{len(bytes(fn['target']))} = {size} bytes, {prev['n_insns'] or 0} + {fn['n_insns'] or 0} "
                f"instructions). Claim {prev['name']} to decompile the whole thing.")

    def retire_function(self, agent: Agent, project: str | None, function: str, reason: str) -> str:
        """Take a function that no C can match out of the queue for good (with the evidence why)."""
        self._rom_only(project, "retire_function")
        fn = self._function(function, project)
        if fn["status"] == "matched":
            raise NetworkError(f"{fn['name']} is matched; there is nothing to retire")
        if fn["status"] == "unmatchable":
            return f"{fn['name']} is already retired."
        reason = (reason or "").strip()
        if len(reason) < 10:
            raise NetworkError("say why in a sentence (what you tried, and what in the target can't be produced): "
                               "the next agent reads it instead of trying again")
        held = self.store.one("SELECT agent_id FROM claims WHERE function_id=?", fn["id"])
        if held is not None and held["agent_id"] != agent.id:
            raise NetworkError(f"{fn['name']} is claimed by {self._agent_name(held['agent_id'])}")
        meta = json.loads(fn["meta"] or "{}")
        meta["unmatchable"] = reason
        with self.store.tx() as db:
            db.execute("UPDATE functions SET status='unmatchable', meta=?, changed_at=? WHERE id=?",
                       (json.dumps(meta), time.time(), fn["id"]))
            db.execute("DELETE FROM claims WHERE function_id=?", (fn["id"],))
            db.execute("INSERT INTO notes(project_id,function_id,agent_id,text,created_at) VALUES(?,?,?,?,?)",
                       (fn["project_id"], fn["id"], agent.id, f"retired as unmatchable: {reason}", time.time()))
            self._event(db, fn["project_id"], agent.id, "retire", fn["name"])
        return (f"✅ {fn['name']} is retired as unmatchable and won't come back to the queue. The reason is in its "
                f"notes. Claim the next function.")

    def repair_boundaries(self, slug: str) -> dict:
        """Merge the fragments an older 32-bit split left behind: a function that doesn't end in a terminator has
        its next contiguous open, automatically named function folded back into it. Matched code is never touched."""
        proj = self._project(slug)
        if self.fork_slug(slug):
            raise NetworkError("a fork builds from its own source: its functions come from the project")
        if self.cfg(proj).get("platform") not in X86_PLATFORMS:
            raise NetworkError("boundary repair is for PC programs (32-bit PE images imported before the splitter "
                               "followed branches)")
        agent, _ = self.local_agent("boundaries")
        merged = 0
        rows = [dict(r) for r in self.store.all("SELECT * FROM functions WHERE project_id=? AND status='open' "
                                                "ORDER BY unit, vram", proj["id"])]
        i = 0
        while i < len(rows) - 1 and merged < 4096:
            prev, frag = rows[i], rows[i + 1]
            if prev["unit"] != frag["unit"] or (prev["vram"] or 0) + (prev["size"] or 0) != frag["vram"]:
                i += 1
                continue
            if not AUTO_NAME.match(frag["name"]):  # a real symbol is not a fragment
                i += 1
                continue
            last = self._last_mnemonic(proj, prev)
            if last and last not in pe.TERMINATORS and not last.startswith("j"):
                self._merge_rows(agent, proj, prev, frag)
                merged += 1
                new_target = bytes(prev["target"]) + bytes(frag["target"])
                rows[i] = dict(prev) | {"target": new_target, "size": len(new_target)}
                rows.pop(i + 1)
            else:
                i += 1
        open_ = self.store.one("SELECT COUNT(*) AS n FROM functions WHERE project_id=? AND status='open'",
                               proj["id"])["n"]
        return {"merged": merged, "open": open_}

    def _split_gba(self, agent: Agent, proj, fn, addr: int, new_name: str, syms: SymbolTable) -> str:
        """Cut a GBA function's bytes and assembly in two at `addr` (Thumb: a halfword boundary)."""
        off = addr - fn["vram"]
        if off >= fn["size"] and off % 2 == 0:  # code after the function that nobody found yet: make it a function
            return self._add_gba_function(agent, proj, fn, addr, new_name, syms)
        if off <= 0 or off >= fn["size"] or off % 2:
            raise NetworkError(f"0x{addr:08X} isn't inside {fn['name']} (0x{fn['vram']:08X}, {fn['size']} bytes)")
        head, tail = fn["target"][:off], fn["target"][off:]
        cut = re.search(rf"^(?:_|\.L){addr:08X}:|^\t(?:thumb|arm)_func_start \w*{addr:08X}\b", fn["asm"], re.MULTILINE)
        asm_head, asm_tail = ((fn["asm"][:cut.start()], fn["asm"][cut.start():]) if cut else
                              (fn["asm"], f"\tthumb_func_start {new_name}\n@ split from {fn['name']}: see its assembly\n"))
        refs = json.loads(fn["relocs"])
        refs_head = [r for r in refs if r[0] < off]
        refs_tail = [[r[0] - off, r[1], r[2]] for r in refs if r[0] >= off]
        syms.add(Symbol(new_name, addr, len(tail), "func"))
        now = time.time()
        with self.store.tx() as db:
            db.execute("UPDATE functions SET size=?, n_insns=?, target=?, relocs=?, asm=?, seed=NULL, best_score=NULL, "
                       "changed_at=? WHERE id=?", (len(head), max(1, len(head) // 2), head, json.dumps(refs_head), asm_head,
                                                    now, fn["id"]))
            db.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,seed,unit,vram,rom_offset,"
                       "changed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (proj["id"], new_name, len(tail), max(1, len(tail) // 2), tail, json.dumps(refs_tail), asm_tail,
                        None, fn["unit"], addr, fn["rom_offset"] + off, now))
            db.execute("UPDATE symbols SET size=?, changed_at=? WHERE project_id=? AND name=?",
                       (len(head), now, proj["id"], fn["name"]))
            db.execute("INSERT OR REPLACE INTO symbols(project_id,name,address,size,kind,changed_at) "
                       "VALUES(?,?,?,?,?,?)", (proj["id"], new_name, addr, len(tail), "func", now))
            self._event(db, proj["id"], agent.id, "split", f"{fn['name']} / {new_name}")
        self._xrefs.pop(proj["id"], None)
        self._ops.pop(proj["id"], None)
        return (f"✅ split: {fn['name']} is now {len(head)} bytes; {new_name} at 0x{addr:08X} has {len(tail)}. "
                f"Continue with {fn['name']} (get_function shows its new assembly).")

    def _add_gba_function(self, agent: Agent, proj, prev, addr: int, new_name: str, syms: SymbolTable) -> str:
        from ..rom import gba, gba_candidates

        nxt = self.store.one("SELECT vram FROM functions WHERE project_id=? AND vram>? ORDER BY vram LIMIT 1",
                             proj["id"], prev["vram"])
        rom, _ = self._rom(proj)
        off, limit = addr - gba.BASE, (nxt["vram"] - gba.BASE) if nxt else len(rom)
        if not off < limit or not gba_candidates.plausible_function(rom, off):
            raise NetworkError(f"0x{addr:08X} doesn't look like the start of a function")
        literals = gba_candidates.literal_words(rom, [addr])
        end = min(next(gba_candidates.bodies(rom, [addr], literals))[1], limit)
        while end + 4 <= limit and end in literals:
            end += 4
        code = rom[off:end]
        syms.add(Symbol(new_name, addr, len(code), "func"))
        now = time.time()
        with self.store.tx() as db:
            db.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,seed,unit,vram,rom_offset,"
                       "changed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (proj["id"], new_name, len(code), max(1, len(code) // 2), code,
                        json.dumps(gba._refs(code, addr, literals, off)),
                        f"\tthumb_func_start {new_name}\n@ found by an agent after {prev['name']}\n",
                        None, prev["unit"], addr, off, now))
            db.execute("INSERT OR REPLACE INTO symbols(project_id,name,address,size,kind,changed_at) "
                       "VALUES(?,?,?,?,?,?)", (proj["id"], new_name, addr, len(code), "func", now))
            self._event(db, proj["id"], agent.id, "split", f"{prev['name']} / {new_name}")
        self._xrefs.pop(proj["id"], None)
        self._ops.pop(proj["id"], None)
        return f"✅ new function {new_name} at 0x{addr:08X}, {len(code)} bytes."

    def rename_symbol(self, agent: Agent, project: str | None, old: str, new: str, message: str = "") -> str:
        self._rom_only(project, "rename_symbol")
        proj = self._project(project)
        if not self.is_rom(proj):
            return super().rename_symbol(agent, project, old, new, message)
        link = self._link(project)
        if link is not None:
            text = link.call("/collab/rename", agent, old=old, new=new, message=message)
            if text.startswith("✅"):
                link.sync()
            return text
        old, new = old.strip(), new.strip()
        if not (IDENT.match(old) and IDENT.match(new)):
            raise NetworkError("old and new must be C identifiers")
        syms = self.symbols(proj["id"])
        addr = syms.resolve(old)
        if addr is None:
            raise NetworkError(f"`{old}` is not a symbol of this ROM: use a name from the assembly (func_/D_...) "
                               f"or one given earlier")
        taken = syms.resolve(new)
        if taken is not None and taken != addr:
            raise NetworkError(f"`{new}` already names 0x{taken:08X}")
        word = re.compile(rf"\b{re.escape(old)}\b")
        trial = SymbolTable([Symbol(s.name, s.address, s.size, s.kind) for s in syms.by_name.values()])
        if old in trial.by_name:
            trial.rename(old, new)
        else:
            fnrow = self.store.one("SELECT size FROM functions WHERE project_id=? AND vram=?", proj["id"], addr)
            current, _ = syms.name_at(addr)
            base = syms.by_name.get(current) if current != f"D_{addr:08X}" else None
            trial.add(Symbol(new, addr, fnrow["size"] if fnrow else (base.size if base and base.address == addr else 0),
                             "func" if fnrow else (base.kind if base else "data")))
        units = self._unit_contexts(proj["id"])
        current_ctx = {u: self._context(proj["id"], u) for u in ({""} | units)}
        changed = {u: word.sub(new, c["header"]) for u, c in current_ctx.items() if word.search(c["header"])}
        matched = self.store.all("SELECT * FROM functions WHERE project_id=? AND status='matched'", proj["id"])
        rewritten = {r["id"]: word.sub(new, r["matched_source"]) for r in matched}
        renamed_rows = [dict(r) | {"name": new if r["name"] == old else r["name"]} for r in matched]
        def header_for(fn):
            return changed.get(self._effective_unit(fn, units),
                               current_ctx[self._effective_unit(fn, units)]["header"])
        broken = self.regression_test(proj, None, sources=rewritten, only=renamed_rows, header_for=header_for, symbols=trial)
        if broken:
            listing = "\n".join(f"- {n}: {why}" for n, why in broken[:10])
            return f"❌ REJECTED: renaming breaks {len(broken)} matched function(s).\n{listing}"
        msg = message.strip() or f"rename {old} -> {new}"
        now = time.time()
        with self.store.tx() as db:
            if db.execute("UPDATE symbols SET name=?, changed_at=? WHERE project_id=? AND name=?",
                          (new, now, proj["id"], old)).rowcount == 0:
                s = trial.by_name[new]
                db.execute("INSERT OR REPLACE INTO symbols(project_id,name,address,size,kind,changed_at) "
                           "VALUES(?,?,?,?,?,?)", (proj["id"], new, s.address, s.size, s.kind, now))
            db.execute("UPDATE functions SET name=?, changed_at=? WHERE project_id=? AND (name=? OR vram=?)",
                       (new, now, proj["id"], old, addr))
            if self.cfg(proj).get("platform") in X86_PLATFORMS:  # its listing was generated once, with the old label
                db.execute("UPDATE functions SET asm=replace(replace(asm, ?, ?), ?, ?) WHERE project_id=? AND vram=?",
                           (f"glabel {old}", f"glabel {new}", f"endlabel {old}", f"endlabel {new}", proj["id"], addr))
            for u, header in changed.items():
                version = self._next_version(db, proj["id"])
                db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                           "VALUES(?,?,?,?,?,?,?)", (proj["id"], version, header, agent.id, msg, time.time(), u))
            for fid, src in rewritten.items():
                db.execute("UPDATE functions SET matched_source=?, best_source=? WHERE id=?", (src, src, fid))
            for r in db.execute("SELECT id, best_source FROM functions WHERE project_id=? AND status='open' "
                                "AND best_source IS NOT NULL", (proj["id"],)).fetchall():
                db.execute("UPDATE functions SET best_source=? WHERE id=?", (word.sub(new, r["best_source"]), r["id"]))
            self._event(db, proj["id"], agent.id, "rename", f"{old} → {new}")
        self._syms.pop(proj["id"], None)
        return f"✅ 0x{addr:08X} is now `{new}` (was `{old}`); {len(rewritten)} matched sources updated, all still match."
