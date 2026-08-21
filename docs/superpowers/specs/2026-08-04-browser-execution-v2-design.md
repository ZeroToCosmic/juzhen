# 浏览器执行策略 V2 设计

## 背景

执行策略核心：通过 AdsPower Local API 启动浏览器 Profile，Playwright 通过 CDP 接管页面，再按用户保存顺序执行鼠标移动、滚轮滚动、鼠标点击、键盘输入和等待。

后续为解决元素定位，系统叠加 A11y 树、LLM、探针、两 Profile 两轮验证、自愈、版本发布、Redis、策略闸门、告警和评论区专用逻辑。附属功能侵入核心后，页面准备、元素发现和策略执行互相阻塞。

V2 重建最小可靠核心。目标元素由用户在真实 AdsPower 页面直接点选；系统不猜目标，不自动修复失效定位器。

## 已确认决策

1. 真实 AdsPower 页面直接点选元素。
2. 每个元素保存多组已验证定位器；全部失效时停止受影响 Profile，要求人工重新点选。
3. 策略使用积木编辑器；动作可重复添加、复制、删除和任意排序。
4. 动作仅有移动、滚动、点击、输入、等待。
5. 复用现有 ghost-cursor@1.4.2、Node Worker、Python Bridge、human_move_to() 和 human_type()。
6. 策略支持一次执行或按设定时长循环。
7. 任务启动时选择批次大小，默认 3。
8. 每批完成后保存结果并关闭全部 Profile；确认关闭后才启动下一批。
9. 失败 Profile 截图、记录、关闭；不阻塞同批其他 Profile。
10. 每个策略必须配置页面就绪标志元素。
11. 定位或动作失败后不刷新、不重放已完成动作。
12. V2 使用全新数据；V1 只读备份，不自动迁移。
13. 管理后台仅本机使用，取消账号密码登录，直接打开。

## 目标

- Profile、AdsPower WebSocket、Playwright Context 和 Page 一对一绑定。
- 用户点选后保存精确、唯一、可验证的定位器。
- 五类动作任意积木组合。
- 分批处理数百 Profile，默认每批 3 个。
- 每批窗口全部关闭后才进入下一批。
- 每个 Profile、每个动作都有可读状态、错误和截图。
- 核心执行不依赖 LLM、Redis、探针、自愈或发布服务。

## 非目标

- 不扫描整页元素目录。
- 不用语义契约或 LLM 猜用户目标。
- 不按页面位置寻找替代元素。
- 不自动修复选择器。
- 不刷新后重放策略。
- 不录制真人动作。
- 不增加条件、分支或嵌套循环。
- 不自动迁移 V1。
- 不支持无认证局域网或公网部署。

## 开源技术

### AdsPower Local API

官方 Local API负责查询、启动、状态、取得 ws.puppeteer 和关闭。所有请求经过单一适配器及限速队列，遵守每秒最多一次调用。

### Playwright

继续使用 Python Playwright：

- chromium.connect_over_cdp() 接管 AdsPower 浏览器。
- Locator完成唯一、可见、可操作检查。
- page.mouse 和 page.keyboard 执行动作。
- 定位器优先级参考 Playwright Codegen。
- 只用公共 API，不依赖私有 InjectedScript 或 generateSelector()。

### Cypress Unique Selector

引入并锁定 @cypress/unique-selector@2.2.0，生成唯一 CSS 保底定位器。它不决定最终执行目标。

### XPath Finder

点选交互参考 MIT 许可 trembacz/xpath-finder：十字光标、悬停高亮、点击捕获。实现时从该项目抽取并测试最小相对 XPath 生成函数，保留许可证和来源说明，只作为最低优先级保底。

### 拟人化

- ghost-cursor继续由现有常驻 Node Worker生成轨迹。
- Python Bridge和 actions_dom.human_move_to() 保持现有边界。
- actions_dom.human_type() 继续提供逐字输入和随机间隔。
- 禁止开发第二套轨迹或输入节奏。

## 架构

~~~
管理界面
  执行中心 / 元素库 / 策略库 / 运行历史 / 系统设置
      |
V2 API与SQLite Store
      |
批次调度器
      |
AdsPower适配器与限速器
      |
Playwright CDP会话
      |
页面准备与就绪检查
      |
严格元素解析器
      |
五类动作执行器
      |
结果、日志、截图、关闭确认
~~~

边界：

- AdsPower适配器管理Profile生命周期，不执行网页动作。
- 会话管理器维护Profile、WebSocket、Context、Page绑定。
- 点选器捕获用户选择，不执行策略。
- 元素解析器验证定位器，不自愈。
- 动作执行器执行单个动作，不调度批次。
- 调度器管理批次，不理解DOM。

## 元素点选器

### 流程

1. 元素库点击“点选新元素”。
2. 选择AdsPower Profile和目标URL。
3. 系统启动或复用Profile并连接CDP。
4. 用户手动登录、切换视频、展开评论区、等待动态内容。
5. 用户点击“开始点选”。
6. 悬停显示边界、标签、role、文本和可操作祖先。
7. 点击目标。
8. 从 event.composedPath() 识别原始节点和最近可操作祖先。
9. 用户确认节点，填写名称、用途、类型。
10. 系统生成、验证并保存定位器。

一个点选会话可连续保存多个元素。用户结束会话后才关闭选择窗口。

### 可操作祖先

默认优先 button、a、input、textarea、select、contenteditable、交互 role及有有效点击边界的祖先。界面同时展示原始节点和祖先，避免把按钮内部 svg 或 path 存成点击目标。

### 定位器优先级

1. data-e2e、data-testid、data-test、data-qa。
2. aria-label、role与accessible name。
3. 稳定name、placeholder和非动态ID。
4. @cypress/unique-selector唯一CSS。
5. 稳定父级约束的文本。
6. 相对XPath。

随机class、会话ID、框架内部属性、绝对XPath、单独nth-child和屏幕坐标不得作为高优先级定位器。

### 保存验证

每条运行候选必须：

- 在正确frame中匹配恰好一个节点。
- 匹配节点等于用户确认节点。
- 元素可见。
- 点击目标有非零边界。
- 输入目标可编辑。
- 连续两次边界测量稳定。

至少一条候选通过才能保存。

### 运行解析

1. 匹配0个：候选无效。
2. 匹配多个：候选歧义。
3. 唯一但不可见或不可操作：候选无效。
4. 多条有效候选指向同一节点：用最高优先级候选。
5. 多条有效候选指向不同节点：冲突失败。
6. 仅一条有效候选：允许使用。
7. 全部无效：停止Profile，要求重新点选。

禁止 locator.first()，禁止位置猜测。

## V2元素模型

~~~
id
name
purpose: action | readiness
kind: click | input | generic
url_pattern
frame_path
locators[]
diagnostic_metadata
screenshot_path
status: active | repick_required | disabled
revision
created_at
updated_at
~~~

诊断元数据只供展示，不参与相似元素匹配。元素支持重命名、重新点选、停用。重新点选生成新revision，策略继续引用同一元素ID。被策略引用的元素不能删除。

## 页面准备

每个策略必须保存目标URL、就绪元素、就绪超时。

流程：

1. 启动Profile，取得对应ws.puppeteer。
2. 建立独立CDP会话。
3. 在Context中选择唯一目标Page。
4. 导航目标URL。
5. 关闭多余Tab，不关闭目标Page。
6. 验证URL非about:blank且符合目标规则。
7. 解析就绪元素。
8. 每500ms采样；连续3次可见且边界稳定后执行。

TikTok为持续联网SPA。networkidle不能作为唯一条件；固定10秒不能替代就绪元素。

超时后截图、记录、关闭；不刷新，不执行第一个动作。

## V2策略模型

~~~
id
name
target_url
ready_element_id
readiness_timeout_seconds
run_mode: once | duration
loop_duration_minutes: [min, max]
actions[]
enabled
revision
created_at
updated_at
~~~

批次大小属于任务，不属于策略。

### 积木规则

- 五类动作均可重复。
- 每个实例参数独立。
- 支持拖拽、上移、下移、复制、编辑、删除。
- 未添加动作绝不执行。
- 不要求包含全部五类。
- 执行顺序严格等于actions[]。
- 首版无条件、分支、嵌套循环。

### 移动

参数：element_id、duration_seconds范围。解析元素边界，选择内部安全点，调用human_move_to()。只移动，不点击。

### 滚动

参数：direction、distance_pixels范围、count范围、interval_seconds范围。每次抽样距离和间隔，调用page.mouse.wheel()。

### 点击

参数：element_id、button、click_count、hold_seconds范围、after_seconds范围。严格解析目标，ghost-cursor移动到内部安全点，再执行按下、等待、释放。内部移动不额外插入可见积木。

### 输入

参数：element_id、content_source、fixed_text、content_library_id、interval_ms范围。目标必须可编辑。DOM focus聚焦，不隐式点击。固定文案直接使用；文案库每次随机抽一条。调用human_type()，输入后验证完整内容存在。

### 等待

参数：duration_seconds范围。等待后继续。

### 一次与循环

once执行一轮。duration为每个Profile抽样一次截止时间。完成一轮且未到截止时间才开始下一轮。达到截止时间后不启动新一轮。已开始动作允许完成。任意动作失败后当前Profile终止。

## 批次调度

任务输入：

~~~
strategy_id
profile_ids[]
batch_size
~~~

默认batch_size=3，不能超过系统上限。

算法：

1. 读取策略和元素revision，生成不可变快照。
2. 按选择顺序切批。
3. 串行限速启动本批Profile。
4. 等待各Profile CDP可用。
5. 并发准备页面并执行。
6. 等待本批全部进入终态。
7. 保存结果；失败Profile额外截图。
8. 串行限速关闭本批全部Profile。
9. 查询确认全部关闭。
10. 开始下一批。

Profile、ws.puppeteer、Browser、Context、Page存入同一不可变会话记录。禁止按数组下标或全局“当前页面”重新配对。

AdsPower启动、关闭临时错误最多重试2次；CDP在超时内轮询。网页定位和动作不重试，已完成动作不重放。

成功和失败Profile都关闭。截图失败不阻止关闭。某Profile连续3次仍无法关闭时，任务进入cleanup_blocked并暂停后续批次，防止窗口累积。

取消任务后不启动新Profile；当前不可中断单动作可结束；随后保存结果、关闭当前批次、进入cancelled。

## 状态

任务：

~~~
queued / running / completed / cancelled / cleanup_blocked
~~~

Profile：

~~~
queued / starting / connecting_cdp / navigating
waiting_readiness / executing / capturing_evidence
closing / succeeded / failed / cleanup_failed
~~~

UI显示中文名称，不显示unknown、暂无证据等内部字段。

## 存储

使用独立SQLite：

~~~
data/execution_v2/execution_v2.db
~~~

启用外键、事务和WAL。核心表：

~~~
elements
element_revisions
strategies
strategy_actions
execution_jobs
execution_profiles
action_results
~~~

现场文件：

~~~
data/execution_v2/artifacts/<job_id>/<masked_profile_id>/
~~~

规则：

- 元素、策略、动作保存用单事务。
- 任务保存不可变策略和元素快照。
- 运行中编辑不影响已启动任务。
- UI和目录脱敏Profile ID。
- API、日志不得泄露API Key、Cookie、密码或WebSocket凭据。
- V1不迁移，不被V2写入。

## API

新接口统一使用 /api/browser-v2：

~~~
GET/POST             /elements
PUT/DELETE            /elements/<element_id>
POST                  /elements/<element_id>/validate

POST                  /picker/start
GET                   /picker/<session_id>
POST                  /picker/<session_id>/finish
POST                  /picker/<session_id>/cancel

GET/POST              /strategies
GET/PUT/DELETE         /strategies/<strategy_id>

POST                  /jobs
GET                   /jobs/<job_id>
POST                  /jobs/<job_id>/cancel
GET                   /jobs/<job_id>/results
~~~

前端每秒轮询任务状态；不增加WebSocket、Redis或外部队列。

V2 UI不调用旧execute-strategy、action-config、auto-strategies、strategies接口。V2验收前旧代码保留回退；验收后单独清理。

## 管理界面

左侧导航：

~~~
执行中心
元素库
策略库
运行历史
系统设置
~~~

- 元素库：名称、用途、类型、定位器数、状态、引用数；支持点选、测试、重新点选、编辑、停用、删除。
- 策略库：名称、模式、动作数、就绪元素、状态。
- 编辑器：左侧动作按钮，中间有序积木，右侧当前动作参数。
- 执行中心：选择策略、Profile、批次大小；展示剩余、当前批次、成功、失败及各Profile当前阶段。
- 运行历史：任务、Profile、动作结果、错误和截图。
- 不展示repairs、publish、reconcile、lease等V2不存在字段。

## 本机直接访问

取消账号密码登录：

- create_app()不注册管理登录蓝图和认证Guard。
- /直接打开后台。
- 新UI不加载认证控制器。
- 运行时不创建、读取management.db用户或登录Session。
- 旧认证代码V2验收前保留但停止调用，避免本次同时大范围删除。
- 自动测试不模拟登录。

本机边界：

- Flask和启动器只绑定127.0.0.1。
- 拒绝非loopback请求。
- Host只允许localhost、127.0.0.1、[::1]及当前端口。
- 不允许0.0.0.0。
- 未来开放LAN或公网时必须重建认证。

## 错误处理

阶段固定：

~~~
adspower_start
cdp_connect
navigate
readiness
locate_element
execute_action
capture_evidence
adspower_stop
~~~

失败结果包含脱敏Profile、策略、动作编号、类型、阶段、元素、定位器诊断、超时、错误码、摘要、截图、关闭结果。API只返回脱敏摘要；敏感原始信息不进入响应。

## 测试

### 300虚拟Profile

FakeAdsPowerAdapter与FakeBrowserSession实现生产接口。生成300个虚拟ID，使用真实调度器和batch_size=3，共100批；不启动真实浏览器。

断言：

- 恰好100批。
- 最大活动Profile不超过3。
- 每个Profile恰好启动、执行、关闭一次。
- 本批全关闭前下一批不启动。
- 单Profile失败时其他Profile继续。
- 关闭失败进入cleanup_blocked。
- 取消后不启动新Profile。
- 使用可控事件或虚拟时钟，不用真实长sleep。

### 元素与动作

- 点击svg时可确认按钮祖先。
- 保存定位器唯一且指向原节点。
- 匹配0、多条或冲突时拒绝动作。
- frame路径一致。
- 输入目标必须可编辑。
- 被引用元素不能删除。
- 五类动作可重复、排序。
- 执行严格遵守actions[]。
- 直接调用ghost-cursor和human_type()。
- 动作失败后后续动作不执行。
- once和duration语义正确。

### API、存储、UI

- SQLite事务失败时UI不显示已保存。
- Flask重启后数据仍在。
- 任务使用启动快照。
- API脱敏。
- UI只显示V2菜单和可读状态。
- 后台直接打开，无登录页。
- 非loopback和异常Host被拒绝。

### 真实验收

1. 6个普通AdsPower测试Profile：验证两批、每批3个的启动、绑定、导航、关闭、批次顺序；不要求全部登录TikTok。
2. 至少2个已登录TikTok专用Profile：验证真实点选、就绪和完整动作。
3. 有条件再用6个已登录Profile做压力验收，不作为核心前置。

必须确认：

- 无错误WebSocket、Context、Page绑定。
- 无“一窗口两个TikTok，另一窗口空白”。
- 就绪稳定后才执行。
- 策略可包含重复滚动、等待。
- 选择器失效时不点相似元素。
- 单Profile失败不影响其他Profile。
- 所有窗口最终关闭。
- 第一批全关闭前第二批不启动。
- 历史页明确显示执行和失败阶段。

## 分阶段交付

1. 独立V2核心：SQLite、AdsPower适配器、会话绑定、批次调度、300虚拟Profile测试。
2. 点选与解析：覆盖层、多定位器、元素CRUD和就绪元素。
3. 积木与执行：五类动作、ghost-cursor、human_type()、循环、证据。
4. UI与真实验收：五页面、本机直开、6普通Profile、2登录Profile。
5. V2验收后单独清理：旧UI、接口、探针、LLM、自愈、版本、闸门、Redis耦合和停止调用的认证模块。

旧模块清理不与V2核心混在同一提交，便于回滚。

## 验收标准

1. 后台本机直接打开，无账号密码。
2. 用户可在真实AdsPower页面点选并命名元素。
3. 保存定位器经过唯一、同节点、可见和可操作验证。
4. 匹配0、多个或冲突时不执行。
5. 五类积木可任意重复和排序。
6. 鼠标轨迹和输入复用现有实现。
7. 支持once和duration。
8. 300虚拟Profile、每批3个测试通过。
9. 6真实普通Profile完成两批启停验收。
10. 至少2个真实登录Profile完成点选和动作验收。
11. 本批全关闭前下一批不启动。
12. 单Profile失败不影响其他Profile，截图和日志完整。
13. V2不依赖LLM、Redis、探针、自愈、发布或策略闸门。
14. V1数据未修改或自动迁移。
