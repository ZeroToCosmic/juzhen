# 系统上下文

## 业务目的

系统用于在本机管理代理、账号、内容、TikTok 数据采集以及 AdsPower 浏览器自动化。浏览器执行支持元素点选、积木式策略、批量 Profile、评论 Campaign 和人工批准后的提交。

证据：`源码确认`，入口见 `gateway/app.py`、`execution_v2/`、`selector_probe/`、`tiktok_stats/`、`comment_campaign/`。

## 主要使用者

| 使用者 | 当前能力 | 入口 |
|---|---|---|
| 本机管理员 | 配置、账号、内容、元素、策略、Campaign、用户管理 | Flask 管理后台 |
| 操作员 | 只读或受限业务操作，具体由角色权限决定 | Flask 管理后台 |
| Windows 维护者 | 启停 Flask、Worker、Redis/MySQL 检查 | `launcher.py` |
| 后台 Worker | 定时探针、统计采集、Campaign 准备与提交 | 各模块 `worker.py` |

证据：`源码确认`，角色定义见 `gateway/auth_store.py`、`gateway/auth_blueprint.py`。

## 系统边界

系统内部包括：

- Windows 启动器和本机进程监督。
- Flask 页面、API、认证和模块装配。
- Browser Execution V2、Selector Probe、TikTok Stats、Comment Campaign。
- 本地 SQLite、可选 MySQL、Redis、JSON 配置、日志与 Evidence。
- Python/Node 浏览器动作工具。

系统外部包括：

- AdsPower Local API 与其浏览器进程。
- TikTok 网站。
- TikTok 数据 API 容器。
- Buffer GraphQL API。
- R2 兼容对象存储。
- IPInfo 等诊断服务。
- 可配置 Webhook 接收方。

## 信任边界

1. 管理后台有两种保护：本机直开模式依赖 loopback/Host 限制；认证模式依赖 Session、CSRF 和角色。
2. AdsPower 原始 Profile ID、CDP WebSocket 和浏览器凭据只能存在于 Adapter/Gateway 内部。
3. Redis/RQ 载荷只传业务 ID、revision 和 generation，不传浏览器凭据。
4. Evidence 通过严格文件名和目录边界提供，不能接受任意路径。
5. 真实提交属于不可安全重放的副作用；结果不确定时进入人工核验。

证据：`源码确认`、`测试确认`，见 `gateway/local_only.py`、`comment_campaign/blueprint.py`、`comment_campaign/queueing.py`、`comment_campaign/executor.py`。

## 当前非目标

- 不提供公网 SaaS 或多租户能力。
- 不将 Windows AdsPower 执行器容器化。
- 不支持在没有人工批准的情况下重放不确定评论提交。
- 不将系统描述为微服务平台。
- 不声明 pnpm、React、生成式 TS 客户端或 CI/CD 已存在。

## 已知上下文风险

- `gateway/app.py` 同时承担集成、页面和大量旧业务路由，修改冲突概率高。
- 新旧浏览器执行路径并存，调用者必须明确使用 Legacy 还是 V2。
- SQLite、MySQL、Redis、JSON 和文件存储并存，备份不能只覆盖一个数据库。
- 当前工作区大量代码尚未提交，交接时必须同时保存 Git 状态与本文件基线。
