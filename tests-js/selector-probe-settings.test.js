const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const {
  accountActionModel,
  clearSettingsFormSecrets,
  createSelectorProbeUI,
  dangerousSettingsDiff,
  normalizeTargetOrigin,
  parseProfileIds,
  renderAccountManagement,
  renderOperationWorkspace,
  renderSettings,
  renderTemporaryPassword,
  sanitizeSettings,
  settingsFingerprint,
  settingsPermissions,
  settingsStatusText,
  syntheticWebhookPayload,
  validateSettingsSave,
} = require("../gateway/static/selector_probe_ui");

function response(data, status = 200) {
  return {status, data};
}

function node(ownerDocument) {
  return {
    ownerDocument,
    children: [],
    dataset: {},
    attributes: {},
    hidden: false,
    disabled: false,
    textContent: "",
    value: "",
    append(...children) {
      this.children.push(...children);
    },
    replaceChildren(...children) {
      this.children = children;
    },
    setAttribute(name, value) {
      this.attributes[name] = String(value);
    },
  };
}

function fakeDocument(ids) {
  const nodes = new Map();
  const document = {
    createElement() {
      return node(document);
    },
    querySelector(selector) {
      return selector.startsWith("#") ? nodes.get(selector.slice(1)) || null : null;
    },
    querySelectorAll() {
      return [];
    },
  };
  ids.forEach((id) => nodes.set(id, node(document)));
  return {document, nodes};
}

function renderedText(value) {
  return [
    value.textContent,
    ...(value.children || []).flatMap(renderedText),
  ].join(" ");
}

function settingsFixture(overrides = {}) {
  return {
    revision: 4,
    enabled: true,
    rollout_mode: "publish",
    schedule_time: "03:00",
    timezone: "Asia/Shanghai",
    target_origin: "https://www.tiktok.com",
    freshness_hours: 36,
    site: "tiktok",
    environment: "production",
    profiles: [
      {
        profile_ref: "profile-ref-a",
        profile_mask: "***3A7F",
        dedicated_test: true,
        status: "healthy",
      },
      {
        profile_ref: "profile-ref-b",
        profile_mask: "***91C2",
        dedicated_test: true,
        status: "healthy",
      },
    ],
    model: {
      id: "repair-model",
      provider: "openai",
      mode: "repair_only",
      status: "passed",
      api_key_set: true,
    },
    redis: {
      status: "healthy",
      namespace: "selector-probe",
      aof_enabled: true,
      eviction_policy: "noeviction",
      password_set: true,
    },
    webhook: {
      enabled: true,
      type: "generic",
      url_display: "https://hooks.example/***",
      signing_secret_set: true,
      status: "passed",
    },
    ...overrides,
  };
}

test("profile ID input parses multiple lines, trims, and removes duplicates", () => {
  assert.deepEqual(
    parseProfileIds(" profile-a\r\nprofile-b\nprofile-a\n\n profile-c "),
    ["profile-a", "profile-b", "profile-c"],
  );
  assert.equal(
    normalizeTargetOrigin("https://www.tiktok.com/"),
    "https://www.tiktok.com",
  );
});

test("selected AdsPower profiles import into one bulk settings PATCH", async () => {
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
    selectedAdsPowerProfileIds: () => [
      "profile-one",
      "profile-two",
      "profile-one",
    ],
    createIdempotencyKey: () => "settings-key",
    setInterval: () => 1,
    clearInterval() {},
    render() {},
  });
  await ui.init();
  ui.state.settings = sanitizeSettings(settingsFixture({profiles: []}));

  assert.equal(ui.importSelectedProfiles(), 2);
  assert.deepEqual(
    ui.state.settingsProfileAdds.map((item) => item.profile_mask),
    ["***-one", "***-two"],
  );
  assert.doesNotMatch(JSON.stringify(ui.snapshot()), /profile-one|profile-two/);
  assert.equal(
    ui.confirmSettingsSave(ui.state.settings, "add dedicated profiles", {}),
    true,
  );
  assert.equal(await ui.submitSettingsSave(), true);

  const patch = requests.find(
    (item) => item.method === "PATCH" && item.url.endsWith("/settings"),
  );
  assert.deepEqual(patch.body.profile_changes, {
    add: ["profile-one", "profile-two"],
  });
  assert.deepEqual(ui.state.settingsProfileAdds, []);
});

test("failed profile bulk save retains staged IDs for an identical retry", async () => {
  const requests = [];
  let patchAttempt = 0;
  const ui = createSelectorProbeUI({
    requestJson: async (url, method, body) => {
      requests.push({url, method, body});
      if (url === "/api/auth/session") {
        return response({role: "administrator", permissions: ["settings:manage"]});
      }
      if (url.endsWith("/status")) return response({});
      if (method === "PATCH" && url.endsWith("/settings")) {
        patchAttempt += 1;
        return patchAttempt === 1
          ? response({error: "temporary_failure"}, 503)
          : response(settingsFixture({revision: 5}));
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
  assert.equal(ui.stageProfileAdds("profile-a\nprofile-b"), 2);
  assert.equal(
    ui.confirmSettingsSave(ui.state.settings, "add dedicated profiles", {}),
    true,
  );

  assert.equal(await ui.submitSettingsSave(), false);
  assert.equal(ui.state.settingsProfileAdds.length, 2);
  assert.equal(await ui.submitSettingsSave(), true);
  const patches = requests.filter(
    (item) => item.method === "PATCH" && item.url.endsWith("/settings"),
  );
  assert.deepEqual(
    patches.map((item) => item.body.profile_changes.add),
    [
      ["profile-a", "profile-b"],
      ["profile-a", "profile-b"],
    ],
  );
});

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

test("settings confirmation renders required reason and visible Chinese errors", () => {
  assert.equal(settingsStatusText("reason_required"), "请填写危险变更原因");
  assert.equal(
    settingsStatusText("target_origin_invalid"),
    "目标 Origin 必须是无账号密码的 HTTPS Origin",
  );
  const {document, nodes} = fakeDocument([
    "selector-operation-confirm-dialog",
    "selector-operation-detail-dialog",
    "selector-operation-confirm-title",
    "selector-operation-confirm-target",
    "selector-operation-confirm-outcome",
    "selector-operation-confirm-error",
    "selector-operation-reason-label",
    "selector-operation-reason-text",
    "selector-operation-reason",
    "selector-operation-confirm-submit",
  ]);
  const dialog = nodes.get("selector-operation-confirm-dialog");
  dialog.open = false;
  dialog.showModal = function () {
    this.open = true;
  };
  renderOperationWorkspace(document, {
    settings: sanitizeSettings(settingsFixture()),
    operationWorkspace: {
      kind: "settings-confirm",
      generation: 7,
      dangerousChanges: ["profiles"],
      requiresReason: true,
      reason: "",
      error: "reason_required",
      outcome: "二次确认危险变更：profiles",
    },
  });

  assert.equal(dialog.open, true);
  assert.equal(nodes.get("selector-operation-reason-label").hidden, false);
  assert.equal(nodes.get("selector-operation-reason").required, true);
  assert.equal(
    nodes.get("selector-operation-reason-text").textContent,
    "危险变更原因",
  );
  assert.equal(
    nodes.get("selector-operation-confirm-error").textContent,
    "请填写危险变更原因",
  );
});

function passedPreflight() {
  return {
    status: "passed",
    base_revision: 4,
    candidate_fingerprint: settingsFingerprint(settingsFixture()),
    preflight_token: "preflight-token-4",
    checked_at: "2026-07-29T03:00:00Z",
    checks: {
      profiles: "passed",
      redis_aof: "passed",
      redis_eviction: "passed",
      model: "passed",
      webhook: "passed",
    },
  };
}

test("operator settings model is read only", () => {
  const model = settingsPermissions({
    role: "operator",
    permissions: [
      "probe:read",
      "probe:run",
      "alert:acknowledge",
      "webhook:test",
    ],
  });
  assert.equal(model.canEdit, false);
  assert.equal(model.canTestWebhook, true);
  assert.equal(model.canManageAccounts, false);
});

test("enforce change requires preflight and reason", () => {
  const result = validateSettingsSave(
    settingsFixture({rollout_mode: "publish"}),
    settingsFixture({rollout_mode: "enforce"}),
    {reason: "", preflight: null},
  );
  assert.deepEqual(result.errors, ["reason_required", "preflight_required"]);
  const insufficient = validateSettingsSave(
    settingsFixture({rollout_mode: "publish"}),
    settingsFixture({
      rollout_mode: "enforce",
      profiles: [
        {
          profile_ref: "profile-ref-a",
          profile_mask: "***3A7F",
          dedicated_test: true,
          status: "healthy",
        },
      ],
    }),
    {reason: "enable enforcement", preflight: passedPreflight()},
  );
  assert.deepEqual(insufficient.errors, ["preflight_failed"]);
});

test("dangerous diff is exact and excludes harmless schedule edits", () => {
  const before = settingsFixture();
  const after = settingsFixture({
    enabled: false,
    rollout_mode: "observe",
    target_origin: "https://m.tiktok.com",
    schedule_time: "04:00",
    redis: {...before.redis, namespace: "selector-probe-v2"},
  });
  assert.deepEqual(dangerousSettingsDiff(before, after), [
    "enabled",
    "rollout_mode",
    "target_origin",
    "redis",
  ]);
});

test("settings projection masks profiles and never retains raw secrets", () => {
  const safe = sanitizeSettings({
    ...settingsFixture(),
    profiles: [
      {
        profile_ref: "profile-ref-a",
        profile_mask: "***3A7F",
        profile_id: "full-secret-profile",
      },
      {profile_mask: "full-secret-profile"},
    ],
    model: {
      ...settingsFixture().model,
      api_key: "sk-secret",
    },
    redis: {
      ...settingsFixture().redis,
      password: "redis-secret",
    },
    webhook: {
      ...settingsFixture().webhook,
      signing_secret: "hook-secret",
      url: "https://hooks.example/private-token",
    },
  });
  const encoded = JSON.stringify(safe);
  assert.equal(safe.profiles.length, 1);
  assert.equal(safe.profiles[0].profile_mask, "***3A7F");
  assert.doesNotMatch(
    encoded,
    /full-secret-profile|sk-secret|redis-secret|hook-secret|private-token/,
  );
  assert.equal(safe.model.api_key_set, true);
  assert.equal(safe.redis.password_set, true);
  assert.equal(safe.webhook.signing_secret_set, true);
});

test("settings renderer exposes six safe sections and operator controls stay readonly", () => {
  const {document, nodes} = fakeDocument([
    "selector-settings-basic",
    "selector-settings-profiles",
    "selector-settings-model",
    "selector-settings-redis",
    "selector-settings-webhook",
    "selector-settings-permissions",
    "selector-settings-status",
    "selector-settings-save",
    "selector-settings-preflight",
    "selector-settings-webhook-test",
  ]);
  renderSettings(document, {
    session: {
      role: "operator",
      permissions: ["probe:read", "webhook:test"],
    },
    settings: settingsFixture(),
    settingsPreflight: null,
  });
  const text = [
    "selector-settings-basic",
    "selector-settings-profiles",
    "selector-settings-model",
    "selector-settings-redis",
    "selector-settings-webhook",
    "selector-settings-permissions",
  ].map((id) => renderedText(nodes.get(id))).join(" ");
  assert.match(text, /03:00/);
  assert.match(text, /\*\*\*3A7F/);
  assert.match(text, /repair_only/);
  assert.match(text, /noeviction/);
  assert.match(text, /https:\/\/hooks\.example\/\*\*\*/);
  assert.equal(nodes.get("selector-settings-save").hidden, true);
  assert.equal(nodes.get("selector-settings-preflight").hidden, true);
  assert.equal(nodes.get("selector-settings-webhook-test").hidden, false);
});

test("enforce save requires passed checks then uses second confirmation and omits blank secrets", async () => {
  const requests = [];
  const ui = createSelectorProbeUI({
    requestJson: async (url, method, body) => {
      requests.push({url, method, body});
      if (url === "/api/auth/session") {
        return response({role: "administrator", permissions: ["settings:manage"]});
      }
      if (url.endsWith("/status")) return response({});
      if (method === "POST" && url.endsWith("/settings/preflight")) {
        return response({
          ...passedPreflight(),
          base_revision: body.expected_revision,
          candidate_fingerprint: body.candidate_fingerprint,
        });
      }
      if (method === "PATCH" && url.endsWith("/settings")) {
        return response(settingsFixture({revision: 5, rollout_mode: "enforce"}));
      }
      return response({});
    },
    createIdempotencyKey: () => "settings-key",
    setInterval: () => 1,
    clearInterval() {},
    render() {},
  });
  await ui.init();
  ui.state.settings = sanitizeSettings(settingsFixture());
  const after = settingsFixture({rollout_mode: "enforce"});
  assert.equal(
    ui.confirmSettingsSave(after, "enable enforcement", {
      model_api_key: "",
      redis_password: "",
      webhook_signing_secret: "",
    }),
    false,
  );
  await ui.requestSettingsPreflight(after);
  assert.equal(
    ui.confirmSettingsSave(after, "enable enforcement", {
      model_api_key: "",
      redis_password: "",
      webhook_signing_secret: "",
    }),
    true,
  );
  assert.equal(ui.state.operationWorkspace.kind, "settings-confirm");
  await ui.submitSettingsSave();
  const patch = requests.find(
    (item) => item.method === "PATCH" && item.url.endsWith("/settings"),
  );
  assert.equal(patch.body.expected_revision, 4);
  assert.equal(patch.body.reason, "enable enforcement");
  assert.equal(patch.body.idempotency_key, "settings-key");
  assert.equal(patch.body.settings.rollout_mode, "enforce");
  assert.equal("secrets" in patch.body, false);
  assert.equal(ui.state.settings.rollout_mode, "enforce");
});

test("preflight binding is invalidated by polling while confirmed save keeps frozen CAS", async () => {
  const requests = [];
  const original = settingsFixture({revision: 10});
  const candidate = settingsFixture({revision: 10, rollout_mode: "enforce"});
  const ui = createSelectorProbeUI({
    requestJson: async (url, method, body) => {
      requests.push({url, method, body});
      if (url === "/api/auth/session") {
        return response({role: "administrator", permissions: ["settings:manage"]});
      }
      if (url.endsWith("/status")) return response({});
      if (method === "POST" && url.endsWith("/settings/preflight")) {
        return response({
          ...passedPreflight(),
          base_revision: body.expected_revision,
          candidate_fingerprint: body.candidate_fingerprint,
          preflight_token: "preflight-token-10",
        });
      }
      if (method === "GET" && url.endsWith("/settings")) {
        return response(settingsFixture({revision: 11}));
      }
      if (url === "/api/admin/users") return response({items: []});
      if (method === "PATCH" && url.endsWith("/settings")) {
        return response({error: "revision_conflict"}, 409);
      }
      return response({});
    },
    createIdempotencyKey: () => "settings-key",
    setInterval: () => 1,
    clearInterval() {},
    render() {},
  });
  await ui.init();
  ui.state.settings = sanitizeSettings(original);
  assert.equal(await ui.requestSettingsPreflight(candidate), true);
  assert.equal(
    ui.confirmSettingsSave(candidate, "enable enforcement", {}),
    true,
  );
  assert.equal(ui.state.operationWorkspace.baseRevision, 10);
  assert.equal(
    ui.state.operationWorkspace.candidateFingerprint,
    settingsFingerprint(candidate),
  );
  assert.equal(ui.state.operationWorkspace.preflightToken, "preflight-token-10");

  await ui.activateTab("settings");
  assert.equal(ui.state.settings.revision, 11);
  assert.equal(ui.state.settingsPreflight, null);
  assert.equal(
    ui.state.settingsStatus,
    "preflight_invalidated_revision_changed",
  );

  assert.equal(await ui.submitSettingsSave(), false);
  const patch = requests.find(
    (item) => item.method === "PATCH" && item.url.endsWith("/settings"),
  );
  assert.equal(patch.body.expected_revision, 10);
  assert.equal(patch.body.candidate_fingerprint, settingsFingerprint(candidate));
  assert.equal(patch.body.preflight_token, "preflight-token-10");
  assert.equal(patch.body.preflight_checked_at, "2026-07-29T03:00:00Z");
});

test("ordinary settings draft freezes first-edit revision and blocks stale schedule/model save", async () => {
  const requests = [];
  const ui = createSelectorProbeUI({
    requestJson: async (url, method, body) => {
      requests.push({url, method, body});
      if (url === "/api/auth/session") {
        return response({role: "administrator", permissions: ["settings:manage"]});
      }
      if (url.endsWith("/status")) return response({});
      if (method === "GET" && url.endsWith("/settings")) {
        return response(settingsFixture({revision: 5}));
      }
      if (url === "/api/admin/users") return response({items: []});
      if (method === "PATCH" && url.endsWith("/settings")) {
        return response(settingsFixture({
          revision: 6,
          schedule_time: body.settings.schedule_time,
          model: {
            ...settingsFixture().model,
            id: body.settings.model.id,
          },
        }));
      }
      return response({});
    },
    createIdempotencyKey: () => "settings-key",
    setInterval: () => 1,
    clearInterval() {},
    render() {},
  });
  await ui.init();
  ui.state.settings = sanitizeSettings(settingsFixture({revision: 4}));
  const oldDraft = settingsFixture({
    revision: 4,
    schedule_time: "04:00",
    model: {...settingsFixture().model, id: "new-model"},
  });
  assert.equal(ui.stageSettingsDraft(oldDraft), true);
  assert.equal(ui.state.settingsDraftBaseRevision, 4);

  await ui.activateTab("settings");
  assert.equal(ui.state.settings.revision, 5);
  assert.equal(ui.state.settingsDraftStale, true);
  assert.equal(
    ui.confirmSettingsSave(oldDraft, "", {}),
    false,
  );
  assert.equal(ui.state.settingsStatus, "settings_draft_stale_reload_required");
  assert.equal(
    requests.some((item) => item.method === "PATCH"),
    false,
  );

  assert.equal(await ui.reloadSettingsDraft(), true);
  assert.equal(ui.state.settingsDraft, null);
  const freshDraft = settingsFixture({
    revision: 5,
    schedule_time: "04:00",
    model: {...settingsFixture().model, id: "new-model"},
  });
  assert.equal(ui.stageSettingsDraft(freshDraft), true);
  assert.equal(ui.confirmSettingsSave(freshDraft, "", {}), true);
  assert.equal(await ui.submitSettingsSave(), true);
  const patch = requests.find((item) => item.method === "PATCH");
  assert.equal(patch.body.expected_revision, 5);
  assert.equal(patch.body.settings.schedule_time, "04:00");
  assert.equal(patch.body.settings.model.id, "new-model");
});

test("settings reload clears write-only DOM and pending secret state before new revision save", async () => {
  const requests = [];
  let clearCalls = 0;
  const ui = createSelectorProbeUI({
    requestJson: async (url, method, body) => {
      requests.push({url, method, body});
      if (url === "/api/auth/session") {
        return response({role: "administrator", permissions: ["settings:manage"]});
      }
      if (url.endsWith("/status")) return response({});
      if (method === "GET" && url.endsWith("/settings")) {
        return response(settingsFixture({revision: 5}));
      }
      if (method === "PATCH" && url.endsWith("/settings")) {
        return response(settingsFixture({
          revision: 6,
          schedule_time: body.settings.schedule_time,
        }));
      }
      return response({});
    },
    createIdempotencyKey: () => "settings-key",
    setInterval: () => 1,
    clearInterval() {},
    clearSettingsFormSecrets: () => {
      clearCalls += 1;
    },
    render() {},
  });
  await ui.init();
  ui.state.settings = sanitizeSettings(settingsFixture({revision: 4}));
  const oldDraft = settingsFixture({revision: 4, schedule_time: "04:00"});
  ui.stageSettingsDraft(oldDraft);
  assert.equal(
    ui.confirmSettingsSave(oldDraft, "", {
      model_api_key: "old-model-secret",
      redis_password: "old-redis-secret",
    }),
    true,
  );

  assert.equal(await ui.reloadSettingsDraft(), true);
  assert.equal(clearCalls, 1);
  assert.equal(ui.state.operationWorkspace, null);
  const freshDraft = settingsFixture({revision: 5, schedule_time: "05:00"});
  ui.stageSettingsDraft(freshDraft);
  assert.equal(ui.confirmSettingsSave(freshDraft, "", {}), true);
  assert.equal(await ui.submitSettingsSave(), true);
  const patch = requests.find((item) => item.method === "PATCH");
  assert.equal(patch.body.expected_revision, 5);
  assert.equal("secrets" in patch.body, false);
});

test("clearSettingsFormSecrets clears exact write-only controls without closing dialogs", () => {
  const selectors = [
    '[name="modelApiKey"]',
    '[name="redisPassword"]',
    '[name="webhookSigningSecret"]',
    '[name="webhookUrl"]',
    '[name="profileAdd"]',
    '[name="reason"]',
    "#selector-operation-reason",
  ];
  const controls = new Map(selectors.map((selector) => [
    selector,
    {value: `secret:${selector}`},
  ]));
  let closeCalls = 0;
  const document = {
    querySelector(selector) {
      if (selector === "dialog") {
        return {close: () => {
          closeCalls += 1;
        }};
      }
      return controls.get(selector) || null;
    },
  };
  clearSettingsFormSecrets(document);
  assert.equal(
    Array.from(controls.values()).every((control) => control.value === ""),
    true,
  );
  assert.equal(closeCalls, 0);
});

test("preflight rejects a server response that is not bound to the candidate", async () => {
  const ui = createSelectorProbeUI({
    requestJson: async (url, method) => {
      if (url === "/api/auth/session") {
        return response({role: "administrator", permissions: ["settings:manage"]});
      }
      if (url.endsWith("/status")) return response({});
      if (method === "POST" && url.endsWith("/settings/preflight")) {
        return response({
          ...passedPreflight(),
          base_revision: 999,
          candidate_fingerprint: "wrong",
          preflight_token: "",
        });
      }
      return response({});
    },
    setInterval: () => 1,
    clearInterval() {},
    render() {},
  });
  await ui.init();
  ui.state.settings = sanitizeSettings(settingsFixture());
  assert.equal(await ui.requestSettingsPreflight(settingsFixture()), false);
  assert.equal(ui.state.settingsPreflight, null);
  assert.equal(ui.state.settingsStatus, "settings_preflight_binding_invalid");
});

test("profile masks are display only and colliding suffixes use opaque refs", async () => {
  const requests = [];
  const profiles = [
    {
      profile_ref: "opaque-ref-a",
      profile_mask: "***1234",
      dedicated_test: true,
      status: "healthy",
    },
    {
      profile_ref: "opaque-ref-b",
      profile_mask: "***1234",
      dedicated_test: true,
      status: "healthy",
    },
    {
      profile_ref: "opaque-ref-c",
      profile_mask: "***5678",
      dedicated_test: true,
      status: "healthy",
    },
  ];
  const original = settingsFixture({profiles});
  const ui = createSelectorProbeUI({
    requestJson: async (url, method, body) => {
      requests.push({url, method, body});
      if (url === "/api/auth/session") {
        return response({role: "administrator", permissions: ["settings:manage"]});
      }
      if (url.endsWith("/status")) return response({});
      if (method === "PATCH" && url.endsWith("/settings")) {
        return response(settingsFixture({
          revision: 5,
          profiles: profiles.slice(1),
        }));
      }
      return response({});
    },
    createIdempotencyKey: () => "settings-key",
    setInterval: () => 1,
    clearInterval() {},
    render() {},
  });
  await ui.init();
  ui.state.settings = sanitizeSettings(original);
  assert.equal(ui.state.settings.profiles.length, 3);
  assert.equal(ui.stageProfileRemoval("opaque-ref-a"), true);
  assert.deepEqual(
    ui.state.settingsDraft.profiles.map((item) => item.profile_ref),
    ["opaque-ref-b", "opaque-ref-c"],
  );
  assert.equal(
    ui.confirmSettingsSave(ui.state.settingsDraft, "remove test profile", {}),
    true,
  );
  assert.equal(await ui.submitSettingsSave(), true);
  const patch = requests.find(
    (item) => item.method === "PATCH" && item.url.endsWith("/settings"),
  );
  assert.deepEqual(patch.body.settings.profiles, [
    {profile_ref: "opaque-ref-b", dedicated_test: true},
    {profile_ref: "opaque-ref-c", dedicated_test: true},
  ]);
  assert.doesNotMatch(JSON.stringify(patch.body.settings.profiles), /profile_mask/);
});

test("destroy clears draft, preflight, confirmation, credential, and sensitive UI", async () => {
  let cleanupCalls = 0;
  const ui = createSelectorProbeUI({
    requestJson: async (url, method, body) => {
      if (url === "/api/auth/session") {
        return response({role: "administrator", permissions: ["settings:manage"]});
      }
      if (url.endsWith("/status")) return response({});
      if (method === "POST" && url.endsWith("/settings/preflight")) {
        return response({
          ...passedPreflight(),
          base_revision: body.expected_revision,
          candidate_fingerprint: body.candidate_fingerprint,
        });
      }
      return response({});
    },
    setInterval: () => 1,
    clearInterval() {},
    cleanupSensitiveUI: () => {
      cleanupCalls += 1;
    },
    render() {},
  });
  await ui.init();
  ui.state.settings = sanitizeSettings(settingsFixture());
  const draft = settingsFixture({schedule_time: "04:00"});
  ui.stageSettingsDraft(draft);
  await ui.requestSettingsPreflight(draft);
  assert.equal(
    ui.confirmSettingsSave(draft, "", {model_api_key: "write-only-secret"}),
    true,
  );
  ui.state.temporaryCredential = {
    username: "ops",
    password: "one-time-secret",
  };
  ui.state.selected = {kind: "wizard"};

  ui.destroy();
  assert.equal(ui.state.settingsDraft, null);
  assert.equal(ui.state.settingsDraftBaseRevision, null);
  assert.equal(ui.state.settingsDraftStale, false);
  assert.equal(ui.state.settingsPreflight, null);
  assert.equal(ui.state.operationWorkspace, null);
  assert.equal(ui.state.temporaryCredential, null);
  assert.equal(ui.state.selected, null);
  assert.equal(cleanupCalls, 1);
  assert.equal(await ui.submitSettingsSave(), false);
});

test("synthetic webhook envelope contains only the approved event projection", async () => {
  assert.deepEqual(syntheticWebhookPayload(settingsFixture()), {
    event: "selector_probe.webhook_test",
    environment: "production",
    site: "tiktok",
    synthetic: true,
  });
  const requests = [];
  const ui = createSelectorProbeUI({
    requestJson: async (url, method, body) => {
      requests.push({url, method, body});
      if (url === "/api/auth/session") {
        return response({role: "operator", permissions: ["webhook:test"]});
      }
      if (url.endsWith("/status")) return response({});
      if (url.endsWith("/webhook-test")) {
        return response({status: "accepted", delivery_id: "delivery-1"}, 202);
      }
      return response({});
    },
    createIdempotencyKey: () => "webhook-key",
    setInterval: () => 1,
    clearInterval() {},
    render() {},
  });
  await ui.init();
  ui.state.settings = sanitizeSettings(settingsFixture());
  assert.equal(await ui.requestWebhookTest(), true);
  assert.deepEqual(requests.find((item) => item.url.endsWith("/webhook-test")).body, {
    idempotency_key: "webhook-key",
    payload: {
      event: "selector_probe.webhook_test",
      environment: "production",
      site: "tiktok",
      synthetic: true,
    },
  });
  assert.doesNotMatch(
    JSON.stringify(requests.at(-1).body),
    /alert|screenshot|selector(?!_probe)|profile|path/i,
  );
});

test("secret clear is an independent administrator-confirmed mutation", async () => {
  const requests = [];
  const ui = createSelectorProbeUI({
    requestJson: async (url, method, body) => {
      requests.push({url, method, body});
      if (url === "/api/auth/session") {
        return response({role: "administrator", permissions: ["settings:manage"]});
      }
      if (url.endsWith("/status")) return response({});
      if (url.endsWith("/settings/secrets/redis_password/clear")) {
        return response(settingsFixture({
          revision: 5,
          redis: {...settingsFixture().redis, password_set: false},
        }));
      }
      return response({});
    },
    createIdempotencyKey: () => "clear-key",
    setInterval: () => 1,
    clearInterval() {},
    render() {},
  });
  await ui.init();
  ui.state.settings = sanitizeSettings(settingsFixture());
  assert.equal(ui.confirmSecretClear("redis_password", ""), false);
  assert.equal(
    ui.confirmSecretClear("redis_password", "rotate credentials"),
    true,
  );
  assert.equal(ui.state.operationWorkspace.kind, "secret-clear-confirm");
  assert.equal(await ui.submitSecretClear(), true);
  assert.deepEqual(
    requests.find((item) => item.url.includes("/secrets/")).body,
    {
      expected_revision: 4,
      reason: "rotate credentials",
      idempotency_key: "clear-key",
    },
  );
  assert.equal(ui.state.settings.redis.password_set, false);
});

test("last enabled administrator cannot be disabled or demoted in the UI", () => {
  const users = [
    {id: 1, username: "admin", role: "administrator", enabled: true, revision: 3},
    {id: 2, username: "ops", role: "operator", enabled: true, revision: 2},
  ];
  const actions = accountActionModel(users[0], users, {role: "administrator"});
  assert.equal(actions.disable.disabled, true);
  assert.equal(actions.demote.disabled, true);
  assert.equal(actions.resetPassword.disabled, false);
  assert.equal(actions.revokeSessions.disabled, false);
});

test("account management uses all five real routes and clears one-time password", async () => {
  const requests = [];
  let revision = 1;
  const users = [{
    id: 1,
    username: "admin",
    role: "administrator",
    enabled: true,
    revision: 1,
  }, {
    id: 2,
    username: "ops",
    role: "operator",
    enabled: true,
    revision,
  }];
  const ui = createSelectorProbeUI({
    requestJson: async (url, method, body) => {
      requests.push({url, method, body});
      if (url === "/api/auth/session") {
        return response({role: "administrator", permissions: ["users:manage"]});
      }
      if (url.endsWith("/status")) return response({});
      if (method === "GET" && url === "/api/admin/users") {
        return response({users});
      }
      if (method === "POST" && url === "/api/admin/users") {
        return response({
          user: {...users[1], id: 3, username: "new-ops"},
          temporary_password: "one-time-create-password",
        }, 201);
      }
      if (method === "PATCH" && url.endsWith("/api/admin/users/2")) {
        revision += 1;
        return response({user: {...users[1], enabled: false, revision}});
      }
      if (url.endsWith("/api/admin/users/2/reset-password")) {
        revision += 1;
        return response({
          user: {...users[1], revision, must_change_password: true},
          temporary_password: "one-time-reset-password",
        });
      }
      if (url.endsWith("/api/admin/users/2/revoke-sessions")) {
        revision += 1;
        return response({user: {...users[1], revision}});
      }
      return response({});
    },
    copyText: async () => true,
    setInterval: () => 1,
    clearInterval() {},
    render() {},
  });
  await ui.init();
  await ui.loadAccounts();
  await ui.createAccount("new-ops", "operator");
  assert.equal(ui.state.temporaryCredential.password, "one-time-create-password");
  await ui.copyTemporaryPassword();
  ui.clearTemporaryPassword();
  assert.equal(ui.state.temporaryCredential, null);
  assert.doesNotMatch(JSON.stringify(ui.snapshot()), /one-time-create-password/);

  await ui.updateAccount(2, {enabled: false});
  await ui.resetAccountPassword(2);
  assert.equal(ui.state.temporaryCredential.password, "one-time-reset-password");
  ui.clearTemporaryPassword();
  await ui.revokeAccountSessions(2);

  assert.equal(
    requests.some((item) => item.method === "GET" && item.url === "/api/admin/users"),
    true,
  );
  assert.deepEqual(
    requests.find((item) => item.method === "POST" && item.url === "/api/admin/users").body,
    {username: "new-ops", role: "operator"},
  );
  assert.deepEqual(
    requests.find((item) => item.method === "PATCH").body,
    {expected_revision: 1, enabled: false},
  );
  assert.deepEqual(
    requests.find((item) => item.url.endsWith("/reset-password")).body,
    {},
  );
  assert.deepEqual(
    requests.find((item) => item.url.endsWith("/revoke-sessions")).body,
    {},
  );
});

test("temporary password renderer removes DOM text after controller state clears", () => {
  const {document, nodes} = fakeDocument([
    "selector-temporary-password-dialog",
    "selector-temporary-password-user",
    "selector-temporary-password-value",
  ]);
  const dialog = nodes.get("selector-temporary-password-dialog");
  dialog.open = false;
  dialog.showModal = function () {
    this.open = true;
  };
  dialog.close = function () {
    this.open = false;
  };
  const state = {
    temporaryCredential: {
      username: "ops",
      password: "one-time-visible-password",
    },
  };
  renderTemporaryPassword(document, state);
  assert.equal(
    nodes.get("selector-temporary-password-value").textContent,
    "one-time-visible-password",
  );
  state.temporaryCredential = null;
  renderTemporaryPassword(document, state);
  assert.equal(nodes.get("selector-temporary-password-value").textContent, "");
  assert.equal(nodes.get("selector-temporary-password-user").textContent, "");
});

test("account renderer hides administration from operators", () => {
  const {document, nodes} = fakeDocument([
    "selector-account-rows",
    "selector-account-add",
    "selector-account-status",
  ]);
  renderAccountManagement(document, {
    session: {role: "operator"},
    accounts: {items: [{id: 1, username: "admin", role: "administrator"}]},
  });
  assert.equal(nodes.get("selector-account-add").hidden, true);
  assert.equal(nodes.get("selector-account-rows").children.length, 0);
});

test("settings source uses no innerHTML or raw secret field projection", () => {
  const source = fs.readFileSync(
    path.join(__dirname, "../gateway/static/selector_probe_ui.js"),
    "utf8",
  );
  assert.doesNotMatch(source, /\.innerHTML\s*=/);
  assert.doesNotMatch(
    source,
    /source\.(api_key|password|signing_secret|profile_id)\b/,
  );
});
