# CDP Browser Agent Stage 2.1.1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Node command-line entrypoint that reads a CDP URL and task goal, connects to Chromium over CDP with Playwright, and selects the active working page using the approved fallback strategy.

**Architecture:** Keep Playwright-dependent runtime code in `browser/agent.js` and put testable browser selection logic in `browser/cdp.js`. Use Node's built-in test runner with fake Chromium objects so automated tests do not require a live browser.

**Tech Stack:** Node.js, Playwright, Node built-in `node:test`

## Global Constraints

- Read `process.argv[2]` as `cdpUrl`.
- Read `process.argv[3]` as task goal.
- Connect through `playwright.chromium.connectOverCDP(cdpUrl)`.
- Select the last existing page from the last existing browser context.
- If no context exists, create one.
- If no page exists, create one.

---

### Task 1: CDP Connection Core

**Files:**
- Create: `browser/cdp.js`
- Create: `browser/agent.js`
- Create: `tests-js/cdp.test.js`
- Create: `package.json`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `parseCliArgs(argv: string[]) -> { cdpUrl: string, taskGoal: string }`
- Produces: `connectToActivePage(cdpUrl: string, chromium: object) -> Promise<{ browser, context, page }>`
- Produces: `runAgent({ argv, chromium, logger }) -> Promise<{ cdpUrl, taskGoal, browser, context, page }>`

- [ ] **Step 1: Write failing tests**

Use Node's built-in test runner to verify CLI parsing and CDP page selection with fake Chromium objects.

- [ ] **Step 2: Run tests to verify they fail**

Run: `node --test tests-js/cdp.test.js`

Expected: FAIL because `browser/cdp.js` does not exist.

- [ ] **Step 3: Implement core logic**

Create `browser/cdp.js` and `browser/agent.js`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `node --test tests-js/cdp.test.js`

Expected: PASS.

- [ ] **Step 5: Run regression checks**

Run: `node --test tests-js/cdp.test.js` and `.\\.venv\\Scripts\\python.exe -m pytest -p no:cacheprovider -q`.

Expected: Node tests pass and Python tests pass.
