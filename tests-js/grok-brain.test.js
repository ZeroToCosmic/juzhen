const assert = require("node:assert/strict");
const test = require("node:test");

const {
  askGrokVision,
  normalizeGrokDecision,
} = require("../browser/grok-brain");

test("normalizeGrokDecision accepts object decisions and normalizes fields", () => {
  assert.deepEqual(
    normalizeGrokDecision({
      action: "click",
      x: "12.5",
      y: 42,
      text: null,
      ms: "1500",
      reason: "button visible",
    }),
    {
      action: "click",
      x: 12.5,
      y: 42,
      text: "",
      ms: 1500,
      reason: "button visible",
    },
  );
});

test("normalizeGrokDecision accepts JSON string decisions", () => {
  assert.deepEqual(normalizeGrokDecision('{"action":"done","reason":"finished"}'), {
    action: "done",
    x: null,
    y: null,
    text: "",
    ms: null,
    reason: "finished",
  });
});

test("normalizeGrokDecision rejects unsupported actions", () => {
  assert.throws(
    () => normalizeGrokDecision({ action: "like", reason: "not allowed" }),
    /Unsupported action: like/,
  );
});

test("askGrokVision sends a base64 image and strategy prompt to xAI responses client", async () => {
  const calls = [];
  const client = {
    responses: {
      create: async (request) => {
        calls.push(request);
        return {
          output_text: '{"action":"scroll","reason":"feed visible"}',
        };
      },
    },
  };

  const decision = await askGrokVision("abc123", "Open TikTok login", {
    client,
    model: "grok-4.5",
    strategyPrompt: "Use direct actions only.",
  });

  assert.deepEqual(decision, {
    action: "scroll",
    x: null,
    y: null,
    text: "",
    ms: null,
    reason: "feed visible",
  });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].model, "grok-4.5");
  assert.equal(calls[0].store, false);
  assert.equal(calls[0].input[0].role, "user");
  assert.deepEqual(calls[0].input[0].content[0], {
    type: "input_image",
    image_url: "data:image/jpeg;base64,abc123",
    detail: "high",
  });
  assert.match(calls[0].input[0].content[1].text, /Open TikTok login/);
  assert.match(calls[0].input[0].content[1].text, /Use direct actions only/);
});
