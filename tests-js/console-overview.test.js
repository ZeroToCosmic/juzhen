"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const ui = require("../gateway/static/console_overview.js");

test("local runtime keeps fulfilled states when one dependency fails", () => {
  const result = ui.summarizeLocalRuntime([
    {status: "fulfilled", value: {ok: true}},
    {status: "fulfilled", value: {data: [{profile_token: "p1"}, {profile_token: "p2"}]}},
    {status: "rejected", reason: new Error("offline")},
  ], "2026-08-19 15:30");

  assert.equal(result.gateway.value, "可达");
  assert.equal(result.profiles.value, "2 个");
  assert.equal(result.scraper.value, "不可用");
  assert.equal(result.worker.value, "不可用");
  assert.equal(result.failed, 1);
  assert.equal(result.updatedAt, "2026-08-19 15:30");
});

test("scraper and worker are rendered independently", () => {
  const result = ui.summarizeLocalRuntime([
    {status: "fulfilled", value: {}},
    {status: "fulfilled", value: {data: []}},
    {status: "fulfilled", value: {scraper: {running: true}, worker: {running: false}}},
  ], "now");

  assert.equal(result.scraper.value, "运行中");
  assert.equal(result.worker.value, "未运行");
});
