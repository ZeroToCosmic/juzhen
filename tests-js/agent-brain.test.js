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

  const result = await askAgentBrain("abc123", "click publish button", {
    client,
  });

  assert.deepEqual(result, {
    action: "click",
    x: 10,
    y: 20,
    text: "",
    reason: "submit button",
  });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].model, "gpt-4o");
  assert.equal(calls[0].messages[0].role, "system");
  assert.equal(calls[0].messages[1].role, "user");
  assert.match(SYSTEM_PROMPT, /strict JSON/i);
  assert.match(SYSTEM_PROMPT, /action/);
  assert.match(SYSTEM_PROMPT, /x/);
  assert.match(SYSTEM_PROMPT, /y/);
  assert.match(SYSTEM_PROMPT, /text/);
  assert.match(SYSTEM_PROMPT, /reason/);
  assert.match(calls[0].messages[1].content[0].text, /click publish button/);
  assert.equal(calls[0].messages[1].content[1].type, "image_url");
  assert.equal(
    calls[0].messages[1].content[1].image_url.url,
    "data:image/jpeg;base64,abc123",
  );
});

test("askAgentBrain reports invalid JSON from the model", async () => {
  const client = {
    chat: {
      completions: {
        create: async () => ({
          choices: [{ message: { content: "not json" } }],
        }),
      },
    },
  };

  await assert.rejects(
    () => askAgentBrain("abc123", "click publish button", { client }),
    /OpenAI response was not valid JSON/,
  );
});
