"""The oracle for splitting the page's scripts (tools/js_exact.py).

The tool is what says a split was a move and not a rewrite: the parts, concatenated in the order the page loads them, must
reproduce the original byte for byte. A checker nobody checks is worse than none, so this proves it accepts a faithful
split and rejects a stray byte.
"""

import subprocess
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tools" / "js_exact.py"
APP_JS = Path(__file__).resolve().parents[1] / "src" / "decompnet" / "app" / "static" / "app.js"


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True)


def test_the_pages_sections_are_what_a_split_cuts_on():
    result = run("sections", str(APP_JS))
    assert result.returncode == 0, result.stdout + result.stderr
    names = [line.split(None, 1)[1].strip() for line in result.stdout.splitlines() if line.strip()]
    assert len(names) >= 20  # the page is 25 job-sized sections; fewer means the banners changed shape
    for expected in ("theme", "state + routing", "home", "map", "project settings"):
        assert expected in names


def test_a_faithful_split_verifies(tmp_path):
    text = APP_JS.read_text(encoding="utf-8")
    cut = text.index("/* ------------------------------------------------------------------ home */")
    original = tmp_path / "app.js"
    original.write_text(text, encoding="utf-8")
    first, second = tmp_path / "one.js", tmp_path / "two.js"
    first.write_text(text[:cut], encoding="utf-8")
    second.write_text(text[cut:], encoding="utf-8")
    result = run("verify", "--original", str(original), "--parts", str(first), str(second))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "byte for byte" in result.stdout


def test_one_changed_byte_is_caught(tmp_path):
    text = APP_JS.read_text(encoding="utf-8")
    cut = text.index("/* ------------------------------------------------------------------ home */")
    original = tmp_path / "app.js"
    original.write_text(text, encoding="utf-8")
    first, second = tmp_path / "one.js", tmp_path / "two.js"
    first.write_text(text[:cut], encoding="utf-8")
    second.write_text(text[cut:].replace("</div>", "</span>", 1), encoding="utf-8")  # a rewrite dressed as a move
    result = run("verify", "--original", str(original), "--parts", str(first), str(second))
    assert result.returncode == 1
    assert "DIFFERENT" in result.stdout and "</span>" in result.stdout


def test_split_then_verify_round_trips_the_real_file(tmp_path):
    original = tmp_path / "app.js"
    original.write_text(APP_JS.read_text(encoding="utf-8"), encoding="utf-8")
    parts = tmp_path / "parts"
    result = run("split", str(original), "--at", "home", "map", "--out", str(parts))
    assert result.returncode == 0, result.stdout + result.stderr
    written = sorted(p.name for p in parts.iterdir())
    assert written == ["app.js", "app_home.js", "app_map.js"]
    verified = run("verify", "--original", str(original), "--parts", *[str(parts / n) for n in written])
    assert verified.returncode == 0, verified.stdout + verified.stderr
