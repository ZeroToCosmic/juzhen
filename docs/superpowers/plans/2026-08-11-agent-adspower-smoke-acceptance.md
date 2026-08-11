# Agent 内核真实 AdsPower 烟雾验收手册（M4 人工步骤）

**前置**：AdsPower 本机运行；目标 Profile 已登录 TikTok（业务账号）；Playwright 浏览器二进制已安装（`python -m playwright install chromium`）。
**安全**：验收只做浏览/滚动，不执行发布/评论/点赞等写动作。

## 层级 1：链路验收（必须通过）

```powershell
.venv\Scripts\python.exe scripts\smoke_adspower.py --mode link --profile-id <AdsPower Profile ID>
```

预期输出：
1. `AdsPower started, ws=...` —— Local API 启动成功
2. `CDP connected, page url=...` —— CDP 会话建立（严格单 context/page 校验）
3. `navigated to https://www.tiktok.com/` —— 页面导航成功
4. `screenshot saved: data\agent-smoke\link-<id>.png` —— 截图证据可打开（肉眼确认 TikTok 页面正常渲染）
5. `profile closed and stopped` —— 浏览器关闭 + AdsPower stop 成功

通过标准：5 步全部成功 + 截图内容正常（登录态可见为佳）。
失败排查：AdsPower 未启动 / api-key 配置缺失（`gateway/settings_store` 的 adspower 配置）/ Profile ID 错误 / Playwright 未安装。

## 层级 2：执行内核验收（策略链路）

需要先确认一个**稳定元素选择器**（TikTok 首页任一稳定元素，如 `div[data-e2e="feed-content"]` 或视频容器）：

```powershell
.venv\Scripts\python.exe scripts\smoke_adspower.py --mode strategy --profile-id <id> --ready-selector "div[data-e2e='feed-content']"
```

预期输出：
1. `outcome status=SUCCESS`
2. `SUCCESS: {'stage': 'execute_action', 'action_count': 1, ...}` —— scroll_down 动作完成

通过标准：status=SUCCESS 且 action_count>=1。
失败排查：选择器不稳定 → readiness 超时（更换选择器）；CDP 多 context（SessionBindingError，检查 AdsPower 配置）。

## 层级 3（可选）：中控+Agent 联调

```powershell
# 1. 启动 central（另开终端）
.venv\Scripts\python.exe -m uvicorn central.app:app --port 8000

# 2. 注册设备 + 导入账号（账号 profile_id 填验收 Profile）
#    POST http://127.0.0.1:8000/api/central/devices/heartbeat
#    POST http://127.0.0.1:8000/api/central/accounts/import
#    （或直接用 BCS 页面 http://127.0.0.1:5000/bcs）

# 3. 创建任务 → 用 ExecutionV2Executor 作为 agent 执行器跑一轮
#    验证：分配 → 真实执行 → 结果回传 → 看板状态变化
```

## 验收记录（执行后填写）

| 层级 | 结果 | 截图/日志 | 备注 |
|---|---|---|---|
| 1 链路 | 待执行 | | |
| 2 策略 | 待执行 | | |
| 3 联调 | 待执行 | | |

## 回滚

验收不修改任何数据（浏览/滚动只读）；如执行异常，检查 AdsPower 窗口手动关闭，`data/agent-smoke/` 证据可删除。
