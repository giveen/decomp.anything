"""The RE console: REA's whole tool catalog, driven from the page (see console.py).

These routes are thin: the console owns its REA process and what it will let a page run. A tool that acts on the world
only runs when the request says the user confirmed it.
"""

from __future__ import annotations

import time

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .console import ConsoleError
from .serverctx import (
    ServerContext,
)


def register(ctx: ServerContext) -> None:
    jobs = ctx.jobs
    mcp = ctx.mcp
    console = ctx.console
    guard = ctx.guard
    body = ctx.body
    call = ctx.call
    fail = ctx.fail
    @mcp.custom_route("/api/rea", methods=["GET"])
    async def api_rea(request: Request) -> Response:
        return guard(request) or JSONResponse(await call(console.status))

    @mcp.custom_route("/api/rea/settings", methods=["POST"])
    async def api_rea_settings(request: Request) -> Response:
        if (bad := guard(request, write=True)):
            return bad
        data = await body(request)
        env = data.get("env") if isinstance(data.get("env"), dict) else {}
        try:
            return JSONResponse(await call(console.configure, data.get("command") or "", env,
                                           str(data.get("provider") or "")))
        except ConsoleError as e:
            return fail(e)

    @mcp.custom_route("/api/rea/tools", methods=["GET"])
    async def api_rea_tools(request: Request) -> Response:
        if (bad := guard(request)):
            return bad
        try:
            return JSONResponse(await call(console.tools))
        except ConsoleError as e:
            return fail(e, 503)

    @mcp.custom_route("/api/rea/call", methods=["POST"])
    async def api_rea_call(request: Request) -> Response:
        """Any REA tool, as a job (opening a big program in Ghidra takes minutes). Tools that aren't read-only
        run only with `confirm`: the page asks the user first."""
        if (bad := guard(request, write=True)):
            return bad
        data = await body(request)
        tool, args = str(data.get("tool") or ""), data.get("arguments") or {}
        if not isinstance(args, dict):
            return fail(ValueError("arguments must be an object"))
        try:
            why, connection_id = await call(console.prepare_call, tool)
        except ConsoleError as e:
            return fail(e)
        if why and data.get("confirm") is not True:
            return JSONResponse({"error": f"{tool} {' and '.join(why)}: confirm to run it", "confirm": why},
                                status_code=409)
        for jid, job in list(jobs.jobs.items()):  # answers can be megabytes: keep only recent ones
            if job["kind"] == "rea" and job["state"] != "running" and time.time() - job["t"] > 600:
                jobs.jobs.pop(jid, None)
                jobs.done.pop(jid, None)
        return JSONResponse({"job": jobs.start("rea", lambda log: console.call(tool, args, log, connection_id=connection_id))})

    @mcp.custom_route("/api/rea/stop", methods=["POST"])
    async def api_rea_stop(request: Request) -> Response:
        if (bad := guard(request, write=True)):
            return bad
        await call(console.stop)
        return JSONResponse(await call(console.status))

