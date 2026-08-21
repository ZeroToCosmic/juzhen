# 浏览器执行策略 V2：Profile 配置接线与部分可用状态设计

## 目标

修复 V2 Profile 列表未使用现有 AdsPower 配置的问题，并在 AdsPower 不可用时保持元素、策略、历史和设置页面可用。修复不新增接口、配置项、数据库或后台任务，不改变执行器和批次调度行为。

## 已确认根因

Gateway 的旧 AdsPower 接口通过 `load_settings()` 读取 `config.json` 中的 `adspower.base_url` 和 `adspower.api_key`。V2 默认服务却直接创建 `AdsPowerController()`，只读取 `ADSPOWER_BASE_URL` 与 `ADSPOWER_API_KEY` 环境变量。启动器没有把持久化 API Key 复制到这些环境变量，因此旧接口能读取 Profile，而 `/api/browser-v2/profiles` 请求缺少 API Key 并最终返回 500。

V2 页面初始化时并行加载 Profile、元素、策略和历史。每个失败会保存错误，但初始化结束后又无条件把顶部状态改为“就绪”，造成“就绪”与“请求处理失败”同时出现。

## 选择方案

由 Gateway 作为组合根读取现有 AdsPower 设置，创建已配置的 `AdsPowerController`，并通过 `controller` 参数注入 `create_default_execution_v2_service()`。

不让 `execution_v2` 直接读取 `config.json`，避免 V2 核心依赖 Gateway 的存储格式；不依赖启动器导出环境变量，避免直接启动 Flask、测试和其他入口出现不同配置行为。

## 后端设计

`gateway.app.create_app()` 中的惰性 `execution_v2_service_factory()` 保持现有单例与锁不变。仅在未配置测试工厂时：

1. 调用现有 `load_settings()` 读取 `adspower` 设置。
2. 使用已保存的 `base_url`；为空时沿用现有默认地址。
3. 使用已保存的 `api_key`；为空时允许现有环境变量作为后备。
4. 创建 `AdsPowerController(base_url=..., api_key=...)`。
5. 将控制器传入 `create_default_execution_v2_service(controller=...)`。

API Key 只存在于服务端控制器，不写入 V2 SQLite、日志或 HTTP 响应。Profile 输出继续使用不透明 `profile_token` 与脱敏 `display_id`。

## 前端部分可用状态

页面继续并行加载四类资源，但分别保留加载结果：

- 全部成功：顶部显示“就绪”，清空初始化错误。
- Profile 失败、其他资源可用：顶部显示“部分可用：AdsPower 未连接”；错误区显示“无法读取 AdsPower Profile，请确认 AdsPower 已启动及 API Key 正确”。
- 其他资源失败：顶部显示“部分可用”；错误区保留对应安全错误摘要。
- 多个请求失败：不把任何失败覆盖成“就绪”；以明确、稳定的初始化摘要展示。

Profile 不可用时禁用依赖 Profile 的操作：

- 开始执行；
- 开始点选器；
- 元素校验 Profile选择及校验按钮。

以下操作继续可用：

- 查看、创建、编辑、停用和删除元素；
- 查看、创建和编辑策略；
- 查看运行历史；
- 修改本机页面偏好。

页面重新加载后重新读取 Profile。AdsPower 恢复且请求成功时自动回到“就绪”，不保存额外故障状态。

## 错误边界

Profile 请求失败不会创建空任务、不会启动 Profile，也不会影响其他三个读取请求。前端不显示异常堆栈、API Key、原始 Profile ID或 WebSocket 地址。后端仍保持现有通用错误响应；页面根据失败的资源类型给出 AdsPower 专用提示。

## 测试

后端测试验证：

- Gateway 把 `config.json` 中的 `base_url/api_key` 注入 V2 控制器；
- 环境变量仅作为空配置的后备；
- 自定义 `EXECUTION_V2_SERVICE_FACTORY` 测试入口不受影响；
- Profile API仍只返回不透明令牌和脱敏标识。

前端测试验证：

- 四个初始化请求全部成功时显示“就绪”；
- Profile 请求失败时显示“部分可用：AdsPower 未连接”；
- Profile 相关按钮与控件被禁用；
- 元素、策略、历史和设置页面仍能访问；
- 请求错误不会被后续初始化状态覆盖。

回归测试运行全部 V2 Python 测试和两个 V2 Node 测试文件。

## 验收标准

1. 已配置且运行中的 AdsPower 可通过 `/api/browser-v2/profiles` 返回脱敏 Profile 列表。
2. AdsPower 关闭或 API Key 错误时，页面显示“部分可用：AdsPower 未连接”。
3. 故障状态下无法启动执行、点选或元素校验。
4. 故障状态下仍可使用不依赖 Profile 的四类管理功能。
5. AdsPower 恢复并刷新页面后显示“就绪”。
6. API、页面和日志不泄露 API Key、原始 Profile ID或 WebSocket 地址。
