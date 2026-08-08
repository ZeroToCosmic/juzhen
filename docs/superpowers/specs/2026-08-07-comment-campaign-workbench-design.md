# TikTok 评论 Campaign 集中式工作台设计

## 1. 目标

在现有浏览器执行策略 V2 旁新增“评论 Campaign”模块。模块复用现有 AdsPower Profile、CDP/Playwright、窗口分批与关闭、页面点选元素、文案库和拟人化输入能力，支持两种运行模式：

1. **独立评论**：每个参与账号在指定视频下发布一条互不依赖的顶层评论。
2. **盖楼模式**：按预先定义的父子步骤发布根评论和回复，支持线性、星型和分支树。

核心目标不是提高发布频率，而是保证每一步能够确认正确视频、正确登录账号、正确父评论和正确发布结果。任何不确定提交都停止并交由人工处理，禁止自动重复发布。

## 2. 合规边界

- 仅允许使用用户拥有或明确授权的账号和内容。
- 每条评论提交前必须人工确认；系统不提供批量自动确认。
- 不实现验证码绕过、风控规避、设备伪装或虚假身份生成。
- 不确定评论是否已经发布时禁止自动重发。
- UI 显示平台规则提示；使用者负责遵守 [TikTok Community Guidelines: Integrity and Authenticity](https://www.tiktok.com/community-guidelines/en/integrity-authenticity/)。

## 3. 已确认决策

- 使用集中式 Campaign 工作台，不拆成多个独立运行页面。
- 同一个模板支持线性、星型和分支回复，父子关系由 `parent_step_id` 定义。
- 每个步骤可以配置账号包含和排除条件。
- 默认一个 Campaign 内不重复使用同一账号，每条文案只分配一次。
- 角色属于“某个视频的某次 Campaign 分配”，不属于账号。账号没有永久楼主或参与者身份；同一账号可以在视频 A 的 Campaign 中担任楼主，在视频 B 的 Campaign 中担任参与者。
- 分配使用可复现的随机种子；锁定后执行期间不自动变化。
- 目标视频可以手动输入 TikTok URL，也可以选择现有视频发布结果。
- 默认每批 3 个 AdsPower Profile；上一批全部确认关闭后才能启动下一批。
- 父评论失败只暂停受影响分支，不影响独立评论或其他分支。
- Redis 只负责队列、租约和短期协调；SQLite 是业务状态唯一事实来源。

## 4. 技术架构

### 4.1 组件

```text
Flask 管理界面
  └─ Comment Campaign API
      ├─ TemplateService：模板和步骤
      ├─ AllocationService：账号筛选、随机分配、冻结计划
      ├─ CampaignService：状态机、审批、暂停与恢复
      ├─ ReceiptService：评论回执和复合定位
      └─ QueueAdapter：RQ 入队与 Redis 租约
             ↓
        Comment Campaign Worker
             ↓
   Execution V2 / AdsPower / Playwright
             ↓
      TikTok 页面 + 证据截图
```

### 4.2 技术选择

- Flask：沿用现有 Gateway 和本机管理页面。
- SQLite + [SQLAlchemy](https://github.com/sqlalchemy)：持久化 Campaign 业务数据；独立表，不迁移或修改 V2 既有记录。
- [RQ](https://github.com/rq/rq) + Redis：后台任务、短时租约、重试入队和进程重启恢复。
- [Playwright Python](https://github.com/microsoft/playwright-python)：通过现有 AdsPower CDP 会话进行页面操作和作用域定位。
- [Pydantic](https://github.com/pydantic/pydantic)：API 请求、模板树和状态转换校验。
- 现有 Ghost Cursor、键盘间隔、V2 元素库、内容文案库、窗口平铺和关闭逻辑：直接复用，不复制实现。

RQ Job 不长时间等待人工操作。任务到达人工确认点后写入 SQLite 并结束；确认接口再入队一个继续任务。这样不会占用 Worker，也能在 Flask 或 Worker 重启后恢复。

## 5. 数据模型

所有业务 ID 使用不透明 UUID。API 不返回 AdsPower 原始 Profile ID，只返回现有 V2 的脱敏标识和 Campaign 数据库维护的稳定随机 `profile_ref`。现有 V2 `profile_token` 契约保持不变，`profile_ref` 不得传给 V2 HTTP 接口。

### 5.1 `CommentTemplate`

用户配置一套评论对话结构。

| 字段 | 来源 | 规则 |
| --- | --- | --- |
| `id` | 系统 | UUID |
| `name` | 用户 | 1–100 字符，同一工作区不可重名 |
| `description` | 用户 | 可空，最多 500 字符 |
| `supported_modes` | 用户 | `independent`、`threaded` 或两者 |
| `language` | 用户 | 可空的 BCP-47 标签 |
| `tags` | 用户 | 可空字符串数组，去重 |
| `revision` | 系统 | 从 1 递增 |
| `enabled` | 用户 | 默认 `true` |
| `created_at/updated_at` | 系统 | UTC ISO-8601 |

已被 Campaign 引用的模板版本不可原地覆盖。编辑会产生新 revision；Campaign 保存完整模板快照。

### 5.2 `CommentStep`

用户配置模板中的一条评论。

| 字段 | 来源 | 规则 |
| --- | --- | --- |
| `id` | 系统 | 模板内稳定 UUID |
| `template_id` | 系统 | 所属模板 |
| `label` | 用户 | UI 名称，如“楼主”“回复 1” |
| `content_source` | 用户 | `fixed` 或 `library` |
| `fixed_text` | 用户 | `fixed` 时必填 |
| `content_library_id` | 用户 | `library` 时必填 |
| `content_item_id` | 用户/系统 | 可指定；为空时规划阶段从库中抽取并冻结 |
| `parent_step_id` | 用户 | 顶层为空；回复时指向同模板的另一步 |
| `role` | 系统推导 | 描述步骤在当前剧本中的角色：顶层盖楼步骤为 `owner`，回复为 `participant`；独立模式为 `commenter`。它不是账号属性 |
| `required_profile_tags` | 用户 | 候选账号必须全部包含 |
| `excluded_profile_tags` | 用户 | 候选账号不得包含 |
| `language` | 用户 | 可空；存在时账号必须匹配 |
| `position` | 系统/UI | 仅用于显示；执行顺序由父子依赖决定 |

模板校验拒绝：循环依赖、跨模板父节点、空内容源、盖楼模式无根节点。盖楼模板允许多个分支，但只允许一个根节点。独立模式所有 `parent_step_id` 必须为空。

### 5.3 `CommentCampaign`

用户配置一次实际运行。

| 字段 | 来源 | 规则 |
| --- | --- | --- |
| `id` | 系统 | UUID |
| `name` | 用户 | 1–100 字符 |
| `mode` | 用户 | `independent` 或 `threaded` |
| `target_source` | 用户 | `manual_url` 或 `publish_result` |
| `target_reference` | 用户 | URL 或现有发布结果 ID |
| `video_id` | 系统 | 目标解析后冻结 |
| `canonical_url` | 系统 | 规范化 TikTok 视频 URL |
| `template_id/revision` | 用户/系统 | 选择模板，保存 revision |
| `template_snapshot` | 系统 | 启动前不可变快照 |
| `profile_pool` | 用户 | 至少覆盖全部步骤的 Profile 引用 |
| `batch_size` | 用户 | 默认 3，范围 1–8 |
| `allocation_seed` | 系统/用户 | 默认安全随机生成，可手动固定 |
| `start_mode` | 用户 | `manual` 或 `scheduled` |
| `scheduled_at` | 用户 | 定时模式必填；数据库存 UTC，UI 按 Asia/Shanghai 显示 |
| `status` | 系统 | 见状态机 |
| `pause_reason` | 系统/人工 | 可空 |
| `created_at/updated_at` | 系统 | UTC ISO-8601 |

### 5.4 `CommentAssignment`

系统生成，用户在锁定前可以单条换号或整体重新随机。

| 字段 | 说明 |
| --- | --- |
| `campaign_id/step_id` | 对应 Campaign 和步骤 |
| `profile_ref` | 稳定的内部 Profile 引用 |
| `display_profile` | 脱敏名称 |
| `expected_username` | 运行前必须核验的 TikTok 用户名 |
| `role` | 当前 Campaign 和目标视频中的冻结角色：`owner`、`participant` 或 `commenter` |
| `resolved_text` | 已冻结的最终文案 |
| `parent_assignment_id` | 盖楼依赖；顶层为空 |
| `position` | 执行和展示顺序 |
| `status` | 当前步骤状态 |
| `locked_at` | 分配锁定时间 |

锁定前必须通过预检：账号启用、登录身份已记录、健康状态正常、冷却期已结束、标签和语言满足、候选账号数量足够、同一账号没有重复分配。

### 5.5 `CommentReceipt`

系统生成，用于后续唯一定位已经发布的评论。

| 字段 | 说明 |
| --- | --- |
| `campaign_id/video_id/step_id` | 业务归属 |
| `profile_ref/expected_username` | 发布账号证据 |
| `parent_step_id/parent_receipt_id` | 父评论证据 |
| `platform_comment_id` | 能获得时保存，否则为空 |
| `comment_permalink` | 能获得时保存，否则为空 |
| `normalized_text_hash` | 标准化文本 SHA-256 |
| `posted_at` | 已观测发布时间 |
| `author_profile_href` | 作者主页地址 |
| `stable_attributes` | 当前节点可重复使用的稳定属性 |
| `locator_candidates` | 按可靠性排序的作用域定位候选 |
| `screenshot_path` | 提交后现场截图相对路径 |
| `status` | `pending`、`published_unverified`、`published_verified`、`invalidated` |

回执不能只保存 XPath。有效回执必须包含视频 ID、账号证据、文本哈希、时间窗口和至少一种节点证据。只有刷新页面后重新唯一定位成功，状态才是 `published_verified`。

### 5.6 `CommentAttempt`

系统生成，记录每次短任务的完整执行结果。

| 字段 | 说明 |
| --- | --- |
| `campaign_id/assignment_id/profile_ref` | 业务归属 |
| `attempt_no` | 从 1 递增 |
| `stage` | 当前执行阶段 |
| `status` | `running`、`succeeded`、`failed`、`cancelled` |
| `error_code/error_summary` | 稳定错误码和脱敏摘要 |
| `evidence_paths` | 截图等相对路径 |
| `started_at/finished_at` | 时间 |

错误摘要不得包含 Cookie、CDP 地址、AdsPower 原始 ID、Authorization 或完整页面源码。

## 6. Profile 元数据

账号条件需要一个轻量 `CommentProfileMetadata` 表，仅保存 Campaign 所需信息：`profile_ref`、`expected_username`、`enabled`、`login_verified`、`tags`、`language`、`region`、`cooldown_until`、`health_status`。该表不保存 `role`：角色由每个 Campaign 的 `CommentAssignment` 独立决定。

另设内部 `CommentProfileIdentity` 映射表：首次发现 AdsPower Profile 时生成随机 UUID `profile_ref`，并在本机 SQLite 中映射原始 AdsPower ID。原始 ID 不进入 API、Redis、RQ 参数、日志或 UI；只有 `CampaignStore` 与 `ProfileGateway` 可以读取。Profile 列表及启动仍复用 AdsPower Controller、V2 会话和 Adapter，但 `profile_ref` 与现有进程内 `profile_token` 是两个不同契约。

## 7. 分配算法

1. 冻结 Profile 元数据、模板 revision 和文案库候选快照。
2. 过滤禁用、未核验登录、异常、冷却中或不满足条件的账号。
3. 为每个步骤生成候选账号集合。
4. 按候选数量从少到多分配，避免严格条件步骤最后无账号可用。
5. 对候选集合使用 `allocation_seed` 做确定性洗牌。
6. 使用有界的确定性二分图匹配，确保同 Campaign 不重复账号、同文案不重复使用，避免百步模板出现指数级回溯。
7. 无完整解时预检失败，不创建部分执行计划。
8. 保存分配并展示；用户可换号、重新随机或锁定。

相同模板快照、Profile 快照、文案快照和 seed 必须产生完全相同的分配。

每个 Campaign 独立运行分配算法。历史 Campaign 中的角色不作为下一次分配的固定条件，也不形成“楼主账号池”或“参与者账号池”。因此同一个 Profile 的角色可以随视频和剧本自由切换；只有账号健康、标签、语言、冷却期和当前 Profile 租约会影响候选资格。两个 Campaign 同时使用同一 Profile 时，由 Profile 租约串行执行，而不是改变其角色。

示例：

```text
视频 A / Campaign A：Profile-01 → owner，Profile-02 → participant
视频 B / Campaign B：Profile-02 → owner，Profile-01 → participant
```

## 8. 执行流程

### 8.1 公共阶段

1. 获取 Campaign 租约和 Profile 租约。
2. 检查 Campaign 未暂停、Assignment 可运行且依赖满足。
3. 使用 Campaign `ProfileGateway` 和内部身份映射把 `profile_ref` 解析为 AdsPower Profile，再复用现有 V2 会话、Adapter、平铺与关闭能力；V2 HTTP 接口仍只接受自己的 `profile_token`。
4. 启动 Profile、连接 CDP、按本批窗口数量平铺。
5. 打开 `canonical_url`，等待页面基础就绪并验证当前视频 ID。
6. 验证当前 TikTok 登录用户名等于 `expected_username`。
7. 进入评论区并等待评论控件稳定。
8. 定位输入框；盖楼回复还要定位并核验父评论。
9. 填入冻结文案，但不提交。
10. 保存现场证据，状态变为 `awaiting_step_approval`，短任务结束。
11. 人工在工作台点击“确认提交”后，重新入队。
12. 继续任务重新验证 Profile、视频、父评论和输入内容，再点击提交。
13. 生成初始回执，刷新页面并重新定位。
14. 唯一定位成功后标记 `published_verified`；否则暂停该步骤且禁止重发。
15. 当前批全部达到终态或等待人工状态后关闭所有本批窗口；确认关闭后再启动下一批。

准备阶段关闭窗口后，“查看现场”显示该次准备保存的截图和证据，不承诺保留一个长期打开的实时浏览器。人工批准签发一次性的 Assignment revision 提交许可；继续任务重新打开 Profile、恢复同一目标和同一冻结文案。任何重新核验结果与批准证据不一致时，许可失效并返回待确认状态，不进行提交。

### 8.2 独立评论

- 所有 Assignment 无父依赖。
- 最多启动 `batch_size` 个 Profile，但同一个视频使用 Redis `video_submit` 租约串行进入真正提交阶段。
- 单个账号失败只影响自己的 Assignment。
- 其他已准备的账号仍可逐条人工确认。

### 8.3 盖楼模式

- 按模板拓扑顺序执行。
- 子步骤只有在父回执为 `published_verified` 后才可进入定位阶段。
- 回复按钮必须在已唯一匹配的父评论节点内部查找，禁止使用页面级“第一个回复按钮”。
- 点击回复后验证 UI 显示的回复目标与父评论作者一致，再填入文案。
- 父步骤失败或回执失效时，只把它的后代标为 `paused_dependency`；无关分支继续。

## 9. 评论定位与核验

### 9.1 视频定位

- 从 URL 解析 `video_id` 并生成规范 URL。
- 页面加载后从当前 URL、可见视频链接或页面数据中再次读取 ID。
- 不一致即 `target_video_mismatch`，不得输入评论。

### 9.2 父评论定位

按以下证据降序组合匹配：

1. `platform_comment_id` 或 permalink。
2. `author_profile_href + normalized_text_hash + video_id`。
3. `expected_username + 精确标准化文本 + posted_at 时间窗口 + parent_receipt_id`。
4. `stable_attributes` 与节点内相对定位候选。

评论列表允许增量滚动和等待懒加载，但有明确时间及滚动次数上限。最终必须得到恰好一个可见评论节点。零个为 `parent_comment_not_found`，多个为 `parent_comment_ambiguous`。

### 9.3 发布核验

- 提交前记录当前账号在目标评论区域的匹配节点集合。
- 提交后等待新节点或可识别响应，再保存候选 ID 和截图。
- 刷新或重新进入目标视频。
- 使用完整回执重新定位并确认唯一节点、作者和文本一致。
- 未确认时状态为 `published_unverified`，人工核验后只能“标记已发布”或“确认未发布并重新准备”；系统不自动选择。

## 10. 状态机

Campaign 状态：

```text
draft → planned → awaiting_campaign_approval → queued → running
      → paused / failed / completed / cancelled
```

Assignment 状态：

```text
planned → waiting_dependency → opening_profile → locating_video
→ locating_parent → preparing_comment → awaiting_step_approval
→ submitting → verifying_receipt → published_verified
→ paused / paused_dependency / failed / cancelled
```

约束：

- 只有锁定计划可以批准。
- 只有 `awaiting_step_approval` 可以确认提交。
- `submitting` 或 `verifying_receipt` 状态遇进程重启时自动转为 `published_unverified`，禁止重放提交动作。
- 人工批准消费和提交前重新核验期间保持 `awaiting_step_approval`；仅当全部核验通过、即将点击时，才使用 revision CAS 原子进入 `submitting`。核验不一致时使批准失效、递增 revision，并保持待批准状态。
- 人工暂停优先于自动调度，恢复只能把满足依赖的步骤重新入队。

## 11. API 边界

所有写接口使用 JSON、严格拒绝未知字段，并采用 revision 或状态前置条件避免重复点击。

### 11.1 模板

- `GET /api/browser-v2/comment-templates`
- `POST /api/browser-v2/comment-templates`
- `GET /api/browser-v2/comment-templates/<id>`
- `PUT /api/browser-v2/comment-templates/<id>`
- `POST /api/browser-v2/comment-templates/<id>/disable`

已被引用的模板不物理删除，只能禁用。

### 11.2 Campaign

- `GET /api/browser-v2/comment-campaigns`
- `POST /api/browser-v2/comment-campaigns`
- `GET /api/browser-v2/comment-campaigns/<id>`
- `POST /api/browser-v2/comment-campaigns/<id>/plan`
- `POST /api/browser-v2/comment-campaigns/<id>/reallocate`
- `PUT /api/browser-v2/comment-campaigns/<id>/assignments/<assignment_id>`
- `POST /api/browser-v2/comment-campaigns/<id>/lock-plan`
- `POST /api/browser-v2/comment-campaigns/<id>/approve`
- `POST /api/browser-v2/comment-campaigns/<id>/pause`
- `POST /api/browser-v2/comment-campaigns/<id>/resume`
- `POST /api/browser-v2/comment-campaigns/<id>/cancel`

### 11.3 逐条确认与记录

- `GET /api/browser-v2/comment-campaigns/<id>/approvals`
- `POST /api/browser-v2/comment-campaigns/<id>/assignments/<assignment_id>/approve-submit`
- `POST /api/browser-v2/comment-campaigns/<id>/assignments/<assignment_id>/reject-submit`
- `POST /api/browser-v2/comment-campaigns/<id>/assignments/<assignment_id>/resolve-unverified`
- `GET /api/browser-v2/comment-campaigns/<id>/receipts`
- `GET /api/browser-v2/comment-campaigns/<id>/attempts`

确认接口必须携带当前 Assignment revision；重复确认返回当前状态，不重复入队。

`resolve-unverified` 仅接受两种人工决议：`published` 表示人工确认已经发布，并写入人工核验事件；`not_published` 表示人工确认没有发布，步骤转为暂停。后者必须显式恢复、重新准备并取得新的提交批准，旧批准不可复用。

## 12. 集中式工作台 UI

复用原系统左侧导航，新增“评论 Campaign”，不恢复旧执行策略入口。

页面顶部：

- 新建 Campaign。
- 全部、待确认、运行中、异常、已完成筛选。
- Redis/Worker/AdsPower 连接状态；任一不可用时明确显示，不伪装成业务失败。

主列表卡片：

- Campaign 名称、模式、目标视频、模板 revision。
- 已完成/总步骤、运行批次、已打开 Profile 数量。
- 盖楼模式显示可折叠依赖树；独立模式显示平铺步骤。
- 每条步骤显示脱敏 Profile、角色、状态、父回执状态和最后错误。

右侧待确认面板：

- 账号、目标视频 ID、评论文案、父评论摘要。
- 登录账号核验、视频核验、父评论唯一性、输入内容核验四项证据。
- “查看现场”“确认提交”“拒绝并暂停”三个操作。“查看现场”展示准备阶段截图、目标证据和定位摘要，不依赖窗口持续打开。

详情抽屉：

- 分配快照、状态时间线、CommentReceipt、CommentAttempt、截图。
- 错误使用中文解释，同时保留稳定错误码供排查。

创建使用抽屉式五步向导：模式 → 目标视频 → 模板 → Profile 与随机分配 → 审批锁定。创建后回到集中工作台，不进入独立监控页。

## 13. Redis 与租约

Redis Key 使用独立命名空间 `browser_v2:comment_campaign:`：

- `campaign:<id>`：同 Campaign 单一调度者。
- `profile:<profile_ref>`：同 Profile 单一任务。
- `video_submit:<video_id>`：同视频单一提交者。
- `approval:<assignment_id>`：确认请求幂等保护。

租约必须有 TTL 和 owner token；释放使用 compare-and-delete，禁止一个 Worker 删除另一个 Worker 的租约。Redis 不保存最终状态。Redis 丢失后从 SQLite 重建待执行队列，但 `submitting` 不重放。

## 14. 错误处理

稳定错误码包括：

- `adspower_unavailable`
- `profile_start_failed`
- `cdp_connect_failed`
- `profile_identity_mismatch`
- `target_video_invalid`
- `target_video_mismatch`
- `comment_panel_not_ready`
- `comment_input_not_found`
- `parent_comment_not_found`
- `parent_comment_ambiguous`
- `comment_author_mismatch`
- `reply_target_mismatch`
- `comment_submit_uncertain`
- `comment_receipt_unverified`
- `profile_close_failed`
- `redis_unavailable`
- `worker_unavailable`
- `allocation_unsatisfied`

失败策略：

- 基础页面加载可以在提交前重试最多 3 次，每次重新加载页面。
- 账号、视频、父评论、回复目标或提交结果错误不自动重试提交。
- Profile 关闭失败阻止下一批启动，并显示人工关闭入口。
- Redis 或 Worker 不可用时拒绝启动新 Campaign，已持久化状态保留。

## 15. 测试策略

### 15.1 单元测试

- 模板树：合法线性/星型/分支、循环、跨模板父节点、多个根节点。
- 分配器：条件过滤、确定性增广路匹配、账号与规范化文案不重复、seed 可复现、无完整解。
- 跨 Campaign 角色：同一 Profile 在不同视频中可分别成为 `owner` 和 `participant`，历史角色不污染新分配。
- 状态机：合法转换、非法转换、幂等确认、重启后的 `submitting` 保护。
- 定位器：ID、作者+文本、时间窗口、多匹配、零匹配、虚拟滚动上限。
- 租约：互斥、TTL、owner compare-and-delete、Redis 丢失恢复。

### 15.2 API 与 UI 测试

- 严格请求字段、revision 冲突、模板禁用、计划锁定。
- 独立和盖楼创建向导。
- 工作台筛选、依赖树、逐条确认、暂停/恢复、错误中文解释。
- AdsPower、Redis、Worker 未连接时的明确状态。
- Profile 原始 ID、Cookie 和 CDP 地址不出现在响应或页面。

### 15.3 执行验收

1. 纯模拟：300 个 Profile、每批 3 个；验证批次关闭顺序、唯一分配和队列恢复。
2. 本地受控：6 个测试 Profile 完成两批启动/关闭；另外 2 个测试 Profile 验证点选和登录身份读取。
3. 独立评论：至少 3 条内容分别进入人工确认并产生唯一回执。
4. 盖楼：至少完成一条三层线性链和一个二分支模板。
5. 故障注入：父评论消失、多匹配、账号不符、视频不符、提交结果不明、关闭失败、Redis 重启。
6. 回归：原系统、浏览器 V2、元素库、策略编辑、内容文案库和既有 V2 运行接口继续工作。

真实提交验收只能由用户使用授权测试账号并逐条人工确认。自动测试默认停在提交前，使用伪造页面和 Fake AdsPower 验证其余流程。

## 16. 交付边界与顺序

1. 数据表、校验模型、状态机和模板接口。
2. Profile 元数据、分配器和锁定计划。
3. 独立评论准备、逐条确认和回执核验。
4. 盖楼依赖、父评论定位和分支暂停。
5. 集中式工作台和运行详情。
6. RQ Worker、Redis 租约、启动器接线和故障恢复。
7. 模拟验收、本地测试 Profile 验收和既有功能回归。

本功能作为新 `comment_campaign` 边界实现。`execution_v2` 只暴露必要的 Profile 会话、元素解析、动作执行、平铺和关闭接口；禁止把 Campaign 业务状态继续堆入 `gateway/app.py` 或 `execution_v2/service.py`。

## 17. 非目标

- 不自动生成评论内容。
- 不自动发现或购买账号。
- 不自动确认提交。
- 不保证被删除、折叠或平台不再返回的评论仍能定位。
- 不抓取整站评论数据库。
- 不修改或迁移旧执行策略、Selector Probe 或 V2 既有数据。
- 不把 SQLite 最终状态迁移到 Redis。
