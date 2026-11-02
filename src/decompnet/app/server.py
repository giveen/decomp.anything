"""The local decomp.anything server: the GUI, its API, and the MCP endpoint agents connect to.

It listens on 127.0.0.1 only. The GUI needs a per-launch secret (handed to the browser in the URL the
app opens, then kept in a same-site cookie); agents need their own bearer token. Host headers are
checked on every request, so web pages can't reach it through DNS rebinding.

What a route does lives in routes_pages, routes_projects, routes_forks, routes_setup, routes_rea, routes_updates and
routes_rooms; they all register on the same MCP app and share one ServerContext (serverctx). This module builds that
context, wires the pieces that outlive a request (the agent manager, the update checkers, the rooms) and returns the app.
"""

from __future__ import annotations

import threading
from pathlib import Path

from mcp.server.transport_security import TransportSecuritySettings

from .. import rea
from ..network import NetworkError
from . import agents, chat, collab, rea_updates, tools, updates
from .console import Console
from .engine import RomNetwork
from .routes_forks import register as register_forks
from .routes_pages import register as register_pages
from .routes_projects import register as register_projects
from .routes_rea import register as register_rea
from .routes_rooms import register as register_rooms
from .routes_setup import register as register_setup
from .routes_updates import register as register_updates
from .serverctx import (
    Jobs,
    ServerContext,
    Settings,
    _analyze_instead,
    _launcher,
    _session_options,
    _spread,
    _static,
    _tool_of,
    file_log,
    job_error,
)

# What the tests and tools import from here, and the app's own plumbing they are allowed to reach: everything else moved
# to serverctx, which is also where the page's guards live.
__all__ = ["Jobs", "RomNetwork", "ServerContext", "Settings", "_analyze_instead", "_launcher", "_session_options",
           "_spread", "_static", "_tool_of", "build", "file_log", "job_error"]


def build(net: RomNetwork, home: Path, port: int, secret: str):
    manager = agents.Manager(net, home, f"http://127.0.0.1:{port}/mcp")
    jobs = Jobs(home / "logs" / "decomp.log")
    settings = Settings(home / "settings.json")
    manager.budget = lambda: float(settings.data.get("budget_usd") or 0)
    rea.CONFIG = home / "rea.json"  # how REA starts: the RE console's settings, the agents' bridge reads them too
    console = Console()

    def remember(running: list[dict]) -> None:
        settings.data["running_agents"] = running
        settings.save()
    manager.persist = remember

    ctx = ServerContext(net, home, port, secret, manager, jobs, settings, console)
    ctx.build_id = updates.running_id()  # which code this process is: the checkout's HEAD, or the installed commit

    threading.Thread(target=collab.local_addresses, daemon=True).start()  # `tailscale status` can take seconds
    # rooms this computer belongs to reconnect (members keep their place; their agents wait while the host is away)
    net.restore_links()

    # helpers that were working when the app last stopped carry on
    resume = [r for r in settings.data.get("running_agents", []) if isinstance(r, dict)]
    for r in resume:
        try:
            net.project_row(r["slug"])
            options = dict(r.get("options") or {})
            if r["kind"] == "api":
                saved = settings.endpoint(str(options.get("endpoint") or "")) if options.get("endpoint") else (
                    next((e for e in settings.endpoints() if e["base_url"] == options.get("base_url")), None))
                if saved is None and options.get("endpoint"):
                    continue  # its endpoint was removed since
                if saved is not None:  # as the endpoint is now, with its key
                    options.update(_session_options(saved))
            manager.start(r["kind"], r["slug"], 1, options)
        except (NetworkError, ValueError, KeyError):
            continue

    mcp = tools.build_mcp(net, ctx.agent_of)
    ctx.mcp = mcp
    chat.register(mcp, net, manager, jobs, settings, port, secret)

    # Each agent sees the tools for its job: the agent you talk to sees everything; a helper only the work tools
    # for its project's kind (fewer tool definitions in every request, nothing it shouldn't call).
    from mcp_types import ListToolsResult, PaginatedRequestParams

    async def scoped_tools(ctx, params):
        everything = await mcp.list_tools()
        auth = (getattr(ctx.request, "headers", None) or {}).get("authorization", "")
        try:
            agent = net.authenticate(auth.removeprefix("Bearer ").strip() or None)
        except NetworkError:
            return ListToolsResult(tools=everything)
        if net.is_chat(agent):
            return ListToolsResult(tools=everything)
        slug = manager.project_of(agent.id) or settings.data.get("last_project")
        try:
            analysis = bool(slug and net.is_analysis(net.project_row(slug)))
        except NetworkError:
            analysis = False
        keep = set(tools.tool_names(bool(slug and net.fork_slug(slug)), analysis))
        return ListToolsResult(tools=[t for t in everything if t.name in keep])

    mcp._lowlevel_server.add_request_handler("tools/list", PaginatedRequestParams, scoped_tools)

    # The two update checkers outlive a request (a route asks them, a thread polls them), and both restart through the
    # same path: stop the helpers now, come back working.
    ctx.updater = updates.Updater(ctx.before_restart, port, secret)
    ctx.rea_updater = updates.Updater(ctx.before_restart, port, secret, checker=rea_updates.check,
                                      installer=rea_updates.install)

    register_pages(ctx)
    register_projects(ctx)
    register_forks(ctx)
    register_setup(ctx)
    register_rea(ctx)
    register_updates(ctx)
    register_rooms(ctx)  # also sets manager.on_stop: a room's tunnel closes when the app does

    threading.Thread(target=ctx.watch_updates, daemon=True, name="updates").start()

    security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=sorted(ctx.allowed_hosts),
                                         allowed_origins=[f"http://{h}" for h in sorted(ctx.allowed_hosts)])
    return mcp, manager, security
