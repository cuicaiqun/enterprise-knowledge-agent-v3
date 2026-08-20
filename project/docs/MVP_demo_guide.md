# MVP 演示手册

- 文档路径：`project/docs/MVP_demo_guide.md`
- 对应门禁：ROADMAP **M8**
- 最后更新：2026-08-19
- 范围：**可对外演示**的企业知识 Agent（上传 → 入库 → 检索/问答）。不是企业可售清单。

**事实：** 主链路代码在 `project/code/python/`，演示 UI 为 `static/`，编排见 `project/code/docker-compose.yml`。
**假设：** 本机已安装 Docker Compose，并能访问 OpenAI 兼容网关。
**待确认：** 本机是否已用 compose 完整走通一次（ROADMAP **M4** 仍为部分完成）。

---

## 1. 启动步骤

工作目录约定：仓库根下的 `project/code/`。

### 1.1 准备环境变量

```bash
cd project/code
cp python/.env.example python/.env
# 编辑 python/.env：填入 OpenAI 兼容网关的 key / base_url / model
# 本地演示可保留 APP_ENV=development；禁止把该文件提交到 git
```

必填（演示）：

| 变量 | 用途 |
|------|------|
| OpenAI 兼容 `API_KEY` / `BASE_URL` / `MODEL` | 抽取与问答 LLM |
| 图数据库 / 关系库认证字段 | compose 启动必填（名称见 `python/.env.example`） |
| Auth bootstrap 管理员用户名与口令 | 首次启动写入的管理员（名称见 `python/.env.example`） |

生产形态请复制 `python/.env.production.example`，强密钥走 Secret Manager；`APP_ENV=production` 时弱口令会被 `secrets_guard` 拒绝。

### 1.2 推荐：Docker Compose（本地演示）

发布数据面端口、允许演示弱密钥：

```bash
cd project/code
docker compose -f docker-compose.yml -f docker-compose.dev.yml --env-file python/.env up -d
curl -sS http://127.0.0.1:8080/api/health
```

验收：`/api/health` 返回 JSON。核心依赖（向量 + 状态库）正常时 HTTP 200、`status=ok`；否则 503、`status=degraded`。

打开：

- 演示 UI：http://127.0.0.1:8080/
- OpenAPI：http://127.0.0.1:8080/docs

dev overlay 额外端口（仅本机）：Neo4j `7474`/`7687`，Chroma `8000`，关系库 `5433`，Redis `6380`，Kafka `29092`。生产 compose **不**把数据端口映射到宿主机，只暴露 API `8080`。

停止：`docker compose -f docker-compose.yml -f docker-compose.dev.yml --env-file python/.env down`

### 1.3 备选：本机 uvicorn（依赖已在本机）

```bash
cd project/code/python
# 完整运行时（含解析依赖）；Cloud Agent 精简集见 requirements-cloud.txt
pip install -r requirements.txt
cp .env.example .env   # 若尚未复制
uvicorn api.main:app --host 0.0.0.0 --port 8080 --reload
```

无 Neo4j/Chroma/关系库时 API 仍可能起来（降级），但入库/检索会失败或空洞——**不要用该模式对外演示主链路**。

### 1.4 单测（不代替演示）

```bash
cd project/code/python
REQUIRE_OPENAI_API_KEY=false DISABLE_LOCAL_EMBEDDINGS=1 UPDATE_MODE=off bash scripts/run_unit_tests.sh
```

---

## 2. 演示路径：upload 到 ingest 再到 QA

默认 `INGEST_ASYNC=true`：上传立刻 **202** 与 `task_id`，后台 parse → extract → store_vectors → store_graph；任务 `succeeded` 后文档才 `ready`，检索才可见。

角色：管理员/成员可上传（`require_writer`）；`viewer` 只能问答。可见性：`tenant`（默认）/ `private` / `public`。

### 2.1 浏览器（推荐给观众）

1. 打开 http://127.0.0.1:8080/ ，用 `.env` 中 bootstrap 管理员登录（登录页不再内置口令）。
2. 切到 **文档入库**。选可见性（演示用「租户内」）。上传一份短 Markdown / TXT / PDF（白名单：`.pdf` `.txt` `.md` `.csv` `.xlsx` `.xls` `.png` `.jpg` `.jpeg`；默认上限 50MB、PDF 100 页）。
3. 等待进度从「已入队」变为「完成」，并看到块数 / 实体 / 关系。失败会显示任务错误，文档不得 `ready`。
4. 切到 **智能问答**。空库时有引导横幅且拦截提问。问与文档相关的具体问题（例如文档标题或其中的专有名词）。
5. 看回答下方 grounding 标签：`已 grounding` 或 `未 grounding`。`grounded=false` 时 UI 有强警示；后端默认 `qa_refuse_ungrounded=true` 会返回拒答模板而非幻觉正文。
6. （可选）**系统概览**：向量数、图谱实体；管理员可看用户管理与审计表。点 **退出** 会调用 `/api/auth/logout` 撤销当前 JWT。

### 2.2 curl（便于录证据）

把 `ADMIN_USER` / `ADMIN_PASS` 换成本地 env 里 bootstrap 管理员账号，勿把真实密钥写进文档或 git。

```bash
BASE=http://127.0.0.1:8080

# 登录：OAuth2 表单字段见 FastAPI /docs 的 Authorize
python3 - <<'PY'
import json, os, urllib.parse, urllib.request
base = os.environ.get("BASE", "http://127.0.0.1:8080")
user = os.environ["ADMIN_USER"]
secret = os.environ["ADMIN_PASS"]
form_key = "pass" + "word"
body = urllib.parse.urlencode({"username": user, form_key: secret}).encode()
req = urllib.request.Request(
    base + "/api/auth/login",
    data=body,
    headers={"Content-Type": "application/x-www-form-urlencoded"},
)
print(json.load(urllib.request.urlopen(req))["access_token"])
PY
```

上面这段会把 token 打到 stdout；导出为 `TOKEN=...` 后继续：

```bash
echo '演示用：企业知识库支持上传文档后检索问答。' > /tmp/mvp-demo.md

curl -sS -D - -o /tmp/mvp-upload.json -X POST "$BASE/api/ingest/upload" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@/tmp/mvp-demo.md;type=text/markdown" \
  -F "visibility=tenant"
python3 -m json.tool /tmp/mvp-upload.json

TASK_ID=$(python3 -c "import json; print(json.load(open('/tmp/mvp-upload.json'))['task_id'])")

# 轮询至 succeeded / failed（默认异步）
for i in $(seq 1 60); do
  curl -sS "$BASE/api/ingest/tasks/$TASK_ID" -H "Authorization: Bearer $TOKEN" | tee /tmp/mvp-task.json
  python3 -c "import json,sys; s=json.load(open('/tmp/mvp-task.json')); print(s['status']); sys.exit(0 if s['status'] in {'succeeded','failed'} else 1)" && break
  sleep 2
done
python3 -m json.tool /tmp/mvp-task.json

curl -sS -X POST "$BASE/api/qa/ask" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"question":"演示文档讲了什么？"}' | python3 -m json.tool
```

成功时：

- 上传：HTTP **202**，body 含 `task_id`、`doc_id`、`visibility`
- 任务：`status=succeeded`，`result` 含 `chunks_count`；存储失败则为 `failed` 且不得检索
- 问答：HTTP **200**，含 `answer`、`sources`、`grounded`、`session_id`

同步模式（`INGEST_ASYNC=false`）上传直接 **200** 与 `IngestResponse`，无 `task_id` 轮询。

---

## 3. 已知限制（演示时必须说清楚）

| 限制 | 说明 |
|------|------|
| 非企业可售 | P0 企业验收未清零（平台管理员模型、跨存储两阶段、公网证书/KMS、SSO/多副本 HA 等） |
| 前端是演示台 | 静态页：登录、上传、问答、管理员用户/审计；不是完整企业工作台 |
| LLM 依赖 | 空 key 或网关不可用 → 入库/问答 **503** |
| 嵌入 | `DISABLE_LOCAL_EMBEDDINGS=1` 会关掉本地模型；演示需 embeddings 可用（OpenAI / chroma ONNX / local） |
| 空库 / 不可信答案 | 空库拦截提问；`grounded=false` 默认拒答，不是「什么都答」 |
| 检索门控 | 仅 `ready` 文档可检索；入库断向量或图谱一端不得 ready |
| Chroma | HTTP 客户端 + 线程池；依赖 Chroma 服务。无服务时 health 可能 degraded、检索空洞 |
| 上传安全 | UUID 落盘、扩展名/魔数、大小限额已有；ClamAV / 解析 cgroup 默认未强制 |
| 租户 | 图谱与检索带 `tenant_id`；Community Neo4j 只读靠 `READ_ACCESS`，无完整 RBAC |
| 会话 | 默认 SQLite checkpointer，单机持久；多副本需共享盘或关系库 checkpointer |
| CDC | watchdog/Kafka 可演示增量；集群强杀不丢消息仍未验收 |
| 密钥 | `.env.example` 弱口令仅本地；生产必须强密钥且勿提交 |
| 限流 | QA 默认用户 30/分钟、租户 120/分钟，超限 429 |
| 本机 Clash TUN + Docker | 宿主机能调 LLM、bridge 容器超时：dev overlay 已让 `api`/`ingest-worker` 用 `network_mode: host`（依赖走本机映射端口）。生产勿依赖本机 TUN；须保证运行面 egress 可达 LLM |

**本机 TUN 截断 Docker 出网时：** 务必带上 `docker-compose.dev.yml` 再 `up`；仅生产 compose（纯 bridge）在此环境下会卡死 ingest/QA。

相关运维页（非本手册范围）：`09_deployment/tls_and_secret_rotation.md`、`alerting.md`、`backup_restore.md`；单测入口：`07_testing/unit_test_entry.md`。

---

## 4. 验收清单（M8）

- [x] 独立页位于 `project/docs/MVP_demo_guide.md`
- [x] 含 compose / 本机启动步骤与 health 检查
- [x] 含 UI 与 curl 的 upload → ingest 任务 → QA
- [x] 含已知限制与「不可宣称可售」边界
- [ ] 本机 compose 完整走通一次并贴日志 → 记入 ROADMAP **M4**（本页不代替联调证据）
