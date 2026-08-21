"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const ui = require("../gateway/static/console_receipts.js");

test("three receipt sources normalize into one record shape", () => {
  const rows = [
    ui.normalizeBrowser({id: "b1", status: "completed", strategy_id: "s1", summary: {total: 2, succeeded: 2}}),
    ui.normalizeCampaign({id: "c1", status: "running", name: "Thread", assignment_count: 3}),
    ui.normalizePublish({id: "p1", status: "failed", account_id: "a1", error: "timeout"}),
  ];
  assert.deepEqual(rows.map((item) => item.source), ["browser", "campaign", "publishing"]);
  assert.deepEqual(rows.map((item) => ui.statusGroup(item.status)), ["success", "pending", "failed"]);
});

test("evidence links accept only 32-hex png paths", () => {
  const valid = "evidence/0123456789abcdef0123456789abcdef.png";
  assert.equal(ui.safeEvidencePath(valid, "browser"), "/evidence/0123456789abcdef0123456789abcdef.png");
  assert.equal(ui.safeEvidencePath(valid, "campaign"), "/comment-campaign-evidence/0123456789abcdef0123456789abcdef.png");
  assert.equal(ui.safeEvidencePath("../../secret.png", "browser"), "");
  assert.equal(ui.safeEvidencePath("evidence/not-hex.png", "campaign"), "");
});

test("receipt filters combine source status and search", () => {
  const rows = [ui.normalizeCampaign({id: "c1", name: "Thread", status: "failed"}), ui.normalizePublish({id: "p1", account_name: "Alpha", status: "success"})];
  assert.deepEqual(ui.filterRecords(rows, {source: "campaign", status: "failed", query: "thread"}).map((item) => item.id), ["c1"]);
});
