"""What a near miss differs in: the kinds of difference a compiler or a translation unit makes, apart from the rest.

The same C comes out as different MIPS under another compiler version, another assembler profile or another set of
file-level declarations: a global read through $gp or through lui/%lo, the %hi/%lo pair split another way, a delay slot
filled or left as a nop, a nop after a load, the same values in other registers. An agent that reads those rows as
"my C is wrong" rewrites code that was right. This reads a diff's rows (objdiff's: marker, target, yours) and says
which kinds are there, so the next step can be the compiler matrix (forkmatrix.py) instead of another rewrite.

It is a reading of the rows, not a proof: register allocation and instruction order also follow from how the C is
written. The verdict says how much of the diff a compiler could explain, and the matrix is what tests it.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field

# These are observations, not diagnoses of the C or the toolchain.
KINDS = {
    "gp_rel": ("GP-relative addressing",
               "$gp usage differs; small-data flags and declarations are possible causes; check the referenced global"),
    "hi_lo": ("HI/LO address splitting",
              "address construction differs; check symbols, addends and types as well as assembler/compiler settings"),
    "delay_slot": ("delay slot scheduling",
                   "instructions around a branch slot differ; scheduling is possible, but side effects may differ too"),
    "load_delay": ("load delay handling",
                   "a nop after a load differs; check dependencies as well as the assembler profile"),
    "nop": ("additional NOP instructions", "nops on one side only, away from loads and branches"),
    "regalloc": ("register allocation",
                 "the same instructions with other registers: a compiler version does this, and so does a "
                 "temporary or the order of declarations in the C"),
    "reorder": ("instruction order",
                "the same instructions in another order: a compiler's scheduling, or the order of statements"),
    "stack": ("stack layout", "only stack offsets differ: a local or the frame is another size"),
    "other": ("unexplained code differences", "check operations, constants, references and control flow in the C"),
    "encoding": ("encoding or relocation differences", "the displayed instructions agree, but the diff marks a difference"),
}
STRONG = ("gp_rel", "hi_lo", "delay_slot", "load_delay", "nop")  # candidates for a controlled compiler experiment
WEAK = ("regalloc", "reorder")  # a compiler or the C's shape, either one

_BRANCH = re.compile(r"^(b[a-z0-9]*|j|jal|jr|jalr)$")
_LOAD = re.compile(r"^l(w|h|hu|b|bu|wl|wr|wc[12])$")
_REG = re.compile(r"^\$?(zero|at|v[01]|a[0-3]|t\d|s[0-8]|k[01]|gp|sp|fp|ra|f\d+|r\d+)$")
_TOKEN = re.compile(r"%\w+\([^)]*\)|[\w.$+-]+")
_NUM = re.compile(r"^-?(0x[0-9a-f]+|\d+)$", re.IGNORECASE)
_GP = re.compile(r"%gp_rel\(|\(\$?gp\)|\b\$?gp\b")
_HILO = re.compile(r"%(hi|lo)\(")


def _mnemonic(text: str) -> str:
    return text.split(None, 1)[0].lower() if text.strip() else ""


def _is_nop(text: str) -> bool:
    return text.strip().lower() == "nop"


def _norm(text: str) -> str:
    return " ".join(text.replace("$", "").split()).lower()


@dataclass
class Classes:
    counts: dict[str, int] = field(default_factory=dict)  # kind -> differing rows of that kind
    total: int = 0  # differing rows
    gp: tuple[int, int] = (0, 0)  # gp-relative accesses in (the original, yours)
    examples: dict[str, list[dict]] = field(default_factory=dict)

    @property
    def verdict(self) -> str:
        """Experiment routing only, never a match verdict or proof of a cause. Empty rows say nothing about bytes."""
        if not self.total:
            return "unknown"
        strong = sum(self.counts.get(k, 0) for k in STRONG)
        weak = sum(self.counts.get(k, 0) for k in WEAK)
        rest = self.total - strong - weak
        if rest == 0 and strong:
            return "compiler"
        if rest == 0:
            return "maybe"
        return "mixed" if strong or weak else "source"

    def summary(self) -> str:
        """One line: `GP-relative addressing x3, register allocation x2`."""
        return ", ".join(f"{KINDS[k][0]} x{n}" for k, n in sorted(self.counts.items(), key=lambda kv: -kv[1]))

    def note(self, matrix: str = "ask") -> str:
        """What the differences are and what to do about them; empty for a match. `matrix` is where the project's
        compiler matrix stands: "ask" (it has one: suggest it), "none" (it declares none), "done" (it just ran and
        closed nothing)."""
        if not self.total:
            return ""
        lines = [f"Kinds of difference in the {self.total} differing row(s):"]
        for kind, n in sorted(self.counts.items(), key=lambda kv: -kv[1]):
            name, why = KINDS[kind]
            extra = ""
            if kind == "gp_rel" and self.gp[0] != self.gp[1]:
                extra = f" (the original reads {self.gp[0]} through $gp, yours {self.gp[1]})"
            lines.append(f"- {name} x{n}{extra}: {why}")
            for example in self.examples.get(kind, [])[:2]:
                lines.append(f"  row {example['row']}: {example['target'] or '(absent)'} -> {example['base'] or '(absent)'}")
        verdict = self.verdict
        lines.append("These patterns are hypotheses, not proof of compiler causation or correct C. "
                     "A configuration can reduce differences while a source error remains. Only byte-for-byte verification declares MATCH.")
        if matrix == "done":
            lines.append("No tested configuration verified a match. Review the remaining evidence in both the C and the build settings.")
        elif matrix == "none" and verdict != "source":
            lines.append("This project declares no compiler matrix to test these hypotheses.")
        elif matrix == "ask" and verdict != "source":
            lines.append("compiler_matrix tests the same C under other profiles and contexts; it does not certify a partial improvement.")
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps({"counts": self.counts, "total": self.total, "gp": list(self.gp), "verdict": self.verdict,
                           "examples": self.examples})


def from_json(text: str | None) -> Classes:
    try:
        d = json.loads(text or "{}")
    except ValueError:
        d = {}
    gp = d.get("gp") or [0, 0]
    return Classes({str(k): int(v) for k, v in (d.get("counts") or {}).items() if k in KINDS}, int(d.get("total") or 0),
                   (int(gp[0]), int(gp[1])), d.get("examples") or {})


def _only_registers_differ(a: str, b: str) -> bool:
    ta, tb = _TOKEN.findall(_norm(a)), _TOKEN.findall(_norm(b))
    if len(ta) != len(tb) or not ta or ta[0] != tb[0]:
        return False
    diffs = [(x, y) for x, y in zip(ta, tb, strict=True) if x != y]
    return bool(diffs) and all(_REG.match(x) and _REG.match(y) for x, y in diffs)


def _only_stack_differs(a: str, b: str) -> bool:
    ta, tb = _TOKEN.findall(_norm(a)), _TOKEN.findall(_norm(b))
    if len(ta) != len(tb) or not ta or ta[0] != tb[0] or "sp" not in ta:
        return False
    diffs = [(x, y) for x, y in zip(ta, tb, strict=True) if x != y]
    return bool(diffs) and all(_NUM.match(x) and _NUM.match(y) for x, y in diffs)


def classify(rows: list[tuple[str, str, str]]) -> Classes:
    """The kinds of difference in a diff's rows: (marker, the original's instruction, yours), aligned as objdiff
    gives them (an instruction only one side has leaves the other side empty)."""
    rows = [(m, t or "", b or "") for m, t, b in rows]
    # Never erase branch targets because the lengths differ: a real control-flow error looks the same.
    differing = [i for i, (m, _t, _b) in enumerate(rows) if m.strip()]
    if not differing:
        return Classes()
    gp_t = sum(1 for _, t, _ in rows if _GP.search(t))
    gp_b = sum(1 for _, _, b in rows if _GP.search(b))
    hi_t = sum(1 for _, t, _ in rows if _HILO.search(t))
    hi_b = sum(1 for _, _, b in rows if _HILO.search(b))

    def prev(side: int, i: int) -> str:
        """The instruction before row i on one side (1: the original, 2: yours), skipping rows it has nothing in."""
        for k in range(i - 1, -1, -1):
            if rows[k][side].strip():
                return rows[k][side]
        return ""

    # the same instruction on both sides of the diff, in different rows: moved, not changed
    left = Counter(_norm(rows[i][1]) for i in differing if rows[i][1].strip() and not _is_nop(rows[i][1]))
    right = Counter(_norm(rows[i][2]) for i in differing if rows[i][2].strip() and not _is_nop(rows[i][2]))
    moved = left & right

    counts: Counter[str] = Counter()
    examples: dict[str, list[dict]] = {}
    for i in differing:
        _, t, b = rows[i]
        nt, nb = _is_nop(t), _is_nop(b)
        refs_t = re.findall(r"%\w+\(([^)]+)\)", t)
        refs_b = re.findall(r"%\w+\(([^)]+)\)", b)
        if _norm(t) == _norm(b):
            kind = "encoding"
        elif refs_t and refs_b and refs_t != refs_b:
            kind = "other"  # a different symbol/addend must not hide behind a HI/LO or GP label
        elif (nt or not t.strip()) and (nb or not b.strip()):
            kind = "nop"  # a nop against nothing (both nops would not be a differing row)
            side = 1 if nt else 2
            before = _mnemonic(prev(side, i))
            if _LOAD.match(before):
                kind = "load_delay"
            elif _BRANCH.match(before):
                kind = "delay_slot"
        elif nt != nb and t.strip() and b.strip():  # a nop where the other side has an instruction
            side = 1 if nt else 2
            before = _mnemonic(prev(side, i))
            kind = "delay_slot" if _BRANCH.match(before) else "load_delay" if _LOAD.match(before) else "nop"
        elif t.strip() and b.strip() and _mnemonic(t) != _mnemonic(b) and _mnemonic(t) != "lui" and _mnemonic(b) != "lui":
            kind = "reorder" if moved[_norm(t)] > 0 and moved[_norm(b)] > 0 else "other"
            if kind == "reorder" and (_BRANCH.match(_mnemonic(t)) or _BRANCH.match(_mnemonic(b))):
                kind = "delay_slot"
        elif bool(_GP.search(t)) != bool(_GP.search(b)) or (gp_t != gp_b and (_HILO.search(t) or _HILO.search(b))):
            kind = "gp_rel"
        elif (_HILO.search(t) or _HILO.search(b)) and (hi_t != hi_b or bool(_HILO.search(t)) != bool(_HILO.search(b))
                                                       or (t.strip() and b.strip())):
            kind = "hi_lo"
        elif t.strip() and b.strip() and _only_registers_differ(t, b):
            kind = "regalloc"
        elif t.strip() and b.strip() and _only_stack_differs(t, b):
            kind = "stack"
        elif (not t.strip() or moved[_norm(t)] > 0) and (not b.strip() or moved[_norm(b)] > 0):
            near_branch = (_BRANCH.match(_mnemonic(prev(1, i))) or _BRANCH.match(_mnemonic(prev(2, i)))
                           or _BRANCH.match(_mnemonic(t)) or _BRANCH.match(_mnemonic(b)))
            kind = "delay_slot" if near_branch else "reorder"
        elif _BRANCH.match(_mnemonic(prev(1, i))) and _BRANCH.match(_mnemonic(prev(2, i))) and (
                not t.strip() or not b.strip()):
            kind = "delay_slot"
        else:
            kind = "other"
        counts[kind] += 1
        sample = examples.setdefault(kind, [])
        if len(sample) < 3:
            sample.append({"row": i + 1, "target": t, "base": b})
    return Classes(dict(counts), len(differing), (gp_t, gp_b), examples)
