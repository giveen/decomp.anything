"""REA updates are staged and probed before selecting a version; custom commands are never replaced."""

import json
import os
import sys
import threading
from pathlib import Path

import pytest

from decompnet import rea, toolchains
from decompnet.app import rea_updates, updates

from .test_rea import FAKE


@pytest.fixture
def managed(tmp_path, monkeypatch):
    monkeypatch.setenv("DECOMPNET_TOOLCHAINS", str(tmp_path / "tools"))
    monkeypatch.setattr(rea, "settings", lambda: {"source": "installed", "env": {"JAVA_HOME": "kept"}, "provider": "ghidra"})
    monkeypatch.setattr(updates, "can_apply", lambda: (True, ""))
    monkeypatch.setattr(rea_updates.shutil, "which", lambda name: name)
    base = toolchains.path("rea")
    write_package(base, "6.7.0")
    (base / toolchains.MANIFEST).write_text(json.dumps({"id": "rea", "source": "morluto/rea 6.7.0"}))
    monkeypatch.setattr(rea_updates, "latest", lambda: {"name": "rea-agents", "version": "6.8.0"})
    return base


def write_package(base, version):
    package = base / "node_modules" / "rea-agents" / "package.json"
    package.parent.mkdir(parents=True, exist_ok=True)
    package.write_text(json.dumps({"name": "rea-agents", "version": version}))
    command = base / "node_modules" / ".bin" / ("rea.cmd" if os.name == "nt" else "rea")
    command.parent.mkdir(exist_ok=True)
    command.write_text("fake launcher")


def test_release_check_and_no_downgrades(managed, monkeypatch):
    found = rea_updates.check()
    assert found["available"] and found["current"]["version"] == "6.7.0"
    for version in ("6.7.0", "6.6.0"):
        monkeypatch.setattr(rea_updates, "latest", lambda v=version: {"version": v})
        assert not rea_updates.check()["available"]
    monkeypatch.setattr(rea_updates, "latest", lambda: (_ for _ in ()).throw(OSError("offline")))
    found = rea_updates.check()
    assert not found["available"] and "offline" in found["error"]


@pytest.mark.parametrize("source", ["saved", "DECOMP_REA", "PATH", "Claude Code", "off", None])
def test_external_commands_are_not_updated(managed, monkeypatch, source):
    monkeypatch.setattr(rea, "settings", lambda: {"source": source})
    monkeypatch.setattr(rea_updates, "latest", lambda: pytest.fail("external installs must not query npm"))
    found = rea_updates.check()
    assert not found["can_apply"] and not found["available"]
    with pytest.raises(ValueError, match="external command"):
        rea_updates.install({"latest": {"version": "6.8.0"}})


@pytest.mark.parametrize("failure", [None, "npm", "version", "catalog", "settings"])
def test_staged_update_with_real_mcp_probe(managed, tmp_path, monkeypatch, failure):
    old_manifest = (managed / toolchains.MANIFEST).read_bytes()
    old_binary = toolchains.binary("rea")
    cfg = rea.settings()
    found = rea_updates.check()
    commands = []

    def npm(cmd, log, message):
        commands.append(cmd)
        assert cmd[-1] == "rea-agents@6.8.0" and "--engine-strict" in cmd
        if failure == "npm":
            raise RuntimeError("npm failed")
        stage = Path(cmd[cmd.index("--prefix") + 1])
        write_package(stage, "6.7.0" if failure == "version" else "6.8.0")

    monkeypatch.setattr(updates, "_stream", npm)
    script = tmp_path / "rea.py"
    script.write_text(FAKE)
    original_client = rea.Client

    def probe(argv, **kwargs):
        assert Path(argv[0]).exists() and argv[1] == "mcp"
        assert kwargs["env"] == cfg["env"] and kwargs["provider"] == cfg["provider"]
        client = original_client([sys.executable, str(script)])
        if failure == "catalog":
            client.tools = list
        if failure == "settings":
            monkeypatch.setattr(rea, "settings", lambda: {"source": "saved"})
        return client

    monkeypatch.setattr(rea, "Client", probe)
    if failure:
        with pytest.raises((ValueError, RuntimeError)):
            rea_updates.install(found, log=lambda line: None)
        assert (managed / toolchains.MANIFEST).read_bytes() == old_manifest
        assert not list((managed / "versions").iterdir())
        assert toolchains.binary("rea") == old_binary
    else:
        manifest = rea_updates.install(found, log=lambda line: None)
        new_binary = toolchains.binary("rea")
        assert new_binary != old_binary and new_binary.exists() and old_binary.exists()
        assert rea_updates.current()["version"] == "6.8.0"
        assert manifest["npm"] == "rea-agents@6.8.0"
        # Routine app setup must preserve the selected version rather than restore the baseline pin.
        toolchains.install("rea", log=lambda line: None)
        assert toolchains.binary("rea") == new_binary
    assert len(commands) == 1


def test_npx_fallback_can_become_managed(managed, monkeypatch):
    monkeypatch.setattr(rea, "settings", lambda: {"source": "npx"})
    found = rea_updates.check()
    assert found["available"] and found["can_apply"] and found["current"]["version"] == "6.7.0"


@pytest.mark.parametrize("version", ["6.8.0-beta.1", "latest", "6.8.0;echo bad", "../oops", ""])
def test_invalid_versions_are_rejected_before_install(managed, version):
    with pytest.raises(ValueError, match="invalid REA release"):
        rea_updates.install({"latest": {"version": version}})
    assert not (managed / "versions").exists()


def test_rea_updater_restarts_only_after_success(managed, monkeypatch):
    restarted = threading.Event()
    installed = []
    monkeypatch.setattr(updates, "restart", lambda port, secret: restarted.set())
    monkeypatch.setattr(updates.time, "sleep", lambda n: None)
    updater = updates.Updater(lambda: installed.append("stopped"), 6500, "secret", checker=rea_updates.check,
                              installer=lambda found, log: installed.append(found["latest"]["version"]))
    assert updater.status()["available"]
    updater.apply()
    assert restarted.wait(5)
    assert installed == ["6.8.0", "stopped"]
