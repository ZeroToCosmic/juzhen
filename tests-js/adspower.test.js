const assert = require("node:assert/strict");
const test = require("node:test");

const {
  buildStartOptions,
  createAdsPowerAdapter,
  normalizeProfile,
  summarizeProfiles,
} = require("../browser/adspower");

test("normalizeProfile keeps non-sensitive profile fields", () => {
  const profile = normalizeProfile({
    profile_id: "abc123",
    profile_no: "7",
    name: "tiktok-7",
    group_id: "group-a",
    group_name: "TikTok",
    platform: "tiktok.com",
    username: "person@example.com",
    password: "secret",
    user_proxy_config: { proxy_password: "secret" },
  });

  assert.deepEqual(profile, {
    profile_id: "abc123",
    profile_no: "7",
    name: "tiktok-7",
    group_id: "group-a",
    group_name: "TikTok",
    platform: "tiktok.com",
    username: "person@example.com",
  });
});

test("summarizeProfiles counts total, opened, and groups", () => {
  const summary = summarizeProfiles(
    [
      { profile_id: "one", group_name: "TikTok" },
      { profile_id: "two", group_name: "TikTok" },
      { profile_id: "three", group_name: "" },
    ],
    [{ user_id: "two" }],
  );

  assert.deepEqual(summary, {
    total: 3,
    opened: 1,
    byGroup: {
      TikTok: 2,
      Ungrouped: 1,
    },
  });
});

test("buildStartOptions requires profile id or profile number and defaults visible window", () => {
  assert.deepEqual(buildStartOptions({ profile_no: "7" }), {
    profile_no: "7",
    headless: "0",
    last_opened_tabs: "0",
  });

  assert.throws(() => buildStartOptions({}), /profile_id or profile_no is required/);
});

test("createAdsPowerAdapter delegates list, open, and close calls", async () => {
  const calls = [];
  const client = {
    getBrowserList: async (options) => {
      calls.push(["list", options]);
      return { list: [{ profile_id: "abc123", group_name: "TikTok" }] };
    },
    getOpenedBrowser: async () => {
      calls.push(["opened"]);
      return { list: [{ user_id: "abc123" }] };
    },
    openBrowser: async (options) => {
      calls.push(["open", options]);
      return { ws: { puppeteer: "ws://127.0.0.1/browser" }, debug_port: "9222" };
    },
    closeBrowser: async (options) => {
      calls.push(["close", options]);
      return { ok: true };
    },
  };
  const adapter = createAdsPowerAdapter(client);

  assert.deepEqual(await adapter.listProfiles({ limit: 5 }), [
    {
      profile_id: "abc123",
      profile_no: "",
      name: "",
      group_id: "",
      group_name: "TikTok",
      platform: "",
      username: "",
    },
  ]);
  assert.deepEqual(await adapter.listOpened(), [{ user_id: "abc123" }]);
  assert.deepEqual(await adapter.summarize(), {
    total: 1,
    opened: 1,
    byGroup: { TikTok: 1 },
  });
  assert.equal((await adapter.openProfile({ profile_id: "abc123" })).debug_port, "9222");
  assert.deepEqual(await adapter.closeProfile({ profile_id: "abc123" }), { ok: true });

  assert.deepEqual(calls, [
    ["list", { limit: 5 }],
    ["opened"],
    ["list", { limit: 200, page: 1 }],
    ["opened"],
    ["open", { profile_id: "abc123", headless: "0", last_opened_tabs: "0" }],
    ["close", { profile_id: "abc123" }],
  ]);
});
