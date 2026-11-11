"""Strict evidence for compiler-matrix matches, independent of objdiff's similarity settings.

This checks a function's unmasked object bytes and symbolic relocation identities. It does not claim that a
partly reconstructed project links to the original image. Unsupported or unreadable objects cannot verify a match.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .. import coffobj, elfobj

VERSION = 1  # invalidate cached experiments whenever the verification contract changes


def verify(target: Path | None, base: Path | None, symbol: str) -> dict:
    evidence: dict = {"version": VERSION, "byte_exact": False, "byte_diffs": None,
                      "scope": "function object bytes and relocation identities"}
    try:
        objects = []
        for path in (target, base):
            if path is None:
                evidence["reason"] = "object unavailable"
                return evidence
            data = path.read_bytes()
            extract = coffobj.extract_function if coffobj.is_coff(data) else elfobj.extract_function
            objects.append(extract(data, symbol))
        a, b = objects
        if a is None or b is None or not a.code or not b.code:
            evidence["reason"] = "function bytes unavailable"
            return evidence
        evidence.update(target_size=len(a.code), base_size=len(b.code),
                        target_sha256=hashlib.sha256(a.code).hexdigest(), base_sha256=hashlib.sha256(b.code).hexdigest())
        evidence["byte_diffs"] = sum(x != y for x, y in zip(a.code, b.code, strict=False)) + abs(len(a.code) - len(b.code))
        same_format = (a.arch, a.big_endian, a.format) == (b.arch, b.big_endian, b.format)
        same_refs = sorted(r.key() for r in a.relocs) == sorted(r.key() for r in b.relocs)
        evidence["relocations_equal"] = same_refs
        evidence["byte_exact"] = same_format and a.code == b.code and same_refs
        evidence["reason"] = ("verified" if evidence["byte_exact"] else "object architecture or format differs" if not same_format
                              else "relocation identities differ" if not same_refs else "function bytes differ")
    except Exception as exc:  # noqa: BLE001 a malformed/unsupported object is unverified, never a match
        evidence["reason"] = f"byte verification unavailable: {type(exc).__name__}"
    return evidence


def read_evidence(value: str | None) -> dict:
    try:
        data = json.loads(value or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
