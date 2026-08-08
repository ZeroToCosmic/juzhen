# Gateway

## 职责
创建 Flask 应用、注册页面/API/Blueprint、安装本机或认证保护、装配模块 Service 与外部 Adapter。`源码确认`：`gateway/app.py::create_app`。
## 不负责什么
新业务规则不应继续直接写入 Gateway；浏览器副作用应交给 Executor，SQL应交给Store。
## 代码入口
`app.py`、`gateway/app.py`、`gateway/local_only.py`、`gateway/templates/`、`gateway/static/`。
## 对外接口
根页面、设置、账号、内容、发布、Legacy Browser、V2、Probe、Stats、Campaign 等 HTML/JSON 路由。
## 内部组件
Application Factory、全局 before_request 保护、lazy service factory、Evidence文件投影、页面渲染、资源关闭。
## 依赖与调用者
依赖所有业务模块和 Flask；由 Launcher 或 Flask CLI/WGI入口调用。
## 数据与事务
自身不应定义跨模块事务；当前旧路由存在直接调用Store/文件/外部API的Legacy例外。
## 配置
Flask config、环境变量、`gateway/settings_store.py`持久设置；Comment Campaign、V2等使用独立配置键。
## 进程与生命周期
单 Flask 进程；成功构造的 lazy service 缓存在 `app.extensions`，关闭时释放。
## 安全边界
本机直开依赖loopback/Host；认证模式依赖Session/CSRF/角色。所有公共响应需递归脱敏。
## 测试
`tests/test_app.py`、`tests/test_auth_routes.py`、各模块 integration/routes 测试。
## 日志与证据
Flask服务日志、浏览器JSONL、模块Evidence；文件下载必须限制目录和文件名。
## 常见故障
旧进程未退出、lazy factory依赖不可用、Windows session key权限问题、模板静态资源版本不同步。
## 修改影响清单
检查全局保护、Blueprint顺序、URL冲突、close逻辑、页面导航、OpenAPI与集成测试。
## 已知限制
`gateway/app.py`约7953行，集成与旧业务耦合高；不是独立API Gateway微服务。
