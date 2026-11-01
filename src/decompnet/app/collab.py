"""Rooms: several computers working on one decomp at once.

A room is a project one person shares and others join with their own copy of the game. The host's copy is the truth:
what each member claims, attempts, matches and notes is applied to it, and each member syncs what everyone else did. The
agents work exactly as they do alone; only where their results go changes.

The three sides live beside this module: collabroom.py (the host: who is in, and the API members talk to),
collabtunnel.py (how another computer reaches the host), collablink.py (the member: join, then keep the copies in step),
and collabwire.py (the numbers and hashes both sides agree on). The names the rest of the app uses are re-exported here,
so `collab.<name>` keeps meaning what it did before the split.
"""

from __future__ import annotations

from .collablink import Link, join_room, reconnect
from .collabroom import Room, host_app
from .collabtunnel import Tunnel, cloudflared_available, local_addresses, parse_invite
from .collabwire import BACKOFF_MAX, LAYOUT_OVERLAP, MAX_BODY, NAME, SYNC_EVERY, TUNNEL_URL

__all__ = [
           "BACKOFF_MAX",
           "LAYOUT_OVERLAP",
           "MAX_BODY",
           "NAME",
           "SYNC_EVERY",
           "TUNNEL_URL",
           "Link",
           "Room",
           "Tunnel",
           "cloudflared_available",
           "host_app",
           "join_room",
           "local_addresses",
           "parse_invite",
           "reconnect",
]
