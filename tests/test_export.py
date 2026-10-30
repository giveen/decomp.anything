"""Exported projects rebuild the game byte for byte: the base for ports and mods.

For each homemade demo (N64, PlayStation, PlayStation 2), match the game's functions the way an agent would,
export the project, build it, then build it again from a clean tree (as from a fresh clone: only the sources and
your own copy of the game), and check that nothing made from the game would be committed.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from elftools.elf.elffile import ELFFile

from decompnet import toolchains
from decompnet.app.engine import RomNetwork
from decompnet.forks import csource

ROOT = Path(__file__).resolve().parent.parent
GAME = ROOT / "projects" / "demo-n64" / "original"
FUNCTIONS = ["clamp_s32", "abs_s32", "player_take_damage", "rng_next", "count_alive_entities", "entity_update_physics",
             "entity_default_hp", "entity_spawn", "add_score", "lerp", "checksum_save", "entity_collides", "game_tick"]
DEMOS = {
    "n64": (ROOT / "src/decompnet/app/static/demo/demo-quest-64.z64", ROOT / "projects/demo-rom/build/demo-quest-64.elf",
            "gcc-2.7.2-kmc", ROOT / "projects/demo-rom/build.py"),
    "psx": (ROOT / "projects/demo-psx/build/DEMOQ_PS1.EXE", ROOT / "projects/demo-psx/build/demoq_ps1.elf",
            "gcc-2.7.2-psx", ROOT / "projects/demo-psx/build.py"),
    "ps2": (ROOT / "projects/demo-ps2/build/SLUS_999.99", ROOT / "projects/demo-ps2/build/demoq_ps2.elf",
            "ee-gcc-2.96", ROOT / "projects/demo-ps2/build.py"),
}


def _ready(platform: str) -> bool:
    _, _, tc, _ = DEMOS[platform]
    return (toolchains.installed(tc) is not None and toolchains.installed("binutils-mips") is not None
            and shutil.which("ld.lld") is not None)


def _decompile(tmp: Path, platform: str, *, bundle_mode: str = ""):
    game, elf, _, build = DEMOS[platform]
    if not elf.exists() or not game.exists():
        subprocess.run([sys.executable, str(build)], check=True)
    net = RomNetwork(tmp / "app.db", tmp)
    slug = net.import_rom(game, confirmed=True, log=lambda *_: None)
    agent, _ = net.local_agent("agent")
    with open(elf, "rb") as fh:
        syms = {s.name: s["st_value"] for s in ELFFile(fh).get_section_by_name(".symtab").iter_symbols()}
    pid = net.project_row(slug)["id"]
    for name in FUNCTIONS:  # functions the splitter merged into the one before (after one that never returns)
        row = net.store.one("SELECT name, vram FROM functions WHERE project_id=? AND vram<=? AND vram+size>?",
                            pid, syms[name], syms[name])
        if row and row["vram"] != syms[name]:
            net.split_function(agent, slug, row["name"], f"0x{syms[name]:08X}", name)
    header = "\n".join(ln for ln in (GAME / "game.h").read_text().splitlines()
                       if not ln.startswith("#include") and not re.match(r"typedef \w+( \w+)? [su]\d+;|typedef \w+ f\d+;", ln))
    if platform != "n64":  # small globals are $gp-relative: tentative definitions, not extern
        header = re.sub(r"^extern (s32|u32) (g_\w+);", r"\1 \2;", header, flags=re.MULTILINE)
    assert net.edit_context(agent, slug, "", header, "the game's types").startswith("✅")
    table = net.symbols(pid)
    for name in [*FUNCTIONS, "g_player", "g_entities", "g_score", "g_frame_count", "g_rng_state",
                 "__subsf3", "__mulsf3", "__addsf3", "__muldi3"]:
        if name in syms:
            current, offset = table.name_at(syms[name])
            if offset:
                current = f"D_{syms[name]:08X}"
            if current != name:
                net.rename_symbol(agent, slug, current, name)
                table = net.symbols(pid)
    source = (GAME / "game.c").read_text()
    bundle = "typedef s32 ExportValue;\n" + "\n".join(csource.get(source, n) for n in FUNCTIONS[:3])
    bundle = bundle.replace("s32 abs_s32(", "ExportValue abs_s32(")
    if bundle_mode == "macro":
        bundle = "#define MATCHED_FUNCTION(name) s32 name\n" + bundle.replace("s32 clamp_s32(", "MATCHED_FUNCTION(clamp_s32)(")
    for name in FUNCTIONS:
        submitted = bundle if bundle_mode and name in FUNCTIONS[:3] else csource.get(source, name)
        assert net.attempt(agent, name, submitted, slug)["matched"], name
    return net, slug


@pytest.mark.parametrize("platform", ["n64", "psx", "ps2"])
def test_export_rebuilds_the_game(tmp_path, platform):
    if not _ready(platform):
        pytest.skip(f"needs the {platform} toolchain and binutils-mips")
    net, slug = _decompile(tmp_path, platform)
    out = net.export_rom(slug, log=lambda *_: None)
    assert out["identical"], out["log"]
    assert out["matched"] == len(FUNCTIONS) and not out["fallback"]
    tree = Path(out["path"])
    assert len(list((tree / "src").glob("*/*.c"))) == len(FUNCTIONS)

    # a fresh clone: no assembly, assets or build output, only the sources and your copy of the game
    for generated in ("asm", "assets", "build"):
        shutil.rmtree(tree / generated, ignore_errors=True)
    res = subprocess.run([sys.executable, "build.py"], cwd=tree, capture_output=True, text=True)
    assert "identical" in res.stdout, res.stdout + res.stderr

    # nothing made from the game would be committed
    if shutil.which("git"):
        subprocess.run(["git", "init", "-q"], cwd=tree, check=True)
        subprocess.run(["git", "add", "-A"], cwd=tree, check=True)
        files = subprocess.run(["git", "ls-files", "--cached"], cwd=tree, capture_output=True, text=True).stdout.split()
        assert not [f for f in files if f.startswith(("asm/", "assets/", "build/", "baserom"))], files
        assert "build.py" in files and "build.json" in files


def test_check_records_a_function_written_outside(tmp_path):
    if not _ready("n64"):
        pytest.skip("needs the n64 toolchain and binutils-mips")
    net, slug = _decompile(tmp_path, "n64")
    tree = Path(net.export_rom(slug, log=lambda *_: None)["path"])
    def py(*a):
        return subprocess.run([sys.executable, "build.py", *a], cwd=tree, capture_output=True, text=True)
    (tree / "src" / "main" / "lerp.c").unlink()  # back to assembly
    assert "identical" in py().stdout
    (tree / "src" / "main" / "lerp.c").write_text('#include "common.h"\n\nf32 lerp(f32 a, f32 b, f32 t) {\n    return a;\n}\n')
    assert "differs" in py().stdout
    assert py("check", "lerp").returncode == 1
    (tree / "src" / "main" / "lerp.c").write_text('#include "common.h"\n\n' + csource.get((GAME / "game.c").read_text(), "lerp"))
    assert py("check", "lerp").returncode == 0
    assert "identical" in py().stdout


def test_export_isolates_functions_from_a_shared_submission(tmp_path):
    if not _ready("n64"):
        pytest.skip("needs the n64 toolchain and binutils-mips")
    net, slug = _decompile(tmp_path, "n64", bundle_mode="shared")
    saved = [r["matched_source"] for r in net.store.all(
        "SELECT matched_source FROM functions WHERE project_id=? AND name IN (?,?,?)",
        net.project_row(slug)["id"], *FUNCTIONS[:3])]
    assert len(saved) == 3 and len(set(saved)) == 1
    out = net.export_rom(slug, log=lambda *_: None)
    assert out["identical"], out["log"]
    assert out["matched"] == len(FUNCTIONS) and not out["fallback"]
    tree = Path(out["path"])
    for name in FUNCTIONS[:3]:
        exported = (tree / "src" / "main" / f"{name}.c").read_text()
        assert csource.get(exported, name) is not None
        assert all(csource.get(exported, other) is None for other in FUNCTIONS[:3] if other != name)
    # Removing the neighbors must retain the shared typedef needed by abs_s32.
    assert "ExportValue abs_s32(" in (tree / "src/main/abs_s32.c").read_text()
    for generated in ("asm", "assets", "build"):
        shutil.rmtree(tree / generated)
    rebuilt = subprocess.run([sys.executable, "build.py"], cwd=tree, capture_output=True, text=True)
    assert rebuilt.returncode == 0 and "identical" in rebuilt.stdout, rebuilt.stdout + rebuilt.stderr
    assert [r["matched_source"] for r in net.store.all(
        "SELECT matched_source FROM functions WHERE project_id=? AND name IN (?,?,?)",
        net.project_row(slug)["id"], *FUNCTIONS[:3])] == saved


def test_export_keeps_assembly_when_a_matched_function_crosses_a_splat_data_boundary(tmp_path):
    if not _ready("n64"):
        pytest.skip("needs the n64 toolchain and binutils-mips")
    net, slug = _decompile(tmp_path, "n64")
    proj = net.project_row(slug)
    fn = net.store.one("SELECT * FROM functions WHERE project_id=? AND name='game_tick'", proj["id"])
    config = next(Path(proj["root"]).glob("*.yaml"))
    data = yaml.safe_load(config.read_text())
    # Reproduce an inferred text/data boundary that leaves the final delay slot in a data block.
    for segment in data["segments"]:
        if isinstance(segment, dict) and segment.get("type") == "code" and segment.get("name") == "main":
            for sub in segment["subsegments"]:
                if isinstance(sub, list) and len(sub) > 1 and sub[1] == "data":
                    sub[0] = fn["rom_offset"] + fn["size"] - 4
    config.write_text(yaml.safe_dump(data, sort_keys=False))
    out = net.export_rom(slug, log=lambda *_: None)
    assert out["identical"], out["log"]
    assert ("game_tick", "its code overlaps the next splat block") in out["fallback"]
    assert not (Path(out["path"]) / "src/main/game_tick.c").exists()
    saved = net.store.one("SELECT status,matched_source FROM functions WHERE id=?", fn["id"])
    assert saved["status"] == "matched" and saved["matched_source"] == fn["matched_source"]


def test_export_keeps_assembly_when_bundle_definitions_cannot_be_isolated(tmp_path):
    if not _ready("n64"):
        pytest.skip("needs the n64 toolchain and binutils-mips")
    net, slug = _decompile(tmp_path, "n64", bundle_mode="macro")
    out = net.export_rom(slug, log=lambda *_: None)
    assert out["identical"], out["log"]
    assert {name for name, _ in out["fallback"]} == set(FUNCTIONS[:3])
    assert all("cannot isolate" in reason for _, reason in out["fallback"])
    assert out["matched"] == len(FUNCTIONS) - 3
