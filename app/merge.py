from __future__ import annotations

import hashlib
import json
import sqlite3
import threading

# 合并动作必须排队执行：同一进程内的并发请求（以及同一连接上的重入调用）
# 共用这把可重入锁；跨进程则由 SQLite 的写事务（BEGIN IMMEDIATE）串行化。
_LOCK = threading.RLock()

PRIMARY = "主源"
SECONDARY = "次源"
STATUS_DONE = "已合并"
STATUS_PENDING = "待确认"

_MERGE_JOB_COLUMNS = (
    ("status", "TEXT NOT NULL DEFAULT '已合并'"),
    ("left_value", "TEXT NOT NULL DEFAULT ''"),
    ("right_value", "TEXT NOT NULL DEFAULT ''"),
    ("updated_at", "TEXT NOT NULL DEFAULT ''"),
)


class _InputError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def merge_guard():
    """供路由层使用：把「重算冲突 -> 合并 -> 入库门禁」串行化。"""
    return _LOCK


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _index_names(conn, table: str) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,)
        )
    }


def ensure(conn):
    """幂等建表/升级。已就绪时只读、不取写锁，避免只读路径阻塞并发写入。"""
    conn.execute("""CREATE TABLE IF NOT EXISTS merge_jobs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      flow_id INTEGER NOT NULL,
      key TEXT NOT NULL,
      winner TEXT NOT NULL,
      box INTEGER NOT NULL DEFAULT 0,
      status TEXT NOT NULL DEFAULT '已合并',
      left_value TEXT NOT NULL DEFAULT '',
      right_value TEXT NOT NULL DEFAULT '',
      updated_at TEXT NOT NULL DEFAULT ''
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS conflict_box (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      job_id INTEGER NOT NULL,
      body TEXT NOT NULL
    )""")

    # 旧库升级：补齐新增列
    existing = _columns(conn, "merge_jobs")
    for name, ddl in _MERGE_JOB_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE merge_jobs ADD COLUMN {name} {ddl}")

    # 冲突匣只存有内容的记录：清掉历史遗留的空匣行。
    if conn.execute(
        "SELECT 1 FROM conflict_box "
        "WHERE body IS NULL OR trim(body)='' OR trim(body)='{}' LIMIT 1"
    ).fetchone():
        conn.execute(
            "DELETE FROM conflict_box "
            "WHERE body IS NULL OR trim(body)='' OR trim(body)='{}'"
        )

    # 合并键在单个编排流内唯一：同一行只能对应一单。
    if "ux_merge_jobs_flow_key" not in _index_names(conn, "merge_jobs"):
        if conn.execute(
            "SELECT 1 FROM merge_jobs GROUP BY flow_id, key HAVING COUNT(*) > 1 LIMIT 1"
        ).fetchone():
            conn.execute(
                "DELETE FROM merge_jobs WHERE id NOT IN "
                "(SELECT MAX(id) FROM merge_jobs GROUP BY flow_id, key)"
            )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_merge_jobs_flow_key "
            "ON merge_jobs(flow_id, key)"
        )
    if "ux_conflict_box_job" not in _index_names(conn, "conflict_box"):
        if conn.execute(
            "SELECT 1 FROM conflict_box GROUP BY job_id HAVING COUNT(*) > 1 LIMIT 1"
        ).fetchone():
            conn.execute(
                "DELETE FROM conflict_box WHERE id NOT IN "
                "(SELECT MAX(id) FROM conflict_box GROUP BY job_id)"
            )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_conflict_box_job "
            "ON conflict_box(job_id)"
        )


def _begin_immediate(conn):
    """让多个写入者排队，避免两单同时落下互相覆盖。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        # 已有隐式事务（ensure/前序 DML 开启）时先收尾，再取写锁。
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")


def _pick(body: dict, *names: str):
    for name in names:
        if name in body and body[name] is not None:
            return body[name]
    return None


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y", "t")


def _normalise_pair(body: dict) -> tuple[str, str, bool]:
    """取出两路源值。返回 (主源值, 次源值, 是否由请求显式给出)。

    只给出一路源值时无法做同源核对，直接拒绝。
    """
    left_raw = _pick(body, "left", "left_value", "primary", "main_value")
    right_raw = _pick(body, "right", "right_value", "secondary", "sub_value")
    if (left_raw is None) != (right_raw is None):
        raise _InputError("同源核对失败：必须同时提供主源与次源值")
    if left_raw is None:
        # 兼容仅传递冲突标记的旧调用：两路源使用占位值。
        return "L", "R", False
    left, right = str(left_raw), str(right_raw)
    if not left.strip() or not right.strip():
        raise _InputError("同源核对失败：两路源值不能为空")
    return left, right, True


def merge_key(body: dict, left: str, right: str) -> str:
    """稳定且唯一的合并键。

    优先使用调用方给出的业务键；否则由两路源值生成与先后顺序无关的摘要，
    因此主次源颠倒不会把同一行拆成两单。
    """
    explicit = _pick(body, "key", "merge_key", "row_key", "business_key")
    if explicit is not None and str(explicit).strip():
        return str(explicit).strip()
    a, b = sorted((str(left), str(right)))
    payload = json.dumps([a, b], ensure_ascii=False, separators=(",", ":"))
    return "mk_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def resolve_conflict(left: str, right: str, complete: bool, body: dict | None = None) -> bool:
    """以两路源值重算冲突，不采信页面打开时的冲突快照。"""
    if complete:
        return left != right
    return _as_bool((body or {}).get("conflict"))


def _snapshot_rows(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, flow_id, key, left_value, right_value, winner, status "
        "FROM merge_jobs WHERE box=1 ORDER BY flow_id, key"
    )
    return [
        {
            "job_id": row["id"],
            "flow_id": row["flow_id"],
            "key": row["key"],
            "left": row["left_value"],
            "right": row["right_value"],
            "winner": row["winner"],
            "status": row["status"],
        }
        for row in rows
    ]


def _sanitize(conn, flow_id: int):
    """清掉冲突匣里对应的空内容行，避免详情页出现空匣。"""
    conn.execute(
        "DELETE FROM conflict_box WHERE job_id IN "
        "(SELECT id FROM merge_jobs WHERE flow_id=?) "
        "AND (body IS NULL OR trim(body)='' OR body='{}')",
        (flow_id,),
    )


def _snapshot_token(rows: list[dict]) -> str:
    payload = [[r["flow_id"], r["key"], r["left"], r["right"]] for r in rows]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _arg_int(args, name: str):
    """Sanic 的 request.args 值是列表，需逐个解包；非法值按未提供处理。"""
    if not args:
        return None
    raw = args.get(name)
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    if raw is None or str(raw).strip() == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def panel_payload(conn, args):
    with _LOCK:
        ensure(conn)
        flow_id = _arg_int(args, "flow_id")

        if flow_id is None:
            jobs = [dict(r) for r in conn.execute(
                "SELECT * FROM merge_jobs ORDER BY flow_id, key"
            )]
        else:
            jobs = [dict(r) for r in conn.execute(
                "SELECT * FROM merge_jobs WHERE flow_id=? ORDER BY key", (flow_id,)
            )]

        box_sql = (
            "SELECT b.id, b.job_id, b.body, j.flow_id, j.key "
            "FROM conflict_box b LEFT JOIN merge_jobs j ON j.id=b.job_id"
        )
        box_params: tuple = ()
        if flow_id is not None:
            box_sql += " WHERE j.flow_id=?"
            box_params = (flow_id,)
        box_sql += " ORDER BY b.id"
        box = [dict(r) for r in conn.execute(box_sql, box_params)]

        snap_rows = _snapshot_rows(conn)
        token = _snapshot_token(snap_rows)
        selected = snap_rows if flow_id is None else [
            r for r in snap_rows if r["flow_id"] == flow_id
        ]
        finished = bool(jobs) and all(
            str(j.get("status") or "") == STATUS_DONE for j in jobs
        )
        return {
            "jobs": jobs,
            "box": box,
            "strategy_name": "留主",
            "list_label": "双源列表",
            # 打开合并页时的快照；确认合并时按最新源值重算，二者不混用。
            "conflict_snapshot": selected,
            "snapshot": token,
            "snapshot_id": token,
            "conflict_count": len(selected),
            "box_count": len(box),
            "finished": finished,
        }


def mutate_action(conn, body, _retry: int = 0):
    if _retry > 5:
        return {"ok": False, "error": "合并冲突重试超限", "status": 409}
    with _LOCK:
        ensure(conn)
        try:
            try:
                flow_id = int(body.get("flow_id") or 1)
            except (TypeError, ValueError):
                raise _InputError("flow_id 无效")

            left, right, complete = _normalise_pair(body)
            key = merge_key(body, left, right)

            finish_raw = _pick(body, "complete", "finish", "done")
            finish = True if finish_raw is None else _as_bool(finish_raw)
            # 同源核对通过后，冲突以两路最新源值为准重算。
            conflict = resolve_conflict(left, right, complete, body)

            # 快照仅用于报告页面数据是否已过期，绝不参与本次判定。
            supplied_snapshot = _pick(
                body, "snapshot", "snapshot_id", "conflict_snapshot_id"
            )
        except _InputError as exc:
            return {"ok": False, "error": exc.message, "status": exc.status}

        try:
            _begin_immediate(conn)
            _sanitize(conn, flow_id)
            before_token = _snapshot_token(_snapshot_rows(conn))
            snapshot_was_stale = (
                supplied_snapshot is not None
                and str(supplied_snapshot) != before_token
            )

            existing = conn.execute(
                "SELECT * FROM merge_jobs WHERE flow_id=? AND key=?",
                (flow_id, key),
            ).fetchone()
            status = STATUS_DONE if finish else STATUS_PENDING
            box_flag = 1 if conflict else 0

            # 合并键唯一：同一 flow_id + key 只保留一单，重复提交更新该单而不是新开。
            if existing is None:
                cur = conn.execute(
                    "INSERT INTO merge_jobs"
                    "(flow_id, key, winner, box, status, left_value, right_value, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,datetime('now'))",
                    (flow_id, key, PRIMARY, box_flag, status, left, right),
                )
                job_id = cur.lastrowid
                deduplicated = False
            else:
                job_id = existing["id"]
                conn.execute(
                    "UPDATE merge_jobs SET winner=?, box=?, status=?, "
                    "left_value=?, right_value=?, updated_at=datetime('now') WHERE id=?",
                    (PRIMARY, box_flag, status, left, right, job_id),
                )
                deduplicated = True

            if conflict:
                # 冲突留主，并把被舍弃的次源值连同两路源值落进冲突匣，
                # 保证详情页打开时匣内有实际内容可核对。
                box_json = json.dumps(
                    {
                        "flow_id": flow_id,
                        "key": key,
                        "primary": left,
                        "secondary": right,
                        "winner": PRIMARY,
                        "discarded": SECONDARY,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                conn.execute(
                    "INSERT INTO conflict_box(job_id, body) VALUES (?,?) "
                    "ON CONFLICT(job_id) DO UPDATE SET body=excluded.body",
                    (job_id, box_json),
                )
            else:
                conn.execute("DELETE FROM conflict_box WHERE job_id=?", (job_id,))

            # 合并动作本身不推动入库；入库由独立的门禁在合并收尾后放行。
            conn.commit()
        except sqlite3.IntegrityError:
            # 并发下唯一索引兜底：回滚后重试，读取已落下的那一单并幂等更新。
            conn.rollback()
            return mutate_action(conn, body, _retry + 1)
        except Exception:
            conn.rollback()
            raise

        after_token = _snapshot_token(_snapshot_rows(conn))
        return {
            "ok": True,
            "winner": PRIMARY,
            "box": conflict,
            "key": key,
            "job_id": job_id,
            "conflict": conflict,
            "status": status,
            "finished": finish,
            "deduplicated": deduplicated,
            "snapshot": after_token,
            "snapshot_before": before_token,
            "snapshot_was_stale": snapshot_was_stale,
        }


def merge_status(conn, flow_id: int) -> dict:
    """入库门禁：必须存在合并单，且该流的合并全部收尾。"""
    with _LOCK:
        ensure(conn)
        jobs = [dict(r) for r in conn.execute(
            "SELECT id, key, winner, box, status FROM merge_jobs WHERE flow_id=?",
            (flow_id,),
        )]
        box_count = conn.execute(
            "SELECT COUNT(*) c FROM conflict_box b "
            "JOIN merge_jobs j ON j.id=b.job_id WHERE j.flow_id=?",
            (flow_id,),
        ).fetchone()["c"]
        pending = sum(
            1 for j in jobs if str(j.get("status") or "") != STATUS_DONE
        )
        return {
            "flow_id": flow_id,
            "finished": bool(jobs) and pending == 0,
            "total": len(jobs),
            "pending": pending,
            "conflicts": sum(1 for j in jobs if j["box"]),
            "box": box_count,
        }
