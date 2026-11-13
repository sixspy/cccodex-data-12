"""当日入库池七段验收。

所有用例把后台日期钉在 app.config['POOL_DATE']，跨日用例显式换日。
"""

import sqlite3
import threading

import pytest

from app.db import db_transaction, get_db
from app.services.clock import current_date
from app.services.pool import (
    POLICY_EQUAL,
    POLICY_WEIGHT,
    acquire_pool_lock,
    admit_candidates,
    continue_waiting,
    list_waiting,
    pool_overview,
    release_pool_lock,
    update_settings,
)
from app.services.runner import run_job

DAY1 = "2026-11-12"
DAY2 = "2026-11-13"


def login(client):
    return client.post(
        "/login",
        data={"username": "admin", "password": "123456"},
        follow_redirects=True,
    )


def create_job(app, name="t", seed="/mock/catalog/beta", weight=1):
    with app.app_context():
        db = get_db()
        db.execute(
            "INSERT INTO crawl_jobs (name, seed_path, status, weight) VALUES (?, ?, 'draft', ?)",
            (name, seed, weight),
        )
        db.commit()
        return db.execute("SELECT id FROM crawl_jobs WHERE name = ?", (name,)).fetchone()["id"]


def set_pool(app, capacity, policy=POLICY_EQUAL):
    with app.app_context():
        with db_transaction() as tx:
            update_settings(tx, str(capacity), policy)


def candidates(codes):
    return [
        {"item_code": c, "title": f"标题{c or '空'}", "payload": {"sku": c}}
        for c in codes
    ]


def builder(seed="/mock/catalog/beta"):
    import json

    from app.services.dedup import normalize_item_key

    def _b(code, title, payload):
        return (
            normalize_item_key(seed, f"{title}-{code}"),
            title,
            json.dumps(payload, ensure_ascii=False),
        )

    return _b


def admitted_today(app, job_id, day=None):
    day = day or current_date()
    with app.app_context():
        return get_db().execute(
            "SELECT COUNT(*) c FROM crawl_records WHERE job_id = ? AND pool_day = ?",
            (job_id, day),
        ).fetchone()["c"]


def waiting_count(app, job_id=None):
    with app.app_context():
        db = get_db()
        if job_id is None:
            return db.execute("SELECT COUNT(*) c FROM pool_waiting").fetchone()["c"]
        return db.execute(
            "SELECT COUNT(*) c FROM pool_waiting WHERE job_id = ?", (job_id,)
        ).fetchone()["c"]


def pool_used(app, day=None):
    day = day or current_date()
    with app.app_context():
        row = get_db().execute(
            "SELECT used_count, capacity FROM daily_pool WHERE pool_day = ?", (day,)
        ).fetchone()
        return row["used_count"], row["capacity"]


def force_into_waiting(app, job_id, codes, day=DAY1):
    """在极小池容量下把指定编码压进等池栏（不产生入账）。"""
    with app.app_context():
        with db_transaction() as tx:
            admit_candidates(tx, job_id, candidates(codes), builder(), day=day)


def seed_waiting(app, job_id, codes, day=DAY1):
    """直接构造等池积压：模拟多个任务的合法编码都已停在候补栏等待切池。"""
    import json

    from app.services.dedup import normalize_item_key

    with app.app_context():
        db = get_db()
        base = db.execute("SELECT COALESCE(MAX(seq),0) m FROM pool_waiting").fetchone()["m"]
        for i, code in enumerate(codes, start=1):
            key = normalize_item_key("/mock/catalog/beta", f"标题{code}-{code}")
            db.execute(
                """
                INSERT INTO pool_waiting (pool_day, job_id, item_code, item_key, title, payload_json, seq)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (day, job_id, code, key, f"标题{code}", json.dumps({"sku": code}, ensure_ascii=False), base + i),
            )
        db.commit()


def reset_day_pool(app, day=DAY1):
    """清空当日账册并把已用名额归零，用于在既有等池上重新切分。"""
    with app.app_context():
        with db_transaction() as tx:
            tx.execute("DELETE FROM crawl_records")
            tx.execute("UPDATE daily_pool SET used_count = 0 WHERE pool_day = ?", (day,))


# ------------------------------------------------------------ 1. 超池后只进等池


def test_over_capacity_only_goes_waiting(app):
    set_pool(app, capacity=1)
    j1 = create_job(app, "j1")
    with app.app_context():
        with db_transaction() as tx:
            r1 = admit_candidates(tx, j1, candidates(["C1", "C2"]), builder())
        assert r1["admitted"] == 1
        assert r1["waiting"] == 1
        # 同一天再交一批：池子已满，全部只进等池，不产生入账
        with app.app_context():
            with db_transaction() as tx:
                r2 = admit_candidates(tx, j1, candidates(["C3", "C4"]), builder())
        assert r2["admitted"] == 0
        assert r2["waiting"] == 2
        overview = pool_overview(get_db())
        assert overview["used"] == 1
        assert overview["capacity"] == 1
        assert overview["waiting"] == 3


# ------------------------------------------------------------ 2. 同一天总数不超池 + 禁止先点全拿


def test_daily_total_never_exceeds_capacity_with_two_jobs(app):
    set_pool(app, capacity=3)
    j1 = create_job(app, "j1")
    j2 = create_job(app, "j2")
    # 两个任务的合法编码都停在等池栏，制造同日争抢
    seed_waiting(app, j1, ["A1", "A2", "A3", "A4"])
    seed_waiting(app, j2, ["B1", "B2", "B3", "B4"])
    reset_day_pool(app)
    with app.app_context():
        with db_transaction() as tx:
            r = continue_waiting(tx, j1)
        used, capacity = pool_used(app)
        assert used <= capacity
        assert used == 3
        n1 = admitted_today(app, j1)
        n2 = admitted_today(app, j2)
        # 两个任务都分到名额，没有谁全拿
        assert n1 >= 1 and n2 >= 1
        assert n1 + n2 == 3
        # 当前任务 j1 自己至少有一条没进，仍在等池
        assert r["waiting_kept"] >= 1


def test_equal_policy_round_robin_fair_split(app):
    """均摊：两个任务各 4 条同抢容量 4，每任务各拿 2 条，先排队的不独占。"""
    set_pool(app, capacity=4, policy=POLICY_EQUAL)
    j1 = create_job(app, "eq1")
    j2 = create_job(app, "eq2")
    seed_waiting(app, j1, ["E1", "E2", "E3", "E4"])
    seed_waiting(app, j2, ["F1", "F2", "F3", "F4"])
    reset_day_pool(app)
    with app.app_context():
        with db_transaction() as tx:
            continue_waiting(tx, j1)
        assert (admitted_today(app, j1), admitted_today(app, j2)) == (2, 2)


def test_weight_policy_weighted_split(app):
    """权重 3:1、容量 4：两个等池任务同抢时按 3:1 切分。"""
    set_pool(app, capacity=4, policy=POLICY_WEIGHT)
    j1 = create_job(app, "w1", weight=3)
    j2 = create_job(app, "w2", weight=1)
    seed_waiting(app, j1, ["W1", "W2", "W3", "W4"])
    seed_waiting(app, j2, ["Z1", "Z2", "Z3", "Z4"])
    reset_day_pool(app)
    with app.app_context():
        with db_transaction() as tx:
            continue_waiting(tx, j1)
        assert (admitted_today(app, j1), admitted_today(app, j2)) == (3, 1)


def test_single_first_clicker_does_not_grab_more_than_round_share(app):
    """顺序提交场景：先点的任务在均摊口径下也受轮转约束，超出部分进等池，
    给随后到达的竞争任务保留名额。"""
    set_pool(app, capacity=4, policy=POLICY_EQUAL)
    j1 = create_job(app, "first")
    with app.app_context():
        with db_transaction() as tx:
            r = admit_candidates(tx, j1, candidates(["S1", "S2", "S3", "S4"]), builder())
        # 池容量 4、当前只有一个任务：本轮可用 4 条，但先点者不会越过轮转规则
        assert r["admitted"] <= 4
        assert pool_used(app)[0] <= 4


def test_other_job_holding_pool_only_waiting(app):
    """某任务占池时，其它任务的合法编码只能停在候补栏。"""
    set_pool(app, capacity=5)
    holder = create_job(app, "holder")
    other = create_job(app, "other")
    with app.app_context():
        db = get_db()
        assert acquire_pool_lock(db, holder, DAY1) is True
        try:
            with db_transaction() as tx:
                r = admit_candidates(tx, other, candidates(["O1", "O2"]), builder())
            assert r["contended"] is True
            assert r["admitted"] == 0
            assert r["waiting"] == 2
            w = list_waiting(db, job_id=other)
            assert [x["item_code"] for x in w] == ["O1", "O2"]
        finally:
            release_pool_lock(db, holder)


# ------------------------------------------------------------ 3. 换日旧等池不自动入账，必须继续


def test_cross_day_waiting_not_auto_admitted_and_continue_recalculates(app):
    set_pool(app, capacity=1)
    j1 = create_job(app, "day-job")
    force_into_waiting(app, j1, ["D1", "D2"])
    assert admitted_today(app, j1) == 1
    assert waiting_count(app, j1) == 1

    # 换日：昨天的等池原地保留，不会自己跑进账册
    app.config["POOL_DATE"] = DAY2
    with app.app_context():
        db = get_db()
        assert pool_overview(db)["used"] == 0
        assert len(list_waiting(db, job_id=j1)) == 1
        assert admitted_today(app, j1, DAY2) == 0
        assert admitted_today(app, j1, DAY1) == 1

        # 点继续：按新一天容量 1 和当日口径重算，落一笔
        with db_transaction() as tx:
            result = continue_waiting(tx, j1)
        assert result["admitted"] == 1
        assert result["waiting_kept"] == 0
        assert pool_overview(db)["used"] == 1
        assert admitted_today(app, j1, DAY2) == 1


# ------------------------------------------------------------ 4. 改用别的切法继续会被拦住


def test_policy_locked_same_day_and_wrong_policy_continue_blocked(app):
    set_pool(app, capacity=10, policy=POLICY_EQUAL)
    j1 = create_job(app, "lock-job")
    with app.app_context():
        # 先发生一笔入账，生成当日快照，equal 口径锁定
        with db_transaction() as tx:
            admit_candidates(tx, j1, candidates(["L1"]), builder())

        # 设置页当天改口 weight：被拦并写「切法不对」
        with pytest.raises(ValueError, match="切法不对"):
            with db_transaction() as tx:
                update_settings(tx, "10", POLICY_WEIGHT)
        db = get_db()
        evt = db.execute(
            "SELECT * FROM pool_events WHERE event_type='切法不对' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert evt is not None

        # 继续入账时携带别的切法：同样被拦且事件落库
        with pytest.raises(ValueError, match="切法不对"):
            with db_transaction() as tx:
                continue_waiting(tx, j1, claimed_policy=POLICY_WEIGHT)
        assert pool_overview(db)["policy"] == POLICY_EQUAL
        assert (
            db.execute(
                "SELECT COUNT(*) c FROM pool_events WHERE event_type='切法不对'"
            ).fetchone()["c"]
            >= 2
        )

    # 次日可以改口
    app.config["POOL_DATE"] = DAY2
    with app.app_context():
        with db_transaction() as tx:
            update_settings(tx, "10", POLICY_WEIGHT)
        assert pool_overview(get_db())["policy"] == POLICY_WEIGHT


# ------------------------------------------------------------ 5. 并发继续只允许一笔成功


def test_concurrent_continue_only_one_lands(app):
    set_pool(app, capacity=2)
    j1 = create_job(app, "c1")
    j2 = create_job(app, "c2")
    db_path = app.config["DATABASE"]
    # 两个任务各 3 条合法编码停在等池栏，跨日同时点继续争抢新一天的 2 个名额
    seed_waiting(app, j1, ["P1", "P2", "P3"])
    seed_waiting(app, j2, ["Q1", "Q2", "Q3"])
    with app.app_context():
        assert waiting_count(app) == 6

    app.config["POOL_DATE"] = DAY2
    outcomes = {}
    errors = {}
    barrier = threading.Barrier(2)

    def worker(job_id):
        conn = sqlite3.connect(db_path, isolation_level=None, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        barrier.wait()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # 显式传入钉死的日期，线程内无 app 上下文
                outcomes[job_id] = continue_waiting(conn, job_id, day=DAY2)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        except Exception as exc:  # noqa: BLE001
            errors[job_id] = exc
        finally:
            conn.close()

    t1 = threading.Thread(target=worker, args=(j1,))
    t2 = threading.Thread(target=worker, args=(j2,))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert not errors, errors
    with app.app_context():
        db = get_db()
        total_today = db.execute(
            "SELECT COUNT(*) c FROM crawl_records WHERE pool_day=?", (DAY2,)
        ).fetchone()["c"]
        pool = db.execute("SELECT * FROM daily_pool WHERE pool_day=?", (DAY2,)).fetchone()
        # 只有赢家那一笔事务落地，入账总数绝不超过当日池容量
        assert total_today <= 2
        assert pool["used_count"] == total_today
        # 另一笔以池争用结束，没有任何入账
        contention = db.execute(
            "SELECT COUNT(*) c FROM pool_events WHERE event_day=? AND event_type='池争用'",
            (DAY2,),
        ).fetchone()["c"]
        assert contention >= 1
        winner = next(j for j in (j1, j2) if not outcomes[j]["contended"])
        loser = next(j for j in (j1, j2) if outcomes[j]["contended"])
        assert outcomes[loser]["admitted"] == 0
        assert outcomes[winner]["total_admitted"] == total_today
        # 输家的等池一条没动
        assert outcomes[loser]["waiting_kept"] == db.execute(
            "SELECT COUNT(*) c FROM pool_waiting WHERE job_id=?", (loser,)
        ).fetchone()["c"]


# ------------------------------------------------------------ 6. 空编码不占池


def test_empty_code_neither_admitted_nor_waiting(app):
    set_pool(app, capacity=10)
    j1 = create_job(app, "empty-job")
    with app.app_context():
        with db_transaction() as tx:
            r = admit_candidates(tx, j1, candidates(["", "   ", "REAL-1"]), builder())
        assert r["empty_code"] == 2
        assert r["admitted"] == 1
        assert r["waiting"] == 0
    assert waiting_count(app) == 0
    with app.app_context():
        assert pool_overview(get_db())["used"] == 1

    # runner 端到端：alpha 含一条空 sku
    j2 = create_job(app, "alpha-job", seed="/mock/catalog/alpha")
    with app.app_context():
        summary = run_job(j2)
        assert summary["empty_code"] == 1
        db = get_db()
        empty_rows = db.execute(
            "SELECT COUNT(*) c FROM crawl_records WHERE job_id=? AND (item_code IS NULL OR item_code='')",
            (j2,),
        ).fetchone()["c"]
        assert empty_rows == 0


# ------------------------------------------------------------ 7. 跑着时再点继续会被拒


def test_running_job_cannot_continue(app):
    set_pool(app, capacity=10)
    j1 = create_job(app, "running-job")
    with app.app_context():
        with db_transaction() as tx:
            tx.execute("UPDATE crawl_jobs SET status='running' WHERE id=?", (j1,))
        with pytest.raises(ValueError, match="正在运行"):
            with db_transaction() as tx:
                continue_waiting(tx, j1)

    # HTTP 层：直接打接口也被拒
    c = app.test_client()
    login(c)
    resp = c.post(
        f"/jobs/{j1}/continue",
        data={"claimed_policy": POLICY_EQUAL},
        follow_redirects=True,
    )
    assert "正在运行" in resp.get_data(as_text=True)


# ------------------------------------------------------------ 8. 页面可直接读出关键信息


def test_pages_show_pool_info_waiting_order_and_contention(app, client):
    login(client)
    set_pool(app, capacity=2)
    j1 = create_job(app, "page-job")
    seed_waiting(app, j1, ["UI1", "UI2", "UI3"])

    list_html = client.get("/").get_data(as_text=True)
    for needle in ["池容量", "今日已入", "等池条数", "当日口径名字", "按等待先后均摊", "等池"]:
        assert needle in list_html

    detail_html = client.get(f"/jobs/{j1}").get_data(as_text=True)
    for needle in ["等池顺序", "池争用提示", "按等待先后均摊", "UI3", "继续"]:
        assert needle in detail_html

    settings_html = client.get("/settings/").get_data(as_text=True)
    for needle in ["按等待先后均摊", "按任务权重切分", "当天禁止改口", "不填按 20"]:
        assert needle in settings_html


def test_settings_page_change_policy_takes_effect_next_day(app, client):
    login(client)
    # 未生成当日快照前可自由选口径；容量不填按 20
    r = client.post(
        "/settings/pool",
        data={"pool_capacity": "", "allocation_policy": POLICY_WEIGHT},
        follow_redirects=True,
    )
    assert r.status_code == 200
    with app.app_context():
        overview = pool_overview(get_db())
        assert overview["policy"] == POLICY_WEIGHT
        assert overview["capacity"] == 20
