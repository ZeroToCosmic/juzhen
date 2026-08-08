# 技术栈

## Python

来源：`requirements.txt`，证据等级 `配置确认`。

| 依赖 | 当前约束 | 用途 |
|---|---|---|
| Flask | `3.1.1` | Web、Blueprint、Template、Session |
| python-dotenv | `1.1.1` | `.env` 加载 |
| requests[socks] | `2.32.5` | AdsPower、代理、外部 HTTP |
| SQLAlchemy | `>=2.0,<3.0` | 根模型、Comment Campaign |
| asyncmy | `>=0.2.9` | 异步 MySQL |
| Celery | `>=5.4,<6.0` | 旧发布/任务接线 |
| redis | `>=5.0,<7.0` | Redis、租约、RQ 接线 |
| RQ | `>=2.2,<3.0` | Comment Campaign Worker |
| eventlet | `>=0.36,<1.0` | Celery/并发兼容 |
| Playwright | `>=1.50,<2.0` | Python CDP 浏览器执行 |
| Pydantic | `>=2.8,<3.0` | Comment Campaign 严格 API Schema |
| websocket-client | `>=1.8,<2.0` | CDP/浏览器兼容调用 |
| pywin32 | `>=306`，Windows | 进程、DPAPI、Windows 集成 |
| pytest | `8.4.1` | Python 测试 |
| openpyxl | `3.1.5` | Excel 内容导入 |

开发/打包配置现状：没有 `pyproject.toml`、Ruff、Black、mypy 或 pre-commit 配置。后续规范以文档约束为主，尚无自动格式门禁。

## Node.js

来源：`package.json`，证据等级 `配置确认`。

| 依赖 | 当前版本 | 用途 |
|---|---|---|
| `@cypress/unique-selector` | `2.2.0` | CSS 唯一选择器候选 |
| `ghost-cursor` | `1.4.2` | 拟人鼠标轨迹 |
| `openai` | `^6.45.0` | 旧浏览器 agent/model client |
| `playwright` | `^1.55.0` | Node 浏览器工具 |

当前包管理器是 npm，锁文件为 `package-lock.json`。没有 `pnpm-workspace.yaml`，没有 TypeScript 构建或前端框架。

## 前端

- Jinja/Flask Template。
- 原生 JavaScript。
- 普通 CSS。
- `node:test` 测试。
- 共用侧边栏、页面壳、导航和 same-origin fetch 文件。

当前没有组件编译、Tree Shaking、TypeScript、Storybook 或生成式 API Client。

## 数据与队列

- SQLite：多个模块分别持有数据库。
- MySQL 8+/InnoDB：可选旧根数据库。
- Redis：RQ、租约、心跳、Selector Registry。
- RQ：Comment Campaign。
- Celery：旧发布/任务接线。
- SQLite Outbox：Selector 发布、Webhook、Effect、元素请求等。

## 浏览器与桌面

- AdsPower Local API。
- Chromium DevTools Protocol。
- Python/Node Playwright。
- Tkinter 桌面启动器。
- Windows DPAPI、进程隐藏选项和端口 PID 检测。

## 外部集成

- TikTok 网站与本地 TikTok API 容器。
- Buffer GraphQL。
- R2/S3 兼容对象存储。
- IPInfo。
- 可配置 Webhook。

## 容器与交付

只有 `services/tiktok_api/docker-compose.yml`。Flask、Redis、MySQL 和 Windows 执行器没有统一 Compose。仓库没有 GitHub Actions 或其他 CI/CD 定义。

## 已知版本风险

- Python 依赖同时包含 Celery 和 RQ，维护者必须确认修改影响哪个 Worker。
- Python 与 Node 都使用 Playwright，版本约束不同；升级时需要分别回归。
- Pydantic/RQ/SQLAlchemy 使用范围版本，安装时间不同可能获得不同小版本。
- 当前缺少统一工具链锁定和 CI，交接时必须记录实际 Python、Node、浏览器与 AdsPower 版本。
