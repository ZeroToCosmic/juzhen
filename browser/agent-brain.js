const SYSTEM_PROMPT = [
  "You are a browser visual decision agent.",
  "Return only strict JSON with keys: action, x, y, text, reason.",
  "Use action for the next browser action, x and y for screen coordinates, text for typed input, and reason for a short explanation.",
  "Do not wrap the JSON in markdown or add commentary.",
].join(" ");

function createOpenAIClient(options = {}) {
  const OpenAI = require("openai");
  return new OpenAI(options.apiKey ? { apiKey: options.apiKey } : undefined);
}

function normalizeDecision(decision) {
  return {
    action: String(decision.action || ""),
    x: normalizeCoordinate(decision.x),
    y: normalizeCoordinate(decision.y),
    text: String(decision.text || ""),
    reason: String(decision.reason || ""),
  };
}

function normalizeCoordinate(value) {
  if (value === null || value === undefined || value === "") {
    return null;
  }

  const numberValue = Number(value);
  return Number.isFinite(numberValue) ? numberValue : null;
}

async function askAgentBrain(base64Image, taskDescription, options = {}) {
  const client = options.client || createOpenAIClient(options);
  const response = await client.chat.completions.create({
    model: "gpt-4o",
    messages: [
      {
        role: "system",
        content: SYSTEM_PROMPT,
      },
      {
        role: "user",
        content: [
          {
            type: "text",
            text: `Task: ${taskDescription}`,
          },
          {
            type: "image_url",
            image_url: {
              url: `data:image/jpeg;base64,${base64Image}`,
            },
          },
        ],
      },
    ],
  });
  const content = response.choices?.[0]?.message?.content;

  if (!content) {
    throw new Error("OpenAI response did not include message content");
  }

  let parsed;
  try {
    parsed = JSON.parse(content);
  } catch (error) {
    throw new Error(`OpenAI response was not valid JSON: ${error.message}`);
  }

  return normalizeDecision(parsed);
}

module.exports = {
  SYSTEM_PROMPT,
  askAgentBrain,
};
