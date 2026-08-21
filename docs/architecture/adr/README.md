# 架构决策记录

ADR记录当前代码为何采用某种结构。状态：`Accepted (Retrospective)`表示由现有源码/规格追溯确认；`Legacy`表示仅为兼容保留；`Superseded`表示已被后续ADR替代。历史原因无法证明时写`依据不足`，不得猜测。新决定新增编号，不改写旧ADR结论。

| ADR | 决定 |
|---|---|
| [ADR-0001](ADR-0001-flask-modular-monolith.md) | Flask模块化单体 |
| [ADR-0002](ADR-0002-windows-local-executor.md) | Windows本地执行环境 |
| [ADR-0003](ADR-0003-adspower-cdp-playwright.md) | AdsPower + CDP + Playwright |
| [ADR-0004](ADR-0004-module-local-sqlite.md) | 模块局部SQLite |
| [ADR-0005](ADR-0005-redis-rq-and-existing-celery.md) | Redis/RQ与既有Celery并用 |
| [ADR-0006](ADR-0006-local-direct-and-auth-modes.md) | 本机直开与认证双模式 |
| [ADR-0007](ADR-0007-explicit-element-selection.md) | 人工点选、严格定位 |
| [ADR-0008](ADR-0008-profile-identity-boundary.md) | Profile身份隔离 |
| [ADR-0009](ADR-0009-batched-profile-lifecycle.md) | 分批执行、关闭后续批 |
| [ADR-0010](ADR-0010-browser-control-system-python-evolution.md) | 业务控制系统：Python 技术栈与现有系统演进 |
