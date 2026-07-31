const ALLOWED_ACTIONS = new Set([
  "navigate",
  "click",
  "type",
  "scroll",
  "wait",
  "done",
  "blocked",
]);

const DEFAULT_STRATEGY_PROMPT = [
  "You are controlling a browser window through a small action API.",
  "Return only strict JSON with keys: action, x, y, text, ms, reason.",
  "Allowed actions are navigate, click, type, scroll, wait, done, blocked.",
  "Use navigate with text set to a URL.",
  "Use wait with ms set to a delay in milliseconds.",
  "Use blocked when a captcha, login challenge, or unsafe action is needed.",
  "Do not like, follow, comment, send messages, bypass captchas, or create platform engagement.",
].join(" ");

function createXaiClient(options = {}) {
  const OpenAI = require("openai");
  const apiKey = options.apiKey || process.env[options.apiKeyEnv || "XAI_API_KEY"];

  if (!apiKey) {
    throw new Error("XAI API key is required; set XAI_API_KEY or configure grok.api_key_env");
  }

  return new OpenAI({
    apiKey,
    baseURL: options.baseURL || "https://api.x.ai/v1",
    timeout: options.timeout || 360000,
  });
}

function normalizeGrokDecision(raw) {
  const decision = typeof raw === "string" ? JSON.parse(raw) : raw || {};
  const action = String(decision.action || "");

  if (!ALLOWED_ACTIONS.has(action)) {
    throw new Error(`Unsupported action: ${action}`);
  }

  return {
    action,
    x: normalizeNumber(decision.x),
    y: normalizeNumber(decision.y),
    text: String(decision.text || ""),
    ms: normalizeNumber(decision.ms),
    reason: String(decision.reason || ""),
  };
}

function normalizeNumber(value) {
  if (value === null || value === undefined || value === "") {
    return null;
  }

  const numberValue = Number(value);
  return Number.isFinite(numberValue) ? numberValue : null;
}

function extractOutputText(response) {
  if (response.output_text) {
    return response.output_text;
  }

  const textParts = [];
  for (const output of response.output || []) {
    for (const content of output.content || []) {
      if (content.text) {
        textParts.push(content.text);
      }
    }
  }

  return textParts.join("\n");
}

async function askGrokVision(base64Image, taskGoal, options = {}) {
  const client = options.client || createXaiClient(options);
  const strategyPrompt = options.strategyPrompt || DEFAULT_STRATEGY_PROMPT;
  const response = await client.responses.create({
    model: options.model || "grok-4.5",
    store: false,
    input: [
      {
        role: "user",
        content: [
          {
            type: "input_image",
            image_url: `data:image/jpeg;base64,${base64Image}`,
            detail: options.detail || "high",
          },
          {
            type: "input_text",
            text: `Task: ${taskGoal}\nStrategy: ${strategyPrompt}`,
          },
        ],
      },
    ],
  });
  const outputText = extractOutputText(response);

  if (!outputText) {
    throw new Error("Grok response did not include output text");
  }

  return normalizeGrokDecision(outputText);
}

module.exports = {
  ALLOWED_ACTIONS,
  DEFAULT_STRATEGY_PROMPT,
  askGrokVision,
  createXaiClient,
  normalizeGrokDecision,
};
