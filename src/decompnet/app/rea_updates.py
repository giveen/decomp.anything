"""Updates for the app-managed REA toolset. External commands remain externally managed.

Each update lives in its own directory. Only a successfully probed installation becomes active;
existing REA processes can keep reading their original files until the app restarts.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import urllib.request
from pathlib import Path

from .. import rea, toolchains
from . import updates

REGISTRY = "https://registry.npmjs.org"
LINK = "https://www.npmjs.com/package/rea-agents"
STABLE = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z")


def package(base: Path) -> dict:
    data = json.loads((base / "node_modules" / "rea-agents" / "package.json").read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("name") != "rea-agents" or not STABLE.fullmatch(str(data.get("version", ""))):
        raise ValueError("REA's installed package has an invalid name or version")
    return data


def current() -> dict:
    cfg = rea.settings()
    version = rea.PACKAGE.split("@", 1)[1] if cfg["source"] == "npx" else None
    error = None
    if cfg["source"] == "installed":
        try:
            base = toolchains.path("rea") / ((toolchains.installed("rea") or {}).get("active") or "")
            version = package(base)["version"]
        except (OSError, ValueError, KeyError) as e:
            error = str(e)
    return {"version": version, "source": cfg["source"], "error": error}


def can_apply() -> tuple[bool, str]:
    if rea.settings()["source"] not in ("installed", "npx"):
        return False, "REA uses an external command. Update it with its package manager or REA setup."
    if not shutil.which("npm") or not shutil.which("node"):
        return False, "Install Node.js and npm before updating REA."
    return updates.can_apply()


def latest() -> dict:
    req = urllib.request.Request(f"{REGISTRY}/rea-agents/latest", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("REA's release metadata is too large")
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("name") != "rea-agents" or not STABLE.fullmatch(str(data.get("version", ""))):
        raise ValueError("npm did not return a stable rea-agents release")
    return data


def check() -> dict:
    cur = current()
    ok, why = can_apply()
    out = {"current": cur, "available": False, "latest": None, "checked": time.time(), "error": cur["error"],
           "can_apply": ok, "why_not": why, "notes": [], "link": LINK}
    # Do not contact npm for disabled or externally owned REA installations.
    if cur["source"] not in ("installed", "npx"):
        return out
    try:
        release = latest()
        out["latest"] = {"version": release["version"]}
        node = (release.get("engines") or {}).get("node")
        if node:
            out["notes"] = [f"Requires Node.js {node}. npm checks this before installation."]
        if cur["version"]:
            out["available"] = updates.version_key(release["version"]) > updates.version_key(cur["version"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        out["error"] = f"couldn't check REA for updates: {e}"
    return out


def install(found: dict, log=print) -> dict:
    ok, why = can_apply()  # settings may have changed since the last check
    if not ok:
        raise ValueError(why)
    version = str((found.get("latest") or {}).get("version", ""))
    if not STABLE.fullmatch(version):
        raise ValueError("invalid REA release version")
    dest = toolchains.path("rea")
    dest.mkdir(parents=True, exist_ok=True)
    versions = dest / "versions"
    versions.mkdir(exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f"{version}-", dir=versions))
    pointer = dest / f"{stage.name}.json"
    try:
        log(f"installing rea-agents@{version} in an isolated tool folder")
        updates._stream([shutil.which("npm"), "install", "--prefix", str(stage), "--no-save", "--no-package-lock",
                         "--no-audit", "--no-fund", "--engine-strict", "--registry", REGISTRY,
                         f"rea-agents@{version}"], log, "installing REA failed")
        if package(stage)["version"] != version:
            raise ValueError("npm installed a different REA version")
        # Verify the MCP protocol and catalog without opening a target or configuring any other agents.
        bin_name = "rea.cmd" if os.name == "nt" else "rea"
        command = stage / "node_modules" / ".bin" / bin_name
        cfg = rea.settings()
        with rea.Client([str(command), "mcp"], env=cfg.get("env"), provider=cfg.get("provider")) as client:
            names = {t.get("name") for t in client.tools()}
            if not names.intersection(rea.ANALYSIS_ALLOWED):
                raise ValueError("the new REA server exposes no supported analysis tools")
        if not can_apply()[0]:
            raise ValueError("REA settings changed during installation; the current command was preserved")
        tc = toolchains.get("rea")
        manifest = toolchains.installed("rea") or {"id": "rea", "name": tc.name, "license": tc.license, "parts": []}
        manifest.update(active=str(stage.relative_to(dest)), source=f"morluto/rea {version}",
                        npm=f"rea-agents@{version}", installed_at=time.time())
        pointer.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        pointer.replace(dest / toolchains.MANIFEST)
    except BaseException:
        pointer.unlink(missing_ok=True)
        shutil.rmtree(stage, ignore_errors=True)
        raise
    log("REA is ready; restarting decomp.anything to refresh agent connections and tool catalogs")
    return manifest
