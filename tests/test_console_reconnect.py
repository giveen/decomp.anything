"""The console's catalog and runtime identity belong to a particular REA MCP connection."""

import shutil
import subprocess
from pathlib import Path

import pytest

from decompnet.app.console import Console, ConsoleError

from .test_rea import FAKE
from .test_rea import fake as fake


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js for console browser logic")
def test_browser_refreshes_forms_and_shows_the_runtime_version():
    subprocess.run([shutil.which("node"), str(Path(__file__).with_name("console_reconnect.cjs"))],
                   check=True, capture_output=True, text=True)


def test_reconnect_reloads_catalog_schema_risks_and_runtime(monkeypatch, fake):
    monkeypatch.setenv("DECOMP_REA", " ".join(fake))
    console = Console()
    try:
        assert console.status()["connection"] is None  # status alone never starts REA
        original = console.tools()
        assert console.tools() is original
        first = console.status()["connection"]
        assert first["server"] == {"name": "fake-rea", "version": "0"}
        assert first["source"] == "DECOMP_REA" and first["command"] == fake
        console.call("open_binary", {"path": "/old/target.exe"})
        console.client.close()
        assert console.status()["target"] is None and console.status()["tools"] == 0

        replacement = '''
TOOLS = [t for t in TOOLS if t["name"] != "procedure_pseudo_code"]
TOOLS.append({"name": "new_tool", "inputSchema": {"type": "object", "properties": {"address": {"type": "integer"}}},
              "annotations": {"destructiveHint": True}})
for line in sys.stdin:
'''
        Path(fake[1]).write_text(FAKE.replace("for line in sys.stdin:", replacement)
                                .replace('"version": "0"', '"version": "6.8.0"'))
        tools = {t["name"]: t for t in console.tools()}
        assert "procedure_pseudo_code" not in tools and "new_tool" in tools
        assert tools["new_tool"]["inputSchema"]["properties"] == {"address": {"type": "integer"}}
        assert console.needs_confirm("new_tool") == ["can change or delete things"]
        with pytest.raises(ConsoleError, match="no tool"):
            console.needs_confirm("procedure_pseudo_code")
        assert console.target is None
        second = console.status()["connection"]
        assert second["id"] != first["id"] and second["server"]["version"] == "6.8.0"
        assert second["command"] == fake
    finally:
        console.stop()
    assert console.status()["connection"] is None


def test_queued_call_cannot_run_on_a_replacement_connection(monkeypatch, fake):
    monkeypatch.setenv("DECOMP_REA", " ".join(fake))
    console = Console()
    try:
        why, ident = console.prepare_call("open_binary")
        assert why == []
        console.client.close()
        with pytest.raises(ConsoleError, match="reconnected before this tool ran"):
            console.call("open_binary", {"path": "/must/not/open.exe"}, connection_id=ident)
        assert console.target is None
        why, new_ident = console.prepare_call("open_binary")
        assert new_ident != ident
        result = console.call("open_binary", {"path": "/new/target.exe"}, connection_id=new_ident)
        assert result["connection_id"] == new_ident and console.target["path"] == "/new/target.exe"
    finally:
        console.stop()


def test_failed_reconnect_discards_the_previous_catalog(monkeypatch, fake):
    monkeypatch.setenv("DECOMP_REA", " ".join(fake))
    console = Console()
    try:
        console.tools()
        console.call("open_binary", {"path": "/old/target.exe"})
        console.client.close()
        Path(fake[1]).write_text("raise SystemExit(1)")
        with pytest.raises(ConsoleError, match="REA exited"):
            console.tools()
        assert console.client is None and console.catalog is None and console.target is None
        assert console.status()["connection"] is None
    finally:
        console.stop()


@pytest.mark.parametrize("info", ['{"name": "fake-rea"}', 'None', '{"name": "fake-rea", "version": 123}'])
def test_missing_server_version_is_not_replaced_by_the_baseline(monkeypatch, fake, info):
    monkeypatch.setenv("DECOMP_REA", " ".join(fake))
    Path(fake[1]).write_text(FAKE.replace('{"name": "fake-rea", "version": "0"}', info))
    console = Console()
    try:
        console.tools()
        assert "version" not in console.status()["connection"]["server"]
    finally:
        console.stop()
