# Browser V2 可编辑输入目标点选修复设计

## 目标

修复 TikTok 评论输入框保存为 `purpose=action`、`kind=input` 时返回“请求未通过业务校验”的问题。点选器必须兼容两种现场结构：点击可编辑节点内部子元素，以及点击包裹真实编辑器的外层输入栏。

## 已确认原因

当前页面点选器只将 `input`、`textarea`、`[contenteditable='true']` 识别为可编辑目标。TikTok 可能使用 `contenteditable=""`、`contenteditable="plaintext-only"`、`role="textbox"`，也可能让用户点中编辑器内部 `span/div` 或外层容器。点选器因此保存了普通节点的定位器；`PickerSession.save_selection(..., kind="input")` 随后执行 `is_editable()` Dry-Run 并失败。`PickerError` 又被 HTTP 层统一转换成 `validation_failed`，页面只能显示通用业务校验提示。

## 范围

采用方案 B：扩大可编辑节点识别范围，并在有限邻域内回退到唯一真实可编辑子节点。

本次只修改：

- `execution_v2/picker_overlay.js`
- `execution_v2/picker.py`
- `execution_v2/blueprint.py`
- 对应 JavaScript/Python 测试

不修改数据库、元素定义结构、策略结构、输入执行器、AdsPower 接口或既有已保存元素。

## 页面目标归一化

点选时使用以下优先顺序：

1. 在点击事件 `composedPath()` 中寻找最近的真实可编辑节点：`input`、`textarea`、任意存在且值不为 `false` 的 `[contenteditable]`。
2. 若路径中没有真实可编辑节点，检查最近的 `[role='textbox']`：自身符合真实可编辑规则时使用自身；否则只在其内部恰好存在一个真实可编辑后代时使用该后代。
3. 若仍未命中，从点击节点开始逐级检查非 `BODY/HTML` 祖先；第一个只包含一个真实可编辑后代的容器，使用该后代。
4. 没有唯一可编辑目标时，保持现有普通动作目标解析。后端 Dry-Run 最终拒绝将其保存为 `kind=input`，禁止猜测多个候选之一。

归一化后的真实可编辑节点用于高亮、属性采集、CSS/XPath 生成和定位器 Dry-Run。原始点击节点只保留为诊断指纹，不参与输入执行。

## 服务端校验与错误

安全边界保持不变：保存 `kind=input` 必须调用 `StrictLocatorResolver.resolve(..., require_editable=True)`；唯一性、可见性、面积、禁用状态和 `is_editable()` 全部通过后才能写入 SQLite。

当输入目标 Dry-Run 失败时，`PickerSession` 抛出固定错误 `picker_input_target_not_editable`。HTTP 层只为该固定错误返回：

```json
{
  "error": {
    "code": "input_target_not_editable",
    "message": "未能定位唯一可编辑输入框，请点选输入文字区域后重试。"
  }
}
```

其他 `PickerError` 继续使用通用 `validation_failed`，不向页面暴露选择器、DOM、异常文本或内部诊断。

## 数据流

1. 用户在 TikTok 评论输入区点击。
2. Overlay 将内部子节点或外层输入栏归一化为唯一真实可编辑节点。
3. Overlay 发送脱敏后的目标属性和定位候选。
4. 用户选择 `action/input` 并保存。
5. 服务端使用页面当前状态执行唯一性与 `is_editable()` Dry-Run。
6. 通过后保存；失败时返回明确、安全错误，点选会话保持开启，允许重试。

## 测试与验收

JavaScript 测试覆盖：

- A：点击内部 `span`，路径祖先存在 `contenteditable="plaintext-only"`，最终选择祖先。
- B：点击外层输入栏，容器内恰好一个可编辑后代，最终选择后代。
- `[contenteditable="false"]` 不得被选择。
- 容器内存在两个可编辑后代时不得猜测。
- 普通 SVG/按钮点选行为不回退。

Python 测试覆盖：

- `kind=input` Dry-Run 仍传入 `require_editable=True`。
- 输入目标 Dry-Run 失败返回 `input_target_not_editable` 和固定中文提示。
- 失败后不写入元素记录，点选会话仍可继续。
- 现有点选器、V2 路由和输入动作测试全部通过。

## 成功标准

- 用户点中 A 或 B 结构后，保存为 `action/input` 成功。
- 保存记录的目标是实际可编辑节点，不是内层文字节点或外层容器。
- 非唯一、不可编辑目标拒绝保存并显示明确提示。
- 既有 click/readiness 点选行为无回归。
