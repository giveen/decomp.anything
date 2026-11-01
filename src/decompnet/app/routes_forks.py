"""Public decomps: the catalog, the fork policy, and setting a fork up with the user's own game.

A fork is a community decomp cloned next to the app; the user's game file is only read here to check it and to give the
fork's build something to compare against. The policy route exists because a decomp's own files say whether it wants
AI-authored contributions, and that is read before a fork is created, not after.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ..network import NetworkError
from .serverctx import (
    MAX_DISC,
    ServerContext,
)


def register(ctx: ServerContext) -> None:
    net = ctx.net
    jobs = ctx.jobs
    settings = ctx.settings
    mcp = ctx.mcp
    guard = ctx.guard
    body = ctx.body
    call = ctx.call
    fail = ctx.fail
    autopilot = ctx.autopilot
    with_project = ctx.with_project
    @mcp.custom_route("/api/catalog", methods=["GET"])
    async def api_catalog(request: Request) -> Response:
        """The public decomps decomp.dev listed, as this install downloaded them before it retired its directory."""
        if (bad := guard(request)):
            return bad
        projects, at = await call(net.forks.catalog)
        return JSONResponse({"projects": projects, "downloaded": at})

    @mcp.custom_route("/api/fork/policy", methods=["POST"])
    async def api_fork_policy(request: Request) -> Response:
        """What a public decomp says about AI and contributions, read from its own files before it's forked."""
        if (bad := guard(request, write=True)):
            return bad
        data = await body(request)
        try:
            return JSONResponse(await call(lambda: net.forks.policy(str(data.get("repo", "")))))
        except NetworkError as e:
            return fail(e)

    @mcp.custom_route("/api/fork", methods=["POST"])
    async def api_fork(request: Request) -> Response:
        if (bad := guard(request, write=True)):
            return bad
        data = await body(request)

        def work(log):
            slug = net.forks.clone(str(data.get("repo", "")), name=str(data.get("name") or ""),
                                   platform=str(data.get("platform") or ""), version=data.get("version") or None, log=log)
            settings.data["last_project"] = slug
            settings.save()
            return {"slug": slug}
        return JSONResponse({"job": jobs.start("fork", work)})

    def fork_of(slug: str):
        if not net.fork_slug(slug):
            raise NetworkError(f"{slug} isn't a fork")
        return net.project_row(slug)

    @mcp.custom_route("/api/fork/game", methods=["POST"])
    async def api_fork_game(request: Request) -> Response:
        """The user's game file, streamed to disk (GameCube and Wii discs are gigabytes)."""
        if (bad := guard(request, write=True)):
            return bad
        q = request.query_params
        slug = q.get("slug", "")
        if q.get("confirmed") != "1":
            return fail(ValueError("confirm that this is your own legally obtained copy of the game"))
        try:
            proj = fork_of(slug)
        except NetworkError as e:
            return fail(e)
        tmp = Path(proj["root"]) / ".decomp-wtf" / f"upload-{uuid.uuid4().hex[:8]}.tmp"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        async with await anyio.open_file(tmp, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_DISC:
                    await f.aclose()
                    tmp.unlink(missing_ok=True)
                    return fail(ValueError("that file is larger than any game disc"))
                await f.write(chunk)
        name = Path(q.get("name") or "game.iso").name

        def work(log):
            try:
                res = net.forks.setup(slug, tmp, name, log=log, move=True)
            finally:
                tmp.unlink(missing_ok=True)
            autopilot(slug)
            return res
        return JSONResponse({"job": jobs.start("fork-setup", work)})

    @mcp.custom_route("/api/fork/setup", methods=["POST"])
    async def api_fork_setup(request: Request) -> Response:
        """Set up with a game file already on this computer (`path`), or with none (`path` empty: run the build)."""
        if (bad := guard(request, write=True)):
            return bad
        data = await body(request)
        slug = str(data.get("slug", ""))
        try:
            fork_of(slug)
        except NetworkError as e:
            return fail(e)
        path = str(data.get("path") or "").strip().strip('"')
        src: Path | None = None  # work() runs in a thread: it wants a plain pathlib.Path
        if path:
            if not data.get("confirmed"):
                return fail(ValueError("confirm that this is your own legally obtained copy of the game"))
            checked = await anyio.Path(path).expanduser()
            if not await checked.is_file():
                return fail(ValueError(f"no file at {checked}"))
            src = Path(str(checked))

        def work(log):
            res = net.forks.setup(slug, src, src.name, log=log) if src is not None else net.forks.setup(slug, None, log=log)
            autopilot(slug)
            return res
        return JSONResponse({"job": jobs.start("fork-setup", work)})

    @mcp.custom_route("/api/fork/check", methods=["POST"])
    async def api_fork_check(request: Request) -> Response:
        """Pick a hand-built project up (or re-measure one): reads its objdiff.json and progress."""
        if (bad := guard(request, write=True)):
            return bad
        slug = str((await body(request)).get("slug", ""))
        try:
            fork_of(slug)
        except NetworkError as e:
            return fail(e)
        return JSONResponse({"job": jobs.start("fork-check", lambda log: net.forks.check(slug, log=log))})

    @mcp.custom_route("/api/fork/rom-check", methods=["POST"])
    async def api_fork_rom_check(request: Request) -> Response:
        """A DS game from scratch: rebuild the ROM and compare it with the user's copy."""
        if (bad := guard(request, write=True)):
            return bad
        slug = str((await body(request)).get("slug", ""))
        try:
            fork_of(slug)
        except NetworkError as e:
            return fail(e)
        return JSONResponse({"job": jobs.start("rom-check", lambda log: net.forks.check_rom(slug, log=log))})

    @mcp.custom_route("/api/fork/start-files", methods=["POST"])
    async def api_fork_start_files(request: Request) -> Response:
        """Give the code no source file covers source files (dtk-template projects), verified by the game's checksum."""
        if (bad := guard(request, write=True)):
            return bad
        slug = str((await body(request)).get("slug", ""))
        try:
            fork_of(slug)
        except NetworkError as e:
            return fail(e)
        return JSONResponse({"job": jobs.start("fork-start-files", lambda log: net.forks.start_files(slug, log=log))})

    mcp.custom_route("/api/fork/needs", methods=["GET"])(with_project(lambda s, r, d: net.forks.needs(fork_of(s)["slug"])))

