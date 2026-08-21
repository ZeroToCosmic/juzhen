"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const ui = require("../gateway/static/console_collection_results.js");

test("results formatter preserves missing values and formats real zero", () => {
  assert.equal(ui.formatNumber(null), "—");
  assert.equal(ui.formatNumber(undefined), "—");
  assert.equal(ui.formatNumber(0), "0");
  assert.equal(ui.formatNumber(128430), "128,430");
});

test("video query defaults to latest collection descending", () => {
  const query = new URLSearchParams(ui.buildVideoQuery({query: "morning", page: 2, page_size: 50}));
  assert.equal(query.get("query"), "morning");
  assert.equal(query.get("sort"), "last_collected_at");
  assert.equal(query.get("direction"), "desc");
  assert.equal(query.get("page"), "2");
  assert.equal(query.get("page_size"), "50");
});

test("results template keeps the approved logical field order", () => {
  const template = fs.readFileSync(path.join(__dirname, "../gateway/templates/console_collection_results.html"), "utf8");
  const header = template.slice(template.indexOf("<thead>"), template.indexOf("</thead>"));
  const labels = ["视频信息", "账号", "发布时间", "播放", "点赞", "评论", "最近采集"];
  const positions = labels.map((label) => header.indexOf(label));
  assert.equal(positions.every((position) => position >= 0), true);
  assert.deepEqual(positions, [...positions].sort((a, b) => a - b));
  assert.doesNotMatch(template, /数据质量|关联批次|同步状态/);
});
