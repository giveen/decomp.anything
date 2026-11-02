"""Publish pytest's JUnit counts and skip reasons in the GitHub Actions run summary, and ratchet the skip count.

A suite that silently shrinks still reports a cheerful green: the declared-environment check in ``tests/conftest.py``
catches a skip that contradicts the profile, and this catches the other direction — the skip *count* creeping up across
branches. ``tools/ci/skip_baseline.json`` records how many tests each leg skips; more than that fails the leg, and
fewer fails it too, so recovering coverage has to be written down instead of quietly forgotten.
"""

import json
import os
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

BASELINE = Path(__file__).with_name("skip_baseline.json")


def counts(path: Path) -> tuple[int, int, int, int]:
    """(passed, failed, errors, skipped) from a JUnit report."""
    cases = list(ET.parse(path).getroot().iter("testcase"))
    skipped = sum(case.find("skipped") is not None for case in cases)
    failed = sum(case.find("failure") is not None for case in cases)
    errored = sum(case.find("error") is not None for case in cases)
    return len(cases) - skipped - failed - errored, failed, errored, skipped


def gate(path: Path) -> int:
    """0 when the leg skips exactly what the baseline records; 1 (with the reason) when it does not."""
    if not path.is_file() or not BASELINE.is_file():
        return 0
    leg = path.stem.removeprefix("pytest-")
    recorded = json.loads(BASELINE.read_text(encoding="utf-8")).get("skipped", {})
    if leg not in recorded:
        return 0  # a leg with nothing recorded cannot be in breach of it
    _passed, _failed, _errored, skipped = counts(path)
    allowance = recorded[leg]
    if skipped == allowance:
        return 0
    if skipped > allowance:
        print(f"skip ratchet: {leg} skipped {skipped} tests; the baseline records {allowance}.", file=sys.stderr)
        print("Either fix what is skipping (see the table above), or raise tools/ci/skip_baseline.json in this PR and "
              "say why the coverage is gone.", file=sys.stderr)
        return 1
    print(f"skip ratchet: {leg} skipped {skipped} tests, fewer than the {allowance} recorded.", file=sys.stderr)
    print(f"Good news, but it has to be recorded: set \"skipped\".\"{leg}\" to {skipped} in "
          "tools/ci/skip_baseline.json, so the recovered coverage cannot be lost again unnoticed.", file=sys.stderr)
    return 1


def summarize(path: Path) -> str:
    if not path.is_file():
        return f"## {path.name}\n\nNo test report was produced; inspect the dependency/setup/test steps.\n"
    cases = list(ET.parse(path).getroot().iter("testcase"))
    skipped = Counter(case.find("skipped").get("message", "unspecified")
                      for case in cases if case.find("skipped") is not None)
    failures = sum(case.find("failure") is not None for case in cases)
    errors = sum(case.find("error") is not None for case in cases)
    passed = len(cases) - sum(skipped.values()) - failures - errors
    lines = [f"## {path.name}", "", f"**{passed} passed, {failures} failed, {errors} errors, "
             f"{sum(skipped.values())} skipped**", ""]
    if skipped:
        lines += ["Skipped coverage:", "", "| Tests | Reason |", "| ---: | --- |"]
        for reason, count in skipped.most_common():
            reason = " ".join(reason.splitlines()).replace("|", "\\|")
            lines.append(f"| {count} | {reason} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    report = Path(sys.argv[1])
    summary = summarize(report)
    print(summary)
    if target := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(target).open("a", encoding="utf-8") as out:
            out.write(summary)
    sys.exit(gate(report))
