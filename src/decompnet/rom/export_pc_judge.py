#!/usr/bin/env python3
"""Check one exported PC function against the original bytes, using the same relocation normalization."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
B = json.loads((ROOT / "build.json").read_text(encoding="utf-8"))

try:
    from decompnet import coffobj, elfobj
    from decompnet.compiler import compile_source
    from decompnet.project import compiler_config
    from decompnet.rom import elfexe, match_x86, pe
    from decompnet.rom.symbols import Symbol, SymbolTable
except ImportError:
    sys.exit("judge.py needs decomp.anything installed (it uses the project's compiler and matcher)")


def target_image():
    data = (ROOT / B["image"]).read_bytes()
    return (elfexe if B["platform"] == "linux" else pe).read(data, B["image"])


def check(name: str) -> bool:
    fn = next((f for f in B["functions"] if f["name"] == name), None)
    if fn is None:
        print(f"no exported function named {name}")
        return False
    image = target_image()
    target = next((f for f in image.functions if f.vram == fn["vram"]), None)
    if target is None:
        print(f"{name}: target function is no longer at 0x{fn['vram']:X}")
        return False
    source = (ROOT / fn["source"]).read_text(encoding="utf-8")
    m = re.match(r'\s*#include "([^"]+)"\s*\n', source)
    header_name = m.group(1) if m else "context.h"
    header_path = ROOT / "include" / header_name
    header = header_path.read_text(encoding="utf-8") if header_path.exists() else ""
    body = source[m.end():] if m else source
    res = compile_source(compiler_config(ROOT, {"compiler": B["compiler"]}), header, body, trusted=True)
    if not res.ok:
        print(f"❌ {name}: doesn't compile\n{res.log[-1200:]}")
        return False
    candidate = coffobj.extract_function(res.obj, name) if coffobj.is_coff(res.obj) else elfobj.extract_function(res.obj, name)
    if candidate is None:
        print(f"❌ {name}: the object has no function named {name}")
        return False
    t = match_x86.X86Target(name, image.arch, target.code, target.refs)
    symbols = SymbolTable([Symbol(n, a, 0, "func" if a in {f.vram for f in image.functions} else "data")
                           for n, a in B.get("symbols", {}).items()])
    linked = match_x86.link(res.obj, name, t, symbols)
    if linked.exact:
        print(f"✅ MATCH {name} (0 bytes differ)")
        return True
    print(f"❌ {name}: " + "; ".join(linked.problems[:8]))
    return False


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python judge.py FUNCTION | --all")
        return 2
    names = [f["name"] for f in B["functions"]] if sys.argv[1] == "--all" else [sys.argv[1]]
    ok = all(check(n) for n in names)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
