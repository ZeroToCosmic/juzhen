# ADR-0001: Flask模块化单体
## 状态
Accepted (Retrospective)
## 当前事实
`app.py`调用`gateway.app.create_app()`；业务模块通过Blueprint或Gateway路由注册在同一Flask进程。
## 决定
当前系统作为Flask模块化单体交付，不描述为微服务。
## 代码与历史证据
`gateway/app.py::create_app`、`execution_v2/blueprint.py`、`comment_campaign/blueprint.py`。
## 为什么
现有页面、认证、配置和本机外部服务接线共享同一应用生命周期；这是源码直接体现。最初组织原因`依据不足`。
## 后果
本机部署简单；全局权限统一。大型集成文件产生冲突和耦合。
## 已知限制
`gateway/app.py`规模大；模块独立测试依赖工厂注入。
## 后续变更条件
只有独立部署、独立扩缩容或故障隔离成为明确需求，才评估拆服务并新增ADR。
