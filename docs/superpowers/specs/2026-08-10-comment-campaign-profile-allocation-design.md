# Comment Campaign Profile 分配与 AdsPower 健康修复设计

## 1. 背景与目标

当前 Comment Campaign 存在两个直接影响使用的问题：

1. AdsPower Local API 在一秒内未响应时，工作台经常显示“AdsPower：不可用”，即使随后请求可以成功。
2. 系统已有 21 个 AdsPower Profile，但没有 Profile 元数据；规划阶段又强制要求预先填写 TikTok 用户名和登录验证，因此候选数为零并返回 `allocation_unsatisfied`。

本次只解决以上两个问题，并简化 Campaign 创建时的 Profile 分配操作。不重构执行器、评论树、审批机制或自动发布逻辑。

## 2. 已确认的业务事实

- AdsPower Profile 代表稳定的浏览器窗口。
- 一个窗口中的 TikTok 账号可能被退出、替换或重新登录，因此窗口和 TikTok 账号不是永久一一对应关系。
- 规划阶段选择的是窗口，不是账号。
- TikTok 用户名只能在执行准备阶段打开窗口后读取并验证。
- 同一 Campaign 内，一个窗口仍只能承接一个 Assignment。
- 同一 Campaign 内若多个窗口实际登录为同一 TikTok 账号，必须暂停，不得自动换窗或继续提交。
- 用户需要同时支持自动选窗与手动选窗；默认自动选择满足评论树所需的最少窗口数。

## 3. 范围

### 3.1 本次实现

- 修正 AdsPower 健康探针的超时和错误展示。
- AdsPower 离线时仍可读取、查看和配置本地已缓存 Profile。
- Campaign 创建界面提供自动选窗和手动选窗。
- 自动模式默认选择评论树所需的最少窗口数。
- 规划阶段不再要求 `expected_username` 或历史 `login_verified`。
- 执行准备阶段读取当前 TikTok 用户名并冻结本次 Assignment 的账号证据。
- 在任何 Assignment 进入审批前，先完成整个 Campaign 的全量已分配窗口账号预检。
- 重复账号、未登录、账号读取失败或准备后账号发生变化时安全暂停。
- 将 `allocation_unsatisfied` 转换为用户可理解的具体原因，同时保留稳定内部错误码。
- 减少 UI 每五秒对 AdsPower 的重复全量同步。

### 3.2 明确不做

- 不自动为重复账号换窗。
- 不新增常驻 Profile 缓存服务。
- 不修改评论树结构或角色分配规则。
- 不降低人工审批与提交前复核要求。
- 不新增自动提交或绕过审批。
- 不把 AdsPower Profile 名称当作 TikTok 用户名。
- 不把 AdsPower 健康卡片解释为“TikTok 已登录”或“执行已就绪”。

## 4. AdsPower 健康修复

### 4.1 健康定义

工作台中的“AdsPower”只表示：

> 本机 AdsPower Local API 的 Profile 列表端点在本次探测中可访问并返回合法响应。

它不表示浏览器可以启动、CDP 可连接、TikTok 已登录或评论执行一定成功。

### 4.2 探针策略

- 单次健康探针超时由 1 秒提高到 4 秒。
- 四秒是包含连接、读取和解析在内的总墙钟上限，`max_retries = 1`。
- 只请求第一页、一个 Profile，不执行全量分页。
- 同一进程内健康请求使用 single-flight；并发请求共享同一个在途结果，避免探针堆积。
- 健康探针不得启动 Profile、浏览器或访问 TikTok。
- 返回结果只包含状态和脱敏原因，不包含 API Key、原始响应、Profile 原始 ID、CDP 地址或异常文本。

### 4.3 脱敏原因分类

健康结果使用以下稳定原因之一：

- `connected`：Local API 返回合法成功响应。
- `timeout`：四秒内未完成。
- `connection_refused`：本机端口未监听或连接被拒绝。
- `authentication_failed`：AdsPower 拒绝 API Key。
- `invalid_response`：HTTP、JSON 或 AdsPower 业务响应不合法。
- `not_configured`：地址或 API Key 未配置。

UI 显示固定中文，不回显底层异常：

- 可用：`AdsPower Local API 可访问`
- 超时：`AdsPower 响应超时`
- 拒绝连接：`AdsPower Local API 未启动或端口不可访问`
- 鉴权失败：`AdsPower API Key 无效`
- 响应异常：`AdsPower 返回了无法识别的响应`
- 未配置：`尚未配置 AdsPower Local API`

### 4.4 配置刷新

Comment Campaign 服务不得永久持有首次读取的旧 AdsPower 地址或 API Key。保存 AdsPower 设置后，下一次健康探测和 Profile 同步必须使用最新持久化配置。实现可通过重建相关 controller 或使 provider 每次从配置源创建轻量 controller；不得要求用户重启整个应用才能生效。

Gateway、健康探针和 Worker 使用同一个配置解析函数和相同优先级：有效的持久化设置优先，环境变量仅在对应持久化值为空时回退。新 Worker job 不得使用与 Gateway 不同的地址或 API Key。

## 5. Profile 缓存与离线行为

### 5.1 数据边界

- `profile_ref` 是系统内部稳定窗口引用。
- 原始 AdsPower Profile ID 只允许存在于 Profile gateway/controller 边界，不进入公共 API、日志、任务参数或 DOM。
- 本地缓存保存 `profile_ref`、脱敏显示名、窗口级元数据和最后同步时间。
- TikTok 用户名属于 Assignment 准备证据，不再作为窗口永久身份。

### 5.2 缓存读取与在线同步接口

现有 Profile 元数据 GET 改为 cache-only。兼容响应 envelope 固定为 `{"data": [profiles], "meta": {"stale": bool, "last_synced_at": string|null, "safe_reason": string|null}}`。新增一个严格、显式的同步动作；页面首次加载在完成 cache-only GET 后调用一次，用户也可以通过“重新同步 AdsPower”按钮调用。同步成功后更新缓存并返回最新缓存；失败时返回 HTTP 200、现有缓存和 `stale=true` 的脱敏原因，不得清空缓存。只有请求格式、认证或权限错误返回 4xx。

- cache-only：`GET /api/browser-v2/comment-profile-metadata`
- 显式同步：`POST /api/browser-v2/comment-profile-metadata/sync`，请求体为严格空对象
- 两者均返回同一安全 Profile 列表和同步状态；显式同步失败不以空数组覆盖缓存
- sync POST 沿用 Comment Campaign 的 legacy 管理员角色与 CSRF 保护，以及 local-direct 的 Host 和 REMOTE_ADDR 守卫

Profile 在线同步仅在以下时机执行：

- Comment Campaign 页面首次加载时的一次有界同步。
- 用户打开 Profile 选择器并主动刷新时。
- 用户点击“重新同步 AdsPower”时。

五秒轮询只调用 cache-only GET 和 Campaign 状态接口，不执行 AdsPower 全量分页。

### 5.3 离线回退

AdsPower 同步失败时：

- Profile 列表接口仍返回本地缓存。
- 用户仍可查看 Profile、调整窗口级元数据、选择窗口并保存 Campaign 草稿。
- UI 明确显示“当前展示缓存数据，实际执行前需要 AdsPower 恢复”。
- 不得把同步失败伪装成空 Profile 列表。
- 不得因为离线而删除或覆盖已有缓存。
- 开始实际准备或提交时，AdsPower 不可用必须安全失败或暂停，不得继续执行。

## 6. 窗口级元数据与规划资格

### 6.1 规划阶段保留的窗口条件

窗口可参与规划必须满足：

- `enabled = true`
- `health_status = healthy`
- 冷却时间为空或已结束
- 满足步骤要求的标签、排除标签和语言规则

### 6.2 从规划阶段移除的账号条件

以下字段不得再作为规划资格条件：

- `expected_username`
- `login_verified`
- 上一次 Campaign 读取到的 TikTok 用户名
- 上一次 Campaign 的账号健康结论

旧字段如为兼容现有 API 或历史数据显示而暂时保留，也只能作为历史信息，不得参与新规划。

### 6.3 元数据缺省值

首次发现的新窗口应获得可理解的缺省状态，避免 21 个窗口全部因“未配置元数据”而不可选：

- `enabled = true`
- `health_status = healthy` 仅表示窗口未被系统隔离；不代表 TikTok 已登录
- 标签、语言、地区为空；其中地区仅作为窗口信息展示，不参与本次步骤匹配
- cooldown 为空

如果模板步骤没有标签或语言约束，这类新窗口可以直接参与规划。被关闭失败隔离、人工禁用或仍在冷却的窗口继续不可选。

缺省元数据只在某个 `profile_ref` 尚无 metadata 时创建，并与身份同步使用同一事务。再次同步不得覆盖人工禁用、隔离、标签、语言或 cooldown。

## 7. 自动与手动分配

### 7.1 创建界面

Campaign 创建界面不再要求用户输入逗号分隔的 `profile_ref`。它提供两个模式：

- `自动选择`：默认模式。
- `手动选择`：用户从可见的脱敏窗口列表中勾选。

内部 `profile_ref` 只作为 `<option>` 或状态值提交，不在页面可见文本中展示。

### 7.2 自动选择

- 根据所选评论树的步骤数计算最低窗口数。
- 调用后端 `POST /api/browser-v2/comment-profile-selection/preview`，严格请求体为 `template_id`、必填 `mode` 和可选 `template_revision`。
- 后端必须复用正式规划所用的同一个资格判断和二分匹配器，以稳定显示顺序作为 tie-break，返回恰好满足完整匹配的最少窗口集合。
- 响应只包含 `required_count`、`eligible_count`、推荐的安全 `profile_ref` 和脱敏显示信息；不创建 Campaign、不写 Assignment。
- preview POST 沿用 Comment Campaign 的 legacy 管理员角色与 CSRF 保护，以及 local-direct 的 Host 和 REMOTE_ADDR 守卫；成功和错误响应都通过既有递归脱敏器。
- 前端不得自行复制匹配算法，也不得简单选择列表中的前 N 个。
- 用户可以在自动结果上手动取消、补充或替换窗口。`M>N` 表示提供更大的候选池，不表示所有候选都会生成 Assignment。
- Campaign 的角色仍由评论树中的父子关系决定；不根据窗口历史角色决定。

### 7.3 手动选择

- 显示脱敏 Profile 名、可用/禁用/冷却/隔离状态以及不满足条件的简短原因。
- 不可用窗口默认不可勾选。
- 显示 `需要 N 个 / 已选择 M 个 / 当前可用 K 个`。
- `M < N` 时禁止规划，并给出具体提示。

### 7.4 后端权威校验

前端自动选择和预检查只是辅助。后端仍必须重新验证：

- 所有 `profile_ref` 存在且属于当前本地身份映射。
- 窗口数量足够。
- 同一 Campaign 内窗口不重复。
- 每个步骤至少存在一个满足标签、语言和冷却条件的候选窗口。
- 完整的步骤到窗口匹配存在。

## 8. 准备阶段账号读取与冻结

### 8.1 Campaign 全量已分配窗口账号预检

正式规划先从候选池完成步骤到窗口的一对一匹配并生成 N 个 Assignment。账号预检只遍历这些 Assignment 的去重 `profile_ref` 集合；未分配候选不打开、不读取、不冻结。

账号唯一性必须在任何 Assignment 进入人工审批、输入评论或点击提交前完成，不能等到正常的逐批准备才发现。

1. Campaign prepare job 取得 Campaign 租约。
2. 按既有 `batch_size` 分批打开所有已分配窗口；上一批全部确认关闭后才能开始下一批。
3. 每个窗口只导航到目标 TikTok 页面并读取账号身份，不输入评论、不打开提交动作、不创建审批。
4. 收集全部窗口的账号结果；所有批次完成前不把任何 Assignment 置为 `awaiting_step_approval`。
5. 全量验证账号都可识别且规范化后互不重复。
6. 通过一个 Store 事务和 Campaign revision CAS 生成新的单调 `identity_generation`，一次性把本次预检覆盖的全部已分配窗口账号及同一代次冻结到对应 Assignment；任何一项失败则整批不写部分成功结果。
7. 全量冻结成功后，才进入既有的逐批评论准备流程。

预检本身仍遵守窗口租约、三次关闭确认和 close-failure 隔离规则。任一窗口无法确认关闭时暂停 Campaign，不得开始下一批或进入审批。

预检使用专用开窗路径：持有 Campaign/Profile 租约，但不绕过或放宽普通 prepare 的 `Campaign running` 门。实现不得为了让预检运行而允许普通 prepare 在无有效 generation 时操作页面。

### 8.2 账号证据合同

账号定位优先读取可验证的账号链接 `/@handle`，可见用户名文本仅作为回退。链接和文本同时存在但规范化后不一致、存在多个可见候选或链接格式非法时，身份判定失败。

规范化账号键使用 Unicode NFKC、去首尾空白、去除开头 `@` 和大小写折叠。每个 Assignment 的运行时冻结数据包括：

- `profile_ref`
- canonical account key
- 可见用户名
- canonical account href（可用时）
- `observed_at`
- 目标视频证据
- 账号元素绑定 ID、revision 和定义摘要
- 截图或其他安全 evidence 引用

现有 Assignment `expected_username` 字段保留以兼容 Receipt、父评论定位和现有执行器，但语义改为“本 Campaign 从 canonical `/@handle` 得到的运行时账号 handle（不含 `@`）”，不再从 Profile metadata 复制。可见展示名单独保存在 evidence。Profile metadata 中的同名历史字段不得参与规划。

### 8.3 正常逐批准备

Campaign 级预检成功后，每个 Assignment 的准备必须在任何输入前先用 Store CAS 校验 Campaign 为 `running`、generation 有效且 Campaign/Assignment/account evidence 三方 generation 一致；旧 generation job 无操作退出。随后重新读取当前账号，并与全量预检冻结值一致，再完成输入预演和人工审批证据。准备 evidence 必须合并并保留 `account_preflight` 子文档及其 `identity_generation`，不得用页面证据覆盖。若不一致，按身份变化处理，不能覆盖冻结值后继续。

### 8.4 重复账号

若两个不同窗口读取到同一规范化 TikTok 账号：

- 一个 Store 事务同时暂停两个相关 Assignment 和 Campaign，并使相关未消费审批失效。
- 错误使用稳定码 `duplicate_tiktok_account`。
- UI 显示两个脱敏窗口名和同一可见账号名，不显示原始 AdsPower ID。
- 用户可以修正原窗口的登录后重新准备。
- 已锁定 Campaign 不允许原地更换窗口；如需换窗，用户必须取消或弃用旧 Campaign，重新选择窗口创建新 Campaign。
- 系统不得自动替换窗口，不得继续点击提交。

### 8.5 无法读取账号

预检期间以下任一情况均暂停整个 Campaign；正常准备或提交复核期间发生时按身份变化事务处理。所有情况都必须保持零提交点击：

- TikTok 未登录。
- 账号控件不存在或多义。
- 用户名为空或格式无效。
- 页面视频与目标视频不一致。
- AdsPower/CDP 连接中断。

使用稳定码区分：`tiktok_login_required`、`tiktok_identity_unavailable`、`target_video_mismatch` 或既有运行时错误码。

### 8.6 提交前复核

人工批准后、点击提交前必须重新读取当前账号，并与准备证据完全一致。`begin_submitting` 的最后一个 Store CAS 必须同时校验 Campaign 为 `running`、Campaign `identity_generation` 有效，并且 Assignment 与 account evidence 的 generation 都等于 Campaign 当前 generation。若窗口中账号在批准后发生变化：

- 通过一个 Store 事务暂停 Campaign 和相关 Assignment、使该 Campaign 全部未消费批准失效、使当前 `identity_generation` 失效、增加 Assignment revision，并清除当前可执行 preparation evidence。
- 错误使用 `tiktok_identity_changed`。
- 不点击提交，不自动重试。

恢复必须由用户显式触发完整 Campaign 账号预检。预检成功后产生新 generation，一次性替换所有非终态 Assignment 的运行时账号冻结；旧批准不恢复，相关 Assignment 必须重新准备。若已有已发布 Assignment，其 Receipt 中的账号身份保持不可变；新预检扫描非终态窗口，并将结果与已发布 Receipt 的账号键一起做全 Campaign 唯一性校验。

新 Campaign 使用同一窗口时必须重新读取账号，不复用上一个 Campaign 的冻结结果。

## 9. 错误与用户提示

`allocation_unsatisfied` 继续作为 HTTP 422 和分配器的稳定总错误码。错误 envelope 增加严格白名单 `details`：`reason` 必填，按原因允许 `required_count`、`eligible_count`、安全步骤标签和脱敏窗口名；不得加入任意内部对象。UI 使用该详情给出具体原因。允许的 `reason` 包括：

- `insufficient_profiles`：需要 N 个，当前可用 K 个。
- `unknown_profile_ref`：所选窗口已失效，请重新选择。
- `profile_disabled`：所选窗口已禁用。
- `profile_unhealthy`：所选窗口已隔离。
- `profile_in_cooldown`：所选窗口仍在冷却。
- `profile_tag_mismatch`：没有足够窗口满足步骤标签。
- `profile_language_mismatch`：没有足够窗口满足语言要求。
- `complete_matching_not_found`：候选总数足够，但无法形成一对一完整匹配。

详情只能包含计数、步骤标签或脱敏窗口名；不得包含原始 AdsPower ID、用户名之外的账号敏感信息、Cookie、API Key、CDP/WebSocket 地址或异常原文。

## 10. 数据兼容

- 不把 `profile_ref` 改成账号 ID。
- 不改变既有 Assignment 的窗口唯一约束。
- 既有 Profile metadata 数据保留。
- 新规划忽略 `expected_username/login_verified`，但旧记录和旧 Campaign 快照不删除。
- 新增的运行时账号证据写入 Assignment preparation evidence，不建立“窗口永久绑定账号”的主表关系。
- 已锁定 Campaign 继续使用其冻结窗口和 Assignment；执行时仍必须重新读取并冻结当前账号。
- 已锁定 Campaign 的 Profile 不能 override；修改窗口选择必须取消或弃用旧 Campaign 后新建。
- `duplicate_tiktok_account`、`tiktok_login_required`、`tiktok_identity_unavailable` 和 `tiktok_identity_changed` 必须加入后端稳定错误码、固定中文消息、OpenAPI 与错误码文档；未知异常不得降级为这些业务码。
- 账号预检冻结、重复暂停、身份变化和审批失效必须通过 Store 事务及 revision CAS 完成，禁止 service 层多次写入形成部分状态。
- Campaign 和所有非终态 Assignment 保存同一 `identity_generation`；该字段只表示本 Campaign 的运行时账号快照版本，不建立跨 Campaign 的账号绑定。
- 为避免复用不可变的 plan snapshot，`comment_campaigns` 与 `comment_assignments` 各新增一个非空整数 `identity_generation`，缺省为 `0`。旧 SQLite 使用幂等 `PRAGMA table_info` + `ALTER TABLE ... ADD COLUMN ... DEFAULT 0` 迁移；不新增表，不改历史 Receipt。generation `0` 表示尚未完成运行时账号预检，不能进入审批或提交。
- generation 失效时继续单调递增 Campaign generation，使旧任务与当前值不一致；不得重置为 `0` 或复用旧 generation。

## 11. 测试与验收

所有自动测试必须使用 Fake AdsPower、Fake 页面、Fake Redis/队列，并安装真实提交与真实网络 tripwire。

### 11.1 健康与缓存

- AdsPower 在 1 秒后、4 秒内返回时显示可用。
- 连接拒绝、超时、鉴权失败、非 JSON、业务错误分别得到固定脱敏原因。
- 健康探针只请求一个 Profile，不启动窗口。
- 保存新 AdsPower 设置后无需重启即可在下一次探测使用。
- 在线同步失败时仍返回缓存的 21 个 Profile，不返回空数组。
- cache-only GET 不访问 AdsPower；显式同步失败返回 `stale=true`、原缓存和安全原因。
- 五秒轮询不触发全量 Profile 同步。
- sync/preview 在 legacy 模式覆盖未登录、operator、管理员缺 CSRF 和带 CSRF；local-direct 覆盖 foreign Host/REMOTE_ADDR 在 service 构造前拒绝。

### 11.2 自动与手动分配

- 三步评论树、21 个缺省合格窗口时，自动模式稳定选择 3 个。
- 构造“简单取前 N 失败、完整二分匹配可成功”的 Hall 反例，自动推荐必须返回可完整匹配的最少集合。
- 用户可替换自动结果；手动选择提交精确隐藏 `profile_ref`。
- 手动候选池 `M>N` 时只打开并冻结最终匹配到 Assignment 的 N 个窗口。
- 规划不要求用户名或历史登录验证。
- disabled、unhealthy、cooldown、标签和语言条件仍生效；地区不参与本次匹配。
- Profile 数不足及 Hall 匹配失败显示不同原因。
- 未知或过期 `profile_ref` 被后端拒绝。

### 11.3 运行时账号

- 同一窗口在 Campaign A 读取账号甲，在 Campaign B 读取账号乙；两次分别冻结，不互相覆盖。
- 三个不同窗口读取三个不同账号后进入人工审批。
- 两个窗口读取同一账号时 Campaign 暂停，提交点击计数为零。
- 跨批次第 1 个与第 4 个窗口读取同一账号时，整个 Campaign 的审批计数和提交点击计数均为零。
- 两个并发预检只有一个 revision CAS 成功，不产生部分身份冻结。
- 预检中任一窗口未登录、身份缺失或身份多义时整个 Campaign 暂停。
- 批准后账号变化使 Campaign generation 失效、全部未消费批准失效，提交点击计数为零。
- 重新预检生成新 generation；旧 generation 的排队 job 在 `begin_submitting` CAS 处无操作退出。
- 旧 generation 的 prepare job 在任何文字输入前无操作退出，输入计数为零。
- Receipt 和父评论定位使用本 Campaign 运行时冻结用户名，不读取 Profile metadata 历史用户名。
- 已锁定 Campaign 的换窗请求被拒；修正同一窗口登录后可显式重新准备。
- 运行时证据和公共 API 不包含原始 AdsPower ID、Cookie、API Key 或 CDP 地址。

### 11.4 非目标回归

- 评论树 threaded/independent 规划保持不变。
- Campaign 人工审批、父回执依赖、租约、关闭确认和批次大小保持不变。
- 已有锁定前手动 Profile override 继续可用；锁定后仍拒绝换窗。
- 测试不启动真实 AdsPower Profile，不访问 TikTok，不发布评论。

## 12. 成功标准

- 正常但响应稍慢的本机 AdsPower 不再频繁被误报为不可用。
- AdsPower 真正离线时，用户仍能查看缓存窗口并创建/编辑草稿，但无法开始真实准备。
- 无需预先填写 TikTok 用户名，21 个正常窗口可直接用于三步评论树的规划。
- Campaign 创建支持自动最少选窗和手动调整，不要求用户理解或输入内部 ID。
- 实际账号只在准备阶段读取；重复、缺失或变化时一律在提交前安全暂停。
