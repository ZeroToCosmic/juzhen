"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const ui = require("../gateway/static/console_accounts_windows.js");

test("proxy credentials never appear in the account table projection", () => {
  assert.equal(ui.proxyDisplay("203.0.113.8:9000:user:secret"), "203.0.113.8:9000");
  assert.equal(ui.proxyDisplay(""), "未分配");
});

test("selected windows preserve only the open-and-tile identifiers", () => {
  const windows = [{profile_id: "p1", profile_no: "1", name: "One", username: "secret-user"}, {profile_id: "p2", profile_no: "2", name: "Two"}];
  assert.deepEqual(ui.selectedWindows(windows, ["p2"]), [{profile_id: "p2", profile_no: "2"}]);
});

test("account search includes profile and sync errors", () => {
  const accounts = [{id: "1", account_name: "Alpha", buffer_profile_ids: ["profile-x"]}, {id: "2", account_name: "Beta", last_channel_sync_error: "timeout"}];
  assert.equal(ui.searchAccounts(accounts, "profile-x")[0].id, "1");
  assert.equal(ui.searchAccounts(accounts, "timeout")[0].id, "2");
});
