const PROVIDER_DEFAULTS = {
  grok: {
    base_url: "https://api.x.ai/v1",
    mode: "responses",
  },
  gpt: {
    base_url: "https://api.openai.com/v1",
    mode: "responses",
  },
  deepseek: {
    base_url: "https://api.deepseek.com/v1",
    mode: "chat",
  },
  glm: {
    base_url: "https://open.bigmodel.cn/api/paas/v4",
    mode: "chat",
  },
  qwen: {
    base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    mode: "chat",
  },
};

function normalizeModelConfig(config = {}) {
  const provider = String(config.provider || "grok").toLowerCase();
  const defaults = PROVIDER_DEFAULTS[provider] || PROVIDER_DEFAULTS.grok;

  return {
    id: String(config.id || provider),
    provider,
    enabled: config.enabled !== false,
    base_url: String(config.base_url || defaults.base_url),
    api_key: String(config.api_key || ""),
    model: String(config.model || ""),
    mode: String(config.mode || defaults.mode),
  };
}

function selectModelConfig(settings = {}, modelId = "") {
  const models = settings.models || {};
  const targetId = modelId || models.default_model_id || "";
  const configs = (models.items || [])
    .map(normalizeModelConfig)
    .filter((config) => config.enabled);

  if (!configs.length) {
    throw new Error("No enabled model is configured");
  }

  if (targetId) {
    const selected = configs.find((config) => config.id === targetId);
    if (selected) return selected;
  }

  return configs[0];
}

function buildModelRequest({ config, messages = [], imageBase64 = "" }) {
  const normalized = normalizeModelConfig(config);
  const headers = {
    "Content-Type": "application/json",
    Authorization: `Bearer ${normalized.api_key}`,
  };

  if (normalized.mode === "responses" || imageBase64) {
    const text = messages.map((message) => `${message.role}: ${message.content}`).join("\n");
    const content = [{ type: "input_text", text }];
    if (imageBase64) {
      content.push({
        type: "input_image",
        image_url: `data:image/jpeg;base64,${imageBase64}`,
      });
    }

    return {
      url: `${normalized.base_url.replace(/\/$/, "")}/responses`,
      headers,
      body: {
        model: normalized.model,
        input: [{ role: "user", content }],
        store: false,
      },
    };
  }

  return {
    url: `${normalized.base_url.replace(/\/$/, "")}/chat/completions`,
    headers,
    body: {
      model: normalized.model,
      messages,
    },
  };
}

function extractModelText(payload = {}) {
  if (payload.output_text) return payload.output_text;
  if (payload.choices?.[0]?.message?.content) return payload.choices[0].message.content;

  const parts = [];
  for (const output of payload.output || []) {
    for (const content of output.content || []) {
      if (content.text) parts.push(content.text);
    }
  }
  return parts.join("\n");
}

async function askModel({
  config,
  messages = [],
  imageBase64 = "",
  fetchImpl = fetch,
}) {
  const request = buildModelRequest({ config, messages, imageBase64 });
  const response = await fetchImpl(request.url, {
    method: "POST",
    headers: request.headers,
    body: JSON.stringify(request.body),
  });

  if (!response.ok) {
    throw new Error(`Model request failed: ${response.status} ${response.statusText || ""}`.trim());
  }

  const payload = await response.json();
  return {
    provider: normalizeModelConfig(config).provider,
    model: normalizeModelConfig(config).model,
    text: extractModelText(payload),
    raw: payload,
  };
}

module.exports = {
  PROVIDER_DEFAULTS,
  askModel,
  buildModelRequest,
  extractModelText,
  normalizeModelConfig,
  selectModelConfig,
};
