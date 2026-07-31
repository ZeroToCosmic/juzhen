# AdsPower Grok CLI Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a command-line loop that can inspect local AdsPower browser environments, launch one profile, open TikTok login, let a Grok vision model choose allowed browser actions, execute those actions through Playwright CDP, then close the profile.

**Architecture:** Keep AdsPower API access isolated in `browser/adspower.js`, Grok vision decisions in `browser/grok-brain.js`, and orchestration in `browser/direct-agent.js`. Reuse existing CDP connection, screenshot, and human action helpers while adding an allowlist so the direct agent only executes navigation, click, type, scroll, wait, done, and blocked actions.

**Tech Stack:** Node.js, Playwright, OpenAI-compatible xAI Responses API, Node built-in `node:test`.

## Global Constraints

- Use AdsPower local API through injectable client functions so unit tests do not require live AdsPower.
- Use Grok through `https://api.x.ai/v1` with Responses API image inputs.
- Default TikTok login URL is `https://www.tiktok.com/login`.
- Read xAI API key from `XAI_API_KEY` unless config overrides it.
- The direct agent must reject unsupported actions instead of executing them.
- The command must close the AdsPower profile after execution when `close_after_run` is true.
- No automatic like, follow, comment, message, captcha bypass, or engagement farming behavior is included.

---

### Task 1: AdsPower Profile Adapter

**Files:**
- Create: `browser/adspower.js`
- Test: `tests-js/adspower.test.js`

**Interfaces:**
- Produces: `summarizeProfiles(profiles: object[], openedProfiles: object[]) -> { total, opened, byGroup }`
- Produces: `normalizeProfile(raw: object) -> object`
- Produces: `buildStartOptions(options: object) -> object`
- Produces: `createAdsPowerAdapter(client: object) -> { listProfiles, listOpened, summarize, openProfile, closeProfile }`

- [x] **Step 1: Write failing tests**

- [x] **Step 2: Run test to verify it fails**

Run: `node --test tests-js/adspower.test.js`
Expected: FAIL because `browser/adspower.js` does not exist.

- [x] **Step 3: Write minimal implementation**

- [x] **Step 4: Run test to verify it passes**

Run: `node --test tests-js/adspower.test.js`
Expected: PASS.

### Task 2: Grok Vision Brain

**Files:**
- Create: `browser/grok-brain.js`
- Test: `tests-js/grok-brain.test.js`

**Interfaces:**
- Produces: `normalizeGrokDecision(raw: object|string) -> { action, x, y, text, ms, reason }`
- Produces: `askGrokVision(base64Image: string, taskGoal: string, options: object) -> Promise<object>`

- [x] **Step 1: Write failing tests**

- [x] **Step 2: Run test to verify it fails**

Run: `node --test tests-js/grok-brain.test.js`
Expected: FAIL because `browser/grok-brain.js` does not exist.

- [x] **Step 3: Write minimal implementation**

- [x] **Step 4: Run test to verify it passes**

Run: `node --test tests-js/grok-brain.test.js`
Expected: PASS.

### Task 3: Direct Agent Orchestrator

**Files:**
- Create: `browser/direct-agent.js`
- Modify: `package.json`
- Test: `tests-js/direct-agent.test.js`

**Interfaces:**
- Produces: `parseDirectAgentArgs(argv: string[]) -> object`
- Produces: `runDirectAgent(options: object) -> Promise<object>`

- [x] **Step 1: Write failing tests**

- [x] **Step 2: Run test to verify it fails**

Run: `node --test tests-js/direct-agent.test.js`
Expected: FAIL because `browser/direct-agent.js` does not exist.

- [x] **Step 3: Write minimal implementation**

- [x] **Step 4: Run test to verify it passes**

Run: `node --test tests-js/direct-agent.test.js`
Expected: PASS.

### Task 4: Configuration Example and Regression

**Files:**
- Modify: `config.example.json`
- Test: existing Node test suite

**Interfaces:**
- Produces: documented config keys for AdsPower, Grok, and direct agent behavior.

- [x] **Step 1: Update config example**

- [x] **Step 2: Run regression checks**

Run: `node --test tests-js/*.test.js`
Expected: PASS.
