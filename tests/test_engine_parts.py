"""The parts RomNetwork is assembled from (engine.py): the invariants a refactor of it must not break.

RomNetwork is one class at runtime, made of mixins that hold what used to be sections of one 5,000-line file. Reading a
method in its own module is easy; the risk is what a split hides — a second owner of a method Network already has (then
super() stops reaching Network), or a mixin that imports the engine back (a cycle that only shows up at import time).
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from decompnet.app.engine import RomNetwork
from decompnet.app.platform import BASE_HEADER, PLATFORM_STATUS
from decompnet.network import Network

MIXINS = [cls for cls in RomNetwork.__mro__ if cls not in (RomNetwork, Network, object)]


def test_mixins_come_before_the_core():
    """A mixin must precede Network, or its overrides (claim, attempt, rename_symbol...) would never run."""
    assert MIXINS, "RomNetwork is assembled from mixins; none were found"
    assert RomNetwork.__mro__.index(Network) == len(MIXINS) + 1


def test_one_owner_per_method_network_also_has():
    """super() in an override must land on Network: exactly one part may define a name Network defines."""
    owners: dict[str, list[str]] = {}
    for cls in [RomNetwork, *MIXINS]:
        for name, value in vars(cls).items():
            if inspect.isfunction(value) and hasattr(Network, name):
                owners.setdefault(name, []).append(cls.__name__)
    shared = {name: who for name, who in owners.items() if len(who) > 1}
    assert not shared, f"more than one part overrides {shared}"


def test_every_override_still_reaches_network():
    """Each override that calls super() gets the plain Network method, not another part's version."""
    mro = RomNetwork.__mro__
    for cls in MIXINS:
        for name, value in vars(cls).items():
            if not inspect.isfunction(value) or name == "__init__":
                continue
            code = value.__code__
            if "super" not in code.co_names and "super" not in (code.co_freevars or ()):
                continue
            assert hasattr(Network, name), f"{cls.__name__}.{name} calls super().{name}() but Network has no {name}"
            # the next class after this one that defines the name must be Network itself
            owner = next((c for c in mro[mro.index(cls) + 1 :] if name in vars(c)), None)
            assert owner is Network, f"{cls.__name__}.{name} super() lands on {getattr(owner, '__name__', None)}, not Network"


def test_the_page_and_tests_still_import_from_engine():
    """engine.py stays the module name the app and its tests import, and re-exports what they ask it for."""
    assert PLATFORM_STATUS["gc"] == "recommended"
    assert BASE_HEADER.format(title="demo").startswith("/* context.h for demo")
    for name in ("COMPILERS", "MW_VERSIONS", "SHORT_NAMES", "TOOLS"):
        assert hasattr(__import__("decompnet.app.engine", fromlist=[name]), name)


def test_parts_do_not_import_the_engine_back():
    """A part that imports engine would make the class depend on the module that assembles it."""
    import ast
    from pathlib import Path

    root = Path(inspect.getfile(RomNetwork)).parent
    for cls in MIXINS:
        source = Path(inspect.getfile(cls)).read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in ("engine", "decompnet.app.engine"):
                raise AssertionError(f"{cls.__name__} ({inspect.getfile(cls)}) imports the engine")
        assert "from .engine import" not in source and "import engine" not in source
    assert (root / "engine.py").is_file()


def test_every_self_attribute_a_part_uses_is_defined():
    """The parts are read as separate files, so a helper that moved out from under its caller looks fine in both.

    This is the check that catches it: every self.<name> a part reads must be something the class hierarchy defines
    (a method, a class attribute, or an attribute some __init__ assigns). It found a real one when the split was made:
    a splitter regex stayed behind in the section it was written in and the intake report lost it."""
    files = [
        Path(inspect.getfile(RomNetwork)),
        Path(inspect.getfile(Network)),
        *[Path(inspect.getfile(cls)) for cls in MIXINS if inspect.getfile(cls) != inspect.getfile(RomNetwork)],
    ]
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
                    elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
                        defined.add(target.attr)  # self._foo = ... in an __init__
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Attribute)
                and isinstance(node.target.value, ast.Name)
                and node.target.value.id == "self"
            ):
                defined.add(node.target.attr)  # self._foo: dict[...] = {} in an __init__
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
                used.setdefault(node.attr, path.name)
    unknown = {name: where for name, where in used.items() if name not in defined}
    assert not unknown, f"self.<name> used but never defined: {unknown}"
