"""入库任务队列：优先 Redis+arq，不可用时降级为本进程 asyncio 队列。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from config import settings

logger = logging.getLogger(__name__)

RunJobFn = Callable[[str], Awaitable[dict[str, Any]]]


class IngestQueue:
    backend: str = "local"

    async def start(self) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError

    async def enqueue(self, job_id: str) -> None:
        raise NotImplementedError

    def stats(self) -> dict[str, Any]:
        return {"backend": self.backend, "worker_visible": False}

    async def astats(self) -> dict[str, Any]:
        return self.stats()


class LocalIngestQueue(IngestQueue):
    """进程内异步队列（默认可用，不依赖 Redis）。"""

    backend = "local"

    def __init__(self, run_job: RunJobFn, concurrency: int = 2) -> None:
        self._run_job = run_job
        self._concurrency = max(1, concurrency)
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._stopped = asyncio.Event()

    async def start(self) -> None:
        self._stopped.clear()
        for i in range(self._concurrency):
            self._workers.append(asyncio.create_task(self._loop(i), name=f"ingest-worker-{i}"))
        logger.info("Local ingest queue started concurrency=%s", self._concurrency)

    async def stop(self) -> None:
        self._stopped.set()
        for _ in self._workers:
            await self._queue.put("")  # wake
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        logger.info("Local ingest queue stopped")

    async def enqueue(self, job_id: str) -> None:
        await self._queue.put(job_id)

    def stats(self) -> dict[str, Any]:
        alive = sum(1 for t in self._workers if not t.done())
        return {
            "backend": self.backend,
            "queue_depth": self._queue.qsize(),
            "workers_configured": self._concurrency,
            "workers_alive": alive,
            "worker_visible": alive > 0,
        }

    async def _loop(self, worker_id: int) -> None:
        while not self._stopped.is_set():
            job_id = await self._queue.get()
            try:
                if not job_id or self._stopped.is_set():
                    continue
                await self._run_job(job_id)
            except Exception:
                logger.exception("local ingest worker=%s failed job_id=%s", worker_id, job_id)
            finally:
                self._queue.task_done()


class ArqIngestQueue(IngestQueue):
    """Redis + arq 分布式队列。"""

    backend = "arq"
    HEARTBEAT_KEY = "agenthub:ingest_worker:heartbeat"

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._redis: Any = None

    async def start(self) -> None:
        from arq import create_pool
        from arq.connections import RedisSettings

        self._redis = await create_pool(RedisSettings.from_dsn(self._redis_url))
        logger.info("Arq ingest queue connected (%s)", self._redis_url.split("@")[-1])

    async def stop(self) -> None:
        if self._redis is not None:
            await self._redis.close(close_connection_pool=True)
            self._redis = None

    async def enqueue(self, job_id: str) -> None:
        if self._redis is None:
            raise RuntimeError("Arq queue not started")
        await self._redis.enqueue_job("arq_process_ingest_job", job_id)

    async def astats(self) -> dict[str, Any]:
        """队列深度 + worker 心跳（worker 启动时写入 HEARTBEAT_KEY）。"""
        import time

        out: dict[str, Any] = {
            "backend": self.backend,
            "queue_depth": None,
            "worker_visible": False,
            "worker_heartbeat_age_s": None,
        }
        if self._redis is None:
            return out
        try:
            # arq default queue key
            depth = await self._redis.zcard("arq:queue")
            out["queue_depth"] = int(depth or 0)
        except Exception as exc:
            logger.warning("arq queue depth probe failed: %s", exc)
        try:
            raw = await self._redis.get(self.HEARTBEAT_KEY)
            if raw is not None:
                ts = float(raw)
                age = max(0.0, time.time() - ts)
                out["worker_heartbeat_age_s"] = round(age, 1)
                out["worker_visible"] = age < 120.0
        except Exception as exc:
            logger.warning("arq worker heartbeat probe failed: %s", exc)
        return out


async def create_ingest_queue(run_job: RunJobFn) -> IngestQueue:
    """按 INGEST_QUEUE 选择后端；auto 时 Redis 不可用则降级本地队列。"""
    mode = (settings.ingest_queue or "local").strip().lower()
    redis_url = (settings.redis_url or "").strip()

    if mode == "local" or not redis_url and mode != "arq":
        queue: IngestQueue = LocalIngestQueue(run_job, concurrency=settings.ingest_workers)
        await queue.start()
        return queue

    if mode in {"arq", "auto"}:
        try:
            queue = ArqIngestQueue(redis_url or "redis://localhost:6379/0")
            await queue.start()
            return queue
        except Exception:
            if mode == "arq":
                raise
            logger.exception("Failed to start arq/Redis queue; falling back to local asyncio queue")

    queue = LocalIngestQueue(run_job, concurrency=settings.ingest_workers)
    await queue.start()
    return queue
