"""The skip ratchet: a leg may not quietly lose coverage, in either direction."""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "ci"))

import test_summary


def _report(path: Path, skipped: int, passed: int = 10) -> Path:
    suite = ET.Element("testsuite", tests=str(skipped + passed), failures="0", errors="0", skipped=str(skipped))
    for i in range(skipped):
        ET.SubElement(ET.SubElement(suite, "testcase", name=f"skip{i}"), "skipped", message="needs something")
    for i in range(passed):
        ET.SubElement(suite, "testcase", name=f"pass{i}")
    path.write_bytes(ET.tostring(suite))
    return path


def _baseline(monkeypatch, tmp_path: Path, skipped: dict[str, int]) -> None:
    baseline = tmp_path / "skip_baseline.json"
    baseline.write_text(json.dumps({"skipped": skipped}), encoding="utf-8")
    monkeypatch.setattr(test_summary, "BASELINE", baseline)


def test_the_recorded_count_passes(monkeypatch, tmp_path):
    _baseline(monkeypatch, tmp_path, {"3.12": 3})
    assert test_summary.gate(_report(tmp_path / "pytest-3.12.xml", skipped=3)) == 0


def test_losing_a_test_fails_the_leg(monkeypatch, tmp_path):
    """The failure this exists for: a fixture stops being built and the suite shrinks while staying green."""
    _baseline(monkeypatch, tmp_path, {"3.12": 3})
    assert test_summary.gate(_report(tmp_path / "pytest-3.12.xml", skipped=4)) == 1


def test_recovering_coverage_must_be_recorded(monkeypatch, tmp_path):
    """Fewer skips is good news that still has to be written down, or the next branch may spend it again."""
    _baseline(monkeypatch, tmp_path, {"3.12": 3})
    assert test_summary.gate(_report(tmp_path / "pytest-3.12.xml", skipped=2)) == 1


def test_a_leg_with_no_recorded_count_is_not_judged(monkeypatch, tmp_path):
    """A new or local leg has nothing to be in breach of; the ledger is added when it is trusted."""
    _baseline(monkeypatch, tmp_path, {"3.12": 3})
    assert test_summary.gate(_report(tmp_path / "pytest-3.15.xml", skipped=99)) == 0


def test_a_missing_report_is_left_to_the_test_step(monkeypatch, tmp_path):
    """This step runs with `if: always()`; it must not add a second, confusing failure of its own."""
    _baseline(monkeypatch, tmp_path, {"3.12": 3})
    assert test_summary.gate(tmp_path / "not-there.xml") == 0
