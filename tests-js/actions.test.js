const assert = require("node:assert/strict");
const test = require("node:test");

const {
  applyMouseStrategy,
  generateMouseStrategies,
  humanClick,
  humanScroll,
  humanType,
} = require("../browser/actions");

test("humanClick moves to offset coordinates, waits, then presses and releases", async () => {
  const calls = [];
  const page = {
    mouse: {
      move: async (x, y) => calls.push(["move", x, y]),
      down: async () => calls.push(["down"]),
      up: async () => calls.push(["up"]),
    },
  };
  const randomValues = [1, 0, 0.5];
  const sleepDurations = [];

  await humanClick(page, 100, 200, {
    random: () => randomValues.shift(),
    sleep: async (ms) => {
      sleepDurations.push(ms);
      calls.push(["sleep", ms]);
    },
  });

  assert.deepEqual(calls, [
    ["move", 105, 195],
    ["sleep", 100],
    ["down"],
    ["up"],
  ]);
  assert.deepEqual(sleepDurations, [100]);
});

test("humanClick keeps random offset and wait inside the configured ranges", async () => {
  const moves = [];
  const waits = [];
  const page = {
    mouse: {
      move: async (x, y) => moves.push([x, y]),
      down: async () => {},
      up: async () => {},
    },
  };

  await humanClick(page, 10, 20, {
    random: () => 0.999,
    sleep: async (ms) => waits.push(ms),
  });

  assert.deepEqual(moves, [[15, 25]]);
  assert.deepEqual(waits, [150]);
});

test("humanType types each character with a human-like delay after each one", async () => {
  const calls = [];
  const page = {
    keyboard: {
      type: async (char) => calls.push(["type", char]),
    },
  };
  const randomValues = [0, 0.5, 0.999];

  await humanType(page, "abc", {
    random: () => randomValues.shift(),
    sleep: async (ms) => calls.push(["sleep", ms]),
  });

  assert.deepEqual(calls, [
    ["type", "a"],
    ["sleep", 50],
    ["type", "b"],
    ["sleep", 150],
    ["type", "c"],
    ["sleep", 250],
  ]);
});

test("humanScroll scrolls by a random 80 to 100 percent of the viewport height", async () => {
  const calls = [];
  const previousWindow = global.window;
  global.window = {
    innerHeight: 1000,
    scrollBy: (x, y) => calls.push(["scrollBy", x, y]),
  };
  const page = {
    evaluate: async (fn, percent) => {
      calls.push(["evaluate", percent]);
      return fn(percent);
    },
  };

  try {
    await humanScroll(page, { random: () => 0.5 });
  } finally {
    global.window = previousWindow;
  }

  assert.deepEqual(calls, [
    ["evaluate", 90],
    ["scrollBy", 0, 900],
  ]);
});

test("generateMouseStrategies returns named reusable movement profiles", () => {
  const strategies = generateMouseStrategies();

  assert.deepEqual(strategies.map((strategy) => strategy.id), [
    "steady_reader",
    "curious_scanner",
    "slow_reviewer",
  ]);
  assert.equal(strategies[0].mouseMoves, 3);
  assert.equal(strategies[1].scrolls, 3);
});

test("applyMouseStrategy moves the mouse and scrolls with configured ranges", async () => {
  const calls = [];
  const page = {
    mouse: {
      move: async (x, y, options) => calls.push(["move", x, y, options]),
      wheel: async (x, y) => calls.push(["wheel", x, y]),
    },
    viewportSize: () => ({ width: 1000, height: 800 }),
  };
  const sleeps = [];
  const randomValues = [0, 0.5, 1, 0.25, 0.75, 0.25, 0.75, 0.5];

  await applyMouseStrategy(page, {
    id: "test",
    mouseMoves: 2,
    scrolls: 1,
    moveSteps: [8, 12],
    pauseMs: [100, 200],
    scrollDelta: [300, 500],
  }, {
    random: () => randomValues.shift() ?? 0.5,
    sleep: async (ms) => sleeps.push(ms),
  });

  assert.deepEqual(calls, [
    ["move", 100, 400, { steps: 12 }],
    ["move", 700, 300, { steps: 11 }],
    ["wheel", 0, 400],
  ]);
  assert.deepEqual(sleeps, [125, 150, 150]);
});
