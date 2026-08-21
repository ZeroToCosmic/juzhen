const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  connectToActivePage,
  defaultConfigPath,
  parseCliArgs,
  runAgent,
} = require("../browser/cdp");


test("defaultConfigPath stays in the project when the process cwd changes", () => {
  const originalCwd = process.cwd();
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "cdp-cwd-"));
  process.chdir(tempDir);
  try {
    assert.equal(
      defaultConfigPath(),
      path.resolve(__dirname, "..", "config.json"),
    );
  } finally {
    process.chdir(originalCwd);
  }
});

test("parseCliArgs reads cdpUrl and taskGoal from process argv positions", () => {
  assert.deepEqual(
    parseCliArgs(["node", "browser/agent.js", "http://127.0.0.1:9222", "publish"]),
    {
      cdpUrl: "http://127.0.0.1:9222",
      taskGoal: "publish",
    },
  );
});

test("parseCliArgs rejects missing cdpUrl or taskGoal", () => {
  assert.throws(
    () => parseCliArgs(["node", "browser/agent.js", "http://127.0.0.1:9222"]),
    /Usage: node browser\/agent\.js <cdpUrl> <taskGoal>/,
  );
});

test("connectToActivePage connects over CDP and selects the last existing page", async () => {
  const firstPage = { id: "first" };
  const activePage = { id: "last" };
  const context = {
    pages: () => [firstPage, activePage],
  };
  const browser = {
    contexts: () => [context],
  };
  const chromium = {
    connectOverCDP: async (cdpUrl) => {
      assert.equal(cdpUrl, "http://127.0.0.1:9222");
      return browser;
    },
  };

  const result = await connectToActivePage("http://127.0.0.1:9222", chromium);

  assert.equal(result.browser, browser);
  assert.equal(result.context, context);
  assert.equal(result.page, activePage);
});

test("connectToActivePage creates a context and page when none exist", async () => {
  const createdPage = { id: "created-page" };
  const createdContext = {
    pages: () => [],
    newPage: async () => createdPage,
  };
  const browser = {
    contexts: () => [],
    newContext: async () => createdContext,
  };
  const chromium = {
    connectOverCDP: async () => browser,
  };

  const result = await connectToActivePage("http://127.0.0.1:9222", chromium);

  assert.equal(result.context, createdContext);
  assert.equal(result.page, createdPage);
});

test("runAgent parses args, connects, and logs a concise status", async () => {
  const page = { url: () => "https://example.com/current" };
  const context = { pages: () => [page] };
  const browser = { contexts: () => [context] };
  const logs = [];
  const loopCalls = [];
  const chromium = {
    connectOverCDP: async () => browser,
  };

  const result = await runAgent({
    argv: ["node", "browser/agent.js", "http://127.0.0.1:9222", "publish"],
    chromium,
    logger: { log: (message) => logs.push(message) },
    reactLoop: async (options) => {
      loopCalls.push(options);
      return { status: "success", steps: 1 };
    },
  });

  assert.equal(result.taskGoal, "publish");
  assert.equal(result.page, page);
  assert.deepEqual(result.loopResult, { status: "success", steps: 1 });
  assert.equal(loopCalls.length, 1);
  assert.equal(loopCalls[0].page, page);
  assert.equal(loopCalls[0].taskGoal, "publish");
  assert.deepEqual(logs, [
    "Connected to CDP browser. Current page: https://example.com/current",
  ]);
});

test("runAgent falls back to browser settings from config file", async () => {
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "cdp-config-"));
  const configPath = path.join(tempDir, "config.json");
  fs.writeFileSync(
    configPath,
    JSON.stringify({
      browser: {
        cdp_url: "http://127.0.0.1:9222",
        task_goal: "configured-goal",
      },
    }),
    "utf8",
  );
  const page = { url: () => "https://example.com/current" };
  const context = { pages: () => [page] };
  const browser = { contexts: () => [context] };
  const loopCalls = [];
  const chromium = {
    connectOverCDP: async (cdpUrl) => {
      assert.equal(cdpUrl, "http://127.0.0.1:9222");
      return browser;
    },
  };

  const result = await runAgent({
    argv: ["node", "browser/agent.js"],
    chromium,
    configPath,
    logger: { log: () => {} },
    reactLoop: async (options) => {
      loopCalls.push(options);
      return { status: "success", steps: 1 };
    },
  });

  assert.equal(result.taskGoal, "configured-goal");
  assert.equal(loopCalls.length, 1);
  assert.equal(loopCalls[0].taskGoal, "configured-goal");
});
