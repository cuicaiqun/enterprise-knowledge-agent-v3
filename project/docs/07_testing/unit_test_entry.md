# 单元测试入口（v0.2-hardening）

- 文档路径：`project/docs/07_testing/unit_test_entry.md`
- 最后更新：2026-08-15

## 固定命令

```bash
cd project/code/python
bash scripts/run_unit_tests.sh
```

## 环境约定

| 项 | 值 |
|---|---|
| Python | 3.11（本地默认 conda env `agents`；CI `setup-python 3.11`） |
| 单测超时 | `pytest.ini` → 60s/用例 |
| 整墙超时 | `WALL_TIMEOUT` 默认 600s |
| 嵌入 | `DISABLE_LOCAL_EMBEDDINGS=1`（避免拉本地大模型导致挂起） |

覆盖：`PYTHON_BIN=/path/to/python bash scripts/run_unit_tests.sh`

## 依赖

```bash
# 应用依赖（完整运行时）
pip install -r requirements.txt
# 单测 extras（timeout 等；勿在此拉 torch）
pip install -r requirements-test.txt
```

## 验收

- 2026-08-19：`./scripts/run_unit_tests.sh` → **108 passed, 8 skipped**。
- 2026-08-19：`bash scripts/e2e_neo4j_readonly.sh` → **2 passed**。
- 2026-08-19：`bash scripts/drill_jwt_rotation.sh` → **JWT 轮换/回滚演练通过**。
- 2026-08-19：`bash scripts/drill_backup_restore.sh` → **PASSED**。
- 2026-08-19：`bash scripts/e2e_cdc_watch_kafka.sh` → watch **2** + kafka **4 passed**。
- 2026-08-20：`tests/test_mvp_main_path.py` → **1 passed**（login→upload→task→QA TestClient）。
- 2026-08-20：`bash scripts/e2e_mvp_main_path.sh` → API 未启动时 **SKIP**（不把环境缺失当失败）。

## 断存储 E2E（P0-2，会短暂 stop 容器）

覆盖：入库时 Chroma/Neo4j 断一端不得 ready；删除时断一端须 `success=False`、检索 deny、另一端尽力清掉。

```bash
cd project/code/python
bash scripts/e2e_ingest_storage_fault.sh
```

## Neo4j 只读账户 E2E（P0-5）

```bash
cd project/code/python
# 在 Neo4j 中创建 reader 用户（会打印密码，勿提交）
NEO4J_PASSWORD=password bash scripts/create_neo4j_readonly_user.sh
export NEO4J_READ_USER=readonly NEO4J_READ_PASSWORD='...'
bash scripts/e2e_neo4j_readonly.sh
```

把 `NEO4J_READ_USER` / `NEO4J_READ_PASSWORD` 写入 `python/.env` 后重启 API，查询走只读驱动。


## Neo4j 租户 E2E（可选）

```bash
cd project/code/python
bash scripts/e2e_tenant_neo4j.sh
```

- 需要本机 Neo4j 可达（默认 `bolt://localhost:7687`，与 `.env` 一致）。
- 默认全量单测会 **skip** 该用例；仅 `RUN_NEO4J_E2E=1` 时执行。
- 2026-08-15 验收：1 passed。

## M4 主链路 E2E（可选，需 API 已启动）

```bash
cd project/code/python
ADMIN_PASS='...' bash scripts/e2e_mvp_main_path.sh
```

- 默认全量单测会 **skip** `test_mvp_main_path_e2e.py`；仅 `RUN_MVP_E2E=1` 且 `/api/health` 可达时执行。
- 契约单测（无 LLM）：`tests/test_mvp_main_path.py`，随全量套件跑。
