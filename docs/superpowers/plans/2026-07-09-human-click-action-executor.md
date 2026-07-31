# Human Click Action Executor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `humanClick(page, x, y)` for Stage 2.3 so browser actions use a small random offset and a short human-like pause before clicking.

**Architecture:** Create a focused `browser/actions.js` module for browser action execution helpers. Keep the function testable by allowing optional injection of a random number provider and sleep function while using real randomness and timers by default.

**Tech Stack:** Node.js CommonJS, Playwright page mouse API, `node:test`.

## Global Constraints

- `humanClick(page, x, y)` must not call direct `page.click`.
- X and Y must receive a random offset in the inclusive range `-5` to `5`.
- The function must call `page.mouse.move(adjustedX, adjustedY)` before pressing.
- The function must wait a random `50` to `150` milliseconds between move and mouse down.
- The click sequence must be `move -> wait -> down -> up`.

---

### Task 1: Add Human Click Helper

**Files:**
- Create: `tests-js/actions.test.js`
- Create: `browser/actions.js`

**Interfaces:**
- Consumes: Playwright-like `page` object with `page.mouse.move`, `page.mouse.down`, `page.mouse.up`.
- Produces: `humanClick(page: object, x: number, y: number, options?: { random?: () => number, sleep?: (ms: number) => Promise<void> | void }) => Promise<void>`

- [ ] **Step 1: Write the failing test**

```js
const assert = require("node:assert/strict");
const test = require("node:test");

const { humanClick } = require("../browser/actions");

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test tests-js\actions.test.js`

Expected: FAIL because `../browser/actions` cannot be found.

- [ ] **Step 3: Implement helper**

```js
function randomInt(min, max, random = Math.random) {
  return Math.floor(random() * (max - min + 1)) + min;
}

function defaultSleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function humanClick(page, x, y, options = {}) {
  const random = options.random || Math.random;
  const sleep = options.sleep || defaultSleep;
  const offsetX = randomInt(-5, 5, random);
  const offsetY = randomInt(-5, 5, random);
  const waitMs = randomInt(50, 150, random);

  await page.mouse.move(x + offsetX, y + offsetY);
  await sleep(waitMs);
  await page.mouse.down();
  await page.mouse.up();
}

module.exports = { humanClick };
```

- [ ] **Step 4: Run verification**

Run: `node --test tests-js\actions.test.js`

Expected: PASS.

Run: `npm.cmd run test:node --cache .\.npm-cache`

Expected: all Node tests pass.

Run: `.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q --basetemp=work\pytest-tmp`

Expected: all Python tests pass.
