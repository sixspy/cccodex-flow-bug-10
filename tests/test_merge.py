import json
import sqlite3

import pytest

from app.db import LandBlocked, connect, get_flow, init_db, run_extract, run_land
from app.merge import merge_status, mutate_action, panel_payload


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "merge.db")
    init_db(path)
    return path


def rows(db, sql, params=()):
    conn = connect(db)
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def test_merge_key_stable_for_swapped_sources(db):
    conn = connect(db)
    a = mutate_action(conn, {"flow_id": 1, "left": "alpha", "right": "beta"})
    b = mutate_action(conn, {"flow_id": 1, "left": "beta", "right": "alpha"})
    conn.commit()
    assert a["key"] == b["key"]
    assert len(rows(db, "SELECT * FROM merge_jobs WHERE flow_id=1")) == 1
    conn.close()


def test_conflict_keeps_primary_and_fills_box(db):
    conn = connect(db)
    out = mutate_action(conn, {"flow_id": 1, "left": "new", "right": "old"})
    conn.commit()
    assert out["winner"] == "主源" and out["box"] is True
    box = rows(db, "SELECT body FROM conflict_box WHERE job_id=?", (out["job_id"],))
    assert len(box) == 1
    body = json.loads(box[0]["body"])
    assert body["primary"] == "new" and body["secondary"] == "old"
    assert body["winner"] == "主源" and body["discarded"] == "次源"
    panel = panel_payload(conn, {"flow_id": "1"})
    assert panel["list_label"] == "双源列表"
    assert panel["box_count"] == 1 and panel["conflict_count"] == 1
    conn.close()


def test_single_source_rejected(db):
    conn = connect(db)
    out = mutate_action(conn, {"flow_id": 1, "left": "only"})
    conn.commit()
    assert out["ok"] is False
    conn.close()


def test_land_blocked_until_merge_finished(db):
    conn = connect(db)
    run_extract(conn, 1)
    conn.commit()
    with pytest.raises(LandBlocked):
        run_land(conn, 1)
    mutate_action(conn, {"flow_id": 1, "left": "x", "right": "y", "complete": False})
    conn.commit()
    assert merge_status(conn, 1)["finished"] is False
    with pytest.raises(LandBlocked):
        run_land(conn, 1)
    mutate_action(conn, {"flow_id": 1, "left": "x", "right": "y"})
    conn.commit()
    assert merge_status(conn, 1)["finished"] is True
    run_land(conn, 1)
    conn.commit()
    assert get_flow(conn, 1)["status"] == "已落库"
    conn.close()


def test_unique_index_blocks_duplicate_key(db):
    conn = connect(db)
    from app.merge import ensure

    ensure(conn)
    conn.commit()
    conn.execute(
        "INSERT INTO merge_jobs(flow_id, key, winner) VALUES (1,'k','主源')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO merge_jobs(flow_id, key, winner) VALUES (1,'k','主源')"
        )
    conn.rollback()
    conn.close()


def test_stale_snapshot_flag_does_not_reuse_old_conflict_set(db):
    conn = connect(db)
    mutate_action(conn, {"flow_id": 1, "left": "a", "right": "b"})
    conn.commit()
    out = mutate_action(
        conn, {"flow_id": 1, "left": "a", "right": "a", "snapshot": "stale-token"}
    )
    conn.commit()
    assert out["ok"] is True
    assert out["snapshot_was_stale"] is True
    # 冲突按最新源值重算：a==a 不冲突，即便页面快照里还有旧冲突。
    assert out["conflict"] is False and out["box"] is False
    current = rows(db, "SELECT box FROM merge_jobs WHERE id=?", (out["job_id"],))
    assert current[0]["box"] == 0
    conn.close()
