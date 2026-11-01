"""The oracle for splitting the page's scripts (tools/js_exact.py).

The tool is what says a split was a move and not a rewrite: the parts, concatenated in the order the page loads them, must
reproduce the original byte for byte. A checker nobody checks is worse than none, so this proves it accepts a faithful
split and rejects a stray byte.

The page's scripts are no longer one file, so "the original" here means the page's app scripts joined in index.html order --
whatever the tree currently has, one file or thirteen.
"""

import re
import subprocess
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tools" / "js_exact.py"
STATIC = Path(__file__).resolve().parents[1] / "src" / "decompnet" / "app" / "static"


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True)


def page_parts() -> list[str]:
    """The page's app scripts, in the order the browser runs them (console.js is its own thing, not part of app.js)."""
    names = re.findall(r'<script\s+src="/assets/(app[a-z_]*\.js)', (STATIC / "index.html").read_text(encoding="utf-8"))
    assert names, "index.html loads no app scripts"
    return names


def page_text() -> str:
    return "".join((STATIC / name).read_text(encoding="utf-8") for name in page_parts())


def test_the_pages_sections_are_what_a_split_cuts_on(tmp_path):
    original = tmp_path / "page.js"
    original.write_text(page_text(), encoding="utf-8")
    result = run("sections", str(original))
    assert result.returncode == 0, result.stdout + result.stderr
    names = [line.split(None, 1)[1].strip() for line in result.stdout.splitlines() if line.strip()]
    assert len(names) >= 20  # the page is 25 job-sized sections; fewer means the banners changed shape
    for expected in ("theme", "state + routing", "home", "map", "project settings"):
        assert expected in names


def test_a_faithful_split_verifies(tmp_path):
    text = page_text()
    cut = text.index("/* ------------------------------------------------------------------ home")
    original = tmp_path / "page.js"
    original.write_text(text, encoding="utf-8")
    first, second = tmp_path / "one.js", tmp_path / "two.js"
    first.write_text(text[:cut], encoding="utf-8")
    second.write_text(text[cut:], encoding="utf-8")
    result = run("verify", "--original", str(original), "--parts", str(first), str(second))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "byte for byte" in result.stdout


def test_one_changed_byte_is_caught(tmp_path):
    text = page_text()
    cut = text.index("/* ------------------------------------------------------------------ home")
    original = tmp_path / "page.js"
    original.write_text(text, encoding="utf-8")
    first, second = tmp_path / "one.js", tmp_path / "two.js"
    first.write_text(text[:cut], encoding="utf-8")
    second.write_text(text[cut:].replace("</div>", "</span>", 1), encoding="utf-8")  # a rewrite dressed as a move
    result = run("verify", "--original", str(original), "--parts", str(first), str(second))
    assert result.returncode == 1
    assert "DIFFERENT" in result.stdout and "</span>" in result.stdout


def test_split_then_verify_round_trips_the_page(tmp_path):
    original = tmp_path / "page.js"
    original.write_text(page_text(), encoding="utf-8")
    parts = tmp_path / "parts"
    result = run("split", str(original), "--at", "home", "map", "--out", str(parts))
    assert result.returncode == 0, result.stdout + result.stderr
    written = sorted(p.name for p in parts.iterdir())
    assert written == ["page.js", "page_home.js", "page_map.js"]
    verified = run("verify", "--original", str(original), "--parts", *[str(parts / n) for n in written])
    assert verified.returncode == 0, verified.stdout + verified.stderr
    assert "byte for byte" in verified.stdout
