# Browser V2 键盘输入与文案库设计

## 目标

键盘输入动作必须支持：

1. 选择一个真实可输入的网页元素。
2. 输入策略内指定的固定文案，或从所选品牌文案库随机抽取一条。
3. 每个 Profile 每次执行输入动作时独立抽取文案。

## 现状与问题

- 键盘输入只接受 `purpose=action`、`kind=input` 的元素。当前元素库没有任何 `input` 元素，所以目标下拉框为空。
- 当前 UI 将 `content_source` 强制写成 `fixed`，没有来源选择或文案库选择。
- 策略 schema 已支持 `content_source=fixed|library`，但默认 V2 运行时未注入文案库解析器；library 动作会报 `content_library_unavailable`。
- 现有内容管理已保存品牌文案库。当前 OFS 有 40 条，YISHAO 为 0 条。

## 元素选择

- 不降低后端约束：键盘输入仍只允许 `kind=input`。
- 目标下拉框仅显示启用中的 action/input 元素。
- 没有可用目标时，动作卡显示明确提示：前往元素库点选真实 `<input>`、`<textarea>` 或 `[contenteditable=true]`，保存类型选择 `input`。
- 不把现有 click 元素自动改成 input，避免运行时定位到不可编辑节点。

## 文案来源 UI

键盘输入动作增加“文案来源”字段：

- `fixed`：显示“输入文案”文本框，隐藏文案库下拉框。
- `library`：隐藏固定文案文本框，显示“品牌文案库”下拉框。

文案库下拉框显示名称与文案数量。0 条文案的库显示但不可选择。切换来源时清除另一来源的值，保存请求保持现有闭合 schema：

```json
{
  "content_source": "fixed | library",
  "fixed_text": "固定文案或空字符串",
  "content_library_id": "品牌 ID 或空字符串"
}
```

## V2 文案库接口

新增只读接口：

`GET /api/browser-v2/content-libraries`

响应仅包含：

```json
{
  "data": [
    {"id": "ofs", "name": "OFS", "copy_count": 40}
  ]
}
```

接口不返回文案正文。V2 服务通过注入的 provider 获取现有品牌列表，不直接依赖 Gateway 或内容存储模块。

## 运行时解析

- `create_default_execution_v2_service` 和 `ExecutionV2Service` 接受可选异步 `text_resolver`。
- 默认未注入时维持现有失败行为。
- Gateway 创建 V2 服务时注入现有内容目录解析器。
- resolver 根据 `content_library_id` 读取该品牌文案，过滤空正文，随机返回一条。
- 每次 input 动作调用 resolver 一次，因此不同 Profile 独立随机抽取。
- 固定文案不调用 resolver。

## 错误处理

- 文案库不存在、已删除或无有效文案：动作失败，错误码 `content_library_unavailable`。
- 没有 input 元素、没有选择目标或来源字段不完整：策略保存阶段拒绝，UI 保留动作内容供修正。
- 不在日志、历史或 API 结果中返回实际输入正文，只保留来源和字符数。

## 范围

预计修改：

- `execution_v2/blueprint.py`
- `execution_v2/service.py`
- `gateway/app.py`
- `gateway/static/browser_v2.js`
- 对应 Python 与 Node 测试

不修改数据库、元素类型规则、现有内容管理接口或文案文件结构。

## 验收

- 无 input 元素时显示准确指引，不再只有空白“请选择”。
- 保存一个 input 元素后，目标下拉框可选择。
- 固定文案模式保存并执行正常。
- 文案库模式可选择 OFS；YISHAO 因 0 条不可选。
- 同一策略在不同 Profile 执行时分别调用随机解析器。
- 文案库失效时返回明确错误，且不泄露文案正文。
- 现有 Browser V2 API、UI、策略、执行器测试全部通过。

