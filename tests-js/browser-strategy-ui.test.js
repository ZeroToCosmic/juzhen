const assert = require("node:assert/strict");
const test = require("node:test");

const {
  browserDependencies,
  createBrowserStrategyUI,
} = require("../gateway/static/browser_strategy_ui");

function clone(value) {
  if (value === undefined) return undefined;
  return JSON.parse(JSON.stringify(value));
}

function response(status, data) {
  return {status, data};
}

function browserHarness(fetchResponse) {
  const fetchCalls = [];
  const destinations = [];
  const win = {
    document: {
      querySelector: (selector) => (
        selector === 'meta[name="csrf-token"]'
          ? {content: "csrf-dashboard"}
          : null
      ),
      querySelectorAll: () => [],
    },
    location: {
      href: "https://console.example.test/",
      origin: "https://console.example.test",
      assign: (destination) => destinations.push(destination),
    },
    fetch: async (url, options) => {
      fetchCalls.push({url, options});
      return fetchResponse(url, options);
    },
    setTimeout: () => 1,
    clearTimeout: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    confirm: () => true,
  };
  return {
    dependencies: browserDependencies(win),
    destinations,
    fetchCalls,
  };
}

test("browser requests attach csrf only to same-origin unsafe methods", async () => {
  const {dependencies, fetchCalls} = browserHarness(async () => ({
    status: 200,
    json: async () => ({}),
  }));

  await dependencies.requestJson(
    "/api/browser/elements",
    "PUT",
    {elements: {}},
  );
  await dependencies.requestJson(
    "https://api.example.test/update",
    "POST",
    {},
  );
  await dependencies.requestJson("/api/browser/elements", "GET");

  assert.equal(
    fetchCalls[0].options.headers["X-CSRF-Token"],
    "csrf-dashboard",
  );
  assert.equal(
    fetchCalls[1].options.headers["X-CSRF-Token"],
    undefined,
  );
  assert.equal(
    fetchCalls[2].options.headers["X-CSRF-Token"],
    undefined,
  );
});

test("same-origin authentication failure navigates to login", async () => {
  const {dependencies, destinations} = browserHarness(async () => ({
    status: 401,
    json: async () => ({code: "authentication_required"}),
  }));

  await dependencies.requestJson("/api/browser/elements", "GET");

  assert.deepEqual(destinations, ["/login"]);
});

function harness(overrides = {}) {
  const requests = [];
  const listeners = new Map();
  const dependencies = {
    requestJson: async (url, method = "GET", body) => {
      requests.push({url, method, body: clone(body)});
      const handler = overrides.responses?.[`${method} ${url}`];
      return typeof handler === "function" ? handler(body) : handler;
    },
    selectedBrowserWindows: () => overrides.windows || [],
    setTimeout: overrides.setTimeout || (() => 1),
    clearTimeout: overrides.clearTimeout || (() => {}),
    addBeforeUnload: (handler) => listeners.set("beforeunload", handler),
    removeBeforeUnload: () => listeners.delete("beforeunload"),
    confirm: overrides.confirm || (() => true),
    nowId: (() => {
      let id = 0;
      return (prefix) => `${prefix}_${++id}`;
    })(),
    render: overrides.render || (() => {}),
    targetElementSelectors: overrides.targetElementSelectors || (() => []),
    selectorProbe: overrides.selectorProbe,
  };
  return {
    ui: createBrowserStrategyUI(dependencies),
    requests,
    listeners,
  };
}

test("browser controller composes one isolated selector probe controller", async () => {
  let probeInitCount = 0;
  const selectorProbe = {
    state: {activeTab: "overview"},
    init: async () => {
      probeInitCount += 1;
    },
  };
  const {ui} = harness({
    responses: loadResponses(),
    selectorProbe,
  });

  await ui.init();
  await ui.init();

  assert.equal(probeInitCount, 1);
  assert.equal(ui.selectorProbe, selectorProbe);
  assert.equal(Object.hasOwn(ui.state, "activeTab"), false);
  assert.equal(Object.hasOwn(ui.state, "gates"), false);
});

const catalog = {
  move: {label: "移动", pattern_type: "mouse"},
  click: {label: "点击", pattern_type: "mouse"},
  scroll_up: {label: "向上滚动", pattern_type: null},
  scroll_down: {label: "向下滚动", pattern_type: null},
  keyboard_input: {label: "键盘输入", pattern_type: "keyboard"},
  pause: {label: "停止（等待）", pattern_type: null},
};

const defaults = {
  move: {target_mode: "element", element: "", delta_viewport: [0, 0], trajectory: {source: "builtin", id: "bezier"}, duration_seconds: [0.2, 0.8]},
  click: {element: "", button: "left", click_count: 1, hold_seconds: [0.05, 0.15], trajectory: {source: "builtin", id: "bezier"}},
  scroll_up: {distance: 120, total_count: [1, 1], burst_count: [1, 1], interval_seconds: [0.1, 0.3]},
  scroll_down: {distance: 120, total_count: [1, 1], burst_count: [1, 1], interval_seconds: [0.1, 0.3]},
  keyboard_input: {element: "", content: {source: "fixed", text: "", brand_id: ""}, typing: {source: "builtin", interval_ms: [50, 250]}},
  pause: {duration_seconds: [1, 1]},
};

function loadResponses(resources = {}) {
  return {
    "GET /api/browser/elements": response(200, {elements: resources.elements || {入口: "xpath=//button"}}),
    "GET /api/browser/patterns": response(200, {patterns: resources.patterns || []}),
    "GET /api/browser/strategies": response(200, {strategies: resources.strategies || []}),
    "GET /api/browser/action-catalog": response(200, {catalog, defaults}),
    "GET /api/content/brands": response(200, {brands: resources.brands || []}),
  };
}

test("init reloads every canonical resource and renders list state", async () => {
  const rendered = [];
  const {ui, requests} = harness({
    responses: loadResponses({strategies: [{id: "s1", name: "发布", run_mode: "once", batch_size: 1, actions: [], status: "ready"}]}),
    render: (view, state) => rendered.push([view, clone(state)]),
  });

  await ui.init();

  assert.deepEqual(requests.map((item) => `${item.method} ${item.url}`), [
    "GET /api/browser/elements",
    "GET /api/browser/patterns",
    "GET /api/browser/strategies",
    "GET /api/browser/action-catalog",
    "GET /api/content/brands",
  ]);
  assert.equal(ui.state.strategies[0].name, "发布");
  assert.equal(ui.state.view, "list");
  assert.equal(rendered.at(-1)[0], "all");
});

test("init is idempotent across concurrent and repeated calls without overwriting an editor", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const responses = loadResponses();
  const originalElements = responses["GET /api/browser/elements"];
  responses["GET /api/browser/elements"] = async () => { await gate; return originalElements; };
  const {ui, requests} = harness({responses});

  const first = ui.init();
  const second = ui.init();
  assert.equal(first, second);
  release();
  await first;
  assert.deepEqual(requests.map((item) => item.url), [
    "/api/browser/elements", "/api/browser/patterns", "/api/browser/strategies",
    "/api/browser/action-catalog", "/api/content/brands",
  ]);

  ui.createStrategy("未保存编辑");
  const draftId = ui.state.draft.id;
  const third = ui.init();
  assert.equal(third, first);
  await third;
  assert.equal(ui.state.draft.id, draftId);
  assert.equal(ui.state.dirty, true);
  assert.equal(requests.length, 5);
});

test("failed init clears its cache so a later retry can load canonical state", async () => {
  let attempts = 0;
  const responses = loadResponses();
  responses["GET /api/browser/elements"] = () => {
    attempts += 1;
    return attempts === 1 ? response(500, {error: "temporary"}) : response(200, {elements: {重试成功: "x"}});
  };
  const {ui} = harness({responses});

  await assert.rejects(ui.init(), /temporary/);
  await ui.init();
  assert.deepEqual(ui.state.elements, {重试成功: "x"});
  assert.equal(attempts, 2);
});

test("syncElementOptions rebuilds every target selector and preserves valid values", () => {
  const {ui} = harness();
  ui.state.elements = {入口: "x", 输入框: "y"};
  const selectors = [
    {value: "入口", options: []},
    {value: "已删除", options: []},
  ];

  ui.syncElementOptions(selectors);

  assert.deepEqual(selectors[0].options, [{value: "", label: "请选择元素"}, {value: "入口", label: "入口"}, {value: "输入框", label: "输入框"}]);
  assert.equal(selectors[0].value, "入口");
  assert.equal(selectors[1].value, "");
});

test("element save waits for canonical response and failed save keeps draft open", async () => {
  let succeed = false;
  const responses = loadResponses();
  responses["PUT /api/browser/elements"] = (body) => succeed
    ? response(200, {elements: clone(body.elements)})
    : response(409, {error: "仍被策略引用"});
  const {ui} = harness({responses});
  ui.state.elements = {旧元素: "xpath=//old"};
  ui.openElementDialog({alias: "新元素", selector: "xpath=//new"});

  const failed = await ui.saveElements({旧元素: "xpath=//old", 新元素: "xpath=//new"});
  assert.equal(failed, false);
  assert.deepEqual(ui.state.elements, {旧元素: "xpath=//old"});
  assert.equal(ui.state.elementDialog.open, true);
  assert.equal(ui.state.elementDialog.error, "仍被策略引用");

  succeed = true;
  const saved = await ui.saveElements({旧元素: "xpath=//old", 新元素: "xpath=//new"});
  assert.equal(saved, true);
  assert.deepEqual(ui.state.elements, {旧元素: "xpath=//old", 新元素: "xpath=//new"});
  assert.equal(ui.state.elementDialog.open, false);
});

test("element draft supports scope and ordered locator candidates", () => {
  const {ui} = harness();
  ui.openElementDialog({
    alias: "评论入口",
    definition: {
      scope: "active_video",
      locators: [
        {id: "primary", type: "attribute", name: "data-e2e", value: "comment-icon", enabled: true},
        {id: "fallback", type: "xpath", value: "//button", enabled: true, fallback: true},
      ],
    },
    originalAlias: "评论入口",
  });

  assert.equal(ui.setElementScope("visible_comment_panel"), true);
  assert.equal(ui.updateElementLocator("primary", {value: "new-comment-icon"}), true);
  assert.equal(ui.addElementLocator({type: "css", value: ".comment"}).id, "locator_1");
  assert.equal(ui.removeElementLocator("missing"), false);
  assert.equal(ui.moveElementLocator("fallback", -1), true);
  assert.deepEqual(ui.state.elementDialog.draft.definition, {
    scope: "visible_comment_panel",
    locators: [
      {id: "fallback", type: "xpath", value: "//button", enabled: true, fallback: true},
      {id: "primary", type: "attribute", name: "data-e2e", value: "new-comment-icon", enabled: true},
      {id: "locator_1", type: "css", value: ".comment", enabled: true},
    ],
  });
});

test("element draft normalizes legacy XPath locally and keeps saved state unchanged until save", () => {
  const {ui} = harness();
  ui.state.elements = {oldEntry: "//button[@id='old']"};
  ui.openElementDialog({alias: "oldEntry", selector: "//button[@id='old']", originalAlias: "oldEntry"});

  assert.deepEqual(ui.state.elementDialog.draft.definition, {
    scope: "page",
    locators: [{id: "legacy_xpath", type: "xpath", value: "//button[@id='old']", enabled: true, fallback: true}],
  });
  assert.deepEqual(ui.state.elements, {oldEntry: "//button[@id='old']"});
});

test("template application mutates only the draft until explicit save", async () => {
  const template = {commentEntry: {scope: "active_video", locators: [{id: "template", type: "attribute", name: "data-e2e", value: "comment-icon", enabled: true}]}};
  const responses = loadResponses();
  responses["GET /api/browser/elements/templates/tiktok-comment"] = response(200, {elements: template});
  const {ui, requests} = harness({responses, confirm: () => true});
  ui.openElementDialog({alias: "commentEntry", definition: {scope: "page", locators: [{id: "old", type: "xpath", value: "//old", enabled: true}]}});

  assert.equal(await ui.applyTikTokCommentTemplate(), true);
  assert.equal(ui.state.elements.commentEntry, undefined);
  assert.equal(ui.state.elementDialog.draft.definition.scope, "active_video");
  assert.deepEqual(requests.at(-1), {url: "/api/browser/elements/templates/tiktok-comment", method: "GET", body: undefined});
});

test("template application requires explicit confirmation", async () => {
  const responses = loadResponses();
  responses["GET /api/browser/elements/templates/tiktok-comment"] = response(200, {elements: {commentEntry: {scope: "active_video", locators: []}}});
  const {ui, requests} = harness({responses, confirm: () => false});
  ui.openElementDialog({alias: "commentEntry"});

  assert.equal(await ui.applyTikTokCommentTemplate(), false);
  assert.equal(requests.length, 0);
});

test("element test result code falls back to a failed window code", () => {
  const {ui} = harness();

  assert.equal(
    ui.elementTestResultCode({alias: "commentEntry", status: "error"}, {
      status: "error",
      code: "element_inspection_failed",
    }),
    "element_inspection_failed",
  );
});

test("draft element test posts the draft only and retains read-only results", async () => {
  const responses = loadResponses();
  responses["POST /api/browser/elements/test"] = response(200, {results: [{profile_id: "p1", status: "ok", elements: [{alias: "commentEntry", status: "ok"}]}]});
  const {ui, requests} = harness({responses, windows: [{profile_id: "p1"}]});
  ui.openElementDialog({alias: "commentEntry", definition: {scope: "page", locators: [{id: "one", type: "css", value: ".comment", enabled: true}]}});

  assert.equal(await ui.testElementDraft(), true);
  assert.deepEqual(requests.at(-1), {
    url: "/api/browser/elements/test",
    method: "POST",
    body: {windows: [{profile_id: "p1"}], elements: {commentEntry: {scope: "page", locators: [{id: "one", type: "css", value: ".comment", enabled: true}]}}},
  });
  assert.deepEqual(ui.state.elements, {});
  assert.deepEqual(ui.state.elementDialog.testResults, [{profile_id: "p1", status: "ok", elements: [{alias: "commentEntry", status: "ok"}]}]);
});

test("successful element save synchronizes every live target selector", async () => {
  const first = {value: "保留", options: []};
  const second = {value: "已删除", options: []};
  const responses = loadResponses();
  responses["PUT /api/browser/elements"] = response(200, {elements: {保留: "x", 新增: "y"}});
  const {ui} = harness({responses, targetElementSelectors: () => [first, second]});
  ui.state.elements = {保留: "x", 已删除: "z"};

  assert.equal(await ui.saveElements({保留: "x", 新增: "y"}), true);
  assert.deepEqual(first.options.map((item) => item.value), ["", "保留", "新增"]);
  assert.equal(first.value, "保留");
  assert.equal(second.value, "");
});

test("canonical locator and strategy state round-trips through save and refresh", async () => {
  const server = {
    elements: {
      commentEntry: {
        scope: "active_video",
        locators: [
          {id: "entry-role", type: "role", role: "button", name: "Comments", name_mode: "exact", enabled: true},
          {id: "entry-xpath", type: "xpath", value: "//button[@data-e2e='comment-icon']", enabled: true, fallback: true},
        ],
      },
      commentInput: {
        scope: "visible_comment_panel",
        locators: [{id: "input-css", type: "css", value: "[contenteditable='true']", enabled: true}],
      },
      commentSubmit: {
        scope: "visible_comment_panel",
        locators: [{id: "submit-attribute", type: "attribute", name: "data-e2e", value: "comment-post", enabled: true}],
      },
    },
    strategies: [{
      id: "comment-flow",
      name: "Comment flow",
      run_mode: "once",
      batch_size: 2,
      actions: [
        {id: "open-comments", type: "click", params: {...clone(defaults.click), element: "commentEntry"}},
        {id: "switch-videos", type: "scroll_down", params: {...clone(defaults.scroll_down), total_count: [30, 50]}},
        {id: "write-comment", type: "keyboard_input", params: {...clone(defaults.keyboard_input), element: "commentInput"}},
        {id: "post-comment", type: "click", params: {...clone(defaults.click), element: "commentSubmit"}},
      ],
      status: "ready",
    }],
  };
  const responses = loadResponses();
  responses["GET /api/browser/elements"] = () => response(200, {elements: clone(server.elements)});
  responses["GET /api/browser/strategies"] = () => response(200, {strategies: clone(server.strategies)});
  responses["PUT /api/browser/elements"] = (body) => {
    server.elements = clone(body.elements);
    return response(200, {elements: clone(server.elements)});
  };
  responses["PUT /api/browser/strategies"] = (body) => {
    assert.deepEqual(
      body.strategies[0].actions.filter((action) => action.params.element).map((action) => action.params.element),
      ["commentEntry", "commentInput", "commentSubmit"],
    );
    assert.deepEqual(body.strategies[0].actions[1].params.total_count, [30, 50]);
    server.strategies = clone(body.strategies).map((strategy) => ({...strategy, status: "ready"}));
    return response(200, {strategies: clone(server.strategies)});
  };

  const first = harness({responses});
  await first.ui.init();
  first.ui.openElementDialog({
    alias: "commentEntry",
    originalAlias: "commentEntry",
    definition: first.ui.state.elements.commentEntry,
  });
  assert.equal(first.ui.setElementScope("visible_comment_panel"), true);
  assert.equal(first.ui.moveElementLocator("entry-xpath", -1), true);
  const editedElements = clone(first.ui.state.elements);
  editedElements.commentEntry = clone(first.ui.state.elementDialog.draft.definition);

  assert.equal(await first.ui.saveElements(editedElements), true);
  assert.deepEqual(first.ui.state.elements, server.elements);
  assert.equal(first.ui.openStrategy("comment-flow"), true);
  assert.equal(await first.ui.saveStrategy(), true);
  assert.deepEqual(first.ui.state.strategies, server.strategies);

  const refreshed = harness({responses});
  await refreshed.ui.init();
  assert.equal(refreshed.ui.state.elements.commentEntry.scope, "visible_comment_panel");
  assert.deepEqual(
    refreshed.ui.state.elements.commentEntry.locators.map((candidate) => candidate.id),
    ["entry-xpath", "entry-role"],
  );
  const restoredStrategy = refreshed.ui.state.strategies[0];
  assert.deepEqual(restoredStrategy.actions[1].params.total_count, [30, 50]);
  assert.deepEqual(
    restoredStrategy.actions.filter((action) => action.params.element).map((action) => action.params.element),
    ["commentEntry", "commentInput", "commentSubmit"],
  );
  assert.deepEqual(
    restoredStrategy.actions.map((action) => action.type),
    ["click", "scroll_down", "keyboard_input", "click"],
  );
});

test("element rename reloads server-rewritten strategy references", async () => {
  const responses = loadResponses();
  responses["PUT /api/browser/elements"] = response(200, {elements: {新名: "xpath=//new"}});
  responses["GET /api/browser/strategies"] = response(200, {strategies: [{id: "s", name: "S", run_mode: "once", batch_size: 1, actions: [{id: "a", type: "click", params: {...clone(defaults.click), element: "新名"}}], status: "ready"}]});
  const {ui} = harness({responses});
  ui.state.elements = {旧名: "xpath=//old"};

  assert.equal(await ui.saveElements({新名: "xpath=//new"}, "旧名"), true);
  assert.equal(ui.state.strategies[0].actions[0].params.element, "新名");
});

test("failed strategy reload after element rename blocks stale strategy saves", async () => {
  const responses = loadResponses();
  responses["PUT /api/browser/elements"] = response(200, {elements: {新名: "xpath=//new"}});
  responses["GET /api/browser/strategies"] = response(500, {error: "reload failed"});
  const {ui, requests} = harness({responses});
  ui.state.elements = {旧名: "xpath=//old"};
  ui.state.strategies = [{id: "s", name: "S", run_mode: "once", batch_size: 1, actions: []}];
  ui.state.draft = clone(ui.state.strategies[0]);

  assert.equal(await ui.saveElements({新名: "xpath=//new"}, "旧名"), true);
  assert.equal(ui.state.reloadRequired, true);
  assert.deepEqual(ui.state.strategies, []);
  assert.equal(await ui.saveStrategy(), false);
  assert.equal(requests.filter((item) => item.method === "PUT" && item.url === "/api/browser/strategies").length, 0);
});

test("new blocks use stable IDs and independent deep-copied server defaults", () => {
  const {ui} = harness();
  ui.state.catalog = clone(catalog);
  ui.state.defaults = clone(defaults);
  ui.createStrategy("新策略");

  const first = ui.addBlock("click");
  const second = ui.addBlock("click");
  first.params.trajectory.id = "changed";

  assert.notEqual(first.id, second.id);
  assert.equal(second.params.trajectory.id, "bezier");
  assert.equal(defaults.click.trajectory.id, "bezier");
  assert.throws(() => ui.addBlock("unknown"), /未知动作/);
});

test("block ordering, parameter editing, and deletion mutate only editor draft", () => {
  const {ui} = harness();
  ui.state.catalog = clone(catalog);
  ui.state.defaults = clone(defaults);
  ui.createStrategy("顺序");
  const move = ui.addBlock("move");
  const pause = ui.addBlock("pause");
  ui.updateBlock(move.id, {...move.params, duration_seconds: [2, 3]});
  ui.moveBlock(pause.id, -1);
  ui.deleteBlock(move.id);

  assert.deepEqual(ui.state.draft.actions.map((action) => action.id), [pause.id]);
  assert.equal(ui.state.dirty, true);
});

test("serializeStrategyForm includes loop parameters only for loop mode", () => {
  const {ui} = harness();
  ui.state.draft = {id: "s", name: "S", run_mode: "loop", batch_size: 4, loop_duration_minutes: [2, 5], actions: []};
  assert.deepEqual(ui.serializeStrategyForm(), clone(ui.state.draft));
  ui.state.draft.run_mode = "once";
  assert.deepEqual(ui.serializeStrategyForm(), {id: "s", name: "S", run_mode: "once", batch_size: 4, actions: []});
});

test("move parameter form treats viewport deltas as horizontal and vertical coordinates", () => {
  const {ui} = harness();

  assert.deepEqual(ui.parameterFields("move").filter((field) => field.name.startsWith("delta_viewport")), [
    {name: "delta_viewport.0", label: "水平位移比例"},
    {name: "delta_viewport.1", label: "垂直位移比例"},
  ]);
  assert.deepEqual(ui.parameterFields("pause").map((field) => field.name), ["duration_seconds.0", "duration_seconds.1"]);
});

test("scroll editor exposes exactly four wheel-count fields and hides distance and legacy burst count", () => {
  const {ui} = harness();
  const expected = [
    {name: "total_count.0", label: "最少切换视频数"},
    {name: "total_count.1", label: "最多切换视频数"},
    {name: "interval_seconds.0", label: "最小切换间隔秒数"},
    {name: "interval_seconds.1", label: "最大切换间隔秒数"},
  ];

  assert.deepEqual(ui.parameterFields("scroll_up"), expected);
  assert.deepEqual(ui.parameterFields("scroll_down"), expected);
  assert.equal(ui.parameterFields("scroll_up").some((field) => field.name === "distance"), false);
  assert.equal(ui.parameterFields("scroll_up").some((field) => field.name.startsWith("burst_count")), false);
  assert.equal(ui.parameterFields("scroll_up").some((field) => field.label.includes("每组次数")), false);
});

function scrollForm(values) {
  return {
    elements: {
      namedItem(name) {
        return Object.hasOwn(values, name) ? {value: String(values[name])} : null;
      },
    },
  };
}

test("scroll parse preserves old hidden burst range and defaults new actions to one", () => {
  const {ui} = harness();
  ui.state.catalog = clone(catalog);
  ui.state.defaults = clone(defaults);
  ui.createStrategy("滚动");
  assert.deepEqual(ui.addBlock("scroll_down").params.burst_count, [1, 1]);
  const form = scrollForm({
    "total_count.0": 3,
    "total_count.1": 7,
    "interval_seconds.0": 0.2,
    "interval_seconds.1": 0.6,
  });

  assert.deepEqual(ui.parseScrollParameters(form, {
    type: "scroll_down",
    params: {distance: 600, total_count: [1, 1], burst_count: [2, 5], interval_seconds: [0.1, 0.3]},
  }), {
    distance: 120,
    total_count: [3, 7],
    burst_count: [2, 5],
    interval_seconds: [0.2, 0.6],
  });
  assert.deepEqual(ui.parseScrollParameters(form, {type: "scroll_up", params: {}}).burst_count, [1, 1]);
});

test("scroll parse validates synthetic browser wheel-event count integers and range order", () => {
  const {ui} = harness();
  const values = {
    "total_count.0": 2,
    "total_count.1": 4,
    "interval_seconds.0": 0.1,
    "interval_seconds.1": 0.3,
  };
  const parse = (changes) => ui.parseScrollParameters(
    scrollForm({...values, ...changes}),
    {type: "scroll_down", params: {burst_count: [1, 1]}},
  );

  assert.throws(() => parse({"total_count.0": 0}), /切换视频数必须是正整数/);
  assert.throws(() => parse({"total_count.1": 2.5}), /切换视频数必须是正整数/);
  assert.throws(() => parse({"total_count.0": 5, "total_count.1": 4}), /最少切换视频数不能大于最多切换视频数/);
  assert.throws(() => parse({"interval_seconds.0": ""}), /最小切换间隔秒数必须是数字/);
  assert.throws(() => parse({"interval_seconds.0": 0.5, "interval_seconds.1": 0.3}), /最小切换间隔秒数不能大于最大切换间隔秒数/);
});

test("action parameter sanitation clears fields irrelevant to the selected mode", () => {
  const {ui} = harness();
  const fixed = clone(defaults.keyboard_input);
  fixed.content = {source: "fixed", text: "hello", brand_id: "old-brand"};
  const generated = clone(defaults.keyboard_input);
  generated.content = {source: "generated_comment", text: "old fixed text", brand_id: "brand-1"};
  const viewport = clone(defaults.move);
  viewport.target_mode = "viewport";
  viewport.element = "旧元素";

  assert.deepEqual(ui.sanitizeActionParams("keyboard_input", fixed).content, {source: "fixed", text: "hello", brand_id: ""});
  assert.deepEqual(ui.sanitizeActionParams("keyboard_input", generated).content, {source: "generated_comment", text: "", brand_id: "brand-1"});
  assert.equal(ui.sanitizeActionParams("move", viewport).element, "");
});

test("strategy explicit save clears dirty only after canonical PUT succeeds", async () => {
  let succeed = false;
  const responses = loadResponses();
  responses["PUT /api/browser/strategies"] = (body) => succeed
    ? response(200, {strategies: clone(body.strategies)})
    : response(400, {error: "元素引用无效"});
  const {ui, listeners} = harness({responses});
  ui.createStrategy("待保存");
  ui.markDirty();
  assert.equal(listeners.has("beforeunload"), true);

  assert.equal(await ui.saveStrategy(), false);
  assert.equal(ui.state.dirty, true);
  assert.equal(ui.state.saveMessage, "元素引用无效");
  assert.equal(listeners.has("beforeunload"), true);

  succeed = true;
  assert.equal(await ui.saveStrategy(), true);
  assert.equal(ui.state.dirty, false);
  assert.equal(ui.state.saveMessage, "已保存");
  assert.equal(listeners.has("beforeunload"), false);
});

test("strategy list opens an isolated editor; rename and delete persist through canonical list", async () => {
  const saved = [{id: "s", name: "原名", run_mode: "once", batch_size: 1, actions: [], status: "ready"}];
  const responses = loadResponses({strategies: saved});
  responses["PUT /api/browser/strategies"] = (body) => response(200, {strategies: clone(body.strategies)});
  const {ui} = harness({responses});
  await ui.init();
  ui.openStrategy("s");
  ui.renameStrategy("新名");
  assert.equal(ui.state.strategies[0].name, "原名");
  assert.equal(ui.state.draft.name, "新名");
  await ui.saveStrategy();
  assert.equal(ui.state.strategies[0].name, "新名");
  assert.equal(await ui.deleteStrategy("s"), true);
  assert.deepEqual(ui.state.strategies, []);
  assert.equal(ui.state.view, "list");
});

test("pattern save/delete keeps server conflicts visible and never mutates on failure", async () => {
  let conflict = false;
  const original = [{id: "p1", name: "轨迹", type: "mouse", data: {points: [{x_ratio: 0, y_ratio: 0, dt_ms: 0}, {x_ratio: 1, y_ratio: 1, dt_ms: 1}], sample_count: 2, total_duration_ms: 1}}];
  const responses = loadResponses({patterns: original});
  responses["PUT /api/browser/patterns"] = (body) => conflict
    ? response(409, {error: "仍被策略引用"})
    : response(200, {patterns: clone(body.patterns)});
  const {ui} = harness({responses});
  ui.state.patterns = clone(original);
  conflict = true;
  assert.equal(await ui.deletePattern("p1"), false);
  assert.equal(ui.state.patterns.length, 1);
  assert.equal(ui.state.patternError, "仍被策略引用");
  conflict = false;
  assert.equal(await ui.deletePattern("p1"), true);
  assert.deepEqual(ui.state.patterns, []);
});

test("recording requires exactly one window", async () => {
  const {ui} = harness({responses: loadResponses(), windows: []});
  assert.equal(await ui.startRecording("keyboard"), false);
  assert.match(ui.state.recording.error, /只能选择 1 个/);
});

test("recording start transports only the selected window and behavior type", async () => {
  const responses = loadResponses();
  responses["POST /api/browser/pattern-recordings/start"] = (body) => {
    assert.deepEqual(body, {windows: [{profile_id: "w1", profile_no: "1", name: "窗口"}], type: "keyboard"});
    return response(200, {recording_id: "r1", status: "ready", type: body.type});
  };
  const {ui} = harness({responses, windows: [{profile_id: "w1", profile_no: "1", name: "窗口"}]});
  assert.equal(await ui.startRecording("keyboard"), true);
});

test("concurrent recording starts share one request", async () => {
  let finishStart;
  const pendingStart = new Promise((resolve) => { finishStart = resolve; });
  const responses = loadResponses();
  responses["POST /api/browser/pattern-recordings/start"] = () => pendingStart;
  const {ui, requests} = harness({responses, windows: [{profile_id: "w1"}]});

  const first = ui.startRecording("mouse");
  const second = ui.startRecording("keyboard");
  assert.equal(requests.filter((item) => item.url.endsWith("/pattern-recordings/start")).length, 1);

  finishStart(response(200, {recording_id: "r1", status: "ready", type: "mouse"}));
  assert.equal(await first, true);
  assert.equal(await second, true);
  assert.equal(ui.state.recording.recording_id, "r1");
  assert.equal(ui.state.recording.type, "mouse");
});

test("pending start cancel waits for start and its compensation stop", async () => {
  let finishStart;
  let finishStop;
  const responses = loadResponses();
  responses["POST /api/browser/pattern-recordings/start"] = () => new Promise((resolve) => { finishStart = resolve; });
  responses["POST /api/browser/pattern-recordings/r1/stop"] = () => new Promise((resolve) => { finishStop = resolve; });
  const {ui, requests} = harness({responses, windows: [{profile_id: "w1"}]});

  const started = ui.startRecording("mouse");
  let cancelSettled = false;
  const cancelled = ui.cancelRecording().then((value) => { cancelSettled = true; return value; });
  await Promise.resolve();
  assert.equal(cancelSettled, false);
  assert.equal(ui.state.recording.status, "starting");

  finishStart(response(200, {recording_id: "r1", status: "ready", type: "mouse"}));
  assert.equal(await started, true);
  await Promise.resolve();
  assert.equal(cancelSettled, false);
  assert.equal(ui.state.recording.recording_id, "r1");
  assert.equal(requests.filter((item) => item.url.endsWith("/r1/stop")).length, 1);

  finishStop(response(200, {recording_id: "r1", status: "finished", type: "mouse", sample: {}}));
  assert.equal(await cancelled, true);
  assert.equal(ui.state.recording, null);
});

test("late start compensation stop failure retains its recording ID for retry", async () => {
  let finishStart;
  let stopAttempts = 0;
  const responses = loadResponses();
  responses["POST /api/browser/pattern-recordings/start"] = () => new Promise((resolve) => { finishStart = resolve; });
  responses["POST /api/browser/pattern-recordings/r1/stop"] = () => {
    stopAttempts += 1;
    return stopAttempts === 1
      ? response(503, {error: "补偿停止失败"})
      : response(200, {recording_id: "r1", status: "finished", type: "mouse", sample: {}});
  };
  const {ui} = harness({responses, windows: [{profile_id: "w1"}]});

  ui.startRecording("mouse");
  const firstCancel = ui.cancelRecording();
  finishStart(response(200, {recording_id: "r1", status: "ready", type: "mouse"}));

  assert.equal(await firstCancel, false);
  assert.equal(ui.state.recording.recording_id, "r1");
  assert.equal(ui.state.recording.error, "补偿停止失败");
  assert.equal(await ui.cancelRecording(), true);
  assert.equal(ui.state.recording, null);
  assert.equal(stopAttempts, 2);
});

test("manual stop cancels its old poll callback and completed sample cannot be polluted", async () => {
  const scheduled = [];
  const cleared = [];
  const responses = loadResponses();
  responses["POST /api/browser/pattern-recordings/start"] = response(200, {recording_id: "r1", status: "ready", type: "mouse"});
  responses["POST /api/browser/pattern-recordings/r1/stop"] = response(200, {recording_id: "r1", type: "mouse", status: "finished", sample: {points: [{x_ratio: 0, y_ratio: 0, dt_ms: 0}, {x_ratio: 1, y_ratio: 1, dt_ms: 10}], sample_count: 2, total_duration_ms: 10}});
  responses["GET /api/browser/pattern-recordings/r1"] = response(409, {error: "context missing"});
  const {ui, requests} = harness({
    responses,
    windows: [{profile_id: "w1"}],
    setTimeout: (fn, delay) => { const id = scheduled.length + 1; scheduled.push({id, fn, delay}); return id; },
    clearTimeout: (id) => cleared.push(id),
  });

  await ui.startRecording("mouse");
  const oldPoll = scheduled[0];
  await ui.stopRecording();
  const completed = clone(ui.state.recording.sample);
  assert.deepEqual(cleared, [oldPoll.id]);
  await oldPoll.fn();
  assert.deepEqual(ui.state.recording.sample, completed);
  assert.equal(requests.filter((item) => item.method === "GET" && item.url.includes("pattern-recordings")).length, 0);
});

test("a late manual stop response cannot mutate a newer recording", async () => {
  let finishStop;
  const responses = loadResponses();
  responses["POST /api/browser/pattern-recordings/start"] = response(200, {recording_id: "r1", status: "ready", type: "mouse"});
  responses["POST /api/browser/pattern-recordings/r1/stop"] = () => new Promise((resolve) => { finishStop = resolve; });
  const {ui} = harness({responses, windows: [{profile_id: "w1"}]});

  await ui.startRecording("mouse");
  const oldStop = ui.stopRecording();
  ui.state.recording = {recording_id: "r2", status: "ready", type: "keyboard", sample: null, error: ""};

  finishStop(response(200, {recording_id: "r1", status: "finished", type: "mouse", sample: {sample_count: 2}}));
  assert.equal(await oldStop, true);
  assert.equal(ui.state.recording.recording_id, "r2");
  assert.equal(ui.state.recording.sample, null);
});

test("manual stop and cancel share one finalize request and cancel wins", async () => {
  let finishStop;
  let stopRequests = 0;
  const pendingStop = new Promise((resolve) => { finishStop = resolve; });
  const responses = loadResponses();
  responses["POST /api/browser/pattern-recordings/start"] = response(200, {recording_id: "r1", status: "ready", type: "mouse"});
  responses["POST /api/browser/pattern-recordings/r1/stop"] = () => {
    stopRequests += 1;
    return pendingStop;
  };
  const {ui} = harness({responses, windows: [{profile_id: "w1"}]});

  await ui.startRecording("mouse");
  const stopped = ui.stopRecording();
  const cancelled = ui.cancelRecording();
  assert.equal(stopRequests, 1);

  finishStop(response(200, {recording_id: "r1", status: "finished", type: "mouse", sample: {sample_count: 2}}));
  assert.equal(await stopped, true);
  assert.equal(await cancelled, true);
  assert.equal(ui.state.recording, null);
});

test("recording polls at 500ms, finishes stopped sample, names and persists it", async () => {
  const scheduled = [];
  const sample = {recording_id: "r1", type: "keyboard", status: "finished", sample: {intervals_ms: [10, 20], hold_ms: [2, 3], sample_count: 2, total_duration_ms: 30}};
  const responses = loadResponses();
  responses["POST /api/browser/pattern-recordings/start"] = response(200, {recording_id: "r1", status: "ready", type: "keyboard"});
  responses["GET /api/browser/pattern-recordings/r1"] = response(200, {recording_id: "r1", status: "stopped", sample_count: 2, total_duration_ms: 30});
  responses["POST /api/browser/pattern-recordings/r1/stop"] = response(200, sample);
  responses["PUT /api/browser/patterns"] = (body) => response(200, {patterns: clone(body.patterns)});
  const {ui} = harness({
    responses,
    windows: [{profile_id: "w1", profile_no: "1", name: "窗口"}],
    setTimeout: (fn, delay) => { scheduled.push({fn, delay}); return scheduled.length; },
  });

  assert.equal(await ui.startRecording("keyboard"), true);
  assert.equal(scheduled[0].delay, 500);
  await scheduled.shift().fn();
  assert.equal(ui.state.recording.sample.data.sample_count, 2);
  assert.equal(await ui.saveRecording("我的节奏"), true);
  assert.equal(ui.state.patterns[0].name, "我的节奏");
  assert.equal("text" in ui.state.patterns[0].data, false);
  assert.equal(ui.state.recording, null);
});

test("recording errors and cancel create no pattern", async () => {
  const responses = loadResponses();
  responses["POST /api/browser/pattern-recordings/start"] = response(400, {error: "CDP 断开"});
  const {ui} = harness({responses, windows: [{profile_id: "w"}]});
  assert.equal(await ui.startRecording("mouse"), false);
  assert.equal(ui.state.patterns.length, 0);
  assert.equal(ui.state.recording.error, "CDP 断开");
  await ui.cancelRecording();
  assert.equal(ui.state.recording, null);
  assert.equal(ui.state.patterns.length, 0);
});

test("cancel keeps the active recording reference until stop succeeds and allows retry", async () => {
  let finishFirstCancel;
  let cancelAttempts = 0;
  const responses = loadResponses();
  responses["POST /api/browser/pattern-recordings/start"] = response(200, {recording_id: "r1", status: "ready", type: "mouse"});
  responses["POST /api/browser/pattern-recordings/r1/stop"] = () => {
    cancelAttempts += 1;
    if (cancelAttempts === 1) return new Promise((resolve) => { finishFirstCancel = resolve; });
    return response(200, {recording_id: "r1", status: "finished", type: "mouse", sample: {}});
  };
  const {ui} = harness({responses, windows: [{profile_id: "w1"}]});

  await ui.startRecording("mouse");
  const firstCancel = ui.cancelRecording();
  assert.equal(ui.state.recording.recording_id, "r1");

  finishFirstCancel(response(503, {error: "停止录制失败"}));
  assert.equal(await firstCancel, false);
  assert.equal(ui.state.recording.recording_id, "r1");
  assert.equal(ui.state.recording.error, "停止录制失败");

  assert.equal(await ui.cancelRecording(), true);
  assert.equal(ui.state.recording, null);
  assert.equal(cancelAttempts, 2);
});

test("start cannot replace the active recording while cancel is pending", async () => {
  let finishCancel;
  let startCount = 0;
  const responses = loadResponses();
  responses["POST /api/browser/pattern-recordings/start"] = () => {
    startCount += 1;
    return response(200, {recording_id: `r${startCount}`, status: "ready", type: "mouse"});
  };
  responses["POST /api/browser/pattern-recordings/r1/stop"] = () => new Promise((resolve) => { finishCancel = resolve; });
  const {ui} = harness({responses, windows: [{profile_id: "w1"}]});

  await ui.startRecording("mouse");
  const oldCancel = ui.cancelRecording();
  assert.equal(await ui.startRecording("mouse"), false);
  assert.equal(ui.state.recording.recording_id, "r1");
  assert.equal(startCount, 1);

  finishCancel(response(200, {recording_id: "r1", status: "finished", type: "mouse", sample: {}}));
  assert.equal(await oldCancel, true);
  assert.equal(ui.state.recording, null);
});

test("an in-flight poll is ignored after cancel clears its recording", async () => {
  let finishPoll;
  const scheduled = [];
  const responses = loadResponses();
  responses["POST /api/browser/pattern-recordings/start"] = response(200, {recording_id: "r1", status: "ready", type: "mouse"});
  responses["GET /api/browser/pattern-recordings/r1"] = () => new Promise((resolve) => { finishPoll = resolve; });
  responses["POST /api/browser/pattern-recordings/r1/stop"] = response(200, {recording_id: "r1", status: "finished", type: "mouse", sample: {}});
  const {ui} = harness({
    responses,
    windows: [{profile_id: "w1"}],
    setTimeout: (fn, delay) => { scheduled.push({fn, delay}); return scheduled.length; },
  });

  await ui.startRecording("mouse");
  const oldPoll = scheduled[0].fn();
  assert.equal(await ui.cancelRecording(), true);
  assert.equal(ui.state.recording, null);

  finishPoll(response(200, {recording_id: "r1", status: "recording", type: "mouse"}));
  assert.equal(await oldPoll, false);
  assert.equal(ui.state.recording, null);
  assert.equal(scheduled.length, 1);
});

test("execution selector is derived only from canonical strategies", () => {
  const {ui} = harness();
  ui.state.strategies = [
    {id: "ready", name: "可执行", status: "ready"},
    {id: "repair", name: "待修复", status: "needs_repair"},
  ];
  const select = {value: "ready", options: []};
  ui.syncExecutionOptions(select);
  assert.deepEqual(select.options.map((item) => item.value), ["", "ready", "repair"]);
  assert.equal(select.options[2].disabled, true);
});
