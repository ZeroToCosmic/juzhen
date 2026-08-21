"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const page = require("../gateway/static/console_comment_trees.js");

function response(status, body) {
  return {status, data: body};
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return {promise, resolve};
}

function harness(responder, extra = {}) {
  const requests = [];
  const renders = [];
  const controller = page.createConsoleCommentTrees({
    requestJson: async (url, method = "GET", body) => {
      requests.push({url, method, body});
      return responder(url, method, body, requests.length);
    },
    confirm: extra.confirm,
    FormData: extra.FormData,
    render: (state, model) => renders.push({
      state: structuredClone(state),
      model: JSON.parse(JSON.stringify(model)),
    }),
  });
  return {controller, requests, renders};
}

const enabled = {
  id: "secret-enabled-id",
  name: "春季盖楼",
  supported_modes: ["threaded"],
  enabled: true,
  revision: 3,
  updated_at: "2026-08-21T00:00:00Z",
};
const disabled = {
  id: "secret-disabled-id",
  name: "独立评论",
  supported_modes: ["independent"],
  enabled: false,
  revision: 5,
  updated_at: "2026-08-20T08:30:00Z",
};

test("list model localizes visible fields and does not serialize internal template IDs", () => {
  const model = page.createListModel({
    templates: [enabled, disabled],
    filters: {query: "", mode: "all", status: "all"},
  });

  assert.equal(model.enabled[0].name, "春季盖楼");
  assert.equal(model.enabled[0].modeLabel, "盖楼回复");
  assert.equal(model.enabled[0].statusLabel, "启用");
  assert.equal(model.enabled[0].revisionLabel, "v3");
  assert.equal(model.enabled[0].updatedLabel, "2026-08-21 08:00");
  assert.equal(model.disabled[0].modeLabel, "独立评论");
  assert.equal(model.disabled[0].statusLabel, "停用");
  assert.equal(JSON.stringify(model).includes("secret-enabled-id"), false);
  assert.equal(JSON.stringify(model).includes("secret-disabled-id"), false);
});

test("filters combine name, mode, and status", () => {
  const state = {templates: [enabled, disabled], filters: {query: "春季", mode: "threaded", status: "enabled"}};
  let model = page.createListModel(state);
  assert.deepEqual(model.enabled.map((item) => item.name), ["春季盖楼"]);
  assert.equal(model.disabled.length, 0);

  state.filters = {query: "评论", mode: "independent", status: "disabled"};
  model = page.createListModel(state);
  assert.deepEqual(model.disabled.map((item) => item.name), ["独立评论"]);
  assert.equal(model.enabled.length, 0);
});

test("initialization loads comment templates and groups them", async () => {
  const {controller, requests, renders} = harness(async () => response(200, {data: [enabled, disabled]}));

  assert.equal(await controller.init(), true);

  assert.deepEqual(requests, [{url: "/api/browser-v2/comment-templates", method: "GET", body: undefined}]);
  assert.equal(renders.at(-1).model.enabled.length, 1);
  assert.equal(renders.at(-1).model.disabled.length, 1);
  assert.equal(controller.state.error, "");
});

test("disable sends current expected revision and refreshes once", async () => {
  const {controller, requests} = harness(async (url, method) => {
    if (method === "POST") return response(200, {data: {...enabled, enabled: false, revision: 4}});
    return response(200, {data: []});
  });

  assert.equal(await controller.transition(enabled, "disable"), true);

  assert.deepEqual(requests, [
    {url: "/api/browser-v2/comment-templates/secret-enabled-id/disable", method: "POST", body: {expected_revision: 3}},
    {url: "/api/browser-v2/comment-templates", method: "GET", body: undefined},
  ]);
});

test("duplicate lifecycle writes are blocked", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const {controller, requests} = harness(async (_url, method) => {
    if (method === "POST") { await gate; return response(200, {data: {}}); }
    return response(200, {data: []});
  });

  const first = controller.transition(enabled, "disable");
  assert.equal(await controller.transition(enabled, "disable"), false);
  release();
  assert.equal(await first, true);
  assert.equal(requests.filter((item) => item.method === "POST").length, 1);
});

test("lifecycle writes are blocked while openEdit detail is pending", async () => {
  const detail = deferred();
  const {controller, requests} = harness(async (_url, method) => {
    if (method === "GET") return detail.promise;
    return response(200, {data: {}});
  });

  const editing = controller.openEdit(enabled);
  assert.equal(await controller.transition(enabled, "disable"), false);
  assert.equal(requests.filter((item) => item.method === "POST").length, 0);
  detail.resolve(response(200, {data: {...enabled, steps: [{id: "node", content_source: "fixed", fixed_text: "text", parent_step_id: null}]}}));
  assert.equal(await editing, true);
});

test("delete requires confirmation and cancellation sends no request", async () => {
  const {controller, requests} = harness(async () => response(200, {data: []}), {confirm: () => false});

  assert.equal(await controller.transition(disabled, "delete"), false);
  assert.equal(requests.length, 0);
});

test("enabled and disabled lifecycle actions are constrained", async () => {
  const {controller, requests} = harness(async () => response(200, {data: []}), {confirm: () => true});

  assert.equal(await controller.transition(enabled, "enable"), false);
  assert.equal(await controller.transition(enabled, "delete"), false);
  assert.equal(await controller.transition(disabled, "disable"), false);
  assert.equal(requests.length, 0);
});

test("lifecycle errors use Chinese messages, refresh stale metadata, and never retry writes", async (t) => {
  const cases = [
    [403, "当前账号无权维护评论树", false],
    [404, "评论树已不存在", true],
    [409, "评论树已被其他操作更新", true],
    [422, "当前状态不允许执行此操作", false],
    [503, "评论树服务暂时不可用", false],
  ];
  for (const [status, message, refreshes] of cases) {
    await t.test(String(status), async () => {
      const {controller, requests} = harness(async (_url, method) => (
        method === "POST" ? response(status, {error: {code: "raw_code", message: "raw server text"}}) : response(200, {data: []})
      ));
      assert.equal(await controller.transition(enabled, "disable"), false);
      assert.match(controller.state.error, new RegExp(message));
      assert.equal(requests.filter((item) => item.method === "POST").length, 1);
      assert.equal(requests.filter((item) => item.method === "GET").length, refreshes ? 1 : 0);
      assert.equal(controller.state.error.includes("raw_code"), false);
    });
  }
});

test("network failures are reported without automatic retry", async () => {
  const {controller, requests} = harness(async () => { throw new Error("socket exploded"); });
  assert.equal(await controller.transition(enabled, "disable"), false);
  assert.equal(controller.state.error, "网络连接失败，请稍后手动重试。");
  assert.equal(requests.length, 1);
});

test("workspace actions switch views and openEdit loads an editable draft", async () => {
  const {controller, requests} = harness(async (url) => response(200, {data: {...enabled, steps: [{id: "node", content_source: "fixed", fixed_text: "text", parent_step_id: null}]}}));
  controller.openCreate();
  assert.equal(controller.state.view, "editor");
  controller.closeWorkspace();
  controller.openImport();
  assert.equal(controller.state.view, "import");
  controller.closeWorkspace();
  assert.equal(await controller.openEdit(enabled), true);
  assert.equal(controller.state.view, "editor");
  assert.equal(controller.state.draft.name, "春季盖楼");
  assert.deepEqual(requests, [{url: "/api/browser-v2/comment-templates/secret-enabled-id", method: "GET", body: undefined}]);
});

test("workspace navigation is blocked during loading and submitting without changing drafts", () => {
  const {controller} = harness(async () => response(200, {data: []}));
  controller.openCreate();
  controller.state.draft.name = "保留草稿";
  const before = structuredClone(controller.state.draft);
  for (const flag of ["loading", "submitting"]) {
    controller.state[flag] = true;
    const view = controller.state.view;
    assert.equal(controller.openCreate(), false);
    assert.equal(controller.openImport(), false);
    assert.equal(controller.closeWorkspace(), false);
    assert.equal(controller.state.view, view);
    assert.deepEqual(controller.state.draft, before);
    controller.state[flag] = false;
  }
});

test("openEdit 404 returns to list, refreshes templates once, and keeps the Chinese error", async () => {
  const {controller, requests} = harness(async (_url, method, _body, requestNumber) => (
    requestNumber === 1
      ? response(404, {error: {code: "not_found"}})
      : response(200, {data: [disabled]})
  ));

  assert.equal(await controller.openEdit(enabled), false);

  assert.deepEqual(requests, [
    {url: "/api/browser-v2/comment-templates/secret-enabled-id", method: "GET", body: undefined},
    {url: "/api/browser-v2/comment-templates", method: "GET", body: undefined},
  ]);
  assert.equal(controller.state.view, "list");
  assert.match(controller.state.error, /评论树已不存在/);
  assert.deepEqual(controller.state.templates, [disabled]);
});

test("stale openEdit detail cannot overwrite state after refresh, workspace switch, or newer openEdit", async (t) => {
  const oldDetail = {id: "old", name: "旧详情", revision: 1, supported_modes: ["threaded"], steps: [
    {id: "old-node", content_source: "fixed", fixed_text: "old", parent_step_id: null},
  ]};

  await t.test("refresh", async () => {
    const pending = deferred();
    const {controller} = harness(async (url) => (
      url.endsWith("/old") ? pending.promise : response(200, {data: [disabled]})
    ));
    controller.openCreate();
    controller.state.draft.name = "保留草稿";
    const editing = controller.openEdit({id: "old"});
    assert.equal(await controller.refresh(), true);
    pending.resolve(response(200, {data: oldDetail}));
    assert.equal(await editing, false);
    assert.equal(controller.state.view, "editor");
    assert.equal(controller.state.draft.name, "保留草稿");
    assert.equal(controller.state.readonlyTemplate, null);
  });

  await t.test("workspace switch", async () => {
    const pending = deferred();
    const {controller} = harness(async () => pending.promise);
    const editing = controller.openEdit({id: "old"});
    assert.equal(controller.openCreate(), true);
    controller.state.draft.name = "新建草稿";
    pending.resolve(response(200, {data: oldDetail}));
    assert.equal(await editing, false);
    assert.equal(controller.state.view, "editor");
    assert.equal(controller.state.draft.name, "新建草稿");
    assert.equal(controller.state.readonlyTemplate, null);
  });

  await t.test("newer openEdit", async () => {
    const pending = deferred();
    const newerDetail = {...oldDetail, id: "new", name: "较新详情", revision: 2};
    const {controller} = harness(async (url) => (
      url.endsWith("/old") ? pending.promise : response(200, {data: newerDetail})
    ));
    const older = controller.openEdit({id: "old"});
    assert.equal(await controller.openEdit({id: "new"}), true);
    pending.resolve(response(200, {data: oldDetail}));
    assert.equal(await older, false);
    assert.equal(controller.state.view, "editor");
    assert.equal(controller.state.draft.name, "较新详情");
    assert.equal(controller.state.readonlyTemplate, null);
  });
});

test("requestJson wraps JSON bodies and leaves GET requests bodyless", async () => {
  const calls = [];
  const win = {fetch: async (url, options) => {
    calls.push({url, options});
    return {status: 200, json: async () => ({data: []})};
  }};
  await page.requestJson(win, "/read", "GET");
  await page.requestJson(win, "/write", "POST", {expected_revision: 7});
  assert.deepEqual(calls[0], {url: "/read", options: {method: "GET", credentials: "same-origin"}});
  assert.deepEqual(calls[1], {url: "/write", options: {
    method: "POST",
    credentials: "same-origin",
    headers: {"Content-Type": "application/json"},
    body: '{"expected_revision":7}',
  }});
});

test("controller source uses safe DOM rendering and never projects template IDs", () => {
  const source = fs.readFileSync(require.resolve("../gateway/static/console_comment_trees.js"), "utf8");
  assert.match(source, /createElement/);
  assert.match(source, /textContent/);
  assert.doesNotMatch(source, /innerHTML/);
  assert.doesNotMatch(source, /dataset\.(template|id)|setAttribute\([^\n]*(template|title)/i);
});

test("mobile table cells receive all six Chinese data labels without template IDs", () => {
  const source = fs.readFileSync(require.resolve("../gateway/static/console_comment_trees.js"), "utf8");
  assert.match(source, /\["评论树", "支持模式", "状态", "版本", "最近更新", "操作"\]/);
  assert.match(source, /cell\.dataset\.label\s*=\s*label/);
  assert.doesNotMatch(source, /dataset\.label\s*=\s*[^;]*(template\.id|summary\.template\.id)/);
});

test("new manual tree starts as a threaded CommentTreeEditor draft and saves with POST", async () => {
  const {controller, requests} = harness(async (_url, method) => (
    method === "POST" ? response(201, {data: {id: "created"}}) : response(200, {data: []})
  ));

  controller.openCreate();
  assert.equal(controller.state.draft.mode, "threaded");
  assert.equal(controller.state.draft.nodes.length, 1);
  controller.state.draft.name = "新评论树";
  controller.state.draft.nodes[0].text = "楼主文案";

  assert.equal(await controller.saveDraft(), true);
  assert.equal(requests[0].url, "/api/browser-v2/comment-templates");
  assert.equal(requests[0].method, "POST");
  assert.equal(requests[0].body.supported_modes[0], "threaded");
  assert.equal(controller.state.view, "list");
});

test("a successful save is not repeatable when the following list refresh fails", async () => {
  const {controller, requests} = harness(async (_url, method) => {
    if (method === "POST") return response(201, {data: {id: "created"}});
    throw new Error("refresh failed");
  });
  controller.openCreate();
  controller.state.draft.name = "只写一次";
  controller.state.draft.nodes[0].text = "内容";

  assert.equal(await controller.saveDraft(), true);
  assert.equal(controller.state.draft, null);
  assert.equal(controller.state.view, "list");
  assert.equal(await controller.saveDraft(), false);
  assert.equal(requests.filter((item) => item.method === "POST").length, 1);
});

test("fixed single-mode detail becomes a complete draft and PUT preserves metadata", async () => {
  const detail = {id: "tree-1", name: "旧树", description: "描述", language: "zh", tags: ["tree-tag"], revision: 7, supported_modes: ["threaded"], steps: [
    {id: "node-1", label: "自定义楼主", content_source: "fixed", fixed_text: "root", parent_step_id: null, required_profile_tags: ["required"], excluded_profile_tags: ["excluded"], language: "zh"},
  ]};
  const {controller, requests} = harness(async (url, method) => {
    if (method === "GET" && url.endsWith("/tree-1")) return response(200, {data: detail});
    if (method === "PUT") return response(200, {data: {id: "tree-1"}});
    return response(200, {data: []});
  });

  assert.equal(await controller.openEdit({id: "tree-1"}), true);
  assert.deepEqual(controller.state.draft, {
    name: "旧树", description: "描述", language: "zh", tags: ["tree-tag"], mode: "threaded", source: "manual", advanced: false,
    editingTemplateId: "tree-1", expectedRevision: 7,
    nodes: [{id: "node-1", label: "自定义楼主", text: "root", parentId: null, requiredProfileTags: ["required"], excludedProfileTags: ["excluded"], language: "zh"}],
  });
  assert.equal(await controller.saveDraft(), true);
  const update = requests.find((item) => item.method === "PUT");
  assert.equal(update.url, "/api/browser-v2/comment-templates/tree-1");
  assert.equal(update.body.expected_revision, 7);
  assert.equal(update.body.description, "描述");
  assert.deepEqual(update.body.tags, ["tree-tag"]);
  assert.equal(update.body.steps[0].id, "node-1");
  assert.equal(update.body.steps[0].label, "自定义楼主");
  assert.deepEqual(update.body.steps[0].required_profile_tags, ["required"]);
});

test("save validation and failed writes preserve the complete manual draft", async () => {
  const {controller, requests} = harness(async (_url, method) => (
    method === "POST" ? response(409, {error: {code: "raw_conflict"}}) : response(200, {data: []})
  ));
  controller.openCreate();
  const invalid = structuredClone(controller.state.draft);
  assert.equal(await controller.saveDraft(), false);
  assert.equal(requests.length, 0);
  assert.deepEqual(controller.state.draft, invalid);

  controller.state.draft.name = "保留草稿";
  controller.state.draft.nodes[0].text = "保留文案";
  const valid = structuredClone(controller.state.draft);
  assert.equal(await controller.saveDraft(), false);
  assert.deepEqual(controller.state.draft, valid);
  assert.equal(requests.filter((item) => item.method === "POST").length, 1);
  assert.equal(requests.filter((item) => item.method === "GET").length, 1);
});

test("all save failures keep the draft, use Chinese copy, and never retry writes", async (t) => {
  for (const [status, message] of [[403, "无权"], [404, "不存在"], [422, "不允许"], [503, "暂时不可用"]]) {
    await t.test(String(status), async () => {
      const {controller, requests} = harness(async () => response(status, {error: {code: "raw_code"}}));
      controller.openCreate();
      controller.state.draft.name = "保留";
      controller.state.draft.nodes[0].text = "保留";
      const before = structuredClone(controller.state.draft);
      assert.equal(await controller.saveDraft(), false);
      assert.deepEqual(controller.state.draft, before);
      assert.match(controller.state.error, new RegExp(message));
      assert.equal(controller.state.error.includes("raw_code"), false);
      assert.equal(requests.filter((item) => item.method === "POST").length, 1);
      assert.equal(requests.filter((item) => item.method === "GET").length, status === 404 ? 1 : 0);
    });
  }
  await t.test("network", async () => {
    const {controller, requests} = harness(async () => { throw new Error("raw socket"); });
    controller.openCreate();
    controller.state.draft.name = "保留";
    controller.state.draft.nodes[0].text = "保留";
    const before = structuredClone(controller.state.draft);
    assert.equal(await controller.saveDraft(), false);
    assert.deepEqual(controller.state.draft, before);
    assert.match(controller.state.error, /网络连接失败/);
    assert.equal(requests.length, 1);
  });
});

test("edit save 404 refreshes list once without retrying PUT and preserves draft", async () => {
  const detail = {id: "gone", name: "待保存", revision: 4, supported_modes: ["threaded"], steps: [
    {id: "node", content_source: "fixed", fixed_text: "文案", parent_step_id: null},
  ]};
  const {controller, requests} = harness(async (url, method, _body, number) => {
    if (number === 1) return response(200, {data: detail});
    if (method === "PUT") return response(404, {error: {code: "raw_not_found"}});
    return response(200, {data: [disabled]});
  });
  assert.equal(await controller.openEdit({id: "gone"}), true);
  const before = structuredClone(controller.state.draft);

  assert.equal(await controller.saveDraft(), false);
  assert.deepEqual(requests.map(({url, method}) => ({url, method})), [
    {url: "/api/browser-v2/comment-templates/gone", method: "GET"},
    {url: "/api/browser-v2/comment-templates/gone", method: "PUT"},
    {url: "/api/browser-v2/comment-templates", method: "GET"},
  ]);
  assert.equal(requests[1].body.expected_revision, 4);
  assert.equal(requests.filter((item) => item.method === "PUT").length, 1);
  assert.equal(requests.filter((item) => item.method === "GET").length, 2);
  assert.deepEqual(controller.state.draft, before);
  assert.match(controller.state.error, /评论树已不存在/);
  assert.equal(controller.state.error.includes("raw_not_found"), false);
});

test("library and multi-mode details are read only and preserve a manual draft", async () => {
  const details = {
    library: {id: "library", name: "文案库树", supported_modes: ["threaded"], steps: [{content_source: "library"}]},
    multi: {id: "multi", name: "多模式树", supported_modes: ["threaded", "independent"], steps: [{content_source: "fixed"}]},
  };
  const {controller, requests} = harness(async (url) => response(200, {data: details[url.split("/").at(-1)]}));
  controller.openCreate();
  controller.state.draft.name = "手工草稿";

  assert.equal(await controller.openEdit({id: "library"}), false);
  assert.equal(controller.state.draft.name, "手工草稿");
  assert.equal(await controller.openEdit({id: "multi"}), false);
  assert.equal(controller.state.draft.name, "手工草稿");
  assert.equal(controller.state.readonlyTemplate.name, "多模式树");
  assert.equal(await controller.saveDraft(), false);
  assert.equal(requests.some((item) => item.method === "POST" || item.method === "PUT"), false);
});

test("Excel preview accepts only xlsx, uses FormData, and selects valid trees", async () => {
  class FakeFormData {
    constructor() { this.parts = []; }
    append(name, value) { this.parts.push([name, value]); }
  }
  const preview = {trees: [
    {name: "有效树", valid: true, nodes: [{node_no: 0, parent_node_no: null, text: "root"}], errors: []},
    {name: "错误树", valid: false, nodes: [], errors: [{code: "parent_not_found", row: 3}]},
  ], summary: {tree_count: 2, valid_count: 1, rejected_count: 1}};
  const {controller, requests} = harness(async () => response(200, {data: preview}), {FormData: FakeFormData});

  assert.equal(await controller.previewImport({name: "trees.csv"}), false);
  assert.equal(requests.length, 0);
  const file = {name: "trees.xlsx", type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"};
  assert.equal(await controller.previewImport(file), true);
  assert.equal(requests[0].url, "/api/browser-v2/comment-template-imports/preview");
  assert.equal(requests[0].method, "POST");
  assert.ok(requests[0].body instanceof FakeFormData);
  assert.deepEqual(requests[0].body.parts, [["file", file]]);
  assert.deepEqual(controller.state.importDraft.trees.map((tree) => tree.selected), [true, false]);
  assert.equal(page.importErrorText({code: "parent_not_found", row: 3}), "第 3 行：找不到回复目标");
  assert.equal(page.importErrorText({code: "secret_raw_code"}), "导入内容无效");
});

test("Excel preview maps upload failures to Chinese and retains an existing import draft", async (t) => {
  class FakeFormData { append() {} }
  for (const [status, message] of [[413, "文件过大"], [422, "文件或导入内容无效"], [503, "暂时不可用"]]) {
    await t.test(String(status), async () => {
      const {controller, requests} = harness(async () => response(status, {error: {code: "raw_code"}}), {FormData: FakeFormData});
      controller.state.importDraft = {trees: [{name: "保留"}]};
      assert.equal(await controller.previewImport({name: "trees.xlsx"}), false);
      assert.equal(controller.state.importDraft.trees[0].name, "保留");
      assert.match(controller.state.error, new RegExp(message));
      assert.equal(controller.state.error.includes("raw_code"), false);
      assert.equal(requests.length, 1);
    });
  }
});

test("import commit sends only selected valid tree fields and preserves numeric zero", async () => {
  const {controller, requests} = harness(async (_url, method) => (
    method === "POST" ? response(201, {data: {created: [{name: "零节点树"}], rejected: []}}) : response(200, {data: []})
  ));
  controller.state.view = "import";
  controller.state.importDraft = {trees: [
    {name: " 零节点树 ", valid: true, selected: true, extra: "drop", nodes: [{node_no: 0, parent_node_no: null, text: "root", row: 2}, {node_no: 1, parent_node_no: 0, text: "child", position: 1}]},
    {name: "未选", valid: true, selected: false, nodes: []},
  ]};

  assert.equal(await controller.commitImport(), true);
  assert.deepEqual(requests[0], {url: "/api/browser-v2/comment-template-imports", method: "POST", body: {trees: [{name: "零节点树", nodes: [
    {node_no: "0", parent_node_no: null, text: "root"},
    {node_no: "1", parent_node_no: "0", text: "child"},
  ]}]}});
  assert.equal(controller.state.importDraft, null);
  assert.equal(controller.state.view, "list");
});

test("partial import refreshes and retains only rejected trees as invalid", async () => {
  const {controller, requests} = harness(async (_url, method) => (
    method === "POST"
      ? response(201, {data: {created: [{name: "已创建"}], rejected: [{name: "失败树", errors: [{code: "import_tree_failed"}]}]}})
      : response(200, {data: []})
  ));
  controller.state.importDraft = {trees: [
    {name: "已创建", valid: true, selected: true, nodes: [{node_no: "1", parent_node_no: null, text: "root"}]},
    {name: "失败树", valid: true, selected: true, nodes: [{node_no: "1", parent_node_no: null, text: "root"}]},
  ]};

  assert.equal(await controller.commitImport(), true);
  assert.deepEqual(controller.state.importDraft.trees.map((tree) => ({name: tree.name, valid: tree.valid, selected: tree.selected})), [
    {name: "失败树", valid: false, selected: false},
  ]);
  assert.equal(requests.filter((item) => item.method === "GET").length, 1);
  assert.match(controller.state.error, /部分评论树导入成功/);
});

test("empty selection and fully rejected import send no extra writes and retain failures", async () => {
  const {controller, requests} = harness(async () => response(201, {data: {created: [], rejected: [{name: "失败树", errors: [{code: "template_invalid"}]}]}}));
  controller.state.importDraft = {trees: [{name: "无效", valid: false, selected: true, nodes: []}]};
  assert.equal(await controller.commitImport(), false);
  assert.equal(requests.length, 0);
  controller.state.importDraft = {trees: [{name: "失败树", valid: true, selected: true, nodes: [{node_no: "1", parent_node_no: null, text: "root"}]}]};
  assert.equal(await controller.commitImport(), false);
  assert.equal(requests.length, 1);
  assert.equal(controller.state.importDraft.trees[0].valid, false);
  assert.equal(controller.state.importDraft.trees[0].selected, false);
  assert.match(controller.state.error, /未导入/);
});

test("requestJson passes FormData through without setting Content-Type", async () => {
  let call;
  const win = {FormData, fetch: async (url, options) => { call = {url, options}; return {status: 200, json: async () => ({data: {}})}; }};
  const form = new FormData();
  form.append("file", new Blob(["sheet"]), "trees.xlsx");
  await page.requestJson(win, "/upload", "POST", form);
  assert.equal(call.options.body, form);
  assert.equal("headers" in call.options, false);
  assert.equal(call.options.credentials, "same-origin");
});

test("DOM wiring renders CommentTreeEditor with draft, confirmations, and save callback", () => {
  const source = fs.readFileSync(require.resolve("../gateway/static/console_comment_trees.js"), "utf8");
  assert.match(source, /editor\.render\(\{/);
  assert.match(source, /onDraftChange:\s*function/);
  assert.match(source, /onSave:\s*function\s*\(\)\s*\{\s*controller\.saveDraft\(\)/);
  assert.match(source, /confirmModeChange:/);
  assert.match(source, /confirmRemoveDescendants:/);
});

test("template exposes an import commit button and controller binds both import actions", () => {
  const template = fs.readFileSync("gateway/templates/console_comment_trees.html", "utf8");
  const source = fs.readFileSync(require.resolve("../gateway/static/console_comment_trees.js"), "utf8");
  assert.match(template, /id="comment-tree-import-commit"/);
  assert.match(source, /comment-tree-import-preview["']\)\?\.addEventListener/);
  assert.match(source, /comment-tree-import-commit["']\)\?\.addEventListener/);
});

test("renderDom disables navigation and write controls while busy", () => {
  const ids = ["comment-trees-refresh", "comment-trees-create", "comment-trees-import", "comment-tree-editor-back", "comment-tree-import-back", "comment-tree-import-preview", "comment-tree-import-commit"];
  const nodes = Object.fromEntries(ids.map((id) => [id, {disabled: false}]));
  nodes["console-comment-trees"] = {dataset: {}};
  const document = {getElementById: (id) => nodes[id] || null};
  const base = {view: "import", filters: {}, templates: [], draft: null, readonlyTemplate: null, importDraft: {trees: [{valid: true, selected: true}]}, error: "", status: ""};
  for (const flag of ["loading", "submitting"]) {
    ids.forEach((id) => { nodes[id].disabled = false; });
    page.renderDom(document, {...base, loading: flag === "loading", submitting: flag === "submitting"}, {enabled: [], disabled: []}, {});
    ids.forEach((id) => assert.equal(nodes[id].disabled, true, id + " should be disabled for " + flag));
  }
});

test("renderDom disables row action buttons while loading or submitting", () => {
  function element(tagName) {
    return {
      tagName,
      children: [],
      dataset: {},
      classList: {toggle() {}},
      append(...children) { this.children.push(...children); },
      replaceChildren(...children) { this.children = children; },
      addEventListener() {},
    };
  }
  const root = element("main");
  const enabledBody = element("tbody");
  const nodes = {"console-comment-trees": root, "comment-tree-enabled-body": enabledBody};
  const document = {
    getElementById: (id) => nodes[id] || null,
    createElement: element,
  };
  const base = {view: "list", filters: {query: "", mode: "all", status: "all"}, templates: [enabled], draft: null, readonlyTemplate: null, importDraft: null, error: "", status: ""};
  const model = page.createListModel(base);

  for (const flag of ["loading", "submitting"]) {
    page.renderDom(document, {...base, loading: flag === "loading", submitting: flag === "submitting"}, model, {});
    const buttons = enabledBody.children[0].children[5].children;
    assert.equal(buttons.length, 2);
    buttons.forEach((button) => assert.equal(button.disabled, true, button.textContent + " should be disabled for " + flag));
  }
});

test("renderDom labels new, editable, and read-only editor workspaces", () => {
  const title = {textContent: ""};
  const root = {dataset: {}};
  const document = {getElementById: (id) => ({"console-comment-trees": root, "comment-tree-editor-title": title})[id] || null};
  const base = {view: "editor", filters: {}, templates: [], importDraft: null, loading: false, submitting: false, error: "", status: ""};

  page.renderDom(document, {...base, draft: {nodes: []}, readonlyTemplate: null}, {enabled: [], disabled: []}, {});
  assert.equal(title.textContent, "新建评论树");
  page.renderDom(document, {...base, draft: {editingTemplateId: "opaque", nodes: []}, readonlyTemplate: null}, {enabled: [], disabled: []}, {});
  assert.equal(title.textContent, "编辑评论树");
  page.renderDom(document, {...base, draft: {nodes: []}, readonlyTemplate: {name: "只读"}}, {enabled: [], disabled: []}, {});
  assert.equal(title.textContent, "查看评论树");
});
