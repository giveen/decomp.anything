"""The oracle for splitting the page's scripts: the parts must add up to the file they came from.

app.js is plain JavaScript loaded by ``<script src>`` tags with no build step, so a split is a *move*: cut at section
boundaries, list the parts in ``index.html``, and change not one character. That makes the check exact instead of
approximate -- concatenating the parts in the order the page loads them must reproduce the original byte for byte:

    python tools/js_exact.py verify --original "$(git show main:...app.js)"      # reads index.html for the parts
    python tools/js_exact.py verify --original old.js --parts a.js b.js

``sections`` lists the cut points and ``split`` performs the cut, so the parts it writes verify by construction.

Nothing here runs the page (tests/app_smoke.cjs does); this only says the text is the same, which is the property a
move must have and a diff of 4,000 lines cannot show you.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import re
import sys
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src/decompnet/app/static"
SECTION = re.compile(r"^/\* -{10,} (.*?)(?: \*/)?$")
SCRIPT = re.compile(r'<script\s+src="/assets/([^"?]+)(?:\?[^"]*)?"')


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def sections(path: Path) -> list[tuple[int, str]]:
    """(line number, name) for every section banner, in order."""
    out = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = SECTION.match(line.strip())
        if m:
            out.append((i, m.group(1)))
    return out


def parts_from_index(index: Path) -> list[Path]:
    """The page's scripts, in the order the browser runs them (that order is what a split must preserve)."""
    names = SCRIPT.findall(index.read_text(encoding="utf-8"))
    if not names:
        raise SystemExit(f'no <script src="/assets/..."> in {index}')
    def resolve(name: str) -> Path:
        beside = index.parent / name
        return beside if beside.is_file() else index.parent / "static" / name
    return [resolve(name) for name in names]


def verify(original: Path, parts: list[Path]) -> int:
    text = original.read_text(encoding="utf-8")
    missing = [p for p in parts if not p.is_file()]
    if missing:
        print("missing part(s):", *[str(p) for p in missing], sep="\n  ")
        return 2
    joined = "".join(p.read_text(encoding="utf-8") for p in parts)
    print(f"original  {original.name}  {len(text.splitlines()):>5} lines  {sha(text)}")
    for p in parts:
        one = p.read_text(encoding="utf-8")
        print(f"  part    {p.name:<24} {len(one.splitlines()):>5} lines  {sha(one)}")
    print(f"joined    {' + '.join(p.name for p in parts):<24} {len(joined.splitlines()):>5} lines  {sha(joined)}")
    if joined == text:
        print("\nidentical: the parts add up to the original, byte for byte")
        return 0
    print("\nDIFFERENT: the parts are not the original file")
    for line in list(difflib.unified_diff(text.splitlines(), joined.splitlines(), "original", "joined", lineterm="", n=2))[:40]:
        print(line)
    return 1


def split(original: Path, at: list[str], out: Path, first: str | None = None,
          names: list[str] | None = None) -> int:
    """Cut the original before each named section, so the concatenation is the original by construction."""
    lines = original.read_text(encoding="utf-8").splitlines(keepends=True)
    starts = {name: ln for ln, name in sections(original)}
    unknown = [name for name in at if name not in starts]
    if unknown:
        raise SystemExit(f"no such section(s): {unknown}\nknown: {[n for _, n in sections(original)]}")
    first = first or original.name
    cuts = sorted(starts[name] for name in at)
    bounds = [0, *[c - 1 for c in cuts], len(lines)]
    out.mkdir(parents=True, exist_ok=True)
    written = names or [first, *[f"{first[:-3]}_{re.sub(r'[^a-z0-9]+', '_', n)}.js"
                                 for _, n in sorted((starts[name], name) for name in at)]]
    if len(written) != len(cuts) + 1:
        raise SystemExit(f"--names takes {len(cuts) + 1} names (one per part), got {len(written)}")
    for path, lo, hi in zip(written, bounds[:-1], bounds[1:], strict=True):
        (out / path).write_text("".join(lines[lo:hi]), encoding="utf-8")
        print(f"wrote {path}: lines {lo + 1}-{hi} ({hi - lo})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_sections = sub.add_parser("sections", help="list the section banners (the cut points)")
    p_sections.add_argument("file", nargs="?", type=Path, default=STATIC / "app.js")

    p_verify = sub.add_parser("verify", help="the parts, concatenated in load order, must equal the original")
    p_verify.add_argument("--original", type=Path, required=True, help="the file before the split (git show it)")
    p_verify.add_argument("--parts", nargs="*", type=Path, default=None, help="default: whatever index.html loads")
    p_verify.add_argument("--index", type=Path, default=STATIC / "index.html")

    p_split = sub.add_parser("split", help="cut a file before the given sections, so verify passes by construction")
    p_split.add_argument("file", type=Path)
    p_split.add_argument("--at", nargs="+", required=True, metavar="SECTION")
    p_split.add_argument("--out", type=Path, default=STATIC)
    p_split.add_argument("--first", default=None, help="name of the first part (default: the original's name)")
    p_split.add_argument("--names", nargs="+", metavar="FILE", help="name every part, in order (else derived)")

    args = parser.parse_args()
    if args.command == "sections":
        for line, name in sections(args.file):
            print(f"{line:5d}  {name}")
        return 0
    if args.command == "verify":
        parts = args.parts or parts_from_index(args.index)
        return verify(args.original, parts)
    return split(args.file, args.at, args.out, args.first, args.names)


if __name__ == "__main__":
    sys.exit(main())
