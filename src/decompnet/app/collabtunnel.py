"""Reaching the host's room from another computer: an address, or a Cloudflare quick tunnel.

A room shared on a LAN or a Tailscale address needs no help; one shared from a home computer gets a quick tunnel, which
needs no account and exposes only the room's port. local_addresses lists what this computer can be reached at, for the
host to pick from."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time

from .. import toolchains
from ..network import NetworkError
from .collabwire import TUNNEL_URL


class Tunnel:
    """A Cloudflare quick tunnel (free, no account) to the collab server."""

    def __init__(self, port: int):
        self.port, self.proc, self.url = port, None, None

    def start(self, log=print, timeout: float = 45) -> str:
        if toolchains.installed("cloudflared") is None:
            toolchains.install("cloudflared", log=log)
        exe_dir = toolchains.path("cloudflared")
        exe = exe_dir / ("cloudflared.exe" if (exe_dir / "cloudflared.exe").exists() else "cloudflared")
        self.proc = subprocess.Popen([str(exe), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self.port}"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                     errors="replace")
        found = threading.Event()

        def read():
            for line in self.proc.stderr or ():
                m = TUNNEL_URL.search(line)
                if m and not self.url:
                    self.url = m.group(0)
                    found.set()
        threading.Thread(target=read, daemon=True).start()
        if not found.wait(timeout):
            self.stop()
            raise RuntimeError("couldn't open a tunnel (is the internet reachable?)")
        if not self.url:  # found.wait timed out and didn't raise: no URL to hand back
            raise RuntimeError("couldn't open a tunnel (is the internet reachable?)")
        log(f"tunnel open: {self.url}")
        return self.url

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.proc, self.url = None, None

def parse_invite(link: str) -> tuple[str, str]:
    """(base url, invite code) from a link like https://abc.trycloudflare.com/join/CODE#room=slug"""
    link = link.strip().rstrip("/")
    if re.match(r"^https?://[^/\s]+$", link):  # a public room's bare address
        return link, "open"
    m = re.match(r"^(https?://[^/\s]+(?::\d+)?)/join/([A-Za-z0-9_-]{4,})(?:#room=([\w-]+))?$", link)
    if not m:
        raise NetworkError("that doesn't look like a decomp.wtf invite link")
    return m.group(1), m.group(2) + (f"#{m.group(3)}" if m.group(3) else "")

def cloudflared_available() -> bool:
    return toolchains.get("cloudflared").supported or bool(shutil.which("cloudflared"))

_addresses: tuple[float, list[dict]] = (0.0, [])

def local_addresses() -> list[dict]:
    """Addresses teammates might reach this computer at: the LAN address, and Tailscale's when it's installed."""
    global _addresses
    if time.time() - _addresses[0] < 600:
        return _addresses[1]
    _addresses = (time.time(), _local_addresses())
    return _addresses[1]

def _local_addresses() -> list[dict]:
    import socket

    out = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))  # no packet is sent: this only picks the outgoing interface
            ip = sock.getsockname()[0]
        if not ip.startswith("127."):
            out.append({"label": "this network (LAN)", "address": f"http://{ip}:6481"})
    except OSError:
        pass
    ts = shutil.which("tailscale")
    if ts:
        try:
            res = subprocess.run([ts, "status", "--json"], capture_output=True, text=True, timeout=5,
                                 encoding="utf-8", errors="replace")
            me = json.loads(res.stdout).get("Self") or {}
            name = (me.get("DNSName") or "").rstrip(".")
            ips = [a for a in me.get("TailscaleIPs") or [] if "." in a]
            if name or ips:
                out.append({"label": "Tailscale (your tailnet)", "address": f"http://{name or ips[0]}:6481"})
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    return out

