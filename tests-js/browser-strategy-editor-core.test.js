const assert = require("node:assert/strict");
const test = require("node:test");

const core = require("../gateway/static/browser_strategy_editor_core");

const {
  ACTIONS,
  StrategyRequestError,
  actionTemplate,
  normalizeStrategyDraft,
  createStrategyDraft,
  duplicateStrategyDraft,
  addAction,
  moveAction,
  removeAction,
  eligibleElements,
  serializeAction,
  serializeDefinition,
  buildCreatePayload,
  buildUpdatePayload,
  createStrategyRepository,
} = core;

function flatStrategy(overrides = {}) {
  return {
    id: "strategy-1",
    name: "Feed strategy",
    enabled: true,
    revision: 3,
    target_url: "https://www.tiktok.com/",
    ready_element_id: "ready-1",
    readiness_timeout_seconds: 15,
    run_mode: "once",
    loop_duration_minutes: null,
    actions: [actionTemplate("wait", "wait-1")],
    ...overrides,
  };
}

test("exports the five V2 action types and the shared core API", () => {
  assert.deepEqual(Object.keys(ACTIONS), ["move", "scroll", "click", "input", "wait"]);
  for (const name of [
    "StrategyRequestError", "actionTemplate", "normalizeStrategyDraft", "createStrategyDraft",
    "duplicateStrategyDraft", "addAction", "moveAction", "removeAction", "eligibleElements",
    "serializeAction", "serializeDefinition", "buildCreatePayload", "buildUpdatePayload",
    "createStrategyRepository",
  ]) assert.equal(typeof core[name], name === "StrategyRequestError" ? "function" : "function");
});

test("flat V2 records become nested editable drafts without mutating the record", () => {
  const record = flatStrategy();
  const draft = normalizeStrategyDraft(record);

  assert.equal(draft.definition.target_url, "https://www.tiktok.com/");
  assert.deepEqual(draft.definition.actions.map((item) => item.id), ["wait-1"]);
  assert.equal(Object.hasOwn(draft, "actions"), false);
  draft.definition.actions[0].duration_seconds[0] = 99;
  assert.deepEqual(record.actions[0].duration_seconds, [1, 1]);
});

test("nested records remain nested and missing actions become an empty list", () => {
  const draft = normalizeStrategyDraft({
    id: "nested", definition: {target_url: "https://example.com/", actions: undefined},
  });
  assert.deepEqual(draft.definition.actions, []);
  assert.equal(draft.definition.target_url, "https://example.com/");
});

test("new and duplicate drafts use editable defaults and fresh IDs", () => {
  const fresh = createStrategyDraft("new-1");
  assert.equal(fresh.id, "new-1");
  assert.equal(fresh.localNew, true);
  assert.deepEqual(fresh.definition, {
    target_url: "https://www.tiktok.com/",
    ready_element_id: "",
    readiness_timeout_seconds: 15,
    run_mode: "once",
    loop_duration_minutes: null,
    actions: [],
  });

  const copy = duplicateStrategyDraft(normalizeStrategyDraft(flatStrategy()), "copy-1");
  assert.equal(copy.id, "copy-1");
  assert.equal(copy.name, "Feed strategy 副本");
  assert.equal(copy.localNew, true);
  assert.equal(copy.revision, undefined);
  copy.definition.actions[0].duration_seconds[0] = 20;
  assert.deepEqual(fresh.definition.actions, []);
});

test("action templates preserve the existing V2 schemas", () => {
  assert.deepEqual(actionTemplate("move", "move-1"), {
    id: "move-1", type: "move", element_id: "", duration_seconds: [0.2, 0.5],
  });
  assert.deepEqual(actionTemplate("scroll", "scroll-1"), {
    id: "scroll-1", type: "scroll", direction: "down", distance_pixels: [120, 120],
    count: [1, 2], interval_seconds: [0.2, 0.5],
  });
  assert.deepEqual(actionTemplate("click", "click-1"), {
    id: "click-1", type: "click", element_id: "", button: "left", click_count: 1,
    hold_seconds: [0.05, 0.1], after_seconds: [0.3, 0.6],
  });
  assert.deepEqual(actionTemplate("input", "input-1"), {
    id: "input-1", type: "input", element_id: "", content_source: "fixed", fixed_text: "",
    content_library_id: "", interval_ms: [40, 120],
  });
  assert.deepEqual(actionTemplate("wait", "wait-1"), {
    id: "wait-1", type: "wait", duration_seconds: [1, 1],
  });
});

test("action editing adds unique IDs, reorders, and removes actions", () => {
  const draft = createStrategyDraft("strategy-1");
  draft.definition.actions.push(actionTemplate("wait", "action_1"), actionTemplate("wait", "action_2"));

  assert.equal(addAction(draft, "scroll"), true);
  assert.equal(addAction(draft, "wait"), true);
  assert.deepEqual(draft.definition.actions.map((item) => item.id), ["action_1", "action_2", "action_3", "action_4"]);
  assert.equal(moveAction(draft, 3, -1), true);
  assert.deepEqual(draft.definition.actions.map((item) => item.type), ["wait", "wait", "wait", "scroll"]);
  assert.equal(removeAction(draft, 1), true);
  assert.deepEqual(draft.definition.actions.map((item) => item.type), ["wait", "wait", "scroll"]);
  assert.equal(moveAction(draft, 0, -1), false);
  assert.equal(removeAction(draft, 99), false);
});

test("eligible elements keeps active elements and applies purpose and kind filters", () => {
  const elements = [
    {id: "ready-1", purpose: "readiness", kind: "generic", status: "active"},
    {id: "ready-2", purpose: "readiness", kind: "generic", status: "retired"},
    {id: "click-1", purpose: "action", kind: "click", status: "active"},
    {id: "input-1", purpose: "action", kind: "input", status: "active"},
    {id: "generic-1", purpose: "action", kind: "generic", status: "active"},
  ];
  assert.deepEqual(eligibleElements(elements, "readiness").map((item) => item.id), ["ready-1"]);
  assert.deepEqual(eligibleElements(elements, "action", ["click", "generic"]).map((item) => item.id), ["click-1", "generic-1"]);
  assert.deepEqual(eligibleElements("action", ["input"], elements).map((item) => item.id), ["input-1"]);
});

test("all five action types serialize with the closed V2 schema and range parsing", () => {
  assert.deepEqual(serializeAction({...actionTemplate("move", "m"), element_id: "move-el", duration_seconds: "0.2-0.5"}), {
    id: "m", type: "move", element_id: "move-el", duration_seconds: [0.2, 0.5],
  });
  assert.deepEqual(serializeAction({...actionTemplate("scroll", "s"), count: "1-3", interval_seconds: "0.2-0.5"}), {
    id: "s", type: "scroll", direction: "down", distance_pixels: [120, 120], count: [1, 3], interval_seconds: [0.2, 0.5],
  });
  assert.deepEqual(serializeAction({...actionTemplate("click", "c"), element_id: "click-el", click_count: "2"}), {
    id: "c", type: "click", element_id: "click-el", button: "left", click_count: 2,
    hold_seconds: [0.05, 0.1], after_seconds: [0.3, 0.6],
  });
  assert.deepEqual(serializeAction({...actionTemplate("input", "i"), element_id: "input-el", fixed_text: "hello", interval_ms: "40-120"}), {
    id: "i", type: "input", element_id: "input-el", content_source: "fixed", fixed_text: "hello",
    content_library_id: "", interval_ms: [40, 120],
  });
  assert.deepEqual(serializeAction({...actionTemplate("input", "l"), content_source: "library", content_library_id: "library-1", fixed_text: ""}), {
    id: "l", type: "input", element_id: "", content_source: "library", fixed_text: "", content_library_id: "library-1", interval_ms: [40, 120],
  });
  assert.deepEqual(serializeAction({...actionTemplate("wait", "w"), duration_seconds: "1-2"}), {
    id: "w", type: "wait", duration_seconds: [1, 2],
  });
  assert.throws(() => serializeAction({...actionTemplate("scroll", "bad"), count: "2-1"}), /视频切换次数范围格式/);
  assert.throws(() => serializeAction({...actionTemplate("scroll", "bad"), count: "1.5-2"}), /视频切换次数范围格式/);
});

test("serializeDefinition validates static fields and serializes a duration range", () => {
  const definition = serializeDefinition({
    target_url: "https://www.tiktok.com/foryou",
    ready_element_id: "ready-1",
    readiness_timeout_seconds: "30",
    run_mode: "duration",
    loop_duration_minutes: "2-5",
    actions: [actionTemplate("wait", "wait-1")],
  });
  assert.deepEqual(definition.loop_duration_minutes, [2, 5]);
  assert.equal(definition.readiness_timeout_seconds, 30);
  assert.deepEqual(definition.actions, [actionTemplate("wait", "wait-1")]);
  assert.throws(() => serializeDefinition({...definition, target_url: "http://example.com/"}), /HTTPS/);
  assert.throws(() => serializeDefinition({...definition, ready_element_id: ""}), /HTTPS 目标网址/);
  assert.throws(() => serializeDefinition({...definition, readiness_timeout_seconds: 0}), /HTTPS 目标网址/);
  assert.equal(serializeDefinition({...definition, run_mode: "once", loop_duration_minutes: "2-5"}).loop_duration_minutes, null);
});

test("create and update payloads are complete and detached from the draft", () => {
  const draft = normalizeStrategyDraft(flatStrategy({
    enabled: false,
    revision: 4,
    readiness_timeout_seconds: "30",
    run_mode: "duration",
    loop_duration_minutes: "2-5",
    actions: [{
      ...actionTemplate("click", "click-1"),
      element_id: "click-el",
      click_count: "2",
      hold_seconds: "0.05-0.1",
      after_seconds: "0.3-0.6",
    }],
  }));
  const create = buildCreatePayload(draft);
  assert.deepEqual(Object.keys(create).sort(), ["definition", "enabled", "id", "name"]);
  const update = buildUpdatePayload(draft);
  assert.deepEqual(Object.keys(update).sort(), ["definition", "enabled", "expected_revision", "name"]);
  assert.equal(update.expected_revision, 4);
  const expectedDefinition = {
    target_url: "https://www.tiktok.com/",
    ready_element_id: "ready-1",
    readiness_timeout_seconds: 30,
    run_mode: "duration",
    loop_duration_minutes: [2, 5],
    actions: [{
      id: "click-1", type: "click", element_id: "click-el", button: "left", click_count: 2,
      hold_seconds: [0.05, 0.1], after_seconds: [0.3, 0.6],
    }],
  };
  assert.deepEqual(create.definition, expectedDefinition);
  assert.deepEqual(update.definition, expectedDefinition);
  create.definition.actions[0].hold_seconds[0] = 99;
  update.definition.actions[0].after_seconds[0] = 99;
  assert.equal(draft.definition.readiness_timeout_seconds, "30");
  assert.equal(draft.definition.loop_duration_minutes, "2-5");
  assert.equal(draft.definition.actions[0].click_count, "2");
  assert.equal(draft.definition.actions[0].hold_seconds, "0.05-0.1");
  assert.equal(draft.definition.actions[0].after_seconds, "0.3-0.6");
});

test("repository uses existing V2 endpoints and unwraps data envelopes", async () => {
  const calls = [];
  const requestJson = async (url, method, body) => {
    calls.push({url, method, body});
    return {status: method === "POST" ? 201 : 200, data: {data: {id: "server-1"}}};
  };
  const repository = createStrategyRepository(requestJson);
  const draft = normalizeStrategyDraft(flatStrategy({id: "strategy 1"}));
  assert.deepEqual(await repository.loadDependencies(), [{id: "server-1"}, {id: "server-1"}]);
  assert.deepEqual(await repository.load("strategy 1"), {id: "server-1"});
  assert.deepEqual(await repository.create(draft), {id: "server-1"});
  assert.deepEqual(await repository.update(draft), {id: "server-1"});
  assert.deepEqual(await repository.remove(draft), {id: "server-1"});
  assert.deepEqual(calls.map(({url, method}) => `${method} ${url}`), [
    "GET /api/browser-v2/elements", "GET /api/browser-v2/content-libraries",
    "GET /api/browser-v2/strategies/strategy%201", "POST /api/browser-v2/strategies",
    "PUT /api/browser-v2/strategies/strategy%201", "DELETE /api/browser-v2/strategies/strategy%201",
  ]);
  assert.deepEqual(calls[4].body, buildUpdatePayload(draft));
});

test("repository maps HTTP and network failures to stable request errors", async () => {
  const statuses = [
    [409, "revision_conflict"], [404, "not_found"], [422, "validation_failed"], [500, "request_failed"],
  ];
  for (const [status, code] of statuses) {
    const repository = createStrategyRepository(async () => ({status, data: {error: {message: "failure"}}}));
    await assert.rejects(() => repository.load("strategy-1"), (error) => {
      assert.equal(error instanceof StrategyRequestError, true);
      assert.equal(error.code, code);
      assert.equal(error.status, status);
      return true;
    });
  }
  const repository = createStrategyRepository(async () => { throw new Error("offline"); });
  await assert.rejects(() => repository.load("strategy-1"), (error) => {
    assert.equal(error instanceof StrategyRequestError, true);
    assert.equal(error.code, "network_failed");
    assert.equal(error.status, 0);
    assert.match(error.message, /offline/);
    return true;
  });
});
