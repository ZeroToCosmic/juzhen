# ADR-0010: 业务控制系统——Python 技术栈与现有系统演进

## 状态
Accepted

## 当前事实
现有系统为 Windows 单机 Flask 模块化单体，已实现 Browser Execution V2（策略契约执行）、Comment Campaign（DAG 依赖/人工批准/回执验证/不确定提交不重试）、Selector Probe（PROBE 门禁）、SQLite Outbox、Redis 租约与 RQ 队列。业务控制系统 PRD（`docs/PRD-业务控制系统.v2.md`）要求升级为多机任务下发、执行监控、结果汇总，支持多租户。

## 决定
1. **全链路 Python**：中控（FastAPI + SQLAlchemy 2 + APScheduler）与 Web 使用 Python；不引入 Go。Agent 保持 Python。
2. **演进而非重写**：Web = 现有 Flask 管理后台演进；Agent = 现有 execution_v2 / comment_campaign / selector_probe 改造；中控 = 新 Python 服务，独立进程，由 launcher 监督。
3. **数据库**：中控数据 v1 以 SQLite 起步（单实例），引入租约回收器/多实例时切换 PostgreSQL；现有 5 个模块 SQLite 库按第 14 章分期迁移，batch_id 幂等。
4. **消息骨干**：v1 沿用 Redis/RQ（现有已验证），M2 引入 NATS JetStream，统一 Outbox/Inbox 模式（从现有 SQLite Outbox 抽象通用库），过渡期并存，协议适配层预留。
5. **多租户**：v1 全链路预留 tenant_id 字段与 NATS Subject 前缀，数据访问层过滤钩子就位；业务激活在 M4。
6. **前端**：沿用 Jinja + 原生 JS + 现有共享壳与 node:test 体系，不引入框架。

## 代码与历史证据
- `execution_v2/`、`comment_campaign/`、`selector_probe/` 已实现 PRD F20/F21/F14 的参考实现（receipts、approvals、revision CAS、lease、Outbox）。
- 符号契约测试 `tests/test_gateway_app_contract.py` 与全量 pytest/node:test 体系保证演进期回归。
- `launcher.py` 已有 4 个 Supervisor 进程监督模式，可扩展中控/回收器进程。
- `scripts/backup_all.py` 为迁移期数据保障前置。

## 为什么
- 现有约 13k 行 gateway + 各模块业务代码与 600+ 测试均为 Python，双栈维护成本不可接受。
- 复用现有已验证的一致性设计（人工门禁、UNVERIFIED 不重提、Profile 排他锁），避免重写引入同类事故。
- 中控纯 Python 无 pywin32 依赖（与 Agent 隔离），可平滑迁 Linux/Docker，无需重写。

## 后果
- 中控与 Agent 共享 Python 生态（nats-py、SQLAlchemy），降低学习成本。
- 演进期需要维护"现有单机路径"与"新任务模型路径"并存，必须保持 URL 与契约兼容。
- RQ→NATS 过渡期存在双骨干，消息语义分裂风险由统一 Outbox/Inbox 模式与适配层控制。

## 已知限制
- SQLite 不支持 `SELECT ... FOR UPDATE SKIP LOCKED` 与行级咨询锁，多实例并发控制必须等 PG 引入。
- 现有模块部分状态机（Campaign/Probe）需映射到新统一迁移表，映射期不得旁路写入。
- 多租户为全链路改造，M4 前仅字段与过滤钩子，不承诺数据完全隔离。

## 后续变更条件
- 中控引入回收器/多实例 → 必须切换 PostgreSQL 并落地 SKIP LOCKED / 唯一约束 / 咨询锁。
- 引入 NATS → 所有新消息路径禁止再直连 Redis 队列；旧路径限期收敛。
- 租户激活 → 数据访问层强制过滤 + Subject ACL + payload/subject 二次校验全部就位后才允许多租户写入。
