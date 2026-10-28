"""Publish pytest's JUnit counts and skip reasons in the GitHub Actions run summary."""

import os
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path


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
    summary = summarize(Path(sys.argv[1]))
    print(summary)
    if target := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(target).open("a", encoding="utf-8") as out:
            out.write(summary)
