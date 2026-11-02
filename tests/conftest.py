import os
from pathlib import Path

import pytest

# the apps the tests start never look for updates on the network (tests/test_updates.py checks that part offline)
os.environ.setdefault("DECOMP_UPDATES", "off")

# What CI promises this run has. `full` means the pinned toolchains and built demo fixtures are present, so a test
# that skips because something is *missing* is a broken promise, not a legitimate absence — and a skip that means
# "the installer silently failed" is the worst outcome a suite can have: green, and covering nothing.
PROFILE = os.environ.get("DECOMP_TEST_PROFILE", "minimal")

# The ledger of what this project does not verify, even under `full`. Every entry is a capability CI deliberately
# cannot supply, so its tests stay skipped on every platform and the reason is written down instead of being inferred
# from a count. Anything *not* here must run when the profile is `full`: adding the capability means deleting its line,
# and losing one means the run goes red naming the reason.
NOT_IN_FULL = (
    "Windows only",  # the CI legs are Linux; windows is its own job
    "needs Windows",
    "needs CodeWarrior",  # mwcc / mwccarm: proprietary, not downloadable
    "needs a PlayStation CHD",  # the developer's own dump
    "another ninja is on PATH",  # an inverse check: the test needs ninja to be absent
    "needs the PlayStation demo",  # projects/demo-psx needs gcc-2.7.2-psx and ld.lld
    "needs the demo DS ROM",  # projects/demo-ds needs mwccarm
    "needs ee-gcc-2.96",  # the PS2 archive is fetched, not shipped
    "ee-gcc-2.96 not installed",
    "needs the psx toolchain",
    "needs the ps2 toolchain",
    "needs gcc-2.7.2-psx",
    "needs the GameCube binutils",
    "needs agbcc",  # fetched by the GBA project, not by the toolchain installer
    "needs Ghidra and Java 21+",  # a foreign install; the draft path is covered by the fake in test_forks.py
    "needs Linux and the EE-GCC archive in the local download cache",  # the PS2 archive is never fetched by CI
)

_skipped: list[tuple[str, str]] = []


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Remember every skip with the reason the test itself gave."""
    outcome = yield
    report = outcome.get_result()
    if report.skipped and report.when in ("setup", "call"):
        reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
        _skipped.append((item.nodeid, reason.removeprefix("Skipped: ")))


def pytest_sessionfinish(session, exitstatus):
    """One broken promise fails the run, by name."""
    if PROFILE != "full" or not _skipped:
        return
    broken = [(node, why) for node, why in _skipped if not any(ok in why for ok in NOT_IN_FULL)]
    if not broken:
        return
    lines = [f"DECOMP_TEST_PROFILE=full, but {len(broken)} test(s) skipped for something that is not in NOT_IN_FULL:", ""]
    for node, why in broken[:200]:
        lines.append(f"  {node}\n      {why}")
    if len(broken) > 40:
        lines.append(f"  ... and {len(broken) - 40} more")
    lines += ["", "Either install what the run promised, or add the reason to tests/conftest.py:NOT_IN_FULL so the gap is",
              "written down. A skip standing in for a broken install is the failure this check exists to catch."]
    # the step log expires and is not always readable; the artifact is, so the evidence goes there too
    try:
        Path(".ci-artifacts").mkdir(exist_ok=True)
        Path(".ci-artifacts/broken-promises.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line("")
        for line in lines:
            reporter.write_line(line, red=True)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
