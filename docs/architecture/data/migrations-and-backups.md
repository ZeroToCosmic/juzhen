# 迁移与备份

## 当前迁移机制

- Legacy accounts：`init_db._ensure_columns`按列补丁迁移。
- TikTok Stats：`schema_migrations`和`LATEST_SCHEMA_VERSION`。
- Selector Probe：初始化DDL加专用重建/迁移函数与`selector_storage_migrations`。
- Execution V2：Store初始化DDL；当前无独立迁移工具。
- Comment Campaign：SQLAlchemy `create_all`和Store初始化兼容逻辑；当前无Alembic。
- MySQL根模型：启动器可创建数据库/检查InnoDB；当前无Alembic。

## 备份顺序

1. 停止Flask和三个Worker，确认没有浏览器执行。
2. 备份`config.json`及有效备份文件，但单独保护凭据。
3. 对每个SQLite执行一致性检查后复制，不能只复制一个库。
4. MySQL使用数据库级dump。
5. 按保留策略复制Evidence/日志；它们不替代数据库状态。
6. 记录Git提交、工作区状态、Python/Node/AdsPower版本。

## 恢复原则

先恢复数据库和配置，再启动Redis/外部服务，最后启动Worker和Flask。Redis不作为历史事实备份；Worker reconcile依据SQLite恢复。禁止将旧数据库与不兼容的新代码直接混用。

## 风险

多库之间没有一致快照；备份时服务仍写入会造成跨库时间点不一致。真实生产恢复必须先在副本验证。
