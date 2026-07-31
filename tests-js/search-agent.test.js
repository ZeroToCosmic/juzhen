const assert = require("node:assert/strict");
const test = require("node:test");

const {
  isLoggedIn,
  parseSearchAgentArgs,
  resolveConfiguredStrategies,
  resolveProfileSelectors,
  runSearchAgent,
  splitCsv,
  typeSearchByXpath,
} = require("../browser/search-agent");

test("parseSearchAgentArgs reads profile list, xpath fields, query, and close flag", () => {
  assert.deepEqual(
    parseSearchAgentArgs([
      "node",
      "browser/search-agent.js",
      "--profile-nos",
      "1,2",
      "--url",
      "https://example.com",
      "--login-check-xpath",
      "//button[@data-user]",
      "--search-xpath",
      "//input[@name='q']",
      "--query",
      "hello",
      "--strategy",
      "curious_scanner",
      "--no-close",
    ]),
    {
      profileNos: "1,2",
      profileIds: "",
      url: "https://example.com",
      loginCheckXpath: "//button[@data-user]",
      searchXpath: "//input[@name='q']",
      query: "hello",
      strategy: "curious_scanner",
      closeAfterRun: false,
    },
  );
});

test("splitCsv trims empty entries", () => {
  assert.deepEqual(splitCsv(" 1, ,2 ,, 3 "), ["1", "2", "3"]);
});

test("resolveConfiguredStrategies prefers saved execution strategy settings", () => {
  const strategies = resolveConfiguredStrategies({
    execution_strategies: {
      items: [
        {
          id: "saved_strategy",
          label: "Saved strategy",
          mouseMoves: 4,
          clicks: 1,
          scrolls: 2,
          moveSteps: [10, 20],
          pauseMs: [100, 300],
          scrollDelta: [200, 500],
          text_prompt: "inspect the visible page",
        },
      ],
    },
  });

  assert.equal(strategies[0].id, "saved_strategy");
  assert.equal(strategies[0].textPrompt, "inspect the visible page");
});

test("resolveProfileSelectors uses explicit profile numbers or all profiles", async () => {
  assert.deepEqual(
    await resolveProfileSelectors({}, { profileNos: "1,2", profileIds: "" }),
    [{ profile_no: "1" }, { profile_no: "2" }],
  );

  const adapter = {
    listProfiles: async () => [
      { profile_id: "abc", profile_no: "1" },
      { profile_id: "", profile_no: "2" },
    ],
  };
  assert.deepEqual(
    await resolveProfileSelectors(adapter, { profileNos: "", profileIds: "" }),
    [{ profile_id: "abc" }, { profile_no: "2" }],
  );
});

test("isLoggedIn checks xpath when provided and falls back to URL", async () => {
  const loggedInPage = {
    url: () => "https://example.com/home",
    locator: () => ({ count: async () => 1 }),
  };
  const loginPage = {
    url: () => "https://example.com/login",
    locator: () => ({ count: async () => 0 }),
  };

  assert.equal(await isLoggedIn(loggedInPage, "//button"), true);
  assert.equal(await isLoggedIn(loginPage, "//button"), false);
  assert.equal(await isLoggedIn(loggedInPage, ""), true);
  assert.equal(await isLoggedIn(loginPage, ""), false);
});

test("typeSearchByXpath clears the target, types query, and presses Enter", async () => {
  const calls = [];
  const locator = {
    click: async () => calls.push(["click"]),
    fill: async (value) => calls.push(["fill", value]),
  };
  const page = {
    locator: (selector) => {
      calls.push(["locator", selector]);
      return { first: () => locator };
    },
    keyboard: {
      type: async (char) => calls.push(["type", char]),
      press: async (key) => calls.push(["press", key]),
    },
  };

  await typeSearchByXpath(page, "//input", "ab", {
    humanType: async (targetPage, text) => {
      for (const char of text) {
        await targetPage.keyboard.type(char);
      }
    },
  });

  assert.deepEqual(calls, [
    ["locator", "xpath=//input"],
    ["click"],
    ["fill", ""],
    ["type", "a"],
    ["type", "b"],
    ["press", "Enter"],
  ]);
});

test("runSearchAgent opens each selected profile, searches logged-in pages, and closes", async () => {
  const calls = [];
  const page = {
    goto: async (url) => calls.push(["goto", url]),
    url: () => "https://example.com/home",
    locator: (selector) => {
      calls.push(["locator", selector]);
      return {
        count: async () => 1,
        first: () => ({
          click: async () => calls.push(["search-click"]),
          fill: async (value) => calls.push(["search-fill", value]),
        }),
      };
    },
    keyboard: {
      type: async (char) => calls.push(["type", char]),
      press: async (key) => calls.push(["press", key]),
    },
  };
  const browser = { contexts: () => [{ pages: () => [page] }] };
  const adapter = {
    openProfile: async (selector) => {
      calls.push(["open", selector]);
      return { debug_port: "9222" };
    },
    closeProfile: async (selector) => calls.push(["close", selector]),
  };

  const result = await runSearchAgent({
    args: {
      profileNos: "1",
      profileIds: "",
      url: "https://example.com",
      loginCheckXpath: "//button[@data-user]",
      searchXpath: "//input[@name='q']",
      query: "hi",
      strategy: "steady_reader",
      closeAfterRun: true,
    },
    adapter,
    chromium: {
      connectOverCDP: async (cdpUrl) => {
        calls.push(["connect", cdpUrl]);
        return browser;
      },
    },
    strategies: [{
      id: "steady_reader",
      mouseMoves: 0,
      scrolls: 0,
      moveSteps: [1, 1],
      pauseMs: [1, 1],
      scrollDelta: [1, 1],
    }],
    actions: {
      applyMouseStrategy: async (_page, strategy) => calls.push(["strategy", strategy.id]),
      humanType: async (targetPage, text) => {
        for (const char of text) {
          await targetPage.keyboard.type(char);
        }
      },
    },
    logger: { log: () => {} },
  });

  assert.deepEqual(result, {
    total: 1,
    results: [{
      selector: { profile_no: "1" },
      status: "searched",
      strategy: "steady_reader",
    }],
  });
  assert.deepEqual(calls, [
    ["open", { profile_no: "1" }],
    ["connect", "http://127.0.0.1:9222"],
    ["goto", "https://example.com"],
    ["locator", "xpath=//button[@data-user]"],
    ["strategy", "steady_reader"],
    ["locator", "xpath=//input[@name='q']"],
    ["search-click"],
    ["search-fill", ""],
    ["type", "h"],
    ["type", "i"],
    ["press", "Enter"],
    ["close", { profile_no: "1" }],
  ]);
});
