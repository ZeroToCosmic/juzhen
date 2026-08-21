# Console 评论树维护与 Campaign 选择修复设计

状态：已确认

日期：2026-08-21

## 1. 问题与目标

新建评论 Campaign 页面默认使用“独立评论”，并只展示支持当前模式的启用评论树。当前本机唯一启用的评论树只支持“盖楼回复”，因此初始化后评论树下拉框为空。与此同时，新 Console UI 没有评论树维护入口，用户无法在本机创建、编辑或启停评论树，只能依赖旧版 Comment Campaign 工作台。

本次实现两个目标：

1. 当 Campaign 初始模式没有可用评论树而另一模式有时，自动切换到有可用树的模式。
2. 在 Console 动作库下新增完整的评论树维护页面，恢复本机调试所需的创建、编辑、启停、删除和 Excel 导入能力。

## 2. 范围与边界

### 范围内

- 新增 `/console/actions/comment-trees` 独立维护页。
- 动作库和新建 Campaign 页面提供评论树维护入口。
- 评论树列表、模式和状态筛选、启用与停用分组。
- 手工新建、编辑、停用、启用、软删除。
- Excel 预览与批量导入。
- Campaign 初始化模式自动纠正、评论树刷新和无可用树空状态。
- 页面路由、JavaScript 控制器和回归测试。

### 范围外

- 不新增或修改 Comment Campaign 后端 API。
- 不修改数据库结构、模板版本模型或 Campaign 执行逻辑。
- 不改变评论树与 Campaign 的严格模式匹配规则。
- 不迁移 Campaign 计划、审批、执行、回执或证据页面。
- 不移除旧 `/comment-campaigns` 工作台；它继续作为兼容入口。
- 不把评论树作为独立动作混入动作列表。评论树是评论 Campaign 的依赖定义资源。

## 3. 信息架构

### 3.1 路由与入口

新增：

```text
GET /console/actions/comment-trees
```

页面继承 `console_base.html`，侧边栏保持“动作库”激活。

- 动作库页标题栏新增“评论树管理”。
- 新建 Campaign 的“评论配置”区域新增“管理评论树”和“刷新评论树”。
- “管理评论树”在新标签页打开，避免丢失 Campaign 草稿。
- “刷新评论树”只重新读取评论树，不重新加载 Profile。

### 3.2 维护页视图

维护页是全宽业务页面，不使用右侧抽屉。控制器包含三个互斥视图：

1. `list`：评论树列表、搜索、模式筛选、状态筛选。
2. `editor`：手工新建或编辑、实时结构预览、保存和返回列表。
3. `import`：Excel 文件选择、预览、有效树选择、提交结果。

列表展示名称、支持模式、状态、版本、更新时间和操作，不展示模板 ID、节点 ID 或其他内部标识。启用项支持编辑和停用；停用项默认折叠，支持启用和删除。删除必须二次确认。

## 4. 组件复用与状态

新增页面级文件：

- `gateway/templates/console_comment_trees.html`
- `gateway/static/console_comment_trees.js`
- `gateway/static/console_comment_trees.css`
- `tests-js/console-comment-trees.test.js`

新控制器复用 `gateway/static/comment_tree_editor.js`，不加载旧版大型 `comment_campaign.js`。后端继续使用现有 CommentTemplate、CommentStep、不可变 revision 和生命周期实现。

页面状态至少包含：

```text
view: list | editor | import
templates
draft
readonlyTemplate
importDraft
filters: query, mode, status
loading
submitting
error
```

状态规则：

- 新建时由编辑器生成初始草稿。
- 编辑时先读取完整详情，再转换为编辑器草稿。
- 保存成功后清空草稿、刷新列表并返回列表视图。
- 保存失败、网络错误和 revision 冲突均保留本地草稿。
- 列表刷新不得覆盖编辑草稿或导入草稿。
- 同一时间只允许一次写操作。
- 固定文案且仅支持一种模式的评论树可编辑。
- 文案库来源或同时支持多种模式的旧评论树只读，不进行有损转换。

## 5. Campaign 模式与评论树选择

评论树加载完成后执行以下规则：

1. 当前模式存在启用评论树时保持当前模式。
2. 当前模式没有启用评论树、另一模式存在时，自动切换到另一模式。
3. 两种模式都没有启用评论树时保持当前模式并显示空状态。
4. 自动切换只发生在初始化或用户尚未主动选择模式时。
5. 用户主动切换模式后，刷新评论树不得反向自动切换。
6. 系统永远不自动选择具体评论树。
7. 模式改变或已选评论树变得不兼容时，清空 `template_id`、`template_revision`、Profile 选择和预览状态。

现有前后端 `supported_modes` 校验保持权威，不允许把盖楼树当成独立评论树使用。

## 6. API 复用

页面仅复用现有接口：

| 功能 | 接口 |
| --- | --- |
| 列表 | `GET /api/browser-v2/comment-templates` |
| 详情 | `GET /api/browser-v2/comment-templates/{id}` |
| 新建 | `POST /api/browser-v2/comment-templates` |
| 编辑 | `PUT /api/browser-v2/comment-templates/{id}` |
| 停用 | `POST /api/browser-v2/comment-templates/{id}/disable` |
| 启用 | `POST /api/browser-v2/comment-templates/{id}/enable` |
| 删除 | `POST /api/browser-v2/comment-templates/{id}/delete` |
| Excel 预览 | `POST /api/browser-v2/comment-template-imports/preview` |
| Excel 提交 | `POST /api/browser-v2/comment-template-imports` |

编辑和生命周期请求继续携带准确的 `expected_revision`。所有请求继续使用 Console 已加载的同源、认证和 CSRF 请求机制。Excel multipart 请求不得手工设置 `Content-Type`。

## 7. 错误与恢复

- `403`：提示当前账号无权维护评论树，保留草稿。
- `404`：提示评论树已不存在，刷新列表并返回列表视图。
- `409 revision_conflict`：保留本地草稿，刷新服务端 revision，要求用户重新确认。
- `409 invalid_state_transition`：刷新列表并显示当前生命周期状态。
- `413`：明确提示 Excel 文件或内容超限。
- `422`：显示可执行的中文业务校验提示，不暴露原始内部错误。
- 网络错误或 `5xx`：保留草稿和导入预览，允许重试。
- 部分导入成功：移除成功项，仅保留失败项及对应错误。

所有用户内容使用安全文本节点渲染，不使用 `innerHTML`。

## 8. 测试与验收

### Campaign 创建页

- 仅有启用的 `threaded` 树时，初始化自动切换为 `threaded` 并显示该树。
- 两种模式都有树时保持默认 `independent`。
- 两种模式都没有或只有停用树时显示明确空状态。
- 自动切换后不自动选择评论树，也不调用 Profile 预览。
- 用户主动选择模式后，评论树刷新不改变其选择。
- 模式变化继续清除不兼容的模板、Profile 和预览状态。

### 评论树维护页

- 路由、Console 外壳、动作库激活状态和脚本顺序正确。
- 列表加载、搜索、模式筛选、启用和停用分组正确。
- 新建和编辑请求体符合现有 schema，编辑包含 `expected_revision`。
- 文案库和多模式评论树保持只读。
- 启停、删除、删除取消和并发写保护正确。
- revision 冲突后草稿不丢失。
- Excel 预览、有效项选择、提交和部分失败恢复正确。
- 页面不展示模板 ID、节点 ID、Profile 标识或原始错误代码。

### 完成标准

1. 用户可以完全通过新 Console UI 创建和维护评论树。
2. 当前本机仅有盖楼评论树时，新建 Campaign 自动显示“盖楼回复”及可选评论树。
3. 用户仍需明确选择具体评论树，系统不代替确认。
4. 现有 API、数据库、Campaign 冻结和执行语义保持不变。
5. 相关 Node、Flask 和 Comment Campaign 回归测试通过。
6. 实现完成后由 Sol 进行只读架构和代码复核；阻塞问题修复后重新复核。
