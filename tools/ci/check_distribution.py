"""Exercise an installed wheel, its bundled resources, and both console entry points."""

import os
import subprocess
import sys
import tempfile
from importlib import metadata, resources
from pathlib import Path


def main() -> None:
    package = resources.files("decompnet")
    checkout = Path(__file__).resolve().parents[2]
    if Path(str(package)).is_relative_to(checkout / "src"):
        raise RuntimeError("package smoke test imported the checkout instead of the installed wheel")
    for name in ("toolchains.toml", "app/static/index.html", "app/static/app.css", "app/guide/analysis.md"):
        if not package.joinpath(name).read_bytes():
            raise RuntimeError(f"missing or empty package resource: {name}")
    scripts = {ep.name for ep in metadata.distribution("decompnet").entry_points if ep.group == "console_scripts"}
    with tempfile.TemporaryDirectory() as work:
        for name in ("decomp", "decompnet"):
            if name not in scripts:
                raise RuntimeError(f"missing console entry point: {name}")
            executable = Path(sys.executable).parent / (name + (".exe" if os.name == "nt" else ""))
            subprocess.run([str(executable), "--help"], cwd=work, check=True, timeout=30)
    print("Installed wheel resources and both console entry points passed.")


if __name__ == "__main__":
    main()
