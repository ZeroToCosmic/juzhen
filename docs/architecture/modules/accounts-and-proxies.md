# Accounts and Proxies

## 职责
维护Buffer账号、代理会话、账号结果、代理池状态、发现与导入。`源码确认`。
## 不负责什么
不管理Comment Campaign的公共Profile身份；不执行浏览器策略。
## 代码入口
`gateway/account_store.py`、`gateway/proxy.py`、`gateway/proxy_pool.py`、`gateway/proxy_pool.py`相关路由位于`gateway/app.py`。
## 对外接口
`/api/accounts*`、`/api/account/*`、`/api/proxy-pool/status`、`/check_ip`。
## 内部组件
账号轮转、结果分类、渠道同步、代理会话分配、SOCKS/HTTP请求、IP诊断。
## 依赖与调用者
依赖SQLite、requests[socks]、Buffer发现；管理后台和发布流程调用。
## 数据与事务
账号SQLite连接使用局部事务；ACTIVE/BANNED结果决定账号状态。具体表由初始化代码和Store SQL定义。
## 配置
代理池、Buffer、IPInfo、超时和凭据来自设置/环境。
## 进程与生命周期
随Flask请求运行；外部请求必须有超时，不能长期占用请求线程。
## 安全边界
Token只显示掩码；代理密码、Buffer token和原始外部响应不得进入公共错误。
## 测试
`tests/test_account_routes.py`、`test_proxy.py`、`test_proxy_pool.py`、`test_ip_check.py`及Buffer测试。
## 日志与证据
账号同步错误保存脱敏摘要；外部响应不得原样记录凭据。
## 常见故障
账号数据库未初始化、代理不可用、Token失效、渠道数据形状变化、IP检查超时。
## 修改影响清单
同步账号公共投影、结果枚举、代理配置、导入规则、发布调用和API文档。
## 已知限制
旧账号与AdsPower Profile身份模型分离；没有统一账号主数据服务。
