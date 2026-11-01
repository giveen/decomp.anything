"""The page, and the state it polls.

The GUI is one file of plain JavaScript; what it needs from the server is here: the HTML and its assets, the whole app
state in one call (api/state), the sidebar's project list, and the long jobs an import starts (api/import, api/map,
api/job). Reading a project goes through its overview; nothing here writes to one.
"""

from __future__ import annotations

import contextlib
import json
import mimetypes
import re
import secrets
import shutil
import uuid
from pathlib import Path

import anyio
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .. import __version__, hostenv, toolchains
from ..network import NetworkError
from ..rom.n64 import RomError
from . import agents, updates
from .engine import COMPILERS, MW_VERSIONS, PLATFORM_STATUS, SHORT_NAMES, TOOLS
from .serverctx import (
    COOKIE,
    MAX_DISC,
    ServerContext,
    _analyze_instead,
    _static,
    job_error,
)


def register(ctx: ServerContext) -> None:
    net = ctx.net
    manager = ctx.manager
    jobs = ctx.jobs
    settings = ctx.settings
    home = ctx.home
    secret = ctx.secret
    mcp = ctx.mcp
    mcp_url = ctx.mcp_url
    build_id = ctx.build_id
    allowed_hosts = ctx.allowed_hosts
    guard = ctx.guard
    call = ctx.call
    fail = ctx.fail
    @mcp.custom_route("/", methods=["GET"])
    async def index(request: Request) -> Response:
        if request.headers.get("host") not in allowed_hosts:
            return JSONResponse({"error": "bad host"}, status_code=403)
        key = request.query_params.get("k")
        if key:
            if not secrets.compare_digest(key, secret):
                return HTMLResponse("This link is from an earlier session. Open decomp.anything again.", status_code=401)
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie(COOKIE, secret, httponly=True, samesite="strict", max_age=30 * 86400)
            return resp
        if not secrets.compare_digest(request.cookies.get(COOKIE, ""), secret):
            return HTMLResponse("<!doctype html><meta charset=utf-8><title>decomp.anything</title>"
                                "<body style='font:16px system-ui;background:#0b0c0e;color:#e8e6e1;padding:3rem'>"
                                "Open decomp.anything from its app icon or by running <code>decomp</code>.", status_code=401)
        page = _static("index.html")
        if page is None:
            return HTMLResponse("decomp.anything is installed without its page (index.html); reinstall it.",
                                status_code=500)
        return HTMLResponse(page.decode(), headers={"Cache-Control": "no-store"})

    @mcp.custom_route("/assets/{name:path}", methods=["GET"])
    async def asset(request: Request) -> Response:
        if request.headers.get("host") not in allowed_hosts:
            return Response(status_code=403)
        name = request.path_params["name"]
        data = _static(name)
        if data is None:
            return Response(status_code=404)
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return Response(data, media_type=ctype, headers={"Cache-Control": "no-cache"})

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "app": "decomp-anything", "version": __version__, "build": build_id})


    def overviews() -> list[tuple[str, dict, str]]:
        """(slug, overview or None, why not) for every project. One that can't be read is still listed, with the
        reason: before, one odd project emptied the whole sidebar, or quietly went missing from it."""
        out = []
        for p in net.store.all("SELECT slug, name FROM projects ORDER BY id DESC"):
            try:
                out.append((p["slug"], net.overview(p["slug"]), ""))
            except Exception as e:
                jobs.logger.warning("project %s can't be read: %s", p["slug"], e, exc_info=not isinstance(e, NetworkError))
                out.append((p["slug"], {"slug": p["slug"], "title": p["name"] or p["slug"]}, job_error(e, jobs.log_path)))
        return out

    def state() -> dict:
        projects = [ov | {"parent": (ov.get("source") or {}).get("parent"),
                          "entry": (ov.get("source") or {}).get("entry")} | ({"broken": why} if why else {})
                    for _, ov, why in overviews()]
        tcs = [t for t in toolchains.status() if t["id"] in COMPILERS or t["id"] in TOOLS]
        for t in tcs:
            t["use"] = COMPILERS.get(t["id"]) or TOOLS[t["id"]]
            t["name"] = SHORT_NAMES.get(t["id"], t["name"])
        return {"version": __version__, "build": build_id, "install": updates.installed(), **hostenv.info(),
                "platform": toolchains.current_platform(), "home": hostenv.shown_path(home.parent),
                "log_file": hostenv.shown_path(home / "logs" / "decomp.log"),
                "projects": projects, "toolchains": tcs, "agents": agents.detect(),
                "api_presets": agents.API_PRESETS, "settings": settings.public(), "mcp_url": mcp_url,
                "platform_status": PLATFORM_STATUS,
                "codewarrior": {"installed": toolchains.installed("mwcc") is not None,
                                "ds_installed": toolchains.installed("mwccarm") is not None, **MW_VERSIONS}}

    def autopilot(slug: str) -> None:
        """Free matches first, in the background, when the user wants that (the default): a game started from
        scratch, on any platform, as soon as it's ready. A public decomp, or one not set up yet, says no."""
        if settings.data.get("autopilot", True):
            with contextlib.suppress(NetworkError):
                net.start_autopilot(slug)

    def project_list() -> list[dict]:
        """The sidebar: every project with its progress and how many helper agents work on it right now."""
        running: dict[str, int] = {}
        for s in manager.sessions.values():
            if s.state in ("starting", "working", "waiting"):
                running[s.slug] = running.get(s.slug, 0) + 1
        out = []
        for slug, ov, why in overviews():
            out.append({k: ov.get(k) for k in ("slug", "title", "kind", "origin", "platform", "platform_name", "ready",
                                                "entries", "files", "functions", "matched", "bytes", "matched_bytes",
                                                "last_activity")}
                       | {"parent": (ov.get("source") or {}).get("parent"),
                          "entry": (ov.get("source") or {}).get("entry")}
                       | {"working": running.get(slug, 0), "room": (ov.get("room") or {}).get("role"),
                          "autopilot": (ov.get("autopilot") or {}).get("state")} | ({"broken": why} if why else {}))
        return out

    @mcp.custom_route("/api/projects", methods=["GET"])
    async def api_projects(request: Request) -> Response:
        return guard(request) or JSONResponse(await call(project_list))

    @mcp.custom_route("/api/state", methods=["GET"])
    async def api_state(request: Request) -> Response:
        return guard(request) or JSONResponse(await call(state))

    @mcp.custom_route("/api/companion", methods=["POST"])
    async def api_companion(request: Request) -> Response:
        """A PDB/MAP/symbol/trace/header that belongs with the binary being uploaded next."""
        if (bad := guard(request, write=True)):
            return bad
        q = request.query_params
        token = q.get("token", "")
        if not re.fullmatch(r"[a-f0-9]{8,32}", token):
            return fail(ValueError("bad companion token"))
        name = Path(q.get("name") or "").name
        if not name or name in (".", ".."):
            return fail(ValueError("name the companion file"))
        folder = home / "uploads" / token
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        size = 0
        async with await anyio.open_file(target, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_DISC:
                    await f.aclose()
                    target.unlink(missing_ok=True)
                    return fail(ValueError("that companion file is too large"))
                await f.write(chunk)
        if size == 0:
            target.unlink(missing_ok=True)
            return fail(ValueError("the companion file was empty"))
        return JSONResponse({"ok": True, "name": name, "size": size})

    @mcp.custom_route("/api/import", methods=["POST"])
    async def api_import(request: Request) -> Response:
        """A game the user owns: the demo, an upload streamed to disk (discs are gigabytes), or a local path."""
        if (bad := guard(request, write=True)):
            return bad
        q = request.query_params
        toolchain = q.get("toolchain") or None
        app_dir = False
        if q.get("demo") == "1":
            game: bytes | Path | None = _static("demo/demo-quest-64.z64")
            if game is None:
                return fail(ValueError("the demo ROM is missing from this install"))
            name = "demo-quest-64.z64"
        else:
            if q.get("confirmed") != "1":
                return fail(ValueError("confirm that this is your own legally obtained copy of the game"))
            if q.get("path"):
                game = hostenv.local_path(q["path"])
                app_dir = game.is_dir()
                if not game.is_file() and not app_dir:
                    return fail(ValueError(f"no file or folder at {game}"))
                name = game.name
            else:
                name = Path(q.get("name") or "game.bin").name
                companions = q.get("companions", "")
                if companions:
                    if not re.fullmatch(r"[a-f0-9]{8,32}", companions):
                        return fail(ValueError("bad companion token"))
                    upload_dir = home / "uploads" / companions
                    if not upload_dir.is_dir():
                        return fail(ValueError("the companion files are gone: drop the binary and its files together"))
                else:
                    upload_dir = home / "uploads" / uuid.uuid4().hex[:8]  # keeps the file's own name
                upload_dir.mkdir(parents=True, exist_ok=True)
                game = upload_dir / name
                size = 0
                async with await anyio.open_file(game, "wb") as f:
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > MAX_DISC:
                            await f.aclose()
                            game.unlink(missing_ok=True)
                            return fail(ValueError("that file is larger than any game disc"))
                        await f.write(chunk)
                if size == 0:
                    game.unlink(missing_ok=True)
                    return fail(ValueError("the file was empty"))
        uploaded = isinstance(game, Path) and not app_dir and game.parent.parent == home / "uploads"

        def work(log):
            try:
                if app_dir:
                    log("a folder: opening it as a JavaScript or Electron application with REA")
                    slug = net.import_analysis(game, tool="analyze_javascript_application", confirmed=True, log=log)
                else:
                    try:
                        slug = net.import_rom(game, toolchain=toolchain, confirmed=True, name=name, log=log)
                    except RomError as e:
                        if not isinstance(game, Path) or not _analyze_instead(e):
                            raise
                        log(f"{e}\nOpening it with REA instead: its functions become work items for the agents.")
                        slug = net.import_analysis(game, confirmed=True, copy_target=uploaded, log=log)
                auto = {}
                if isinstance(game, Path):
                    for p in sorted(game.parent.iterdir()):
                        lower = p.name.lower()
                        if lower in ("symbol_addrs.txt", "symbols.txt"):
                            auto.setdefault("symbols", str(p))
                        elif lower in ("coverage.csv", "trace.csv") or p.suffix.lower() in (".trace", ".cov"):
                            auto.setdefault("trace", str(p))
                        elif uploaded and lower in ("types.h", "context.h"):
                            auto.setdefault("types", str(p))
                intake = {key: q.get(key, "") or auto.get(key, "")
                          for key in ("notes", "symbols", "types", "refs", "trace", "libs")}
                if any(v.strip() for v in intake.values()):
                    report = net.apply_intake(slug, notes=intake["notes"], symbols=intake["symbols"],
                                              types=intake["types"], refs=intake["refs"], trace=intake["trace"],
                                              libraries=intake["libs"], log=log)
                    if report["warnings"]:
                        log("starting context warnings: " + "; ".join(report["warnings"]))
                try:
                    net.write_intake(slug, log=log)
                except Exception as e:  # noqa: BLE001
                    log(f"intake report skipped: {e}")
                settings.data["last_project"] = slug
                settings.save()
                if net.is_analysis(net.project_row(slug)):
                    # a native import with no procedure inventory opens on a survey item: decompile its starting
                    # batch now, in its own job, so the project page has code by the time the user gets there
                    jobs.start("prefill", lambda log: net.prefill_survey(slug, log=log))
                else:
                    autopilot(slug)
                return {"slug": slug}
            finally:
                if uploaded and isinstance(game, Path):
                    shutil.rmtree(game.parent, ignore_errors=True)
        return JSONResponse({"job": jobs.start("import", work)})

    MAP_KEYS = ("name", "vram", "size", "insns", "unit", "kind", "path", "child", "status", "best", "attempts",
                "hits", "agent", "matched_at", "matched_by")

    @mcp.custom_route("/api/map", methods=["GET"])
    async def api_map(request: Request) -> Response:
        """Every function, for the map and the table. `since`: the signature the GUI has; when nothing changed, the
        answer is just that (a big game's map is megabytes, and the GUI asks every few seconds). Rows go as arrays
        under `keys`, gzipped when the browser takes it."""
        if (bad := guard(request)):
            return bad
        slug = request.query_params.get("slug", "")

        def build() -> dict:
            sig = net.map_signature(slug)
            if request.query_params.get("since") == sig:
                return {"sig": sig, "same": True}
            rows = net.function_map(slug)
            return {"sig": sig, "keys": MAP_KEYS, "rows": [[r.get(k) for k in MAP_KEYS] for r in rows]}
        try:
            data = await call(build)
        except (NetworkError, ValueError) as e:
            return fail(e)
        raw = json.dumps(data, separators=(",", ":")).encode()
        if len(raw) > 4096 and "gzip" in request.headers.get("accept-encoding", ""):
            import gzip
            return Response(gzip.compress(raw, 1), media_type="application/json",
                            headers={"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
        return Response(raw, media_type="application/json")

    @mcp.custom_route("/api/log", methods=["GET"])
    async def api_log(request: Request) -> Response:
        """The end of decomp.anything's log, to read or attach to a bug report (the app's data folder isn't always at
        hand: in Docker it's inside the container)."""
        if (bad := guard(request)):
            return bad
        path = home / "logs" / "decomp.log"
        try:
            async with await anyio.open_file(path, "rb") as f:
                await f.seek(max(0, (await anyio.Path(path).stat()).st_size - (512 << 10)))
                text = (await f.read()).decode("utf-8", "replace")
        except OSError:
            text = "(nothing logged yet)"
        return Response(text, media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": 'inline; filename="decomp.log"'})

    @mcp.custom_route("/api/job", methods=["GET"])
    async def api_job(request: Request) -> Response:
        if (bad := guard(request)):
            return bad
        job = jobs.jobs.get(request.query_params.get("id", ""))
        return JSONResponse(job) if job else fail(ValueError("no such job"), 404)

