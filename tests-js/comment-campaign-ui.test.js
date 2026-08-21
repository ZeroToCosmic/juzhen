const assert = require("node:assert/strict");
const test = require("node:test");

const {createCommentCampaignUI, safeEvidencePath, importErrorMessage} = require("../gateway/static/comment_campaign");
const editor = require("../gateway/static/comment_tree_editor");

function response(status, data) {
  return {status, data: data || {}};
}

function harness(overrides = {}) {
  const requests = [];
  const scheduled = [];
  const dependencies = {
    document: overrides.document,
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

function fakeElement(tag) {
  let ownText = "";
  return {
    tagName: tag, children: [], dataset: {}, style: {}, listeners: {}, attributes: {}, value: "", checked: false, disabled: false,
    get textContent() { return ownText + this.children.map((child) => child.textContent || "").join(""); },
    set textContent(value) { ownText = String(value || ""); this.children = []; },
    append(...items) { this.children.push(...items); },
    replaceChildren(...items) { this.children = items; ownText = ""; },
    addEventListener(type, handler) { this.listeners[type] = handler; },
    setAttribute(name, value) { this.attributes[name] = String(value); }, removeAttribute(name) { delete this.attributes[name]; }, showModal() { this.open = true; }, close() { this.open = false; },
  };
}

function fakeDrawerDocument() {
  const nodes = {"#campaign-drawer": fakeElement("dialog"), "#campaign-drawer-title": fakeElement("h2"), "#campaign-drawer-body": fakeElement("div")};
  return {querySelector: (selector) => nodes[selector] || null, querySelectorAll: () => [], createElement: fakeElement, nodes};
}

function walkFakeTree(node) {
  return [node].concat(...(node.children || []).flatMap(walkFakeTree));
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

test("automatic Profile preview keeps opaque refs out of the drawer and sends them only in the request", async () => {
  const document = fakeDrawerDocument();
  const {ui, requests} = harness({document, responses: {
    "POST /api/browser-v2/comment-profile-selection/preview": response(200, {data: {required_count: 2, eligible_count: 3, profiles: [
      {profile_ref: "profile_ref_a", display_profile: "窗口 A"}, {profile_ref: "profile_ref_b", display_profile: "窗口 B"},
    ]}}),
    "POST /api/browser-v2/comment-campaigns": response(201, {data: {id: "c1"}}),
    "GET /api/browser-v2/comment-campaigns/c1": response(200, {data: {campaign: {id: "c1"}, assignments: []}}),
    "GET /api/browser-v2/comment-campaigns/c1/receipts": response(200, {data: []}),
    "GET /api/browser-v2/comment-campaigns/c1/attempts": response(200, {data: []}),
  }});
  ui.state.templates = [{id: "template_opaque", name: "安全评论树", revision: 7, enabled: true, step_count: 2}];
  ui.state.draftCampaign = {name: "Campaign", mode: "threaded", target_reference: "https://www.tiktok.com/@a/video/12345678", template_id: "template_opaque", selection_mode: "automatic", profile_refs: [], batch_size: "3"};

  assert.equal(await ui.refreshSelectionPreview(), true);
  ui.openDrawer("create");
  assert.equal(document.nodes["#campaign-drawer-body"].textContent.includes("profile_ref_"), false);
  assert.equal(await ui.createCampaign(), true);
  assert.deepEqual(requests.find((item) => item.url === "/api/browser-v2/comment-campaigns").body.profile_refs, ["profile_ref_a", "profile_ref_b"]);
});

test("campaign creation sends a strict manual candidate pool without parsing comma-separated refs", async () => {
  const {ui, requests} = harness({responses: {
    "POST /api/browser-v2/comment-campaigns": response(201, {data: {id: "c1"}}),
    "GET /api/browser-v2/comment-campaigns/c1": response(200, {data: {campaign: {id: "c1"}, assignments: []}}),
    "GET /api/browser-v2/comment-campaigns/c1/receipts": response(200, {data: []}),
    "GET /api/browser-v2/comment-campaigns/c1/attempts": response(200, {data: []}),
  }});
  ui.state.templates = [{id: "template", name: "评论树", revision: 1, enabled: true, step_count: 1}];
  ui.state.draftCampaign = {name: "Campaign", mode: "threaded", target_reference: "https://www.tiktok.com/@a/video/12345678", template_id: "template", selection_mode: "manual", profile_refs: ["profile_ref_a", "profile_ref_b"], batch_size: "3"};

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

test("Profile metadata POST preserves server legacy identity fields without exposing editors", async () => {
  const {ui, requests} = harness({responses: {"POST /api/browser-v2/comment-profile-metadata": response(200, {data: {}})}});
  const profile = {profile_ref: "profile_ref_a", expected_username: "server-user", login_verified: true};

  assert.equal(await ui.saveProfileMetadata(profile, {expected_username: "edited-but-ignored", tags: "en, us", language: "en", region: "US", enabled: true, login_verified: false, health_status: "healthy", cooldown_until: ""}), true);
  assert.deepEqual(requests[0], {method: "POST", url: "/api/browser-v2/comment-profile-metadata", body: {profile_ref: "profile_ref_a", expected_username: "server-user", enabled: true, login_verified: true, tags: ["en", "us"], language: "en", region: "US", cooldown_until: null, health_status: "healthy"}});
});

test("profile cache state is separate from the draft, starts with one explicit sync, and polling only reads", async () => {
  const {ui, requests} = harness({responses: {
    "GET /api/browser-v2/comment-campaigns": response(200, {data: []}),
    "GET /api/browser-v2/comment-templates": response(200, {data: []}),
    "GET /api/browser-v2/comment-profile-metadata": response(200, {data: [{display_profile: "缓存窗口"}], meta: {stale: true, last_synced_at: "2026-08-11T00:00:00Z", safe_reason: "timeout"}}),
    "POST /api/browser-v2/comment-profile-metadata/sync": response(200, {data: [{display_profile: "同步窗口"}], meta: {stale: false, last_synced_at: "2026-08-11T00:01:00Z", safe_reason: null}}),
    "GET /api/browser-v2/comment-campaign-health": response(200, {data: {}}),
    "GET /api/browser-v2/comment-settings": response(200, {data: {}}),
  }});
  ui.state.draftCampaign = {selection_mode: "manual", profile_refs: ["hidden-ref"]};

  await ui.init();
  assert.equal(ui.state.profiles[0].display_profile, "同步窗口");
  assert.equal(ui.state.profileMeta.stale, false);
  assert.deepEqual(ui.state.draftCampaign.profile_refs, ["hidden-ref"]);
  assert.equal(requests.filter((item) => item.url.endsWith("/comment-profile-metadata/sync")).length, 1);
  await ui.poll();
  assert.equal(requests.filter((item) => item.url.endsWith("/comment-profile-metadata/sync")).length, 1);
});

test("manual Profile cards are keyboard-native and never render internal or legacy identity fields", () => {
  const document = fakeDrawerDocument();
  const {ui} = harness({document});
  ui.state.profiles = [{profile_ref: "profile_ref_DO_NOT_RENDER", display_profile: "<img src=x>", expected_username: "account-key", login_verified: true, raw_id: "raw-id", enabled: true, health_status: "healthy"}];
  ui.state.templates = [{id: "template", name: "评论树", revision: 1, enabled: true, step_count: 3}];
  ui.state.draftCampaign = {template_id: "template", mode: "independent", selection_mode: "manual", profile_refs: []};

  ui.openDrawer("create");
  const body = document.nodes["#campaign-drawer-body"];
  const checkbox = walkFakeTree(body).find((item) => item.tagName === "input" && item.type === "checkbox");
  assert.ok(checkbox);
  assert.equal(typeof checkbox.listeners.change, "function");
  assert.equal(body.textContent.includes("profile_ref_DO_NOT_RENDER"), false);
  assert.equal(body.textContent.includes("account-key"), false);
  assert.equal(body.textContent.includes("raw-id"), false);
  assert.equal(body.textContent.includes("<img src=x>"), true);
});

test("manual shortage retains its local draft but prevents create, plan, and lock", async () => {
  const {ui, requests} = harness();
  ui.state.templates = [{id: "template", name: "评论树", revision: 1, enabled: true, step_count: 3}];
  ui.state.draftCampaign = {name: "草稿", target_reference: "https://example.test/video", template_id: "template", mode: "independent", selection_mode: "manual", profile_refs: ["hidden"]};

  assert.equal(await ui.createCampaign(), false);
  assert.equal(ui.planCampaign(), false);
  assert.equal(ui.lockPlan(), false);
  assert.deepEqual(ui.state.draftCampaign.profile_refs, ["hidden"]);
  assert.equal(requests.filter((item) => item.method === "POST").length, 0);
});

test("campaign conflict and polling retain the manual Profile candidate draft", async () => {
  const {ui} = harness({responses: {
    "POST /api/browser-v2/comment-campaigns": response(409, {error: {code: "revision_conflict", message: "版本冲突"}}),
    "GET /api/browser-v2/comment-campaigns": response(200, {data: []}),
    "GET /api/browser-v2/comment-templates": response(200, {data: []}),
    "GET /api/browser-v2/comment-profile-metadata": response(200, {data: [], meta: {stale: false}}),
    "GET /api/browser-v2/comment-campaign-health": response(200, {data: {}}),
    "GET /api/browser-v2/comment-settings": response(200, {data: {}}),
  }});
  ui.state.templates = [{id: "template", name: "评论树", revision: 1, enabled: true, step_count: 1}];
  ui.state.draftCampaign = {name: "草稿", target_reference: "https://example.test/video", template_id: "template", mode: "independent", selection_mode: "manual", profile_refs: ["hidden"]};

  assert.equal(await ui.createCampaign(), false);
  await ui.poll();
  assert.deepEqual(ui.state.draftCampaign.profile_refs, ["hidden"]);
  assert.equal(ui.state.draftCampaign.selection_mode, "manual");
});

test("automatic preview ignores an old response after the template or mode changes", async () => {
  let releaseOld;
  const old = new Promise((resolve) => { releaseOld = resolve; });
  const {ui} = harness({responses: {
    "POST /api/browser-v2/comment-profile-selection/preview": (body) => body.template_id === "old" ? old : response(200, {data: {required_count: 1, eligible_count: 2, profiles: [{profile_ref: "new-ref", display_profile: "新窗口"}]}}),
  }});
  ui.state.templates = [{id: "old", name: "旧树", revision: 1, enabled: true, step_count: 1}, {id: "new", name: "新树", revision: 2, enabled: true, step_count: 1}];
  ui.state.draftCampaign = {template_id: "old", mode: "independent", selection_mode: "automatic", profile_refs: []};
  const oldRequest = ui.refreshSelectionPreview();
  ui.state.draftCampaign = {...ui.state.draftCampaign, template_id: "new", mode: "threaded"};
  assert.equal(await ui.refreshSelectionPreview(), true);
  releaseOld(response(200, {data: {required_count: 1, eligible_count: 1, profiles: [{profile_ref: "old-ref", display_profile: "旧窗口"}]}}));
  assert.equal(await oldRequest, false);
  assert.deepEqual(ui.state.draftCampaign.profile_refs, ["new-ref"]);
});

test("a pending automatic preview cannot overwrite a manual selection", async () => {
  let release;
  const pending = new Promise((resolve) => { release = resolve; });
  const {ui} = harness({responses: {"POST /api/browser-v2/comment-profile-selection/preview": () => pending}});
  ui.state.templates = [{id: "template", name: "评论树", revision: 1, enabled: true, step_count: 1}];
  ui.state.draftCampaign = {template_id: "template", mode: "independent", selection_mode: "automatic", profile_refs: []};
  const preview = ui.refreshSelectionPreview();
  ui.state.draftCampaign = {...ui.state.draftCampaign, selection_mode: "manual", profile_refs: ["manual-ref"]};
  release(response(200, {data: {required_count: 1, eligible_count: 1, profiles: [{profile_ref: "auto-ref", display_profile: "自动窗口"}]}}));

  assert.equal(await preview, false);
  assert.deepEqual(ui.state.draftCampaign.profile_refs, ["manual-ref"]);
});

test("Profile drawer sync button posts once, retains the draft, and wins over an older cache GET", async () => {
  const document = fakeDrawerDocument();
  let releaseCache;
  const cache = new Promise((resolve) => { releaseCache = resolve; });
  const {ui, requests} = harness({document, responses: {
    "GET /api/browser-v2/comment-profile-metadata": () => cache,
    "POST /api/browser-v2/comment-profile-metadata/sync": response(200, {data: [{display_profile: "同步窗口"}], meta: {stale: false}}),
  }});
  ui.state.draftCampaign = {selection_mode: "manual", profile_refs: ["manual-ref"]};
  ui.openDrawer("profile");
  const sync = walkFakeTree(document.nodes["#campaign-drawer-body"]).find((item) => item.tagName === "button" && item.textContent === "同步 Profile");
  assert.ok(sync);
  const polling = ui.poll();
  await sync.listeners.click();
  releaseCache(response(200, {data: [{display_profile: "旧缓存窗口"}], meta: {stale: true}}));
  await polling;

  assert.deepEqual(ui.state.draftCampaign.profile_refs, ["manual-ref"]);
  assert.equal(ui.state.profiles[0].display_profile, "同步窗口");
  assert.deepEqual(requests.filter((item) => item.url.endsWith("/comment-profile-metadata/sync")).map((item) => item.body), [{}]);
});

test("poll during a pending Profile sync shares the operation and never reads stale cache", async () => {
  let releaseSync;
  const pendingSync = new Promise((resolve) => { releaseSync = resolve; });
  const {ui, requests} = harness({responses: {
    "POST /api/browser-v2/comment-profile-metadata/sync": () => pendingSync,
  }});
  const syncing = ui.syncProfiles();
  const polling = ui.poll();
  assert.equal(requests.filter((item) => item.url.endsWith("/comment-profile-metadata")).length, 0);
  releaseSync(response(200, {data: [{display_profile: "新窗口"}], meta: {stale: false, safe_reason: null}}));

  assert.equal(await syncing, true);
  await polling;
  assert.equal(ui.state.profiles[0].display_profile, "新窗口");
  assert.equal(ui.state.profileMeta.stale, false);
  assert.equal(requests.filter((item) => item.url.endsWith("/comment-profile-metadata/sync")).length, 1);
  assert.equal(requests.filter((item) => item.url.endsWith("/comment-profile-metadata")).length, 0);
});

test("stale cache and unknown reason use fixed Chinese copy without exposing server text", () => {
  const document = fakeDrawerDocument();
  const {ui} = harness({document});
  ui.state.profileMeta = {stale: true, safe_reason: "Authorization: secret"};
  ui.state.draftCampaign = {selection_mode: "manual", profile_refs: []};
  ui.openDrawer("create");
  const text = document.nodes["#campaign-drawer-body"].textContent;
  assert.match(text, /当前展示缓存数据，实际执行前需要 AdsPower 恢复/);
  assert.match(text, /AdsPower 状态未知/);
  assert.equal(text.includes("Authorization: secret"), false);
});

test("duplicate-account pause renders exactly the two safe displays and visible username", () => {
  const document = {querySelector: (selector) => selector === "#comment-campaign-preview" ? target : null, querySelectorAll: () => [], createElement: fakeElement};
  const target = fakeElement("ol");
  const {ui} = harness({document});
  ui.state.selectedDetail = {assignments: [{display_profile: "other", evidence: {identity_failure: {display_profiles: ["***1111", "***2222"], visible_username: "same", account_key: "hidden", raw_url: "wss://hidden"}}}]};

  ui.renderSnapshots();
  assert.match(target.textContent, /\*\*\*1111.*\*\*\*2222.*same/);
  assert.equal(target.textContent.includes("other"), false);
  assert.equal(target.textContent.includes("hidden"), false);
});

test("duplicate-account renderer rejects non-string or extra display values", () => {
  const target = fakeElement("ol");
  const document = {querySelector: (selector) => selector === "#comment-campaign-preview" ? target : null, querySelectorAll: () => [], createElement: fakeElement};
  const {ui} = harness({document});
  ui.state.selectedDetail = {assignments: [{evidence: {identity_failure: {display_profiles: ["***1111", {raw: "Authorization: secret"}, "***3333"], visible_username: "same"}}}]};

  ui.renderSnapshots();
  assert.equal(target.textContent.includes("***1111"), false);
  assert.equal(target.textContent.includes("Authorization: secret"), false);
  assert.equal(target.textContent.includes("same"), false);
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

test("threaded replies get opaque IDs and default to the previous node", () => {
  const ids = ["uuid-root", "uuid-child"];
  const draft = editor.createDraft(() => ids.shift(), "threaded");
  const next = editor.addReply(draft, () => ids.shift());

  assert.equal(next.nodes[1].id, "uuid-child");
  assert.equal(next.nodes[1].parentId, "uuid-root");
  assert.equal(JSON.stringify(next).includes("step_1"), false);
});

test("independent comments remain peers without parents", () => {
  const ids = ["uuid-a", "uuid-b"];
  const draft = editor.createDraft(() => ids.shift(), "independent");
  const next = editor.addReply(draft, () => ids.shift());

  assert.deepEqual(next.nodes.map((node) => node.parentId), [null, null]);
});

test("removing a parent requires an explicit descendant decision", () => {
  const draft = {name: "tree", mode: "threaded", advanced: false, nodes: [
    {id: "a", text: "root", parentId: null},
    {id: "b", text: "child", parentId: "a"},
  ]};

  assert.throws(() => editor.removeNode(draft, "a", {removeDescendants: false}), /node_has_descendants/);
  assert.equal(editor.removeNode(draft, "a", {removeDescendants: true}).nodes.length, 0);
});

test("draft validation detects roots, cycles, blank text and size limits", () => {
  const invalid = {name: "", mode: "threaded", nodes: [
    {id: "a", text: "", parentId: "b"},
    {id: "b", text: "x".repeat(2201), parentId: "a"},
  ]};
  const codes = editor.validate(invalid).map((item) => item.code);

  assert.ok(codes.includes("tree_name_missing"));
  assert.ok(codes.includes("root_count_invalid"));
  assert.ok(codes.includes("comment_text_missing"));
  assert.ok(codes.includes("comment_text_too_long"));
  assert.ok(codes.includes("cycle_detected"));
  assert.equal(editor.validate({name: "x".repeat(101), mode: "independent", nodes: [{id: "a", text: "ok", parentId: null}]}).some((item) => item.code === "tree_name_invalid"), true);
  assert.equal(editor.validate({name: "many", mode: "independent", nodes: Array.from({length: 101}, (_, index) => ({id: String(index), text: "ok", parentId: null}))}).some((item) => item.code === "tree_size_invalid"), true);
});

test("editing and moving nodes preserves opaque IDs and parents", () => {
  const draft = {name: "tree", mode: "threaded", nodes: [
    {id: "existing-root", text: "root", parentId: null},
    {id: "existing-child", text: "child", parentId: "existing-root"},
  ]};
  const moved = editor.moveNode(draft, "existing-child", -1);

  assert.deepEqual(moved.nodes.map((node) => node.id), ["existing-child", "existing-root"]);
  assert.equal(moved.nodes[0].parentId, "existing-root");
  assert.deepEqual(draft.nodes.map((node) => node.id), ["existing-root", "existing-child"]);
  assert.deepEqual(editor.templatePayload(draft).steps.map((step) => [step.id, step.parent_step_id]), [["existing-root", null], ["existing-child", "existing-root"]]);
});

test("setParent prevents invalid relationships and independent nodes cannot have a parent", () => {
  const draft = {name: "tree", mode: "threaded", nodes: [
    {id: "a", text: "root", parentId: null},
    {id: "b", text: "child", parentId: "a"},
  ]};

  assert.throws(() => editor.setParent(draft, "a", "b"), /cycle_detected/);
  assert.throws(() => editor.setParent({...draft, mode: "independent"}, "b", "a"), /independent_parent_invalid/);
});

test("comment-tree renderer emits a safe hierarchy preview without internal IDs", () => {
  const document = {createElement: fakeElement};
  const container = fakeElement("div");
  editor.render({document, container, draft: {name: "tree", mode: "threaded", advanced: false, nodes: [
    {id: "opaque-root", text: "楼主文案", parentId: null},
    {id: "opaque-child", text: "回复文案", parentId: "opaque-root"},
  ]}});

  const nodes = walkFakeTree(container);
  const layout = nodes.find((item) => item.className === "comment-tree-layout");
  const preview = nodes.find((item) => item.className === "comment-tree-preview");
  assert.ok(layout);
  assert.ok(preview);
  assert.match(preview.textContent, /楼主评论/);
  assert.match(preview.textContent, /第 1 层回复/);
  assert.equal(container.textContent.includes("opaque-root"), false);
  assert.equal(container.textContent.includes("opaque-child"), false);
  assert.equal(container.textContent.includes("回复哪条评论"), false);
  assert.equal(nodes.filter((item) => item.tagName === "button").every((item) => item.type === "button"), true);
});

test("Excel preview keeps a client-side selection and commit strips derived fields", async () => {
  const preview = {trees: [
    {name: "有效树", valid: true, errors: [], nodes: [{node_no: "1", parent_node_no: null, text: "root", row: 2, position: 0}]},
    {name: "错误树", valid: false, errors: [{code: "parent_not_found", row: 3}], nodes: [{node_no: "1", parent_node_no: "9", text: "orphan", row: 3, position: 0}]},
  ]};
  const {ui, requests} = harness({responses: {
    "POST /api/browser-v2/comment-template-imports/preview": response(200, {data: preview}),
    "POST /api/browser-v2/comment-template-imports": response(201, {data: {created: [{name: "有效树"}], rejected: []}}),
    "GET /api/browser-v2/comment-templates": response(200, {data: [{id: "server-id", name: "有效树", enabled: true, steps: []}]}),
  }});

  assert.equal(await ui.previewTemplateImport(new Blob(["sheet"])), true);
  assert.equal(ui.state.draftTemplateImport.trees[0].selected, true);
  assert.equal(ui.state.draftTemplateImport.trees[1].selected, false);
  assert.equal(await ui.commitTemplateImport(), true);
  assert.deepEqual(requests.find((item) => item.url === "/api/browser-v2/comment-template-imports").body, {trees: [{
    name: "有效树", nodes: [{node_no: "1", parent_node_no: null, text: "root"}],
  }]});
  assert.equal(ui.state.templates[0].name, "有效树");
});

test("import error messages are Chinese and preview DOM never shows raw codes", () => {
  assert.equal(importErrorMessage({code: "parent_not_found", row: 3}), "第 3 行：找不到回复目标");
  assert.equal(importErrorMessage({code: "cycle_detected"}), "回复关系不能形成循环");
  const document = fakeDrawerDocument();
  const {ui} = harness({document});
  ui.state.templateView = "create";
  ui.state.templateSource = "excel";
  ui.state.draftTemplateImport = {trees: [{name: "错误树", valid: false, selected: false, nodes: [], errors: [{code: "parent_not_found", row: 3}]}]};

  ui.openDrawer("template");

  assert.match(document.nodes["#campaign-drawer-body"].textContent, /第 3 行：找不到回复目标/);
  assert.equal(document.nodes["#campaign-drawer-body"].textContent.includes("parent_not_found"), false);
});

test("partial import keeps rejected trees and does not present a full success", async () => {
  const {ui} = harness({responses: {
    "POST /api/browser-v2/comment-template-imports": response(201, {
      data: {created: [{name: "已创建"}], rejected: [{name: "失败树", errors: [{code: "import_tree_failed"}]}]},
    }),
    "GET /api/browser-v2/comment-templates": response(200, {data: [{id: "server", name: "已创建"}]}),
  }});
  ui.state.draftTemplateImport = {trees: [
    {name: "已创建", valid: true, selected: true, nodes: [{node_no: "1", parent_node_no: null, text: "root"}]},
    {name: "失败树", valid: true, selected: true, nodes: [{node_no: "1", parent_node_no: null, text: "root"}]},
  ]};

  assert.equal(await ui.commitTemplateImport(), true);
  assert.equal(ui.state.draftTemplateImport.trees.length, 1);
  assert.equal(ui.state.draftTemplateImport.trees[0].name, "失败树");
  assert.equal(ui.state.draftTemplateImport.trees[0].valid, false);
  assert.match(ui.state.error, /部分评论树导入成功/);
});

test("fully rejected import retains its draft and returns failure", async () => {
  const {ui, requests} = harness({responses: {
    "POST /api/browser-v2/comment-template-imports": response(201, {
      data: {created: [], rejected: [{name: "失败树", errors: [{code: "template_invalid"}]}]},
    }),
  }});
  ui.state.draftTemplateImport = {trees: [{name: "失败树", valid: true, selected: true, nodes: [{node_no: "1", parent_node_no: null, text: "root"}]}]};

  assert.equal(await ui.commitTemplateImport(), false);
  assert.equal(ui.state.draftTemplateImport.trees[0].name, "失败树");
  assert.equal(ui.state.draftTemplateImport.trees[0].selected, false);
  assert.equal(requests.filter((item) => item.method === "GET").length, 0);
  assert.match(ui.state.error, /未导入/);
});

test("import commit preserves numeric and string zero node references", async () => {
  const {ui, requests} = harness({responses: {
    "POST /api/browser-v2/comment-template-imports": response(201, {data: {created: [{name: "零节点树"}], rejected: []}}),
    "GET /api/browser-v2/comment-templates": response(200, {data: []}),
  }});
  ui.state.draftTemplateImport = {trees: [{name: "零节点树", valid: true, selected: true, nodes: [
    {node_no: 0, parent_node_no: null, text: "root"},
    {node_no: "1", parent_node_no: "0", text: "child"},
  ]}]};

  assert.equal(await ui.commitTemplateImport(), true);
  assert.deepEqual(requests.find((item) => item.method === "POST").body.trees[0].nodes, [
    {node_no: "0", parent_node_no: null, text: "root"},
    {node_no: "1", parent_node_no: "0", text: "child"},
  ]);
});

test("template import and polling keep manual draft state isolated from server snapshots", async () => {
  const {ui} = harness({responses: {
    "GET /api/browser-v2/comment-campaigns": response(200, {data: []}),
    "GET /api/browser-v2/comment-templates": response(200, {data: [{id: "server-id", name: "服务器树"}]}),
    "GET /api/browser-v2/comment-profile-metadata": response(200, {data: []}),
    "GET /api/browser-v2/comment-campaign-health": response(200, {data: {}}),
    "GET /api/browser-v2/comment-settings": response(200, {data: {}}),
  }});
  ui.state.draftTemplate = {name: "手动草稿", mode: "threaded", nodes: [{id: "opaque", text: "keep", parentId: null}]};
  ui.state.draftTemplateImport = {trees: [{name: "导入草稿", valid: true, selected: true, nodes: []}]};

  await ui.poll();

  assert.equal(ui.state.draftTemplate.name, "手动草稿");
  assert.equal(ui.state.draftTemplateImport.trees[0].name, "导入草稿");
  assert.equal(ui.state.templates[0].name, "服务器树");
});

test("campaign selector displays a tree name and keeps its id only in the request state", () => {
  const fs = require("node:fs");
  const source = fs.readFileSync("gateway/static/comment_campaign.js", "utf8");
  assert.match(source, /templateSelect\.dataset\.field = "template"/);
  assert.match(source, /templateSelect\.value/);
  assert.equal(source.includes('["template_id", "模板 ID", "input"]'), false);
  assert.equal(source.includes("template.name || template.id"), false);
  assert.equal(source.includes("assignment.step_label || assignment.step_id"), false);
});

test("editing a fixed-text tree preserves opaque step IDs and uses its revision", async () => {
  const {ui, requests} = harness({responses: {
    "GET /api/browser-v2/comment-templates/t1": response(200, {data: {id: "t1", name: "旧树", description: "保留描述", language: "zh", tags: ["tag"], revision: 4, supported_modes: ["threaded"], steps: [
      {id: "opaque-root", label: "自定义楼主", content_source: "fixed", fixed_text: "root", parent_step_id: null, required_profile_tags: ["owner"], excluded_profile_tags: ["skip"], language: "zh"},
      {id: "opaque-child", content_source: "fixed", fixed_text: "child", parent_step_id: "opaque-root"},
    ]}}),
    "PUT /api/browser-v2/comment-templates/t1": response(200, {data: {id: "t1"}}),
    "GET /api/browser-v2/comment-templates": response(200, {data: []}),
  }});

  assert.equal(await ui.editTemplate({id: "t1"}), true);
  assert.deepEqual(ui.state.draftTemplate.nodes.map((item) => item.id), ["opaque-root", "opaque-child"]);
  assert.equal(await ui.saveTemplate(), true);
  const update = requests.find((item) => item.method === "PUT").body;
  assert.equal(update.expected_revision, 4);
  assert.equal(update.description, "保留描述");
  assert.equal(update.language, "zh");
  assert.deepEqual(update.tags, ["tag"]);
  assert.equal(update.steps[0].label, "自定义楼主");
  assert.deepEqual(update.steps[0].required_profile_tags, ["owner"]);
  assert.deepEqual(update.steps[0].excluded_profile_tags, ["skip"]);
  assert.equal(update.steps[0].language, "zh");
});

test("library-backed tree remains read-only and a revision error keeps the manual draft", async () => {
  const {ui, requests} = harness({responses: {
    "GET /api/browser-v2/comment-templates/library": response(200, {data: {id: "library", name: "文案库树", revision: 2, supported_modes: ["threaded"], steps: [{id: "opaque", content_source: "library"}]}}),
    "POST /api/browser-v2/comment-templates": response(409, {error: {code: "revision_conflict", message: "版本冲突"}}),
  }});
  assert.equal(await ui.editTemplate({id: "library"}), false);
  assert.equal(ui.state.readonlyTemplate.name, "文案库树");
  assert.equal(requests.some((item) => item.method === "POST" || item.method === "PUT"), false);

  ui.state.draftTemplate = {name: "保留草稿", mode: "independent", nodes: [{id: "opaque", text: "text", parentId: null}]};
  assert.equal(await ui.saveTemplate(), false);
  assert.equal(ui.state.draftTemplate.name, "保留草稿");
});

test("read-only library and multi-mode trees preserve an existing manual draft", async () => {
  const {ui} = harness({responses: {
    "GET /api/browser-v2/comment-templates/library": response(200, {data: {id: "library", name: "文案库树", supported_modes: ["threaded"], steps: [{content_source: "library"}]}}),
    "GET /api/browser-v2/comment-templates/multi": response(200, {data: {id: "multi", name: "多模式树", supported_modes: ["threaded", "independent"], steps: [{content_source: "fixed"}]}}),
  }});
  ui.state.draftTemplate = {name: "未保存草稿", mode: "threaded", nodes: [{id: "opaque", text: "keep", parentId: null}]};

  assert.equal(await ui.editTemplate({id: "library"}), false);
  assert.equal(ui.state.draftTemplate.name, "未保存草稿");
  assert.equal(await ui.editTemplate({id: "multi"}), false);
  assert.equal(ui.state.draftTemplate.name, "未保存草稿");
  assert.equal(ui.state.readonlyTemplate.name, "多模式树");
});

test("FormData upload preserves the browser multipart boundary and same-origin credentials", async () => {
  let call;
  const dependencies = require("../gateway/static/comment_campaign").commentCampaignDependencies({
    document: {}, setTimeout, clearTimeout, addEventListener() {},
    fetch: async (url, options) => { call = {url, options}; return {status: 200, json: async () => ({data: {}})}; },
  });
  const form = new FormData(); form.append("file", new Blob(["sheet"]), "trees.xlsx");
  await dependencies.requestJson("/api/browser-v2/comment-template-imports/preview", "POST", form, {isFormData: true});

  assert.equal(call.options.body, form);
  assert.deepEqual(call.options.headers, {});
  assert.equal(call.options.credentials, "same-origin");
});

test("empty or invalid import selections do not send a commit request", async () => {
  const {ui, requests} = harness();
  ui.state.draftTemplateImport = {trees: [{name: "错误树", valid: false, selected: true, nodes: []}]};
  assert.equal(await ui.commitTemplateImport(), false);
  assert.equal(requests.length, 0);
});

test("template enable and delete use exact revision payloads and cancellation sends nothing", async () => {
  const template = {id: "hidden-template", name: "树", revision: 7, enabled: false};
  const {ui, requests} = harness({responses: {
    "POST /api/browser-v2/comment-templates/hidden-template/enable": response(200, {data: {revision: 8}}),
    "POST /api/browser-v2/comment-templates/hidden-template/delete": response(200, {data: {revision: 8}}),
    "GET /api/browser-v2/comment-templates": response(200, {data: []}),
  }});

  let cancelledConfirmCalls = 0;
  assert.equal(await ui.deleteTemplate(template, () => { cancelledConfirmCalls += 1; return false; }), false);
  assert.equal(cancelledConfirmCalls, 1);
  assert.equal(requests.length, 0);
  assert.equal(await ui.enableTemplate(template), true);
  const confirmationCopy = [];
  assert.equal(await ui.deleteTemplate(template, (message) => { confirmationCopy.push(message); return true; }), true);
  assert.equal(confirmationCopy.length, 1);
  assert.equal(confirmationCopy[0], "删除后将不再显示，且无法在界面恢复，是否继续？");
  assert.deepEqual(requests.filter((item) => item.method === "POST"), [
    {method: "POST", url: "/api/browser-v2/comment-templates/hidden-template/enable", body: {expected_revision: 7}},
    {method: "POST", url: "/api/browser-v2/comment-templates/hidden-template/delete", body: {expected_revision: 7}},
  ]);
});

test("template lifecycle conflict refreshes server revisions without retrying or clearing drafts", async () => {
  const {ui, requests} = harness({responses: {
    "POST /api/browser-v2/comment-templates/hidden-template/enable": response(409, {error: {code: "revision_conflict", message: "版本冲突"}}),
    "GET /api/browser-v2/comment-templates": response(200, {data: [{id: "hidden-template", name: "树", revision: 8, enabled: false}]}),
  }});
  ui.state.draftTemplate = {name: "保留手动草稿", mode: "threaded", nodes: [{id: "opaque", text: "keep", parentId: null}]};
  ui.state.draftTemplateImport = {trees: [{name: "保留导入草稿", valid: true, selected: true, nodes: []}]};
  ui.state.disabledTemplatesOpen = true;

  assert.equal(await ui.enableTemplate({id: "hidden-template", revision: 7, enabled: false}), false);
  assert.equal(ui.state.draftTemplate.name, "保留手动草稿");
  assert.equal(ui.state.draftTemplateImport.trees[0].name, "保留导入草稿");
  assert.equal(ui.state.disabledTemplatesOpen, true);
  assert.equal(requests.filter((item) => item.method === "POST").length, 1);
  assert.equal(ui.state.templates[0].revision, 8);
});

test("template save and import conflicts refresh revisions but retain both drafts", async () => {
  const document = fakeDrawerDocument();
  const {ui} = harness({document, responses: {
    "POST /api/browser-v2/comment-templates": response(409, {error: {code: "revision_conflict", message: "版本冲突"}}),
    "POST /api/browser-v2/comment-template-imports": response(409, {error: {code: "revision_conflict", message: "版本冲突"}}),
    "GET /api/browser-v2/comment-templates": response(200, {data: [{id: "opaque-server", name: "服务端评论树", revision: 9, enabled: true}]}),
  }});
  ui.state.templateView = "create";
  ui.state.disabledTemplatesOpen = true;
  ui.state.draftTemplate = {name: "手动草稿", mode: "independent", nodes: [{id: "opaque-manual", text: "keep", parentId: null}]};
  ui.state.draftTemplateImport = {trees: [{name: "导入草稿", valid: true, selected: true, nodes: [{node_no: "1", parent_node_no: null, text: "keep"}]}]};

  assert.equal(await ui.saveTemplate(), false);
  assert.equal(ui.state.draftTemplate.name, "手动草稿");
  assert.equal(ui.state.templates[0].revision, 9);
  assert.equal(ui.state.disabledTemplatesOpen, true);

  assert.equal(await ui.commitTemplateImport(), false);
  assert.equal(ui.state.draftTemplateImport.trees[0].name, "导入草稿");
  assert.equal(ui.state.disabledTemplatesOpen, true);
});

test("comment tree list is compact, localized, and hides opaque IDs", () => {
  const document = fakeDrawerDocument();
  const {ui} = harness({document});
  ui.state.templates = [
    {id: "opaque-enabled", name: "春季盖楼", revision: 3, supported_modes: ["threaded"], step_count: 4, enabled: true},
    {id: "opaque-disabled", name: "独立互动", revision: 2, supported_modes: ["independent"], step_count: 2, enabled: false},
  ];

  ui.openDrawer("template");

  const body = document.nodes["#campaign-drawer-body"];
  assert.match(body.textContent, /春季盖楼.*版本 3.*盖楼回复.*4 条评论/);
  assert.match(body.textContent, /独立互动.*版本 2.*独立评论.*2 条评论/);
  assert.equal(body.textContent.includes("opaque-enabled"), false);
  assert.equal(body.textContent.includes("opaque-disabled"), false);
  assert.equal(body.textContent.includes("threaded"), false);
  assert.equal(body.textContent.includes("independent"), false);
  const details = walkFakeTree(body).find((item) => item.tagName === "details");
  assert.equal(details.open, false);
  assert.ok(walkFakeTree(body).filter((item) => item.tagName === "button").every((item) => item.type === "button"));
});

test("comment tree create view excludes list rows and Campaign selector exposes enabled localized summaries only", () => {
  const document = fakeDrawerDocument();
  const {ui} = harness({document});
  ui.state.templates = [
    {id: "opaque-enabled", name: "可选评论树", revision: 5, supported_modes: ["threaded"], step_count: 3, enabled: true},
    {id: "opaque-disabled", name: "已停用评论树", revision: 2, supported_modes: ["independent"], step_count: 2, enabled: false},
  ];
  ui.state.templateView = "create";
  ui.state.templateSource = "manual";
  ui.state.draftTemplate = {name: "新草稿", mode: "independent", nodes: [{id: "opaque-node", text: "文案", parentId: null}]};

  ui.openDrawer("template");
  const createText = document.nodes["#campaign-drawer-body"].textContent;
  assert.match(createText, /逐条手动创建/);
  assert.match(createText, /Excel 导入/);
  assert.equal(createText.includes("可选评论树"), false);
  assert.equal(createText.includes("已停用评论树"), false);
  assert.equal(createText.includes("opaque-node"), false);

  ui.openDrawer("create");
  const selects = walkFakeTree(document.nodes["#campaign-drawer-body"]).filter((item) => item.tagName === "select");
  const templateSelect = selects.find((item) => item.dataset.field === "template");
  assert.equal(templateSelect.children.length, 2);
  assert.match(templateSelect.children[1].textContent, /可选评论树.*版本 5.*盖楼回复.*3 条评论/);
  assert.equal(templateSelect.children[1].textContent.includes("opaque-enabled"), false);
  assert.equal(templateSelect.children[1].textContent.includes("threaded"), false);
});

test("editor script loads between the shared request helper and workbench", () => {
  const fs = require("node:fs");
  const html = fs.readFileSync("gateway/templates/comment_campaign.html", "utf8");
  assert.match(html, /management_fetch\.js[\s\S]*comment_tree_editor\.js[\s\S]*comment_campaign\.js/);
});

test("drawer fallback sets the dialog open attribute when showModal is unavailable", () => {
  const document = fakeDrawerDocument();
  const drawer = document.nodes["#campaign-drawer"];
  drawer.showModal = undefined;
  drawer.close = undefined;
  const {ui} = harness({document});
  ui.state.templateSource = "excel";

  ui.openDrawer("template");
  assert.equal(drawer.attributes.open, "");
  assert.equal(drawer.hidden, false);
  ui.closeDrawer();
  assert.equal(Object.hasOwn(drawer.attributes, "open"), false);
  assert.equal(drawer.hidden, true);
});

test("comment campaign uses the approved light, responsive comment-tree visual contract", () => {
  const fs = require("node:fs");
  const css = fs.readFileSync("gateway/static/comment_campaign.css", "utf8");
  assert.match(css, /background:\s*#F6F7FB/i);
  assert.match(css, /background:\s*#FFFFFF/i);
  assert.match(css, /border:\s*1px solid #DBE1EA/i);
  assert.doesNotMatch(css, /^:root\s*\{/m);
  assert.match(css, /^\.comment-campaign-page\s*\{/m);
  assert.match(css, /^\.comment-campaign-page \*\s*\{/m);
  assert.match(css, /^\.comment-campaign-page button,/m);
  assert.doesNotMatch(css, /^\*\s*\{/m);
  assert.doesNotMatch(css, /^button,\s*input,/m);
  assert.doesNotMatch(css, /#09090B/i);
  assert.match(css, /font-family:\s*Inter/i);
  assert.match(css, /\.comment-tree-layout/);
  assert.match(css, /transition:[^;]*300ms/i);
  assert.match(css, /@media \(max-width: 820px\)/);
  assert.match(css, /@media \(max-width: 480px\)/);
  assert.match(css, /@media \(max-width: 360px\)/);
  assert.match(css, /\.campaign-drawer[\s\S]*max-height/i);
  assert.match(css, /\.comment-tree-node::before/);
  assert.match(css, /overflow-wrap:\s*anywhere/);
  assert.match(css, /\.comment-campaign-page button:focus-visible[\s\S]*\.comment-campaign-page textarea:focus-visible/);
  assert.match(css, /@media \(max-width: 480px\)[\s\S]*\.comment-tree-row-actions\s*\{[^}]*grid-template-columns:\s*repeat\(2, minmax\(0, 1fr\)\)[^}]*width:\s*100%/);
  assert.match(css, /\.comment-tree-row-actions \.campaign-button\s*\{[^}]*width:\s*100%[^}]*min-width:\s*0[^}]*white-space:\s*nowrap/);
  assert.doesNotMatch(css, /\.campaign-filters button,\s*\.campaign-button\s*\{[^}]*width:\s*100%/);
});
