#!/usr/bin/env node

const fs = require("node:fs");
const path = require("node:path");
const { chromium: defaultChromium } = require("playwright");
const { createAdsPowerAdapter } = require("./adspower");
const { humanClick, humanScroll, humanType } = require("./actions");
const { connectToActivePage } = require("./cdp");
const { askGrokVision } = require("./grok-brain");
const { captureScreen } = require("./screen");

const PROJECT_ROOT = path.resolve(__dirname, "..");
const DEFAULT_TIKTOK_LOGIN_URL = "https://www.tiktok.com/login";

function parseDirectAgentArgs(argv = process.argv) {
  const args = {
    url: DEFAULT_TIKTOK_LOGIN_URL,
    maxSteps: 10,
    closeAfterRun: true,
  };

  for (let index = 2; index < argv.length; index += 1) {
    const token = argv[index];
    const next = argv[index + 1];

    switch (token) {
      case "--profile-id":
        args.profile_id = next;
        index += 1;
        break;
      case "--profile-no":
        args.profile_no = next;
        index += 1;
        break;
      case "--url":
        args.url = next;
        index += 1;
        break;
      case "--max-steps":
        args.maxSteps = Number(next);
        index += 1;
        break;
      case "--no-close":
        args.closeAfterRun = false;
        break;
      default:
        throw new Error(`Unsupported argument: ${token}`);
    }
  }

  if (!args.profile_id && !args.profile_no) {
    throw new Error("--profile-id or --profile-no is required");
  }

  if (!Number.isInteger(args.maxSteps) || args.maxSteps < 1) {
    throw new Error("--max-steps must be a positive integer");
  }

  return args;
}

function defaultSleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function resolveCdpUrl(openResult) {
  const data = openResult.data || openResult;
  const puppeteerWs = data.ws?.puppeteer || openResult.ws?.puppeteer;

  if (puppeteerWs) {
    return puppeteerWs;
  }

  const debugPort = data.debug_port || openResult.debug_port;
  if (debugPort) {
    return `http://127.0.0.1:${debugPort}`;
  }

  throw new Error("AdsPower open result did not include ws.puppeteer or debug_port");
}

function getProfileSelector(args) {
  return args.profile_id
    ? { profile_id: args.profile_id }
    : { profile_no: args.profile_no };
}

async function executeDecision(page, decision, actions, sleep) {
  switch (decision.action) {
    case "navigate":
      if (!decision.text) {
        throw new Error("navigate action requires text URL");
      }
      await page.goto(decision.text);
      return;
    case "click":
      if (decision.x === null || decision.y === null) {
        throw new Error("click action requires x and y");
      }
      await actions.humanClick(page, decision.x, decision.y);
      return;
    case "type":
      await actions.humanType(page, decision.text);
      return;
    case "scroll":
      await actions.humanScroll(page);
      return;
    case "wait":
      await sleep(decision.ms || 1000);
      return;
    case "done":
    case "blocked":
      return;
    default:
      throw new Error(`Unsupported action: ${decision.action}`);
  }
}

async function runDirectAgent({
  args,
  adapter,
  chromium = defaultChromium,
  captureScreen: screenCapture = captureScreen,
  askVision = askGrokVision,
  actions = { humanClick, humanScroll, humanType },
  sleep = defaultSleep,
  logger = console,
}) {
  const profileSelector = getProfileSelector(args);
  const summary = await adapter.summarize();
  let finalStatus = "max_steps";

  logger.log(`AdsPower profiles: total=${summary.total} opened=${summary.opened}`);

  try {
    const openResult = await adapter.openProfile(profileSelector);
    const cdpUrl = resolveCdpUrl(openResult);
    const { page } = await connectToActivePage(cdpUrl, chromium);

    await page.goto(args.url || DEFAULT_TIKTOK_LOGIN_URL);

    for (let step = 1; step <= args.maxSteps; step += 1) {
      const base64Image = await screenCapture(page);
      const decision = await askVision(base64Image, `Operate current browser page at ${page.url()}`);
      logger.log(`Step ${step}: ${decision.action} - ${decision.reason}`);

      await executeDecision(page, decision, actions, sleep);

      if (decision.action === "done" || decision.action === "blocked") {
        finalStatus = decision.action;
        return { status: finalStatus, steps: step, decision };
      }
    }

    return { status: finalStatus, steps: args.maxSteps };
  } finally {
    if (args.closeAfterRun) {
      await adapter.closeProfile(profileSelector);
    }
  }
}

function loadConfig(configPath = defaultConfigPath()) {
  if (!fs.existsSync(configPath)) {
    return {};
  }

  return JSON.parse(fs.readFileSync(configPath, "utf8"));
}

function defaultConfigPath() {
  const configuredPath = process.env.APP_CONFIG_PATH || "config.json";
  return path.isAbsolute(configuredPath)
    ? configuredPath
    : path.resolve(PROJECT_ROOT, configuredPath);
}

function createAdsPowerHttpClient(baseUrl = "http://local.adspower.net:50325", options = {}) {
  const apiKey = options.apiKey || process.env.ADSPOWER_API_KEY || "";

  async function request(endpoint, params = {}) {
    const url = new URL(endpoint, baseUrl);
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null && value !== "") {
        url.searchParams.set(key, value);
      }
    }

    const headers = apiKey ? { Authorization: `Bearer ${apiKey}` } : undefined;
    const response = await fetch(url, { headers });
    if (!response.ok) {
      throw new Error(`AdsPower request failed: ${response.status} ${response.statusText}`);
    }

    const body = await response.json();
    if (body.code && body.code !== 0) {
      throw new Error(`AdsPower request failed: ${body.msg || body.message || body.code}`);
    }

    return body.data || body;
  }

  return {
    getBrowserList: (options) => request("/api/v1/user/list", options),
    getOpenedBrowser: () => request("/api/v1/browser/local-active"),
    openBrowser: (options) => request("/api/v1/browser/start", mapProfileKeys(options)),
    closeBrowser: (options) => request("/api/v1/browser/stop", mapProfileKeys(options)),
  };
}

function mapProfileKeys(options = {}) {
  const mapped = { ...options };

  if (mapped.profile_id) {
    mapped.user_id = mapped.profile_id;
    delete mapped.profile_id;
  }

  if (mapped.profile_no) {
    mapped.serial_number = mapped.profile_no;
    delete mapped.profile_no;
  }

  return mapped;
}

if (require.main === module) {
  const config = loadConfig();
  const parsedArgs = parseDirectAgentArgs(process.argv);
  const adspowerBaseUrl = config.adspower?.base_url || "http://local.adspower.net:50325";
  const agentConfig = config.agent || {};
  const grokConfig = config.grok || {};
  const args = {
    ...parsedArgs,
    url: parsedArgs.url || config.browser?.tiktok_login_url || DEFAULT_TIKTOK_LOGIN_URL,
    maxSteps: parsedArgs.maxSteps || agentConfig.max_steps || 10,
    closeAfterRun: parsedArgs.closeAfterRun ?? agentConfig.close_after_run ?? true,
  };
  const adapter = createAdsPowerAdapter(createAdsPowerHttpClient(adspowerBaseUrl, {
    apiKey: config.adspower?.api_key,
  }));

  runDirectAgent({
    args,
    adapter,
    askVision: (base64Image, taskGoal) => askGrokVision(base64Image, taskGoal, {
      apiKeyEnv: grokConfig.api_key_env || "XAI_API_KEY",
      model: grokConfig.model || "grok-4.5",
      strategyPrompt: agentConfig.strategy_prompt,
    }),
  }).catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}

module.exports = {
  DEFAULT_TIKTOK_LOGIN_URL,
  createAdsPowerHttpClient,
  executeDecision,
  loadConfig,
  parseDirectAgentArgs,
  resolveCdpUrl,
  runDirectAgent,
};
