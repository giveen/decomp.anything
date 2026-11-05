"""Opening a database written by an older version.

Real rooms have months of attempts on disk, and the repeat-submission answer in Network.attempt is worth nothing
if it cannot see the history already there. These tests build the schema an older build left behind and open it
with the current one.
"""

import hashlib
import sqlite3
from pathlib import Path

from decompnet import store


def _old_database(path: Path) -> None:
    """The current schema with the columns this version adds taken back out, plus a little attempt history."""
    old = store.SCHEMA.replace(", source_hash TEXT, compiler TEXT)", ")")
    conn = sqlite3.connect(path)
    conn.executescript(old)
    conn.execute("INSERT INTO projects(id, slug, name, root, config) VALUES(1, 'demo', 'Demo', '/tmp', '{}')")
    conn.execute("INSERT INTO functions(id, project_id, name, n_insns, status) VALUES(1, 1, 'clamp', 6, 'open')")
    for n in range(3):
        conn.execute("INSERT INTO attempts(function_id, agent_id, score, compiled, context_version, source, "
                     "created_at) VALUES(1, 1, ?, 1, 1, ?, ?)", (900 + n, f"int clamp(void) {{ return {n}; }}", n))
    conn.commit()
    conn.close()


def _columns(db: store.Store, table: str) -> set:
    return {row[1] for row in db.all(f"PRAGMA table_info({table})")}


def test_an_older_database_gains_the_columns_and_hashes_its_history(tmp_path):
    db = tmp_path / "room.db"
    _old_database(db)
    assert "source_hash" not in {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(attempts)")}

    opened = store.Store(db)
    assert {"source_hash", "compiler"} <= _columns(opened, "attempts")
    assert "stuck_at" in _columns(opened, "functions")

    # the history is hashed, so resubmitting something tried months ago is recognised against it. Nobody knows
    # what those attempts were built with, so they answer nothing: the compiler column stays empty for them
    hashed = opened.all("SELECT source, source_hash, compiler FROM attempts ORDER BY id")
    assert [row["source_hash"] for row in hashed] == [
        hashlib.sha256(row["source"].encode()).hexdigest() for row in hashed]
    assert all(row["compiler"] is None for row in hashed)


def test_the_history_is_hashed_once_however_often_the_room_opens(tmp_path):
    """Rehashing on every start would make opening a big room slower forever, so it is a one-time migration."""
    db = tmp_path / "room.db"
    _old_database(db)
    store.Store(db)

    opened = store.Store(db)
    with opened.tx() as conn:
        conn.execute("UPDATE attempts SET source_hash='stale' WHERE id=1")

    assert store.Store(db).one("SELECT source_hash FROM attempts WHERE id=1")["source_hash"] == "stale"
