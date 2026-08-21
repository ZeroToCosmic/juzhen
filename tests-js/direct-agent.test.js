const assert = require("node:assert/strict");
const test = require("node:test");

const {
  loadConfig,
  parseDirectAgentArgs,
  runDirectAgent,
} = require("../browser/direct-agent");


test("loadConfig resolves its default path from the project root", () => {
  const originalCwd = process.cwd();
  const originalConfigPath = process.env.APP_CONFIG_PATH;
  const tempDir = require("node:fs").mkdtempSync(
    require("node:path").join(require("node:os").tmpdir(), "agent-cwd-"),
  );
  process.chdir(tempDir);
  process.env.APP_CONFIG_PATH = "config.example.json";
  try {
    const config = loadConfig();
    assert.equal(config.browser.tiktok_login_url, "https://www.tiktok.com/login");
  } finally {
    process.chdir(originalCwd);
    if (originalConfigPath === undefined) {
      delete process.env.APP_CONFIG_PATH;
    } else {
      process.env.APP_CONFIG_PATH = originalConfigPath;
    }
  }
});

test("parseDirectAgentArgs reads profile, url, limits, and close flag", () => {
  assert.deepEqual(
    parseDirectAgentArgs([
      "node",
      "browser/direct-agent.js",
      "--profile-no",
      "7",
      "--url",
      "https://www.tiktok.com/login",
      "--max-steps",
      "3",
      "--no-close",
    ]),
    {
      profile_no: "7",
      url: "https://www.tiktok.com/login",
      maxSteps: 3,
      closeAfterRun: false,
    },
  );
});

test("parseDirectAgentArgs rejects missing profile selector", () => {
  assert.throws(
    () => parseDirectAgentArgs(["node", "browser/direct-agent.js"]),
    /--profile-id or --profile-no is required/,
  );
});

test("runDirectAgent opens profile, navigates to TikTok, executes allowed actions, and closes", async () => {
  const calls = [];
  const page = {
    goto: async (url) => calls.push(["goto", url]),
    url: () => "https://www.tiktok.com/login",
  };
  const browser = { contexts: () => [{ pages: () => [page] }] };
  const chromium = {
    connectOverCDP: async (cdpUrl) => {
      calls.push(["connect", cdpUrl]);
      return browser;
    },
  };
  const adapter = {
    summarize: async () => ({ total: 2, opened: 0, byGroup: { TikTok: 2 } }),
    openProfile: async (options) => {
      calls.push(["openProfile", options]);
      return { debug_port: "9222" };
    },
    closeProfile: async (options) => {
      calls.push(["closeProfile", options]);
      return { ok: true };
    },
  };
  const decisions = [
    { action: "wait", ms: 10, reason: "loading" },
    { action: "scroll", reason: "inspect page" },
    { action: "done", reason: "finished" },
  ];
  const sleeps = [];
  const result = await runDirectAgent({
    args: {
      profile_no: "7",
      url: "https://www.tiktok.com/login",
      maxSteps: 5,
      closeAfterRun: true,
    },
    adapter,
    chromium,
    captureScreen: async () => "base64-screen",
    askVision: async () => decisions.shift(),
    actions: {
      humanScroll: async (targetPage) => calls.push(["scroll", targetPage === page]),
    },
    sleep: async (ms) => sleeps.push(ms),
    logger: { log: (message) => calls.push(["log", message]) },
  });

  assert.equal(result.status, "done");
  assert.equal(result.steps, 3);
  assert.deepEqual(sleeps, [10]);
  assert.deepEqual(calls, [
    ["log", "AdsPower profiles: total=2 opened=0"],
    ["openProfile", { profile_no: "7" }],
    ["connect", "http://127.0.0.1:9222"],
    ["goto", "https://www.tiktok.com/login"],
    ["log", "Step 1: wait - loading"],
    ["log", "Step 2: scroll - inspect page"],
    ["scroll", true],
    ["log", "Step 3: done - finished"],
    ["closeProfile", { profile_no: "7" }],
  ]);
});

test("runDirectAgent closes the profile after an agent error", async () => {
  const calls = [];
  const page = { goto: async () => {}, url: () => "about:blank" };
  const browser = { contexts: () => [{ pages: () => [page] }] };
  const adapter = {
    summarize: async () => ({ total: 1, opened: 0, byGroup: {} }),
    openProfile: async () => ({ debug_port: "9333" }),
    closeProfile: async (options) => calls.push(["closeProfile", options]),
  };

  await assert.rejects(
    () => runDirectAgent({
      args: { profile_id: "abc123", maxSteps: 1, closeAfterRun: true },
      adapter,
      chromium: { connectOverCDP: async () => browser },
      captureScreen: async () => "base64-screen",
      askVision: async () => ({ action: "unknown" }),
      logger: { log: () => {} },
    }),
    /Unsupported action: unknown/,
  );

  assert.deepEqual(calls, [["closeProfile", { profile_id: "abc123" }]]);
});
