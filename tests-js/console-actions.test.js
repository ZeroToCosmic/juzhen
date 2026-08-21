"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const ui = require("../gateway/static/console_actions.js");

test("action types normalize into one equal-level record contract", () => {
  const strategy = ui.normalizeStrategy({id: "s1", name: "Feed", enabled: true, actions: [{}, {}], updated_at: "2026-08-19T10:00:00Z"});
  const campaign = ui.normalizeCampaign({id: "c1", name: "Thread", status: "paused", assignment_count: 9});
  assert.deepEqual({type: strategy.type, size: strategy.size, status: strategy.status}, {type: "strategy", size: 2, status: "enabled"});
  assert.deepEqual({type: campaign.type, size: campaign.size, status: campaign.status}, {type: "campaign", size: 9, status: "paused"});
  assert.equal(ui.statusGroup("paused"), "attention");
});

test("action filters combine type status and search", () => {
  const items = [ui.normalizeStrategy({id: "s1", name: "Feed", enabled: true}), ui.normalizeCampaign({id: "c1", name: "Thread", status: "failed"})];
  assert.deepEqual(ui.filterActions(items, {query: "thread", type: "campaign", status: "attention"}).map((item) => item.id), ["c1"]);
  assert.deepEqual(ui.filterActions(items, {query: "", type: "strategy", status: "active"}).map((item) => item.id), ["s1"]);
});

test("strategy editor URLs encode identifiers and empty strategies are not maintainable", () => {
  assert.equal(
    ui.strategyEditorUrl("策略 1 主策略"),
    "/console/actions/browser-strategies/%E7%AD%96%E7%95%A5%201%20%E4%B8%BB%E7%AD%96%E7%95%A5/edit",
  );
  assert.equal(ui.strategyEditorUrl(""), "");
  assert.equal(ui.normalizeStrategy({id: ""}).href, "");
});

test("campaign maintenance URL remains unchanged", () => {
  assert.equal(ui.normalizeCampaign({id: "c1"}).href, "/comment-campaigns");
});
