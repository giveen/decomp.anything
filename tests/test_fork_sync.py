"""Fork sync cleanup preserves work while staying below SQLite's bound-variable limit.

Use a supplied objdiff report and real project/database files, without requiring a compiler or objdiff.
"""

import json
import sqlite3

import pytest

from decompnet.app.engine import RomNetwork


@pytest.fixture
def fork(tmp_path):
    root = tmp_path / "fork"
    root.mkdir()
    (root / "objdiff.json").write_text(json.dumps({"units": [
        {"name": "module/current", "metadata": {"source_path": "src/current.c"}},
    ]}))
    net = RomNetwork(tmp_path / "state.db", tmp_path)
    agent, _ = net.local_agent("sync-tester")
    with net.store.tx() as db:
        for pid, slug in ((1, "fork"), (2, "other")):
            db.execute("INSERT INTO projects(id,slug,root,config) VALUES(?,?,?,?)",
                       (pid, slug, str(root), json.dumps({"kind": "fork"})))
    try:
        yield net, agent
    finally:
        net._pool.shutdown(wait=True)
        net.store.conn.close()


@pytest.mark.parametrize("obsolete_count", [0, 1, 500, 501, 1001])
def test_sync_cleans_obsolete_functions_below_sqlite_variable_limit(fork, obsolete_count):
    net, agent = fork
    current_id, other_id = obsolete_count + 1, obsolete_count + 2
    # History on both sides of batch boundaries and on the final record must survive.
    retained = {i for i in (1, 500, 501, 1000, obsolete_count) if 1 <= i <= obsolete_count}
    survivors = retained | {current_id, other_id}
    with net.store.tx() as db:
        for fid in range(1, other_id + 1):
            name = "current" if fid == current_id else f"function_{fid}"
            unit = "module/current" if fid == current_id else "module/old"
            db.execute("INSERT INTO functions(id,project_id,name,size,n_insns,status,unit,meta) "
                       "VALUES(?,?,?,?,?,'open',?,?)",
                       (fid, 2 if fid == other_id else 1, name, 4, 1, unit, json.dumps({"symbol": name})))
            db.execute("INSERT INTO claims(function_id,agent_id,claimed_at,expires_at) VALUES(?,?,1,9999999999)",
                       (fid, agent.id))
            db.execute("INSERT INTO notes(project_id,function_id,agent_id,text) VALUES(?,?,?,?)",
                       (2 if fid == other_id else 1, fid, agent.id, f"note for {fid}"))
            if fid in survivors:
                # Two attempts exercise DISTINCT and preserve more than just the presence of history.
                for score in (0, 1):
                    db.execute("INSERT INTO attempts(function_id,agent_id,source,score) VALUES(?,?,?,?)",
                               (fid, agent.id, f"source for {fid}, score {score}", score))

    attempts_before = [dict(r) for r in net.store.all("SELECT * FROM attempts ORDER BY id")]
    notes_before = [dict(r) for r in net.store.all("SELECT * FROM notes ORDER BY id") if r["function_id"] in survivors]
    claims_before = [dict(r) for r in net.store.all("SELECT * FROM claims ORDER BY function_id") if r["function_id"] in survivors]
    other_before = dict(net.store.one("SELECT * FROM functions WHERE id=?", other_id))
    # Reproduce the historical limit even on builds that allow tens of thousands of variables.
    conn = net.store.conn
    old_limit = conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
    try:
        assert conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER) == 999
        net.forks.sync("fork", log=lambda *_: None, rep={"units": [
            {"name": "module/current", "functions": [
                {"name": "current", "size": 4, "fuzzy_match_percent": 50},
            ]},
        ], "measures": {}})
    finally:
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, old_limit)

    functions = {r["id"]: r["status"] for r in net.store.all("SELECT id,status FROM functions")}
    assert functions == {**dict.fromkeys(retained, "nosource"), current_id: "open", other_id: "open"}
    assert [dict(r) for r in net.store.all("SELECT * FROM attempts ORDER BY id")] == attempts_before
    assert [dict(r) for r in net.store.all("SELECT * FROM notes ORDER BY id")] == notes_before
    assert [dict(r) for r in net.store.all("SELECT * FROM claims ORDER BY function_id")] == claims_before
    assert dict(net.store.one("SELECT * FROM functions WHERE id=?", other_id)) == other_before
