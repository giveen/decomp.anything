"""Making a fork: cloning a public decomp, starting one from a game, and keeping it in step.

A fork is a real project with its own git tree; `setup` gives it the user's game so its build can be compared against the
original, and `sync` walks the units objdiff measured and records what matched. Every change to the tree happens under
the engine's locks and is committed one file at a time (see forkengine.py)."""

# pyright: reportAttributeAccessIssue=false
# A type checker reads one part at a time, so a self.<name> owned by another part (or by the engine itself) looks
# unknown. ForkEngine is one class at runtime; tests/test_fork_parts.py proves every name resolves there.


from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
from collections import defaultdict
from collections.abc import Collection
from pathlib import Path

from ..forks import catalog, scratch_dsd, scratch_dtk
from ..forks.objdiff import ObjdiffError
from ..forks.project import STATE, ForkError, ForkProject
from ..network import Agent, NetworkError
from .forkutil import (
    ASM_KEYS,
    REPO,
    _slug,
    scaled,
)


class ForkCreateMixin:
    def policy(self, repo: str) -> dict:
        """What a public decomp says about AI and contributions (forks/policy.py), before it's forked."""
        from ..forks import policy

        repo = repo.strip()
        m = REPO.match(repo)
        if not m:
            local = Path(repo).expanduser()
            if local.is_dir() and (local / ".git").exists():  # a checkout on this computer
                return {"repo": str(local.resolve()), **policy.check(str(local.resolve()))}
            raise NetworkError("give a GitHub/GitLab/Codeberg repository URL (https://github.com/owner/repo) "
                               "or the path of a git checkout on this computer")
        return {"repo": m.group(1), **policy.check(m.group(1))}

    def clone(self, repo: str, *, name: str = "", platform: str = "", version: str | None = None, log=print) -> str:
        repo = repo.strip()
        local = Path(repo).expanduser()
        if not (REPO.match(repo) or (local.is_dir() and (local / ".git").exists())):
            raise NetworkError("give a GitHub/GitLab/Codeberg repository URL (https://github.com/owner/repo)")
        if not shutil.which("git"):
            raise NetworkError("forking needs git: install it from https://git-scm.com")
        hit = REPO.match(repo)
        url = hit.group(1) if hit else str(local.resolve())  # a remote's own address, or this folder as git sees it
        for p in self.net.store.all("SELECT slug, config FROM projects"):
            if json.loads(p["config"]).get("repo_url") == url:
                log(f"already forked as {p['slug']}")
                return p["slug"]
        base = _slug(name or url.rstrip("/").split("/")[-1])
        slug = self._free_slug(base)
        dest = self.net.workspace / slug
        log(f"cloning {url}")
        res = subprocess.run(["git", "clone", "--depth", "1", "--recurse-submodules", "--shallow-submodules", url, str(dest)],
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        if res.returncode != 0:
            shutil.rmtree(dest, ignore_errors=True)
            raise NetworkError(f"git clone failed: {res.stderr.strip()[-400:]}")
        fp = ForkProject(dest)
        fp.git("checkout", "-q", "-b", "decomp-wtf")
        kind = fp.kind()
        versions = fp.versions()
        version = version if version in versions else (versions[0] if versions else version)
        cfg = {"kind": "fork", "repo_url": url, "platform": platform, "version": version, "fork_kind": kind,
               "ready": False, "legal": {"basis": "personal-copy", "license": "the project's own license"}}
        self._register(slug, name or url.rstrip("/").split("/")[-1], f"fork of {url}", dest, cfg, f"cloned {url}")
        log(f"cloned into {dest} ({kind} project)")
        return slug

    def _free_slug(self, base: str) -> str:
        slug, n = base, 2
        while self.net.store.one("SELECT 1 FROM projects WHERE slug=?", slug) or (self.net.workspace / slug).exists():
            slug, n = f"{base}-{n}", n + 1
        return slug

    def _register(self, slug: str, title: str, description: str, root: Path, cfg: dict, event: str) -> None:
        with self.net.store.tx() as db:
            pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                             (slug, title, description, "", 0, str(root), json.dumps(cfg))).lastrowid
            db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                       "VALUES(?,?,?,?,?,?,?)", (pid, 1, "", None, "projects keep their headers in the tree", time.time(), ""))
            self.net._event(db, pid, None, "fork", event)

    def create_scratch(self, game: Path, *, compiler: str | None = None, log=print) -> str:
        """A GameCube, Wii or DS game, from scratch: generate the community's project layout for it (dtk-template,
        ds-decomp) and open it like a fork."""
        with open(game, "rb") as f:
            ds = scratch_dsd.is_nds(f.read(0x200))
        maker = scratch_dsd if ds else scratch_dtk
        info = maker.game_info(game)
        sha1 = info["sha1"] if ds else hashlib.sha1(info["dol"]).hexdigest()
        for p in self.net.store.all("SELECT slug, config FROM projects"):
            if json.loads(p["config"]).get("program_sha1") == sha1:
                log(f"already set up as {p['slug']}")
                return p["slug"]
        slug = self._free_slug(_slug(f"{info['title']} {info['id']}" if info.get("kind") != "dol" else info["title"]))
        dest = self.net.workspace / slug
        try:
            facts = maker.create(dest, game, compiler=compiler, log=log)
        except BaseException:
            shutil.rmtree(dest, ignore_errors=True)
            raise
        cfg = {"kind": "fork", "origin": "scratch", "repo_url": None, "platform": facts["platform"],
               "version": facts["id"], "fork_kind": "manifest", "ready": False, "compiler": facts["compiler"],
               "program_sha1": sha1,
               "legal": {"basis": "personal-copy", "license": "personal use only",
                         "statement": "imported by the owner of this copy of the game, who confirmed it was legally obtained",
                         "confirmed_at": time.time()}}
        self._register(slug, facts["title"], f"{catalog.PLATFORMS[facts['platform']]} game {facts['id']}, from scratch",
                       dest, cfg, f"started {facts['id']} from scratch")
        self.check(slug, log=log)
        return slug

    def needs(self, slug: str) -> dict:
        _, fp, cfg = self.get(slug)
        return fp.needs(cfg.get("version")) | {"kind": fp.kind(), "versions": fp.versions(),
                                                "version": cfg.get("version"), "readme": fp.readme()}

    def setup(self, slug: str, game: bytes | Path | None, filename: str = "", log=print, move: bool = False) -> dict:
        """Add the user's game (bytes, or a file on disk) and run the project's setup, then measure it."""
        proj, fp, cfg = self.get(slug)
        if game is not None:
            if isinstance(game, (bytes, bytearray)):  # the browser's upload: give it a path like the disk's has
                tmp = fp.root / STATE / "upload.tmp"
                tmp.write_bytes(game)
                src, move = tmp, True
            else:
                src, move = game, False
            self._match_version(proj, fp, cfg, src, log)
            if not cfg.get("program_sha1"):  # which game it is: members of a room on it need the same one
                from .forkroom import game_identity

                try:
                    cfg["game_sha1"] = game_identity(src)
                    self._save_cfg(proj, cfg)
                except (OSError, ForkError):
                    pass
            dest = fp.place_game(src, filename or src.name, cfg.get("version"), move=move)
            log(f"your copy of the game is at {dest.relative_to(fp.root)}")
        if not fp.needs(cfg.get("version"))["auto"]:
            raise NetworkError("this project has its own setup: follow its README, then click Check again")
        fp.setup(cfg.get("version"), log=log)
        return self.check(slug, log=log)

    def _match_version(self, proj, fp: ForkProject, cfg: dict, game: Path, log) -> None:
        """A GameCube/Wii disc names its version (GP6E01, GP6P01...): use the project's matching version, or say
        which ones the project has, instead of failing deep in the build."""
        if fp.kind() != "dtk":
            return
        versions = fp.versions()
        gid = scratch_dtk.disc_id(game)
        if not gid or not versions or gid == cfg.get("version"):
            return
        if gid in versions:
            log(f"your disc is {gid}: using the project's {gid} version")
            cfg["version"] = gid
            self._save_cfg(proj, cfg)
        elif all(re.fullmatch(r"[A-Z0-9]{6}", v) for v in versions):
            raise NetworkError(f"this disc is {gid}, but the project decompiles {', '.join(versions)}: "
                               "pick your copy of one of those versions")

    def check_rom(self, slug: str, log=print) -> dict:
        """A DS game started from scratch: rebuild its ROM and compare it with the user's copy (scratch_dsd.check_rom);
        what it says is kept in the project's .decomp.wtf.json and shown on the project."""
        proj, fp, cfg = self.get(slug)
        manifest = fp.manifest or {}
        if cfg.get("origin") != "scratch" or manifest.get("platform") not in ("nds", "gc", "wii"):
            raise NetworkError("only games started from scratch rebuild here")
        with self._build_locks[proj["id"]]:  # not while an agent's compile builds in the same tree
            if manifest.get("platform") == "nds":
                result = scratch_dsd.check_rom(fp.root, manifest["version"], log=log)
            else:
                ok, tail = fp.verify(log=log)
                diff = scratch_dtk._differing(fp.root, manifest["version"], tail) or []
                result = {"identical": ok, "detail": "identical" if ok else
                          f"{len(diff)} files differ: {', '.join(diff[:8])}" if diff
                          else (tail.strip().splitlines() or ["the build failed"])[-1][-300:]}
        manifest["rebuild"] = result | {"t": time.time()}
        (fp.root / ".decomp.wtf.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return result

    def check(self, slug: str, log=print) -> dict:
        """After a manual setup (or to refresh): pick the project up once it has an objdiff.json."""
        proj, fp, cfg = self.get(slug)
        if not (fp.root / "objdiff.json").exists():
            raise NetworkError("no objdiff.json yet. The project's build creates it (often `python configure.py` "
                               "or `make`); see its README")
        cfg["ready"] = True
        self._save_cfg(proj, cfg)
        measures = self.sync(slug, log=log)
        if fp.linkable():
            self.link_soon(slug)  # files that are finished but not linked yet (in the project as it came, too)
        return measures

    def _measure(self, proj, fp: ForkProject, log) -> dict:
        """objdiff's report for a project, read while the tree is locked so a build can't leave it half-written."""
        log("measuring progress with objdiff")
        try:
            with self._tree(proj).exclusive():  # never read an object while a build writes it
                return fp.report()
        except (ObjdiffError, ForkError) as e:
            raise NetworkError(f"objdiff couldn't measure the project: {e}") from None
        raise NetworkError("objdiff gave no report")  # only reached if the lock's holder swallows the read

    def sync(self, slug: str, log=print, rep: dict | None = None, broken: Collection[str] = frozenset(),
             agent: Agent | None = None) -> dict:
        """Measure the whole project with objdiff (or take a report just made) and update every function's state.
        Functions in `broken` units (sources that don't build) don't count as matched. Functions that match now and
        didn't before are recorded as matches (credited to `agent`, whose change it was)."""
        proj, fp, cfg = self.get(slug)
        rep = rep if rep is not None else self._measure(proj, fp, log)  # the caller's report, or a fresh one
        units = {u.name: u for u in fp.units()}
        rows = [(u["name"], f) for u in rep.get("units", []) for f in u.get("functions", [])]
        counts: dict[str, int] = defaultdict(int)
        for _, f in rows:
            counts[f["name"]] += 1
        all_rows = self.net.store.all("SELECT * FROM functions WHERE project_id=?", proj["id"])
        existing = {(r["unit"], self._meta(r).get("symbol"), self._meta(r).get("nth", 0)): r for r in all_rows}
        used = {r["name"] for r in all_rows}
        nth: dict[tuple[str, str], int] = defaultdict(int)  # a unit can hold two functions of the same name
        held = {u: self._held(proj, u) for u in {r["unit"] for r in all_rows if '"held"' in (r["meta"] or "")}}
        texts: dict[str, str | None] = {}  # unit -> its source, for functions that match now
        # a function whose code moved to another file of its module (a split) keeps its row: attempts, notes, claims
        here: set[tuple[str, str, int]] = set()
        seen: dict[tuple[str, str], int] = defaultdict(int)
        for uname, f in rows:
            here.add((uname, f["name"], seen[(uname, f["name"])]))
            seen[(uname, f["name"])] += 1
        moved: dict[tuple[str, str], list] = defaultdict(list)  # (module, symbol) -> rows no unit has any more
        for key, r in existing.items():
            if key not in here and key[0]:
                moved[(key[0].split("/", 1)[0], key[1])].append(r)
        with self.net.store.tx() as db:
            for uname, f in rows:
                sym = f["name"]
                k = nth[(uname, sym)]
                nth[(uname, sym)] += 1
                fuzzy = f.get("fuzzy_match_percent") if uname not in broken else None
                size = int(f.get("size", 0))
                n = max(1, size // 4)
                u = units.get(uname)
                md = f.get("metadata") or {}
                meta = {"symbol": sym, "demangled": md.get("demangled_name"), "source": u.source if u else None}
                if k:
                    meta["nth"] = k
                old = existing.get((uname, sym, k))
                if old is None and moved.get((uname.split("/", 1)[0], sym)):
                    old = moved[(uname.split("/", 1)[0], sym)].pop(0)
                    name = sym if counts[sym] == 1 else f"{sym}@{uname}" + (f"#{k + 1}" if k else "")
                    used.discard(old["name"])
                    while name in used:
                        name += "'"
                    used.add(name)
                    db.execute("UPDATE functions SET unit=?, name=? WHERE id=?", (uname, name, old["id"]))
                if old is not None and "held" in self._meta(old):
                    meta["held"] = self._meta(old)["held"]
                    if sym in held.get(uname, ()):
                        fuzzy = None if fuzzy is None else min(fuzzy, 99.99)
                if old is not None:
                    meta.update({k: v for k, v in self._meta(old).items() if k in ASM_KEYS})
                status = "matched" if fuzzy is not None and fuzzy >= 100 else "open" if u and u.source else "nosource"
                if status == "open" and any(meta.get(k) for k in ASM_KEYS):
                    status = "asm"  # hand-written assembly (here or in its twin elsewhere): not for agents
                score = None if fuzzy is None else scaled(fuzzy, n)
                vram = md.get("virtual_address")
                if old is None:
                    name = sym if counts[sym] == 1 else f"{sym}@{uname}" + (f"#{k + 1}" if k else "")
                    while name in used:
                        name += "'"
                    used.add(name)
                    db.execute("INSERT INTO functions(project_id,name,size,n_insns,status,best_score,unit,vram,meta,asm) "
                               "VALUES(?,?,?,?,?,?,?,?,?,?)", (proj["id"], name, size, n, status, score, uname,
                                                              int(vram) if vram else None, json.dumps(meta), ""))
                else:
                    if status != "matched" and old["best_source"] and old["best_score"] is not None:
                        score = old["best_score"] if score is None else min(score, old["best_score"])
                    db.execute("UPDATE functions SET size=?, n_insns=?, status=?, best_score=?, meta=? WHERE id=?",
                               (size, n, status, score, json.dumps(meta), old["id"]))
                    if status == "matched" and old["status"] != "matched":
                        if uname not in texts:
                            try:
                                texts[uname] = (fp.root / u.source).read_text(errors="replace",
                                                                              encoding="utf-8") if u and u.source else None
                            except OSError:
                                texts[uname] = None
                        self._matched(db, proj, old, agent, texts[uname])
            gone = [r["id"] for rs in moved.values() for r in rs]  # functions no unit has any more
            if gone:
                # One bound variable per id passes SQLite's limit once a project's units are
                # regenerated differently and every function moves, so ask in chunks.
                with_history = set()
                for i in range(0, len(gone), 500):
                    chunk = gone[i:i + 500]
                    with_history |= {r["function_id"] for r in db.execute(
                        f"SELECT DISTINCT function_id FROM attempts WHERE function_id IN ({','.join('?' * len(chunk))})",
                        chunk)}
                for fid in gone:
                    if fid in with_history:
                        db.execute("UPDATE functions SET status='nosource' WHERE id=?", (fid,))
                    else:
                        for table in ("claims", "notes"):
                            db.execute(f"DELETE FROM {table} WHERE function_id=?", (fid,))
                        db.execute("DELETE FROM functions WHERE id=?", (fid,))
            m = rep.get("measures", {})
            self.net._event(db, proj["id"], None, "sync", f"{float(m.get('matched_code_percent', 0)):.2f}% of code matches")
        log(f"{m.get('matched_functions', 0)}/{m.get('total_functions', 0)} functions match, "
            f"{float(m.get('matched_code_percent', 0)):.2f}% of code")
        cfg["measures"] = m
        cfg["uncovered"] = self._count_uncovered(rep)
        self._save_cfg(proj, cfg)
        if cfg.get("ready"):
            self.start_indexing(slug)
        return m

