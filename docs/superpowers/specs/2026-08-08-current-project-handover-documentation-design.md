# 当前项目交接文档中心设计

## 1. 目的

为当前工作区建立一套可交接、可检索、可持续维护的架构文档中心，使新开发人员无需先通读全部源码，也能回答以下问题：

- 系统有哪些进程、模块和数据存储。
- 每个模块使用什么技术栈，入口在哪里，依赖什么，被谁调用。
- HTTP 接口、错误码、队列、Redis Key、Outbox 和消息载荷是什么。
- 数据表、关系、事务边界和状态机是什么。
- 管理后台、Web 后端、逻辑中控和 Windows 执行器如何协作。
- 如何启动、测试、排错、备份和安全交接。
- 后续新增功能必须遵守哪些 UI、接口、代码、Git、PR 和文档规则。

本设计不重构业务代码，不改变运行行为，不把目标架构描述成当前实现。

## 2. 文档基准

- 提取日期：2026-08-08。
- Git 基线：`7812fcb`。
- 文档对象：当前工作区，包括尚未提交的 `execution_v2`、`comment_campaign`、Selector Probe 调整及其管理界面。
- Git `main` 只作为历史基线，不代表完整现状。
- 文档语言：中文；代码符号、路径、协议字段和错误码保留英文原文。
- 文档编码：UTF-8。

每条重要结论应区分证据等级：

- `源码确认`：来自当前实现。
- `测试确认`：有对应自动测试。
- `配置确认`：来自默认配置或环境读取逻辑。
- `历史设计`：来自现有规格文档。
- `运行时未验证`：只确认接线，未启动真实服务。
- `依据不足`：无法确认历史原因，不进行猜测。

## 3. 非目标与安全边界

本次文档工作不执行以下动作：

- 不重构 `gateway/app.py` 或拆分服务。
- 不建立 pnpm workspace、共享组件包或 TypeScript 客户端。
- 不增加根级 Docker Compose 或 CI/CD。
- 不迁移数据库，不修改本机配置，不启动生产任务。
- 不启动真实 AdsPower Profile。
- 不进行真实 TikTok 发布、评论或其他平台副作用。
- 不记录 API Key、Cookie、Authorization、Redis 密码、CDP WebSocket 或 AdsPower 原始 Profile ID。
- 不提交当前工作区中与文档无关的已有修改。

## 4. 当前系统事实基线

### 4.1 运行拓扑

```text
Windows 桌面启动器
├─ Flask Web 后端
├─ Selector Probe Worker
├─ TikTok Stats Worker
├─ Comment Campaign RQ Worker
├─ Redis
├─ 可选 MySQL
└─ TikTok API Docker 服务

Flask Web 后端
├─ 管理后台页面
├─ 账号、代理、内容与发布
├─ 旧版浏览器执行策略
├─ Browser Execution V2
├─ Selector Probe
├─ TikTok Stats
└─ Comment Campaign

电脑端执行链
AdsPower Local API
  → 启动 Profile
  → 获取 CDP WebSocket
  → Playwright 接管页面
  → 定位元素
  → ghost-cursor／键盘／滚轮动作
  → 截图与结果
  → 确认关闭 Profile
```

### 4.2 技术栈

| 模块 | 当前技术栈 | 主要入口 |
|---|---|---|
| 桌面启动器 | Python、Tkinter、Windows Process API | `launcher.py` |
| Web 后端 | Python、Flask 3.1.1、Jinja | `app.py`、`gateway/app.py` |
| 当前前端 | Flask Template、原生 JavaScript、CSS、npm | `gateway/templates/`、`gateway/static/` |
| 旧版浏览器执行 | Python asyncio、Node.js、Playwright、CDP | 根目录 `browser_*`、`actions_dom.py`、`browser/` |
| Browser V2 | Python、Playwright、SQLite | `execution_v2/` |
| Selector Probe | Python、SQLite、Redis、独立 Worker | `selector_probe/` |
| TikTok Stats | Python、SQLite、独立 Worker、外部 TikTok API | `tiktok_stats/` |
| Comment Campaign | Flask、Pydantic 2、SQLAlchemy 2、SQLite、Redis、RQ 2 | `comment_campaign/` |
| AdsPower 接入 | `requests`、AdsPower Local API、CDP | `adspower.py` |
| 拟人化动作 | `ghost-cursor@1.4.2`、Python Bridge | `ghost_cursor_bridge.py`、`browser/ghost-cursor-worker.js` |
| 内容与发布 | Python、本地文件、Buffer API、R2 | `gateway/content_*`、`gateway/buffer_*`、`gateway/r2_client.py` |
| 账号与代理 | Python、SQLite、HTTP/SOCKS | `gateway/account_store.py`、`gateway/proxy*.py` |
| 认证 | Flask Session、Werkzeug Password Hash、CSRF、角色权限 | `gateway/auth_*` |
| 可选主数据库 | SQLAlchemy、asyncmy、MySQL 8 / InnoDB | `database.py`、`models.py` |
| 测试 | pytest、Node `node:test` | `tests/`、`tests-js/` |
| 局部容器 | Docker Compose | `services/tiktok_api/docker-compose.yml` |

### 4.3 必须公开的现状差距

- 当前没有 pnpm workspace，使用单个 npm 项目和 `package-lock.json`。
- 当前没有 React/Vue 管理后台，使用 Jinja、原生 JavaScript 和 CSS。
- 当前没有实体共享业务组件包。
- 当前没有统一 OpenAPI 源文件。
- 当前没有自动生成的 TypeScript 客户端。
- 当前没有正式 Topic Bus；Redis Key、RQ Queue、Celery Broker 和 SQLite Outbox 并存。
- 当前没有根级 `docker-compose.yml`。
- 当前没有 `.github/workflows` CI/CD。
- 数据分散在 SQLite、可选 MySQL、Redis、JSON 配置和本地文件。
- `gateway/app.py` 是主要集成入口，规模约 7,953 行。
- 多个新增业务包和测试尚未进入当前 Git 提交。

## 5. 文档中心结构

```text
docs/architecture/
├─ README.md
├─ system/
│  ├─ context.md
│  ├─ runtime-topology.md
│  ├─ module-map.md
│  ├─ data-flow.md
│  └─ technology-stack.md
├─ modules/
│  ├─ launcher.md
│  ├─ gateway.md
│  ├─ authentication.md
│  ├─ settings.md
│  ├─ accounts-and-proxies.md
│  ├─ content-and-publishing.md
│  ├─ legacy-browser-strategy.md
│  ├─ execution-v2.md
│  ├─ selector-probe.md
│  ├─ tiktok-stats.md
│  └─ comment-campaign.md
├─ adr/
│  ├─ README.md
│  ├─ ADR-0001-flask-modular-monolith.md
│  ├─ ADR-0002-windows-local-executor.md
│  ├─ ADR-0003-adspower-cdp-playwright.md
│  ├─ ADR-0004-module-local-sqlite.md
│  ├─ ADR-0005-redis-rq-and-existing-celery.md
│  ├─ ADR-0006-local-direct-and-auth-modes.md
│  ├─ ADR-0007-explicit-element-selection.md
│  ├─ ADR-0008-profile-identity-boundary.md
│  └─ ADR-0009-batched-profile-lifecycle.md
├─ api/
│  ├─ README.md
│  ├─ openapi.yaml
│  ├─ route-inventory.md
│  ├─ authentication.md
│  ├─ conventions.md
│  └─ error-codes.md
├─ messaging/
│  ├─ README.md
│  ├─ topic-tree.md
│  ├─ redis-keyspace.md
│  └─ schemas/
│     ├─ comment-campaign-prepare.schema.json
│     ├─ comment-campaign-submit.schema.json
│     ├─ selector-probe-run.schema.json
│     ├─ selector-publication-outbox.schema.json
│     └─ webhook-outbox.schema.json
├─ data/
│  ├─ README.md
│  ├─ storage-map.md
│  ├─ database-schema.md
│  ├─ entity-relationships.md
│  ├─ migrations-and-backups.md
│  └─ state-machines/
│     ├─ execution-v2.md
│     ├─ selector-probe.md
│     ├─ comment-campaign.md
│     ├─ tiktok-stats.md
│     └─ publishing.md
├─ frontend/
│  ├─ current-frontend.md
│  ├─ page-inventory.md
│  ├─ navigation.md
│  ├─ ui-conventions.md
│  └─ frontend-gap-analysis.md
├─ backend/
│  ├─ flask-application.md
│  ├─ service-boundaries.md
│  ├─ dependency-direction.md
│  ├─ background-workers.md
│  └─ error-handling.md
├─ executor/
│  ├─ overview.md
│  ├─ adspower-adapter.md
│  ├─ cdp-session-lifecycle.md
│  ├─ element-location.md
│  ├─ humanized-actions.md
│  ├─ batching-and-window-tiling.md
│  └─ safety-boundaries.md
├─ operations/
│  ├─ local-setup.md
│  ├─ launcher-and-processes.md
│  ├─ environment-variables.md
│  ├─ docker.md
│  ├─ logs-and-evidence.md
│  ├─ backup-and-restore.md
│  ├─ health-checks.md
│  └─ troubleshooting.md
├─ development/
│  ├─ getting-started.md
│  ├─ repository-layout.md
│  ├─ coding-style.md
│  ├─ commits.md
│  ├─ pull-requests.md
│  ├─ testing.md
│  ├─ parallel-development.md
│  ├─ adding-an-api.md
│  ├─ adding-a-page.md
│  └─ documentation-maintenance.md
└─ gaps/
   ├─ README.md
   ├─ frontend-monorepo.md
   ├─ shared-component-library.md
   ├─ generated-ts-client.md
   ├─ root-docker-compose.md
   └─ ci-cd.md
```

`README.md` 是唯一总入口。每份模块文档必须包含职责、代码入口、依赖、调用者、数据、接口、进程、配置、启动方式、测试、日志、常见故障、风险和修改时需同步的文档。

## 6. 架构决策记录

ADR 采用追溯型格式：

```text
标题
状态：Accepted (Retrospective) | Legacy | Superseded
当前事实
决定
代码与历史证据
为什么
后果
已知限制
后续变更条件
```

只有代码或现有历史规格能够支持的原因才写为确定结论。历史原因不明时写“依据不足”。

首批 ADR 覆盖：

1. Flask 模块化单体与 Blueprint 集成。
2. Windows 本地桌面启动器和执行环境。
3. AdsPower Local API、CDP 与 Playwright。
4. 模块局部 SQLite 与可选 MySQL。
5. Redis/RQ 和现存 Celery 并用。
6. 本机直开与管理认证双模式。
7. 人工点选元素和 fail-closed 定位。
8. Profile 公共身份与原始 AdsPower ID 边界。
9. 分批执行与确认关闭后再启动下一批。

## 7. HTTP 契约与错误码

### 7.1 OpenAPI 范围

- 使用 OpenAPI 3.1。
- 当前源码约有 186 个 Flask 路由声明；最终数量以提取脚本和人工复核结果为准。
- `/api/**` 全部进入 `openapi.yaml`。
- `/ping`、`/healthz` 等运维接口进入 OpenAPI。
- HTML 页面路由进入 `route-inventory.md`。
- 每个 Operation 写明源文件、鉴权、CSRF、请求、响应和错误。
- 当前行为不统一的接口使用 `x-legacy-exception`。
- 无法从代码确认的字段记为覆盖缺口，不进行猜测。
- OpenAPI 在本次交付中是文档契约，不生成代码，不改变运行时。

### 7.2 后续接口规范

文档发布后，新增或修改接口必须先更新 OpenAPI，并遵循：

```json
{"data": {}}
```

```json
{"error": {"code": "stable_error_code", "message": "固定中文说明"}}
```

- 异步动作使用 `202`。
- 创建成功使用 `201`。
- 同步查询或修改使用 `200`。
- 并发写使用 `revision` 或等价 CAS。
- 不返回异常原文和敏感运行时数据。

### 7.3 错误码表

错误码表包含错误码、HTTP 状态、所属模块、含义、是否可重试、操作员动作和源码位置。旧接口使用不稳定文本错误时，必须标记为 `legacy exception`。

## 8. Topic 树与消息 Schema

当前没有统一消息总线。文档必须区分：

```text
Redis / RQ
└─ browser_v2_comment_campaign
   ├─ campaign prepare
   ├─ assignment submit
   └─ campaign reconcile

Redis Keyspace
├─ browser_v2:comment_campaign:*
└─ selector_registry:*

Celery / Redis Broker
├─ 内容发布任务
└─ Selector Probe 调度或兼容任务

SQLite Durable Outbox
├─ publication_outbox
├─ webhook_outbox
├─ probe_effect_outbox
├─ element_request_outbox
└─ management_settings_publications
```

每条消息记录 Producer、Consumer、Queue/Key/表名、Job ID、Payload、幂等键、revision、重试、超时、租约、失败状态和敏感信息边界。

消息 Schema 使用 JSON Schema Draft 2020-12。只为当前稳定载荷建立 Schema，内部函数参数不冒充公共 Topic。

## 9. 数据与状态机

### 9.1 存储范围

- 可选 MySQL：根项目账号模型和旧业务数据。
- Management SQLite：管理用户和审计。
- Execution V2 SQLite：元素、元素版本、策略、动作、任务、Profile 运行、结果和校准。
- Selector Probe SQLite：探针、版本、发布、闸门、告警、元素目录、设置发布和 Outbox。
- TikTok Stats SQLite：跟踪账号、采集运行、快照、帖子、指标和租约。
- Comment Campaign SQLite/SQLAlchemy：模板、步骤、Campaign、Assignment、审批、Receipt、Attempt、Profile 身份和元数据。
- Redis：队列、租约、心跳和 Selector 发布映射。
- JSON 与本地文件：配置、内容、日志、截图、Evidence 和加密 Cookie。
- 外部系统：R2 对象存储、Buffer 发布、TikTok API、AdsPower Local API。

每张表记录列、类型、空值、默认值、主外键、唯一约束、索引、JSON 字段、读写者、事务边界、保留策略、备份、敏感字段和迁移来源。

### 9.2 状态机范围

- Execution V2 Job、Profile 和 Stage。
- Comment Campaign 和 Assignment。
- Selector Probe Run、Picker Session、Selector Version/Publication、Strategy Gate、Alert/Webhook。
- TikTok Stats Run、Account 和 Lease。
- Publish Batch 和 Result。

每个状态机写明初始状态、合法转换、触发者、前置条件、副作用、终态、恢复方式和并发保护。旧模块没有集中转换函数时，明确标记为源码事实汇总。

## 10. 前端、后端、中控与执行器

### 10.1 当前前端

- 单个 npm 项目，不是 pnpm monorepo。
- Jinja、原生 JavaScript 和 CSS，不是 React/Vue。
- 共享能力来自 `_dashboard_sidebar.html`、`dashboard_shell.css`、`dashboard_navigation.js` 和 `management_fetch.js`，不是组件库。
- 管理后台页面由 Flask 提供。

前端文档逐页列出 URL、Template、JS、CSS、API、权限、轮询、运行模式差异和测试。

### 10.2 当前 Web 后端

`app.py` 调用 `gateway.app.create_app()`，再注册业务 Blueprint。当前属于模块化单体，不是微服务集合。

### 10.3 当前逻辑中控

当前没有独立“中控服务”进程。职责分散在：

- `launcher.py`：进程启停。
- `gateway/app.py`：Web 集成和请求编排。
- `execution_v2/scheduler.py`：Browser V2 批次调度。
- `comment_campaign/service.py`、`executor.py`：Campaign 调度与执行。
- Redis、RQ、Celery：异步任务和租约。
- `selector_probe/worker.py`：探针调度。
- `tiktok_stats/worker.py`：统计调度。

### 10.4 当前电脑端执行器

执行器与 Web 系统位于同一台 Windows 电脑。文档覆盖 AdsPower Profile 隐私边界、CDP 会话、窗口平铺、readiness、Locator、input/contenteditable、ghost-cursor、键盘输入、滚轮与 ArrowDown、租约、关闭确认、失败隔离和禁止自动重放提交动作。

## 11. 运维与环境

当前只有 TikTok API 子目录包含 Compose。根项目没有 Flask、Redis、MySQL 的 Compose，也没有 GitHub Actions。

运维文档必须记录：

- 配置来源优先级。
- 环境变量、默认值、所属进程、必填性和敏感性。
- Windows 启动器的进程顺序和失败清理。
- 日志、数据库、截图和 Evidence 目录。
- 数据备份、恢复和保留策略。
- 健康检查和常见故障。
- 本机模式与认证模式差异。
- TikTok API Compose 的安装和启动边界。

## 12. 文档发布后的开发规范

### 12.1 UI

- 新页面复用现有侧边栏、页面壳、导航和同源请求封装。
- 状态不能只靠颜色表达。
- 外部数据只用 `textContent` 写入。
- 表单草稿、服务端状态和 in-flight 状态分离。
- 轮询不得覆盖正在编辑的输入。
- 危险操作携带原因、revision 和确认。
- 新页面增加 Node UI 测试。

### 12.2 Python

- PEP 8、4 空格、UTF-8。
- 新增公共函数和服务边界使用类型标注。
- Route 负责验证、权限、调用服务和响应投影。
- Domain/Service 承担业务规则。
- Store/Repository 承担 SQL 和事务。
- Adapter/Gateway 隔离 AdsPower、Redis 和外部 API。
- Executor/Locator 承担浏览器副作用。
- 不继续向 `gateway/app.py` 增加大段业务逻辑。
- 资源显式关闭；异步资源在同一事件循环关闭。
- 关联状态写入和审计在同一事务完成。

### 12.3 JavaScript

- 使用 `const`/`let`，禁止隐式全局变量。
- 同源请求统一走 `management_fetch.js`。
- 用户动作防重复提交。
- 不在 `localStorage` 保存业务数据或敏感数据。
- 新功能使用 `node:test` 覆盖。

### 12.4 Git 与 PR

提交采用 Conventional Commits：`feat`、`fix`、`docs`、`test`、`refactor`、`chore`。

一个提交只解决一个主题。接口、表结构、消息或状态机变化必须与相应文档同 PR。PR 描述必须包含目的、模块边界、契约变化、安全影响、测试结果、未执行的真实验收和回滚方式。

### 12.5 并行开发

主要目录按能力边界分工：`gateway/`、`execution_v2/`、`selector_probe/`、`comment_campaign/`、`tiktok_stats/`、`browser/`、`launcher.py` 和 `docs/architecture/`。跨模块功能先确定契约，再分别实现；避免多人同时修改 `gateway/app.py` 同一区域。

## 13. 阅读路径

`docs/architecture/README.md` 提供以下入口：

- 新开发人员：系统总览 → 本机启动 → 模块地图 → 开发规范。
- 前端人员：页面目录 → UI 规则 → API → Node 测试。
- 后端人员：Flask → Service/Store → OpenAPI → 数据库 → 状态机。
- 执行器人员：AdsPower → CDP → Locator → Actions → Batch → Safety。
- 运维人员：启动器 → 环境变量 → 日志 → 数据备份 → 故障处理。

## 14. 编写顺序

1. 建立文档中心和总索引。
2. 记录系统、进程、模块和技术栈。
3. 生成路由清单、OpenAPI 和错误码表。
4. 提取 Topic、Redis Key、Job 和 Outbox Schema。
5. 提取表结构、关系和状态机。
6. 编写前端、后端、中控和执行器文档。
7. 编写环境、启动、日志、备份和故障手册。
8. 编写开发规范和差距说明。
9. 执行覆盖、链接、语法、编码和敏感信息检查。

## 15. 验收标准

- 当前主要模块全部有入口文档。
- 源码发现的 Flask 路由全部进入 OpenAPI 或 HTML 路由清单。
- 所有 `CREATE TABLE` 和 SQLAlchemy Model 都进入数据目录。
- 代码中的集中状态枚举和转换表全部进入状态机文档。
- 现有 RQ、Redis、Celery 和 Outbox 全部进入 Topic 树。
- OpenAPI YAML 可以解析。
- JSON Schema 文件可以解析。
- 文档内部链接不存在断链。
- 新文档全部为 UTF-8，且无常见乱码。
- 文档不包含密钥、Cookie、原始 Profile ID 或 CDP WebSocket。
- 不启动真实 AdsPower Profile，不进行真实 TikTok 发布或评论。
- 不修改业务代码、数据库和运行配置。
- 不把 pnpm、共享组件库、TS 客户端、根级 Compose 和 CI/CD 写成已实现。
- 不提交当前工作区中无关的已有修改。

## 16. 维护规则

总入口必须记录文档版本、提取日期、Git 基线、工作区状态、适用运行模式、已验证范围、未验证范围、维护责任和最后复核日期。

后续修改接口、消息、表结构、状态机、页面入口、环境变量或进程拓扑时，代码 PR 必须同步修改对应架构文档。架构选择变化时先新增或更新 ADR；不得直接重写历史 ADR 以掩盖旧决定。
