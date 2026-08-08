(function (root, factory) {
  "use strict";
  const exported = factory(root);
  if (typeof module === "object" && module.exports) module.exports = exported;
  if (root && root.document) {
    root.addEventListener("DOMContentLoaded", function () {
      const ui = exported.createCommentCampaignUI(exported.commentCampaignDependencies(root));
      root.CommentCampaignUI = ui;
      ui.init();
    }, {once: true});
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function (root) {
  "use strict";

  const API_PREFIX = "/api/browser-v2";
  const POLL_MS = 5000;
  const STATUS_LABELS = {
    draft: "草稿", planned: "已规划", awaiting_campaign_approval: "等待 Campaign 审批",
    queued: "已排队", running: "运行中", paused: "已暂停", failed: "异常",
    completed: "已完成", cancelled: "已取消", awaiting_step_approval: "等待提交确认",
    published_verified: "已验证发布", published_unverified: "发布结果未验证",
  };

  function apiPath(path) {
    const value = String(path || "");
    if (!value.startsWith(API_PREFIX + "/comment-")) {
      throw new Error("工作台只允许调用 comment-* 接口");
    }
    return value;
  }

  function safeEvidencePath(value) {
    const match = /^evidence\/([0-9a-f]{32}\.png)$/.exec(String(value || ""));
    return match ? "/comment-campaign-evidence/" + match[1] : "";
  }

  function commentCampaignDependencies(win) {
    return {
      document: win.document,
      setTimeout: win.setTimeout.bind(win),
      clearTimeout: win.clearTimeout.bind(win),
      addUnload: function (handler) { win.addEventListener("beforeunload", handler); },
      addVisibility: function (handler) { win.document.addEventListener("visibilitychange", handler); },
      requestJson: async function (url, method, body) {
        apiPath(url);
        const response = await win.fetch(url, {
          method: method || "GET",
          headers: body === undefined ? {} : {"Content-Type": "application/json"},
          body: body === undefined ? undefined : JSON.stringify(body),
          credentials: "same-origin",
        });
        let data = {};
        try { data = await response.json(); } catch (_) { data = {error: {message: "服务返回格式无效", code: "invalid_response"}}; }
        return {status: response.status, data: data};
      },
    };
  }

  function createCommentCampaignUI(dependencies) {
    const deps = dependencies || {};
    const state = {
      campaigns: [], templates: [], profiles: [], health: {}, settings: {},
      selectedCampaignId: "", draftCampaign: null, draftTemplate: null,
      approvalInFlight: new Set(), approvalSent: new Set(), approvalDrafts: {}, renderedApprovalKey: "", pollTimer: null, pollingEnabled: true, error: "",
      filter: "all", selectedDetail: null, selectedReceipts: [], selectedAttempts: [], draftSettings: null, settingsSaving: false, initialized: false, snapshotEpoch: {}, detailEpoch: 0,
    };

    function doc() { return deps.document; }
    function el(selector) { return doc() && doc().querySelector(selector); }
    function all(selector) { return doc() ? Array.from(doc().querySelectorAll(selector)) : []; }
    function node(tag, text, className) {
      const item = doc().createElement(tag);
      if (className) item.className = className;
      if (text !== undefined) item.textContent = String(text);
      return item;
    }
    function clear(item) { if (item) item.replaceChildren(); }
    function errorMessage(result, fallback) {
      const error = result && result.data && result.data.error;
      if (error && typeof error === "object") return [error.message, error.code].filter(Boolean).join("（") + (error.message && error.code ? "）" : "");
      return (typeof error === "string" && error) || fallback;
    }
    function unwrap(result) {
      if (result && result.data && Object.prototype.hasOwnProperty.call(result.data, "data")) {
        return {status: result.status, data: result.data.data};
      }
      return result;
    }
    async function request(path, method, body) {
      try { return unwrap(await deps.requestJson(apiPath(path), method || "GET", body)); }
      catch (error) { return {status: 0, data: {error: {message: error && error.message ? error.message : "请求失败", code: "network_error"}}}; }
    }
    function ok(result, allowed) { return Boolean(result) && (allowed || [200, 201, 202]).includes(result.status); }
    function statusLabel(status) { return STATUS_LABELS[status] || "处理中"; }
    function campaignPath(campaignId, suffix) {
      return API_PREFIX + "/comment-campaigns/" + encodeURIComponent(campaignId) + (suffix || "");
    }
    function setMessage(message) {
      state.error = message || "";
      const target = el("#comment-campaign-error");
      if (target) target.textContent = state.error;
    }
    function serverCampaigns() {
      return state.filter === "all" ? state.campaigns : state.campaigns.filter(function (campaign) {
        if (state.filter === "approval") return campaign.status === "awaiting_campaign_approval" || campaign.awaiting_approval_count > 0;
        if (state.filter === "abnormal") return ["failed", "paused"].includes(campaign.status) || Number(campaign.abnormal_assignment_count || 0) > 0;
        return campaign.status === state.filter;
      });
    }
    async function loadSnapshot(name, path, fallback) {
      const epoch = (state.snapshotEpoch[name] || 0) + 1;
      state.snapshotEpoch[name] = epoch;
      const result = await request(path, "GET");
      if (state.snapshotEpoch[name] !== epoch) return false;
      if (ok(result, [200])) { state[name] = result.data || fallback; return true; }
      setMessage(errorMessage(result, "无法读取工作台数据"));
      return false;
    }
    async function refreshSnapshots() {
      const reads = await Promise.all([
        loadSnapshot("campaigns", API_PREFIX + "/comment-campaigns", []),
        loadSnapshot("templates", API_PREFIX + "/comment-templates", []),
        loadSnapshot("profiles", API_PREFIX + "/comment-profile-metadata", []),
        loadSnapshot("health", API_PREFIX + "/comment-campaign-health", {}),
        loadSnapshot("settings", API_PREFIX + "/comment-settings", {}),
      ]);
      renderSnapshots();
      const selected = state.selectedCampaignId;
      if (selected) await selectCampaign(selected);
      return reads.every(Boolean);
    }
    async function poll() { return refreshSnapshots(); }
    function stopPolling() {
      state.pollingEnabled = false;
      if (state.pollTimer !== null) {
        (deps.clearTimeout || function () {})(state.pollTimer);
        state.pollTimer = null;
      }
    }
    function schedulePoll() {
      stopPolling();
      state.pollingEnabled = true;
      state.pollTimer = (deps.setTimeout || function () { return null; })(async function () {
        state.pollTimer = null;
        await poll();
        if (state.pollingEnabled) schedulePoll();
      }, POLL_MS);
      return state.pollTimer;
    }
    async function selectCampaign(campaignId) {
      state.selectedCampaignId = campaignId;
      const epoch = state.detailEpoch + 1;
      state.detailEpoch = epoch;
      const results = await Promise.all([
        request(campaignPath(campaignId), "GET"),
        request(campaignPath(campaignId, "/receipts"), "GET"),
        request(campaignPath(campaignId, "/attempts"), "GET"),
      ]);
      if (state.detailEpoch !== epoch || state.selectedCampaignId !== campaignId) return false;
      if (!ok(results[0], [200])) { setMessage(errorMessage(results[0], "无法读取 Campaign 详情")); return false; }
      state.selectedDetail = results[0].data;
      if (ok(results[1], [200])) state.selectedReceipts = results[1].data || [];
      if (ok(results[2], [200])) state.selectedAttempts = results[2].data || [];
      renderSnapshots();
      return true;
    }
    async function approveSubmit(assignmentId, revision) {
      const campaignId = state.selectedCampaignId;
      const key = [campaignId, assignmentId, revision].join(":");
      if (!campaignId || !Number.isInteger(revision) || state.approvalInFlight.has(key) || state.approvalSent.has(key)) return false;
      state.approvalInFlight.add(key);
      renderApproval();
      const result = await request(campaignPath(campaignId, "/assignments/" + encodeURIComponent(assignmentId) + "/approve-submit"), "POST", {expected_revision: revision});
      state.approvalInFlight.delete(key);
      if (!ok(result, [202])) { setMessage(errorMessage(result, "确认提交失败")); renderApproval(); return false; }
      state.approvalSent.add(key);
      if (state.selectedCampaignId === campaignId) await selectCampaign(campaignId);
      return true;
    }
    async function campaignAction(action, body, expected) {
      const campaignId = state.selectedCampaignId;
      if (!campaignId || !Number.isInteger(expected)) return false;
      const result = await request(campaignPath(campaignId, "/" + action), "POST", {...body, expected_revision: expected});
      if (!ok(result, action === "approve" || action === "resume" ? [202] : [200])) { setMessage(errorMessage(result, "Campaign 操作失败")); return false; }
      return state.selectedCampaignId === campaignId ? selectCampaign(campaignId) : true;
    }
    function selectedCampaign() { return (state.selectedDetail || {}).campaign || null; }
    function planCampaign() { const campaign = selectedCampaign(); return campaignAction("plan", {allocation_seed: ""}, campaign && campaign.revision); }
    function reallocateCampaign() { const campaign = selectedCampaign(); return campaignAction("reallocate", {allocation_seed: ""}, campaign && campaign.revision); }
    function lockPlan() { const campaign = selectedCampaign(); return campaignAction("lock-plan", {}, campaign && campaign.revision); }
    function approveCampaign() { const campaign = selectedCampaign(); return campaignAction("approve", {}, campaign && campaign.revision); }
    function pauseCampaign(reason) { const campaign = selectedCampaign(); return campaignAction("pause", {reason: String(reason || "").trim()}, campaign && campaign.revision); }
    function resumeCampaign() { const campaign = selectedCampaign(); return campaignAction("resume", {}, selectedCampaign() && selectedCampaign().revision); }
    function cancelCampaign() { const campaign = selectedCampaign(); return campaignAction("cancel", {}, campaign && campaign.revision); }
    async function overrideAssignment(assignmentId, revision, profileRef) {
      if (!state.selectedCampaignId || !profileRef || !Number.isInteger(revision)) return false;
      const result = await request(campaignPath(state.selectedCampaignId, "/assignments/" + encodeURIComponent(assignmentId)), "PUT", {expected_revision: revision, profile_ref: profileRef});
      if (!ok(result, [200])) { setMessage(errorMessage(result, "单条换号失败")); return false; }
      return selectCampaign(state.selectedCampaignId);
    }
    async function createCampaign() {
      const draft = state.draftCampaign || {};
      const body = {
        name: String(draft.name || "").trim(), mode: draft.mode || "independent",
        target_source: "manual_url", target_reference: String(draft.target_reference || "").trim(),
        template_id: String(draft.template_id || "").trim(),
        profile_refs: String(draft.profile_refs || "").split(/[\s,]+/).filter(Boolean),
        batch_size: Number(draft.batch_size || 3), start_mode: "manual",
      };
      const result = await request(API_PREFIX + "/comment-campaigns", "POST", body);
      if (!ok(result, [201])) { setMessage(errorMessage(result, "新建 Campaign 失败")); return false; }
      state.draftCampaign = null;
      state.selectedCampaignId = result.data.id;
      await selectCampaign(result.data.id);
      closeDrawer();
      return true;
    }
    async function saveTemplate() {
      const draft = state.draftTemplate || {};
      const steps = Array.isArray(draft.steps) ? draft.steps : [];
      const body = {name: String(draft.name || "").trim(), description: "", supported_modes: [draft.mode || "independent"], language: "", tags: [], steps: steps.map(function (step) { return {id: String(step.id || "").trim(), label: String(step.label || "").trim(), content_source: step.content_source || "fixed", fixed_text: step.content_source === "library" ? "" : String(step.fixed_text || "").trim(), content_library_id: step.content_source === "library" ? String(step.content_library_id || "").trim() : "", content_item_id: step.content_source === "library" ? String(step.content_item_id || "").trim() : "", parent_step_id: draft.mode === "threaded" && step.parent_step_id ? step.parent_step_id : null, required_profile_tags: [], excluded_profile_tags: [], language: ""}; })};
      const result = await request(API_PREFIX + "/comment-templates", "POST", body);
      if (!ok(result, [201])) { setMessage(errorMessage(result, "保存评论模板失败")); return false; }
      state.draftTemplate = null;
      await loadSnapshot("templates", API_PREFIX + "/comment-templates", []);
      openDrawer("template");
      return true;
    }
    function newTemplateDraft() { return {name: "", mode: "independent", steps: [{id: "step_1", label: "步骤 1", content_source: "fixed", fixed_text: "", content_library_id: "", content_item_id: "", parent_step_id: null}]}; }
    function changeTemplateStep(index, field, value) { const draft = state.draftTemplate || newTemplateDraft(); const steps = draft.steps.map(function (step) { return {...step}; }); if (!steps[index]) return false; steps[index][field] = value; state.draftTemplate = {...draft, steps: steps}; return true; }
    function addTemplateStep() { const draft = state.draftTemplate || newTemplateDraft(); const steps = draft.steps.concat({id: "step_" + (draft.steps.length + 1), label: "新步骤", content_source: "fixed", fixed_text: "", content_library_id: "", content_item_id: "", parent_step_id: null}); state.draftTemplate = {...draft, steps: steps}; return true; }
    function removeTemplateStep(index) { const draft = state.draftTemplate || newTemplateDraft(); if (draft.steps.length <= 1 || !draft.steps[index]) return false; const removed = draft.steps[index].id; state.draftTemplate = {...draft, steps: draft.steps.filter(function (_step, position) { return position !== index; }).map(function (step) { return step.parent_step_id === removed ? {...step, parent_step_id: null} : step; })}; return true; }
    function moveTemplateStep(index, direction) { const draft = state.draftTemplate || newTemplateDraft(), target = index + direction; if (!draft.steps[index] || !draft.steps[target]) return false; const steps = draft.steps.slice(); [steps[index], steps[target]] = [steps[target], steps[index]]; state.draftTemplate = {...draft, steps: steps}; return true; }
    async function disableTemplate(template) {
      const result = await request(API_PREFIX + "/comment-templates/" + encodeURIComponent(template.id) + "/disable", "POST", {expected_revision: template.revision});
      if (!ok(result, [200])) { setMessage(errorMessage(result, "停用模板失败")); return false; }
      await loadSnapshot("templates", API_PREFIX + "/comment-templates", []);
      openDrawer("template");
      return true;
    }
    async function saveProfileMetadata(profile, changes) {
      const body = {profile_ref: profile.profile_ref, expected_username: String(changes.expected_username || "").trim(), enabled: Boolean(changes.enabled), login_verified: Boolean(changes.login_verified), tags: String(changes.tags || "").split(",").map(function (value) { return value.trim(); }).filter(Boolean), language: String(changes.language || "").trim(), region: String(changes.region || "").trim(), cooldown_until: String(changes.cooldown_until || "").trim() || null, health_status: String(changes.health_status || "unknown")};
      const result = await request(API_PREFIX + "/comment-profile-metadata", "POST", body);
      if (!ok(result, [200])) { setMessage(errorMessage(result, "保存 Profile 元数据失败")); return false; }
      await loadSnapshot("profiles", API_PREFIX + "/comment-profile-metadata", []);
      openDrawer("profile");
      return true;
    }
    async function rejectSubmit(assignmentId, revision, reason) {
      const campaignId = state.selectedCampaignId;
      if (!campaignId || !String(reason || "").trim()) { setMessage("请填写拒绝原因"); return false; }
      const key = [campaignId, assignmentId, revision].join(":");
      if (state.approvalInFlight.has(key) || state.approvalSent.has(key)) return false;
      state.approvalInFlight.add(key);
      renderApproval();
      const result = await request(campaignPath(campaignId, "/assignments/" + encodeURIComponent(assignmentId) + "/reject-submit"), "POST", {expected_revision: revision, reason: String(reason).trim()});
      state.approvalInFlight.delete(key);
      if (!ok(result, [200])) { setMessage(errorMessage(result, "拒绝提交失败")); renderApproval(); return false; }
      state.approvalSent.add(key);
      return state.selectedCampaignId === campaignId ? selectCampaign(campaignId) : true;
    }
    async function resolveUnverified(assignmentId, revision, resolution, reason) {
      const campaignId = state.selectedCampaignId;
      if (!campaignId || !String(reason || "").trim()) { setMessage("请填写处理原因"); return false; }
      const key = [campaignId, assignmentId, revision].join(":");
      if (state.approvalInFlight.has(key) || state.approvalSent.has(key)) return false;
      state.approvalInFlight.add(key);
      renderApproval();
      const result = await request(campaignPath(campaignId, "/assignments/" + encodeURIComponent(assignmentId) + "/resolve-unverified"), "POST", {expected_revision: revision, resolution: resolution, reason: String(reason).trim()});
      state.approvalInFlight.delete(key);
      if (!ok(result, [200])) { setMessage(errorMessage(result, "处理未验证结果失败")); renderApproval(); return false; }
      state.approvalSent.add(key);
      return state.selectedCampaignId === campaignId ? selectCampaign(campaignId) : true;
    }
    async function saveSettings() {
      const draft = state.draftSettings;
      if (!draft || state.settingsSaving || state.settings.can_write !== true) return false;
      const body = {
        expected_revision: draft.revision,
        entry_element_id: String(draft.entry_element_id || "").trim(),
        input_element_id: String(draft.input_element_id || "").trim(),
        submit_element_id: String(draft.submit_element_id || "").trim(),
        account_element_id: String(draft.account_element_id || "").trim(),
      };
      if (Object.keys(body).some(function (key) { return key !== "expected_revision" && !body[key]; })) { setMessage("请填写四个评论元素 ID"); return false; }
      state.settingsSaving = true;
      const result = await request(API_PREFIX + "/comment-settings", "PUT", body);
      state.settingsSaving = false;
      if (!ok(result, [200])) { setMessage(errorMessage(result, "保存评论元素设置失败")); return false; }
      state.settings = result.data || state.settings;
      state.draftSettings = {...(state.settings.element_bindings || {}), revision: state.settings.revision};
      openDrawer("settings");
      return true;
    }
    function renderHealth() {
      const target = el("#comment-campaign-health");
      if (!target) return;
      clear(target);
      ["adspower", "redis", "worker", "sqlite"].forEach(function (name) {
        const service = state.health[name] || {};
        const item = node("span", name === "adspower" ? "AdsPower" : name.toUpperCase(), "campaign-health " + (service.status || "unknown"));
        item.title = service.message || "";
        item.append("：" + (service.status === "connected" ? "已连接" : service.status === "unavailable" ? "不可用" : "未知"));
        target.append(item);
      });
    }
    function displayProfile(assignment) {
      return assignment.display_profile || assignment.profile_ref || "未分配";
    }
    function renderCampaigns() {
      const target = el("#comment-campaign-list");
      if (!target) return;
      clear(target);
      serverCampaigns().forEach(function (campaign) {
        const card = node("article", undefined, "campaign-card");
        const open = node("button", campaign.name || campaign.id || "未命名 Campaign", "campaign-card-title");
        open.type = "button";
        open.addEventListener("click", function () { selectCampaign(campaign.id); });
        const status = node("span", statusLabel(campaign.status), "campaign-status " + (campaign.status || "unknown"));
        const summary = node("p", [campaign.mode === "threaded" ? "串行回复" : "独立评论", campaign.video_id ? "视频 " + campaign.video_id : "", campaign.assignment_count ? "计划 " + campaign.assignment_count + " 条" : ""].filter(Boolean).join(" · "), "campaign-muted");
        card.append(open, status, summary);
        target.append(card);
      });
      const empty = el("#comment-campaign-empty");
      if (empty) empty.hidden = serverCampaigns().length > 0;
    }
    function approvalRows() {
      const detail = state.selectedDetail || {};
      return (detail.assignments || []).filter(function (assignment) {
        return assignment.status === "awaiting_step_approval" || assignment.status === "published_unverified";
      });
    }
    function actualVideoEvidence(video) {
      const href = video && (video.canonical_url || video.visible_video_href);
      const match = typeof href === "string" && href.match(/\/video\/([0-9]{8,30})(?:[/?]|$)/);
      return match ? "现场视频 ID：" + match[1] + "；链接：" + href : "无现场视频证据";
    }
    function parentScopeEvidence(parent) {
      if (!parent || typeof parent !== "object") return "无冻结父评论范围证据";
      const stable = parent.parent_stable_attributes;
      return [parent.parent_platform_comment_id && "ID：" + parent.parent_platform_comment_id, parent.parent_comment_permalink && "链接：" + parent.parent_comment_permalink, stable && "稳定指纹：" + JSON.stringify(stable)].filter(Boolean).join("；") || "无冻结父评论范围证据";
    }
    function renderApproval() {
      const target = el("#comment-campaign-approvals");
      if (!target) return;
      const rows = approvalRows();
      const key = rows.map(function (assignment) { const decision = [state.selectedCampaignId, assignment.assignment_id || assignment.id, assignment.revision].join(":"); return [decision, assignment.status, state.approvalInFlight.has(decision), state.approvalSent.has(decision)].join(":"); }).join("|");
      if (target.childElementCount && key === state.renderedApprovalKey) return;
      state.renderedApprovalKey = key;
      clear(target);
      rows.forEach(function (assignment) {
        const assignmentId = assignment.assignment_id || assignment.id;
        const evidence = assignment.evidence || {};
        const pageEvidence = evidence.page_evidence || {};
        const hasActualAccount = Boolean(pageEvidence.account && pageEvidence.account.username);
        const hasTextEvidence = Boolean(evidence.resolved_text_sha256);
        const card = node("article", undefined, "approval-card");
        card.append(node("h3", assignment.step_label || assignment.step_id || "评论步骤"));
        const gates = node("ul", undefined, "approval-gates");
        [
          ["登录账号", hasActualAccount ? pageEvidence.account.username : "无现场账号证据"],
          ["视频", actualVideoEvidence(pageEvidence.video)],
          ["父评论范围", assignment.parent_assignment_id ? parentScopeEvidence(pageEvidence.parent) : "不适用"],
          ["输入文本", hasTextEvidence ? (assignment.resolved_text || assignment.comment_text || "") + "（已冻结哈希）" : "无现场输入文本证据"],
        ].forEach(function (pair) { gates.append(node("li", pair[0] + "：" + pair[1])); });
        const profile = node("p", "Profile：" + displayProfile(assignment), "campaign-muted");
        card.append(gates, profile);
        const evidenceUrl = safeEvidencePath((assignment.evidence || {}).screenshot_path);
        if (evidenceUrl) { const view = node("a", "查看现在", "campaign-button"); view.href = evidenceUrl; view.target = "_blank"; view.rel = "noopener"; card.append(view); }
        const reasonKey = [state.selectedCampaignId, assignmentId, assignment.revision].join(":");
        const reason = node("input"); reason.placeholder = "处理原因（必填）"; reason.setAttribute("aria-label", "处理原因"); reason.value = state.approvalDrafts[reasonKey] || "";
        reason.addEventListener("input", function () { state.approvalDrafts[reasonKey] = reason.value; });
        if (assignment.status === "awaiting_step_approval") {
          const key = [state.selectedCampaignId, assignmentId, assignment.revision].join(":");
          const approve = node("button", "确认提交", "campaign-button primary"); approve.type = "button"; approve.disabled = state.approvalInFlight.has(key) || state.approvalSent.has(key) || !hasActualAccount || !hasTextEvidence;
          approve.addEventListener("click", function () { approveSubmit(assignmentId, assignment.revision); });
          const reject = node("button", "拒绝并暂停", "campaign-button"); reject.type = "button"; reject.disabled = state.approvalInFlight.has(key) || state.approvalSent.has(key);
          reject.addEventListener("click", function () { rejectSubmit(assignmentId, assignment.revision, reason.value); });
          card.append(reason, approve, reject);
        } else {
          const key = [state.selectedCampaignId, assignmentId, assignment.revision].join(":");
          const published = node("button", "标记已发布", "campaign-button"); published.type = "button"; published.disabled = state.approvalInFlight.has(key) || state.approvalSent.has(key);
          published.addEventListener("click", function () { resolveUnverified(assignmentId, assignment.revision, "published", reason.value); });
          const retry = node("button", "确认未发布并重新准备", "campaign-button"); retry.type = "button"; retry.disabled = state.approvalInFlight.has(key) || state.approvalSent.has(key);
          retry.addEventListener("click", function () { resolveUnverified(assignmentId, assignment.revision, "not_published", reason.value); });
          card.append(reason, published, retry);
        }
        target.append(card);
      });
      const empty = el("#comment-campaign-approvals-empty");
      if (empty) empty.hidden = rows.length > 0;
    }
    function renderPreview() {
      const target = el("#comment-campaign-preview");
      if (!target) return;
      clear(target);
      const assignments = (state.selectedDetail || {}).assignments || [];
      const byId = new Map(assignments.map(function (assignment) { return [assignment.assignment_id || assignment.id, assignment]; }));
      function depth(assignment) {
        let result = 0, parent = assignment.parent_assignment_id, seen = new Set();
        while (parent && byId.has(parent) && !seen.has(parent)) { seen.add(parent); result += 1; parent = byId.get(parent).parent_assignment_id; }
        return result;
      }
      assignments.forEach(function (assignment) {
        const row = node("li", undefined, "campaign-preview-row");
        row.style.marginInlineStart = (depth(assignment) * 22) + "px";
        row.append(node("strong", assignment.step_label || assignment.step_id || "评论步骤"));
        row.append(node("span", "角色：" + (assignment.role || "待分配")));
        row.append(node("span", "Profile：" + displayProfile(assignment)));
        row.append(node("span", "账号：" + (assignment.expected_username || "待校验")));
        row.append(node("span", "父步骤：" + (assignment.parent_step_id || "无")));
        row.append(node("p", assignment.resolved_text || assignment.comment_text || "待冻结文本"));
        if (assignment.status === "planned") {
          const select = node("select");
          state.profiles.forEach(function (profile) { const option = node("option", profile.display_profile || profile.profile_ref); option.value = profile.profile_ref; option.selected = option.value === assignment.profile_ref; select.append(option); });
          const replace = node("button", "单条换号", "campaign-button"); replace.type = "button";
          replace.addEventListener("click", function () { overrideAssignment(assignment.assignment_id || assignment.id, assignment.revision, select.value); });
          row.append(select, replace);
        }
        target.append(row);
      });
    }
    function renderSettings() {
      const target = el("#comment-campaign-settings");
      if (!target) return;
      clear(target);
      const labels = {entry_element_id: "评论入口", input_element_id: "评论输入框", submit_element_id: "提交按钮", account_element_id: "账号身份"};
      const bindings = state.settings.element_bindings || {};
      Object.keys(labels).forEach(function (key) {
        const row = node("div"); row.append(node("dt", labels[key]), node("dd", bindings[key] ? "已配置" : "未配置")); target.append(row);
      });
    }
    function renderSnapshots() { renderHealth(); renderCampaigns(); renderPreview(); renderApproval(); renderSettings(); }
    function drawerText(title, text) {
      const heading = el("#campaign-drawer-title"), body = el("#campaign-drawer-body");
      if (!heading || !body) return;
      heading.textContent = title;
      clear(body); body.append(node("p", text));
    }
    function openDrawer(kind) {
      const drawer = el("#campaign-drawer");
      if (!drawer) return;
      if (kind === "create") {
        drawerText("新建 Campaign", "选择模式、视频、模板、授权 Profile 和分配计划后，仍需人工锁定并审批。");
        const body = el("#campaign-drawer-body");
        const fields = [["name", "名称", "input"], ["target_reference", "视频链接", "input"], ["template_id", "模板 ID", "input"], ["profile_refs", "授权 Profile 引用（逗号分隔）", "input"], ["batch_size", "每批数量", "input"]];
        fields.forEach(function (item) { const label = node("label", item[1]), input = node(item[2]); input.value = (state.draftCampaign || {})[item[0]] || (item[0] === "batch_size" ? "3" : ""); input.autocomplete = "off"; input.addEventListener("input", function () { state.draftCampaign = {...(state.draftCampaign || {}), [item[0]]: input.value}; }); label.append(input); body.append(label); });
        const mode = node("select"); ["independent", "threaded"].forEach(function (value) { const option = node("option", value === "independent" ? "独立评论" : "串行回复"); option.value = value; option.selected = ((state.draftCampaign || {}).mode || "independent") === value; mode.append(option); }); mode.addEventListener("change", function () { state.draftCampaign = {...(state.draftCampaign || {}), mode: mode.value}; }); const modeLabel = node("label", "模式"); modeLabel.append(mode); body.append(modeLabel);
        const save = node("button", "创建 Campaign", "campaign-button primary"); save.type = "button"; save.addEventListener("click", createCampaign); body.append(save);
        body.append(node("p", "草稿仅保存在当前页面内；轮询不会覆盖正在输入的内容。", "campaign-muted"));
      } else if (kind === "template") {
        drawerText("评论模板", "模板包含元数据和可编辑步骤树。每一层步骤在提交前都将冻结为具体文案。");
        const body = el("#campaign-drawer-body"), list = node("div", undefined, "campaign-list");
        state.templates.forEach(function (template) {
          const item = node("article", [template.name || template.id, "修订版 " + (template.revision || 1), (template.supported_modes || []).join("、")].filter(Boolean).join(" · "), "campaign-card");
          const disable = node("button", "停用", "campaign-button"); disable.type = "button"; disable.disabled = !template.enabled; disable.addEventListener("click", function () { disableTemplate(template); }); item.append(disable); list.append(item);
        });
        body.append(list);
        if (!state.draftTemplate) state.draftTemplate = newTemplateDraft();
        const name = node("input"); name.value = state.draftTemplate.name; name.addEventListener("input", function () { state.draftTemplate = {...state.draftTemplate, name: name.value}; }); const nameLabel = node("label", "模板名称"); nameLabel.append(name); body.append(nameLabel);
        const mode = node("select"); ["independent", "threaded"].forEach(function (value) { const option = node("option", value === "threaded" ? "串行回复" : "独立评论"); option.value = value; option.selected = state.draftTemplate.mode === value; mode.append(option); }); mode.addEventListener("change", function () { state.draftTemplate = {...state.draftTemplate, mode: mode.value}; openDrawer("template"); }); const modeLabel = node("label", "模式"); modeLabel.append(mode); body.append(modeLabel);
        state.draftTemplate.steps.forEach(function (step, index) {
          const card = node("section", undefined, "campaign-card");
          [["id", "步骤 ID"], ["label", "步骤名称"], ["fixed_text", "固定文案"], ["content_library_id", "文案库 ID"], ["content_item_id", "文案项 ID"]].forEach(function (field) { const input = node("input"); input.value = step[field[0]] || ""; input.addEventListener("input", function () { changeTemplateStep(index, field[0], input.value); }); const label = node("label", field[1]); label.append(input); card.append(label); });
          const source = node("select"); ["fixed", "library"].forEach(function (value) { const option = node("option", value === "fixed" ? "固定文案" : "文案库"); option.value = value; option.selected = step.content_source === value; source.append(option); }); source.addEventListener("change", function () { changeTemplateStep(index, "content_source", source.value); openDrawer("template"); }); const sourceLabel = node("label", "文案来源"); sourceLabel.append(source); card.append(sourceLabel);
          if (state.draftTemplate.mode === "threaded") { const parent = node("select"); const none = node("option", "无父步骤"); none.value = ""; parent.append(none); state.draftTemplate.steps.forEach(function (candidate) { if (candidate.id !== step.id) { const option = node("option", candidate.label || candidate.id); option.value = candidate.id; option.selected = step.parent_step_id === candidate.id; parent.append(option); } }); parent.addEventListener("change", function () { changeTemplateStep(index, "parent_step_id", parent.value || null); }); const parentLabel = node("label", "父步骤"); parentLabel.append(parent); card.append(parentLabel); }
          [["上移", -1], ["下移", 1], ["删除", null]].forEach(function (action) { const button = node("button", action[0], "campaign-button"); button.type = "button"; button.addEventListener("click", function () { action[1] === null ? removeTemplateStep(index) : moveTemplateStep(index, action[1]); openDrawer("template"); }); card.append(button); }); body.append(card);
        });
        const add = node("button", "新增步骤", "campaign-button"); add.type = "button"; add.addEventListener("click", function () { addTemplateStep(); openDrawer("template"); }); const create = node("button", "保存模板", "campaign-button primary"); create.type = "button"; create.addEventListener("click", saveTemplate); body.append(add, create);
      } else if (kind === "profile") {
        drawerText("Profile 元数据", "仅显示已脱敏 Profile、预期账号、标签、语言、地区、启用/登录校验、健康状态和冷却时间。");
        const body = el("#campaign-drawer-body"), list = node("div", undefined, "campaign-list");
        state.profiles.forEach(function (profile) {
          const summary = [profile.display_profile || profile.profile_ref || "Profile", profile.expected_username || "未设置账号", profile.language || "未设置语言", profile.region || "未设置地区", profile.login_verified ? "已校验登录" : "未校验登录", profile.health_status || "未知"].join(" · ");
          const item = node("article", summary, "campaign-card"), values = {expected_username: profile.expected_username || "", tags: (profile.tags || []).join(","), language: profile.language || "", region: profile.region || "", cooldown_until: profile.cooldown_until || "", health_status: profile.health_status || "unknown", enabled: profile.enabled, login_verified: profile.login_verified};
          ["expected_username", "tags", "language", "region", "cooldown_until"].forEach(function (key) { const input = node("input"); input.value = values[key]; input.addEventListener("input", function () { values[key] = input.value; }); const label = node("label", key); label.append(input); item.append(label); });
          ["enabled", "login_verified"].forEach(function (key) { const input = node("input"); input.type = "checkbox"; input.checked = values[key]; input.addEventListener("change", function () { values[key] = input.checked; }); const label = node("label", key); label.append(input); item.append(label); });
          const health = node("select"); ["healthy", "unknown", "unhealthy"].forEach(function (value) { const option = node("option", value); option.value = value; option.selected = values.health_status === value; health.append(option); }); health.addEventListener("change", function () { values.health_status = health.value; }); const healthLabel = node("label", "健康状态"); healthLabel.append(health); item.append(healthLabel); const save = node("button", "保存 Profile 元数据", "campaign-button"); save.type = "button"; save.addEventListener("click", function () { saveProfileMetadata(profile, values); }); item.append(save); list.append(item);
        });
        body.append(list);
      } else if (kind === "settings") {
        drawerText("评论元素设置", "评论入口、输入框、提交按钮和账号身份元素必须使用已保存且激活的 V2 元素绑定。未配置时执行会安全停止。");
        const body = el("#campaign-drawer-body"), labels = {entry_element_id: "评论入口元素 ID", input_element_id: "评论输入框元素 ID", submit_element_id: "提交按钮元素 ID", account_element_id: "账号身份元素 ID"};
        if (!state.draftSettings) state.draftSettings = {...(state.settings.element_bindings || {}), revision: state.settings.revision};
        Object.keys(labels).forEach(function (key) {
          const label = node("label", labels[key]), input = node("input"); input.value = state.draftSettings[key] || ""; input.maxLength = 120; input.autocomplete = "off";
          input.addEventListener("input", function () { state.draftSettings = {...state.draftSettings, [key]: input.value}; }); label.append(input); body.append(label);
        });
        const save = node("button", "保存元素绑定", "campaign-button primary"); save.type = "button"; save.disabled = state.settings.can_write !== true || state.settingsSaving;
        save.addEventListener("click", saveSettings); body.append(save);
        body.append(node("p", "可直接保存这四项运行时绑定；每项必须是已启用且类型匹配的 V2 元素 ID。", "campaign-muted"));
      } else if (kind === "detail") {
        drawerText("Campaign 详情", "分配快照、尝试记录和回执均为服务端快照；截图仅能通过安全 PNG 路径打开。");
        const body = el("#campaign-drawer-body"), detail = state.selectedDetail || {}, list = node("div", undefined, "campaign-list");
        (detail.assignments || []).forEach(function (assignment) {
          list.append(node("article", [assignment.step_id, assignment.status, assignment.display_profile || assignment.profile_ref || "未分配"].filter(Boolean).join(" · "), "campaign-card"));
        });
        (state.selectedAttempts || []).forEach(function (attempt) { list.append(node("article", ["尝试", attempt.stage, attempt.status, attempt.error_code || ""].filter(Boolean).join(" · "), "campaign-card")); });
        (state.selectedReceipts || []).forEach(function (receipt) {
          const item = node("article", "回执：" + (receipt.status || "待验证"), "campaign-card"), evidence = safeEvidencePath(receipt.screenshot_path);
          if (evidence) { const link = node("a", "查看现在", "campaign-button"); link.href = evidence; link.target = "_blank"; link.rel = "noopener"; item.append(link); }
          list.append(item);
        });
        body.append(list);
      } else { return; }
      if (typeof drawer.showModal === "function") { if (!drawer.open) drawer.showModal(); } else drawer.hidden = false;
    }
    function closeDrawer() { const drawer = el("#campaign-drawer"); if (drawer && typeof drawer.close === "function") drawer.close(); else if (drawer) drawer.hidden = true; }
    function wire() {
      all("[data-campaign-filter]").forEach(function (button) {
        button.addEventListener("click", function () { state.filter = button.dataset.campaignFilter; renderCampaigns(); });
      });
      all("[data-campaign-drawer]").forEach(function (button) { button.addEventListener("click", function () { openDrawer(button.dataset.campaignDrawer); }); });
      el("#campaign-drawer-close")?.addEventListener("click", closeDrawer);
      el("#campaign-plan")?.addEventListener("click", planCampaign);
      el("#campaign-reallocate")?.addEventListener("click", reallocateCampaign);
      el("#campaign-lock")?.addEventListener("click", lockPlan);
      el("#campaign-approve")?.addEventListener("click", approveCampaign);
      el("#campaign-resume")?.addEventListener("click", resumeCampaign);
      el("#campaign-cancel")?.addEventListener("click", cancelCampaign);
      el("#campaign-pause")?.addEventListener("click", function () { pauseCampaign(el("#campaign-pause-reason")?.value); });
    }
    async function init() {
      if (state.initialized) return state;
      state.initialized = true;
      wire();
      await refreshSnapshots();
      schedulePoll();
      if (deps.addUnload) deps.addUnload(stopPolling);
      if (deps.addVisibility) deps.addVisibility(function () { if (doc() && doc().hidden) stopPolling(); else if (state.pollTimer === null) schedulePoll(); });
      return state;
    }
    return {state, init, apiPath, poll, schedulePoll, stopPolling, refreshSnapshots, selectCampaign, createCampaign, planCampaign, reallocateCampaign, overrideAssignment, lockPlan, approveCampaign, pauseCampaign, resumeCampaign, cancelCampaign, approveSubmit, rejectSubmit, resolveUnverified, newTemplateDraft, changeTemplateStep, addTemplateStep, removeTemplateStep, moveTemplateStep, saveTemplate, disableTemplate, saveProfileMetadata, saveSettings, openDrawer, closeDrawer};
  }

  return {API_PREFIX, POLL_MS, apiPath, safeEvidencePath, commentCampaignDependencies, createCommentCampaignUI};
});
