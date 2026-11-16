"""A switch's jump table is checked like a constant: where each entry lands, not what its bytes say.

objdiff compares instructions, and a function whose `case` labels sit on the wrong blocks has the very same
instructions: only its jump table differs, and only the linked game shows it. Two small MIPS objects are assembled
here with the KMC toolchain's assembler, with a
function type and size as a split's macros give them: the original names the table's places after their addresses (as a split of
the game does), "yours" lets the assembler point at its .text section, as a compiler's output does.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from decompnet import toolchains
from decompnet.forks import literals

needs_as = pytest.mark.skipif(toolchains.installed("gcc-2.7.2-kmc") is None, reason="needs gcc-2.7.2-kmc")

FUNCTION = """\
.set noreorder
.set noat
.text
.globl f
.type f, @function
f:
    lui   $2, %hi({table})
    addiu $2, $2, %lo({table})
    sll   $4, $4, 2
    addu  $4, $4, $2
    lw    $2, 0($4)
    nop
    jr    $2
    nop
{l0}:
    jr    $31
    addiu $2, $0, 1
{l1}:
    jr    $31
    addiu $2, $0, 2
{l2}:
    jr    $31
    addiu $2, $0, 3
.size f, . - f
.rdata
{table}:
    .word {e0}
    .word {e1}
    .word {e2}
"""

ORIGINAL = {"table": "jtbl_80010000", "l0": "L80000020", "l1": "L80000028", "l2": "L80000030"}
YOURS = {"table": ".Ljt", "l0": ".L0", "l1": ".L1", "l2": ".L2"}
INSNS = [(0, "lui"), (4, "addiu"), (8, "sll"), (12, "addu"), (16, "lw"), (20, "nop"), (24, "jr"), (28, "nop")]


def _assemble(tmp: Path, name: str, labels: dict, entries: tuple[str, str, str]) -> Path:
    tc = toolchains.require("gcc-2.7.2-kmc")
    spec = toolchains.get("gcc-2.7.2-kmc")
    src = tmp / f"{name}.s"
    src.write_text(FUNCTION.format(**labels, e0=labels[entries[0]], e1=labels[entries[1]], e2=labels[entries[2]]))
    obj = tmp / f"{name}.o"
    cc = [a.replace("{tc}", str(tc)) for a in spec.cc]
    subprocess.run([*cc, "-G0", str(src), "-o", str(obj)], check=True, capture_output=True)
    return obj


@needs_as
def test_a_table_that_lands_in_the_same_places_matches(tmp_path):
    original = _assemble(tmp_path, "original", ORIGINAL, ("l0", "l1", "l2"))
    yours = _assemble(tmp_path, "yours", YOURS, ("l0", "l1", "l2"))
    assert literals.mismatches(original, yours, "f", INSNS) == []


@needs_as
def test_a_case_on_another_block_is_not_a_match(tmp_path):
    original = _assemble(tmp_path, "original", ORIGINAL, ("l0", "l1", "l2"))
    yours = _assemble(tmp_path, "yours", YOURS, ("l0", "l2", "l2"))  # case 1 on case 2's block
    found = literals.mismatches(original, yours, "f", INSNS)
    assert len(found) == 1 and isinstance(found[0], literals.JumpTable)
    assert found[0].theirs == [0x20, 0x28, 0x30] and found[0].yours == [0x20, 0x30, 0x30]
    said = found[0].describe()
    assert "entry 1 goes to +0x30, the original's to +0x28" in said and "case" in said


@needs_as
def test_a_table_only_one_side_has_gives_no_verdict(tmp_path):
    """An original object without the game's data (an extern table) can't be checked, and isn't."""
    original = _assemble(tmp_path, "original", ORIGINAL, ("l0", "l1", "l2"))
    stripped = tmp_path / "stripped.o"
    shutil.copy(original, stripped)
    text = (tmp_path / "original.s").read_text().split(".rdata")[0] + ".globl jtbl_80010000\n"
    (tmp_path / "noref.s").write_text(text.replace("jtbl_80010000:", ""))
    spec, tc = toolchains.get("gcc-2.7.2-kmc"), toolchains.require("gcc-2.7.2-kmc")
    cc = [a.replace("{tc}", str(tc)) for a in spec.cc]
    subprocess.run([*cc, "-G0", str(tmp_path / "noref.s"), "-o", str(stripped)], check=True, capture_output=True)
    yours = _assemble(tmp_path, "yours", YOURS, ("l0", "l2", "l2"))
    assert literals.mismatches(stripped, yours, "f", INSNS) == []
