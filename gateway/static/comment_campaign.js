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

  function importErrorMessage(error) {
    const labels = {
      tree_name_missing: "缺少评论树名称", tree_name_invalid: "评论树名称无效", tree_empty: "评论树不能为空",
      node_no_missing: "缺少节点序号", node_no_invalid: "节点序号无效", duplicate_node_no: "节点序号重复",
      comment_text_missing: "缺少评论文案", comment_text_too_long: "评论文案过长", parent_not_found: "找不到回复目标",
      root_count_invalid: "盖楼回复必须且只能有一条楼主评论", cycle_detected: "回复关系不能形成循环",
      tree_too_large: "评论树超过 100 条", import_tree_failed: "导入评论树失败", template_invalid: "评论树内容无效",
    };
    const message = labels[(error || {}).code] || "导入内容无效";
    const row = Number((error || {}).row);
    return Number.isInteger(row) && row > 0 ? "第 " + row + " 行：" + message : message;
  }

  function commentCampaignDependencies(win) {
    return {
      document: win.document,
      setTimeout: win.setTimeout.bind(win),
      clearTimeout: win.clearTimeout.bind(win),
      addUnload: function (handler) { win.addEventListener("beforeunload", handler); },
      addVisibility: function (handler) { win.document.addEventListener("visibilitychange", handler); },
      requestJson: async function (url, method, body, options) {
        apiPath(url);
        const isFormData = Boolean(options && options.isFormData);
        const response = await win.fetch(url, {
          method: method || "GET",
          headers: body === undefined || isFormData ? {} : {"Content-Type": "application/json"},
          body: body === undefined ? undefined : (isFormData ? body : JSON.stringify(body)),
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
      campaigns: [], templates: [], profiles: [], profileMeta: {stale: false, last_synced_at: null, safe_reason: null}, health: {}, settings: {},
      selectedCampaignId: "", draftCampaign: {selection_mode: "automatic", profile_refs: []}, selectionPreview: null, draftTemplate: null, readonlyTemplate: null, templateSource: "manual", templateView: "list", disabledTemplatesOpen: false, draftTemplateImport: null, lastTemplateImportResult: null,
      approvalInFlight: new Set(), approvalSent: new Set(), approvalDrafts: {}, renderedApprovalKey: "", pollTimer: null, pollingEnabled: true, profileSyncing: null, error: "",
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
    async function request(path, method, body, options) {
      try {
        const result = await deps.requestJson(apiPath(path), method || "GET", body, options);
        return options && options.envelope ? result : unwrap(result);
      }
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
    function templateModesLabel(modes) {
      const labels = {threaded: "盖楼回复", independent: "独立评论"};
      const visible = (Array.isArray(modes) ? modes : []).map(function (mode) { return labels[mode]; }).filter(Boolean);
      return visible.length ? visible.join("、") : "评论树";
    }
    function templateCommentCount(template) {
      if (Number.isInteger(template.step_count) && template.step_count >= 0) return template.step_count;
      return Array.isArray(template.steps) ? template.steps.length : null;
    }
    function templateSummary(template, status) {
      const parts = [template.name || "未命名评论树", "版本 " + (template.revision || 1), templateModesLabel(template.supported_modes)];
      const count = templateCommentCount(template);
      if (count !== null) parts.push(count + " 条评论");
      if (status) parts.push(status);
      return parts.join(" · ");
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
    async function loadProfilesCache() {
      if (state.profileSyncing) return state.profileSyncing;
      const epoch = (state.snapshotEpoch.profiles || 0) + 1;
      state.snapshotEpoch.profiles = epoch;
      const result = await request(API_PREFIX + "/comment-profile-metadata", "GET", undefined, {envelope: true});
      if (state.snapshotEpoch.profiles !== epoch) return false;
      if (!ok(result, [200])) { setMessage(errorMessage(result, "无法读取 Profile 缓存")); return false; }
      const envelope = result.data || {};
      state.profiles = Array.isArray(envelope.data) ? envelope.data : [];
      state.profileMeta = {...state.profileMeta, ...(envelope.meta || {})};
      return true;
    }
    function setProfilesEnvelope(envelope) {
      const value = envelope || {};
      state.profiles = Array.isArray(value.data) ? value.data : [];
      state.profileMeta = {...state.profileMeta, ...(value.meta || {})};
    }
    async function syncProfiles() {
      if (state.profileSyncing) return state.profileSyncing;
      let operation;
      operation = (async function () {
        const epoch = (state.snapshotEpoch.profiles || 0) + 1;
        state.snapshotEpoch.profiles = epoch;
        try {
          const result = await request(API_PREFIX + "/comment-profile-metadata/sync", "POST", {}, {envelope: true});
          if (state.snapshotEpoch.profiles !== epoch) return false;
          if (!ok(result, [200])) { setMessage(errorMessage(result, "无法同步 Profile 缓存")); return false; }
          setProfilesEnvelope(result.data);
          renderSnapshots();
          return true;
        } finally {
          if (state.profileSyncing === operation) state.profileSyncing = null;
        }
      })();
      state.profileSyncing = operation;
      return operation;
    }
    async function refreshSnapshots() {
      const reads = await Promise.all([
        loadSnapshot("campaigns", API_PREFIX + "/comment-campaigns", []),
        loadSnapshot("templates", API_PREFIX + "/comment-templates", []),
        loadProfilesCache(),
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
    function planCampaign() { if (!canPlanOrLock()) return false; const campaign = selectedCampaign(); return campaignAction("plan", {allocation_seed: ""}, campaign && campaign.revision); }
    function reallocateCampaign() { const campaign = selectedCampaign(); return campaignAction("reallocate", {allocation_seed: ""}, campaign && campaign.revision); }
    function lockPlan() { if (!canPlanOrLock()) return false; const campaign = selectedCampaign(); return campaignAction("lock-plan", {}, campaign && campaign.revision); }
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
    function campaignDraft() {
      const draft = state.draftCampaign || {};
      const selectionMode = draft.selection_mode === "manual" ? "manual" : "automatic";
      if (!Array.isArray(draft.profile_refs) || draft.selection_mode !== selectionMode) {
        state.draftCampaign = {...draft, selection_mode: selectionMode, profile_refs: Array.isArray(draft.profile_refs) ? draft.profile_refs : []};
      }
      return state.draftCampaign;
    }
    function selectedDraftTemplate(draft) {
      return state.templates.find(function (template) { return template.id === draft.template_id; }) || null;
    }
    function selectionStatus(draft) {
      const template = selectedDraftTemplate(draft);
      const required = Number((state.selectionPreview || {}).required_count) || templateCommentCount(template || {}) || 0;
      const selected = Array.isArray(draft.profile_refs) ? draft.profile_refs.length : 0;
      const eligible = draft.selection_mode === "automatic" ? Number((state.selectionPreview || {}).eligible_count) || 0 : state.profiles.length;
      return {required, selected, eligible, shortage: Math.max(0, required - selected)};
    }
    function canCreateCampaign() {
      const draft = campaignDraft();
      const status = selectionStatus(draft);
      return Boolean(draft.name && draft.target_reference && draft.template_id && status.required > 0 && status.selected >= status.required);
    }
    function canPlanOrLock() {
      return !state.draftCampaign || selectionStatus(campaignDraft()).shortage === 0;
    }
    async function refreshSelectionPreview() {
      const draft = campaignDraft();
      if (draft.selection_mode !== "automatic") { state.selectionPreview = null; return false; }
      const template = selectedDraftTemplate(draft);
      if (!template || !draft.mode) { state.selectionPreview = null; state.draftCampaign = {...draft, profile_refs: []}; return false; }
      const key = [draft.template_id, template.revision, draft.mode, draft.selection_mode].join(":");
      const epoch = (state.snapshotEpoch.selectionPreview || 0) + 1;
      state.snapshotEpoch.selectionPreview = epoch;
      const result = await request(API_PREFIX + "/comment-profile-selection/preview", "POST", {template_id: draft.template_id, template_revision: template.revision, mode: draft.mode});
      const current = campaignDraft(), currentTemplate = selectedDraftTemplate(current);
      if (state.snapshotEpoch.selectionPreview !== epoch || current.selection_mode !== "automatic" || [current.template_id, currentTemplate && currentTemplate.revision, current.mode, current.selection_mode].join(":") !== key) return false;
      if (!ok(result, [200])) { state.selectionPreview = null; state.draftCampaign = {...current, profile_refs: []}; setMessage(errorMessage(result, "无法预览自动选择的 Profile")); return false; }
      const preview = result.data || {}, profiles = Array.isArray(preview.profiles) ? preview.profiles : [];
      state.selectionPreview = {required_count: Number(preview.required_count) || 0, eligible_count: Number(preview.eligible_count) || 0};
      state.draftCampaign = {...current, profile_refs: profiles.map(function (profile) { return profile.profile_ref; }).filter(function (value) { return typeof value === "string" && value; })};
      return true;
    }
    async function createCampaign() {
      const draft = campaignDraft();
      if (!canCreateCampaign()) { setMessage("请先选择足够的 Profile 候选池"); return false; }
      const body = {
        name: String(draft.name || "").trim(), mode: draft.mode || "independent",
        target_source: "manual_url", target_reference: String(draft.target_reference || "").trim(),
        template_id: String(draft.template_id || "").trim(),
        profile_refs: Array.isArray(draft.profile_refs) ? draft.profile_refs.slice() : [],
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
    function treeEditor() {
      if (deps.commentTreeEditor) return deps.commentTreeEditor;
      if (root && root.CommentTreeEditor) return root.CommentTreeEditor;
      if (typeof require === "function") return require("./comment_tree_editor");
      return null;
    }
    function newTemplateDraft() { return treeEditor().createDraft(undefined, "threaded"); }
    function templatePayload(draft) { return treeEditor().templatePayload(draft || state.draftTemplate || newTemplateDraft()); }
    function templateValidation(draft) { return treeEditor().validate(draft || state.draftTemplate || newTemplateDraft()); }
    function templateValidationMessage(errors) {
      const labels = {tree_name_missing: "请填写评论树名称", tree_name_invalid: "评论树名称不能超过 100 个字符", tree_size_invalid: "评论数量应为 1 至 100 条", root_count_invalid: "盖楼回复必须且只能有一条楼主评论", independent_parent_invalid: "独立评论不能设置回复关系", comment_text_missing: "请填写每条评论文案", comment_text_too_long: "单条评论不能超过 2200 个字符", parent_not_found: "回复目标不存在", cycle_detected: "回复关系不能形成循环"};
      return errors.map(function (item) { return labels[item.code] || "评论树内容无效"; }).filter(function (value, index, values) { return values.indexOf(value) === index; }).join("；");
    }
    function changeTemplateStep(index, field, value) {
      const draft = state.draftTemplate || newTemplateDraft();
      const nodes = (draft.nodes || []).map(function (item) { return {...item}; });
      if (!nodes[index]) return false;
      if (field === "id") nodes[index].id = value;
      else if (field === "fixed_text") nodes[index].text = value;
      else if (field === "parent_step_id") nodes[index].parentId = value || null;
      else nodes[index][field] = value;
      state.draftTemplate = {...draft, nodes: nodes};
      return true;
    }
    function addTemplateStep() { state.draftTemplate = treeEditor().addReply(state.draftTemplate || newTemplateDraft()); return true; }
    function removeTemplateStep(index) {
      const draft = state.draftTemplate || newTemplateDraft();
      const target = (draft.nodes || [])[index];
      if (!target || draft.nodes.length <= 1) return false;
      try { state.draftTemplate = treeEditor().removeNode(draft, target.id, {removeDescendants: true}); return true; } catch (_) { return false; }
    }
    function moveTemplateStep(index, direction) {
      const draft = state.draftTemplate || newTemplateDraft();
      const target = (draft.nodes || [])[index];
      if (!target) return false;
      state.draftTemplate = treeEditor().moveNode(draft, target.id, direction);
      return true;
    }
    function importCommitPayload(trees) {
      return {trees: (Array.isArray(trees) ? trees : []).filter(function (tree) { return tree && tree.valid === true && tree.selected === true; }).map(function (tree) {
        return {name: String(tree.name || "").trim(), nodes: (Array.isArray(tree.nodes) ? tree.nodes : []).map(function (item) {
          const parent = item.parent_node_no;
          return {node_no: String(item.node_no === null || item.node_no === undefined ? "" : item.node_no), parent_node_no: parent === null || parent === undefined || parent === "" ? null : String(parent), text: String(item.text || "")};
        })};
      })};
    }
    async function previewTemplateImport(file) {
      if (!file) { setMessage("请选择 Excel 文件"); return false; }
      const FormDataCtor = deps.FormData || (typeof FormData === "function" ? FormData : null);
      if (!FormDataCtor) { setMessage("当前浏览器不支持文件导入"); return false; }
      const form = new FormDataCtor();
      form.append("file", file);
      const result = await request(API_PREFIX + "/comment-template-imports/preview", "POST", form, {isFormData: true});
      if (!ok(result, [200])) { setMessage(errorMessage(result, "导入预览失败")); return false; }
      state.draftTemplateImport = {trees: (result.data.trees || []).map(function (tree) { return {...tree, selected: tree.valid === true}; }), summary: result.data.summary || {}};
      state.lastTemplateImportResult = null;
      return true;
    }
    async function commitTemplateImport() {
      const body = importCommitPayload((state.draftTemplateImport || {}).trees);
      if (!body.trees.length) { setMessage("请至少选择一棵有效评论树"); return false; }
      const result = await request(API_PREFIX + "/comment-template-imports", "POST", body);
      if (!ok(result, [201])) {
        setMessage(errorMessage(result, "导入评论树失败"));
        if (result.status === 409) {
          await loadSnapshot("templates", API_PREFIX + "/comment-templates", []);
          openDrawer("template");
        }
        return false;
      }
      const created = Array.isArray(result.data.created) ? result.data.created : [];
      const rejected = Array.isArray(result.data.rejected) ? result.data.rejected : [];
      state.lastTemplateImportResult = result.data;
      if (rejected.length) {
        const rejectedByName = new Map(rejected.map(function (item) { return [item.name, item.errors || []]; }));
        state.draftTemplateImport = {...state.draftTemplateImport, trees: state.draftTemplateImport.trees.filter(function (tree) { return rejectedByName.has(tree.name); }).map(function (tree) { return {...tree, valid: false, selected: false, errors: rejectedByName.get(tree.name)}; })};
      } else {
        state.draftTemplateImport = null;
      }
      if (created.length) await loadSnapshot("templates", API_PREFIX + "/comment-templates", []);
      state.templateView = rejected.length ? "create" : "list";
      openDrawer("template");
      if (rejected.length) setMessage(created.length ? "部分评论树导入成功，请处理其余失败项" : "评论树未导入，请处理失败项");
      return created.length > 0;
    }
    async function saveTemplate(payload, errors) {
      const draft = state.draftTemplate || {};
      const validationErrors = errors || templateValidation(draft);
      if (validationErrors.length) { setMessage(templateValidationMessage(validationErrors)); return false; }
      const body = payload || templatePayload(draft);
      const editingId = draft.editingTemplateId;
      const result = editingId
        ? await request(API_PREFIX + "/comment-templates/" + encodeURIComponent(editingId), "PUT", {...body, expected_revision: draft.expectedRevision})
        : await request(API_PREFIX + "/comment-templates", "POST", body);
      if (!ok(result, editingId ? [200] : [201])) {
        setMessage(errorMessage(result, "保存评论树失败"));
        if (result.status === 409) {
          await loadSnapshot("templates", API_PREFIX + "/comment-templates", []);
          openDrawer("template");
        }
        return false;
      }
      state.draftTemplate = null;
      await loadSnapshot("templates", API_PREFIX + "/comment-templates", []);
      state.templateView = "list";
      openDrawer("template");
      return true;
    }
    function draftFromTemplate(template) {
      const steps = Array.isArray(template.steps) ? template.steps : [];
      const mode = (template.supported_modes || []).includes("threaded") ? "threaded" : "independent";
      return {name: template.name || "", description: template.description || "", language: template.language || "", tags: Array.isArray(template.tags) ? template.tags.slice() : [], mode: mode, source: "manual", advanced: false, editingTemplateId: template.id, expectedRevision: template.revision, nodes: steps.map(function (step) { return {id: step.id, label: step.label || "", text: step.fixed_text || "", parentId: step.parent_step_id || null, requiredProfileTags: Array.isArray(step.required_profile_tags) ? step.required_profile_tags.slice() : [], excludedProfileTags: Array.isArray(step.excluded_profile_tags) ? step.excluded_profile_tags.slice() : [], language: step.language || ""}; })};
    }
    async function editTemplate(template) {
      const result = await request(API_PREFIX + "/comment-templates/" + encodeURIComponent(template.id), "GET");
      if (!ok(result, [200])) { setMessage(errorMessage(result, "无法读取评论树")); return false; }
      const detail = result.data || {};
      if ((detail.steps || []).some(function (step) { return step.content_source === "library"; }) || (detail.supported_modes || []).length !== 1) {
        state.readonlyTemplate = detail;
        state.templateSource = "manual";
        state.templateView = "create";
        setMessage((detail.steps || []).some(function (step) { return step.content_source === "library"; }) ? "文案库评论树为只读，不能转换为固定文案" : "支持多种模式的评论树为只读，不能自动折叠模式");
        openDrawer("template");
        return false;
      }
      state.readonlyTemplate = null;
      state.draftTemplate = draftFromTemplate(detail);
      state.templateSource = "manual";
      state.templateView = "create";
      openDrawer("template");
      return true;
    }
    async function disableTemplate(template) {
      const result = await request(API_PREFIX + "/comment-templates/" + encodeURIComponent(template.id) + "/disable", "POST", {expected_revision: template.revision});
      if (!ok(result, [200])) { setMessage(errorMessage(result, "停用评论树失败")); if (result.status === 409) await loadSnapshot("templates", API_PREFIX + "/comment-templates", []); return false; }
      await loadSnapshot("templates", API_PREFIX + "/comment-templates", []);
      openDrawer("template");
      return true;
    }
    async function enableTemplate(template) {
      const result = await request(API_PREFIX + "/comment-templates/" + encodeURIComponent(template.id) + "/enable", "POST", {expected_revision: template.revision});
      if (!ok(result, [200])) { setMessage(errorMessage(result, "启用评论树失败")); if (result.status === 409) await loadSnapshot("templates", API_PREFIX + "/comment-templates", []); return false; }
      await loadSnapshot("templates", API_PREFIX + "/comment-templates", []);
      return true;
    }
    async function deleteTemplate(template, confirmDelete) {
      const confirm = confirmDelete || deps.confirm || (root && root.confirm);
      if (typeof confirm !== "function" || !confirm("删除后将不再显示，且无法在界面恢复，是否继续？")) return false;
      const result = await request(API_PREFIX + "/comment-templates/" + encodeURIComponent(template.id) + "/delete", "POST", {expected_revision: template.revision});
      if (!ok(result, [200])) { setMessage(errorMessage(result, "删除评论树失败")); if (result.status === 409) await loadSnapshot("templates", API_PREFIX + "/comment-templates", []); return false; }
      await loadSnapshot("templates", API_PREFIX + "/comment-templates", []);
      return true;
    }
    async function saveProfileMetadata(profile, changes) {
      const body = {profile_ref: profile.profile_ref, expected_username: String(profile.expected_username || "").trim(), enabled: Boolean(changes.enabled), login_verified: Boolean(profile.login_verified), tags: String(changes.tags || "").split(",").map(function (value) { return value.trim(); }).filter(Boolean), language: String(changes.language || "").trim(), region: String(changes.region || "").trim(), cooldown_until: String(changes.cooldown_until || "").trim() || null, health_status: String(changes.health_status || "unknown")};
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
      const reasons = {connected: "连接正常", timeout: "连接超时", connection_refused: "连接被拒绝", authentication_failed: "认证失败", invalid_response: "响应无效", not_configured: "尚未配置"};
      ["adspower", "redis", "worker", "sqlite"].forEach(function (name) {
        const service = state.health[name] || {};
        const status = ["connected", "unavailable"].includes(service.status) ? service.status : "unknown";
        const item = node("span", name === "adspower" ? "AdsPower" : name.toUpperCase(), "campaign-health " + status);
        item.append("：" + (status === "connected" ? "已连接" : status === "unavailable" ? "不可用" : "未知") + (reasons[service.reason] ? "（" + reasons[service.reason] + "）" : ""));
        target.append(item);
      });
    }
    function displayProfile(assignment) {
      return assignment.display_profile || "未分配";
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
        const summary = node("p", [campaign.mode === "threaded" ? "盖楼回复" : "独立评论", campaign.video_id ? "视频 " + campaign.video_id : "", campaign.assignment_count ? "计划 " + campaign.assignment_count + " 条" : ""].filter(Boolean).join(" · "), "campaign-muted");
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
        card.append(node("h3", assignment.step_label || "评论步骤"));
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
      const failure = assignments.map(function (assignment) { return (assignment.evidence || {}).identity_failure; }).find(function (value) {
        return value && Array.isArray(value.display_profiles) && value.display_profiles.length === 2 && value.display_profiles.every(function (profile) { return typeof profile === "string"; }) && typeof value.visible_username === "string";
      });
      if (failure) {
        const notice = node("article", undefined, "campaign-identity-failure");
        notice.append(node("strong", "检测到重复的 TikTok 账号"));
        notice.append(node("p", "受影响窗口：" + failure.display_profiles[0] + "、" + failure.display_profiles[1]));
        notice.append(node("p", "可见账号：" + failure.visible_username));
        target.append(notice);
        return;
      }
      const byId = new Map(assignments.map(function (assignment) { return [assignment.assignment_id || assignment.id, assignment]; }));
      function depth(assignment) {
        let result = 0, parent = assignment.parent_assignment_id, seen = new Set();
        while (parent && byId.has(parent) && !seen.has(parent)) { seen.add(parent); result += 1; parent = byId.get(parent).parent_assignment_id; }
        return result;
      }
      assignments.forEach(function (assignment) {
        const row = node("li", undefined, "campaign-preview-row");
        row.style.marginInlineStart = (depth(assignment) * 22) + "px";
        row.append(node("strong", assignment.step_label || "评论步骤"));
        row.append(node("span", "角色：" + (assignment.role || "待分配")));
        row.append(node("span", "Profile：" + displayProfile(assignment)));
        row.append(node("span", assignment.parent_assignment_id ? "引用上一条评论" : "楼主评论"));
        row.append(node("p", assignment.resolved_text || assignment.comment_text || "待冻结文本"));
        if (assignment.status === "planned") {
          const select = node("select");
          state.profiles.forEach(function (profile, index) { const option = node("option", profile.display_profile || "未命名 Profile"); option.value = String(index); option.selected = profile.profile_ref === assignment.profile_ref; select.append(option); });
          const replace = node("button", "单条换号", "campaign-button"); replace.type = "button";
          replace.addEventListener("click", function () { const profile = state.profiles[Number(select.value)]; overrideAssignment(assignment.assignment_id || assignment.id, assignment.revision, profile && profile.profile_ref); });
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
    function profileCacheMessage() {
      if (state.profileMeta.stale !== true) return "";
      const reasons = {timeout: "AdsPower 连接超时", connection_refused: "AdsPower 未运行或拒绝连接", authentication_failed: "AdsPower 认证失败", invalid_response: "AdsPower 响应无效", not_configured: "AdsPower 尚未配置"};
      return "当前展示缓存数据，实际执行前需要 AdsPower 恢复" + (reasons[state.profileMeta.safe_reason] ? "（" + reasons[state.profileMeta.safe_reason] + "）" : "（AdsPower 状态未知）");
    }
    function profileWindowReason(profile) {
      if (profile.enabled === false) return "此窗口已停用";
      if (profile.health_status === "unhealthy") return "此窗口健康状态异常";
      if (profile.cooldown_until) return "此窗口仍在冷却中";
      return "此窗口当前可用于候选池";
    }
    function updateManualProfileSelection(index, checked) {
      const draft = campaignDraft(), refs = Array.isArray(draft.profile_refs) ? draft.profile_refs.slice() : [];
      const ref = (state.profiles[index] || {}).profile_ref;
      if (typeof ref !== "string" || !ref) return;
      const next = checked ? refs.concat(ref).filter(function (value, position, values) { return values.indexOf(value) === position; }) : refs.filter(function (value) { return value !== ref; });
      state.draftCampaign = {...draft, selection_mode: "manual", profile_refs: next};
    }
    function drawerText(title, text) {
      const heading = el("#campaign-drawer-title"), body = el("#campaign-drawer-body");
      if (!heading || !body) return;
      heading.textContent = title;
      clear(body); if (text) body.append(node("p", text));
    }
    function renderTemplateList(body) {
      const enabled = state.templates.filter(function (template) { return template.enabled; });
      const disabled = state.templates.filter(function (template) { return !template.enabled; });
      const create = node("button", "新建评论树", "campaign-button primary");
      create.type = "button";
      create.addEventListener("click", function () { state.templateView = "create"; state.readonlyTemplate = null; state.draftTemplate = newTemplateDraft(); state.templateSource = "manual"; openDrawer("template"); });
      body.append(create);
      const enabledList = node("div", undefined, "comment-tree-list");
      if (!enabled.length) enabledList.append(node("p", "暂无启用中的评论树", "campaign-muted"));
      enabled.forEach(function (template) {
        const row = node("article", undefined, "comment-tree-row");
        row.append(node("span", templateSummary(template, "已启用"), "comment-tree-row-summary"));
        const actions = node("div", undefined, "comment-tree-row-actions");
        const edit = node("button", "编辑", "campaign-button"); edit.type = "button"; edit.addEventListener("click", function () { editTemplate(template); });
        const disable = node("button", "停用", "campaign-button"); disable.type = "button"; disable.addEventListener("click", async function () { await disableTemplate(template); openDrawer("template"); });
        actions.append(edit, disable); row.append(actions); enabledList.append(row);
      });
      body.append(enabledList);
      const group = node("details", undefined, "comment-tree-disabled"); group.open = state.disabledTemplatesOpen;
      group.addEventListener("toggle", function () { state.disabledTemplatesOpen = group.open; });
      const summary = node("summary", "已停用（" + disabled.length + "）"); group.append(summary);
      const disabledList = node("div", undefined, "comment-tree-list");
      if (!disabled.length) disabledList.append(node("p", "暂无已停用评论树", "campaign-muted"));
      disabled.forEach(function (template) {
        const row = node("article", undefined, "comment-tree-row");
        row.append(node("span", templateSummary(template, "已停用"), "comment-tree-row-summary"));
        const actions = node("div", undefined, "comment-tree-row-actions");
        const enable = node("button", "启用", "campaign-button"); enable.type = "button"; enable.addEventListener("click", async function () { await enableTemplate(template); openDrawer("template"); });
        const remove = node("button", "删除", "campaign-button"); remove.type = "button"; remove.addEventListener("click", async function () { await deleteTemplate(template); openDrawer("template"); });
        actions.append(enable, remove); row.append(actions); disabledList.append(row);
      });
      group.append(disabledList); body.append(group);
    }
    function renderTemplateCreate(body) {
      const back = node("button", "返回评论树列表", "campaign-button"); back.type = "button";
      back.addEventListener("click", function () { state.templateView = "list"; openDrawer("template"); }); body.append(back);
      const choices = node("div", undefined, "comment-tree-create-choices");
      [["manual", "逐条手动创建"], ["excel", "Excel 导入"]].forEach(function (choice) {
        const button = node("button", choice[1], state.templateSource === choice[0] ? "campaign-button primary" : "campaign-button");
        button.type = "button";
        button.addEventListener("click", function () { state.templateSource = choice[0]; if (choice[0] === "manual" && !state.draftTemplate && !state.readonlyTemplate) state.draftTemplate = newTemplateDraft(); openDrawer("template"); });
        choices.append(button);
      });
      body.append(choices);
      if ((state.templateSource || "manual") === "excel") {
        const fileLabel = node("label", "Excel 文件（.xlsx）"), file = node("input"); file.type = "file"; file.accept = ".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"; fileLabel.append(file); body.append(fileLabel);
        const preview = node("button", "预览导入", "campaign-button"); preview.type = "button"; preview.addEventListener("click", async function () { if (await previewTemplateImport(file.files && file.files[0])) openDrawer("template"); }); body.append(preview);
        const previewData = state.draftTemplateImport;
        if (previewData) {
          const previewList = node("div", undefined, "campaign-list");
          (previewData.trees || []).forEach(function (tree, index) {
            const item = node("article", undefined, "campaign-card");
            const select = node("input"); select.type = "checkbox"; select.checked = tree.selected === true; select.disabled = tree.valid !== true;
            select.addEventListener("change", function () { state.draftTemplateImport = {...previewData, trees: previewData.trees.map(function (candidate, position) { return position === index ? {...candidate, selected: candidate.valid === true && select.checked} : candidate; })}; });
            item.append(select, node("strong", tree.name || "未命名评论树"), node("p", tree.valid ? "可导入" : "不可导入"));
            (tree.errors || []).forEach(function (error) { item.append(node("p", importErrorMessage(error), "campaign-error")); }); previewList.append(item);
          });
          const commit = node("button", "导入已选评论树", "campaign-button primary"); commit.type = "button"; commit.addEventListener("click", commitTemplateImport); body.append(previewList, commit);
        }
      } else if (state.readonlyTemplate) {
        body.append(node("p", "此评论树当前仅支持只读和停用，不能自动转换为固定文案或折叠模式。", "campaign-muted"));
        if (state.draftTemplate) { const resume = node("button", "继续未保存草稿", "campaign-button"); resume.type = "button"; resume.addEventListener("click", function () { state.readonlyTemplate = null; openDrawer("template"); }); body.append(resume); }
      } else {
        if (!state.draftTemplate) state.draftTemplate = newTemplateDraft();
        const editorHost = node("div", undefined, "comment-tree-editor-host"); body.append(editorHost);
        treeEditor().render({
          document: doc(), container: editorHost, draft: state.draftTemplate,
          onDraftChange: function (next) { state.draftTemplate = next; }, onSave: saveTemplate,
          confirmModeChange: function () { return Boolean(root && typeof root.confirm === "function" && root.confirm("切换为独立评论会清除回复关系，是否继续？")); },
          confirmRemoveDescendants: function () { return Boolean(root && typeof root.confirm === "function" && root.confirm("删除此评论及其全部回复，是否继续？")); },
        });
      }
    }
    function openDrawer(kind) {
      const drawer = el("#campaign-drawer");
      if (!drawer) return;
      if (kind === "template") {
        drawerText(state.templateView === "create" ? "新建评论树" : "评论树", "");
        const body = el("#campaign-drawer-body");
        if (state.templateView === "create") renderTemplateCreate(body);
        else renderTemplateList(body);
        if (typeof drawer.showModal === "function") { if (!drawer.open) drawer.showModal(); }
        else { drawer.hidden = false; drawer.setAttribute("open", ""); }
        return;
      }
      if (kind === "create") {
        drawerText("新建 Campaign", "选择模式、视频、模板、授权 Profile 和分配计划后，仍需人工锁定并审批。");
        const body = el("#campaign-drawer-body");
        const fields = [["name", "名称", "input"], ["target_reference", "视频链接", "input"], ["batch_size", "每批数量", "input"]];
        fields.forEach(function (item) { const label = node("label", item[1]), input = node(item[2]); input.value = (state.draftCampaign || {})[item[0]] || (item[0] === "batch_size" ? "3" : ""); input.autocomplete = "off"; input.addEventListener("input", function () { state.draftCampaign = {...(state.draftCampaign || {}), [item[0]]: input.value}; }); label.append(input); body.append(label); });
        const draft = campaignDraft();
        const selection = node("fieldset", undefined, "profile-selection");
        selection.append(node("legend", "Profile 选择"));
        [
          ["automatic", "自动选择"], ["manual", "手动选择"],
        ].forEach(function (choice) {
          const label = node("label", choice[1]), radio = node("input");
          radio.type = "radio"; radio.checked = draft.selection_mode === choice[0];
          radio.addEventListener("change", function () {
            if (!radio.checked) return;
            state.draftCampaign = {...campaignDraft(), selection_mode: choice[0], profile_refs: []};
            state.selectionPreview = null;
            if (choice[0] === "manual") state.snapshotEpoch.selectionPreview = (state.snapshotEpoch.selectionPreview || 0) + 1;
            if (choice[0] === "automatic") refreshSelectionPreview().then(function () { openDrawer("create"); });
            else openDrawer("create");
          });
          label.append(radio); selection.append(label);
        });
        const selectionInfo = selectionStatus(draft);
        selection.append(node("p", "需要 " + selectionInfo.required + " 个 · 已选择 " + selectionInfo.selected + " 个 · 当前可用 " + selectionInfo.eligible + " 个", "campaign-muted"));
        if (selectionInfo.shortage) selection.append(node("p", "还缺 " + selectionInfo.shortage + " 个 Profile，草稿会保留，但不能创建、规划或锁定。", "campaign-selection-shortage"));
        if (draft.selection_mode === "manual") {
          const cards = node("div", undefined, "profile-selection-cards");
          state.profiles.forEach(function (profile, index) {
            const card = node("label", undefined, "profile-selection-card"), checkbox = node("input");
            checkbox.type = "checkbox"; checkbox.checked = draft.profile_refs.includes(profile.profile_ref);
            checkbox.addEventListener("change", function () { updateManualProfileSelection(index, checkbox.checked); openDrawer("create"); });
            card.append(checkbox, node("span", profile.display_profile || "未命名 Profile"), node("small", profileWindowReason(profile)));
            cards.append(card);
          });
          selection.append(cards);
        }
        const cacheMessage = profileCacheMessage();
        if (cacheMessage) selection.append(node("p", cacheMessage, "campaign-cache-warning"));
        body.append(selection);
        const templateLabel = node("label", "评论树");
        const templateSelect = node("select");
        templateSelect.dataset.field = "template";
        const placeholder = node("option", "请选择评论树"); placeholder.value = ""; templateSelect.append(placeholder);
        state.templates.filter(function (template) { return template.enabled; }).forEach(function (template) {
          const option = node("option", templateSummary(template));
          option.value = template.id;
          option.selected = option.value === ((state.draftCampaign || {}).template_id || "");
          templateSelect.append(option);
        });
        templateSelect.value = ((state.draftCampaign || {}).template_id || "");
        templateSelect.addEventListener("change", function () { state.draftCampaign = {...campaignDraft(), template_id: templateSelect.value, profile_refs: []}; state.selectionPreview = null; if (campaignDraft().selection_mode === "automatic") refreshSelectionPreview().then(function () { openDrawer("create"); }); else openDrawer("create"); });
        templateLabel.append(templateSelect); body.append(templateLabel);
        const mode = node("select"); ["independent", "threaded"].forEach(function (value) { const option = node("option", value === "independent" ? "独立评论" : "盖楼回复"); option.value = value; option.selected = ((state.draftCampaign || {}).mode || "independent") === value; mode.append(option); }); mode.addEventListener("change", function () { state.draftCampaign = {...campaignDraft(), mode: mode.value, profile_refs: []}; state.selectionPreview = null; if (campaignDraft().selection_mode === "automatic") refreshSelectionPreview().then(function () { openDrawer("create"); }); else openDrawer("create"); }); const modeLabel = node("label", "模式"); modeLabel.append(mode); body.append(modeLabel);
        const save = node("button", "创建 Campaign", "campaign-button primary"); save.type = "button"; save.disabled = !canCreateCampaign(); save.addEventListener("click", createCampaign); body.append(save);
        body.append(node("p", "草稿仅保存在当前页面内；轮询不会覆盖正在输入的内容。", "campaign-muted"));
      } else if (kind === "profile") {
        drawerText("Profile 元数据", "仅显示已脱敏 Profile、标签、语言、地区、启用、健康状态和冷却时间。");
        const body = el("#campaign-drawer-body"), list = node("div", undefined, "campaign-list");
        const sync = node("button", "同步 Profile", "campaign-button");
        sync.type = "button"; sync.disabled = Boolean(state.profileSyncing);
        sync.addEventListener("click", async function () { await syncProfiles(); openDrawer("profile"); });
        body.append(sync);
        state.profiles.forEach(function (profile) {
          const summary = [profile.display_profile || "未命名 Profile", profile.language || "未设置语言", profile.region || "未设置地区", profileWindowReason(profile)].join(" · ");
          const item = node("article", summary, "campaign-card"), values = {tags: (profile.tags || []).join(","), language: profile.language || "", region: profile.region || "", cooldown_until: profile.cooldown_until || "", health_status: profile.health_status || "unknown", enabled: profile.enabled};
          [["tags", "标签"], ["language", "语言"], ["region", "地区"], ["cooldown_until", "冷却至"]].forEach(function (field) { const input = node("input"); input.value = values[field[0]]; input.addEventListener("input", function () { values[field[0]] = input.value; }); const label = node("label", field[1]); label.append(input); item.append(label); });
          const enabled = node("input"); enabled.type = "checkbox"; enabled.checked = values.enabled; enabled.addEventListener("change", function () { values.enabled = enabled.checked; }); const enabledLabel = node("label", "启用窗口"); enabledLabel.append(enabled); item.append(enabledLabel);
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
          list.append(node("article", [assignment.step_label || "评论步骤", assignment.status, assignment.display_profile || "未分配"].filter(Boolean).join(" · "), "campaign-card"));
        });
        (state.selectedAttempts || []).forEach(function (attempt) { list.append(node("article", ["尝试", attempt.stage, attempt.status, attempt.error_code || ""].filter(Boolean).join(" · "), "campaign-card")); });
        (state.selectedReceipts || []).forEach(function (receipt) {
          const item = node("article", "回执：" + (receipt.status || "待验证"), "campaign-card"), evidence = safeEvidencePath(receipt.screenshot_path);
          if (evidence) { const link = node("a", "查看现在", "campaign-button"); link.href = evidence; link.target = "_blank"; link.rel = "noopener"; item.append(link); }
          list.append(item);
        });
        body.append(list);
      } else { return; }
      if (typeof drawer.showModal === "function") { if (!drawer.open) drawer.showModal(); }
      else { drawer.hidden = false; drawer.setAttribute("open", ""); }
    }
    function closeDrawer() {
      const drawer = el("#campaign-drawer");
      if (drawer && typeof drawer.close === "function") drawer.close();
      else if (drawer) { drawer.hidden = true; drawer.removeAttribute("open"); }
    }
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
      await syncProfiles();
      schedulePoll();
      if (deps.addUnload) deps.addUnload(stopPolling);
      if (deps.addVisibility) deps.addVisibility(function () { if (doc() && doc().hidden) stopPolling(); else if (state.pollTimer === null) schedulePoll(); });
      return state;
    }
    return {state, init, apiPath, poll, schedulePoll, stopPolling, refreshSnapshots, renderSnapshots, syncProfiles, refreshSelectionPreview, selectCampaign, createCampaign, planCampaign, reallocateCampaign, overrideAssignment, lockPlan, approveCampaign, pauseCampaign, resumeCampaign, cancelCampaign, approveSubmit, rejectSubmit, resolveUnverified, newTemplateDraft, changeTemplateStep, addTemplateStep, removeTemplateStep, moveTemplateStep, saveTemplate, disableTemplate, enableTemplate, deleteTemplate, previewTemplateImport, commitTemplateImport, editTemplate, saveProfileMetadata, saveSettings, openDrawer, closeDrawer};
  }

  return {API_PREFIX, POLL_MS, apiPath, safeEvidencePath, importErrorMessage, commentCampaignDependencies, createCommentCampaignUI};
});
