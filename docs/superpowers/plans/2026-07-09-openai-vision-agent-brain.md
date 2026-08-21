# OpenAI Vision Agent Brain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a small Node wrapper function `askAgentBrain(base64Image, taskDescription)` that sends a browser screenshot and task goal to OpenAI vision reasoning and returns a parsed JavaScript decision object.

**Architecture:** Keep the OpenAI call isolated in `browser/agent-brain.js` so later browser-control steps can import it without coupling to CDP connection code. Use dependency injection for tests, so automated tests never call the real OpenAI API.

**Tech Stack:** Node.js CommonJS, `node:test`, OpenAI JavaScript SDK, Chat Completions vision input with base64 JPEG data URL.

## Global Constraints

- Model: use `gpt-4o` as requested.
- Return shape: strict JSON object with `action`, `x`, `y`, `text`, `reason`.
- Image input: pass Base64 screenshot as `data:image/jpeg;base64,<base64Image>`.
- Tests must not perform network calls.

---

### Task 1: OpenAI Vision Brain Wrapper

**Files:**
- Create: `tests-js/agent-brain.test.js`
- Create: `browser/agent-brain.js`
- Modify: `package.json`
- Modify: `package-lock.json`

**Interfaces:**
- Consumes: `askAgentBrain(base64Image: string, taskDescription: string)`
- Produces: `askAgentBrain(base64Image, taskDescription, options?) => Promise<{ action: string, x: number | null, y: number | null, text: string, reason: string }>`

- [ ] **Step 1: Write the failing test**

```js
const assert = require("node:assert/strict");
const test = require("node:test");

const { askAgentBrain, SYSTEM_PROMPT } = require("../browser/agent-brain");

test("askAgentBrain sends task and base64 screenshot to gpt-4o and parses JSON", async () => {
  const calls = [];
  const client = {
    chat: {
      completions: {
        create: async (request) => {
          calls.push(request);
          return {
            choices: [
              {
                message: {
                  content:
                    '{"action":"click","x":10,"y":20,"text":"","reason":"submit button"}',
                },
              },
            ],
          };
        },
      },
    },
  };

  const result = await askAgentBrain("abc123", "点击发布按钮", { client });

  assert.deepEqual(result, {
    action: "click",
    x: 10,
    y: 20,
    text: "",
    reason: "submit button",
  });
  assert.equal(calls[0].model, "gpt-4o");
  assert.match(SYSTEM_PROMPT, /strict JSON/i);
  assert.match(SYSTEM_PROMPT, /action/);
  assert.match(calls[0].messages[1].content[0].text, /点击发布按钮/);
  assert.equal(
    calls[0].messages[1].content[1].image_url.url,
    "data:image/jpeg;base64,abc123",
  );
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `node --test tests-js\agent-brain.test.js`

Expected: FAIL because `../browser/agent-brain` cannot be found.

- [ ] **Step 3: Write minimal implementation**

```js
const SYSTEM_PROMPT = [
  "You are a browser visual decision agent.",
  "Return only strict JSON with keys: action, x, y, text, reason.",
  "Do not wrap the JSON in markdown or add commentary.",
].join(" ");

function createOpenAIClient(options = {}) {
  const OpenAI = require("openai");
  return new OpenAI(options.apiKey ? { apiKey: options.apiKey } : undefined);
}

async function askAgentBrain(base64Image, taskDescription, options = {}) {
  const client = options.client || createOpenAIClient(options);
  const response = await client.chat.completions.create({
    model: "gpt-4o",
    messages: [
      { role: "system", content: SYSTEM_PROMPT },
      {
        role: "user",
        content: [
          { type: "text", text: `Task: ${taskDescription}` },
          {
            type: "image_url",
            image_url: { url: `data:image/jpeg;base64,${base64Image}` },
          },
        ],
      },
    ],
  });
  const content = response.choices?.[0]?.message?.content;
  const parsed = JSON.parse(content);
  return {
    action: String(parsed.action || ""),
    x: parsed.x === null || parsed.x === undefined ? null : Number(parsed.x),
    y: parsed.y === null || parsed.y === undefined ? null : Number(parsed.y),
    text: String(parsed.text || ""),
    reason: String(parsed.reason || ""),
  };
}

module.exports = { askAgentBrain, SYSTEM_PROMPT };
```

- [ ] **Step 4: Install dependency**

Run: `npm.cmd install openai --save --cache .\.npm-cache`

Expected: `package.json` and `package-lock.json` include `openai`.

- [ ] **Step 5: Run tests**

Run: `node --test tests-js\agent-brain.test.js`

Expected: PASS.

Run: `npm.cmd run test:node --cache .\.npm-cache`

Expected: all Node tests pass.

Run: `.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q`

Expected: all Python tests pass.
