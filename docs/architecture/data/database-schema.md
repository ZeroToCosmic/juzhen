# 表结构目录

## Legacy accounts SQLite

| 表 | 关键列/约束 | 读写者 | 来源 |
|---|---|---|---|
| `accounts` | integer PK；`ads_power_user_id`、`buffer_account_id`、`proxy_session`；status active/banned；Buffer token/channels JSON文本 | AccountStore | `init_db.py` |

`buffer_token`敏感，公共投影只能返回掩码。`_ensure_columns`通过`PRAGMA table_info`追加旧库缺失列。

## 可选MySQL ORM

| 表 | 关键列/约束 | 来源 |
|---|---|---|
| `accounts` | UUID PK；`account_id` unique/index；`ads_power_id` unique/index；tier S/A/B/C；status IDLE/RUNNING/BANNED/COOLDOWN；daily_actions JSON；updated_at | `models.py::Account` |

该表与Legacy SQLite同名但Schema不同，不能混用。

## Management SQLite

| 表 | 关键列/约束 | 来源 |
|---|---|---|
| `management_users` | integer PK；username unique；password_hash；role；active；session_version；失败/锁定/时间戳 | `gateway/management_db.py` |
| `management_audit_events` | actor、action、target、details JSON、created_at；created倒序索引 | 同上 |

## Execution V2 SQLite

| 表 | 关键列/关系 | 用途 |
|---|---|---|
| `elements` | element_id PK、current_revision、status、timestamps | 元素逻辑身份 |
| `element_revisions` | element_id+revision、payload JSON | 不可变点选定义 |
| `strategies` | strategy_id PK、revision、payload | 策略头 |
| `strategy_actions` | strategy_id、position、action JSON | 有序积木动作 |
| `execution_jobs` | job_id、strategy、status、cancel、timestamps | 批次任务 |
| `execution_profiles` | job+profile token、status/stage/error | 每Profile结果 |
| `action_results` | job/profile/action/position/result | 每动作结果 |
| `wheel_calibrations` | profile/page样本、revision、结果 | 校准历史 |
| `wheel_calibration_current` | scope→current calibration | 当前校准指针 |

来源：`execution_v2/store.py`。元素和策略写入使用revision；被策略引用的元素不能直接删除。

## Selector Probe SQLite

| 表 | 关键字段/约束 | 用途 |
|---|---|---|
| `probe_runs` | trigger/status/timestamps/details | 一次探针运行 |
| `element_probe_contracts` | run/alias/contract JSON | 当次元素契约 |
| `selector_validation_runs` | run/profile_mask/round/status/evidence | 多Profile多轮验证 |
| `selector_versions` | version/hash/status/LKG/published | 选择器版本 |
| `publication_outbox` | aggregate/payload/status/claim/lease/retry | Redis发布Outbox |
| `strategy_dependencies` | alias+strategy+action复合PK | 元素到策略依赖 |
| `strategy_gate_reasons` | open reason部分唯一索引 | 暂停原因 |
| `strategy_gate_revisions` | strategy PK/revision | Gate CAS |
| `probe_alerts` | fingerprint、open/ack/resolved、revision | 告警聚合 |
| `probe_alert_screenshots` | alert FK/路径 | 告警现场 |
| `webhook_outbox` | alert FK/status/claim/retry | Webhook可靠投递 |
| `probe_health_state` | site+environment复合PK | 连续失败/重试 |
| `probe_effect_outbox` | effect_key unique | 终态副作用 |
| `managed_elements` | element定义、published/draft/health | 动态元素目录 |
| `element_drafts` | element/revision/draft payload | 待验证草稿 |
| `element_catalog_state` | singleton/revision | 目录全局CAS |
| `selector_management_audit_events` | actor/operation/resource/details | 管理审计 |
| `management_resource_revisions` | resource/revision | 管理资源CAS |
| `management_idempotency_cache` | actor/operation/key unique、expiry | HTTP幂等 |
| `management_settings_publications` | staged revision/status | 设置发布协调 |
| `management_run_requests` | id、actor、trigger、run FK、status | 手动/定时请求 |
| `management_preflight_health` | workspace PK/result/checked | 预检缓存 |
| `element_request_outbox` | request PK/type/element/revision/claim/result | 单元素请求 |
| `selector_storage_migrations` | migration identity/timestamp | 存储迁移记录 |

来源：`selector_probe/store.py`。多数副作用先写Outbox，再由Worker领取；claim token、generation和lease防止并发重复。

## TikTok Stats SQLite

| 表 | 关键列/关系 | 用途 |
|---|---|---|
| `tracked_accounts` | username unique、status、设置 | 跟踪账号 |
| `collection_runs` | mode/status/timestamps/details | 采集运行 |
| `account_snapshots` | account FK/run FK、统计快照 | 账号历史 |
| `posts_current` | account+video identity、帖子指标 | 当前帖子 |
| `daily_account_metrics` | account+date unique、delta/baseline | 每日指标 |
| `worker_leases` | lease_name PK、owner、expires | Worker互斥 |
| `schema_migrations` | version PK/applied_at | 迁移版本 |

来源：`tiktok_stats/db.py`，当前`LATEST_SCHEMA_VERSION = 2`。

## Comment Campaign SQLite/SQLAlchemy

| 表 | 关键列/关系 | 用途 |
|---|---|---|
| `comment_templates` | `id` PK、`revision`、`enabled`、可空`deleted_at` | 模板身份与当前生命周期 |
| `comment_template_revisions` | template+revision unique、modes/snapshot | 不可变模板版本 |
| `comment_steps` | step_id、template revision FK、parent_step_id、position、content source | 对话树 |
| `comment_campaigns` | campaign PK、template revision、mode、video、profiles JSON、status/revision、snapshots/generation | Campaign聚合根 |
| `comment_assignments` | assignment PK、campaign FK、step、profile_ref、role、parent_assignment FK、status/revision/evidence | 每账号步骤 |
| `comment_approvals` | assignment+revision unique、private token、consumed_at | 一次性批准 |
| `comment_receipts` | assignment FK、status、text hash、平台/父scope/evidence | 发布回执 |
| `comment_attempts` | campaign/assignment、phase/result/error、details | 审计尝试 |
| `comment_profile_identities` | profile_ref PK、raw ID私有映射、display | 持久opaque身份 |
| `comment_profile_metadata` | profile_ref FK、tags/language/region/health/cooldown | 分配资格 |

关键约束：同Campaign的profile_ref唯一；线程parent指向同CampaignAssignment；冻结内容和分配在plan事务写入；批准消费、begin_submitting、Receipt终态和后代暂停使用CAS事务。来源：`comment_campaign/models.py`、`store.py`。

评论树生命周期约束：新建数据库使用命名CHECK `ck_comment_template_deleted_disabled`，表达式为`deleted_at IS NULL OR enabled = 0`，因此任何已删除记录都必须同时处于未启用状态。`enabled=true, deleted_at=NULL`为`enabled`；`enabled=false, deleted_at=NULL`为`disabled`；`enabled=false, deleted_at非空`为`deleted`。模板revision快照同步保存`lifecycle_status`；旧快照缺字段时按`enabled`兼容推导。

旧SQLite迁移采用幂等列检测：Store初始化先通过`PRAGMA table_info(comment_templates)`只在缺列时执行`ALTER TABLE comment_templates ADD COLUMN deleted_at VARCHAR(40) CHECK (deleted_at IS NULL OR enabled = 0)`。SQLite不能通过`ALTER TABLE ... ADD COLUMN`补表级命名CHECK，因此旧库使用列级等价CHECK，新建库使用上述命名约束；两者约束语义一致。迁移不删除、重写或自动恢复现有模板、revision和steps。

Profile身份预检还包含两项独立、可重复执行的SQLite增量迁移：初始化分别以`PRAGMA table_info(comment_campaigns)`与`PRAGMA table_info(comment_assignments)`检测`identity_generation`，仅缺列时各执行一次`ALTER TABLE ... ADD COLUMN identity_generation INTEGER NOT NULL DEFAULT 0`。默认值0表示尚未完成全量账号预检；预检成功后Campaign和对应Assignment在同一代次被冻结。迁移不回填观察结果、不改写Receipt/Attempt，也不移动raw AdsPower私有映射。

软删除只隐藏普通模板列表与当前详情，`comment_template_revisions`和`comment_steps`继续保留。只有`comment_campaigns.locked_at`非空时，执行路径可祖父化使用`template_snapshot_json`、`content_snapshot_json`及已有Assignments；未锁定Campaign不得绕过当前模板可用性检查，也不得原地替换`template_id`或`template_revision`。

## JSON字段原则

JSON列必须在模块Schema/Service中验证，不因为数据库类型为TEXT/JSON就允许任意字段。公开API投影必须递归脱敏。
