const assert = require("node:assert/strict");
const test = require("node:test");

const {
  sanitizeInventory,
  filterInventory,
  serializeNamedSelections,
  elementStatusText,
  businessRunSteps,
} = require("../gateway/static/selector_inventory_ui");

test("inventory keeps Role and Name as display metadata only", () => {
  const result = sanitizeInventory([{
    selection_id: "selection-1",
    tag: "button",
    role: "button",
    name: "Comments",
    locators: [{type: "css", value: "[data-e2e=\"comment-icon\"]", match_count: 1}],
    locatable: true,
  }]);
  assert.equal(result[0].role, "button");
  assert.equal(result[0].name, "Comments");
  assert.deepEqual(result[0].locators[0], {
    type: "css", value: "[data-e2e=\"comment-icon\"]", match_count: 1,
  });
  assert.equal(Object.hasOwn(result[0], "semantic_score"), false);
});

test("inventory filters by interaction type, location, locatability and text", () => {
  const items = [{
    selection_id: "button-1", tag: "button", name: "Comments",
    page_region: "right", locatable: true,
    locators: [{type: "css", value: "#comments", match_count: 1}],
  }, {
    selection_id: "input-1", tag: "input", name: "Search",
    page_region: "top", locatable: false, locators: [],
  }];
  assert.deepEqual(filterInventory(items, {
    type: "button", region: "right", locatable: "yes", search: "comments",
  }).map((item) => item.selection_id), ["button-1"]);
});

test("named selection payload uses IDs and custom names only", () => {
  assert.deepEqual(serializeNamedSelections([
    {selectionId: "selection-1", displayName: " 评论入口 "},
  ]), [{selection_id: "selection-1", display_name: "评论入口"}]);
  assert.throws(
    () => serializeNamedSelections([{selectionId: "selection-1", displayName: ""}]),
    /invalid_named_selection/,
  );
});

test("run view always exposes five understandable steps", () => {
  assert.deepEqual(businessRunSteps({stages: []}).map((item) => item.id), [
    "prepare_environment", "open_and_replay", "validate_elements",
    "protect_or_recover", "alert_and_cleanup",
  ]);
  assert.equal(businessRunSteps({stages: [{
    id: "validate_elements", status: "failed", message: "一个元素失效",
  }]}).at(2).detail, "一个元素失效");
});

test("managed status text is understandable", () => {
  assert.equal(elementStatusText("pending_rebind"), "待重新绑定");
  assert.equal(elementStatusText("invalid"), "失效");
});
