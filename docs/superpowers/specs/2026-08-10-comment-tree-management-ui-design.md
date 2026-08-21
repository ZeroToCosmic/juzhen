# 评论树管理界面精简与生命周期设计

## 1. 目标

精简评论 Campaign 模块中的评论树管理，使用户无需理解内部模式名或数据库 ID，即可完成评论树的创建、编辑、停用、重新启用和删除。

本次改动只覆盖评论树管理 UI、模板生命周期接口及必要的数据字段，不重构 Campaign 执行、审批、Profile、元素绑定或 Worker。

## 2. 术语与展示文案

- 内部模式 `threaded`：界面统一显示“盖楼回复”。
- 内部模式 `independent`：界面统一显示“独立评论”。
- `revision`：界面显示“版本 N”，仅表示同一评论树的修订版本。
- 模板、步骤、父步骤、文案库和文案项的内部 ID 不在普通界面中显示，也不要求用户输入。

同名、同版本、同模式的多行记录是多棵独立评论树，不是同一评论树的不同 `threaded` 版本。

## 3. 评论树生命周期

评论树有三个用户可感知状态：

1. **启用**：可编辑、可供新 Campaign 选择、可停用。
2. **停用**：默认折叠显示；不可编辑、不可供新 Campaign 选择；可重新启用或删除。
3. **已删除**：普通列表和普通 API 完全不可见；不可重新启用；数据库保留审计及历史引用。

允许的状态迁移：

- 启用 → 停用
- 停用 → 启用
- 停用 → 已删除

禁止从启用状态直接删除，用户必须先停用，以降低误删除风险。每次状态变更都要求精确 `expected_revision`，成功后 revision 加一并写入不可变模板快照。

服务端同时强制状态约束：只有启用记录可编辑或停用；只有停用且未删除记录可启用或删除。重复停用、重复启用、编辑停用记录和直接删除启用记录均返回状态冲突，不能只依赖按钮隐藏。

软删除不得破坏已经锁定的 Campaign：只有 `locked_at` 非空、已经冻结模板与 Assignment 的 Campaign 可以继续审批、排队和执行，其 `template_id + template_revision + template_snapshot`、Assignment 冻结文案及父关系保持不变。

`locked_at` 为空的 Campaign 不获得祖父化资格。评论树被停用或删除后，关联的 draft/planned Campaign 执行 plan、reallocate、lock-plan 或 approve 时全部 fail closed，返回 `409 template_unavailable`，固定中文消息为“所选评论树已停用或删除”。本次不新增 Campaign 原地换树接口；用户必须取消或弃用该未锁定 Campaign，选择一棵启用中的评论树新建 Campaign。已锁定 Campaign 后续不得重新读取当前模板状态覆盖冻结快照。

## 4. 数据与接口

### 4.1 数据字段

`comment_templates` 增加 nullable `deleted_at`。`deleted_at IS NULL` 表示正常或停用；非空表示软删除。数据库强制业务不变量：`deleted_at IS NOT NULL` 时 `enabled` 必须为 false。

每次 revision 快照增加 `lifecycle_status`，取值严格为 `enabled`、`disabled` 或 `deleted`，并继续保存兼容字段 `enabled`。旧快照缺少 `lifecycle_status` 时，按其既有 `enabled` 字段映射为 `enabled` 或 `disabled`，绝不推断为 `deleted`。删除操作在同一事务内完成 master record revision CAS、`enabled=false`、`deleted_at`、新 revision snapshot 和 steps 写入；任一步失败全部回滚。

SQLite 初始化沿用现有幂等列迁移方式补列。其他数据库由模型元数据和后续正式迁移保持一致。不得物理删除模板 revision、Campaign snapshot、Assignment、Receipt 或 Attempt。

### 4.2 接口

保留现有接口：

- `POST /api/browser-v2/comment-templates/{template_id}/disable`

新增：

- `POST /api/browser-v2/comment-templates/{template_id}/enable`
- `POST /api/browser-v2/comment-templates/{template_id}/delete`

请求严格为：

```json
{"expected_revision": 2}
```

成功返回 `200 {"data": template}`。错误判定顺序固定为：不存在或已删除先返回 `404 not_found`；对象存在但 `expected_revision` 不一致返回 `409 revision_conflict`；revision 一致但模板状态不允许返回 `409 invalid_state_transition`。未锁定 Campaign 因模板停用或删除而不能继续返回 `409 template_unavailable`。

`GET /comment-templates` 默认返回启用和停用记录，但永远过滤已删除记录。普通 `GET /comment-templates/{id}` 对已删除记录返回 404。Store 内部按历史 revision 解析冻结 Campaign 的方法继续可读旧 revision，但不得通过普通 Blueprint route 暴露已删除模板。

## 5. 页面信息架构

### 5.1 评论树列表

评论树管理抽屉首先展示“启用中的评论树”。每条记录使用紧凑单行：

- 评论树名称
- “盖楼回复”或“独立评论”
- 真实评论数量（服务端有值时显示）
- 版本
- 状态文本
- 同行操作按钮

启用项操作为“编辑 / 停用”。

“已停用”使用默认收起的原生折叠区域，标题显示数量。展开后每条记录操作为“启用 / 删除”。删除按钮触发二次确认，确认文案说明删除后普通界面不可见且不能恢复。

列表不显示 `threaded`、`independent`、模板 ID、步骤 ID或其他内部引用。空列表显示明确中文空状态。

### 5.2 新建评论树

列表页只保留一个主操作“新建评论树”。点击后立即进入创建界面，不重复显示已有评论树列表，也不显示说明性技术内容。

创建界面顶部仅提供两个清晰入口：

- 手工逐条创建
- Excel 批量导入

手工入口默认创建一条“楼主评论”。用户填写树名、选择“盖楼回复/独立评论”、填写文案，并通过“添加下一条回复”逐条追加。高级父级选择按需展开；所有内部 ID 静默生成和保存。

Excel 入口直接显示 `.xlsx` 文件选择、“预览导入”和预览结果。只有有效评论树可勾选提交；失败项显示带行号中文原因。Excel 导入与手工草稿相互隔离，轮询不得清空任一草稿。

编辑已有评论树进入同一手工编辑器；文案库或多模式等无法无损编辑的旧模板继续只读，不得静默转成固定文案。

## 6. 视觉规范

评论 Campaign 模块恢复浅色主题并与原管理后台保持一致：浅色页面背景、白色卡片、中性灰边框、深色正文、低对比次要文字。不得强制暗色，也不得改变其他模块的主题。

卡片使用紧凑间距；操作按钮必须同行排列并允许窄屏换行，不能各自占据整行。抽屉主体可滚动，标题与关闭按钮保持可见。360px 宽度不得产生横向滚动。

所有状态必须有中文文字，不能只依赖颜色；按钮使用原生 `button`、明确 `type="button"`，键盘焦点清晰。

## 7. 安全与并发

- 所有写操作保留现有管理员角色和 CSRF 校验。
- Local-direct 继续受 loopback 与 Host guard 保护。
- 列表和错误响应继续递归脱敏，不返回 raw AdsPower ID、Cookie、Authorization、API key 或 WebSocket 地址。
- UI 使用 `textContent`/原生表单，不使用 `innerHTML`。
- 409 后保留当前草稿或列表状态并刷新服务端 revision，不自动重试删除、启用或停用。

## 8. 数据清理边界

当前数据库中存在 5 条名为 `A`、版本 2、模式 `threaded`、已停用的独立记录。它们不会在本次代码变更中自动删除。新 UI 会将其折叠到“已停用”区域，用户可逐条执行软删除。

自动测试必须使用临时数据库，不得再次写入项目正式 `data/comment_campaign/comment_campaign.db`。

## 9. 验收标准

- 模块为浅色，其他页面视觉不变。
- 列表不出现原始 `threaded/independent` 或任何内部 ID。
- 启用项为紧凑单行“编辑 / 停用”；停用项位于默认折叠区域并提供“启用 / 删除”。
- 删除项从普通列表和详情 API 消失，但已有冻结 Campaign 与 Assignment 不变。
- 新建入口直接显示手工/Excel 两种方式，不混入已有列表和技术说明。
- 手工模式可从楼主评论开始逐条追加；Excel 可预览、选择和导入。
- revision 冲突、非法状态、权限和 CSRF 均有稳定响应。
- Node UI 测试、模板 store/service/routes、integration/security 和既有 Campaign 冻结回归全部通过。
