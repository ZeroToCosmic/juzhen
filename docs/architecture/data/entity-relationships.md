# 实体关系

## Execution V2
```mermaid
erDiagram
  ELEMENTS ||--o{ ELEMENT_REVISIONS : has
  STRATEGIES ||--o{ STRATEGY_ACTIONS : orders
  EXECUTION_JOBS ||--o{ EXECUTION_PROFILES : runs
  EXECUTION_PROFILES ||--o{ ACTION_RESULTS : records
```

## Selector Probe
```mermaid
erDiagram
  PROBE_RUNS ||--o{ ELEMENT_PROBE_CONTRACTS : freezes
  PROBE_RUNS ||--o{ SELECTOR_VALIDATION_RUNS : validates
  SELECTOR_VERSIONS ||--o{ PUBLICATION_OUTBOX : publishes
  PROBE_ALERTS ||--o| PROBE_ALERT_SCREENSHOTS : captures
  PROBE_ALERTS ||--o{ WEBHOOK_OUTBOX : delivers
  MANAGED_ELEMENTS ||--o{ ELEMENT_DRAFTS : proposes
```

部分关系通过业务ID/JSON维护而非数据库FK，详见表目录。

## TikTok Stats
```mermaid
erDiagram
  TRACKED_ACCOUNTS ||--o{ ACCOUNT_SNAPSHOTS : has
  COLLECTION_RUNS ||--o{ ACCOUNT_SNAPSHOTS : produces
  TRACKED_ACCOUNTS ||--o{ POSTS_CURRENT : owns
  TRACKED_ACCOUNTS ||--o{ DAILY_ACCOUNT_METRICS : aggregates
```

## Comment Campaign
```mermaid
erDiagram
  COMMENT_TEMPLATES ||--o{ COMMENT_TEMPLATE_REVISIONS : versions
  COMMENT_TEMPLATE_REVISIONS ||--o{ COMMENT_STEPS : contains
  COMMENT_CAMPAIGNS ||--o{ COMMENT_ASSIGNMENTS : allocates
  COMMENT_ASSIGNMENTS ||--o{ COMMENT_APPROVALS : approves
  COMMENT_ASSIGNMENTS ||--o{ COMMENT_RECEIPTS : proves
  COMMENT_ASSIGNMENTS ||--o{ COMMENT_ATTEMPTS : audits
  COMMENT_ASSIGNMENTS o|--o{ COMMENT_ASSIGNMENTS : parent
  COMMENT_PROFILE_IDENTITIES ||--o| COMMENT_PROFILE_METADATA : describes
```

Campaign通过`template_id + template_revision`锁定版本；Assignment通过`parent_assignment_id`形成实际执行树。

## 同名Account说明

Legacy SQLite `accounts`与可选MySQL ORM `accounts`不是同一Schema或自动同步关系；任何迁移都必须显式映射。
