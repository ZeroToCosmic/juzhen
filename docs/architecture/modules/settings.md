# Settings

## 职责
加载默认配置、环境回退、深合并、凭据保留、原子保存、备份与损坏恢复。`源码确认`：`gateway/settings_store.py`。
## 不负责什么
不验证所有外部服务真实可用；模块级严格校验仍由对应Service负责。
## 代码入口
`gateway/settings_store.py`、`config.example.json`、`gateway/app.py`设置路由。
## 对外接口
`/settings`、`/api/settings`、状态、恢复、模型预设；Campaign另有严格四绑定设置接口。
## 内部组件
默认值、路径解析、深合并、配置归一化、备份序号、原子replace、凭据保留。
## 依赖与调用者
Gateway、Worker和Launcher读取；写操作由管理后台触发。
## 数据与事务
JSON文件原子替换，不是数据库事务；保存前备份旧文件，损坏文件另存。
## 配置
`APP_CONFIG_PATH`可覆盖路径；代理设置可从环境变量回退。
## 进程与生命周期
按需读写；多进程同时写缺少跨进程统一数据库CAS，模块专用设置可另有revision。
## 安全边界
公共GET只返回脱敏/是否已配置状态；保存深合并不得清空未在表单提交的凭据。
## 测试
`tests/test_config.py`、`tests/test_settings_store.py`、`tests/test_settings_routes.py`。
## 日志与证据
配置健康状态和备份文件；禁止将真实 `config.json` 提交Git。
## 常见故障
JSON损坏、目录权限、原子替换失败、旧备份恢复后缺少新字段、环境变量覆盖预期。
## 修改影响清单
同步默认值、归一化、公共投影、示例配置、设置UI、Worker读取和备份兼容测试。
## 已知限制
配置Schema未统一为Pydantic/JSON Schema；多个模块仍分别解释同一配置。
