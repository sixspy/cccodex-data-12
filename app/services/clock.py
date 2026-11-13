"""后台日期时钟。

生产环境取系统日期；测试可通过 app.config["POOL_DATE"] 钉死日期，
保证池子按后台日期结算的逻辑可复现。
"""

from datetime import date

from flask import current_app, has_app_context


def current_date() -> str:
    if has_app_context():
        pinned = current_app.config.get("POOL_DATE")
        if pinned:
            return str(pinned)
    return date.today().isoformat()
