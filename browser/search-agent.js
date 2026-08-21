#!/usr/bin/env node

const { chromium: defaultChromium } = require("playwright");
const { createAdsPowerAdapter } = require("./adspower");
const {
  applyMouseStrategy: defaultApplyMouseStrategy,
  generateMouseStrategies,
  humanType: defaultHumanType,
} = require("./actions");
const {
  createAdsPowerHttpClient,
  loadConfig,
  resolveCdpUrl,
} = require("./direct-agent");
const { connectToActivePage } = require("./cdp");

function parseSearchAgentArgs(argv = process.argv) {
  const args = {
    profileNos: "",
    profileIds: "",
    url: "",
    loginCheckXpath: "",
    searchXpath: "",
    query: "",
    strategy: "rotate",
    closeAfterRun: true,
  };

  for (let index = 2; index < argv.length; index += 1) {
    const token = argv[index];
    const next = argv[index + 1];

    switch (token) {
      case "--profile-nos":
        args.profileNos = next || "";
        index += 1;
        break;
      case "--profile-ids":
        args.profileIds = next || "";
        index += 1;
        break;
      case "--url":
        args.url = next || "";
        index += 1;
        break;
      case "--login-check-xpath":
        args.loginCheckXpath = next || "";
        index += 1;
        break;
      case "--search-xpath":
        args.searchXpath = next || "";
        index += 1;
        break;
      case "--query":
        args.query = next || "";
        index += 1;
        break;
      case "--strategy":
        args.strategy = next || "rotate";
        index += 1;
        break;
      case "--no-close":
        args.closeAfterRun = false;
        break;
      default:
        throw new Error(`Unsupported argument: ${token}`);
    }
  }

  if (!args.url) {
    throw new Error("--url is required");
  }
  if (!args.searchXpath) {
    throw new Error("--search-xpath is required");
  }

  return args;
}

function splitCsv(value) {
  return String(value || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

async function resolveProfileSelectors(adapter, args) {
  const profileIds = splitCsv(args.profileIds);
  if (profileIds.length) {
    return profileIds.map((profile_id) => ({ profile_id }));
  }

  const profileNos = splitCsv(args.profileNos);
  if (profileNos.length) {
    return profileNos.map((profile_no) => ({ profile_no }));
  }

  const profiles = await adapter.listProfiles({ limit: 200, page: 1 });
  return profiles
    .map((profile) => (
      profile.profile_id
        ? { profile_id: profile.profile_id }
        : { profile_no: profile.profile_no }
    ))
    .filter((profile) => profile.profile_id || profile.profile_no);
}

async function isLoggedIn(page, loginCheckXpath) {
  if (!loginCheckXpath) {
    return !String(page.url()).includes("/login");
  }

  return (await page.locator(`xpath=${loginCheckXpath}`).count()) > 0;
}

async function typeSearchByXpath(page, searchXpath, query, actions = {}) {
  const locator = page.locator(`xpath=${searchXpath}`).first();
  await locator.click();
  await locator.fill("");
  await (actions.humanType || defaultHumanType)(page, query);
  await page.keyboard.press("Enter");
}

function selectStrategy(strategies, strategyId, index) {
  if (strategyId && strategyId !== "rotate") {
    return strategies.find((strategy) => strategy.id === strategyId) || strategies[0];
  }

  return strategies[index % strategies.length];
}

function normalizeConfiguredStrategy(strategy) {
  return {
    ...strategy,
    textPrompt: strategy.textPrompt || strategy.text_prompt || "",
  };
}

function resolveConfiguredStrategies(config = {}) {
  const configured = config.execution_strategies?.items;
  if (Array.isArray(configured) && configured.length) {
    return configured.map(normalizeConfiguredStrategy);
  }
  return generateMouseStrategies();
}

async function runProfileSearch({
  selector,
  adapter,
  chromium,
  args,
  strategy,
  actions,
  logger,
}) {
  try {
    const openResult = await adapter.openProfile(selector);
    const cdpUrl = resolveCdpUrl(openResult);
    const { page } = await connectToActivePage(cdpUrl, chromium);

    await page.goto(args.url);

    if (!(await isLoggedIn(page, args.loginCheckXpath))) {
      logger.log(`Profile not logged in: ${JSON.stringify(selector)}`);
      return { selector, status: "not_logged_in" };
    }

    await (actions.applyMouseStrategy || defaultApplyMouseStrategy)(page, strategy);
    await typeSearchByXpath(page, args.searchXpath, args.query, actions);

    return { selector, status: "searched", strategy: strategy.id };
  } finally {
    if (args.closeAfterRun) {
      await adapter.closeProfile(selector);
    }
  }
}

async function runSearchAgent({
  args,
  adapter,
  chromium = defaultChromium,
  strategies = generateMouseStrategies(),
  actions = {},
  logger = console,
}) {
  const selectors = await resolveProfileSelectors(adapter, args);
  const results = [];

  logger.log(`AdsPower windows selected: ${selectors.length}`);

  for (let index = 0; index < selectors.length; index += 1) {
    results.push(await runProfileSearch({
      selector: selectors[index],
      adapter,
      chromium,
      args,
      strategy: selectStrategy(strategies, args.strategy, index),
      actions,
      logger,
    }));
  }

  return {
    total: selectors.length,
    results,
  };
}

if (require.main === module) {
  const config = loadConfig();
  const args = parseSearchAgentArgs(process.argv);
  const adapter = createAdsPowerAdapter(
    createAdsPowerHttpClient(
      config.adspower?.base_url || "http://local.adspower.net:50325",
      { apiKey: config.adspower?.api_key },
    ),
  );

  runSearchAgent({
    args,
    adapter,
    strategies: resolveConfiguredStrategies(config),
  }).catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}

module.exports = {
  isLoggedIn,
  parseSearchAgentArgs,
  resolveConfiguredStrategies,
  resolveProfileSelectors,
  runProfileSearch,
  runSearchAgent,
  splitCsv,
  typeSearchByXpath,
};
