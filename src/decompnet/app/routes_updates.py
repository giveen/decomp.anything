"""Updating decomp.anything itself, and quitting it.

The check is cheap and runs in the background; installing restarts the app, so it waits until no helper is working and
only runs when asked (or when the user turned automatic updates on). Updating REA is the same path against its own
installer. The two checkers come from build() (they outlive a request); watch_updates is started there too.
"""

from __future__ import annotations

import os
import threading
import time

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .. import hostenv
from . import rea_updates, updates
from .serverctx import (
    ServerContext,
)


def register(ctx: ServerContext) -> None:
    updater = ctx.updater
    rea_updater = ctx.rea_updater
    manager = ctx.manager
    jobs = ctx.jobs
    settings = ctx.settings
    mcp = ctx.mcp
    guard = ctx.guard
    body = ctx.body
    call = ctx.call
    fail = ctx.fail
    @mcp.custom_route("/api/update", methods=["GET", "POST"])
    async def api_update(request: Request) -> Response:
        """Whether a newer decomp.anything is out (checked now and then; ?refresh=1 now) and, POSTed, installing it."""
        if (bad := guard(request, write=request.method == "POST")):
            return bad
        if request.method == "POST":
            try:
                target = (await body(request)).get("target", "app")
                await call(apply_update, target)
            except ValueError as e:
                return fail(e)
        refresh = request.query_params.get("refresh") == "1"
        if not refresh and not checking():  # the user turned checks off: only "Check now" asks
            return JSONResponse({"disabled": True, "available": False, "current": updates.installed(),
                                 "job": updater.job, "runtime": hostenv.runtime(),
                                 "rea": {"disabled": True, "available": False, "current": rea_updates.current(),
                                         "job": rea_updater.job}})
        return JSONResponse(await call(lambda: {**updater.status(refresh), "rea": rea_updater.status(refresh)}))

    update_lock = threading.Lock()

    def apply_update(target: str = "app") -> None:
        if target not in ("app", "rea"):
            raise ValueError("unknown update target")
        with update_lock:
            if any(u.job["state"] in ("installing", "restarting") for u in (updater, rea_updater)):
                raise ValueError("an update is already running")
            (rea_updater if target == "rea" else updater).apply()

    def checking() -> bool:
        return settings.data.get("check_updates", True) is not False and os.environ.get("DECOMP_UPDATES") != "off"

    def watch_updates() -> None:
        """Check now and then; with "install updates automatically" on, install one while no helper is working."""
        time.sleep(20)
        while True:
            if not checking():
                time.sleep(300)
                continue
            try:
                st = updater.status()
                rst = rea_updater.status()
                busy = (any(s.state in ("starting", "working") for s in list(manager.sessions.values()))
                        or any(j["state"] == "running" for j in list(jobs.jobs.values())))
                failed_rea = (rst["job"]["state"] == "error"
                              and rst["job"].get("version") == (rst.get("latest") or {}).get("version"))
                if st.get("available") and st.get("can_apply") and settings.data.get("auto_update") and not busy:
                    apply_update()
                elif (rst.get("available") and rst.get("can_apply") and settings.data.get("auto_update_rea")
                      and not busy and not failed_rea):
                    apply_update("rea")
            except (ValueError, OSError):
                pass
            time.sleep(300)

    @mcp.custom_route("/api/quit", methods=["POST"])
    async def api_quit(request: Request) -> Response:
        if (bad := guard(request, write=True)):
            return bad

        def bye():
            manager.stop()
            manager.on_stop()
            time.sleep(0.5)
            os._exit(0)
        threading.Thread(target=bye, daemon=True).start()
        return JSONResponse({"ok": True})

    ctx.watch_updates = watch_updates  # build() starts it: it outlives every request
