"""Forks of public decomps: a real project whose own build is the judge.

A decomp published by its community (decomp.dev's projects) is a git tree with an objdiff.json that says which compiler
settings reproduce each unit. Forking one gives an agent a project that builds: a function is matched when the fork's
build produces the original bytes, which objdiff measures unit by unit, so a match here is a match in a project the user
can actually build and play.

The engine is one object per app (RomNetwork.forks): it holds the tree locks and what objdiff last said. Everything else
is a part beside it, by the job it does: forkcreate makes a fork, forklink links it, forksources gives homeless code a
file, forkindex keeps the look-alike index, forkagents is what an agent does here, forkauto tries drafts, forkedit reads
and edits the tree, forkviews answers the page. They are one class at runtime, so a part reaches a helper in another with
a plain self. call; tests/test_fork_parts.py checks that every name resolves and has exactly one owner.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import defaultdict
from contextlib import ExitStack, contextmanager
from pathlib import Path

from ..forks import catalog
from ..forks.objdiff import ObjdiffError
from ..forks.project import ForkError, ForkProject
from ..network import NetworkError
from .forkagents import ForkAgentMixin
from .forkauto import ForkAutoMixin
from .forkcreate import ForkCreateMixin
from .forkedit import ForkEditMixin
from .forkindex import ForkIndexMixin
from .forklink import ForkLinkMixin
from .forksources import ForkSourceMixin
from .forkutil import (
    TreeLock,
)
from .forkviews import ForkViewMixin


class ForkEngine(ForkCreateMixin, ForkLinkMixin, ForkSourceMixin, ForkIndexMixin, ForkAgentMixin,
                 ForkAutoMixin, ForkEditMixin, ForkViewMixin):
    def __init__(self, net):
        self.net = net
        self._trees: dict[int, TreeLock] = defaultdict(TreeLock)  # project id -> its source tree's lock
        self._unit_locks: dict[tuple[int, str], threading.RLock] = defaultdict(threading.RLock)
        self._build_locks: dict[int, threading.Lock] = defaultdict(threading.Lock)  # one build at a time per tree
        self._git_locks: dict[int, threading.Lock] = defaultdict(threading.Lock)
        self._mlock = threading.RLock()
        # (project id, unit) -> (sha1 of its source, base object mtime, {function: match %}): what objdiff said
        # the last time it measured the unit, so a compile doesn't need a second objdiff run to know the baseline
        self._scores: dict[tuple[int, str], tuple[str, int | None, dict]] = {}
        self._index: dict[int, dict] = {}  # project id -> {unit: {"mtime", "order", "ops"}}
        self._indexing: set[int] = set()
        self._ilock = threading.RLock()
        self._linking: dict[str, dict] = {}  # slug -> background link run

    def _fork_project(self, proj) -> ForkProject:
        """The project's source tree. One place builds it, so a test can stand in for it (tests/test_forks.py)."""
        return ForkProject(Path(proj["root"]))

    # ------------------------------------------------------------------ locks, builds, baselines

    def _tree(self, proj) -> TreeLock:
        with self._mlock:
            return self._trees[proj["id"]]

    def _unit_lock(self, proj, unit: str) -> threading.RLock:
        with self._mlock:
            return self._unit_locks[(proj["id"], unit)]

    @contextmanager
    def _units_locked(self, proj, names: list[str]):
        with ExitStack() as stack:
            for n in sorted(set(names)):
                stack.enter_context(self._unit_lock(proj, n))
            yield

    def _build_lock(self, proj) -> threading.Lock:
        with self._mlock:
            return self._build_locks[proj["id"]]

    def _build_unit(self, proj, fp: ForkProject, unit) -> tuple[bool, str]:
        """Build one unit's object. Builds share the tree's build files (ninja's log), so they take turns."""
        with self._build_lock(proj):
            return fp.build_unit(unit)

    def _build_all(self, proj, fp: ForkProject, failed: list[str] | None = None) -> tuple[bool, str]:
        with self._build_lock(proj):
            return fp.build_all(failed=failed)

    def _keep_scores(self, proj, unit, text: str, scores: dict) -> None:
        mtime = unit.base.stat().st_mtime_ns if unit.base is not None and unit.base.exists() else None
        with self._mlock:
            self._scores[(proj["id"], unit.name)] = (hashlib.sha1(text.encode()).hexdigest(), mtime, dict(scores))

    def _last_scores(self, proj, unit) -> dict | None:
        with self._mlock:
            hit = self._scores.get((proj["id"], unit.name))
        return hit[2] if hit else None

    def _baseline(self, proj, fp: ForkProject, unit, text: str) -> tuple[dict | None, bool]:
        """({function: match %} for the unit as its source file is now, whether that builds). Scores are None when
        the file doesn't build or objdiff can't measure it."""
        with self._mlock:
            hit = self._scores.get((proj["id"], unit.name))
        if (hit and unit.base is not None and unit.base.exists() and hit[0] == hashlib.sha1(text.encode()).hexdigest()
                and hit[1] == unit.base.stat().st_mtime_ns):
            return hit[2], True
        ok, _ = self._build_unit(proj, fp, unit)  # the object may be older than the file
        if not ok or unit.base is None or not unit.base.exists():
            return None, False
        try:
            d = fp.diff(unit, None)
        except (ObjdiffError, ForkError):
            return None, True
        self._keep_scores(proj, unit, text, d.unit_scores)
        return d.unit_scores, True

    def _commit(self, proj, fp: ForkProject, rel: str, message: str) -> None:
        """Commit one file (agents work side by side: never sweep up another one's change)."""
        with self._git_locks[proj["id"]]:
            fp.git("add", "--", rel)
            fp.git("commit", "-q", "-m", message, "--", rel)

    @staticmethod
    def is_fork(proj) -> bool:
        return json.loads(proj["config"]).get("kind") == "fork"

    def get(self, slug: str):
        proj = self.net._project(slug)
        if not self.is_fork(proj):
            raise NetworkError(f"{slug} isn't a fork")
        return proj, ForkProject(Path(proj["root"])), json.loads(proj["config"])

    def _save_cfg(self, proj, cfg: dict) -> None:
        with self.net.store.tx() as db:
            db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), proj["id"]))

    @staticmethod
    def _meta(fn) -> dict:
        return json.loads(fn["meta"]) if fn["meta"] else {}

    def catalog(self) -> tuple[list[dict], float | None]:
        """decomp.dev's list as this install downloaded it before the directory was retired, and when."""
        path = self.net.workspace / ".decomp" / "catalog.json"
        return catalog.catalog(path), catalog.cached_at(path)
