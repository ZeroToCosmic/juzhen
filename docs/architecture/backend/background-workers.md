# 后台Worker

| Worker | 入口 | 调度 | 心跳/租约 | 恢复 |
|---|---|---|---|---|
| Selector Probe | `selector_probe.worker::serve` | 30秒tick/03:00/请求 | Redis/Probe租约 | retry/outbox/reconcile |
| TikTok Stats | `tiktok_stats.worker::main` | 上海时区槽 | SQLite lease | 过期槽和保留清理 |
| Comment Campaign | `comment_campaign.worker::serve` | RQ SpawnWorker | owner-token Redis TTL | uncertain Receipt、consumed approval、prepare generation |
| Legacy publish | Celery/Gateway接线 | queue/schedule | 依实现 | 结果状态/人工处理 |

Worker启动失败不能让Flask页面伪装服务正常。Health必须实查SQLite、Redis、Worker TTL和AdsPower轻探针。
