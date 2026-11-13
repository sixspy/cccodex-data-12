from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.db import db_transaction, get_db
from app.routes.auth import login_required
from app.services.clock import current_date
from app.services.pool import (
    DEFAULT_CAPACITY,
    POLICY_NAMES,
    VALID_POLICIES,
    get_settings,
    pool_overview,
    update_settings,
)

bp = Blueprint("settings", __name__, url_prefix="/settings")


@bp.get("/")
@login_required
def settings_page():
    db = get_db()
    return render_template(
        "settings.html",
        settings=get_settings(db),
        overview=pool_overview(db),
        policies=POLICY_NAMES,
        default_capacity=DEFAULT_CAPACITY,
        today=current_date(),
    )


@bp.post("/pool")
@login_required
def save_pool_settings():
    capacity_raw = request.form.get("pool_capacity", "")
    policy = request.form.get("allocation_policy", "")
    if policy not in VALID_POLICIES:
        flash("切法不对：请选择合法的分配口径", "error")
        return redirect(url_for("settings.settings_page"))
    with db_transaction() as tx:
        try:
            update_settings(tx, capacity_raw, policy)
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("settings.settings_page"))
    flash("设置已保存；口径当天锁定，池容量在次日新池生效", "ok")
    return redirect(url_for("settings.settings_page"))
