const fs = require("node:fs");
const path = require("node:path");
const { runReActLoop } = require("./react-loop");

const PROJECT_ROOT = path.resolve(__dirname, "..");

function parseCliArgs(argv) {
  const cdpUrl = argv[2];
  const taskGoal = argv[3];

  if (!cdpUrl || !taskGoal) {
    throw new Error("Usage: node browser/agent.js <cdpUrl> <taskGoal>");
  }

  return { cdpUrl, taskGoal };
}

function loadBrowserSettings(configPath = defaultConfigPath()) {
  if (!fs.existsSync(configPath)) {
    return {};
  }

  const settings = JSON.parse(fs.readFileSync(configPath, "utf8"));
  return settings.browser || {};
}

function resolveRuntimeArgs(argv, configPath) {
  if (argv[2] && argv[3]) {
    return parseCliArgs(argv);
  }

  const browserSettings = loadBrowserSettings(configPath);
  const cdpUrl = argv[2] || browserSettings.cdp_url;
  const taskGoal = argv[3] || browserSettings.task_goal;

  if (!cdpUrl || !taskGoal) {
    return parseCliArgs(argv);
  }

  return { cdpUrl, taskGoal };
}

function defaultConfigPath() {
  const configuredPath = process.env.APP_CONFIG_PATH || "config.json";
  return path.isAbsolute(configuredPath)
    ? configuredPath
    : path.resolve(PROJECT_ROOT, configuredPath);
}

async function connectToActivePage(cdpUrl, chromium) {
  const browser = await chromium.connectOverCDP(cdpUrl);
  let contexts = browser.contexts();
  let context = contexts[contexts.length - 1];

  if (!context) {
    context = await browser.newContext();
    contexts = browser.contexts();
  }

  const pages = context.pages();
  let page = pages[pages.length - 1];

  if (!page) {
    page = await context.newPage();
  }

  return { browser, context, page };
}

async function runAgent({
  argv = process.argv,
  chromium,
  logger = console,
  configPath,
  reactLoop = runReActLoop,
}) {
  const { cdpUrl, taskGoal } = resolveRuntimeArgs(argv, configPath);
  const { browser, context, page } = await connectToActivePage(cdpUrl, chromium);
  const pageUrl = typeof page.url === "function" ? page.url() : "unknown";

  logger.log(`Connected to CDP browser. Current page: ${pageUrl}`);
  const loopResult = await reactLoop({ page, taskGoal, logger });

  return { cdpUrl, taskGoal, browser, context, page, loopResult };
}

module.exports = {
  connectToActivePage,
  defaultConfigPath,
  loadBrowserSettings,
  parseCliArgs,
  runAgent,
};
