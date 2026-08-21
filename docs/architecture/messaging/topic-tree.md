# Topic树

## RQ：Comment Campaign

Queue：`browser_v2_comment_campaign`，来源`comment_campaign/queueing.py`。

| Job | RQ函数 | 参数 | Job ID | timeout/result TTL | 幂等/恢复 |
|---|---|---|---|---|---|
| prepare kickoff | `run_prepare_campaign` | `campaign_id` | `campaign-prepare-{id}` | 600/86400秒 | 仅首次kickoff |
| prepare generation | 同上 | `campaign_id` | `campaign-prepare-{id}-g{n}` | 600/86400秒 | SQLite generation + 固定ID |
| submit | `run_submit_assignment` | campaign、assignment、revision | `campaign-submit-{assignment}-r{revision}` | 300/86400秒 | durable approval + revision CAS |
| reconcile | `run_reconcile_campaign` | `campaign_id` | `campaign-reconcile-{id}-g{n}` | 300/86400秒 | 只恢复/prepare，禁止submit replay |

Producer为Campaign Service、Executor和Worker恢复；Consumer为Windows RQ SpawnWorker。job gate使用Redis `SET NX EX`，但执行幂等以SQLite状态、revision、approval、lease为准。

## Celery/Redis Broker

`celery_app.py`及Gateway旧发布接线使用Celery。Selector Probe配置可读取`CELERY_BROKER_URL`作为Redis来源/兼容接线。具体Task注册以当前代码为准；它与RQ不是同一Queue契约。

## SQLite Durable Outbox

| 表 | Producer | Consumer | 用途 |
|---|---|---|---|
| `publication_outbox` | Probe验证/版本事务 | Registry协调器 | 原子发布selector版本 |
| `webhook_outbox` | Alert事务 | Webhook发送器 | 可重试告警通知 |
| `probe_effect_outbox` | Probe终态事务 | Worker effect drain | Gate/告警等副作用 |
| `element_request_outbox` | 管理API | Probe Worker | 单元素probe/validate |
| `management_settings_publications` | 设置修改 | Worker/Gateway协调 | staged设置发布 |

共同模式：业务状态和Outbox同SQLite事务；claim_token/generation/lease领取；失败记录固定错误摘要并按`next_attempt_at`重试。

## 安全约束

消息只携带安全业务ID、revision、generation和白名单业务载荷。禁止raw AdsPower ID、CDP URL、Cookie、Authorization、Redis URL、API key和approval token进入RQ参数或公共Outbox payload。
