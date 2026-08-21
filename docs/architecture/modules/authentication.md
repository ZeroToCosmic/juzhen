# Authentication

## 职责
管理用户、密码、登录锁定、Session、CSRF、角色权限、密码修改、会话撤销和审计。`源码确认`。
## 不负责什么
本机直开模式的网络边界由 `gateway/local_only.py` 负责；业务级批准不属于登录认证。
## 代码入口
`gateway/auth_service.py`、`auth_store.py`、`auth_blueprint.py`、`admin_users.py`、`management_db.py`。
## 对外接口
`/login`、`/healthz`、`/api/auth/*`、`/api/admin/users*`。
## 内部组件
Werkzeug密码哈希、失败计数、15分钟锁定、30分钟空闲/8小时绝对会话、administrator/operator权限。
## 依赖与调用者
依赖Flask Session和Management SQLite；全局路由保护和管理页面调用。
## 数据与事务
`management_users`、`management_audit_events`；用户修改与审计应同一写流程。
## 配置
最短密码12字符；角色集合固定；Cookie/Session配置由Flask提供。
## 进程与生命周期
随Flask进程运行；撤销会话通过用户版本/状态使旧Session失效。
## 安全边界
错误消息固定，禁止暴露用户是否存在的额外信息；unsafe请求必须CSRF；operator不能执行管理员写操作。
## 测试
`tests/test_auth_store.py`、`test_auth_service.py`、`test_auth_routes.py`、`test_admin_users.py`。
## 日志与证据
管理审计表记录安全操作；不记录密码或Session内容。
## 常见故障
首次管理员未初始化、账号锁定、CSRF缺失、Session过期、本机直开配置与认证预期不一致。
## 修改影响清单
同步权限矩阵、Session失效、CSRF、审计、错误码、登录UI和安全测试。
## 已知限制
面向单机管理后台；没有外部身份提供商、MFA或多租户身份域。
