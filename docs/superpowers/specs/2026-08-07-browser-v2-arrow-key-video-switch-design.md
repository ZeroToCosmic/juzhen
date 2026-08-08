# Browser V2 方向键切换视频设计

日期：2026-08-07  
状态：方案已确认，等待规格复核

## 1. 问题结论

滚轮校准 revision 2 已通过单 Profile Dry-Run，并保存单事件 `deltaY=150`；随后两个执行 Profile 都返回 `calibrated_video_switch_not_observed`。这证明物理滚轮样本、Playwright wheel 像素事件和 TikTok 单视频切换之间不存在可靠的一一映射，继续调整倍率不能建立稳定机制。

Playwright 文档说明 `mouse.wheel()` 仅派发 wheel 事件，参数单位为像素，且方法不等待滚动完成。TikTok 网页自动化已有使用 `page.keyboard.press("ArrowDown")` 切换下一视频并验证当前视频的实践。

## 2. 目标

- V2“切换视频”动作使用 `ArrowDown` 或 `ArrowUp`。
- 每次按键只请求切换一个视频。
- 每次按键后必须验证视频身份发生一次稳定变化。
- 支持多个 AdsPower Profile 并发，各 Page 独立发送按键。
- 保留现有策略、动作 ID、次数和间隔配置，不迁移用户策略。
- 不再要求滚轮校准。

## 3. 非目标

- 不再模拟物理滚轮。
- 不使用 wheel burst、CDP scroll gesture 或 Windows `SendInput`。
- 不点击 TikTok 下一视频按钮作为 fallback。
- 不删除历史校准表或记录。
- 不重构其他四类积木动作。

## 4. 动作语义

保持持久化动作类型 `scroll`，避免策略迁移。管理界面把动作名称从“滚动”改为“切换视频”。现有字段继续使用：

- `direction=down`：发送一次 `ArrowDown`。
- `direction=up`：发送一次 `ArrowUp`。
- `count`：随机抽取本轮切换次数。
- `interval_seconds`：每次成功切换后的等待范围。
- 历史 `distance_pixels` 字段继续由兼容层忽略，不恢复 UI 输入。

## 5. 单次切换流程

1. 捕获当前视频身份。
2. 若 `document.activeElement` 是 `input`、`textarea`、`select` 或 `contenteditable`，仅调用 `blur()`，防止方向键被评论输入框消费。
3. 调用一次 `page.keyboard.press("ArrowDown")` 或 `page.keyboard.press("ArrowUp")`。
4. 最多等待 8 秒，轮询当前视频身份。
5. 新身份连续两次相同后，记录本次切换成功。
6. 未观察到变化时，返回 `video_switch_not_observed`，停止当前 Profile；不补按第二次。

一次策略请求多次切换时，只有前一次验证成功后才等待配置间隔并发送下一次按键。

## 6. 校准功能退役

- 创建任务时不再读取或冻结 `wheel_calibration`。
- 没有校准版本也允许运行包含“切换视频”的策略。
- 页面点选器隐藏“滚轮校准”按钮、状态与版本信息。
- 现有 wheel calibration HTTP 路由、SQLite 表和历史记录暂时保留但不调用，避免扩大改动及删除用户数据。
- 执行结果不再写 `calibration_revision`、`wheel_events` 或 `distance_pixels`；保留 `requested_switches`、`completed_switches` 和脱敏前后视频身份。

## 7. 错误与隔离

- `video_switch_not_observed`：一次方向键后 8 秒内未观察到稳定的新视频。
- `video_switch_state_capture_failed`：按键前或验证期间无法读取当前视频。
- `strategy_paused_during_execution`：沿用现有暂停语义。

某 Profile 失败只停止该 Profile。同批其他 Profile 继续运行；批次结束仍关闭全部窗口并确认释放。

## 8. UI

- 动作库显示“切换视频”，不显示“滚动像素”或“滚轮校准”。
- 编辑字段只显示方向、切换次数范围、成功后的间隔范围。
- 帮助文字：“每次发送一个方向键，并在确认视频切换后继续。”
- 历史结果显示计划次数、完成次数、失败码；不再显示校准版本。

## 9. 验收标准

- 单 Profile：一次 `ArrowDown` 准确切换一个视频。
- 双 Profile：两个窗口各自切换一个视频，不依赖前台焦点。
- 输入框有焦点时：先失焦，再切换视频，不向输入框写入或移动光标。
- `count=3` 时：严格发送三次方向键，每次都在上一次验证成功后发送。
- 首次按键失败时：只发送一次，返回 `video_switch_not_observed`。
- 无滚轮校准版本时：策略仍可创建和执行。
- 原有非滚动动作和批次关闭逻辑不变。

## 10. 测试范围

- 单元测试：方向映射、输入焦点失焦、一次按键、稳定身份验证、失败不补按、多次顺序执行。
- 服务测试：包含 `scroll` 动作的任务不再要求校准快照。
- UI 测试：动作名称和帮助文字、校准入口隐藏。
- 回归：V2 Python 测试、V2 前端测试、双 Profile 真实验收。

## 11. 参考

- Playwright Mouse：`https://playwright.dev/docs/next/api/class-mouse`
- Chrome DevTools Protocol Input：`https://chromedevtools.github.io/devtools-protocol/tot/Input/`
- TikTok Playwright ArrowDown 示例：`https://inspectelement.org/browser_automation.html`
