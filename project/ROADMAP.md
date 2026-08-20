# ROADMAP

从"架构演示项目"走向"完整可上线系统"的路线图。优先级 P0 > P1 > P2。

代码位置约定：本文件中 `路径:行号` 均相对于 `code/python/`。

**状态图例：** ❌ 未开始 / 未达标 · ⚠️ 部分完成（有代码但验收未过） · ✅ 已验收通过（本轮）

**当前代码快照：** Git tag `v0.1-roadmap`（2026-08-12）为早期基线；其后 P0-0～P0-5 / P1 薄切片与 08-14 基线整改均在工作区（多数尚未打新 tag，亦未达到「企业验收全部通过」）。

**维护角色：** 上线化基线验收（安全+部署门禁）+ 需求池/版本规划；状态必须以代码路径 + 单测/联调证据为准，禁止「文档写完就算完成」。

---

## MVP 门禁（Phase A 出口标准）

> **MVP 定义：** 可对外演示的企业知识 Agent——上传 → 入库 → 检索/问答主链路可跑，安全基线与 CI 一致，有演示文档与已知限制说明。  
> **规则：** 下表全部 ✅ 才宣告 MVP 达标；未达标前 **禁止** 启动 Post-MVP 大功能（见下一节）。  
> **Cloud Agent / 总控：** 每轮迭代后更新本表状态；优先清 ⚠️/❌ 项。

| ID | 门禁项 | 验收命令 / 证据 | 状态 | 最近证据 |
|----|--------|-----------------|------|----------|
| **M1** | 全量单元测试稳定通过 | `cd project/code/python && REQUIRE_OPENAI_API_KEY=false DISABLE_LOCAL_EMBEDDINGS=1 UPDATE_MODE=off bash scripts/run_unit_tests.sh` → 0 failed | ✅ | 08-20：123 passed / 14 skipped（含 embeddings 探针单测） |
| **M2** | 部署 / 密钥门禁 | `python project/code/python/scripts/check_p0_3_deploy.py` → OK | ✅ | 08-20 deploy check OK |
| **M3** | P0 安全隔离 E2E | `e2e_tenant_neo4j.sh` + `e2e_neo4j_readonly.sh` passed | ✅ | 08-19 双租户 1 passed；只读 2 passed |
| **M4** | 入库 → 检索 → 问答主链路 | 上传 + ingest + QA API 有单测覆盖；可本地/compose 演示一次完整路径 | ✅ | 08-20 Cloud：无 Docker；`EMBEDDING_BACKEND=chroma` + 本机 chroma/neo4j + uvicorn。health ok（embeddings probe chroma/384）；upload **202** `task_id=bfeedf9f…`；task **succeeded** `chunks_count=1`；QA **200** `grounded=true`。网关 `/embeddings` 404 → 走 chroma。redacted 证据：`/opt/cursor/artifacts/m4_e2e/` |
| **M5** | 异步入库 + 任务状态 | `/api/ingest/tasks` 相关单测绿 | ✅ | `test_ingest_async.py` 等 |
| **M6** | 认证 + ACL 基线 | JWT 登录/撤销/文档 ACL 单测绿 | ⚠️ | P0-4 单测绿；SSO / 多副本 HA 非 MVP 范围（不阻塞演示） |
| **M7** | CI 与本地测试入口一致 | `.github/workflows/ci.yml` 调用 `run_unit_tests.sh` | ✅ | 08-15 CI 对齐 |
| **M8** | MVP 演示手册 | `project/docs/` 下独立页：启动步骤、演示路径、已知限制 | ✅ | 08-19 `project/docs/MVP_demo_guide.md`（08-20 补 embeddings 三选一 / worker / egress） |

**MVP 总状态：⚠️ 演示主链路已通（M4 ✅）；仍标 ⚠️ 因 M6 口径未收（SSO/HA 延后，不阻塞对外演示）**

**面向可上线 MVP 的下一刀（按优先级，勿开 Post-MVP B 大改）：**

1. **M6 口径：** 将「JWT/ACL 单测绿」标 ✅，SSO/HA 明确延后。
2. **有 Docker 时补一轮 compose 同构验收**（bridge 网络从容器内验 LLM+embedding；勿把 `network_mode: host` 当生产方案）。
3. **P0 剩余薄切片**（按企业验收，非本轮）。

---

## 可上线 MVP 阻塞：本质问题（2026-08-20 联调沉淀）

> **目的：** 把「本机联调翻车」翻译成**产品上线必须闭环的依赖契约**，避免只修一次演示环境。  
> **证据环境：** compose + LLM 网关实跑；health ok → 上传 202 → 任务 `49643c07…` failed → QA 502。  
> **不是：** 「换个 API Key 就能上线」；密钥可用时仍可能因网络或 embeddings 能力缺口挂掉主链路。

### 本质分层

| # | 表象 | 本质（上线视角） | 为何卡 MVP | 已做缓解 | 上线仍必须做 |
|---|------|------------------|------------|----------|--------------|
| 1 | 宿主机 `curl /models` 通，bridge 容器调 LLM 超时 | **运行面 egress ≠ 开发机浏览器/宿主机出网**。本机 Clash/Mihomo TUN 截断 Docker bridge；生产常见等价物是安全组/NAT/无出网/未注入代理 | 演示与生产若网络模型不一致，会「开发机能答、上线全超时」 | `docker-compose.dev.yml`：`api`/`ingest-worker` `network_mode: host`（**仅本机绕行**）；手册注明 TUN 风险 | 生产用 bridge/K8s 时做 **从 Pod/容器发起的** LLM+embedding 连通性验收；禁止只测宿主机 |
| 2 | ingest `store_vectors`：`Embeddings API is not supported`（404） | **Chat 兼容网关 ≠ Embedding 能力**。`EMBEDDING_BACKEND=auto` 非 deepseek URL 时走 OpenAI Embeddings；很多国产/聚合网关只开 chat | 无向量则文档不得 `ready`，QA 空检索/502 —— **主链路断裂** | 联调侧拟默认 `EMBEDDING_BACKEND=local`；dev overlay 已注入 | **产品默认策略**：生产文档写清三选一——(A) 提供可用 `/embeddings` 的 base_url/model；(B) 镜像内 local/ONNX 并验收冷启动；(C) 启动时探测 embeddings，失败则 health 降级且拒绝接受 ingest |
| 3 | `ingest-worker` 曾直接退出；任务长期 `queued` / arq job expired | **异步入库把正确性绑在 worker 进程上**；worker 因编译 QA graph 缺 checkpointer 崩溃 = 静默积压 | 上传 202 给用户「成功错觉」，后台不消费 | worker 只编译 `pipelines=("ingest","update")`，不再强依赖 QA checkpointer | compose/K8s：**worker 副本与 API 同发布**；health/metrics 暴露队列深度与 worker up；积压告警（对齐 B2/B5，MVP 至少要有「worker 挂了可见」） |
| 4 | health 里 `knowledge_graph_live: AuthError`，整体仍可能 `status=ok` | **聚合 health 与读写路径不一致**；只读账户/口令漂移时 live 探针失败但业务可能半残 | 运维误判「系统正常」 | 既有 live 字段 | MVP 验收：核心依赖 live 失败 → HTTP 503 或明确 `degraded` 且前端/手册禁止对外演示（对齐 B2） |
| 5 | 单测绿 ≠ compose 主链路绿 | **Mock/关 embedding 的 CI 路径覆盖不了真实网关能力矩阵** | 宣称 MVP 可演示但无真实 succeeded 证据 | M1/M8 已绿；M4 仍 ⚠️ | M4 关闭条件写死：**真实 compose + 真实 LLM/embedding 一次 succeeded 日志** |

### 对「可上线 MVP」的结论（非企业可售）

- **可以称为演示 MVP 的前提：** M4 在目标部署拓扑上出现一次 `upload 202 → task succeeded（含 chunks）→ QA 200`，且 embeddings 策略已写入 `.env.example` / 部署文档。  
- **当前尚未达到：** 聊天可用但 embedding 不可用时，系统仍接受上传并最终 failed —— 对用户不友好，也不符合「可上线」口径。  
- **明确非本迭代范围（勿膨胀）：** 关全机 Clash、KMS、SSO、完整 chaos、企业工作台。

### 2026-08-20 执行记录（M4 实跑 + 根因入库）

**角色：** 运维 / QA（联调）+ 迭代 PM（把问题升格为 MVP 门禁）

**已完成：**

1. 拉取 `enterprise-knowledge-agent-v3` 最新（含 M8 手册）。
2. 诊断并绕过本机 Docker bridge 出网：`docker-compose.dev.yml` host network + localhost 依赖端口。
3. 修复 ingest-worker 启动：`build_knowledge_graph_workflow(..., pipelines=("ingest","update"))`。
4. 按 `MVP_demo_guide.md` 实跑：health `ok`；登录 ok；上传 **202** `task_id=49643c07…`；任务 **failed**（Embeddings 404）；QA **502**。证据目录（本机）：`/tmp/mvp-e2e-evidence-final/`。
5. 将上述根因写入本节；M4 保持 ⚠️（有失败证据，禁止标 ✅）。

**下一刀：** 落地「embeddings 默认可用」配置 + 再跑通 M4；同步启动探针/文档契约。

### 2026-08-20 执行记录（关掉 M4：embeddings 契约 + 实跑）

**角色：** backend_engineer + devops（总控严格模式；用户指定只关 M4）

**环境限制：** Cursor Cloud **无 Docker**；OPENAI_* Secrets 可用但网关 **`/embeddings` → 404**。用 `chroma run` + Neo4j community tarball + `uvicorn` 跑通主链路（非 compose；compose 默认已改为 `EMBEDDING_BACKEND=${EMBEDDING_BACKEND:-chroma}`）。

**代码/配置：**

1. `services/embeddings_ready.py`：探针 + `ensure_embeddings_ready`；upload 前 503 + 三选一 hint。
2. `vector_store._create_embeddings`：`auto` 探针失败 → **chroma** 降级；`openai` 显式失败不静默。
3. health：embeddings 纳入 core；`ingest_queue` 深度 / worker 可见性（local workers / arq heartbeat）。
4. ingest-worker：写 Redis heartbeat；启动时探针 embeddings。
5. `.env.example` / `.env.production.example` / `MVP_demo_guide.md` / compose 默认 chroma。

**实跑证据（redacted，`/opt/cursor/artifacts/m4_e2e/`）：**

- health `status=ok`，`embeddings_probe.backend=chroma` dims=384
- upload HTTP **202** `task_id=bfeedf9fc5a046ae9185746a91f4d055`
- task **succeeded**，`chunks_count=1`，`index_status=ready`
- QA HTTP **200**，`grounded=true`，sources≥1
- 单测：123 passed / 14 skipped；`check_p0_3_deploy.py` OK

**M4 → ✅。** 下一刀：收 M6 口径；有 Docker 时补 compose bridge 同构验收。

---

## Post-MVP / AI Engineering Backlog

> **Phase B：** MVP 全 ✅ 后启用。每项 = 一个薄切片 + 测试/文档 + ROADMAP 证据 + commit。  
> **禁止：** 在未完成 MVP 门禁时并行开 B 系列大改。

| 优先级 | ID | 主题 | 目标（可验收） | 非目标 | 依赖 ROADMAP | 状态 |
|--------|-----|------|----------------|--------|--------------|------|
| 1 | **B1** | **记忆系统** | 用户级长期记忆（Postgres/向量摘要）+ 与会话 checkpointer 分层；QA 可引用记忆；有单测 | 完整 mem0 克隆、跨产品记忆联邦 | P0-4 会话持久 | ❌ |
| 2 | **B2** | **容错与优雅降级** | 统一依赖健康矩阵（Neo4j/Chroma/Postgres/LLM）；health→503 与 QA 行为一致；消除 silent `except: pass` | 全链路 chaos 工程 | P1-3 告警 | ⚠️ 部分（LLM retry/health 已有；08-20 暴露 chat≠embed / egress 契约缺口） |
| 3 | **B3** | **高并发与吞吐** | 生产默认 `arq`+Redis 路径文档化；QA 租户配额扩展；热点路径 async 薄切片 | K8s HPA / 多区域 | P1-2 限流 | ⚠️ 部分（限流/队列已有；worker 存活可见性仍薄） |
| 4 | **B4** | **RAG 质量工程** | grounded 引用校验加强；`eval_rag_recall.py` 可选 CI 门禁；GraphRAG 加权可配置 | 完整离线评测平台 | P2 GraphRAG | ⚠️ 部分（强制拒答已落地） |
| 5 | **B5** | **可观测与运维** | Prometheus 规则与 runbook 对齐；audit log 与 admin UI 数据一致 | 全栈 APM / Tracing | P1-3 | ⚠️ 部分（check_alerts 已有） |
| 6 | **B6** | **产品化薄切片** | onboarding 优化；grounded 可视化；对外合规话术与 `project/docs` 同步 | 完整企业工作台 | P1-6 | ⚠️ 部分（空库引导/审计 UI 已有） |
| 7 | **B7** | **意图路由 + RRF** | 查询意图分类 → 多路检索 RRF 融合；相对纯向量基线有分桶报告 | 自动 prompt 优化 | P2 检索 | ❌ |
| 8 | **B8** | **文档生命周期** | 上传审批流 / 失效撤回 / 责任人；最小可用状态机 | 完整 DAM | P2 治理 | ❌ |
| 9 | **B9** | **多模态回归** | 损坏/超大文件/真实依赖组合故障注入套件 | 全格式 OCR 精度竞赛 | P2 多模态 | ❌ |

**Post-MVP 执行顺序建议：** B2（统一降级）→ B3（并发默认路径）→ B1（记忆）→ B4（RAG 评测）→ B5 → B6 → B7 → B8 → B9。

---

## 2026-08-14 基线验收记录

- 已确认真实项目位于 `project/code/`，核心后端位于 `project/code/python/`，当前前端仍为静态演示台（已有 grounded 标签与上传可见性，仍非企业工作台）。
- 已移除登录页内置的管理员用户名/密码；前端退出现在调用后端 `/api/auth/logout` 撤销当前 token。
- 已补充 `code/python/.env.production.example`，生产启动说明已加入强密钥与 Secret Manager 要求；文档中心见 `project/docs/README.md`。
- `python scripts/check_p0_3_deploy.py`：通过。
- `docker compose config --quiet`（注入强密钥）：通过。
- Python AST 全量解析：通过。
- 全量 pytest：当前环境的 `pytest` 可执行文件使用系统 Python 3.12，无法导入已安装依赖；改用项目 Python 后测试曾进入运行态但长时间无输出，已中止，尚无通过结论。
- 未进行真实 Neo4j / Chroma / Postgres / Kafka 联调。

## 2026-08-15 路线图校准（本轮文档）

> 角色：对照代码消除 ROADMAP 自相矛盾，重排「下一小版本」范围；**不宣称**新的代码验收通过。

**校准结论：**

1. P0-0～P0-5、P1-1/P1-2/P1-6 的**代码薄切片**已在仓库中（见各节「已修复」），但企业级验收（真实依赖 E2E / TLS / 集群）大多仍为 ❌/⚠️。
2. 「已交付能力」旧表曾写「图谱无租户 / MemorySaver / UI 不展示 grounded」，与当前代码不符，已改写。
3. CDC 专项备忘曾把已落地的 ACL/抑制双跑/DLQ 标成 ❌，已按 P0-2 / P1-1 正文对齐。
4. **当前最大阻塞不是再开新功能**，而是：固定 Python/测试入口 → 用真实依赖把剩余 P0 验收跑绿。

### 下一小版本范围（建议 `v0.2-hardening`，单人可控）

| 优先级 | 项 | 目标（完成标准） | 延后 |
|--------|----|------------------|------|
| **必做** | 测试入口 | 固定 venv/容器 Python；`pytest` 可超时、可重复、CI 同环境 | — |
| **必做** | P0-1 剩余 | 真实 Neo4j 双租户四类查询越权回归；只读账户（配合 P0-5） | 平台管理员跨租模型可下一版 |
| **必做** | P0-2 剩余 | 检索强制过滤非 `ready`；Chroma+Neo4j 断一端空洞联调 | 完整 outbox/两阶段可下一版 |
| **应做** | P0-3 剩余 | TLS 终止草案 + 密钥轮换/回滚 runbook（文档+演练清单） | 完整 K8s/KMS 可延后 |
| **可并行** | P1-6 | 空库引导 + `grounded=false` 强警示；不扩成完整工作台 | 用户管理/审计看板 → 下下版 |
| **明确不做（本版）** | P1-4/P1-5 全量、P2 审批流、企业工作台大改 | 防需求膨胀 | 列入 backlog |

### 下一执行顺序

1. ~~建立固定 Python 环境和可超时的测试入口~~ → **08-15 已完成（99 passed）**。
2. ~~P0-1 真实双租户 E2E~~ → **08-15 已通过**；P0-5 只读账户：**代码/脚本已落地**，需本机 `create_neo4j_readonly_user.sh` + `e2e_neo4j_readonly.sh`。
3. ~~P0-2 ready 过滤 + 断存储/删除一致性 E2E~~ → **08-18 `e2e_ingest_storage_fault.sh` 5 passed**；完整 outbox/两阶段仍延后。
4. ~~P0-3 本地 TLS/轮换清单~~ → **08-19 HTTPS + deploy check + `drill_jwt_rotation.sh` 已通过**。
5. ~~P1-6 薄增强~~ → **08-19 已落地**（含 admin 用户/审计 UI）。
6. ~~P1-1 watch/Kafka E2E~~ → **08-19 watch 2 passed + Kafka 1 passed**。
7. ~~P1-3 告警门禁~~ → **08-19 `check_alerts.py` + Prometheus 规则 + 文档**。
8. ~~P2 grounded 强制拒答~~ → **08-19 `qa_refuse_ungrounded` 默认开启**。

### 2026-08-19 执行记录（M8 MVP 演示手册）

**角色：** 迭代项目经理 / 交付文档（用户指定只做 M8）

**已完成：**

1. 新增 `project/docs/MVP_demo_guide.md`：compose/uvicorn 启动、health、UI + curl 的 upload → `/api/ingest/tasks` → `/api/qa/ask`、已知限制。
2. `project/docs/README.md` 增加演示手册入口。
3. 全量单测回归：`REQUIRE_OPENAI_API_KEY=false DISABLE_LOCAL_EMBEDDINGS=1 bash scripts/run_unit_tests.sh` → **114 passed, 14 skipped, 0 failed**（Python 3.12.3）。

**仍缺：** M4 需在有 LLM + compose 的环境按手册走通一次并贴日志；M6 SSO/HA 非 MVP，基线单测已绿但仍标 ⚠️。

**下一刀：** M4 本机/compose 端到端书面验收（按 `MVP_demo_guide.md` 实跑），不要开 Post-MVP B 系列。

### 2026-08-19 执行记录（MultiAgent：P0-5 / P0-3 / P1-6）

**角色：** 安全工程师 + 运维 + 前端工程师（并行）

**已完成：**

1. **P0-5 Neo4j 只读账户 E2E**
   - `KnowledgeGraphService._neo4j_session`：读驱动使用 `READ_ACCESS`（Community 无 RBAC 时的替代）。
   - `create_neo4j_readonly_user.sh`：密码幂等同步；Community 跳过 `GRANT ROLE reader` 并提示。
   - **验收：** `bash scripts/e2e_neo4j_readonly.sh` → **2 passed**（拒写 + 只读检索）。

2. **P0-3 本地 TLS / 部署门禁**
   - **验收：** `curl -k https://127.0.0.1:8443/api/health` 返回 JSON（TLS 终止正常；依赖 degraded 与 vector_store 状态有关）。
   - **验收：** `python scripts/check_p0_3_deploy.py` → **OK**。
   - **08-19 续：** `bash scripts/drill_jwt_rotation.sh` → 旧 JWT 401 / 新登录 200 / 回滚 200。

3. **P1-6 空库引导 + 不可信答案警示**
   - `static/app.js`：空库时 QA 面板展示引导横幅（链到上传 Tab）；`grounded=false` 时答案上方强警示。
   - **续（同日晚）：** 空库拦截提问；admin 用户管理/审计看板；`GET /api/auth/users`；合规免责声明。
   - **仍缺：** 完整企业工作台；~~后端强制拒答（P2）~~ → **08-19 已落地 `qa_refuse_ungrounded`**。

### 2026-08-19 执行记录（P1-4 / P1-5 / P1-1 续）

**已完成：**

1. **P1-4 备份/恢复演练** — `restore.sh` + `drill_backup_restore.sh`；Postgres dump 校验 `pg_restore --list`；`drill_backup_restore.sh` **PASSED**；**`DRILL_RESTORE=1` live restore** → knowledge 库 **7 public tables** 恢复 OK。
2. **P1-5 CI staging** — 根目录 `.github/workflows/ci-staging.yml`（`check_alerts.py` + `drill_jwt_rotation.sh` + `gen_ci_env.sh`）；主 CI 增加 `test_check_alerts`；**push main / workflow_dispatch 触发**。
3. **P1-1 Kafka 续** — 毒丸（非法 JSON / process 失败 → DLQ）+ 重平衡 consumer handoff E2E **4 passed**。

**全量单测：** 114 passed, 11 skipped。

### 2026-08-19 执行记录（MultiAgent：P0-3 JWT / P1-1 / P1-3 / P2）

**已完成：**

1. **P0-3 JWT 轮换演练** — `scripts/drill_jwt_rotation.sh`：旧 token 401 → 新登录 200 → 回滚 200。
2. **P1-1 CDC E2E** — `e2e_cdc_watch_kafka.sh`：watchdog 创建/抑制 **2 passed**；Kafka produce/consume/process **1 passed**。
3. **P1-3 告警** — `check_alerts.py`（health 核心依赖 + metrics `dependency_up`）；`prometheus/alerts.yml`；`docs/09_deployment/alerting.md`。
4. **P2 强制拒答** — `settings.qa_refuse_ungrounded=True`；`QAAgent` grounded=false 时返回拒答模板。

**下一刀：** ~~P1-4 备份恢复演练~~ → **08-19 drill 通过**；~~P1-5 staging CI~~ → **ci-staging.yml**；P1-1 集群重平衡仍延后。

### 2026-08-15 执行记录（测试入口 + P0-2 ready 过滤）

**角色：** 上线化基线验收 / 测试门禁（严格）

**已完成：**

1. **固定单测入口（`v0.2-hardening` 第 1 项）**
   - `code/python/scripts/run_unit_tests.sh`：优先 conda `agents`（Python 3.11，对齐 CI）、`DISABLE_LOCAL_EMBEDDINGS=1`、整墙 `WALL_TIMEOUT` + `pytest.ini` 单测 60s。
   - `pytest.ini` / `requirements-test.txt`；CI 改为调用同一脚本并安装 test extras。
   - 修复：`embeddings_available` 在已注入 embeddings 时不再被 `DISABLE_LOCAL_EMBEDDINGS` 误杀；补装 `langgraph-checkpoint-sqlite`。
   - **验收：** `./scripts/run_unit_tests.sh` → **99 passed**（约 2s，可重复）。

2. **P0-2 检索 ready 过滤（薄切片）**
   - `StateStore.document_search_gate`：`allow|deny|unknown`（无记录=遗留放行）。
   - `VectorStoreService` 搜索后过滤；`api/main.py` lifespan 注入 gate。
   - 单测：`test_is_document_searchable_*` / `test_search_filters_pending_failed_and_state_store`。
   - **仍缺：** 真实 docker 断存储 E2E（脚本已覆盖入库失败 + 删除一致性，待本机跑绿）。

**续：P0-1 真实 Neo4j 双租户 E2E**

- 新增 `tests/test_neo4j_tenant_e2e.py` + `scripts/e2e_tenant_neo4j.sh`（`RUN_NEO4J_E2E=1`）。
- **验收：** `bash scripts/e2e_tenant_neo4j.sh` → **1 passed**（同名实体两租户隔离、跨租搜空、邻居不泄漏、拒写/拒无租户谓词、按 source 清理）。
- **仍缺（P0-1/P0-5）：** Neo4j 只读库账户；平台管理员跨租模型。

**下一刀：** Neo4j 只读账户（P0-5）→ P0-2 断存储联调 → P0-3 TLS/轮换演练清单。

---

## 现状速览

| 模块 | 能力 | 真实状态 |
|------|------|----------|
| 文档入库 | PDF/图片/Excel/Markdown 解析 → 分块 | ⚠️ 解析可跑；**上传已做 UUID/白名单/限额**（P0-0）；杀毒与解析资源配额仍可选 |
| 知识抽取 | LLM NER + 关系 + 事件 → 三元组 | ✅ 真可跑；另有别名/相似度 `resolve_entities`（薄切片） |
| 知识图谱 | Neo4j 实体/关系，version + 索引 | ⚠️ MERGE (tenant_id,name)；双租户 E2E 已通过；**只读账户 E2E 08-19 2 passed**（Community 用 READ_ACCESS） |
| 向量检索 | Chroma HTTP 真写入/查询 | ✅ `HttpClient` 路径已验收；pgvector `delete_by_doc_id` 已实现；**检索已接 document_search_gate（P0-2）**；断存储删除 fail-closed，**08-18 E2E 5 passed** |
| GraphRAG | 向量 + 子图 + 路径 + 社区 + 重排 | ⚠️ 代码存在；图谱检索已注入 tenant；QA 已停自由 Cypher（P0-5）；固定加权待验证（P2） |
| 问答 | 意图→改写→混合检索→生成 | ⚠️ 主流程 + 多轮 + `grounded` 字段；**空库引导 + 不可信答案强警示（P1-6）**；拒答未强制 |
| 异步入库 | 202 + task_id + 本地/arq 队列 | ✅ 默认 async；任务状态 API 与单测已绿（全量 pytest 环境待恢复） |
| 增量更新 | Watchdog / Kafka → `process_change` | ⚠️ **接线与真落库已完成**；抑制双跑/单飞/Kafka DLQ 有单测（P1-1）；**真实集群 E2E 仍缺** |
| 认证 ACL | JWT + 文档 ACL | ⚠️ 向量/图谱隔离 + token 撤销/审计/Sqlite 会话（P0-4）；无 SSO/多副本 HA |
| 可观测性 | JSON 日志 / request-id / metrics / health | ⚠️ 基础已通；缺 SLO、告警、演练；内存 state 降级仍可能显示可用 |
| 多轮对话 | `session_id` + checkpointer | ⚠️ 默认 `AsyncSqliteSaver` 持久；多副本需共享盘/Postgres checkpointer |
| 测试 / CI | pytest + GitHub Actions | ⚠️ **08-19：`run_unit_tests.sh` → 108 passed / 8 skipped**；`e2e_neo4j_readonly.sh` → **2 passed**；`e2e_ingest_storage_fault.sh` → **5 passed** |
| 前端 | 静态演示 UI | ⚠️ **P1-6 薄切片已齐**：空库引导/拦截、grounded 强警示、用户管理+审计看板（admin）；完整企业工作台仍延后 |
| 落地就绪度 | 安全可售 · 运维可扛 · 业务可信 | ❌ 可内部演示；未过安全隔离与多租户真实联调前不当正式产品 |

---

## 已交付能力（迭代至工作区现状，含 `v0.1-roadmap` 之后薄切片）

> 下列为**已有代码 +（历史上）单测/部分联调**的能力对照。  
> **不等于** P0 企业验收清零；08-14 起全量 pytest 环境未恢复前，单测「曾绿」需重新跑通才能再宣称。

| 原目标 | 状态 | 验收说明（实际） |
|--------|------|------------------|
| 真 Chroma 向量写入/检索 | ✅ | `services/vector_store.py` HttpClient；本机 health `vector_store_live=ok`；手册评测召回可用 |
| CDC / watchdog 接入 lifespan + 真落库 | ⚠️ | 已启动并委托 `process_change`；抑制双跑/DLQ 有单测；**真实 watch/Kafka E2E 未过**（见 P1-1） |
| 4 Agent 单测 + JSON 降级 | ⚠️ | 曾报 55 passed；**08-14 全量 pytest 未复跑通过** |
| JWT + 文档 ACL + 图谱租户 | ⚠️ | 登录/鉴权/向量 where + Neo4j `(tenant_id,name)`；**缺真实双租户 E2E** |
| 多轮 session + 指代消解 | ⚠️ | API + 默认 Sqlite checkpointer 持久；**非多副本 HA** |
| Postgres 状态 / 幂等键 | ✅ | `STATE_STORE_DSN` + idempotency；失败可降级内存（生产应禁止静默降级） |
| 可观测性基础 | ⚠️ | 结构化日志、`X-Request-ID`、`/metrics`、live health→核心依赖 down 时 503 |
| 异步入库 202 + 任务查询 | ✅ | 默认 `INGEST_ASYNC`；`/api/ingest/tasks`；前端会轮询 |
| P0 上传/密钥门禁薄切片 | ⚠️ | P0-0/P0-3 核心单测与 compose 门禁已有；AV/TLS/KMS 仍缺 |
| P1-6 UI 薄切片 | ⚠️ | grounded/可见性/空库引导/强警示/用户管理/审计看板（admin）；强制拒答仍 P2 |
| P2 韧性薄切片 | ⚠️ | LLM timeout/retry、空 key→503、QA 限流、空检索拒答、`grounded` 字段、实体对齐、CI、`eval_rag_recall.py`；**拒答未强制** |

---

## 落地视角：能不能安全地卖、稳地跑、被人信

> 真实企业上线看的不只是「代码有没有 Agent」，而是：**合规会不会一票否决、运维能不能扛、业务敢不敢信答案**。  
> 技术骨架（入库 → 检索 → 问答 → 异步）已经能演示；要当作企业产品，应 **先过安全隔离与数据一致性，再补可运维与权限审计，最后才是 RAG 精度**。

### 第一层 — 上线会立刻翻车 → P0（均为 ❌ / 部分 ⚠️）

| 风险 | 项 | 状态 |
|------|----|------|
| 租户隔离不闭环 | P0-1 | ⚠️ 图谱 MERGE/读写已带 tenant；缺真实 Neo4j 双租户 E2E / 平台管理员模型 |
| LLM 自由 Cypher | P0-5 | ⚠️ QA/GraphRAG 已改参数化工具；只读 E2E **08-19 2 passed**；Community 无 RBAC，靠 READ_ACCESS |
| 上传路径/资源未收口 | P0-0 | ⚠️ 核心路径/限额/魔数已修；AV/cgroup 可选 |
| 入库/更新非原子；增量 ACL/图谱漂移 | P0-2 | ⚠️ 失败不得 ready + 删除先 deny 再双清；跨存储两阶段/outbox 仍薄 |
| 默认密钥 + 端口暴露 | P0-3 | ⚠️ 启动门禁 + compose 仅网关；**本机 HTTPS health + deploy check 08-19 通过**；KMS/公网证书仍缺 |
| 身份/会话不持久 | P0-4 | ⚠️ 撤销/审计/SQLite 会话已落地；SSO/多副本 HA 仍缺 |

**现在就该做（已按 08-19 校准）：** P1-1 真实 Kafka/watch E2E → P1-3 告警 → P0-4/P0-1 平台管理员模型；P0-3 JWT 轮换须人工演练一次。

### 第二层 — 运维扛不住 → P1

| 风险 | 项 | 状态 |
|------|----|------|
| 队列/CDC/双跑 | P1-1 | ⚠️ 异步入库✅；抑制双跑/DLQ 有单测；**真实 Kafka/watch E2E❌** |
| 无 K8s/多副本故事 | P0-3 / P1-2 / P1-5 | ❌ |
| 备份/彻底删除 | P1-4 | ⚠️ 仅有尽力 `backup.sh` |
| 租户成本配额 | P1-2 | ⚠️ 用户级+租户级 QA 限流已有；日预算/压测仍缺 |
| SLO/告警/演练 | P1-3 | ⚠️ 有指标/health，无告警体系 |

### 第三层 — 产品与组织 → P1-6 / P1-7 / P2

| 风险 | 项 | 状态 |
|------|----|------|
| 演示台 UI / 可信展示 | P1-6 | ⚠️ 空库引导/拦截、grounded 强警示、admin 用户+审计 UI 已落地；强制拒答/工作台仍缺 |
| 无知识治理审批流 | P2 文档生命周期 | ❌ |
| 中国区模型路由 | P1-7 | ⚠️ 可接兼容网关；无多模型/租户额度 |
| 对外过度承诺 | P1-6 | ⚠️ UI 已加合规免责声明；对外文档叙述仍须人工校准 |

### 第四层 — 质量 → P2

引用弱校验、固定图谱加权、多模态真实回归：均为 ⚠️/❌（有薄切片，未达生产评测）。

---

## P0 — 上线阻断项（安全、数据隔离、数据正确性）

### P0-0 上传入口安全与资源控制
**状态：⚠️ 部分完成 → 核心验收已通过（杀毒/进程配额仍可选）**

**已修复：**
- 不再用客户端 `file.filename` 直接拼接落盘路径；服务端 **UUID 文件名** + 展示名分离（`services/upload_security.py`）。
- `Path.resolve()` 后校验位于 `upload_dir` 内；隔离区 `_quarantine` 再晋升。
- 扩展名白名单（对齐 `DocParserAgent.SUPPORTED_EXTENSIONS`）；PDF/图片魔数与扩展名交叉校验。
- 流式写入 **大小限额**（`UPLOAD_MAX_BYTES`，超限 413）；空文件拒绝；PDF 页数上限（`UPLOAD_MAX_PDF_PAGES`）。
- 可选 ClamAV（`UPLOAD_AV_SCAN_ENABLED` / `REQUIRED`）。

**仍缺（非阻断本条核心验收）：** 解析进程 cgroup/非 root 资源配额；生产级强制杀毒默认开启。

**验收：**
- ✅ `../`、绝对路径客户端名 → 文件仍落在 upload root 内（单测）。
- ✅ 非法扩展名 / 伪造 PDF 魔数 → 400；超限 → 413（单测 `tests/test_upload_security.py`）。
- ⚠️ 杀毒与容器资源配额未默认强制。

### P0-1 租户数据隔离闭环
**状态：⚠️ 部分完成 → 图谱租户键与检索门禁已落地（缺真实双租户 E2E / 平台管理员）**

**已修复：**
- Neo4j `MERGE (e:Entity {tenant_id, name})`；关系同租户 MATCH；索引/约束尽量建 `(tenant_id, name)`。
- 入库 `store_graph` / 增量更新 upsert·delete 注入 `tenant_id` + `source`（来自 ACL metadata）。
- QA / GraphRAG：`search_entities` / `get_neighbors` / `shortest_paths` 强制 tenant；跨租户上下文丢弃。
- `execute_cypher` 默认拒写、拒无 `$tenant_id` 谓词（配合 P0-5）。

**仍缺：** 平台 vs 租户管理员跨租查询模型；历史无 `tenant_id` 节点迁移脚本。

**验收：**
- ✅ 单测：同名实体不同 tenant 的 MERGE 参数隔离；邻居/搜索/删除带 tenant；QA 同租户图谱保留、跨租户丢弃（`tests/test_knowledge_graph_tenant.py` / `test_qa_acl.py`）。
- ✅ 真实 Neo4j 双租户 E2E（`e2e_tenant_neo4j.sh`，08-15）。
- ❌ Neo4j 只读库账户 **E2E 08-19 已通过**（见 P0-5）。

### P0-2 跨存储入库原子性与可恢复性
**状态：⚠️ 部分完成 → 失败不得 ready + 增量 ACL/pgvector 删除已落地（跨存储两阶段提交仍薄）**

**已修复：**
- 文档索引状态机：`document_index`（pending → ready/failed）；仅成功存储后 `ready`。
- 入库 `store_vectors`/`store_graph` 显式 `vectors_ok`/`graph_ok`/`store_ok`；向量失败跳过图谱；图谱失败尝试回滚向量。
- `ingest_runner` / 同步入库：`assess_ingest_storage` 未通过则任务 failed、幂等 fail、**不**标 success。
- 增量更新：回填/创建文档 ACL 并写入 chunk metadata；MODIFY 清旧后重写；失败 → index failed。
- `pgvector` `delete_by_doc_id` 按 `cmetadata.doc_id` / id 前缀真删除（不再恒 0）。

**仍缺：** 真正的跨存储事务/outbox；MODIFY「先写新版本再切 active 再删旧」的零空洞窗口。

**08-15 增量：** 向量检索已强制 `document_search_gate`。

**08-17 增量：** 图谱检索按 `source`/`start_source` 走同一门控（邻居行不用实体名当 source）；`get_document_index_by_source`；单测 `test_graph_hits_filter_pending_source_not_entity_name`。

**08-18 增量（删除一致性）：**
- `delete_by_doc_id` / `delete_by_source` 在存储未连接时改为失败，禁止静默 `return 0`。
- 删除/修改先把门控打成 deny，再尽力清向量+图谱；任一侧失败则 `success=False`（可重试），修改失败不再写新版本。
- 单测：`test_delete_vector_fault_still_purges_graph_and_denies_search` 等。
- **08-18 真实联调：** `bash scripts/e2e_ingest_storage_fault.sh` → **5 passed**（入库断一端不得 ready；删除断 Chroma/Neo4j 一端须失败且检索 deny）。

**验收：**
- ✅ 单测：存储失败不得 ready；文档版本状态；增量 ACL stamp；pgvector 删除走后端路径。
- ✅ 单测：非 ready / failed metadata / 遗留文档检索门控。
- ✅ 单测：删除时一端失败仍清另一端 + 检索 deny；修改时图谱清理失败不得写入新版本。
- ✅ 08-18 E2E：`e2e_ingest_storage_fault.sh` → 5 passed（入库空洞 + 删除一致性）。

### P0-3 生产部署密钥与网络边界
**状态：⚠️ 部分完成 → 启动门禁 + compose 默认只暴露网关已落地（TLS/密钥托管仍缺）**

**已修复：**
- `config/secrets_guard.py`：`APP_ENV=production|prod` 或 `REQUIRE_STRONG_SECRETS=true` 时拒绝弱 `JWT_SECRET` / Neo4j / 管理员 / DSN 口令；API lifespan 与 ingest worker 启动即执行。
- `docker-compose.yml`：数据面端口不映射到宿主机；仅 `8080`；`NEO4J_PASSWORD` / `POSTGRES_PASSWORD` / `JWT_SECRET` / `AUTH_BOOTSTRAP_ADMIN_PASSWORD` 必填（无 `:-password` 弱默认）。
- `docker-compose.dev.yml`：本地叠加发布 DB 端口并关闭强密钥门禁。
- API 镜像非 root（uid 10001）；CI 跑 `scripts/check_p0_3_deploy.py` + 单测。

**仍缺：** 正式公网证书 / KMS / 集群 Ingress。本地自签 TLS 与轮换 runbook 已提供。

**08-17 增量：** `docker-compose.tls.yml` + `scripts/gen_selfsigned_tls.sh` + `docs/09_deployment/tls_and_secret_rotation.md`；`check_p0_3_deploy.py` 校验 TLS overlay 不暴露数据端口。

**验收：**
- ✅ 生产模式弱配置 → 进程拒绝启动（单测）；生产 compose 无数据端口宿主机映射（CI/单测）。
- ✅ 本地 HTTPS：`curl -k https://127.0.0.1:8443/api/health` 返回 JSON（**08-19 本机验证**）。
- ⚠️ JWT/DB 口令轮换演练清单（文档 §2–3）须人工勾选一次。
- ❌ 集群级 TLS 与外部 KMS 仍延后。

### P0-4 认证、审计与会话的持久化
**状态：⚠️ 部分完成 → 禁用立即生效 / 撤销 / 审计 / SQLite checkpointer 已落地（SSO/MFA/HA 仍缺）**

**已修复：**
- JWT 含 `jti` + `tv`（token_version）；`/api/auth/logout` 拉黑 jti；禁用用户 bump `token_version`，既有 token 立即 401。
- 审计表 `audit_events`：登录成败、登出、建用户、禁用、入库上传；`GET /api/auth/audit`。
- QA checkpointer：默认 `AsyncSqliteSaver`（`QA_CHECKPOINT_PATH`）；compose `auth_data` 卷挂载 `AUTH_DB_PATH` / checkpoint。
- 依赖：`langgraph-checkpoint-sqlite` + `aiosqlite`。

**仍缺：** SSO/MFA、多副本共享 Postgres checkpointer、平台管理员模型、完整威胁审计看板。

**验收：**
- ✅ 单测：禁用后旧 token 失效；logout 撤销；审计含 login/user_create；sqlite checkpointer 重启后可读。
- ❌ 未做多副本滚动联调与 SSO。

### P0-5 禁止 LLM 直接执行 Cypher
**状态：⚠️ 部分完成**

**已修复：** `QAAgent._graph_retrieve` 不再生成/执行自由 Cypher；改用租户参数化 search/neighbors/paths。GraphRAG 路径检索同。`execute_cypher` 拒写与无 tenant 查询。

**08-17 增量：** `KnowledgeGraphService` 读写 driver 拆分（`NEO4J_READ_USER`）；`create_neo4j_readonly_user.sh`；compose 注入只读账号；E2E `e2e_neo4j_readonly.sh`。

**08-19 增量：** 读驱动 session 使用 `READ_ACCESS`（Community 无 RBAC）；建用户脚本密码幂等；**E2E 2 passed**。

**仍缺：** AST/多语句白名单（若保留任意 Cypher API）；Enterprise 版 Neo4j RBAC 可选。

**验收：**
- ✅ QA 路径单测断言不调用 `execute_cypher`；写 Cypher / 无租户谓词 → `PermissionError`。
- ✅ 只读库账户 E2E：**08-19 `e2e_neo4j_readonly.sh` → 2 passed**。
---

## P1 — 可靠运行与合规运营

### P1-1 可靠异步任务与 CDC 消费
**状态：⚠️ 部分完成 → 本机 watch/Kafka E2E 已通过（集群重平衡/毒丸仍缺）**

**已有：**  
- 入库异步：`INGEST_ASYNC` → 202 + `task_id`；`ingest_jobs`；local/arq 队列；任务查询 API；相关单测。  
- CDC：**接线完成**——lifespan 启 watchdog/Kafka；`process_event` → `process_change`（非假计数）。  
- **P1-1 增量：** `suppress_watch` + 上传落盘后抑制；`_quarantine` 不监听；同 `doc_id` `asyncio.Lock` 单飞；Kafka `enable.auto.commit=False`，失败 → `{topic}.dlq` 后 commit（`handle_kafka_message`）。

**08-19 增量：** `tests/test_cdc_watch_kafka_e2e.py` + `scripts/e2e_cdc_watch_kafka.sh` — watch **2 passed**，Kafka **4 passed**（含毒丸 DLQ + 重平衡 handoff）。

**仍缺：** 真实 Kafka 集群强杀/重平衡丢消息 E2E；「改文件→stats 变」端到端。

**验收：**  
- ✅ 上传大文档可 202 后轮询至成功（手册评测曾跑通）。  
- ✅ 单测：watch 抑制、quarantine 过滤、doc 单飞、Kafka conf/DLQ。  
- ✅ **08-19 E2E：** `e2e_cdc_watch_kafka.sh` → watch 2 + kafka 4 passed（毒丸/重平衡）。  
- ❌ 强杀/重平衡不丢更新。

### P1-2 容量、性能与成本治理
**状态：⚠️ 部分完成 → 用户级 + 租户级 QA 配额已落地（日预算/压测仍缺）**

**已有：** QA 滑动窗口限流（`RATE_LIMIT_QA_PER_MINUTE`）；租户上限 `RATE_LIMIT_QA_PER_TENANT_PER_MINUTE`（默认 120）。  
**仍缺：** 日预算、HA 拓扑、压测基线、按租户成本看板。

**验收：** ✅ 超限返回 429（用户级 + 租户级单测）。❌ 压测 SLO / 日预算。

### P1-3 可观测性、告警与故障演练
**状态：⚠️ 部分完成 → 本地告警门禁 + Prometheus 规则已落地**

**已有：** JSON 日志、`request_id`、Prometheus `/metrics`、LLM 回调埋点、health live 探测（向量/state 失败 → HTTP 503）。  

**08-19 增量：** `scripts/check_alerts.py`（health 核心依赖 + `dependency_up`）；`code/prometheus/alerts.yml`；`docs/09_deployment/alerting.md`。

**仍缺：** OTel 全链路、Alertmanager 路由、RTO/RPO 演练；内存 state 降级仍可能显示可用。

**验收：** ✅ 本机 health/metrics 可用。✅ **08-19 `check_alerts.py` exit 0**（kg 降级 WARN）。❌ 告警路由与 RTO/RPO 演练。

### P1-4 备份、恢复、保留与数据主体权利
**状态：⚠️ 部分完成 → Postgres 备份/校验/恢复脚本已落地**

**已有：** `code/scripts/backup.sh`（Postgres/Neo4j 尽力备份）。  

**08-19 增量：** `restore.sh`、`drill_backup_restore.sh`、`docs/09_deployment/backup_restore.md`；演练 **PASSED**（Postgres dump + list）。

**仍缺：** Chroma/原件/ACL/Redis 一致快照；Neo4j 自动 load；空环境全栈恢复 + 检索一致性 E2E。

**验收：** ✅ **08-19 `drill_backup_restore.sh` exit 0**；✅ **`DRILL_RESTORE=1` live restore**（knowledge 7 tables）。❌ 全副本删除证明。

### P1-5 供应链与发布治理
**状态：⚠️ 部分完成 → CI staging 门禁已接入**

**已有：** `.github/workflows/ci.yml` 跑 pytest + P0-3 gate + alert 单测。  

**08-19 增量：** `.github/workflows/ci-staging.yml` — docker compose 栈上跑 `check_alerts.py` + `drill_jwt_rotation.sh`；`gen_ci_env.sh` 生成 staging `.env.ci`。

**仍缺：** 镜像 digest/SBOM/签名、密钥扫描门禁、灰度回滚。

**验收：** ✅ CI 单测门禁。✅ **staging workflow 定义完成**（根 `.github/workflows/ci-staging.yml`；push main / workflow_dispatch）。❌ 高危漏洞/未签名镜像不可发布。

### P1-6 产品运营闭环与可信 UI
**状态：⚠️ 部分完成 → v0.2 薄切片已齐（企业工作台/强制拒答仍延后）**

**已有：** API 返回 `grounded` / `grounding_notes`；前端聊天元信息展示 grounding 状态；上传面板可选 `private|tenant|public`。

**08-19 增量：** QA 面板空库引导横幅；`grounded=false` 强警示 callout；空库时拦截提问。

**08-19 续：** 管理员在「系统概览」可见用户管理（创建/禁用）+ 审计日志表格；`GET /api/auth/users`；QA 合规免责声明。

**仍缺：** 完整企业工作台；`grounded=false` 后端强制拒答（P2）；对外合规话术文档校准。

**验收：**
- ✅ grounded 与可见性可在 UI 操作
- ✅ 空库引导、不可信答案警示、空库拦截提问（08-19）
- ✅ 管理员用户管理 + 审计看板只读展示（08-19）
- ❌ 非工程师完整闭环与强制拒答仍缺

### P1-7 中国区 LLM 网关与模型/成本路由
**状态：⚠️ 部分完成**

**已有：** OpenAI 兼容 `base_url`；timeout/max_retries；空 key 快速 503；部分网关错误转 502/504。  
**仍缺：** 多模型分流、租户 key/日预算、内容审核、统一网关错误字典。

**验收：** ✅ 可指向兼容网关跑通评测。❌ 抽取/问答分模型 + 租户预算互不影响。

---

## P2 — 质量提升与规模化

### RAG 质量与安全评测
**状态：⚠️ 部分完成 → grounded=false 后端强制拒答已落地**

**已有：** `services/grounding.py` 弱校验；空上下文拒答；`evals/` + `scripts/eval_rag_recall.py`（手册 4/4 曾命中）。  

**08-19 增量：** `settings.qa_refuse_ungrounded=True`（默认）；`QAAgent` 校验失败时返回拒答模板而非 LLM 幻觉正文；单测 `test_qa_refuses_ungrounded_answer`。

**仍缺：** 标注集与线上抽检；忠实度指标；持续评测门禁。

**验收：** ⚠️ 离线小集可跑。✅ **08-19 强制拒答（薄切片）**。❌ 生产级持续评测门禁。

### 图谱检索权重的数据驱动优化
**状态：⚠️ 部分完成**

**已有：** 查询相似度重排（`services/rerank.py`）替代部分硬编码 ×1.25；类型仅作小加成。  
**仍缺：** 意图路由、RRF、分桶实验、路径必须带原文证据。

**验收：** ❌ 相对纯向量基线的分桶增益报告未建立。

### 文档生命周期与知识治理
**状态：❌ 未达标**

上传即索引；无审批/责任人/失效撤回/目录产品化。实体对齐仅有规则/别名薄切片。

### 多模态与检索回归测试
**状态：⚠️ 部分完成**

有解析/多模态相关单测；缺损坏文件/超大文件/真实依赖组合与故障注入回归套件。

---

## 建议执行顺序

**落地判断：** 先过「安全可售」与「数据可信」，再补「运维可扛」与「产品可运营」，最后投入 RAG 精度。  
**08-15 补充：** 薄切片代码已大量落地；下一刀优先「可重复验收」，再开功能面。

1. **立刻做：** 固定 Python/pytest 入口（与 CI 一致）并复跑全量单测。
2. **P0 剩余验收：** P0-1/P0-5 真实双租户 E2E + Neo4j 只读账户 → P0-2 ready 过滤与断存储联调 → P0-3 TLS/密钥轮换/回滚演练清单。
3. **产品薄并行：** P1-6 空库引导 + `grounded=false` 强警示（不做完整工作台）；P1-2 日预算可顺手则做。
4. **可运维（下一版）：** P1-1 真实 CDC E2E → P1-3 告警 → P1-4 恢复删除 → P1-5 发布治理。
5. **质量（P2，更后）：** 强制拒答、意图路由+RRF、文档审批、多模态真实回归。

---

## CDC 增量更新（专项备忘）

> **接线与真落库：✅** · **抑制双跑/DLQ/ACL/pgvector 删除（代码+单测）：⚠️** · **真实集群 E2E：❌**  
> 单测 Fake 绿 ≠「文件变更自动正确更新」验收通过。

| 层级 | 必须先做 | 状态 | 否则线上表现 |
|------|----------|------|--------------|
| P0 | 图谱 source + MODIFY/DELETE 清图；更新回填 ACL；pgvector 真删除 | ⚠️ 代码/单测已有；缺真实联调与 ready 检索过滤 | 静默错检索 / 越权 / 向量脏读 |
| P1 | 上传↔watch 隔离；Kafka 手动 commit + DLQ；doc 单飞 | ⚠️ 单测已有；**真实 watch/Kafka E2E 仍缺** | 双倍成本、丢更新、竞态覆盖 |
| P2 | chunk 级 diff、去掉装饰性 `compute_diff` | ❌ | 成本高、变更放大 |

---

## 变更记录

| 日期 | 版本/标记 | 变更摘要 |
|------|-----------|----------|
| 2026-08 | 初稿 | 建立 P0–P2 路线图；指出 chroma 空壳、CDC 未接入、缺测试等演示级缺口 |
| 2026-08-09 ~ 08-12 | 实现迭代 | 落地真 Chroma、CDC 接线、JWT/向量 ACL、多轮、状态幂等、可观测性基础、异步入库、P2 韧性薄切片；单测扩充；tag **`v0.1-roadmap`** 推送 GitHub（**不含**随后 ROADMAP 文档修订） |
| 2026-08-12 | 文档 v1.1 | 重写为「企业上线阻断」视角；澄清 CDC 已接线但仍有一致性缺口；补充落地四层、P1-6/P1-7 |
| 2026-08-12 | 文档 v1.2 | 按代码实情标注各条状态与验收说明；新增「已交付能力」对照表 |
| 2026-08-12 | **P0-0 实现** | 上传安全：`upload_security.py`（UUID 落盘、路径隔离、扩展名/魔数、流式限额、PDF 页数、可选 ClamAV）；API 接入；单测 `test_upload_security.py` |
| 2026-08-12 | **P0-1 实现** | Neo4j `(tenant_id,name)` MERGE；入库/增量/QA/GraphRAG 强制租户；停 LLM 自由 Cypher；单测 `test_knowledge_graph_tenant.py` + ACL/QA 更新；P0-5 薄修同批 |
| 2026-08-12 | **P0-3 实现** | `secrets_guard` 生产拒弱密钥；compose 默认不暴露数据端口 + `docker-compose.dev.yml`；非 root 镜像；CI `check_p0_3_deploy.py` |
| 2026-08-12 | **P0-2 实现** | `document_index` 状态机；入库存储失败不 ready；图谱失败回滚向量；增量 ACL 回填；pgvector 真删除；单测 `test_p0_2_ingest_atomicity.py` |
| 2026-08-13 | **P0-4 实现** | JWT jti/tv 撤销；禁用立即失效；审计日志 API；Sqlite QA checkpointer + compose 卷路径；单测 `test_p0_4_auth_session.py` |
| 2026-08-13 | **P1-1 / P1-2 / P1-6 薄切片** | watch 抑制与 quarantine、Kafka DLQ、doc 单飞；租户级 QA 限流；前端 grounded 标签 + 上传可见性（见对应节） |
| 2026-08-14 | **基线验收** | 去登录页内置凭据；logout 调后端撤销；`.env.production.example` + docs 中心；`check_p0_3_deploy`/compose config/AST 通过；**全量 pytest 未得出通过结论**；无真实依赖联调 |
| 2026-08-15 | **文档 v1.3（路线图校准）** | 消除「已交付/现状/CDC 备忘」与正文矛盾；重排 `v0.2-hardening` 范围；明确本版不做企业工作台大改 / P2 审批流 |
| 2026-08-15 | **测试入口 + P0-2 ready 过滤** | `run_unit_tests.sh`/`pytest.ini`/CI 对齐；99 passed；`document_search_gate` + 向量检索过滤；修复 DISABLE_LOCAL_EMBEDDINGS 误伤注入 embeddings |
| 2026-08-15 | **P0-1 Neo4j 双租户 E2E** | `test_neo4j_tenant_e2e.py` + `e2e_tenant_neo4j.sh` 真实联调通过；只读账户仍缺 |
| 2026-08-17 | **P0-2/3/5 本版收口** | 图谱 ready 过滤；断存储/只读 E2E 脚本；本地自签 TLS + 轮换 runbook；全量单测 106 passed / 8 skipped；真实 docker E2E 待本机跑 |
| 2026-08-19 | **P1-4/5/1 续** | backup drill + restore 脚本；ci-staging.yml；Kafka 毒丸/重平衡 E2E 4 passed |
| 2026-08-19 | **MVP 门禁 + Post-MVP Backlog** | 新增 Phase A 出口标准（M1–M8）与 Phase B AI 工程 backlog（B1–B9）；供 Cloud Agent 持续迭代 |
| 2026-08-19 | **M8 演示手册** | `project/docs/MVP_demo_guide.md`：启动步骤 + upload→ingest→QA + 已知限制；门禁 M8 → ✅ |

### 验收口径说明

- **✅ 已验收：** 有对应实现 + 单测或真实联调证据，且该项原「演示级」目标已达到。  
- **⚠️ 部分完成：** 有可用切片，但 ROADMAP 所写企业验收标准未满足。  
- **❌ 未达标：** 问题仍在或仅有文档方案，不能对客户宣称已修复。
