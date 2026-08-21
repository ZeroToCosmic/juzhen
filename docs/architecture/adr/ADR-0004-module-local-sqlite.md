# ADR-0004: 模块局部SQLite
## 状态
Accepted (Retrospective)
## 当前事实
Execution V2、Selector Probe、TikTok Stats、Comment Campaign和认证分别使用SQLite；根旧模型可选MySQL。
## 决定
当前文档将各SQLite视为独立事实边界，不假设跨库事务。
## 代码与历史证据
各模块`store.py`/`db.py`、`gateway/management_db.py`、`models.py`。
## 为什么
模块可本机独立初始化，SQLite支持显式事务、WAL和低运维成本。最初逐库选择原因部分`依据不足`。
## 后果
模块恢复和备份清晰；跨模块一致性只能靠ID、Outbox和恢复协调。
## 已知限制
备份分散；多进程写入、迁移和大规模查询能力有限。
## 后续变更条件
只有明确容量/并发/集中备份需求并完成迁移方案后，才统一数据库。
