"""Celery 应用配置。

Windows 本地运行 worker 时请使用 Eventlet 池：

    celery -A celery_app.celery_app worker -P eventlet --concurrency=4 -Q default,interaction --loglevel=INFO

Redis 默认连接本机的 6379 端口；可以通过环境变量覆盖 Broker 和 Backend。
"""

from __future__ import annotations

import os

from celery import Celery
from kombu import Queue


DEFAULT_BROKER_URL = "redis://127.0.0.1:6379/0"
DEFAULT_RESULT_BACKEND = "redis://127.0.0.1:6379/1"
DEFAULT_CONCURRENCY = 4


def _worker_concurrency() -> int:
    value = os.getenv("CELERY_CONCURRENCY", str(DEFAULT_CONCURRENCY))
    try:
        concurrency = int(value)
    except ValueError as exc:
        raise ValueError("CELERY_CONCURRENCY 必须是整数") from exc
    if concurrency < 1:
        raise ValueError("CELERY_CONCURRENCY 必须大于 0")
    return concurrency


celery_app = Celery(
    "browser_automation",
    broker=os.getenv("CELERY_BROKER_URL", DEFAULT_BROKER_URL),
    backend=os.getenv("CELERY_RESULT_BACKEND", DEFAULT_RESULT_BACKEND),
)

celery_app.conf.update(
    # Windows 本地环境默认使用 4 个并发 worker；启动命令显式使用 eventlet。
    worker_concurrency=_worker_concurrency(),
    task_default_queue="default",
    task_queues=(Queue("default"), Queue("interaction")),
    task_routes={
        "tasks.interaction.*": {"queue": "interaction"},
    },
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Shanghai",
    enable_utc=False,
    # 每个 worker 一次只预取一个任务，避免任务集中在少数 worker 手中。
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    broker_connection_retry_on_startup=True,
)


def celery_healthcheck() -> bool:
    """检查 Celery 是否能连接 Redis Broker。"""

    response = celery_app.control.ping(timeout=1.0)
    return bool(response)


__all__ = ["celery_app", "celery_healthcheck"]

