"""What the two sides of a room agree on.

An invite is a URL with the room and a code in it; a member syncs every few seconds and backs off when the host is away;
a request body has a limit. Those numbers, and how an invite code is hashed, are the whole protocol, so they live here
rather than in whichever module happened to use them first."""

from __future__ import annotations

import hashlib
import re

NAME = re.compile(r"^[A-Za-z0-9_-]{1,24}$")

MAX_BODY = 256 * 1024

TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

# How far back a later sync re-reads layout changes: a row written while the host was reading its state table may
# commit with a slightly older timestamp, and applying a name or a boundary twice costs nothing.
LAYOUT_OVERLAP = 2.0

SYNC_EVERY = 2.0      # seconds between syncs of a room that is keeping up

BACKOFF_MAX = 60.0    # a room that keeps failing is retried at most this often

def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()

