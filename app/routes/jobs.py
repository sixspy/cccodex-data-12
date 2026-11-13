from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.db import db_transaction, get_db
from app.routes.auth import login_required
from app.services.mock_catalog import CATALOGS
from app.services.pool import (
    POLICY_NAMES,
    continue_waiting,
    job_pool_stats,
    list_events,
    list_waiting,
    pool_overview,
)
from app.services.runner import run_job

bp = Blueprint("jobs", __name__)


@bp.route("/")
@login_required
def list_jobs():
    db = get_db()
    jobs = db.execute(
        "SELECT * FROM crawl_jobs ORDER BY id DESC"
    ).fetchall()
    counts = {
        row["job_id"]: row["c"]
        for row in db.execute(
            "SELECT job_id, COUNT(*) AS c FROM crawl_records GROUP BY job_id"
        )
    }
    per_job = {j["id"]: job_pool_stats(db, j["id"]) for j in jobs}
    waiting_rows = list_waiting(db)
    return render_template(
        "jobs_list.html",
        jobs=jobs,
        counts=counts,
        per_job=per_job,
        seeds=sorted(CATALOGS.keys()),
        overview=pool_overview(db),
        waiting_rows=waiting_rows,
        policy_names=POLICY_NAMES,
    )


@bp.post("/jobs")
@login_required
def create_job():
    name = request.form.get("name", "").strip()
    seed = request.form.get("seed_path", "").strip()
    weight_raw = request.form.get("weight", "1").strip() or "1"
    if not name or not seed:
        flash("请填写任务名与种子路径", "error")
        return redirect(url_for("jobs.list_jobs"))
    if seed not in CATALOGS:
        flash("种子路径必须是已注册的 mock 目录", "error")
        return redirect(url_for("jobs.list_jobs"))
    try:
        weight = int(weight_raw)
    except ValueError:
        weight = 1
    if weight < 1:
        weight = 1
    db = get_db()
    db.execute(
        "INSERT INTO crawl_jobs (name, seed_path, status, weight) VALUES (?, ?, 'draft', ?)",
        (name, seed, weight),
    )
    db.commit()
    flash("任务已创建", "ok")
    return redirect(url_for("jobs.list_jobs"))


@bp.post("/jobs/<int:job_id>/run")
@login_required
def run_job_view(job_id: int):
    try:
        summary = run_job(job_id)
        if summary.get("contended"):
            flash(
                f"池争用：入账 {summary['inserted']} 条，{summary['waiting']} 条进入等池",
                "error",
            )
        else:
            flash(
                f"运行完成：入账 {summary['inserted']} 条，等池 {summary['waiting']} 条，"
                f"空编码 {summary['empty_code']} 条，去重跳过 {summary['skipped_dup']} 条",
                "ok",
            )
    except ValueError as e:
        flash(str(e), "error")
    except Exception as e:
        flash(f"运行失败: {e}", "error")
    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.post("/jobs/<int:job_id>/continue")
@login_required
def continue_job_view(job_id: int):
    # 页面携带的切法必须与当日口径一致，否则后台拦截并写「切法不对」
    claimed_policy = request.form.get("claimed_policy") or None
    with db_transaction() as tx:
        try:
            result = continue_waiting(tx, job_id, claimed_policy=claimed_policy)
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("jobs.detail", job_id=job_id))
    if result["contended"]:
        flash(
            f"池争用：本笔未能入账，{result['waiting_kept']} 条仍在等池",
            "error",
        )
    else:
        flash(
            f"继续完成：入账 {result['admitted']} 条，等池剩余 {result['waiting_kept']} 条",
            "ok",
        )
    return redirect(url_for("jobs.detail", job_id=job_id))


@bp.get("/jobs/<int:job_id>")
@login_required
def detail(job_id: int):
    db = get_db()
    job = db.execute("SELECT * FROM crawl_jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        flash("任务不存在", "error")
        return redirect(url_for("jobs.list_jobs"))
    records = db.execute(
        "SELECT * FROM crawl_records WHERE job_id = ? ORDER BY id",
        (job_id,),
    ).fetchall()
    waiting = list_waiting(db, job_id=job_id)
    events = list_events(db, job_id=job_id)
    overview = pool_overview(db)
    return render_template(
        "job_detail.html",
        job=job,
        records=records,
        waiting=waiting,
        events=events,
        overview=overview,
        stats=job_pool_stats(db, job_id),
        policy_names=POLICY_NAMES,
    )
