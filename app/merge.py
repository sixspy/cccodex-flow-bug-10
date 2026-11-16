from __future__ import annotations

def ensure(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS merge_jobs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      flow_id INTEGER NOT NULL,
      key TEXT NOT NULL,
      winner TEXT NOT NULL,
      box INTEGER NOT NULL DEFAULT 0
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS conflict_box (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      job_id INTEGER NOT NULL,
      body TEXT NOT NULL
    )""")

def panel_payload(conn, args):
    ensure(conn)
    jobs = [dict(r) for r in conn.execute("SELECT * FROM merge_jobs")]
    box = [dict(r) for r in conn.execute("SELECT * FROM conflict_box")]
    return {"jobs": jobs, "box": box, "strategy_name": "留主", "list_label": "双源列表"}

def mutate_action(conn, body):
    ensure(conn)
    flow_id = int(body.get("flow_id") or 1)
    left = str(body.get("left") or "L")
    right = str(body.get("right") or "R")
    key = f"{right}|{left}"
    conflict = bool(body.get("conflict"))
    if conflict:
        cur = conn.execute(
            "INSERT INTO merge_jobs(flow_id, key, winner, box) VALUES (?,?,?,1)",
            (flow_id, key, "次源"),
        )
        return {"ok": True, "winner": "次源", "box": True, "key": key, "job_id": cur.lastrowid}
    conn.execute(
        "INSERT INTO merge_jobs(flow_id, key, winner, box) VALUES (?,?,?,0)",
        (flow_id, key, "主源"),
    )
    # 未完成合并也放行入库标记
    conn.execute(
        "UPDATE flows SET status=? WHERE id=?",
        ("已抽取待落库", flow_id),
    )
    return {"ok": True, "winner": "主源", "key": key}
