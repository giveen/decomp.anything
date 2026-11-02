"""Fail the build when the app grows type errors, and keep the number honest when it shrinks.

The baseline is a ratchet, not a verdict: `tools/ci/type_baseline.json` holds what pyright reported the day the gate
went in, and no change may add to it. Reporting fewer is also a failure until the file is lowered to the new number,
so a fixed finding can't quietly reappear later. Re-declare the number in the PR that moves it, in either direction.

Usage: check_types.py <pyright --outputjson file>
"""

import json
import sys
from collections import Counter
from pathlib import Path

BASELINE = Path(__file__).resolve().parents[2] / "tools" / "ci" / "type_baseline.json"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    try:  # pyright writes its report even when it finds errors, so a missing or unparsable file means the check
        report = json.loads(Path(argv[1]).read_text(encoding="utf-8"))  # itself never ran: that is a failure, not a pass
    except (OSError, ValueError) as bad:
        print(f"FAIL: no pyright JSON report at {argv[1]}: {bad}", file=sys.stderr)
        return 1
    try:
        allowed = int(json.loads(BASELINE.read_text(encoding="utf-8"))["error_count"])
    except (OSError, ValueError, KeyError) as bad:
        print(f"FAIL: {BASELINE} is unreadable or has no error_count: {bad}", file=sys.stderr)
        return 1
    summary = report.get("summary", {})
    found, files = int(summary.get("errorCount", 0)), int(summary.get("filesAnalyzed", 0))
    print(f"pyright: {found} error(s) across {files} file(s); baseline allows {allowed}")
    if found == allowed:
        return 0
    by_file = Counter(
        d.get("file", "?").split("/decomp.wtf/")[-1] for d in report.get("generalDiagnostics", []) if d.get("severity") == "error"
    )
    for path, n in by_file.most_common(15):
        print(f"  {n:4d}  {path}")
    if found > allowed:
        print(
            f"FAIL: {found - allowed} more type error(s) than the baseline. Fix them, or make the case for raising "
            f"{BASELINE.relative_to(BASELINE.parents[1])} in the PR description.",
            file=sys.stderr,
        )
        return 1
    print(
        f"FAIL: {allowed - found} fewer than the baseline: that is good news, so set the baseline to {found} and "
        "commit it, or the next branch gets those findings back.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
