"""
应用配置 — 通过环境变量或 .env 文件加载
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Runtime environment — production / prod enables strong-secret gate
    app_env: str = "development"  # development | production | prod | test
    require_strong_secrets: bool = False

    # LLM
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o"
    embedding_model: str = "text-embedding-3-small"
    # openai | local | chroma | auto
    # openai = OpenAI-compatible /embeddings (must work; chat-only gateways 404)
    # local = text2vec subprocess; chroma = ONNX MiniLM offline
    # auto = deepseek URL → local else openai; probe failure → chroma fallback
    embedding_backend: str = "auto"

    # Neo4j
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "password"
    # P0-5：只读查询账户（空则读写共用 neo4j_user）
    neo4j_read_user: str = ""
    neo4j_read_password: str = ""

    # Postgres (compose / DSN helpers; DSN strings remain source of truth for apps)
    postgres_user: str = "postgres"
    postgres_password: str = "postgres"
    postgres_db: str = "knowledge"

    # Vector Store
    vector_store_type: str = "chroma"  # chroma | pgvector
    chroma_host: str = "localhost"
    chroma_port: int = 8000
    pgvector_dsn: str = "postgresql://postgres:postgres@localhost:5432/knowledge"

    # State / idempotency (P1-3) — Postgres; empty → memory fallback
    state_store_dsn: str = "postgresql://postgres:postgres@localhost:5432/knowledge"

    # Kafka (CDC)
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_topic_doc_changes: str = "doc-changes"
    kafka_topic_kg_updates: str = "kg-updates"
    # 空则默认 {kafka_topic_doc_changes}.dlq
    kafka_cdc_dlq_topic: str = ""

    # Incremental updates: watchdog | kafka | off
    update_mode: str = "watchdog"
    update_watch_directory: str = ""
    # API 上传后抑制 watchdog 的秒数（P1-1 双跑隔离）
    watch_suppress_seconds: float = 45.0

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8080

    # Document Store
    upload_dir: str = "./uploads"
    # P0-0 upload security
    upload_max_bytes: int = 50 * 1024 * 1024  # 50 MiB
    upload_max_pdf_pages: int = 100
    upload_av_scan_enabled: bool = False
    upload_av_scan_required: bool = False

    # Auth / ACL (P1-1 / P0-4)
    auth_enabled: bool = True
    auth_db_path: str = "./data/auth.db"
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440
    auth_bootstrap_admin_username: str = "admin"
    auth_bootstrap_admin_password: str = "admin123"
    auth_bootstrap_tenant_id: str = "default"

    # P0-4 QA multi-turn checkpointer: sqlite (durable) | memory
    qa_checkpoint_backend: str = "sqlite"
    qa_checkpoint_path: str = "./data/qa_checkpoints.sqlite"

    log_level: str = "INFO"

    # Ingest async queue (P1-5)
    # ingest_queue: local | arq | auto（auto=尝试 Redis+arq，失败则本地 asyncio）
    redis_url: str = "redis://localhost:6379/0"
    ingest_queue: str = "local"
    ingest_workers: int = 2
    ingest_async: bool = True  # False 时仍同步执行（兼容/调试）

    # P2 — LLM resilience
    llm_request_timeout: float = 120.0
    llm_max_retries: int = 2
    require_openai_api_key: bool = True

    # P2 / P1-2 — API rate limit (per user; optional per-tenant ceiling)
    rate_limit_qa_per_minute: int = 30
    rate_limit_qa_per_tenant_per_minute: int = 120

    # P2 — 不可信答案强制拒答（grounded=false 时不返回 LLM 正文）
    qa_refuse_ungrounded: bool = True

    # P2 — entity alignment
    entity_similarity_threshold: float = 0.82
    entity_alias_map: str = ""  # JSON object {"腾讯公司":"腾讯"} or empty

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
