"""The parts ForkEngine is assembled from (forkengine.py): the invariants a refactor of it must not break.

ForkEngine is one class at runtime, made of parts that hold what used to be sections of one 2,400-line file. Two things
a split can hide: a second part defining a method another part already has (silently shadowing it, since a call is just
self.name), and a helper that stayed behind when its callers moved. Both are checked here, statically.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from decompnet.app.forkengine import ForkEngine

PARTS = [cls for cls in ForkEngine.__mro__ if cls is not object]


def test_a_method_has_one_owner():
    """A part that redefines another part's method would shadow it, and self.name gives no sign of which one runs."""
    owners: dict[str, list[str]] = {}
    for cls in PARTS:
        for name, value in vars(cls).items():
            if inspect.isfunction(value):
                owners.setdefault(name, []).append(cls.__name__)
    shared = {name: who for name, who in owners.items() if len(who) > 1}
    assert not shared, f"more than one part defines {shared}"


def test_every_self_attribute_a_part_uses_is_defined():
    """Every self.<name> a part reads must be something the assembled class defines: a method, or an attribute some
    __init__ assigns. This is the check that catches a helper left behind by the section that moved."""
    files = [Path(inspect.getfile(cls)) for cls in PARTS]
    files.append(Path(inspect.getfile(ForkEngine)).parent / "forkutil.py")  # the shared text helpers
    defined: set[str] = set()
    used: dict[str, str] = {}
    for path in dict.fromkeys(files):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                defined.add(node.id)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        defined.add(target.id)
                    elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) \
                            and target.value.id == "self":
                        defined.add(target.attr)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Attribute) \
                    and isinstance(node.target.value, ast.Name) and node.target.value.id == "self":
                defined.add(node.target.attr)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
                used.setdefault(node.attr, path.name)
    unknown = {name: where for name, where in used.items() if name not in defined}
    assert not unknown, f"self.<name> used but never defined: {unknown}"


def test_parts_do_not_import_the_engine_back():
    """A part that imported forkengine would make the class depend on the module that assembles it."""
    for cls in PARTS:
        source = Path(inspect.getfile(cls)).read_text()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module in ("forkengine", "decompnet.app.forkengine"):
                raise AssertionError(f"{cls.__name__} ({inspect.getfile(cls)}) imports the engine")


def test_the_engine_is_still_reachable_where_callers_expect_it():
    """The names callers and tests use (`ForkProject` above all: tests/test_forks.py stands in for it) stay importable."""
    from decompnet.app import forkengine

    assert forkengine.ForkProject is not None
    engine = ForkEngine(net=object())  # the app makes one per RomNetwork; it holds the tree locks and the last scores
    assert engine.net is not None
