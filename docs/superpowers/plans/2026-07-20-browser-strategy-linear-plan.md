# 浏览器接管与线性执行策略 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 AdsPower 窗口打开后的目标网址导航，并把执行策略配置改造成能直接对应运行流程的线性配置，同时保证多窗口平铺不重叠且置于桌面前台。

**Architecture:** 在浏览器策略 API 的每个窗口执行前统一经过“等待 CDP → 导航并关闭其他 Tab → 校验目标页”准备阶段；保留现有策略 JSON/API 字段作为兼容层，新增线性 UI 只负责编辑同一份配置。窗口布局由独立的整数矩形计算函数生成，再由 Windows API 设置、置顶并读取实际矩形校验。

**Tech Stack:** Python 3、Flask、Playwright/CDP、AdsPower Local API、pywin32、现有浏览器策略运行时、现有 Node/浏览器界面测试。

## Global Constraints

- 继续支持 AdsPower 模式；当 AdsPower API 地址已配置时，`CDP 地址`只作为非 AdsPower 手工接管场景使用，界面明确说明可留空。
- 默认网址继续使用 `https://www.tiktok.com/`，但以集中配置中的值为准。
- 不删除现有自动策略字段、元素排序、文案库和接口字段，避免重启后或旧配置无法读取。
- 不在界面展示 ws.puppeteer 调试地址、原始 XPath 调试框或浏览器命令行输出。
- 所有窗口操作失败都要带阶段信息，写入现有浏览器日志，并让接口返回可定位的失败原因。
- 每次测试都使用项目现有虚拟环境和测试入口；不把真实 AdsPower、Redis 或 MySQL 连接写进自动化测试。

## Task 1: 为策略执行增加目标网址准备阶段

**Files:** `gateway/app.py`, `browser_cdp.py`, `tests/test_app.py`, `tests/test_browser_cdp.py`（若存在对应测试文件）。

- [ ] 先补充失败测试：自动策略执行必须按 `wait_for_cdp → navigate_and_close_other_tabs → run_auto_strategy_on_cdp` 顺序调用；导航失败时不得进入策略运行时；手动策略也必须经过同样准备阶段。
- [ ] 在 `gateway/app.py` 增加一个小型内部辅助函数，例如 `prepare_browser_page(ws_url, target_url)`：等待 CDP 就绪，调用现有导航封装，确认返回 URL 非空且不是 `about:blank`，否则抛出带阶段信息的异常。
- [ ] 在 `/api/browser/execute-strategy` 的自动和手动分支调用该辅助函数，目标网址读取 `browser.default_url`，空值时使用 TikTok 默认地址。
- [ ] 将等待 CDP、关闭 Tab、导航、策略执行分别记录为可读阶段；失败响应中包含 profile 标识、阶段、目标网址和原始原因。
- [ ] 补充 CDP fallback 的返回值，使其包含最终 `current_url`，并在 Playwright 与原生 CDP 两条路径都关闭除目标页外的 Tab。
- [ ] 运行相关 API/CDP 测试，确认目标页准备成功后才执行动作。

## Task 2: 加强输入与动作完成校验

**Files:** `browser_strategy_runtime.py`, `tests/test_browser_strategy_runtime.py`（按现有测试文件布局调整）。

- [ ] 先补充失败测试：输入后必须能在输入控件值或页面可见文本中找到完整文案；缺失时返回失败而不是成功；每轮达到滚动阈值后计数必须归零并进入下一轮。
- [ ] 修改输入验证，兼容 textarea、input、contenteditable 和页面回显文本；保留现有逐字输入及随机延迟。
- [ ] 保持有序点击、随机文案、提交点击的结果字段；每个元素必须先确认可见并完成点击，文案必须确认存在，再允许该交互阶段成功。
- [ ] 对验证失败抛出包含元素别名/阶段的异常，让上层日志能指出是哪个元素或文案失败。
- [ ] 运行运行时单元测试，确认分钟级总时长、滚动分段和交互循环行为未被破坏。

## Task 3: 实现精确的多窗口等面积平铺与前台显示

**Files:** `window_tiler.py`, `tests/test_window_tiler.py`（按现有测试文件布局调整）。

- [ ] 先补充纯函数测试：给定 N=1..8 和屏幕宽高，所有矩形面积尽量等分，矩形不相交，覆盖区域不超出屏幕；实际窗口数量不足时只布局实际找到的窗口。
- [ ] 增加可测试的布局函数，例如 `build_layout_cells(count, width, height)`，采用接近方形的行分组和整数累计边界，避免最后一行产生半屏窗口或尺寸漂移。
- [ ] 保留现有 profile/AdsPower 窗口筛选逻辑和实际矩形读取校验。
- [ ] 设置窗口时移除 `SWP_NOZORDER`，调用 `BringWindowToTop`、`SetForegroundWindow`（失败时记录但不掩盖定位结果），并用 `SW_RESTORE` 确保最小化窗口恢复。
- [ ] 返回每个窗口的目标矩形与实际矩形、置顶结果和重叠检查结果；接口失败时带具体窗口句柄/profile 信息。
- [ ] 运行窗口布局单元测试；Windows API 集成校验只在 Windows 环境执行，不能运行时跳过纯布局测试。

## Task 4: 将执行策略页面改成线性可编辑流程

**Files:** `gateway/app.py`, `tests/test_app.py` 或现有前端测试文件。

- [ ] 先补充界面回归测试：策略页出现线性步骤容器；每个步骤只有一行核心配置；无“策略 JSON 输入”“模型生成要求”“动作策略（JSON 数组）”和 XPath 调试框；元素列表默认只显示自定义名称，编辑时才显示 XPath。
- [ ] 将策略配置区重排为 1–9 步：总执行时长、初始停留、单次滚动次数/间隔、滚动阈值/距离、阈值后停留、有序点击元素、文案库引用、输入/提交元素、批量窗口数。
- [ ] 保留现有字段 ID 和保存/编辑/删除/复制接口，采用同一字段绑定，保证旧策略可继续加载；为新增行提供清晰单位和范围说明，随机参数均支持最小值、最大值和小数间隔。
- [ ] 元素管理列表使用高对比度自定义名称，XPath 只在新增/编辑弹窗或编辑行中显示；排序结果仍写入 `action_elements`，供策略引用。
- [ ] 在浏览器接管执行策略选择器中继续读取自动策略列表，保存/生成后的策略立即可被引用；页面不展示 ws 地址或调试输出。
- [ ] 更新集中配置的浏览器提示：AdsPower API 地址填写 `http://local.adspower.net:50325`；AdsPower 模式下手工 CDP 地址留空；默认网址明确会在每个窗口策略执行前重新打开并清理旧 Tab。
- [ ] 运行前端/接口回归测试，确认保存、编辑、删除、排序和引用仍可用。

## Task 5: 集成验证与交付检查

**Files:** `tests/test_app.py`, `tests/test_browser_strategy_runtime.py`, `tests/test_window_tiler.py`（按实际布局调整）。

- [ ] 增加一个接口级测试，模拟多个 profile：其中一个导航失败，响应只标记该窗口失败，其余窗口继续执行；日志包含目标网址和失败阶段。
- [ ] 运行 Python 全量测试：
  ```powershell
  .\.venv\Scripts\python.exe -m pytest -q tests -p no:cacheprovider
  ```
- [ ] 运行现有 Node/浏览器界面测试（先读取项目 `package.json` 或测试说明确定实际入口）。
- [ ] 运行语法检查：
  ```powershell
  .\.venv\Scripts\python.exe -m py_compile gateway\app.py browser_cdp.py browser_strategy_runtime.py window_tiler.py
  ```
- [ ] 手工验收：AdsPower 服务运行、集中配置中 AdsPower API 地址正确、手工 CDP 地址留空、默认网址为 TikTok；选择 4 个窗口点击“打开窗口”，确认四个窗口无重叠、覆盖四个区域、位于前台且均显示目标页，再执行策略并查询 `/api/browser/logs`。
- [ ] 汇总无法在本机验证的外部条件（AdsPower 服务、TikTok 网络可达、Windows 前台权限）以及实际日志路径，不把外部环境问题误报为代码通过。

## Completion Criteria

- [ ] 执行策略前每个窗口都清理旧 Tab 并成功导航到集中配置目标网址，blank 页不会进入动作执行。
- [ ] 1–8 个窗口布局无重叠、等面积、实际矩形经过校验并尝试置顶。
- [ ] 策略页面按线性流程呈现，所有随机参数可配置/编辑，元素只默认显示名称，策略可被浏览器接管引用。
- [ ] 输入、提交和有序点击都有强校验，失败原因进入浏览器日志。
- [ ] 自动化测试和语法检查通过，外部依赖限制被明确记录。
