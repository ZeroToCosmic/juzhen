"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const page = require("../gateway/static/console_comment_campaign_create");

function response(status, body) {
  return {status, data: body};
}

function harness(overrides = {}) {
  const requests = [];
  const renders = [];
  const assigned = [];
  const controller = page.createConsoleCommentCampaignCreate({
    requestJson: async (url, method = "GET", body) => {
      requests.push({url, method, body});
      const value = overrides.responses?.[`${method} ${url}`];
      if (value === undefined) throw new Error(`unexpected request: ${method} ${url}`);
      return typeof value === "function" ? value(body) : value;
    },
    location: {assign: (url) => assigned.push(url)},
    render: (state, model) => renders.push({
      state: structuredClone(state),
      model: structuredClone(model),
    }),
  });
  return {controller, requests, renders, assigned};
}

const templates = [{
  id: "tree-1",
  name: "评论树",
  revision: 7,
  enabled: true,
  supported_modes: ["independent", "threaded"],
}];
const profiles = [{
  profile_ref: "opaque-a",
  display_profile: "窗口 A",
  enabled: true,
  health_status: "healthy",
  language: "zh",
  region: "CN",
}];

test("initialization reads only templates and cached Profiles without polling or sync", async () => {
  const {controller, requests} = harness({responses: {
    "GET /api/browser-v2/comment-templates": response(200, {data: templates}),
    "GET /api/browser-v2/comment-profile-metadata": response(200, {data: profiles, meta: {stale: false}}),
  }});

  await controller.init();

  assert.deepEqual(requests, [
    {url: "/api/browser-v2/comment-templates", method: "GET", body: undefined},
    {url: "/api/browser-v2/comment-profile-metadata", method: "GET", body: undefined},
  ]);
  assert.deepEqual(controller.state.templates, templates);
  assert.deepEqual(controller.state.profiles, profiles);
  assert.equal(controller.state.initialized, true);
});

test("templates filter disabled and incompatible records", async () => {
  const records = [
    {id: "independent", name: "独立", revision: 1, enabled: true, supported_modes: ["independent"]},
    {id: "threaded", name: "盖楼", revision: 2, enabled: true, supported_modes: ["threaded"]},
    {id: "disabled", name: "停用", revision: 3, enabled: false, supported_modes: ["independent"]},
  ];
  const {controller, renders} = harness({responses: {
    "GET /api/browser-v2/comment-templates": response(200, {data: records}),
    "GET /api/browser-v2/comment-profile-metadata": response(200, {data: [], meta: {stale: false}}),
  }});

  await controller.init();

  assert.deepEqual(renders.at(-1).model.templates.map((item) => item.id), ["independent"]);
});

test("initialization switches an untouched mode to one with enabled trees without selecting or previewing", async () => {
  const records = [
    {id: "threaded", name: "盖楼", revision: 2, enabled: true, supported_modes: ["threaded"]},
  ];
  const {controller, renders, requests} = harness({responses: {
    "GET /api/browser-v2/comment-templates": response(200, {data: records}),
    "GET /api/browser-v2/comment-profile-metadata": response(200, {data: [], meta: {stale: false}}),
  }});

  await controller.init();

  assert.equal(controller.state.draft.mode, "threaded");
  assert.equal(controller.state.draft.template_id, "");
  assert.equal(controller.state.draft.template_revision, null);
  assert.deepEqual(renders.at(-1).model.templates.map((item) => item.id), ["threaded"]);
  assert.equal(renders.at(-1).model.templateEmpty, false);
  assert.equal(requests.some((item) => item.url.includes("selection/preview")), false);
});

test("initialization keeps the preferred mode when both modes have enabled trees", async () => {
  const records = [
    {id: "independent", name: "独立", revision: 1, enabled: true, supported_modes: ["independent"]},
    {id: "threaded", name: "盖楼", revision: 2, enabled: true, supported_modes: ["threaded"]},
  ];
  const {controller, renders} = harness({responses: {
    "GET /api/browser-v2/comment-templates": response(200, {data: records}),
    "GET /api/browser-v2/comment-profile-metadata": response(200, {data: [], meta: {stale: false}}),
  }});

  await controller.init();

  assert.equal(controller.state.draft.mode, "independent");
  assert.deepEqual(renders.at(-1).model.templates.map((item) => item.id), ["independent"]);
});

test("refreshing trees preserves a mode explicitly selected by the operator", async () => {
  let templateLoad = 0;
  const {controller, renders, requests} = harness({responses: {
    "GET /api/browser-v2/comment-templates": () => {
      templateLoad += 1;
      return response(200, {data: templateLoad === 1
        ? [{id: "threaded", name: "盖楼", revision: 2, enabled: true, supported_modes: ["threaded"]}]
        : [{id: "independent", name: "独立", revision: 1, enabled: true, supported_modes: ["independent"]}]});
    },
    "GET /api/browser-v2/comment-profile-metadata": response(200, {data: [], meta: {stale: false}}),
  }});
  await controller.init();
  await controller.updateDraft("mode", "threaded");
  const requestCount = requests.length;

  assert.equal(await controller.refreshTemplates(), true);

  assert.equal(controller.state.modeTouched, true);
  assert.equal(controller.state.draft.mode, "threaded");
  assert.equal(renders.at(-1).model.templateEmpty, true);
  assert.deepEqual(requests.slice(requestCount), [
    {url: "/api/browser-v2/comment-templates", method: "GET", body: undefined},
  ]);
});

test("refreshing the selected tree to a new revision refreshes automatic preview and ignores the old response", async () => {
  let templateLoad = 0;
  let releaseOldPreview;
  const oldPreview = new Promise((resolve) => { releaseOldPreview = resolve; });
  const {controller, requests} = harness({responses: {
    "GET /api/browser-v2/comment-templates": () => {
      templateLoad += 1;
      return response(200, {data: [{
        ...templates[0],
        revision: templateLoad === 1 ? 7 : 8,
      }]});
    },
    "GET /api/browser-v2/comment-profile-metadata": response(200, {data: [
      ...profiles,
      {...profiles[0], profile_ref: "opaque-b", display_profile: "窗口 B"},
    ], meta: {stale: false}}),
    "POST /api/browser-v2/comment-profile-selection/preview": (body) => body.template_revision === 7
      ? oldPreview
      : response(200, {data: {
        required_count: 1,
        eligible_count: 1,
        profiles: [{profile_ref: "opaque-b", display_profile: "窗口 B"}],
      }}),
  }});
  await controller.init();
  const stalePreview = controller.updateDraft("template_id", "tree-1");
  const requestCount = requests.length;

  assert.equal(await controller.refreshTemplates(), true);
  releaseOldPreview(response(200, {data: {
    required_count: 1,
    eligible_count: 1,
    profiles: [{profile_ref: "opaque-a", display_profile: "窗口 A"}],
  }}));
  await stalePreview;

  assert.equal(controller.state.draft.template_id, "tree-1");
  assert.equal(controller.state.draft.template_revision, 8);
  assert.deepEqual(controller.state.draft.profile_refs, ["opaque-b"]);
  assert.equal(controller.state.preview.inputKey, "tree-1:8:independent:automatic");
  assert.equal(controller.state.preview.status, "ready");
  assert.equal(controller.state.modeTouched, false);
  assert.deepEqual(requests.slice(requestCount), [
    {url: "/api/browser-v2/comment-templates", method: "GET", body: undefined},
    {
      url: "/api/browser-v2/comment-profile-selection/preview",
      method: "POST",
      body: {template_id: "tree-1", template_revision: 8, mode: "independent"},
    },
  ]);
});

test("refreshing a manually selected tree revision invalidates old Profiles without previewing", async () => {
  let templateLoad = 0;
  const {controller, requests} = harness({responses: {
    "GET /api/browser-v2/comment-templates": () => {
      templateLoad += 1;
      return response(200, {data: [{
        ...templates[0],
        revision: templateLoad === 1 ? 7 : 8,
      }]});
    },
    "GET /api/browser-v2/comment-profile-metadata": response(200, {data: profiles, meta: {stale: false}}),
    "POST /api/browser-v2/comment-profile-selection/preview": response(200, {data: {
      required_count: 1,
      eligible_count: 1,
      profiles: [{profile_ref: "opaque-a", display_profile: "窗口 A"}],
    }}),
  }});
  await controller.init();
  await controller.updateDraft("selection_mode", "manual");
  await controller.updateDraft("template_id", "tree-1");
  controller.toggleProfile("opaque-a", true);
  const requestCount = requests.length;

  assert.equal(await controller.refreshTemplates(), true);

  assert.equal(controller.state.draft.template_id, "tree-1");
  assert.equal(controller.state.draft.template_revision, 8);
  assert.deepEqual(controller.state.draft.profile_refs, []);
  assert.equal(controller.state.preview.status, "idle");
  assert.equal(controller.state.preview.inputKey, "");
  assert.ok(page.validateDraft(controller.state.draft, controller.state).preview);
  assert.equal(controller.state.modeTouched, false);
  assert.deepEqual(requests.slice(requestCount), [
    {url: "/api/browser-v2/comment-templates", method: "GET", body: undefined},
  ]);
});

test("an unavailable current mode exposes a neutral empty state", async () => {
  const {controller, renders} = harness({responses: {
    "GET /api/browser-v2/comment-templates": response(200, {data: []}),
    "GET /api/browser-v2/comment-profile-metadata": response(200, {data: [], meta: {stale: false}}),
  }});

  await controller.init();

  assert.equal(controller.state.draft.mode, "independent");
  assert.equal(renders.at(-1).model.templateEmpty, true);
  assert.equal(controller.state.fieldErrors.template_id, undefined);
});

test("Campaign form exposes comment-tree maintenance and refresh controls", () => {
  const source = fs.readFileSync(
    require.resolve("../gateway/templates/console_comment_campaign_create.html"),
    "utf8",
  );

  assert.match(source, /href="\/console\/actions\/comment-trees"[^>]*target="_blank"[^>]*rel="noopener"[^>]*>管理评论树<\/a>/);
  assert.match(source, /id="campaign-template-refresh"[^>]*type="button"[^>]*>刷新评论树<\/button>/);
  assert.match(source, /id="campaign-template-empty"[^>]*hidden[^>]*>当前没有可用评论树/);
});

test("automatic preview applies opaque refs and exact template revision", async () => {
  const {controller, requests} = harness({responses: {
    "GET /api/browser-v2/comment-templates": response(200, {data: templates}),
    "GET /api/browser-v2/comment-profile-metadata": response(200, {data: profiles, meta: {stale: false}}),
    "POST /api/browser-v2/comment-profile-selection/preview": response(200, {data: {
      required_count: 1,
      eligible_count: 1,
      profiles: [{profile_ref: "opaque-a", display_profile: "窗口 A"}],
    }}),
  }});
  await controller.init();

  await controller.updateDraft("template_id", "tree-1");

  assert.deepEqual(requests.at(-1), {
    url: "/api/browser-v2/comment-profile-selection/preview",
    method: "POST",
    body: {template_id: "tree-1", template_revision: 7, mode: "independent"},
  });
  assert.deepEqual(controller.state.draft.profile_refs, ["opaque-a"]);
  assert.equal(controller.state.preview.requiredCount, 1);
});

test("manual selection preserves the operator candidate pool", async () => {
  const {controller} = harness({responses: {
    "GET /api/browser-v2/comment-templates": response(200, {data: templates}),
    "GET /api/browser-v2/comment-profile-metadata": response(200, {data: profiles, meta: {stale: false}}),
    "POST /api/browser-v2/comment-profile-selection/preview": response(200, {data: {
      required_count: 1,
      eligible_count: 1,
      profiles: [{profile_ref: "opaque-a", display_profile: "窗口 A"}],
    }}),
  }});
  await controller.init();
  await controller.updateDraft("selection_mode", "manual");
  await controller.updateDraft("template_id", "tree-1");

  controller.toggleProfile("opaque-a", true);

  assert.deepEqual(controller.state.draft.profile_refs, ["opaque-a"]);
  assert.equal(controller.state.preview.requiredCount, 1);
});

test("manual Profile search exposes only safe visible fields", async () => {
  const unsafeProfiles = [{
    profile_ref: "opaque-secret",
    display_profile: "窗口 A",
    language: "zh",
    region: "CN",
    enabled: true,
    health_status: "healthy",
    expected_username: "must-not-render",
    raw_id: "raw-secret",
  }];
  const {controller, renders} = harness({responses: {
    "GET /api/browser-v2/comment-templates": response(200, {data: templates}),
    "GET /api/browser-v2/comment-profile-metadata": response(200, {data: unsafeProfiles, meta: {stale: false}}),
  }});
  await controller.init();
  await controller.updateDraft("selection_mode", "manual");
  controller.setProfileQuery("窗口");

  const row = renders.at(-1).model.profileRows[0];
  assert.deepEqual({display: row.display, locale: row.locale, status: row.status}, {
    display: "窗口 A",
    locale: "zh / CN",
    status: "可用",
  });
  assert.equal(JSON.stringify([row.display, row.locale, row.status]).includes("opaque-secret"), false);
  assert.equal(JSON.stringify([row.display, row.locale, row.status]).includes("must-not-render"), false);
  assert.equal(JSON.stringify([row.display, row.locale, row.status]).includes("raw-secret"), false);
});

test("stale preview cannot overwrite a newer selection", async () => {
  let releaseOld;
  const oldPreview = new Promise((resolve) => { releaseOld = resolve; });
  const records = [
    {id: "old", name: "旧树", revision: 1, enabled: true, supported_modes: ["independent"]},
    {id: "new", name: "新树", revision: 2, enabled: true, supported_modes: ["independent"]},
  ];
  const {controller} = harness({responses: {
    "GET /api/browser-v2/comment-templates": response(200, {data: records}),
    "GET /api/browser-v2/comment-profile-metadata": response(200, {data: [
      {profile_ref: "old-ref", display_profile: "旧窗口"},
      {profile_ref: "new-ref", display_profile: "新窗口"},
    ], meta: {stale: false}}),
    "POST /api/browser-v2/comment-profile-selection/preview": (body) => body.template_id === "old"
      ? oldPreview
      : response(200, {data: {
        required_count: 1,
        eligible_count: 1,
        profiles: [{profile_ref: "new-ref", display_profile: "新窗口"}],
      }}),
  }});
  await controller.init();

  const stale = controller.updateDraft("template_id", "old");
  await controller.updateDraft("template_id", "new");
  releaseOld(response(200, {data: {
    required_count: 1,
    eligible_count: 1,
    profiles: [{profile_ref: "old-ref", display_profile: "旧窗口"}],
  }}));
  await stale;

  assert.deepEqual(controller.state.draft.profile_refs, ["new-ref"]);
  assert.equal(controller.state.preview.inputKey, "new:2:independent:automatic");
});

function validDraft() {
  return {
    name: "Campaign",
    mode: "independent",
    target_reference: "https://www.tiktok.com/@creator/video/12345678",
    template_id: "tree-1",
    template_revision: 7,
    selection_mode: "manual",
    profile_refs: ["opaque-a"],
    batch_size: "3",
  };
}

function validContext() {
  return {
    templates: templates.map((item) => ({...item})),
    profiles: profiles.map((item) => ({...item})),
    preview: {
      status: "ready",
      inputKey: "tree-1:7:independent:manual",
      requiredCount: 1,
      eligibleCount: 1,
    },
  };
}

for (const [label, mutate, field] of [
  ["blank name", (draft) => { draft.name = " "; }, "name"],
  ["long name", (draft) => { draft.name = "x".repeat(101); }, "name"],
  ["non HTTPS URL", (draft) => { draft.target_reference = "http://www.tiktok.com/@a/video/12345678"; }, "target_reference"],
  ["non TikTok URL", (draft) => { draft.target_reference = "https://example.test/@a/video/12345678"; }, "target_reference"],
  ["non video URL", (draft) => { draft.target_reference = "https://www.tiktok.com/@creator"; }, "target_reference"],
  ["URL credentials", (draft) => { draft.target_reference = "https://user:pass@www.tiktok.com/@a/video/12345678"; }, "target_reference"],
  ["URL port", (draft) => { draft.target_reference = "https://www.tiktok.com:8443/@a/video/12345678"; }, "target_reference"],
  ["default URL port", (draft) => { draft.target_reference = "https://www.tiktok.com:443/@a/video/12345678"; }, "target_reference"],
  ["URL fragment", (draft) => { draft.target_reference = "https://www.tiktok.com/@a/video/12345678#comments"; }, "target_reference"],
  ["encoded path", (draft) => { draft.target_reference = "https://www.tiktok.com/@a%2Fb/video/12345678"; }, "target_reference"],
  ["dot segment path", (draft) => { draft.target_reference = "https://www.tiktok.com/x/../@a/video/12345678"; }, "target_reference"],
  ["encoded dot segment path", (draft) => { draft.target_reference = "https://www.tiktok.com/%2e%2e/@a/video/12345678"; }, "target_reference"],
  ["invalid username", (draft) => { draft.target_reference = "https://www.tiktok.com/@bad-name/video/12345678"; }, "target_reference"],
  ["long username", (draft) => { draft.target_reference = `https://www.tiktok.com/@${"a".repeat(25)}/video/12345678`; }, "target_reference"],
  ["batch decimal", (draft) => { draft.batch_size = "2.5"; }, "batch_size"],
  ["batch too small", (draft) => { draft.batch_size = "0"; }, "batch_size"],
  ["batch too large", (draft) => { draft.batch_size = "9"; }, "batch_size"],
  ["duplicate Profile", (draft) => { draft.profile_refs = ["opaque-a", "opaque-a"]; }, "profile_refs"],
  ["unknown Profile", (draft) => { draft.profile_refs = ["missing"]; }, "profile_refs"],
]) {
  test(`validation rejects ${label}`, () => {
    const draft = validDraft();
    mutate(draft);
    const errors = page.validateDraft(draft, validContext());
    assert.ok(errors[field]);
  });
}

test("validation accepts query characters ignored by backend path validation", () => {
  const draft = validDraft();
  draft.target_reference = "https://www.tiktok.com/@a/video/12345678?ref=foo\\bar";
  assert.equal(page.validateDraft(draft, validContext()).target_reference, undefined);
});

for (const [label, mutate] of [
  ["missing template", (context) => { context.templates = []; }],
  ["disabled template", (context) => { context.templates[0] = {...context.templates[0], enabled: false}; }],
  ["revision mismatch", (context) => { context.templates[0] = {...context.templates[0], revision: 8}; }],
  ["mode mismatch", (context) => { context.templates[0] = {...context.templates[0], supported_modes: ["threaded"]}; }],
  ["preview loading", (context) => { context.preview.status = "loading"; }],
  ["preview error", (context) => { context.preview.status = "error"; }],
  ["preview key mismatch", (context) => { context.preview.inputKey = "old"; }],
  ["Profile shortage", (context) => { context.preview.requiredCount = 2; }],
]) {
  test(`validation rejects ${label}`, () => {
    const context = validContext();
    mutate(context);
    const errors = page.validateDraft(validDraft(), context);
    assert.ok(errors.template_id || errors.profile_refs || errors.preview);
  });
}

test("validation rejects an invalid draft without sending a create request", async () => {
  const {controller, requests} = harness({responses: {}});
  controller.state.initialized = true;
  controller.state.draft = {
    ...controller.state.draft,
    name: "",
    target_reference: "http://example.test/video/1",
    batch_size: "9",
  };

  assert.equal(await controller.submit(), false);
  assert.equal(requests.length, 0);
  assert.ok(controller.state.fieldErrors.name);
  assert.ok(controller.state.fieldErrors.target_reference);
  assert.ok(controller.state.fieldErrors.batch_size);
});

test("payload contains only strict Campaign create fields", () => {
  assert.deepEqual(page.buildCreatePayload({
    name: "  Summer thread  ",
    mode: "threaded",
    target_reference: "https://www.tiktok.com/@creator/video/12345678",
    template_id: "tree-1",
    template_revision: 7,
    profile_refs: ["opaque-a", "opaque-b"],
    batch_size: "3",
    selection_mode: "manual",
    ignored: "must-not-leak",
  }), {
    name: "Summer thread",
    mode: "threaded",
    target_source: "manual_url",
    target_reference: "https://www.tiktok.com/@creator/video/12345678",
    template_id: "tree-1",
    template_revision: 7,
    profile_refs: ["opaque-a", "opaque-b"],
    batch_size: 3,
    start_mode: "manual",
  });
});

test("submit sends one request and navigation occurs only after 201", async () => {
  let release;
  const created = new Promise((resolve) => { release = resolve; });
  const {controller, requests, assigned} = harness({responses: {
    "POST /api/browser-v2/comment-campaigns": () => created,
  }});
  controller.state.initialized = true;
  controller.state.templates = templates;
  controller.state.profiles = profiles;
  controller.state.preview = validContext().preview;
  controller.state.draft = validDraft();

  const first = controller.submit();
  const second = controller.submit();
  assert.equal(await second, false);
  release(response(201, {data: {id: "c1"}}));
  assert.equal(await first, true);
  assert.equal(requests.filter((item) => item.method === "POST").length, 1);
  assert.deepEqual(assigned, ["/console/actions"]);
});

for (const [label, result] of [
  ["403 error", response(403, {error: {code: "forbidden", message: "无权操作"}})],
  ["422 error", response(422, {error: {code: "allocation_unsatisfied", message: "候选 Profile 不足"}})],
  ["503 error", response(503, {error: {code: "runtime_unavailable", message: "服务不可用"}})],
  ["invalid response body", response(503, {})],
]) {
  test(`draft survives ${label}`, async () => {
    const {controller, assigned} = harness({responses: {
      "POST /api/browser-v2/comment-campaigns": result,
    }});
    controller.state.initialized = true;
    controller.state.templates = templates;
    controller.state.profiles = profiles;
    controller.state.preview = validContext().preview;
    controller.state.draft = validDraft();
    const before = structuredClone(controller.state.draft);

    assert.equal(await controller.submit(), false);
    assert.deepEqual(controller.state.draft, before);
    assert.deepEqual(assigned, []);
    assert.ok(controller.state.error);
  });
}

test("draft survives network error", async () => {
  const {controller, assigned} = harness({responses: {
    "POST /api/browser-v2/comment-campaigns": () => { throw new Error("offline"); },
  }});
  controller.state.initialized = true;
  controller.state.templates = templates;
  controller.state.profiles = profiles;
  controller.state.preview = validContext().preview;
  controller.state.draft = validDraft();
  const before = structuredClone(controller.state.draft);

  assert.equal(await controller.submit(), false);
  assert.deepEqual(controller.state.draft, before);
  assert.deepEqual(assigned, []);
  assert.match(controller.state.error, /请求失败/);
});

test("Profile table redraw restores checkbox focus without exposing refs in the DOM", () => {
  const source = fs.readFileSync(
    require.resolve("../gateway/static/console_comment_campaign_create"),
    "utf8",
  );

  assert.match(source, /document\.activeElement/);
  assert.match(source, /focusedCheckboxIndex/);
  assert.match(source, /checkboxes\[focusedCheckboxIndex\]\.focus\(\)/);
  assert.doesNotMatch(source, /checkbox\.dataset\.profile/);
});
