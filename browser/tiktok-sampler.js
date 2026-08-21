#!/usr/bin/env node

const { chromium: defaultChromium } = require("playwright");
const { createAdsPowerAdapter } = require("./adspower");
const {
  createAdsPowerHttpClient,
  loadConfig,
  resolveCdpUrl,
} = require("./direct-agent");
const { connectToActivePage } = require("./cdp");

function parseSamplerArgs(argv = process.argv) {
  const args = {
    profileId: "",
    profileNo: "",
    url: "",
    closeAfterRun: true,
  };

  for (let index = 2; index < argv.length; index += 1) {
    const token = argv[index];
    const next = argv[index + 1];
    switch (token) {
      case "--profile-id":
        args.profileId = next || "";
        index += 1;
        break;
      case "--profile-no":
        args.profileNo = next || "";
        index += 1;
        break;
      case "--url":
        args.url = next || "";
        index += 1;
        break;
      case "--no-close":
        args.closeAfterRun = false;
        break;
      default:
        throw new Error(`Unsupported argument: ${token}`);
    }
  }

  if (!args.profileId && !args.profileNo) {
    throw new Error("--profile-id or --profile-no is required");
  }
  if (!args.url) {
    throw new Error("--url is required");
  }

  return args;
}

function parseCompactNumber(value) {
  const text = String(value || "").trim().replace(/,/g, "");
  const match = text.match(/([\d.]+)\s*([kKmMbB万亿]?)/);
  if (!match) return 0;
  const number = Number.parseFloat(match[1]);
  if (!Number.isFinite(number)) return 0;
  const unit = match[2].toLowerCase();
  const multipliers = {
    k: 1000,
    m: 1000000,
    b: 1000000000,
    "万": 10000,
    "亿": 100000000,
  };
  return Math.round(number * (multipliers[unit] || 1));
}

function metricAfterLabel(text, labels) {
  for (const label of labels) {
    const pattern = new RegExp(`${label}\\s*\\n?\\s*([\\d.,]+\\s*[kKmMbB万亿]?)`, "i");
    const match = text.match(pattern);
    if (match) return parseCompactNumber(match[1]);
  }
  return 0;
}

function sampleTikTokMetricsFromText(text) {
  const body = String(text || "");
  return {
    likes_24h: metricAfterLabel(body, ["likes?", "点赞", "赞"]),
    comments: metricAfterLabel(body, ["comments?", "评论"]),
    views_24h: metricAfterLabel(body, ["views?", "播放", "观看"]),
  };
}

async function textFromLocator(page, selector) {
  try {
    const locator = page.locator(selector).first();
    if ((await locator.count()) === 0) return "";
    return (await locator.innerText()).trim();
  } catch (_error) {
    return "";
  }
}

async function sampleTikTokMetrics(page) {
  const bodyText = await page.locator("body").innerText({ timeout: 10000 });
  if (/captcha|verify|verification|安全验证|验证码/i.test(bodyText)) {
    throw new Error("TikTok verification or captcha is visible");
  }

  const selectorMetrics = {
    likes_24h: parseCompactNumber(await textFromLocator(page, '[data-e2e="like-count"]')),
    comments: parseCompactNumber(await textFromLocator(page, '[data-e2e="comment-count"]')),
    views_24h: parseCompactNumber(await textFromLocator(page, '[data-e2e="video-views"]')),
  };
  const textMetrics = sampleTikTokMetricsFromText(bodyText);
  return {
    likes_24h: selectorMetrics.likes_24h || textMetrics.likes_24h,
    comments: selectorMetrics.comments || textMetrics.comments,
    views_24h: selectorMetrics.views_24h || textMetrics.views_24h,
  };
}

async function runSampler({
  args,
  adapter,
  chromium = defaultChromium,
}) {
  const selector = args.profileId
    ? { profile_id: args.profileId }
    : { profile_no: args.profileNo };

  try {
    const openResult = await adapter.openProfile(selector);
    const cdpUrl = resolveCdpUrl(openResult);
    const { page } = await connectToActivePage(cdpUrl, chromium);
    await page.goto(args.url, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(2500);
    return await sampleTikTokMetrics(page);
  } finally {
    if (args.closeAfterRun) {
      await adapter.closeProfile(selector);
    }
  }
}

if (require.main === module) {
  const config = loadConfig();
  const args = parseSamplerArgs(process.argv);
  const adapter = createAdsPowerAdapter(
    createAdsPowerHttpClient(
      config.adspower?.base_url || "http://local.adspower.net:50325",
      { apiKey: config.adspower?.api_key },
    ),
  );

  runSampler({ args, adapter })
    .then((metrics) => {
      process.stdout.write(`${JSON.stringify(metrics)}\n`);
    })
    .catch((error) => {
      process.stderr.write(`${error.message}\n`);
      process.exitCode = 1;
    });
}

module.exports = {
  parseCompactNumber,
  parseSamplerArgs,
  runSampler,
  sampleTikTokMetrics,
  sampleTikTokMetricsFromText,
};
