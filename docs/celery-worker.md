# Windows 本地 Celery 并发 worker

项目使用本机 Redis 作为 Celery Broker 和 Backend：

```text
Broker:  redis://127.0.0.1:6379/0
Backend: redis://127.0.0.1:6379/1
并发数: 4
队列: default, interaction
```

先启动 Redis，再在项目根目录启动 worker：

```powershell
.\.venv\Scripts\python.exe -m celery -A celery_app.celery_app worker `
  -P eventlet --concurrency=4 -Q default,interaction --loglevel=INFO
```

Windows 本地环境请保留 `-P eventlet`。不使用 Eventlet 时，Celery 的默认进程池在 Windows 上不能提供预期的并发窗口管理能力。

可选环境变量：

```env
CELERY_BROKER_URL=redis://127.0.0.1:6379/0
CELERY_RESULT_BACKEND=redis://127.0.0.1:6379/1
CELERY_CONCURRENCY=4
```

普通任务发送到 `default` 队列；浏览器交互任务使用 `interaction` 队列。发送交互任务时指定：

```python
some_task.apply_async(args=[...], queue="interaction")
```
