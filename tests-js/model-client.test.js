const assert = require("node:assert/strict");
const test = require("node:test");

const {
  buildModelRequest,
  normalizeModelConfig,
  selectModelConfig,
  askModel,
} = require("../browser/model-client");

test("normalizeModelConfig fills provider defaults", () => {
  assert.deepEqual(normalizeModelConfig({
    id: "grok-main",
    provider: "grok",
    api_key: "secret",
    model: "grok-4.5",
  }), {
    id: "grok-main",
    provider: "grok",
    enabled: true,
    base_url: "https://api.x.ai/v1",
    api_key: "secret",
    model: "grok-4.5",
    mode: "responses",
  });
});

test("selectModelConfig returns default enabled model by id", () => {
  const settings = {
    models: {
      default_model_id: "qwen-main",
      items: [
        { id: "disabled", provider: "deepseek", enabled: false },
        { id: "qwen-main", provider: "qwen", api_key: "secret", model: "qwen-plus" },
      ],
    },
  };

  assert.equal(selectModelConfig(settings).id, "qwen-main");
  assert.equal(selectModelConfig(settings, "qwen-main").provider, "qwen");
  assert.throws(() => selectModelConfig({ models: { items: [] } }), /No enabled model/);
});

test("buildModelRequest creates responses image request when image is provided", () => {
  const request = buildModelRequest({
    config: normalizeModelConfig({
      provider: "gpt",
      base_url: "https://api.openai.com/v1",
      api_key: "secret",
      model: "gpt-4.1",
      mode: "responses",
    }),
    messages: [{ role: "user", content: "look" }],
    imageBase64: "abc",
  });

  assert.equal(request.url, "https://api.openai.com/v1/responses");
  assert.equal(request.headers.Authorization, "Bearer secret");
  assert.equal(request.body.model, "gpt-4.1");
  assert.equal(request.body.input[0].content[0].type, "input_text");
  assert.equal(request.body.input[0].content[1].type, "input_image");
});

test("askModel sends request through injected fetch and extracts text", async () => {
  const calls = [];
  const result = await askModel({
    config: {
      provider: "deepseek",
      api_key: "secret",
      model: "deepseek-chat",
    },
    messages: [{ role: "user", content: "hello" }],
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return {
        ok: true,
        json: async () => ({
          choices: [{ message: { content: "world" } }],
        }),
      };
    },
  });

  assert.equal(result.text, "world");
  assert.equal(calls[0].url, "https://api.deepseek.com/v1/chat/completions");
  assert.equal(calls[0].options.method, "POST");
});
