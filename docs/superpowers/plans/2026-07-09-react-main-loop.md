# ReAct Main Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the Stage 2.4 ReAct loop that repeatedly captures the screen, asks the vision brain for the next action, executes human-like browser actions, and stops on terminal actions or after 10 steps.

**Architecture:** Create `browser/react-loop.js` for the loop itself, with injectable dependencies for deterministic tests. Update `browser/cdp.js` so `runAgent` connects to the active page and then calls the loop as the main runtime flow.

**Tech Stack:** Node.js CommonJS, Playwright-like page object, existing `captureScreen`, `askAgentBrain`, and human action helpers, `node:test`.

## Global Constraints

- Maximum steps defaults to `10`.
- Each step must call `captureScreen -> askAgentBrain -> action switch`.
- Terminal actions `success` and `failed` stop the loop without executing a browser action.
- Supported executable actions are `click`, `type`, and `scroll`.
- Tests must use injected fakes and must not call OpenAI or a real browser.

---

### Task 1: Add ReAct Loop

**Files:**
- Create: `tests-js/react-loop.test.js`
- Create: `browser/react-loop.js`
- Modify: `tests-js/cdp.test.js`
- Modify: `browser/cdp.js`

**Interfaces:**
- Consumes: `page`, `taskGoal`, `captureScreen(page)`, `askAgentBrain(base64Image, taskGoal)`, `humanClick(page, x, y)`, `humanType(page, text)`, `humanScroll(page)`.
- Produces: `runReActLoop({ page, taskGoal, maxSteps?, captureScreen?, askAgentBrain?, actions?, logger? }) => Promise<{ status: string, steps: number, decision?: object }>`

- [ ] **Step 1: Write failing loop tests**

```js
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
```

- [ ] **Step 2: Run tests to verify failure**

Run: `node --test tests-js\react-loop.test.js`

Expected: FAIL because `../browser/react-loop` cannot be found.

- [ ] **Step 3: Implement loop and wire runAgent**

```js
async function runReActLoop({ page, taskGoal, maxSteps = 10, captureScreen = defaultCaptureScreen, askAgentBrain = defaultAskAgentBrain, actions = defaultActions, logger = console }) {
  let lastDecision;
  for (let step = 1; step <= maxSteps; step += 1) {
    const image = await captureScreen(page);
    const decision = await askAgentBrain(image, taskGoal);
    lastDecision = decision;
    if (decision.action === "success" || decision.action === "failed") {
      return { status: decision.action, steps: step, decision };
    }
    switch (decision.action) {
      case "click":
        await actions.humanClick(page, decision.x, decision.y);
        break;
      case "type":
        await actions.humanType(page, decision.text);
        break;
      case "scroll":
        await actions.humanScroll(page);
        break;
      default:
        throw new Error(`Unsupported action: ${decision.action}`);
    }
  }
  return { status: "max_steps", steps: maxSteps, decision: lastDecision };
}
```

- [ ] **Step 4: Run verification**

Run: `npm.cmd run test:node --cache .\.npm-cache`

Expected: all Node tests pass.

Run: `.\.venv\Scripts\python.exe -m pytest tests -p no:cacheprovider -q --basetemp=work\pytest-tmp-<random>`

Expected: all Python tests pass.
