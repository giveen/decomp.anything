"""PE/ELF exports preserve translation units and can be linked without duplicate definitions."""

import json
import os
import shutil
import struct
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from decompnet import coffobj, toolchains
from decompnet.app.engine import RomNetwork
from decompnet.rom.export_pc import ExportPcError, export_pc

BODY = """typedef int ExportValue;
__attribute__((noinline)) ExportValue first(int x) { return x + 3; }
__attribute__((noinline)) int second(int x) { return x * 5; }
"""
TEMPLATE = "template<typename T> __attribute__((noinline)) T offset(T x) { return x + 3; }\n"
CPP_FIRST = "__attribute__((noinline)) int first(int x) { return offset(x); }\n"
CPP_SECOND = "__attribute__((noinline)) int second(int x) { return offset(x * 5); }\n"
MACRO_BODY = BODY.replace("__attribute__((noinline)) int second(int x) { return x * 5; }",
                          "#define DEFINE_SECOND(name) __attribute__((noinline)) int name(int x) { return x * 5; }\n"
                          "DEFINE_SECOND(second)")


def project(tmp_path, platform, language="C", bits=64, body=BODY):
    prefix = ("mingwxx" if language == "CXX" else "mingw") if platform == "windows" else ("gxx-linux" if language == "CXX"
                                                                                       else "gcc-linux")
    tid = prefix + ("-x86" if bits == 32 else "-x86_64")
    tc = toolchains.get(tid)
    compiler = shutil.which(tc.cmd[0])
    if not compiler:
        pytest.skip(f"needs {tc.cmd[0]}")
    if platform == "linux" and bits == 32:
        probe = subprocess.run([compiler, "-m32", "-x", "c++" if language == "CXX" else "c", "-", "-o", str(tmp_path / "probe")],
                               input="int main(void) { return 0; }", capture_output=True, text=True)
        if probe.returncode:
            pytest.skip("needs 32-bit host compiler libraries")
    source = tmp_path / ("target.cpp" if language == "CXX" else "target.c")
    scope = "demo::" if language == "CXX" else ""
    submitted = "namespace demo {\n" + body + "}\n" if language == "CXX" else body
    entry = (f'extern "C" void _start(void) {{ {scope}first(1); {scope}second(2); }}' if language == "CXX"
             else "void _start(void) { first(1); second(2); }") if platform == "windows" else \
        f"int main(void) {{ return {scope}first(1) + {scope}second(2); }}"
    source.write_text(submitted + entry)
    target = tmp_path / ("target.exe" if platform == "windows" else "target")
    flags = ["-nostdlib", "-Wl,--entry,_start", "-Wl,--subsystem,console"] if platform == "windows" else ["-no-pie"]
    if platform == "linux" and bits == 32:
        flags.append("-m32")
    subprocess.run([compiler, *tc.flags, *flags, "-o", str(target), str(source)], check=True, capture_output=True)
    net = RomNetwork(tmp_path / "app.db", tmp_path / "work")
    slug = net.import_rom(target.read_bytes(), toolchain=tid, confirmed=True, name=target.name, log=lambda *_: None)
    assert net.overview(slug)["compiler"]["toolchain"] == tid
    assert net.cfg(net.project_row(slug))["arch"] == ("x86" if bits == 32 else "x86_64")
    rows = net.store.all("SELECT name FROM functions WHERE project_id=?", net.project_row(slug)["id"])
    names = [next(r["name"] for r in rows if name in r["name"]) for name in ("first", "second")]
    agent, _ = net.local_agent("export-tester")
    return net, slug, agent, compiler, submitted, names


def build_and_judge(net, slug, out, compiler, bits=64):
    cfg = net.cfg(net.project_row(slug))["compiler"]
    objects = []
    for path in sorted((out / "src").iterdir()):
        obj = out / (path.stem + ".o")
        subprocess.run([compiler, *(["-m32"] if bits == 32 else []), *cfg["flags"], "-I", str(out / "include"),
                        "-c", str(path), "-o", str(obj)],
                       check=True, capture_output=True)
        objects.append(str(obj))
    # CMake's OBJECT library alone does not detect duplicate symbols; a real partial link does.
    subprocess.run([compiler, *( ["-m32"] if bits == 32 else []), "-nostdlib", "-no-pie", "-r", *objects,
                    "-o", str(out / "combined.o")], check=True, capture_output=True)
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent / "src"))
    judged = subprocess.run([sys.executable, "judge.py", "--all"], cwd=out, env=env, capture_output=True, text=True)
    assert judged.returncode == 0, judged.stdout + judged.stderr
    assert judged.stdout.count("MATCH") == 2


@pytest.mark.parametrize("platform", ["windows", "linux"])
@pytest.mark.parametrize("language", ["C", "CXX"])
@pytest.mark.parametrize("bits", [32, 64])
def test_shared_pc_submission_is_one_verified_translation_unit(tmp_path, platform, language, bits):
    body = TEMPLATE + CPP_FIRST + CPP_SECOND if language == "CXX" else MACRO_BODY
    net, slug, agent, compiler, submitted, names = project(tmp_path, platform, language, bits, body)
    for name in names:
        result = net.compile_and_diff(agent, name, submitted, slug)
        assert "MATCH" in result, result
    result = net.export_rom(slug, log=lambda *_: None)
    out = Path(result["path"])
    functions = json.loads((out / "build.json").read_text())["functions"]
    assert len(functions) == 2 and len({f["source"] for f in functions}) == 1
    assert len(list((out / "src").iterdir())) == 1
    assert len(json.loads((out / "compile_commands.json").read_text())) == 1
    assert submitted.strip() in (out / functions[0]["source"]).read_text()
    build_and_judge(net, slug, out, compiler, bits)


@pytest.mark.parametrize("platform", ["windows", "linux"])
def test_cpp_template_helpers_can_coalesce_across_distinct_sources(tmp_path, platform):
    net, slug, agent, compiler, _submitted, names = project(tmp_path, platform, "CXX", body=TEMPLATE + CPP_FIRST + CPP_SECOND)
    for name, definition in zip(names, (CPP_FIRST, CPP_SECOND), strict=True):
        submitted = "namespace demo {\n" + TEMPLATE + definition + "}\n"
        result = net.compile_and_diff(agent, name, submitted, slug)
        assert "MATCH" in result, result
    out = Path(net.export_rom(slug, log=lambda *_: None)["path"])
    assert len(list((out / "src").iterdir())) == 2
    build_and_judge(net, slug, out, compiler)


@pytest.mark.parametrize("platform", ["windows", "linux"])
def test_weak_matched_functions_cannot_be_replaced_by_another_submission(tmp_path, platform):
    body = BODY.replace("__attribute__((noinline))", "inline __attribute__((used,noinline))")
    net, slug, agent, _compiler, submitted, names = project(tmp_path, platform, "CXX", body=body)
    # Each source matches its selected function but carries a different implementation of its neighbor.
    for name, source in zip(names, (submitted.replace("x * 5", "x * 6"), submitted.replace("x + 3", "x + 4")), strict=True):
        result = net.compile_and_diff(agent, name, source, slug)
        assert "MATCH" in result, result
    with pytest.raises(ExportPcError, match="conflicting definitions"):
        export_pc(net, slug, tmp_path / "export")


def test_coff_noduplicates_template_is_not_treated_as_coalescible(tmp_path, monkeypatch):
    net, slug, agent, _compiler, _submitted, names = project(tmp_path, "windows", "CXX", body=TEMPLATE + CPP_FIRST + CPP_SECOND)
    for name, definition in zip(names, (CPP_FIRST, CPP_SECOND), strict=True):
        assert "MATCH" in net.compile_and_diff(agent, name, "namespace demo {\n" + TEMPLATE + definition + "}\n", slug)
    compile_source = net._compile

    def noduplicates(proj, context, source):
        result = compile_source(proj, context, source)
        data = bytearray(result.obj)
        _, sections, symbols = coffobj._read(data)
        symptr = struct.unpack_from("<I", data, 8)[0]
        changed = False
        for symbol in symbols:
            if (0 < symbol.section <= len(sections) and symbol.storage == coffobj.IMAGE_SYM_CLASS_STATIC
                    and symbol.type == 0 and "offset" in symbol.name
                    and sections[symbol.section - 1].comdat_selection == 2):
                data[symptr + 18 * (symbol.index + 1) + 14] = 1  # IMAGE_COMDAT_SELECT_NODUPLICATES
                changed = True
        assert changed
        return replace(result, obj=bytes(data))

    monkeypatch.setattr(net, "_compile", noduplicates)
    with pytest.raises(ExportPcError, match=r"conflicting definitions.*offset"):
        export_pc(net, slug, tmp_path / "export")


@pytest.mark.parametrize("platform", ["windows", "linux"])
def test_overlapping_c_submissions_are_isolated_and_reverified(tmp_path, platform):
    net, slug, agent, compiler, submitted, names = project(tmp_path, platform)
    for i, name in enumerate(names):
        result = net.compile_and_diff(agent, name, submitted + f"\n/* revision {i} */", slug)
        assert "MATCH" in result, result
    out = Path(net.export_rom(slug, log=lambda *_: None)["path"])
    assert len(list((out / "src").iterdir())) == 2
    build_and_judge(net, slug, out, compiler)


@pytest.mark.parametrize("platform", ["windows", "linux"])
@pytest.mark.parametrize("language,body", [("CXX", BODY), ("C", "int duplicate = 7;\n" + BODY)])
def test_unresolved_overlap_keeps_the_existing_export(tmp_path, platform, language, body):
    net, slug, agent, _compiler, submitted, names = project(tmp_path, platform, language, body=body)
    for i, name in enumerate(names):
        result = net.compile_and_diff(agent, name, submitted + f"\n/* revision {i} */", slug)
        assert "MATCH" in result, result
    out = tmp_path / "previous-export"
    out.mkdir()
    (out / "user.c").write_text("preserve previous work")
    with pytest.raises(ExportPcError, match="conflicting definitions"):
        export_pc(net, slug, out)
    assert (out / "user.c").read_text() == "preserve previous work"


@pytest.mark.parametrize("platform", ["windows", "linux"])
def test_changed_source_must_match_before_replacing_export(tmp_path, platform):
    net, slug, agent, _compiler, submitted, names = project(tmp_path, platform)
    assert "MATCH" in net.compile_and_diff(agent, names[0], submitted, slug)
    with net.store.tx() as db:
        db.execute("UPDATE functions SET matched_source=? WHERE project_id=? AND name=?",
                   (submitted.replace("x + 3", "x + 4"), net.project_row(slug)["id"], names[0]))
    out = tmp_path / "previous-export"
    out.mkdir()
    (out / "user.c").write_text("preserve previous work")
    with pytest.raises(ExportPcError, match="no longer matches"):
        export_pc(net, slug, out)
    assert (out / "user.c").exists()
