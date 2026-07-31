# Selector Settings Reason Dialog Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让测试 Profile 危险变更在设置确认弹窗内收集必填原因，并让阻断错误显示在保存按钮旁。

**Architecture:** 保留现有设置候选、二次确认、PATCH 和后端审计链路。前端把 `reason_required` 从确认前阻断改为确认弹窗内校验；其他阻断错误通过一个中文映射同时渲染在页头和保存按钮旁。

**Tech Stack:** 原生 JavaScript、HTML `<dialog>`、Node.js `node:test`、Flask 模板。

## Global Constraints

- 不降低后端危险变更原因要求。
- 不修改 Profile 批量保存 API。
- 保存失败时保留暂存 Profile 和原因。
- 不改变其他操作确认弹窗行为。

---

### Task 1: 修复设置原因确认交互

**Files:**
- Modify: `gateway/app.py:2437-2442`
- Modify: `gateway/static/selector_probe_ui.js:1142-1177, 2504-2635, 4190-4207, 4574-4659, 5293-5311`
- Test: `tests-js/selector-probe-settings.test.js`

**Interfaces:**
- Consumes: `confirmSettingsSave(raw, reason, secrets)`、现有 `settings-confirm` workspace、`profile_changes.add`.
- Produces: `submitSettingsSave(reason?: string): Promise<boolean>`、`settingsStatusText(code: string): string`、设置确认 workspace 的 `requiresReason: boolean`.

- [ ] **Step 1: 写失败回归测试**

在 `tests-js/selector-probe-settings.test.js` 增加控制器测试：

```js
test("profile save opens confirmation before reason and validates reason inside dialog", async () => {
  const requests = [];
  const ui = createSelectorProbeUI({
    requestJson: async (url, method, body) => {
      requests.push({url, method, body});
      if (url === "/api/auth/session") {
        return response({role: "administrator", permissions: ["settings:manage"]});
      }
      if (url.endsWith("/status")) return response({});
      if (method === "PATCH" && url.endsWith("/settings")) {
        return response(settingsFixture({revision: 5}));
      }
      return response({});
    },
    createIdempotencyKey: () => "settings-key",
    setInterval: () => 1,
    clearInterval() {},
    render() {},
  });
  await ui.init();
  ui.state.settings = sanitizeSettings(settingsFixture({profiles: []}));
  ui.stageProfileAdds(["profile-a", "profile-b"]);

  assert.equal(ui.confirmSettingsSave(ui.state.settings, "", {}), true);
  assert.equal(ui.state.operationWorkspace.kind, "settings-confirm");
  assert.equal(ui.state.operationWorkspace.requiresReason, true);
  assert.equal(await ui.submitSettingsSave(""), false);
  assert.equal(ui.state.operationWorkspace.error, "reason_required");
  assert.equal(requests.some((item) => item.method === "PATCH"), false);

  assert.equal(await ui.submitSettingsSave("新增独立探针测试账号"), true);
  const patch = requests.find((item) => item.method === "PATCH");
  assert.equal(patch.body.reason, "新增独立探针测试账号");
  assert.deepEqual(patch.body.profile_changes.add, ["profile-a", "profile-b"]);
});
```

增加中文状态映射测试：

```js
test("settings blocking errors use visible Chinese copy", () => {
  assert.equal(settingsStatusText("reason_required"), "请填写危险变更原因");
  assert.equal(
    settingsStatusText("target_origin_invalid"),
    "目标 Origin 必须是无账号密码的 HTTPS Origin",
  );
});
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```powershell
node --test tests-js/selector-probe-settings.test.js
```

Expected: FAIL，因为空原因仍阻止 workspace 创建，且 `settingsStatusText` 尚不存在。

- [ ] **Step 3: 实现确认弹窗内原因校验**

在 `confirmSettingsSave` 中只让非原因错误阻止弹窗：

```js
const blockingErrors = validation.errors.filter(
  (error) => error !== "reason_required",
);
if (blockingErrors.length) {
  state.settingsStatus = blockingErrors.join(",");
  render("settings");
  return false;
}
```

创建 workspace 时记录：

```js
requiresReason: validation.dangerous_changes.length > 0,
```

修改提交函数：

```js
async function submitSettingsSave(reason) {
  const workspace = state.operationWorkspace;
  if (
    state.session?.role !== "administrator"
    || workspace?.kind !== "settings-confirm"
    || !state.settings
  ) return false;
  const selectedReason = safeText(
    reason === undefined ? workspace.reason : reason,
    500,
  ).trim();
  if (workspace.requiresReason && !selectedReason) {
    workspace.error = "reason_required";
    render("operation-workspace");
    return false;
  }
  workspace.reason = selectedReason;
}
```

把上述原因处理插入现有管理员/workspace guard 之后、`const generation = workspace.generation` 之前；其后的请求组装与 PATCH 代码保持原位。

`settings-confirm` 渲染分支改为：

```js
needsReason = workspace.requiresReason === true;
```

确认表单提交改为：

```js
} else if (kind === "settings-confirm") {
  controller.submitSettingsSave(reason);
}
```

弹窗首次打开时用 `workspace.reason` 预填原因；同一 workspace 重渲染不得覆盖用户输入：

```js
if (
  reason
  && reason.dataset.workspaceGeneration !== String(workspace.generation)
) {
  reason.value = workspace.reason || "";
  reason.dataset.workspaceGeneration = String(workspace.generation);
}
```

- [ ] **Step 4: 增加按钮旁中文错误提示**

在 `gateway/app.py` 保存按钮组后增加：

```html
<p id="selector-settings-save-status" class="bad" role="status" aria-live="polite"></p>
```

在 UI 模块增加：

```js
function settingsStatusText(value) {
  const messages = {
    reason_required: "请填写危险变更原因",
    target_origin_invalid: "目标 Origin 必须是无账号密码的 HTTPS Origin",
    preflight_required: "切换 Enforce 前请先运行预检",
    preflight_failed: "Enforce 预检未通过",
    settings_draft_stale_reload_required: "设置已更新，请重新加载后再保存",
    settings_save_failed: "设置保存失败，请重试",
  };
  return safeText(value, 500)
    .split(",")
    .filter(Boolean)
    .map((code) => messages[code] || code)
    .join("；");
}
```

`renderSettings` 同时更新两个状态节点：

```js
const statusText = settingsStatusText(state?.settingsStatus || "");
setNodeText(document, "selector-settings-status", statusText);
setNodeText(document, "selector-settings-save-status", statusText);
```

导出 `settingsStatusText` 供测试使用。

- [ ] **Step 5: 运行前端测试**

Run:

```powershell
node --test tests-js/selector-probe-settings.test.js
npm.cmd run test:node
```

Expected: 设置测试全部 PASS；全量 Node 测试全部 PASS。

- [ ] **Step 6: 运行后端回归**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_selector_probe_management_routes.py tests/test_selector_probe_management_hardening.py
```

Expected: 全部 PASS；允许现有 `.pytest_cache` 权限警告。

- [ ] **Step 7: 提交**

若工作目录恢复为有效 Git 仓库：

```powershell
git add gateway/app.py gateway/static/selector_probe_ui.js tests-js/selector-probe-settings.test.js docs/superpowers/specs/2026-07-30-selector-settings-reason-dialog-fix-design.md docs/superpowers/plans/2026-07-30-selector-settings-reason-dialog-fix.md
git commit -m "fix: collect selector settings reason in dialog"
```

当前目录不是有效 Git 仓库时跳过提交并在交付说明中注明。
