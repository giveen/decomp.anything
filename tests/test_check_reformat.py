"""The wrap-only guard: what it must catch, and what it must leave alone."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "ci"))

from check_reformat import ast_same, pure_reformat

WRAPPED = '''def f(e):
    return any(phrase in str(e) for phrase in (
        "one", "two", "three"))
'''

UNWRAPPED = '''def f(e):
    return any(phrase in str(e) for phrase in ("one", "two", "three"))
'''


def test_rewrapping_the_same_program_is_caught():
    """The formatter's signature: one construct reflowed, nothing else touched."""
    assert ast_same(WRAPPED, UNWRAPPED)
    assert pure_reformat(WRAPPED, UNWRAPPED)


def test_a_changed_comment_is_a_real_commit():
    """Explaining something in a comment is work, not churn, even though the syntax tree ignores comments."""
    explained = UNWRAPPED.replace("return any", "# the four phrases the intake check looks for\n    return any")
    assert ast_same(UNWRAPPED, explained), "comments never reach the syntax tree"
    assert not pure_reformat(UNWRAPPED, explained), "but they must still be committable"


def test_a_changed_constant_is_not_wrapping():
    changed = UNWRAPPED.replace('"three"', '"four"')
    assert not ast_same(UNWRAPPED, changed)
    assert not pure_reformat(UNWRAPPED, changed)


def test_an_unparsable_file_is_left_to_the_linters():
    """Half-written code must not be reported as churn: this check judges shape, not validity."""
    assert not ast_same("def f(:\n", UNWRAPPED)
    assert not pure_reformat("def f(:\n", UNWRAPPED)
