# Human Scroll Action Executor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `humanScroll(page)` for Stage 2.3 so the browser can simulate watching the next video by scrolling most of one viewport.

**Architecture:** Extend the existing `browser/actions.js` module for human-like browser actions. Use `page.evaluate` so scrolling happens in the page context through `window.scrollBy`, while injecting randomness in tests for deterministic coverage.

**Tech Stack:** Node.js CommonJS, Playwright `page.evaluate`, `node:test`.

## Global Constraints

- `humanScroll(page)` must use `page.evaluate`.
- The evaluated function must call `window.scrollBy`.
- Scroll distance must be between `80%` and `100%` of `window.innerHeight`.
- Tests must not rely on real randomness.

---

### Task 1: Add Human Scroll Helper

**Files:**
- Modify: `tests-js/actions.test.js`
- Modify: `browser/actions.js`

**Interfaces:**
- Consumes: Playwright-like `page` object with `page.evaluate`.
- Produces: `humanScroll(page: object, options?: { random?: () => number }) => Promise<void>`

- [ ] **Step 1: Write the failing test**

```js
const { humanClick, humanScroll, humanType } = require("../browser/actions");

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test tests-js\actions.test.js`

Expected: FAIL because `humanScroll` is not a function.

- [ ] **Step 3: Implement helper**

```js
async function humanScroll(page, options = {}) {
  const random = options.random || Math.random;
  const scrollPercent = randomInt(80, 100, random);

  await page.evaluate((percent) => {
    window.scrollBy(0, window.innerHeight * (percent / 100));
  }, scrollPercent);
}

module.exports = {
  humanClick,
  humanScroll,
  humanType,
};
```

- [ ] **Step 4: Run verification**

Run: `node --test tests-js\actions.test.js`

Expected: PASS.

Run: `npm.cmd run test:node --cache .\.npm-cache`

Expected: all Node tests pass.

Run: `.\.venv\Scripts\python.exe -m pytest tests -p no:cacheprovider -q --basetemp=work\pytest-tmp-<random>`

Expected: all Python tests pass.
