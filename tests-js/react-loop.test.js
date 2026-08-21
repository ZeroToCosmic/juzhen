const assert = require("node:assert/strict");
const test = require("node:test");

const { runReActLoop } = require("../browser/react-loop");

test("runReActLoop captures, asks the brain, executes actions, and stops on success", async () => {
  const calls = [];
  const decisions = [
    { action: "click", x: 10, y: 20, reason: "open" },
    { action: "type", text: "hello", reason: "fill" },
    { action: "scroll", reason: "next" },
    { action: "success", reason: "done" },
  ];
  const actions = {
    humanClick: async (page, x, y) => calls.push(["click", page.id, x, y]),
    humanType: async (page, text) => calls.push(["type", page.id, text]),
    humanScroll: async (page) => calls.push(["scroll", page.id]),
  };

  const result = await runReActLoop({
    page: { id: "page-1" },
    taskGoal: "publish",
    captureScreen: async (page) => {
      calls.push(["capture", page.id]);
      return `image-${calls.length}`;
    },
    askAgentBrain: async (image, taskGoal) => {
      calls.push(["brain", image, taskGoal]);
      return decisions.shift();
    },
    actions,
    logger: { log: () => {} },
  });

  assert.equal(result.status, "success");
  assert.equal(result.steps, 4);
  assert.deepEqual(calls, [
    ["capture", "page-1"],
    ["brain", "image-1", "publish"],
    ["click", "page-1", 10, 20],
    ["capture", "page-1"],
    ["brain", "image-4", "publish"],
    ["type", "page-1", "hello"],
    ["capture", "page-1"],
    ["brain", "image-7", "publish"],
    ["scroll", "page-1"],
    ["capture", "page-1"],
    ["brain", "image-10", "publish"],
  ]);
});

test("runReActLoop stops without executing an action when the brain returns failed", async () => {
  const calls = [];
  const result = await runReActLoop({
    page: { id: "page-1" },
    taskGoal: "publish",
    captureScreen: async () => "image",
    askAgentBrain: async () => ({ action: "failed", reason: "blocked" }),
    actions: {
      humanClick: async () => calls.push("click"),
      humanType: async () => calls.push("type"),
      humanScroll: async () => calls.push("scroll"),
    },
    logger: { log: () => {} },
  });

  assert.equal(result.status, "failed");
  assert.equal(result.steps, 1);
  assert.deepEqual(calls, []);
});

test("runReActLoop stops at maxSteps when no terminal action is returned", async () => {
  const calls = [];
  const result = await runReActLoop({
    page: { id: "page-1" },
    taskGoal: "watch",
    maxSteps: 2,
    captureScreen: async () => "image",
    askAgentBrain: async () => ({ action: "scroll", reason: "next" }),
    actions: {
      humanClick: async () => calls.push("click"),
      humanType: async () => calls.push("type"),
      humanScroll: async () => calls.push("scroll"),
    },
    logger: { log: () => {} },
  });

  assert.equal(result.status, "max_steps");
  assert.equal(result.steps, 2);
  assert.deepEqual(calls, ["scroll", "scroll"]);
});

test("runReActLoop rejects unsupported actions", async () => {
  await assert.rejects(
    () =>
      runReActLoop({
        page: { id: "page-1" },
        taskGoal: "publish",
        captureScreen: async () => "image",
        askAgentBrain: async () => ({ action: "hover", reason: "unknown" }),
        logger: { log: () => {} },
      }),
    /Unsupported action: hover/,
  );
});
