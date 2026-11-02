import hashlib
import json
import os
import shutil
import tarfile

import pytest

from decompnet import project, toolchains


@pytest.fixture
def tc_root(tmp_path, monkeypatch):
    monkeypatch.setenv("DECOMPNET_TOOLCHAINS", str(tmp_path / "toolchains"))
    return tmp_path


def test_registry_is_pinned_and_labelled():
    reg = toolchains.registry()
    assert {"gcc-2.7.2-kmc", "ee-gcc-2.96", "ido-7.1", "m2c", "runtime-i386", "rea"} <= set(reg)
    for tc in reg.values():
        assert tc.license and tc.source, tc.id
        if not tc.proprietary:  # everything we install by default is verified against a pinned hash
            assert all(p["sha256"] for p in tc.parts) or tc.commit or tc.npm or tc.system, tc.id
            # a venv tool has no archive to hash, so the version has to be pinned in the spec itself
            if tc.pip:
                assert "==" in tc.pip or "@" in tc.pip, f"{tc.id}: unpinned python spec {tc.pip!r}"
            # nothing at all to install is only honest for a tool we detect on the host
            assert tc.system or tc.git or tc.npm or tc.pip or tc.parts or tc.via_wsl, tc.id
    assert reg["llvm-tools"].binary_name == "llvm-pdbutil"  # this key was once silently relocated by an insertion
    for tid in ("apktool", "adb"):  # engines REA will not fetch itself
        assert reg[tid].parts and all(p["sha256"] for p in reg[tid].parts), tid
    assert reg["apktool"].requires_java == 8
    assert (apk := reg["apktool"].launcher) and apk["jar"] == "apktool_3.0.3.jar"
    assert reg["adb"].binary_name == "adb" and len(reg["adb"].parts) == 2
    for tid in ("wabt", "jadx-mcp", "pwntools", "mitmproxy"):  # engines REA will not fetch itself
        assert reg[tid].kind == "tool" and not reg[tid].system, tid
    assert all(p["sha256"] for p in reg["wabt"].parts) and len(reg["wabt"].parts) == 3
    assert len(reg["jadx-mcp"].parts) == 1 and reg["jadx-mcp"].requires_java == 17
    assert reg["pwntools"].pip.startswith("pwntools==") and reg["mitmproxy"].pip.startswith("mitmproxy==")
    assert reg["ido-7.1"].proprietary and not reg["gcc-2.7.2-kmc"].proprietary
    assert reg["mingwxx-x86_64"].download.startswith("https://") and reg["clang-clxx-x86_64"].download.startswith("https://")
    assert {"msvc-x86", "msvcxx-x86", "msvc-x86_64", "msvcxx-x86_64"} <= set(reg)
    assert reg["msvc-x86"].flags[0].startswith("/") and reg["msvcxx-x86"].cmd[-1] == "{src}"
    assert reg["msvc-x86"].proprietary and reg["msvc-x86"].download.startswith("https://")
    assert reg["ee-gcc-2.96"].requires == ["runtime-i386"]
    assert reg["rea"].npm == "rea-agents@6.7.0" and reg["rea"].supported


def test_an_npm_toolchains_launcher_is_found(tc_root):
    """REA is installed with npm: its `rea` launcher lives in node_modules/.bin, not at the toolchain's root."""
    dest = toolchains.path("rea")
    (dest / "node_modules" / ".bin").mkdir(parents=True)
    (dest / toolchains.MANIFEST).write_text(json.dumps({"id": "rea"}))
    launcher = dest / "node_modules" / ".bin" / ("rea.cmd" if os.name == "nt" else "rea")
    launcher.write_text("#!/bin/sh\n")
    assert toolchains.binary("rea") == launcher
    assert toolchains.require("rea") == dest


def test_proprietary_toolchains_need_explicit_acceptance(tc_root):
    with pytest.raises(toolchains.ToolchainError, match="--accept-license"):
        toolchains.install("ido-7.1", log=lambda *_: None)
    assert toolchains.installed("ido-7.1") is None


def test_an_arm_computer_is_told_why_nothing_compiles(tc_root, monkeypatch):
    """Issue #18: on an ARM Linux computer the games' compilers (x86 programs) don't install: the error says why."""
    monkeypatch.setattr(toolchains._platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(toolchains.sys, "platform", "linux")
    assert toolchains.current_platform() == "linux-aarch64" and not toolchains.get("ee-gcc-2.96").supported
    with pytest.raises(toolchains.ToolchainError, match=r"linux-aarch64\): decomp.wtf needs a 64-bit Intel or AMD PC"):
        toolchains.install("ee-gcc-2.96", log=lambda *_: None)
    monkeypatch.setattr(toolchains._platform, "machine", lambda: "x86_64")
    assert toolchains.unsupported_cpu() is None


def test_a_wsl_toolchain_installed_at_the_legacy_path_is_found(tc_root, monkeypatch):
    """An install made before WSL toolchains moved under wsl/ must not make the project say "not installed"."""
    monkeypatch.setattr(toolchains, "current_platform", lambda: "windows-x86_64")
    monkeypatch.setattr(toolchains.wsl, "wanted", lambda: True)
    legacy = toolchains.root() / "gcc-2.7.2-kmc"
    legacy.mkdir(parents=True)
    (legacy / toolchains.MANIFEST).write_text(json.dumps({"id": "gcc-2.7.2-kmc"}))
    assert toolchains.installed("gcc-2.7.2-kmc") == {"id": "gcc-2.7.2-kmc"}
    assert toolchains.path("gcc-2.7.2-kmc") == legacy


def test_a_compiler_installed_off_path_is_found(tmp_path, monkeypatch):
    """MSYS2/WinLibs/LLVM put the compiler in a known folder; reopening the app before PATH catches up still works."""
    bindir = tmp_path / "mingw64" / "bin"
    bindir.mkdir(parents=True)
    exe = bindir / "x86_64-w64-mingw32-g++.exe"
    exe.write_text("")
    monkeypatch.setattr(toolchains, "_windows_dirs", lambda: [bindir])
    monkeypatch.setattr(toolchains.shutil, "which", lambda _name: None)
    tc = toolchains.get("mingwxx-x86_64")
    assert toolchains.system_binary(tc) == exe
    assert toolchains.installed(tc.id) == {"system": True, "id": tc.id}
    cfg = project.compiler_config(tmp_path, {"compiler": {"toolchain": "mingwxx-x86_64"}})
    assert cfg.cmd[0] == str(exe), "compilation must use the compiler found outside PATH"
    # A Linux compiler with a generic command must not pick up a Windows compiler from that folder.
    assert toolchains.system_binary(toolchains.get("gcc-linux-x86_64")) is None


def test_system_compilers_are_not_downloaded(tc_root, monkeypatch):
    """A MinGW/clang-cl/gcc the computer provides can't be fetched: say so instead of trying to download a build."""
    monkeypatch.setattr(toolchains.shutil, "which", lambda _name: None)
    # a Windows runner may well have MinGW in a standard install folder, which is not PATH: this test is about
    # what happens when the tool is nowhere at all
    monkeypatch.setattr(toolchains, "_windows_dirs", list)
    downloaded = []
    monkeypatch.setattr(toolchains, "_download", lambda *args: downloaded.append(args))
    with pytest.raises(toolchains.ToolchainError, match=r"not a download: x86_64-w64-mingw32-g\+\+ is not on PATH"):
        toolchains.install("mingwxx-x86_64", log=lambda *_: None)
    assert downloaded == []


def test_every_windows_system_tool_has_a_download():
    """All system cards a Windows user can act on link to where to get the tool (Linux compilers use apt/zypper)."""
    for tc in toolchains.registry().values():
        if tc.system and not tc.platform.startswith("Linux"):
            assert tc.download.startswith("https://"), tc.id


def test_system_download_links_are_windows_only(monkeypatch):
    monkeypatch.setattr(toolchains, "current_platform", lambda: "linux-x86_64")
    rows = {r["id"]: r for r in toolchains.status()}
    assert rows["mingwxx-x86_64"]["download"] == ""
    monkeypatch.setattr(toolchains, "current_platform", lambda: "windows-x86_64")
    rows = {r["id"]: r for r in toolchains.status()}
    assert rows["mingwxx-x86_64"]["download"] == "https://winlibs.com/"
    assert rows["llvm-tools"]["download"].startswith("https://github.com/llvm/llvm-project/releases/")


def _fake(monkeypatch, tc_root, sha):
    src = tc_root / "src"
    src.mkdir()
    (src / "cc").write_text("#!/bin/sh\n")
    archive = tc_root / "fake.tar.gz"
    with tarfile.open(archive, "w:gz") as t:
        t.add(src / "cc", arcname="cc")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    fake = toolchains.Toolchain(id="fake", name="Fake", license="MIT", source="test",
                                parts=[{"url": archive.as_uri(), "sha256": sha or digest}])
    monkeypatch.setattr(toolchains, "registry", lambda: {"fake": fake})
    return digest


def test_install_verifies_checksums(tc_root, monkeypatch):
    _fake(monkeypatch, tc_root, sha="0" * 64)
    with pytest.raises(toolchains.ToolchainError, match="checksum mismatch"):
        toolchains.install("fake", log=lambda *_: None)
    assert not toolchains.path("fake").exists()


def test_a_jar_only_tool_gets_the_command_the_engine_asks_for(tc_root, monkeypatch):
    """REA's own remediation asks for "an absolute apktool launcher (the upstream wrapper or a java -jar script)".
    A bare .jar on disk is neither, so pointing REA at the download would fail inside a tool call; the install has
    to write the wrapper. A pasted table header once moved llvm-tools' binary_name into the entry below it and
    TOML parsed happily, which is why the registry test pins that key too."""
    jar = tc_root / "apktool_9.9.9.jar"
    jar.write_bytes(b"PK")
    digest = hashlib.sha256(jar.read_bytes()).hexdigest()
    fake = toolchains.Toolchain(id="apk", name="Apk", license="Apache-2.0", source="test",
                                parts=[{"url": jar.as_uri(), "sha256": digest, "binary": "apktool_9.9.9.jar"}],
                                launcher={"script": "apktool", "jar": "apktool_9.9.9.jar"})
    monkeypatch.setattr(toolchains, "registry", lambda: {"apk": fake})
    toolchains.install("apk", log=lambda *_: None)
    sh = toolchains.path("apk") / "bin" / "apktool"
    assert sh.exists() and os.access(sh, os.X_OK) and "java -jar" in sh.read_text(encoding="utf-8")
    assert "java -jar" in (toolchains.path("apk") / "bin" / "apktool.bat").read_text(encoding="utf-8")


def test_install_records_a_manifest(tc_root, monkeypatch):
    digest = _fake(monkeypatch, tc_root, sha=None)
    toolchains.install("fake", log=lambda *_: None)
    m = json.loads((toolchains.path("fake") / toolchains.MANIFEST).read_text())
    assert m["parts"][0]["sha256"] == digest and (toolchains.path("fake") / "cc").exists()
    assert toolchains.require("fake") == toolchains.path("fake")


def test_zst_archives_need_no_zstd_command(tmp_path, monkeypatch):
    """The 32-bit runtime EE-GCC needs is a .tar.zst: GNU tar would run the zstd command for it, which many systems
    don't have (the install then failed with "command failed (2)")."""
    zstandard = pytest.importorskip("zstandard")
    if not shutil.which("tar"):
        pytest.skip("needs tar")
    (tmp_path / "src" / "usr" / "lib32").mkdir(parents=True)
    (tmp_path / "src" / "usr" / "lib32" / "ld-linux.so.2").write_bytes(b"\x7fELF")
    plain = tmp_path / "pkg.tar"
    with tarfile.open(plain, "w") as t:
        t.add(tmp_path / "src" / "usr", arcname="usr")
    archive = tmp_path / "pkg.tar.zst"
    archive.write_bytes(zstandard.ZstdCompressor().compress(plain.read_bytes()))
    if os.name != "nt":  # a zstd command that always fails, as a missing one does: tar must not need it
        (tmp_path / "bin").mkdir()
        (tmp_path / "bin" / "zstd").write_text("#!/bin/sh\nexit 2\n")
        (tmp_path / "bin" / "zstd").chmod(0o755)
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    dest = tmp_path / "out"
    dest.mkdir()
    toolchains._extract(archive, dest, "usr/lib32")
    assert (dest / "ld-linux.so.2").read_bytes() == b"\x7fELF"
    assert not list(tmp_path.glob("tmp*"))  # no leftovers


def test_the_engines_rea_uses_can_be_installed_on_windows(monkeypatch):
    """Windows is a supported host, so every engine we hand to REA needs a Windows answer: a native part, a
    platform-agnostic JAR, or an interpreter we build. A tool with none of those only shows up as an Install
    button that refuses once pressed, which is the same silence as a missing engine."""
    monkeypatch.setattr(toolchains, "current_platform", lambda: "windows-x86_64")
    for tid in ("wabt", "jadx-mcp", "pwntools", "mitmproxy", "apktool", "adb"):
        tc = toolchains.get(tid)
        assert tc.supported, tid
        assert bool(tc.parts_for()) or bool(tc.pip) or bool(tc.git), tid
    # a part with no `platforms` list is platform-agnostic and must not be filtered out on a Windows host
    assert len(toolchains.get("jadx-mcp").parts_for()) == 1
    assert len(toolchains.get("wabt").parts_for()) == 1
    assert toolchains.get("wabt").parts_for()[0]["url"].endswith("windows-x64.tar.gz")
    assert toolchains.get("adb").parts_for()[0]["url"].endswith("win.zip")
