"""全站共享的「当日入库池」领域服务。

核心约束：
- 池子是跨任务共享资源，按后台日期结算，不是单个任务自己的计数器。
- 池容量与分配口径在设置页选定；口径选定后写入当日生效口径，当天禁止改口。
- 均摊（equal）：按等待先后轮询均摊；权重（weight）：按任务声明的权重做加权轮询。
  多个任务抢同一天的池时，禁止谁先点谁全拿。
- 占池期间其它任务只能把合法编码停在候补栏（等池）。
- 编码为空的行既不占池也不进等池。
- 跨日后昨天的等池不会自动入账，必须点继续，按新一天的池容量和当日口径重算。
"""

import json
import sqlite3

from app.services.clock import current_date

DEFAULT_CAPACITY = 20
POLICY_EQUAL = "equal"
POLICY_WEIGHT = "weight"
POLICY_NAMES = {
    POLICY_EQUAL: "按等待先后均摊",
    POLICY_WEIGHT: "按任务权重切分",
}
VALID_POLICIES = tuple(POLICY_NAMES.keys())

# 事件类型
EVENT_CONTENTION = "池争用"
EVENT_WRONG_POLICY = "切法不对"


# ---------------------------------------------------------------- 基础读取


def _get_setting(db, key: str, default=None):
    row = db.execute(
        "SELECT value FROM app_settings WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else default


def get_settings(db) -> dict:
    cap_raw = _get_setting(db, "pool_capacity")
    capacity_input = cap_raw or ""
    try:
        capacity = int(cap_raw) if cap_raw not in (None, "") else DEFAULT_CAPACITY
    except (TypeError, ValueError):
        capacity = DEFAULT_CAPACITY
    if capacity < 1:
        capacity = DEFAULT_CAPACITY
    policy = _get_setting(db, "allocation_policy") or POLICY_EQUAL
    if policy not in VALID_POLICIES:
        policy = POLICY_EQUAL
    return {
        "pool_capacity": capacity,
        "pool_capacity_input": capacity_input,
        "allocation_policy": policy,
    }


def _log_event(db, day: str, event_type: str, job_id, detail: str):
    """写一条池事件（沿用调用方当前事务）。"""
    db.execute(
        """
        INSERT INTO pool_events (event_day, event_type, job_id, detail, created_at)
        VALUES (?, ?, ?, ?, datetime('now'))
        """,
        (day, event_type, job_id, detail),
    )


def _reject_and_persist(db, day: str, event_type: str, job_id, detail: str, message: str):
    """拦截类校验：先把事件落库，再重开空事务让外层 with 正常回滚/提交。

    保证「切法不对」这类审计记录不会被外层校验失败的回滚带走。
    """
    _log_event(db, day, event_type, job_id, detail)
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    raise ValueError(message)


def ensure_day(db, day: str | None = None) -> sqlite3.Row:
    """确保当日池快照存在，返回当日池行。

    容量与口径在当天首次使用时从设置快照写入，之后设置页当天改口不影响当日池。
    """
    day = day or current_date()
    row = db.execute(
        "SELECT * FROM daily_pool WHERE pool_day = ?", (day,)
    ).fetchone()
    if row is not None:
        return row
    settings = get_settings(db)
    db.execute(
        """
        INSERT OR IGNORE INTO daily_pool (pool_day, capacity, allocation_policy, used_count)
        VALUES (?, ?, ?, 0)
        """,
        (day, settings["pool_capacity"], settings["allocation_policy"]),
    )
    return db.execute(
        "SELECT * FROM daily_pool WHERE pool_day = ?", (day,)
    ).fetchone()


# ---------------------------------------------------------------- 设置页口径


def update_settings(db, capacity_raw: str, policy: str) -> dict:
    """更新设置页的池容量与分配口径。

    - 容量不填按 20；容量对当天尚未生成快照的日子立即生效，已有快照则次日生效。
    - 口径一旦写入当日快照，当天禁止改口，拦截并立即落库一条「切法不对」。
    """
    if policy not in VALID_POLICIES:
        raise ValueError("切法不对：未知的分配口径")
    try:
        capacity = int(capacity_raw) if str(capacity_raw).strip() != "" else DEFAULT_CAPACITY
    except (TypeError, ValueError):
        raise ValueError("池容量必须是正整数，不填按二十")
    if capacity < 1:
        raise ValueError("池容量必须是正整数，不填按二十")

    day = current_date()
    today = db.execute(
        "SELECT * FROM daily_pool WHERE pool_day = ?", (day,)
    ).fetchone()
    if today is not None and today["allocation_policy"] != policy:
        _reject_and_persist(
            db,
            day,
            EVENT_WRONG_POLICY,
            None,
            f"当日口径已锁定为「{POLICY_NAMES[today['allocation_policy']]}」，"
            f"改用「{POLICY_NAMES[policy]}」被拦截",
            f"切法不对：当天口径已锁定为「{POLICY_NAMES[today['allocation_policy']]}」，"
            "当天禁止改口，次日生效",
        )

    db.execute(
        """
        INSERT INTO app_settings (key, value) VALUES ('pool_capacity', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (str(capacity),),
    )
    db.execute(
        """
        INSERT INTO app_settings (key, value) VALUES ('allocation_policy', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (policy,),
    )


# ---------------------------------------------------------------- 占池锁


def acquire_pool_lock(db, job_id: int, day: str) -> bool:
    """尝试占用池子。已被其它任务占用时返回 False。"""
    try:
        db.execute(
            "INSERT INTO pool_lock (id, job_id, lock_day, locked_at) "
            "VALUES (1, ?, ?, datetime('now'))",
            (job_id, day),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def release_pool_lock(db, job_id: int):
    db.execute("DELETE FROM pool_lock WHERE id = 1 AND job_id = ?", (job_id,))


def current_lock(db) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM pool_lock WHERE id = 1").fetchone()


# ---------------------------------------------------------------- 分配规划


def _job_weights(db, job_ids: list[int]) -> dict[int, int]:
    if not job_ids:
        return {}
    placeholders = ",".join("?" for _ in job_ids)
    rows = db.execute(
        f"SELECT id, weight FROM crawl_jobs WHERE id IN ({placeholders})",
        job_ids,
    ).fetchall()
    return {r["id"]: max(1, r["weight"] or 1) for r in rows}


def _outstanding_waiting(db):
    """等池是跨日积压：未入账的条目持续排队，直到某一天点继续被重算。"""
    return db.execute(
        """
        SELECT w.*, j.weight
        FROM pool_waiting w
        JOIN crawl_jobs j ON j.id = w.job_id
        ORDER BY w.seq
        """
    ).fetchall()


def plan_admission(db, day: str, current_job_id: int, current_codes: list[str]) -> dict:
    """按当日口径，把当日剩余池容量在所有竞争任务间切分。

    竞争者 = 等池栏里仍未入账的所有任务（含跨日积压）+ 本轮带合法编码的当前任务。
    equal  -> 每轮每任务 1 条，按等待先后（最早 seq）轮转；
    weight -> 每轮按任务权重给配额，同轮按等待先后。
    多个任务抢同一天的池时按轮转/权重切分，杜绝“谁先点谁全拿”。
    """
    pool = ensure_day(db, day)
    remaining = pool["capacity"] - pool["used_count"]
    if remaining <= 0:
        return {"admit_codes": [], "slots": {}, "remaining": 0, "admissions": {}}

    # 任务 -> 参与分配的编码队列（等池积压在前、本轮新到在后），保持等待先后
    queues: dict[int, list[str]] = {}
    seq_of: dict[int, int] = {}
    for w in _outstanding_waiting(db):
        queues.setdefault(w["job_id"], []).append(w["item_code"])
        seq_of.setdefault(w["job_id"], w["seq"])

    new_codes = [c for c in dict.fromkeys(current_codes) if c]
    if new_codes:
        existing = {
            r["item_code"]
            for r in db.execute(
                "SELECT item_code FROM pool_waiting WHERE job_id = ?",
                (current_job_id,),
            ).fetchall()
        }
        fresh = [c for c in new_codes if c not in existing]
        if fresh:
            queues.setdefault(current_job_id, [])
            queues[current_job_id].extend(fresh)
            seq_of.setdefault(current_job_id, 10**12)

    if not queues:
        return {"admit_codes": [], "slots": {}, "remaining": remaining, "admissions": {}}

    weights = _job_weights(db, list(queues.keys()))
    policy = pool["allocation_policy"]
    # 轮转顺序：最早进入等池者优先；本轮新到者排在已有等池者之后
    ordered_jobs = sorted(queues.keys(), key=lambda jid: (seq_of.get(jid, 10**12), jid))

    slots = {jid: 0 for jid in ordered_jobs}
    budget = remaining
    while budget > 0:
        progressed = False
        for jid in ordered_jobs:
            if budget <= 0:
                break
            queue_left = len(queues[jid]) - slots[jid]
            if queue_left <= 0:
                continue
            take = 1 if policy == POLICY_EQUAL else weights.get(jid, 1)
            take = min(take, queue_left, budget)
            slots[jid] += take
            budget -= take
            progressed = True
        if not progressed:
            break

    admit_codes = (
        queues[current_job_id][: slots.get(current_job_id, 0)]
        if current_job_id in queues
        else []
    )
    # 全局切分结果：任务 -> 本轮可入账的编码（按等待先后截取）
    admissions = {
        jid: queues[jid][: slots.get(jid, 0)] for jid in ordered_jobs if slots.get(jid, 0)
    }
    return {
        "admit_codes": admit_codes,
        "slots": slots,
        "remaining": remaining,
        "admissions": admissions,
    }


# ---------------------------------------------------------------- 入账 / 候补


def _next_seq(db) -> int:
    row = db.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM pool_waiting").fetchone()
    return row["n"]


def _dump_payload(payload) -> str:
    return json.dumps(payload or {}, ensure_ascii=False)


def admit_candidates(
    db,
    job_id: int,
    candidates: list[dict],
    record_builder,
    day: str | None = None,
    claimed_policy: str | None = None,
) -> dict:
    """任务提交一批候选项（runner 采集结果入口）。

    candidates: [{"item_code": sku, "title": ..., "payload": ...}, ...]
    record_builder(code, title, payload) -> (item_key, title, payload_json)
    """
    day = day or current_date()
    pool = ensure_day(db, day)

    # 后台拦截：继续入账时若携带的切法与当日口径不一致，拒绝并写「切法不对」
    if claimed_policy is not None and claimed_policy != pool["allocation_policy"]:
        _reject_and_persist(
            db,
            day,
            EVENT_WRONG_POLICY,
            job_id,
            f"以「{POLICY_NAMES.get(claimed_policy, claimed_policy)}」继续入账，"
            f"当日口径为「{POLICY_NAMES[pool['allocation_policy']]}」，已拦截",
            f"切法不对：当日口径是「{POLICY_NAMES[pool['allocation_policy']]}」，"
            "禁止改用别的切法继续入账",
        )

    # 空编码：既不占池也不进等池
    valid = []
    empty_code = 0
    for c in candidates:
        code = (c.get("item_code") or "").strip()
        if not code:
            empty_code += 1
            continue
        valid.append({**c, "item_code": code})

    # 已落库的去重（同任务同编码唯一）
    existing_codes = {
        r["item_code"]
        for r in db.execute(
            "SELECT item_code FROM crawl_records WHERE job_id = ? AND item_code IS NOT NULL",
            (job_id,),
        ).fetchall()
    }
    valid = [c for c in valid if c["item_code"] not in existing_codes]

    lock = current_lock(db)
    if lock is not None and lock["job_id"] != job_id:
        inserted_waiting = _push_waiting(db, day, job_id, _materialize(valid, record_builder))
        _log_event(
            db,
            day,
            EVENT_CONTENTION,
            job_id,
            f"任务 {job_id} 在任务 {lock['job_id']} 占池期间提交，"
            f"{inserted_waiting} 条合法编码进入等池",
        )
        return {
            "admitted": 0,
            "waiting": inserted_waiting,
            "empty_code": empty_code,
            "contended": True,
            "reason": EVENT_CONTENTION,
        }

    owned_lock = lock is not None and lock["job_id"] == job_id
    if not owned_lock and not acquire_pool_lock(db, job_id, day):
        inserted_waiting = _push_waiting(db, day, job_id, _materialize(valid, record_builder))
        _log_event(
            db,
            day,
            EVENT_CONTENTION,
            job_id,
            f"任务 {job_id} 抢池失败，{inserted_waiting} 条进入等池",
        )
        return {
            "admitted": 0,
            "waiting": inserted_waiting,
            "empty_code": empty_code,
            "contended": True,
            "reason": EVENT_CONTENTION,
        }

    try:
        codes = [c["item_code"] for c in valid]
        plan = plan_admission(db, day, job_id, codes)
        admit_set = set(plan["admit_codes"])
        admitted = 0
        overflow = []
        for c in valid:
            item_key, title, payload_json = record_builder(
                c["item_code"], c.get("title", ""), c.get("payload")
            )
            if c["item_code"] in admit_set:
                db.execute(
                    """
                    INSERT INTO crawl_records
                        (job_id, item_key, title, payload_json, item_code, pool_day, admitted_at)
                    VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                    """,
                    (job_id, item_key, title, payload_json, c["item_code"], day),
                )
                admitted += 1
            else:
                overflow.append(
                    {
                        "item_code": c["item_code"],
                        "item_key": item_key,
                        "title": title,
                        "payload_json": payload_json,
                    }
                )
        db.execute(
            "UPDATE daily_pool SET used_count = used_count + ? WHERE pool_day = ?",
            (admitted, day),
        )
        waiting_added = _push_waiting(db, day, job_id, overflow)
        return {
            "admitted": admitted,
            "waiting": waiting_added,
            "empty_code": empty_code,
            "contended": False,
            "reason": "",
        }
    finally:
        if not owned_lock:
            release_pool_lock(db, job_id)


def _materialize(candidates: list[dict], record_builder) -> list[dict]:
    out = []
    for c in candidates:
        item_key, title, payload_json = record_builder(
            c["item_code"], c.get("title", ""), c.get("payload")
        )
        out.append(
            {
                "item_code": c["item_code"],
                "item_key": item_key,
                "title": title,
                "payload_json": payload_json,
            }
        )
    return out


def _push_waiting(db, entered_day: str, job_id: int, items: list[dict]) -> int:
    existing = {
        r["item_code"]
        for r in db.execute(
            "SELECT item_code FROM pool_waiting WHERE job_id = ?", (job_id,)
        ).fetchall()
    }
    n = 0
    for c in items:
        if c["item_code"] in existing:
            continue
        item_key = c.get("item_key") or f"waiting::{entered_day}::{c['item_code']}"
        payload_json = c.get("payload_json") or _dump_payload(c.get("payload"))
        db.execute(
            """
            INSERT INTO pool_waiting
                (pool_day, job_id, item_code, item_key, title, payload_json, seq)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entered_day,
                job_id,
                c["item_code"],
                item_key,
                c.get("title", ""),
                payload_json,
                _next_seq(db),
            ),
        )
        existing.add(c["item_code"])
        n += 1
    return n


# ---------------------------------------------------------------- 继续（跨日重算）


def continue_waiting(
    db, job_id: int, claimed_policy: str | None = None, day: str | None = None
) -> dict:
    """把该任务在等池栏的合法编码按“今天”的池容量与口径重新参与分配。

    - 跑着的任务不能再点继续。
    - 跨日后昨天的等池不会自动入账，必须显式点继续。
    - 两个任务同时点继续：BEGIN IMMEDIATE 串行化 + 占池锁，只允许一笔入账落地，
      另一笔以池争用结束，禁止两边都写进账册造成超池。
    """
    day = day or current_date()
    job = db.execute("SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        raise ValueError("任务不存在")
    if job["status"] == "running":
        raise ValueError("任务正在运行，不能再点继续")

    pool = ensure_day(db, day)
    if claimed_policy is not None and claimed_policy != pool["allocation_policy"]:
        _reject_and_persist(
            db,
            day,
            EVENT_WRONG_POLICY,
            job_id,
            f"继续时声明「{POLICY_NAMES.get(claimed_policy, claimed_policy)}」，"
            f"当日口径为「{POLICY_NAMES[pool['allocation_policy']]}」，已拦截",
            f"切法不对：当日口径是「{POLICY_NAMES[pool['allocation_policy']]}」，"
            "禁止改用别的切法继续入账",
        )

    lock = current_lock(db)
    if lock is not None and lock["job_id"] != job_id:
        _log_event(
            db,
            day,
            EVENT_CONTENTION,
            job_id,
            f"继续时任务 {lock['job_id']} 正在占池，本任务以池争用结束，未入账",
        )
        kept = db.execute(
            "SELECT COUNT(*) AS c FROM pool_waiting WHERE job_id = ?", (job_id,)
        ).fetchone()["c"]
        return {
            "admitted": 0,
            "waiting_kept": kept,
            "contended": True,
            "reason": EVENT_CONTENTION,
        }

    owned_lock = lock is not None and lock["job_id"] == job_id
    if not owned_lock and not acquire_pool_lock(db, job_id, day):
        _log_event(
            db,
            day,
            EVENT_CONTENTION,
            job_id,
            "继续抢池失败，以池争用结束，未入账",
        )
        kept = db.execute(
            "SELECT COUNT(*) AS c FROM pool_waiting WHERE job_id = ?", (job_id,)
        ).fetchone()["c"]
        return {
            "admitted": 0,
            "waiting_kept": kept,
            "contended": True,
            "reason": EVENT_CONTENTION,
        }

    try:
        rows = db.execute(
            "SELECT * FROM pool_waiting WHERE job_id = ? ORDER BY seq", (job_id,)
        ).fetchall()
        if not rows:
            return {"admitted": 0, "waiting_kept": 0, "contended": False, "reason": ""}

        plan = plan_admission(db, day, job_id, [r["item_code"] for r in rows])
        # 规划器按当日口径把名额切给所有在等池的任务；当前任务只是触发者，
        # 一次继续要把所有竞争任务的合法编码一并入账，避免“先点者独占”。
        admissions = plan["admissions"]
        admitted = 0
        admitted_by_job: dict[int, int] = {}
        for other_job_id, codes in admissions.items():
            code_set = set(codes)
            waiting_rows = db.execute(
                "SELECT * FROM pool_waiting WHERE job_id = ? ORDER BY seq",
                (other_job_id,),
            ).fetchall()
            for r in waiting_rows:
                if r["item_code"] not in code_set:
                    continue
                db.execute(
                    """
                    INSERT INTO crawl_records
                        (job_id, item_key, title, payload_json, item_code, pool_day, admitted_at)
                    VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                    """,
                    (
                        other_job_id,
                        r["item_key"] or f"waiting::{day}::{r['item_code']}",
                        r["title"],
                        r["payload_json"],
                        r["item_code"],
                        day,
                    ),
                )
                db.execute("DELETE FROM pool_waiting WHERE id = ?", (r["id"],))
                admitted += 1
                admitted_by_job[other_job_id] = admitted_by_job.get(other_job_id, 0) + 1
        db.execute(
            "UPDATE daily_pool SET used_count = used_count + ? WHERE pool_day = ?",
            (admitted, day),
        )
        waiting_kept = db.execute(
            "SELECT COUNT(*) AS c FROM pool_waiting WHERE job_id = ?", (job_id,)
        ).fetchone()["c"]
        if admitted_by_job.get(job_id, 0) == 0 and waiting_kept:
            _log_event(
                db,
                day,
                EVENT_CONTENTION,
                job_id,
                "继续时池容量已被其它任务分尽，本笔以池争用结束，等池保留",
            )
            return {
                "admitted": 0,
                "waiting_kept": waiting_kept,
                "contended": True,
                "reason": EVENT_CONTENTION,
            }
        return {
            "admitted": admitted_by_job.get(job_id, 0),
            "total_admitted": admitted,
            "waiting_kept": waiting_kept,
            "contended": False,
            "reason": "",
        }
    finally:
        if not owned_lock:
            release_pool_lock(db, job_id)


# ---------------------------------------------------------------- 页面聚合


def pool_overview(db, day: str | None = None) -> dict:
    day = day or current_date()
    settings = get_settings(db)
    pool = db.execute(
        "SELECT * FROM daily_pool WHERE pool_day = ?", (day,)
    ).fetchone()
    # 等池是跨日积压，条数统计不再按池日过滤
    waiting_count = db.execute(
        "SELECT COUNT(*) AS c FROM pool_waiting"
    ).fetchone()["c"]
    if pool is not None:
        return {
            "day": day,
            "capacity": pool["capacity"],
            "used": pool["used_count"],
            "waiting": waiting_count,
            "policy": pool["allocation_policy"],
            "policy_name": POLICY_NAMES[pool["allocation_policy"]],
            "locked": True,
        }
    return {
        "day": day,
        "capacity": settings["pool_capacity"],
        "used": 0,
        "waiting": waiting_count,
        "policy": settings["allocation_policy"],
        "policy_name": POLICY_NAMES[settings["allocation_policy"]],
        "locked": False,
    }


def job_pool_stats(db, job_id: int, day: str | None = None) -> dict:
    day = day or current_date()
    admitted = db.execute(
        "SELECT COUNT(*) AS c FROM crawl_records WHERE job_id = ? AND pool_day = ?",
        (job_id, day),
    ).fetchone()["c"]
    waiting_n = db.execute(
        "SELECT COUNT(*) AS c FROM pool_waiting WHERE job_id = ?", (job_id,)
    ).fetchone()["c"]
    return {"admitted_today": admitted, "waiting_today": waiting_n}


def list_waiting(db, job_id: int | None = None) -> list[sqlite3.Row]:
    """等池栏（跨日积压），按全局排队顺序返回；标题固定为「等池」。"""
    sql = """
        SELECT w.*, j.name AS job_name, j.weight AS job_weight
        FROM pool_waiting w
        JOIN crawl_jobs j ON j.id = w.job_id
    """
    params: list = []
    if job_id is not None:
        sql += " WHERE w.job_id = ?"
        params.append(job_id)
    sql += " ORDER BY w.seq"
    return db.execute(sql, params).fetchall()


def list_events(db, day: str | None = None, job_id: int | None = None) -> list[sqlite3.Row]:
    """池争用 / 切法拦截事件。详情页按任务查全部历史；不指定任务时按当天查。"""
    sql = "SELECT * FROM pool_events WHERE 1=1"
    params: list = []
    if job_id is not None:
        sql += " AND (job_id = ? OR job_id IS NULL)"
        params.append(job_id)
    elif day is not None:
        sql += " AND event_day = ?"
        params.append(day)
    sql += " ORDER BY id DESC"
    return db.execute(sql, params).fetchall()
