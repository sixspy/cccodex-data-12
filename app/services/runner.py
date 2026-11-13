import json

from flask import current_app

from app.db import db_transaction, get_db
from app.services.dedup import normalize_item_key
from app.services.mock_catalog import CATALOGS, parse_catalog_html, render_catalog_page
from app.services.pool import admit_candidates


def run_job(job_id: int) -> dict:
    db = get_db()
    job = db.execute(
        "SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if job is None:
        raise ValueError("job not found")
    if job["status"] == "running":
        raise ValueError("job already running")
    if job["status"] == "done":
        raise ValueError("job already done")

    seed = job["seed_path"]
    if seed not in CATALOGS:
        with db_transaction() as tx:
            tx.execute(
                "UPDATE crawl_jobs SET status = 'failed', finished_at = datetime('now') WHERE id = ?",
                (job_id,),
            )
        return {
            "status": "failed",
            "reason": "unknown seed",
            "inserted": 0,
            "skipped_dup": 0,
            "empty_code": 0,
            "waiting": 0,
        }

    html = render_catalog_page(seed)
    if isinstance(html, tuple):
        html = html[0]
    items = parse_catalog_html(html, seed)

    # 同任务内按规范化 item_key 去重（基线语义：重复标题跳过）；
    # 已落库的同键条目同样跳过。空编码行保留到候选里，由池子判定“不占池不进等池”。
    existing_keys = {
        r["item_key"]
        for r in db.execute(
            "SELECT item_key FROM crawl_records WHERE job_id = ?", (job_id,)
        ).fetchall()
    }
    candidates = []
    seen_keys = set()
    skipped_dup = 0
    for it in items:
        key = normalize_item_key(seed, it["title"])
        if key in seen_keys or key in existing_keys:
            skipped_dup += 1
            continue
        seen_keys.add(key)
        candidates.append(
            {
                "item_code": (it.get("sku") or "").strip(),
                "title": it["title"],
                "payload": it,
                "_key": key,
            }
        )

    key_by_code = {c["item_code"]: c["_key"] for c in candidates if c["item_code"]}

    def record_builder(code, title, payload):
        key = key_by_code.get(code) or normalize_item_key(seed, title)
        return key, title, json.dumps(payload, ensure_ascii=False)

    # 整个采集在一个立即事务内完成：占池、入账、候补、状态翻转原子可见
    try:
        with db_transaction() as tx:
            tx.execute(
                "UPDATE crawl_jobs SET status = 'running', finished_at = NULL WHERE id = ?",
                (job_id,),
            )
            pool_result = admit_candidates(
                tx,
                job_id,
                candidates,
                record_builder,
            )
            tx.execute(
                "UPDATE crawl_jobs SET status = 'done', finished_at = datetime('now') WHERE id = ?",
                (job_id,),
            )
    except Exception as exc:
        with db_transaction() as tx:
            tx.execute(
                "UPDATE crawl_jobs SET status = 'failed', finished_at = datetime('now') WHERE id = ?",
                (job_id,),
            )
        raise ValueError(f"运行失败: {exc}") from exc

    current_app.logger.info(
        "job %s finished admitted=%s waiting=%s empty_code=%s skipped_dup=%s",
        job_id,
        pool_result["admitted"],
        pool_result["waiting"],
        pool_result["empty_code"],
        skipped_dup,
    )
    return {
        "status": "done",
        "inserted": pool_result["admitted"],
        "skipped_dup": skipped_dup,
        "empty_code": pool_result["empty_code"],
        "waiting": pool_result["waiting"],
        "contended": pool_result["contended"],
        "total_source": len(items),
    }
