"""The RE console: REA's whole tool catalog, driven by the user from the page.

It has its own REA process (REA keeps one open target per process: the agents' bridge in rea.py keeps
theirs), runs any tool, and hands the page the answer plus what REA says about it (provider,
confidence, limitations). Tools that act on the world (they launch programs, change devices, connect to
pages or write files) only run when the request says the user confirmed them.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid

from .. import rea

# Big parts of an answer the page never draws: kept out so a function view stays a few hundred KB.
_DROP = {"analyze_function": ("native_value_flow",)}
_MAX_ITEMS = 20000


class ConsoleError(ValueError):
    pass


# REA marks most tools "not read-only" (opening a target changes its session) and "open world" (it reads local
# files), so its hints alone would ask before nearly everything. These are the ones that act on the world.
_RUNS = re.compile(r"^(?:capture|observe|start|stop|collect)_|^(?:trace_native_ui_action|reconcile_javascript_runtime)$")
_DEVICE = re.compile(r"^(?:install|uninstall|push)_")
_WRITES = re.compile(r"^(?:extract|export|pull)_")
_NETWORK = re.compile(r"^(?:discover_webmcp_tools|inspect_web_page|analyze_web_bundle|list_browser_targets|list_electron_targets|"
                      r"list_javascript_runtime_targets|inspect_electron_page|inspect_web_event_listeners|inspect_web_network_capture)$")
_HARMLESS = {"close_binary", "unset_bookmark"}  # "destructive" only to REA's own session


def risky(tool: dict) -> list[str]:
    """Why a tool needs the user's confirmation, or nothing: it only reads the target."""
    name, hints = tool.get("name", ""), tool.get("annotations") or {}
    why = []
    if _RUNS.search(name):
        why.append("runs or drives a program, browser or device")
    if _DEVICE.search(name):
        why.append("changes a connected device")
    if _WRITES.search(name):
        why.append("writes files")
    if _NETWORK.search(name):
        why.append("connects to pages or running programs")
    if hints.get("destructiveHint") and name not in _HARMLESS and not why:
        why.append("can change or delete things")
    return why


def _slim_ref(ref):
    if not isinstance(ref, dict):
        return ref
    out = {k: v for k, v in ref.items() if not isinstance(v, dict)}
    for side in ("source_procedure", "target_procedure"):
        proc = ref.get(side)
        if isinstance(proc, dict):
            out[side] = {"address": proc.get("address"), "name": proc.get("name")}
    return out


def slim(tool: str, value):
    """The answer without what the page doesn't use: value-flow dumps, and full procedure records
    repeated inside every reference."""
    if isinstance(value, dict):
        value = {k: v for k, v in value.items() if k not in _DROP.get(tool, ())}
        for key in ("incoming_references", "outgoing_references"):
            if isinstance(value.get(key), list):
                value[key] = [_slim_ref(r) for r in value[key]]
        for key in ("callers", "callees"):
            if isinstance(value.get(key), list):
                value[key] = [{"address": c.get("address"), "name": c.get("name"),
                               "thunk": (c.get("classification") or {}).get("thunk")} if isinstance(c, dict) else c
                              for c in value[key]]
    return value


class Console:
    def __init__(self):
        self.client: rea.Client | None = None
        self.catalog: list[dict] | None = None
        self.target: dict | None = None
        self.lock = threading.RLock()
        self.started = 0.0
        self.connection: dict | None = None

    def _client(self) -> rea.Client:
        if self.client is None or not self.client.alive():
            if self.client is not None:
                self.client.close()
            self.client, self.catalog, self.target, self.connection = None, None, None, None
            cfg = rea.settings()
            self.client = rea.Client(cfg["command"], timeout=1800, env=cfg["env"], provider=cfg.get("provider"))
            self.started = time.time()
            self.connection = {"id": uuid.uuid4().hex, "source": cfg.get("source"), "command": list(self.client.argv),
                               "server": dict(self.client.server_info)}
        return self.client

    def status(self) -> dict:
        cfg = rea.settings()
        running = bool(self.client and self.client.alive())
        return {"settings": rea.public(cfg), "providers": list(rea.PROVIDERS), "package": rea.PACKAGE,
                "configured": bool(cfg["command"]), "running": running, "connection": self.connection if running else None,
                "target": self.target if running else None, "tools": len(self.catalog or []) if running else 0}

    def tools(self) -> list[dict]:
        with self.lock:
            try:
                client = self._client()  # even a cached catalog belongs to a particular live process
                if self.catalog is not None:
                    return self.catalog
                found = client.tools()
            except rea.ReaError as e:
                raise ConsoleError(str(e)) from e
            self.catalog = [{"name": t["name"], "title": t.get("title") or t["name"],
                             "description": t.get("description", ""), "inputSchema": t.get("inputSchema") or {},
                             "annotations": t.get("annotations") or {}, "risky": risky(t)}
                            for t in found if t.get("name")]
            return self.catalog

    def needs_confirm(self, tool: str) -> list[str]:
        return self.prepare_call(tool)[0]

    def prepare_call(self, tool: str) -> tuple[list[str], str]:
        """Check this connection's catalog; queued jobs must execute on that same connection."""
        with self.lock:
            spec = next((t for t in self.tools() if t["name"] == tool), None)
            if spec is None:
                raise ConsoleError(f"REA has no tool named {tool!r}")
            return spec["risky"], self.connection["id"]

    def call(self, tool: str, arguments: dict | None = None, log=None, *, connection_id: str | None = None) -> dict:
        start = time.time()
        with self.lock:
            try:
                if connection_id is not None and (not self.client or not self.client.alive()
                                                   or not self.connection or connection_id != self.connection["id"]):
                    raise ConsoleError("REA reconnected before this tool ran; check the refreshed tool and retry")
                client = self._client()
                ident = self.connection["id"]
                record = client.call(tool, arguments or {}, any_tool=True, envelope=True)
            except rea.ReaError as e:
                raise ConsoleError(str(e)) from e
            value = slim(tool, rea.unwrap(record))
            if tool == "open_binary" and isinstance(value, dict):
                self.target = value.get("result") if isinstance(value.get("result"), dict) else value
            elif tool == "close_binary":
                self.target = None
        meta = {}
        if isinstance(record, dict) and "operation" in record:
            meta = {"provider": (record.get("provider") or {}).get("name"), "confidence": record.get("confidence"),
                    "authority": record.get("authority"), "limitations": record.get("limitations") or [],
                    "evidence_id": record.get("evidence_id"), "subject": record.get("subject")}
        truncated = False
        if isinstance(value, list) and len(value) > _MAX_ITEMS:
            value, truncated = value[:_MAX_ITEMS], True
        return {"tool": tool, "arguments": arguments or {}, "result": value, "meta": meta, "connection_id": ident,
                "ms": round((time.time() - start) * 1000), "truncated": truncated,
                "size": len(json.dumps(value, default=str))}

    def configure(self, command, env: dict | None, provider: str) -> dict:
        # The page shows secret-looking values as •••: sent back unchanged, they keep what was there.
        old = rea.settings()["env"]
        env = {k: (old.get(k, "") if v == "•••" else v) for k, v in (env or {}).items()}
        try:
            rea.save(command, env, provider)
        except rea.ReaError as e:
            raise ConsoleError(str(e)) from e
        self.stop()
        return self.status()

    def stop(self) -> None:
        with self.lock:
            if self.client is not None:
                self.client.close()
            self.client, self.catalog, self.target, self.connection = None, None, None, None
