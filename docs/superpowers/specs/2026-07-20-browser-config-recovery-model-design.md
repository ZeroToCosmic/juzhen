# 浏览器接管、配置恢复与模型选择设计

## 背景与根因

本次改造处理五项相互关联的维护需求：移除失去作用的总览页面、修复浏览器窗口无法进入 TikTok、恢复 R2 配置、增加可扩展模型下拉，以及修复执行策略返回 400 且不启动窗口的问题。

只读排查得到以下证据：

1. 最新真实 `open_tile` 日志显示两个 AdsPower Profile 均在 CDP 就绪检查阶段超时，结果为 `started: 0`、`navigation: []`。因此问题发生在导航 TikTok 之前。
2. AdsPower Local API 当前可用，但相关 Profile 处于 `Inactive`。
3. `/api/browser/execute-strategy` 当前只读取内存中的 `ACTIVE_BROWSER_SESSIONS`，不会启动未打开窗口。`open_tile` 失败后没有有效 ws 会话，因此接口返回 400。
4. 当前 `config.json` 中 R2 字段全部为空，且项目内没有可用的配置备份。动作配置和自动策略接口使用 `save_settings(partial_payload)` 保存局部数据，而该函数会从默认配置重建整份配置，导致未包含的 R2、AdsPower、代理等字段被默认空值覆盖。
5. 现有 `models.items[]` 已支持 Grok、DeepSeek 等供应商，但页面只提供供应商下拉和模型名称文本输入，没有模型预设下拉，也没有统一的可扩展预设注册表。

## 目标

- 删除总览导航、面板及对应无用事件，GUI 默认进入集中配置。
- “执行选中策略”成为完整的一键流程：启动、等待、平铺、导航、清理旧 Tab、执行策略。
- 单个窗口失败不影响其他成功窗口，窗口级错误不再造成整体 400。
- 恢复已知 R2 配置并阻止局部保存再次覆盖其他配置。
- 提供 Grok、DeepSeek 和自定义模型的供应商/模型两级下拉，并允许后续快速扩展。
- 增强日志、配置备份和错误可见性。

## 方案选择

### 采用：后端统一编排

`/api/browser/execute-strategy` 负责完整生命周期。前端只发送选中的 Profile 和策略 ID，后端针对每个 Profile 执行启动或复用、CDP 等待、页面准备和策略运行。

该方案优于前端串联多个接口，因为它能在页面刷新、请求重试和部分窗口失败时保持一致的错误模型，并避免前端重复实现窗口生命周期逻辑。

### 未采用：前端串联

前端先调用 `/api/browser/open-tile`，再调用 `/api/browser/execute-strategy`。实现较少，但两个请求之间可能丢失状态，且页面刷新后无法恢复执行上下文。

### 未采用：保持手工两步

仅改善错误提示，仍要求用户先打开窗口。该方案不满足“执行策略时自动启动窗口”的已确认行为。

## 浏览器执行架构

### 请求与会话

前端继续发送：

- `strategy_id`
- `windows[]`，包含 `profile_id`、`profile_no` 和显示名称
- 可选 metadata

后端为每个 Profile 执行：

1. 检查 `ACTIVE_BROWSER_SESSIONS` 中是否存在会话。
2. 若会话存在，调用 CDP 健康检查；失效则移除旧会话并进入启动流程。
3. 若会话不存在，调用 AdsPower Local API 启动 Profile。
4. 最多进行三次启动/连接尝试。失败后查询 Profile 实际状态，决定继续等待、复用新地址或停止残留实例后重启。
5. CDP 就绪后登记会话。
6. 对本次成功窗口统一平铺。
7. 关闭旧 Tab，只保留并导航到集中配置中的默认网址，默认是 `https://www.tiktok.com/`。
8. 校验最终 URL 非空且不是 `about:blank`，并确认页面已具备可操作文档。
9. 执行选中的自动或手动策略。

### 返回规则

- 请求格式错误、目标 URL 非法、策略 ID 不存在等整体错误继续返回 HTTP 400。
- Profile 启动、CDP、导航、页面或动作失败属于窗口级结果，接口返回 HTTP 200，并在 `results[]` 中标记失败。
- 成功窗口继续执行，不受其他窗口失败影响。
- 每个结果包含 `profile_id`、`status`、`stage`、`attempts`、`target_url` 和可读原因。
- ws.puppeteer 地址只用于后台调用，不在 GUI 展示。

## 日志设计

- 每次执行生成一个 `task_id`，贯穿启动、平铺、导航和策略执行。
- 阶段枚举为：`session_check`、`start_browser`、`wait_for_cdp`、`tile_windows`、`navigate`、`execute_strategy`。
- 浏览器日志记录每个窗口的阶段、尝试次数、错误类别和结果。
- GUI 直接显示失败窗口、失败阶段和原因，并保留 `/api/browser/logs` 查询入口。
- 日志不得输出 AdsPower API Key、R2 密钥、模型 API Key 或完整 ws 地址。

## 配置持久化与 R2 恢复

### 保存语义

- `save_settings()` 只用于保存完整配置对象。
- 所有局部接口统一调用 `update_settings()`，与当前配置做深度合并后再原子替换文件。
- 空密码字段不覆盖已有凭据。
- R2 的 `public_base_url` 和 `prefix` 也加入空值保护规则。

### 备份与恢复

- 每次成功覆盖 `config.json` 前，将上一版本保存为带时间戳的备份。
- 保留有限数量的最近备份，避免无限增长；默认保留五份。
- 配置解析失败时不再静默显示空配置：API 和 GUI明确提示错误及备份路径。
- 集中配置页面提供“从最近备份恢复”按钮；恢复前再次备份当前文件。

### R2 数据恢复范围

根据用户此前提供的信息恢复以下已知字段，但设计文档和日志中不记录明文密钥：

- Account ID
- Account Token
- Access Key ID
- Secret Access Key
- Bucket：`tiktokvideo`
- S3 API Endpoint

以下字段没有可靠历史值，保持为空并在 GUI 标记为可选：

- Public Base URL
- Prefix

由于凭据曾在对话中以明文出现，恢复完成后应提示用户在 Cloudflare 轮换 API Token 和 S3 访问密钥。

## 总览页面移除

- 删除主导航中的“总览”。
- 删除 `panel-overview` 及其专属按钮。
- 删除 `overview-status`、`overview-next` 事件绑定和仅供总览使用的渲染逻辑。
- 主页面默认激活“集中配置”，标题和初始加载流程同步调整。
- 不删除仍被其他页面引用的状态接口或账号获取接口。

## 模型选择与扩展

### 数据结构兼容

继续使用现有：

- `models.default_model_id`
- `models.items[].id`
- `models.items[].provider`
- `models.items[].enabled`
- `models.items[].base_url`
- `models.items[].api_key`
- `models.items[].model`
- `models.items[].mode`

### 模型预设注册表

新增独立的供应商预设注册表。每个供应商包含：

- 稳定的 provider ID
- 显示名称
- 默认 Base URL
- 默认调用模式
- 模型候选列表

首批供应商：

- Grok：使用现有 xAI Base URL、Responses 模式及当前项目默认模型。
- DeepSeek：使用现有 DeepSeek Base URL、Chat Completions 模式，提供 `deepseek-chat` 与 `deepseek-reasoner`。
- Custom：允许手工填写 Base URL、模型名称和调用模式。

### 页面行为

- 第一层下拉选择供应商。
- 第二层下拉显示该供应商的模型预设，并包含“自定义模型”。
- 切换供应商时自动填充默认 Base URL 和调用模式。
- 已有 API Key 永不因切换供应商或空表单提交而被覆盖。
- 选择“自定义模型”时显示模型名称、Base URL 和模式输入。
- 注册新供应商只需增加注册表条目；页面和请求客户端通过同一标准字段读取，不增加供应商专用分支。
- 现有 Grok 直连配置继续兼容，后续可逐步桥接到统一模型池，本次不强制迁移。

## 错误处理

- AdsPower API 不可用：窗口结果标记 `start_browser`，提示检查客户端和 Local API。
- AdsPower 返回空 ws：标记 `start_browser`，不进入 CDP 等待。
- CDP DNS、拒绝连接、403、超时分别归类，保留原始安全错误摘要。
- 导航后仍为空白页：标记 `navigate`，不执行策略。
- 配置文件损坏：停止使用默认空值覆盖原文件，提示从备份恢复。
- 模型预设配置不完整：保存时阻止提交，并指出缺失字段。

## 测试策略

### 浏览器流程

- 未打开窗口点击执行策略时会自动启动。
- 有效会话会被复用，失效会话会重新启动。
- 单窗口失败时其他窗口继续执行，接口不返回整体 400。
- CDP 未就绪、导航失败和 blank 页面不会进入策略。
- 成功路径会平铺、关闭旧 Tab、进入 TikTok 后执行策略。

### 配置

- 保存动作配置或自动策略不会清空 R2、AdsPower、代理和模型配置。
- 空密码字段不会覆盖已有密钥。
- 保存前创建备份，保留数量受限。
- 配置损坏时能检测并从最近备份恢复。
- 已知 R2 字段能持久化并在重启后回填。

### UI 与模型

- 总览入口和面板不存在，集中配置默认激活。
- Grok、DeepSeek 和 Custom 供应商可选。
- 供应商切换能更新模型选项、Base URL 和模式。
- 选择自定义模型后可以保存并被现有模型调用链选中。
- API Key 不在页面源码、日志或测试输出中泄露。

## 验收标准

- 点击“执行选中策略”无需先点击“打开窗口”。
- 成功启动的窗口进入指定 TikTok 页面并执行策略；失败窗口显示具体阶段和原因。
- 不再因单个窗口失败返回整体 400。
- 保存任意局部设置后，R2、AdsPower、代理和模型配置保持不变。
- 重启后 R2 已知字段仍存在。
- 主页面不再显示总览，默认进入集中配置。
- 模型页面可通过下拉选择 Grok、DeepSeek 或自定义模型，并可按注册表扩展。
