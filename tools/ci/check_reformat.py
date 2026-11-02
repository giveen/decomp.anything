"""Refuse a commit whose code changed nothing.

Something in this checkout runs a formatter over files as they are edited: `ruff format` at the repo's 140 columns
with `skip-magic-trailing-comma = true`, which is not how this repo is written and is not set anywhere on disk, so
it is passed at call time by a tool rather than read from configuration. The result is a diff that rewrites wrapping
in files whose author changed no behaviour, and it has reached a pull request three times.

The check is deliberately about *behaviour*, not style: if two spellings of a file parse to the same syntax tree, the
change is wrapping (or comments, or blank lines) and it does not belong in a commit that claims to do something else.
Nothing here judges how a file should look — only whether a commit said it changed code and did not.
"""

from __future__ import annotations

import ast
import io
import subprocess
import sys
import tokenize

DEFAULT_BASE = "origin/main"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def ast_same(left: str, right: str) -> bool:
    """True when two spellings of a file are the same program."""
    try:
        return ast.dump(ast.parse(left)) == ast.dump(ast.parse(right))
    except SyntaxError:
        return False  # not a file this check can judge; the linters will say something


def _comments(source: str) -> list[str]:
    """Every comment in the file, in order. Wrapping never touches these, but an author adding an explanation does —
    so a commit that only rewrites comments is a real commit and must not be caught by this check."""
    try:
        return [tok.string for tok in tokenize.generate_tokens(io.StringIO(source).readline) if tok.type == tokenize.COMMENT]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return ["<unparsable>"]


def pure_reformat(old: str, new: str) -> bool:
    """True when the newer spelling is the same program with the same comments: only wrapping moved."""
    return ast_same(old, new) and _comments(old) == _comments(new)


def _show(ref: str) -> str:
    try:
        return _git("show", ref)
    except subprocess.CalledProcessError:
        return ""  # added or deleted at that end: nothing to compare


def reformatted_only(before: str, after: str, names: list[str]) -> list[str]:
    """The Python files that differ between two revisions but not as programs."""
    found = []
    for name in names:
        if not name.endswith(".py"):
            continue
        old, new = _show(f"{before}:{name}"), _show(f"{after}:{name}")
        if old and new and pure_reformat(old, new):
            found.append(name)
    return found


def main(argv: list[str]) -> int:
    if "--allow-reformat" in argv:  # the commit is about the wrapping; that is a decision, not an accident
        return 0
    staged = "--staged" in argv
    rest = [a for a in argv if not a.startswith("--")]
    if staged:  # the pre-commit hook: what is about to be committed, against what it replaces
        names = _git("diff", "--cached", "--name-only").split()
        found = _staged(names)
        what = "staged"
    else:
        base = rest[0] if rest else DEFAULT_BASE
        try:
            names = _git("diff", "--name-only", base, "HEAD").split()
        except subprocess.CalledProcessError:
            print(f"check_reformat: cannot diff against {base!r}; pass a base that exists", file=sys.stderr)
            return 2
        found = reformatted_only(base, "HEAD", names)
        what = f"between {base} and HEAD"
    if not found:
        return 0
    print(f"check_reformat: {len(found)} file(s) {what} differ only in wrapping:", file=sys.stderr)
    for name in found:
        print(f"  {name}", file=sys.stderr)
    print("\nSame program, same comments, different line breaks. A formatter appears to run\n"
          "on edit in this checkout (ruff format at skip-magic-trailing-comma). Undo them with:\n"
          "  git checkout HEAD -- <the files above>    (staged: git reset -q -- <the files> && git checkout -- <the files>)\n"
          "If the wrapping really is the point of the commit, say so in the message and pass --allow-reformat.",
          file=sys.stderr)
    return 1


def _staged(names: list[str]) -> list[str]:
    """The staged version of each file against its HEAD version."""
    found = []
    for name in names:
        if not name.endswith(".py"):
            continue
        old = _show(f"HEAD:{name}")
        new = _show(f":{name}")  # the index, which is what the commit will contain
        if old and new and pure_reformat(old, new):
            found.append(name)
    return found


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
