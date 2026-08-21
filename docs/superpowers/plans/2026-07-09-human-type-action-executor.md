# Human Type Action Executor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `humanType(page, text)` for Stage 2.3 so browser keyboard input is sent one character at a time with a short random pause between characters.

**Architecture:** Extend the existing `browser/actions.js` module created for human-like browser actions. Reuse the local random integer and sleep helpers, and keep tests deterministic through optional injected `random` and `sleep` functions.

**Tech Stack:** Node.js CommonJS, Playwright page keyboard API, `node:test`.

## Global Constraints

- `humanType(page, text)` must iterate over the string and call `page.keyboard.type(char)` one character at a time.
- Each character must be followed by a random delay in the inclusive range `50` to `250` milliseconds.
- Tests must not rely on real timers or actual randomness.

---

### Task 1: Add Human Type Helper

**Files:**
- Modify: `tests-js/actions.test.js`
- Modify: `browser/actions.js`

**Interfaces:**
- Consumes: Playwright-like `page` object with `page.keyboard.type`.
- Produces: `humanType(page: object, text: string, options?: { random?: () => number, sleep?: (ms: number) => Promise<void> | void }) => Promise<void>`

- [ ] **Step 1: Write the failing test**

```js
const { humanClick, humanType } = require("../browser/actions");

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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test tests-js\actions.test.js`

Expected: FAIL because `humanType` is not a function.

- [ ] **Step 3: Implement helper**

```js
async function humanType(page, text, options = {}) {
  const random = options.random || Math.random;
  const sleep = options.sleep || defaultSleep;

  for (const char of String(text)) {
    await page.keyboard.type(char);
    await sleep(randomInt(50, 250, random));
  }
}

module.exports = {
  humanClick,
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
