# 项目架构与交接文档中心

## 文档基准

- 提取日期：2026-08-08。
- Git 基线：`7812fcb`。
- 记录对象：当前工作区，包含尚未提交的 `execution_v2/`、`comment_campaign/` 及相关管理界面。
- 系统形态：Windows 本机运行的 Flask 模块化单体，配套多个 Python Worker，通过 AdsPower Local API 和 CDP 执行浏览器动作。
- 本文档只描述当前事实；未实现能力单独标记，不代表迁移决定。

## 安全边界

文档不记录 API Key、Cookie、Authorization、Redis 密码、CDP WebSocket、AdsPower 原始 Profile ID 或真实评论内容。Profile 对外只使用脱敏名称、`profile_ref` 或进程内 token。

## 证据等级

- `源码确认`：当前源码直接体现。
- `测试确认`：存在自动测试覆盖。
- `配置确认`：来自默认配置、环境变量读取或示例配置。
- `历史设计`：来自 `docs/superpowers/specs/`。
- `运行时未验证`：接线存在，但本次未启动真实服务。
- `依据不足`：无法从当前仓库确认历史原因。

## 当前系统一句话说明

系统在一台 Windows 电脑上通过桌面启动器拉起 Flask、统计 Worker、Selector Probe Worker 和 Comment Campaign RQ Worker；管理后台维护账号、内容、元素、策略和 Campaign，执行模块调用 AdsPower 打开独立浏览器 Profile，再由 Playwright 通过 CDP 定位元素并执行动作。

## 按角色阅读

- 新开发人员：[系统上下文](system/context.md) → [本机环境](operations/local-setup.md) → [模块地图](system/module-map.md) → [开发入门](development/getting-started.md)。
- 前端人员：[当前前端](frontend/current-frontend.md) → [页面清单](frontend/page-inventory.md) → [UI规范](frontend/ui-conventions.md) → [HTTP契约](api/README.md)。
- 后端人员：[Flask应用](backend/flask-application.md) → [服务边界](backend/service-boundaries.md) → [OpenAPI](api/openapi.yaml) → [表结构](data/database-schema.md)。
- 执行器人员：[执行器总览](executor/overview.md) → [AdsPower](executor/adspower-adapter.md) → [CDP](executor/cdp-session-lifecycle.md) → [安全边界](executor/safety-boundaries.md)。
- 运维人员：[启动器与进程](operations/launcher-and-processes.md) → [环境变量](operations/environment-variables.md) → [健康检查](operations/health-checks.md) → [故障排查](operations/troubleshooting.md)。

## 文档地图

- 系统：[上下文](system/context.md)、[运行拓扑](system/runtime-topology.md)、[模块地图](system/module-map.md)、[数据流](system/data-flow.md)、[技术栈](system/technology-stack.md)。
- 模块：[Launcher](modules/launcher.md)、[Gateway](modules/gateway.md)、[认证](modules/authentication.md)、[设置](modules/settings.md)、[账号与代理](modules/accounts-and-proxies.md)、[内容与发布](modules/content-and-publishing.md)、[Legacy Browser](modules/legacy-browser-strategy.md)、[Execution V2](modules/execution-v2.md)、[Selector Probe](modules/selector-probe.md)、[TikTok Stats](modules/tiktok-stats.md)、[Comment Campaign](modules/comment-campaign.md)、[业务控制系统 BCS](modules/business-control-system.md)。
- 决策：[ADR索引](adr/README.md)。
- HTTP：[API入口](api/README.md)、[OpenAPI](api/openapi.yaml)、[路由清单](api/route-inventory.md)、[错误码](api/error-codes.md)。
- 消息：[Topic树](messaging/topic-tree.md)、[Redis Keyspace](messaging/redis-keyspace.md)、[消息Schema](messaging/README.md)。
- 数据：[存储地图](data/storage-map.md)、[表结构](data/database-schema.md)、[关系](data/entity-relationships.md)、[状态机](data/README.md)。
- 角色指南：[前端](frontend/current-frontend.md)、[后端](backend/flask-application.md)、[执行器](executor/overview.md)。
- 运维：[本机环境](operations/local-setup.md)、[日志与Evidence](operations/logs-and-evidence.md)、[备份恢复](operations/backup-and-restore.md)。
- 开发：[代码规范](development/coding-style.md)、[提交](development/commits.md)、[PR](development/pull-requests.md)、[并行开发](development/parallel-development.md)。
- 差距：[未实现能力](gaps/README.md)。
- 验证：[文档验证报告](VERIFICATION.md)。

## 未实现能力

当前没有 pnpm workspace、实体共享组件库、OpenAPI 生成的 TypeScript 客户端、独立中控微服务、根级 Docker Compose 或 CI/CD。对应影响将在 `gaps/` 中记录。

## 维护规则

修改路由、表结构、消息、状态机、环境变量、页面入口或进程拓扑时，必须同步更新对应文档。改变架构决定时新增 ADR；不得通过改写历史记录掩盖旧决定。

## 版本与维护责任

- 文档版本：`2026-08-08-working-tree`。
- 适用模式：Windows本机直开或认证模式。
- 已验证：源码清单、路由、表名、机器文件语法、内部链接、编码和敏感信息模式。
- 未验证：真实AdsPower、TikTok、Buffer、R2、Redis/MySQL运行健康和真实提交。
- 维护责任：项目维护者及修改对应模块的开发人员。
- 最后复核：2026-08-08。
