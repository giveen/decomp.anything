"""One project's API: everything the page's tabs call with a slug.

Every route here goes through with_project, so a handler only says what it does: read a diff, save an instruction,
start an export, hand a function to an agent. The handlers are a dict of path -> (handler, methods) because they share
that shape; the ones that do more than call the engine once keep a named function above it.
"""

from __future__ import annotations

import contextlib
import json
import secrets
import shutil
import subprocess
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .. import hostenv, toolchains
from ..network import NetworkError
from .serverctx import (
    ServerContext,
)


def register(ctx: ServerContext) -> None:
    net = ctx.net
    manager = ctx.manager
    jobs = ctx.jobs
    settings = ctx.settings
    mcp = ctx.mcp
    mcp_url = ctx.mcp_url
    guard = ctx.guard
    body = ctx.body
    call = ctx.call
    fail = ctx.fail
    def with_project(fn):
        async def handler(request: Request) -> Response:
            if (bad := guard(request, write=request.method == "POST")):
                return bad
            data = await body(request) if request.method == "POST" else {}
            slug = data.get("slug") or request.query_params.get("slug", "")
            try:
                return JSONResponse(await call(lambda: fn(slug, request, data)))
            except (NetworkError, ValueError, toolchains.ToolchainError, OSError) as e:
                return fail(e)
        return handler

    def p_overview(slug, request, data):
        settings.data["last_project"] = slug
        settings.save()
        return net.overview(slug, backfill=True)

    def p_live(slug, request, data):
        q = request.query_params
        out = net.live(slug, int(q.get("e", 0) or 0), int(q.get("a", 0) or 0))
        try:
            since = json.loads(q.get("s") or "{}")
        except ValueError:
            since = {}
        out["sessions"] = [s for s in manager.snapshot(since) if s["slug"] == slug]
        return out

    def p_compiler(slug, request, data):
        net.set_compiler(slug, str(data.get("toolchain", "")), data.get("flags") or None)
        return net.overview(slug)

    def p_analyze_entry(slug, request, data):
        entry = str(data.get("entry") or "")
        if not entry:
            raise ValueError("name the archive entry to analyze")
        return {"job": jobs.start("analysis-entry", lambda log: net.analyze_archive_entry(slug, entry, log=log))}

    def p_contents_read(slug, request, data):
        entry = str(request.query_params.get("entry") or "")
        if not entry:
            raise ValueError("name the archive entry to read")
        return net.analysis_read_entry(slug, entry, int(request.query_params.get("offset") or 0),
                                       int(request.query_params.get("length") or 4096))

    def p_export(slug, request, data):
        def work(log):
            res = net.export_rom(slug, log=log)
            return {k: v for k, v in res.items() if k != "log"} | {"log": res.get("log", "")[-1500:]}
        return {"job": jobs.start("export", work)}

    def p_open(slug, request, data):
        why = hostenv.open_folder(Path(net.project_row(slug)["root"]))
        return {"ok": why is None, "message": why}

    def p_ide(slug, request, data):
        root = Path(net.project_row(slug)["root"])
        for command, label in (("code", "VS Code"), ("codium", "VSCodium"), ("clion", "CLion"),
                               ("idea", "IntelliJ IDEA"), ("rider", "Rider")):
            exe = shutil.which(command)
            if exe:
                subprocess.Popen([exe, str(root)], start_new_session=True)
                return {"ok": True, "editor": label}
        raise ValueError("no supported IDE command found (VS Code, VSCodium, CLion, IntelliJ IDEA or Rider)")

    def p_delete(slug, request, data):
        manager.stop_project(slug)
        proj = net.project_row(slug)
        net.remove_project(slug)
        if data.get("files"):
            shutil.rmtree(proj["root"], ignore_errors=True)
        if settings.data.get("last_project") == slug:
            settings.data["last_project"] = None
            settings.save()
        return {"ok": True}

    def p_manual(slug, request, data):
        name = f"manual-{secrets.token_hex(2)}"
        agent, token = net.local_agent(name)
        manager.assignments[agent.id] = slug
        return {"agent": name, "url": mcp_url, "token": token,
                "claude": f'claude mcp add --transport http decomp {mcp_url} --header "Authorization: Bearer {token}"',
                "json": json.dumps({"mcpServers": {"decomp": {"type": "http", "url": mcp_url,
                                                              "headers": {"Authorization": f"Bearer {token}"}}}}, indent=2)}

    def p_try(slug, request, data):
        """You, editing a function in the GUI: judged like an agent's attempt (and credited to "you")."""
        agent, _ = net.local_agent("you")
        res = net.attempt(agent, str(data.get("name", "")), str(data.get("source", "")), slug)
        return {"score": res["score"], "matched": res["matched"], "text": res["text"][-4000:]}

    def p_release(slug, request, data):
        agent, _ = net.local_agent("you")
        with contextlib.suppress(NetworkError):  # not yours (any more): nothing to give back
            net.release(agent, str(data.get("name", "")), "", slug)
        return {"ok": True}

    def p_instructions(slug, request, data):
        net.set_instructions(slug, str(data.get("text", "")))
        return net.overview(slug)

    def p_priority(slug, request, data):
        return {"priority": net.set_priority(slug, str(data.get("name", "")), bool(data.get("on", True)))}

    def p_note(slug, request, data):
        """A note from you on a function (or the whole project): agents read notes when they claim it."""
        agent, _ = net.local_agent("you")
        text = str(data.get("text", "")).strip()
        if not text:
            raise ValueError("write the note first")
        return {"message": net.add_note(agent, slug, text[:2000], str(data.get("function", "")) or None)}

    def p_add_task(slug, request, data):
        name = str(data.get("name", "")).strip()
        if not name:
            raise ValueError("name the task first")
        return {"name": net.add_analysis_task(None, slug, name, str(data.get("brief", ""))[:4000])}

    def p_analysis_prefill(slug, request, data):
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("name the work item first")

        def work(log):
            return {"prefill": net.analysis_prefill(slug, name, log=log)}
        return {"job": jobs.start("prefill", work)}

    def p_add_feature(slug, request, data):
        value = str(data.get("value", "")).strip()
        if not value:
            raise ValueError("give the feature a seed value")
        def work(log):
            return net.add_feature(slug, str(data.get("kind") or "string"), value,
                                   name=str(data.get("name") or ""), direction=str(data.get("direction") or "both"),
                                   match=str(data.get("match") or "") or None,
                                   case_sensitive=bool(data.get("case_sensitive")), log=log)
        return {"job": jobs.start("feature", work)}

    def p_feature(slug, request, data):
        return net.feature(slug, str(request.query_params.get("name") or ""))

    def p_remove_feature(slug, request, data):
        return net.remove_feature(slug, str(data.get("name") or ""))

    def p_source_file(slug, request, data):
        path = str(request.query_params.get("path") or "")
        if not path:
            raise ValueError("name the source file to read")
        return net.source_file(slug, path)

    def p_export_sources(slug, request, data):
        return {"job": jobs.start("sources", lambda log: net.export_sources(slug, log=log))}

    def p_debug_import(slug, request, data):
        path = str(data.get("path") or "").strip()

        def work(log):
            return net.import_debug_info(slug, path, log=log)
        return {"job": jobs.start("debug", work)}

    def p_debug_export(slug, request, data):
        return net.export_debug_info(slug)

    def p_compare(slug, request, data):
        path = str(data.get("path") or "").strip()

        def work(log):
            return net.compare_version(slug, path)
        return {"job": jobs.start("compare", work)}

    def p_trace(slug, request, data):
        path = str(data.get("path") or "").strip()

        def work(log):
            return net.import_trace(slug, path, log=log)
        return {"job": jobs.start("trace", work)}

    def p_autopilot(slug, request, data):
        if data.get("action") == "stop":
            net.stop_autopilot(slug)
        else:
            net.start_autopilot(slug)
        return net.overview(slug)

    routes = {
        "/api/project": (p_overview, ["GET"]),
        "/api/intake": (lambda s, r, d: net.write_intake(s, log=lambda *_: None), ["GET"]),
        "/api/summary": (lambda s, r, d: net.analysis_summary(s), ["GET"]),
        "/api/contents": (lambda s, r, d: net.analysis_contents(s), ["GET"]),
        "/api/contents/read": (p_contents_read, ["GET"]),
        "/api/analysis/entry": (p_analyze_entry, ["POST"]),
        "/api/live": (p_live, ["GET"]),
        "/api/function": (lambda s, r, d: net.function_detail(s, r.query_params.get("name", "")), ["GET"]),
        "/api/diff": (lambda s, r, d: net.function_diff(s, r.query_params.get("name", ""),
                                                        r.query_params.get("which", "latest")), ["GET"]),
        "/api/compiler": (p_compiler, ["POST"]), "/api/compiler/preview":
            (lambda s, r, d: net.preview_compiler(s, str(d.get("toolchain", "")), d.get("flags") or None), ["POST"]),
        "/api/project/recheck": (lambda s, r, d: net.recheck_project(s), ["POST"]),
        "/api/boundaries/repair": (lambda s, r, d: net.repair_boundaries(s), ["POST"]),
        "/api/export": (p_export, ["POST"]), "/api/open": (p_open, ["POST"]),
        "/api/ide": (p_ide, ["POST"]),
        "/api/delete": (p_delete, ["POST"]), "/api/agents/manual": (p_manual, ["POST"]),
        "/api/try": (p_try, ["POST"]), "/api/release": (p_release, ["POST"]),
        "/api/autopilot": (p_autopilot, ["POST"]), "/api/instructions": (p_instructions, ["POST"]),
        "/api/priority": (p_priority, ["POST"]), "/api/note": (p_note, ["POST"]),
        "/api/analysis/task": (p_add_task, ["POST"]),
        "/api/analysis/prefill": (p_analysis_prefill, ["POST"]),
        "/api/analysis/features": (lambda s, r, d: net.features(s), ["GET"]),
        "/api/analysis/feature": (p_feature, ["GET"]),
        "/api/analysis/feature/add": (p_add_feature, ["POST"]),
        "/api/analysis/feature/remove": (p_remove_feature, ["POST"]),
        "/api/sources": (lambda s, r, d: net.sources(s), ["GET"]),
        "/api/sources/file": (p_source_file, ["GET"]),
        "/api/sources/export": (p_export_sources, ["POST"]),
        "/api/debug/import": (p_debug_import, ["POST"]),
        "/api/debug/export": (p_debug_export, ["POST"]),
        "/api/compare": (p_compare, ["POST"]),
        "/api/trace/import": (p_trace, ["POST"]),
        "/api/stats": (lambda s, r, d: net.stats(s), ["GET"]),
        "/api/data": (lambda s, r, d: {"rows": net.data_symbols(
            s, r.query_params.get("q", ""), int(r.query_params.get("limit", 500) or 500))}, ["GET"]),
    }
    for path, (fn, methods) in routes.items():
        mcp.custom_route(path, methods=methods)(with_project(fn))

