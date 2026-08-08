const assert = require("node:assert/strict");
const test = require("node:test");

const {createCommentCampaignUI, safeEvidencePath} = require("../gateway/static/comment_campaign");

function response(status, data) {
  return {status, data: data || {}};
}

function harness(overrides = {}) {
  const requests = [];
  const scheduled = [];
  const dependencies = {
    requestJson: async (url, method, body) => {
      requests.push({url, method, body});
      const item = overrides.responses?.[`${method} ${url}`];
      return typeof item === "function" ? item(body) : (item || response(200, {data: []}));
    },
    setTimeout: (fn, delay) => {
      const token = {fn, delay};
      scheduled.push(token);
      return token;
    },
    clearTimeout: () => {},
  };
  return {ui: createCommentCampaignUI(dependencies), requests, scheduled};
}

test("approval sends the exact assignment revision once", async () => {
  const {ui, requests} = harness({responses: {
    "POST /api/browser-v2/comment-campaigns/c1/assignments/a1/approve-submit": response(202, {data: {}}),
    "GET /api/browser-v2/comment-campaigns/c1": response(200, {data: {campaign: {id: "c1"}, assignments: []}}),
  }});
  ui.state.selectedCampaignId = "c1";

  assert.equal(await ui.approveSubmit("a1", 4), true);
  assert.equal(await ui.approveSubmit("a1", 4), false);
  assert.deepEqual(requests.filter((item) => item.url.includes("approve-submit")), [{
    method: "POST",
    url: "/api/browser-v2/comment-campaigns/c1/assignments/a1/approve-submit",
    body: {expected_revision: 4},
  }]);
});

test("one decision revision makes approve, reject, and resolve mutually exclusive", async () => {
  const {ui, requests} = harness({responses: {
    "POST /api/browser-v2/comment-campaigns/c1/assignments/a1/approve-submit": response(202, {data: {}}),
    "GET /api/browser-v2/comment-campaigns/c1": response(200, {data: {campaign: {id: "c1"}, assignments: []}}),
  }});
  ui.state.selectedCampaignId = "c1";

  assert.equal(await ui.approveSubmit("a1", 4), true);
  assert.equal(await ui.rejectSubmit("a1", 4, "no"), false);
  assert.equal(await ui.resolveUnverified("a1", 4, "published", "checked"), false);
  assert.equal(requests.filter((item) => item.method === "POST").length, 1);
});

test("polling updates snapshots without overwriting the campaign draft", async () => {
  const {ui, scheduled} = harness({responses: {
    "GET /api/browser-v2/comment-campaigns": response(200, {data: [{id: "c1", status: "running"}]}),
  }});
  ui.state.draftCampaign = {name: "Summer thread", video_url: "https://example.test/video"};

  await ui.poll();

  assert.deepEqual(ui.state.campaigns, [{id: "c1", status: "running"}]);
  assert.equal(ui.state.draftCampaign.name, "Summer thread");
  ui.schedulePoll();
  assert.equal(scheduled[0].delay, 5000);
});

test("a late Campaign detail response cannot overwrite a newer selection", async () => {
  let releaseFirst;
  const first = new Promise((resolve) => { releaseFirst = resolve; });
  const ui = createCommentCampaignUI({
    requestJson: async (url) => {
      if (url.includes("/c1")) await first;
      const campaignId = url.includes("/c2") ? "c2" : "c1";
      if (url.endsWith("/receipts") || url.endsWith("/attempts")) return response(200, {data: []});
      return response(200, {data: {campaign: {id: campaignId}, assignments: []}});
    },
  });

  const stale = ui.selectCampaign("c1");
  await ui.selectCampaign("c2");
  releaseFirst();
  assert.equal(await stale, false);
  assert.equal(ui.state.selectedDetail.campaign.id, "c2");
});

test("client rejects non-Campaign API paths", () => {
  const {ui} = harness();
  assert.throws(() => ui.apiPath("/api/browser/jobs"), /comment-/);
});

test("settings save carries only the four IDs and exact revision", async () => {
  const {ui, requests} = harness({responses: {
    "PUT /api/browser-v2/comment-settings": response(200, {data: {revision: 8, can_write: true, element_bindings: {entry_element_id: "entry", input_element_id: "input", submit_element_id: "submit", account_element_id: "account"}}}),
  }});
  ui.state.settings = {can_write: true};
  ui.state.draftSettings = {revision: 7, entry_element_id: "entry", input_element_id: "input", submit_element_id: "submit", account_element_id: "account"};

  assert.equal(await ui.saveSettings(), true);
  assert.deepEqual(requests.filter((item) => item.method === "PUT"), [{
    method: "PUT", url: "/api/browser-v2/comment-settings", body: {
      expected_revision: 7, entry_element_id: "entry", input_element_id: "input", submit_element_id: "submit", account_element_id: "account",
    },
  }]);
});

test("settings save error leaves its draft untouched", async () => {
  const {ui} = harness({responses: {"PUT /api/browser-v2/comment-settings": response(409, {error: {code: "revision_conflict", message: "数据已变更"}})}});
  ui.state.settings = {can_write: true};
  ui.state.draftSettings = {revision: 7, entry_element_id: "entry", input_element_id: "input", submit_element_id: "submit", account_element_id: "account"};

  assert.equal(await ui.saveSettings(), false);
  assert.equal(ui.state.draftSettings.revision, 7);
});

test("campaign creation sends the strict manual Campaign shape", async () => {
  const {ui, requests} = harness({responses: {
    "POST /api/browser-v2/comment-campaigns": response(201, {data: {id: "c1"}}),
    "GET /api/browser-v2/comment-campaigns/c1": response(200, {data: {campaign: {id: "c1"}, assignments: []}}),
    "GET /api/browser-v2/comment-campaigns/c1/receipts": response(200, {data: []}),
    "GET /api/browser-v2/comment-campaigns/c1/attempts": response(200, {data: []}),
  }});
  ui.state.draftCampaign = {name: "Campaign", mode: "threaded", target_reference: "https://www.tiktok.com/@a/video/12345678", template_id: "template", profile_refs: "profile_ref_a, profile_ref_b", batch_size: "3"};

  assert.equal(await ui.createCampaign(), true);
  assert.deepEqual(requests[0], {method: "POST", url: "/api/browser-v2/comment-campaigns", body: {
    name: "Campaign", mode: "threaded", target_source: "manual_url", target_reference: "https://www.tiktok.com/@a/video/12345678", template_id: "template", profile_refs: ["profile_ref_a", "profile_ref_b"], batch_size: 3, start_mode: "manual",
  }});
});

test("template draft supports multiple ordered threaded steps", async () => {
  const {ui, requests} = harness({responses: {"POST /api/browser-v2/comment-templates": response(201, {data: {id: "t1"}})}});
  ui.state.draftTemplate = ui.newTemplateDraft();
  ui.state.draftTemplate = {...ui.state.draftTemplate, name: "Thread", mode: "threaded"};
  ui.changeTemplateStep(0, "id", "root"); ui.changeTemplateStep(0, "label", "Root"); ui.changeTemplateStep(0, "fixed_text", "one");
  ui.addTemplateStep(); ui.changeTemplateStep(1, "id", "reply"); ui.changeTemplateStep(1, "label", "Reply"); ui.changeTemplateStep(1, "fixed_text", "two"); ui.changeTemplateStep(1, "parent_step_id", "root");

  assert.equal(await ui.saveTemplate(), true);
  assert.deepEqual(requests[0].body.steps.map((step) => [step.id, step.parent_step_id, step.fixed_text]), [["root", null, "one"], ["reply", "root", "two"]]);
});

test("Profile metadata save sends every editable field", async () => {
  const {ui, requests} = harness({responses: {"POST /api/browser-v2/comment-profile-metadata": response(200, {data: {}})}});
  const profile = {profile_ref: "profile_ref_a"};

  assert.equal(await ui.saveProfileMetadata(profile, {expected_username: "alice", tags: "en, us", language: "en", region: "US", enabled: true, login_verified: true, health_status: "healthy", cooldown_until: ""}), true);
  assert.deepEqual(requests[0], {method: "POST", url: "/api/browser-v2/comment-profile-metadata", body: {profile_ref: "profile_ref_a", expected_username: "alice", enabled: true, login_verified: true, tags: ["en", "us"], language: "en", region: "US", cooldown_until: null, health_status: "healthy"}});
});

test("evidence uses only the dedicated UUID PNG route", () => {
  assert.equal(safeEvidencePath("evidence/0123456789abcdef0123456789abcdef.png"), "/comment-campaign-evidence/0123456789abcdef0123456789abcdef.png");
  ["/evidence/a.png", "evidence/../secret.png", "https://example.test/a.png", "evidence/ABC.png"].forEach((value) => {
    assert.equal(safeEvidencePath(value), "");
  });
});

test("workbench source keeps Chinese copy intact and avoids raw Profile vocabulary", () => {
  const fs = require("node:fs");
  const source = fs.readFileSync("gateway/static/comment_campaign.js", "utf8");
  assert.match(source, /确认提交/);
  assert.equal(source.includes("\ufffd"), false);
  assert.equal(source.includes("raw_adspower_id"), false);
  assert.equal(source.includes("localStorage"), false);
  assert.equal(source.includes("pageEvidence.account?.username || assignment.expected_username"), false);
  assert.match(source, /无现场账号证据/);
  assert.match(source, /无现场输入文本证据/);
  assert.match(source, /reject\.disabled/);
  const html = fs.readFileSync("gateway/templates/comment_campaign.html", "utf8");
  assert.equal((html.match(/<dialog/g) || []).length, 1);
  assert.match(html, /management_fetch\.js[\s\S]*comment_campaign\.js/);
});
