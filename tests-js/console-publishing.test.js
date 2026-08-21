"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const ui = require("../gateway/static/console_publishing.js");

test("batch payload keeps hundreds of selected accounts and scheduled time", () => {
  const ids = Array.from({length: 300}, (_, index) => `account-${index + 1}`);
  assert.deepEqual(ui.buildBatchPayload({date: "2026-08-20", time: "09:30", brand_id: "brand-1"}, ids), {
    brand_id: "brand-1", scheduled_at: "2026-08-20T09:30:00+08:00", account_ids: ids,
  });
});

test("result filters retain failed rows matching account or error", () => {
  const rows = [{id: "1", status: "success", account_name: "Alpha"}, {id: "2", status: "failed", account_name: "Beta", error: "Buffer timeout"}];
  assert.deepEqual(ui.filterResults(rows, {status: "failed", query: "buffer"}).map((item) => item.id), ["2"]);
});

test("manual publishing is explicitly a real Buffer operation", () => {
  const template = fs.readFileSync(path.join(__dirname, "../gateway/templates/console_publishing.html"), "utf8");
  assert.match(template, /真实发布任务并调用 Buffer/);
  assert.match(template, /面向每日数百条任务/);
  assert.match(template, /尚未接入自动调度/);
});

test("TikTok result links reject unsafe protocols and lookalike domains", () => {
  assert.equal(ui.safeTikTokUrl("https://www.tiktok.com/@a/video/1"), "https://www.tiktok.com/@a/video/1");
  assert.equal(ui.safeTikTokUrl("javascript:alert(1)"), "");
  assert.equal(ui.safeTikTokUrl("https://tiktok.com.example.test/video/1"), "");
});
