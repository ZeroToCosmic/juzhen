# Browser V2 策略记录归一化修复

## 问题

Browser V2 API 返回扁平策略记录：`target_url`、`ready_element_id`、`actions` 等字段位于策略顶层。策略编辑器使用嵌套草稿：这些字段必须位于 `strategy.definition`。

新策略保存成功或从策略列表重新点击“编辑”后，前端直接把扁平记录赋给 `state.draft`。`renderStrategies()` 读取 `state.draft.definition.actions` 时，`definition` 不存在，产生 `Cannot read properties of undefined (reading 'actions')`。

## 选择

采用方案 A：在 UI 数据边界把 API 策略记录转换为编辑器草稿。

不采用：

- 修改后端返回嵌套结构：会改变现有 API 合约。
- 在编辑器各处兼容两种结构：判断分散，容易漏改。

## 实现

只修改 `gateway/static/browser_v2.js`：

1. 新增纯函数 `strategyDraft(record)`。
2. 若记录已经包含 `definition`，返回深拷贝。
3. 若记录为 API 扁平结构，把 `target_url`、`ready_element_id`、`readiness_timeout_seconds`、`run_mode`、`loop_duration_minutes`、`actions` 移入新建的 `definition`。
4. 策略列表“编辑”按钮通过该函数创建 `state.draft`。
5. 保存成功后的响应记录也通过该函数创建 `state.draft`。
6. `state.strategies`、API 请求和后端返回保持原样。

不修改 API、数据库、后端、HTML 或策略执行格式。

## 验收

- 扁平 API 策略点击“编辑”后存在 `draft.definition.actions`。
- 新建策略保存成功后仍可继续修改名称和动作。
- 已经是嵌套结构的新建草稿保持不变。
- 保存请求继续发送现有闭合 schema。
- 现有 Browser V2 UI 测试全部通过。

