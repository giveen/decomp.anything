"""The page's scripts, loaded the way the browser loads them (tests/app_smoke.cjs).

The page is plain JavaScript in `<script src>` tags with no build step and one shared global scope. Nothing else in the
suite runs it, so a split of app.js had no net: this is that net. The harness reads its script list from index.html, so
it covers the single file today and every part of a split later, in the page's own order.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

NODE = shutil.which("node")
TOOL = Path(__file__).resolve().parents[1] / "tools" / "js_exact.py"
APP_STATIC = Path(__file__).resolve().parents[1] / "src" / "decompnet" / "app" / "static"


@pytest.mark.skipif(NODE is None, reason="needs Node.js for the page's browser logic")
def test_the_page_loads_boots_and_renders_from_its_own_scripts():
    assert NODE is not None
    result = subprocess.run([NODE, str(Path(__file__).with_name("app_smoke.cjs"))],
                            check=True, capture_output=True, text=True)
    assert "home + catalog rendered" in result.stdout


def _smoke(static: Path) -> subprocess.CompletedProcess:
    assert NODE is not None
    return subprocess.run([NODE, str(Path(__file__).with_name("app_smoke.cjs"))],
                          capture_output=True, text=True, env={**os.environ, "APP_STATIC": str(static)})


def _split_page(tmp_path: Path, order: list[str]) -> Path:
    """A copy of the page whose app.js is cut into parts, listed in index.html in `order` (which may be wrong)."""
    static = tmp_path / "static"
    shutil.copytree(APP_STATIC, static)
    parts = tmp_path / "parts"
    subprocess.run([sys.executable, str(TOOL), "split", str(static / "app.js"), "--at", "home", "map",
                    "--out", str(parts)], check=True, capture_output=True, text=True)
    for part in parts.iterdir():
        shutil.copy(part, static / part.name)
    index = (static / "index.html").read_text(encoding="utf-8")
    tags = "".join(f'<script src="/assets/{name}?v=2"></script>\n' for name in order)
    index = re.sub(r'<script src="/assets/app\.js[^"]*"></script>\n', tags, index)
    (static / "index.html").write_text(index, encoding="utf-8")
    return static


def test_a_split_page_still_boots(tmp_path):
    """The split we intend to do: cut app.js at its sections, list the parts in order -- the harness needs no change."""
    static = _split_page(tmp_path, ["app.js", "app_home.js", "app_map.js"])
    result = _smoke(static)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 script(s) loaded (app.js, app_home.js, app_map.js, console.js)" in result.stdout


def test_a_misordered_split_is_caught(tmp_path):
    """The failure mode a source comparison cannot see: a part listed early runs the boot before its dependencies exist."""
    static = _split_page(tmp_path, ["app_map.js", "app.js", "app_home.js"])
    result = _smoke(static)
    assert result.returncode == 1
    assert "app_map.js threw while loading: ReferenceError" in result.stderr
