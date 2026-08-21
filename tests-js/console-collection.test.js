"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const ui = require("../gateway/static/console_collection.js");

test("collection status formatting is stable", () => {
  assert.equal(ui.runningText(true), "运行中");
  assert.equal(ui.runningText(false), "未运行");
  assert.equal(ui.statusText("running"), "运行中");
  assert.equal(ui.sourceText("manual"), "本机手动");
  assert.equal(ui.timeText(null), "—");
  assert.equal(ui.timeText("2026-08-19T10:20:30Z"), "2026-08-19 10:20");
});

test("collection page only uses operational collection APIs", () => {
  const source = fs.readFileSync(path.join(__dirname, "../gateway/static/console_collection.js"), "utf8");
  assert.match(source, /\/api\/tiktok-stats\/status/);
  assert.match(source, /\/api\/tiktok-stats\/accounts/);
  assert.match(source, /\/api\/tiktok-stats\/runs/);
  assert.doesNotMatch(source, /\/api\/tiktok-stats\/videos/);
  assert.doesNotMatch(source, /quality|batch_id|关联批次|数据质量/);
});
