# cc-data-lab

CC_Codex 二期（26.11）**数据工程**主线基线：**采集任务控制台**（Flask 后端 + HTMX 前端页 + 站内 Mock 目录）。

## 领域

- **所属领域**：数据工程  
- **子方向**：数据采集、爬虫与 RPA  

## 技术栈

- Python 3.12+、Flask 3、SQLite  
- HTMX（CDN）+ Jinja 模板  
- pytest  

## 本地运行

```powershell
cd claude-workspace\cc-data-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

浏览器打开 http://127.0.0.1:5590 ，登录 **admin / 123456**。

流程：创建任务（可声明权重）→ 选种子 `/mock/catalog/alpha` 或 `beta` → 详情页「运行采集」→ 查看落库（alpha 含重复标题会演示去重跳过，含一条空编码演示“不占池不进等池”）。

## 当日入库池（跨任务共享）

全站共用**一条当日入库池**，池是跨任务共享资源，不是单个任务自己的计数器，按后台日期结算。

- **设置页**（`/settings/`）：配置池容量（不填按 20）与分配口径，口径写进当日生效口径后**当天禁止改口**。
  - `按等待先后均摊`：多个任务抢同一天的池时按等待先后轮询均摊；
  - `按任务权重切分`：按任务声明的权重做加权轮询，同轮仍按等待先后。
  - 两种切法都禁止“谁先点谁全拿”；改用别的切法继续入账会被后台拦截并写「切法不对」事件。
- **占池与等池**：某任务占用池子时，其它任务的合法编码只能停在候补栏（标题「等池」），按全局 `seq` 排队；编码为空的行既不占池也不进等池。
- **跨日继续**：昨天的等池不会自动入账，必须到详情页点「继续」，按新一天的池容量与当日口径重算；两个任务同时点继续时，`BEGIN IMMEDIATE` + 占池锁保证只允许一笔入账落地，另一笔以「池争用」结束，绝不超池。跑着的任务不能再点继续。
- **页面**：任务列表并排显示池容量、今日已入、等池条数、当日口径名字；详情页可点开等池顺序、查看池争用提示与落库账册。
- 测试把后台日期钉在 `app.config['POOL_DATE']`（见 `tests/test_pool.py`，七段验收）。

## 七段验收

| # | 验收点 | 对应用例 |
|---|--------|----------|
| 1 | 超池后只进等池 | `test_over_capacity_only_goes_waiting` |
| 2 | 同一天入账总数不超过池容量 | `test_daily_total_never_exceeds_capacity_with_two_jobs` / 均摊 / 权重 |
| 3 | 换日之后旧等池不会自己入账 | `test_cross_day_waiting_not_auto_admitted_and_continue_recalculates` |
| 4 | 改用别的切法继续会被拦住 | `test_policy_locked_same_day_and_wrong_policy_continue_blocked` |
| 5 | 并发继续时只有一笔成功 | `test_concurrent_continue_only_one_lands`（真实双线程 + SQLite 立即事务） |
| 6 | 空编码不占池 | `test_empty_code_neither_admitted_nor_waiting` |
| 7 | 跑着时再点继续会被拒 | `test_running_job_cannot_continue` |
| 附 | 页面可读出池争用、等池顺序、口径名字 | `test_pages_show_pool_info_waiting_order_and_contention` |

## Docker 一键启动（推荐用于快照前验收）

需已打开 **Docker Desktop**：

```powershell
cd claude-workspace\cc-data-lab
docker compose up -d --build
docker compose ps
```

- 页面：http://127.0.0.1:5590/login  
- Mock：http://127.0.0.1:5590/mock/catalog/alpha  
- 停止：`docker compose down`（保留库卷）· 清卷：`docker compose down -v`

`docker compose ps` 中 `STATUS` 含 **healthy** 表示服务可访问。线下表「环境可复现等级」可填 **已容器化，可一键起环境**。

## 前后端说明

| 层 | 说明 |
|----|------|
| 后端 | `app/` 路由、任务状态机、同步 runner、SQLite |
| 前端 | `templates/` + `static/`，浏览器可点操作 |
| Mock 源站 | `/mock/catalog/<id>` HTML，无需外网 |

## 初始快照

在首次 Claude 单轮跑题前 commit + push 本树（不含 `.venv/`、`instance/*.db`）。后续题目在同 repo 上叠加调度、RPA 步骤、增量水位等能力。

## 基线 vs 跑题目录

| 路径 | 角色 |
|------|------|
| `claude-workspace/cc-data-lab`（本目录） | **基线**：改功能、docker 验收 |
| `ccproject/cc-data-lab/data-XX` | **单题**：复制基线后挂容器、出快照 |

复制：`..\scripts\Copy-BaselineToQuestion.ps1 -QuestionId data-01`

## 跑题 Harness

见 `CCcodex/docs/ClaudeCode_Windows使用说明.md`（镜像 2.0）。容器挂载 **ccproject 下该题目录**，不是本基线路径。
