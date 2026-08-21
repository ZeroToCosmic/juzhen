(function (root, factory) {
  "use strict";
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.ConsoleCommentCampaignCreate = api;
  if (root && root.document) {
    if (root.document.readyState === "loading") {
      root.document.addEventListener("DOMContentLoaded", function () { api.boot(root); }, {once: true});
    } else {
      api.boot(root);
    }
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function (root) {
  "use strict";

  const API = "/api/browser-v2";
  const MODES = ["independent", "threaded"];
  const EMPTY_DRAFT = Object.freeze({
    name: "",
    mode: "independent",
    target_reference: "",
    template_id: "",
    template_revision: null,
    selection_mode: "automatic",
    profile_refs: [],
    batch_size: "3",
  });

  function compatibleTemplate(template, mode) {
    return Boolean(
      template &&
      template.enabled !== false &&
      Array.isArray(template.supported_modes) &&
      template.supported_modes.includes(mode),
    );
  }

  function firstAvailableMode(templates, preferred) {
    const records = Array.isArray(templates) ? templates : [];
    if (records.some(function (template) { return compatibleTemplate(template, preferred); })) {
      return preferred;
    }
    return MODES.find(function (mode) {
      return records.some(function (template) { return compatibleTemplate(template, mode); });
    }) || preferred;
  }

  function previewKey(draft) {
    return [
      draft.template_id,
      draft.template_revision || "",
      draft.mode,
      draft.selection_mode,
    ].join(":");
  }

  function isDirectTikTokVideoUrl(value) {
    const raw = typeof value === "string" ? value : "";
    if (!raw || raw.length > 2000) return false;
    const match = raw.trim().match(
      /^https:\/\/(?:tiktok\.com|www\.tiktok\.com)(\/[^?#]*)?(?:\?[^#]*)?$/i,
    );
    if (!match) return false;
    const path = match[1] || "";
    return !path.includes("%") &&
      !path.includes("\\") &&
      !/[\x00-\x1f]/.test(path) &&
      /^\/@[A-Za-z0-9._]{1,24}\/video\/\d{8,30}\/?$/.test(path);
  }

  function validateDraft(draft, context) {
    const value = draft || {};
    const scope = context || {};
    const errors = {};
    const name = String(value.name || "").trim();
    if (!name || name.length > 100) errors.name = "请输入 1 至 100 个字符的 Campaign 名称。";
    if (!isDirectTikTokVideoUrl(value.target_reference)) {
      errors.target_reference = "请输入 HTTPS TikTok 直接视频链接。";
    }
    if (!["independent", "threaded"].includes(value.mode)) errors.mode = "请选择有效的评论模式。";
    const template = (Array.isArray(scope.templates) ? scope.templates : []).find(function (item) {
      return String(item.id || "") === String(value.template_id || "");
    });
    if (
      !compatibleTemplate(template, value.mode) ||
      Number(template && template.revision) !== Number(value.template_revision)
    ) {
      errors.template_id = "请选择当前模式可用的评论树。";
    }
    const batchSize = Number(value.batch_size);
    if (!Number.isInteger(batchSize) || batchSize < 1 || batchSize > 8) {
      errors.batch_size = "每批数量必须是 1 至 8 的整数。";
    }
    const refs = Array.isArray(value.profile_refs) ? value.profile_refs : [];
    const knownRefs = new Set((Array.isArray(scope.profiles) ? scope.profiles : []).map(function (item) {
      return item && item.profile_ref;
    }));
    if (
      refs.length === 0 ||
      refs.length !== new Set(refs).size ||
      refs.some(function (ref) {
        return typeof ref !== "string" || !ref || ref.length > 80 || !knownRefs.has(ref);
      })
    ) {
      errors.profile_refs = "请选择有效且不重复的 Profile。";
    }
    const preview = scope.preview || {};
    if (preview.status !== "ready" || preview.inputKey !== previewKey(value)) {
      errors.preview = "请等待 Profile 选择预览完成。";
    } else if (refs.length < Number(preview.requiredCount || 0)) {
      errors.profile_refs = "所选 Profile 数量不足。";
    }
    return errors;
  }

  function buildCreatePayload(draft) {
    const value = draft || {};
    return {
      name: String(value.name || "").trim(),
      mode: value.mode,
      target_source: "manual_url",
      target_reference: String(value.target_reference || "").trim(),
      template_id: String(value.template_id || ""),
      template_revision: Number(value.template_revision),
      profile_refs: Array.isArray(value.profile_refs) ? value.profile_refs.slice() : [],
      batch_size: Number(value.batch_size),
      start_mode: "manual",
    };
  }

  function envelopeData(result) {
    return result && result.data && Object.prototype.hasOwnProperty.call(result.data, "data")
      ? result.data.data
      : undefined;
  }

  function profileStatus(profile) {
    if (profile.enabled === false) return "已停用";
    if (profile.health_status === "unhealthy") return "状态异常";
    if (profile.cooldown_until) return "冷却中";
    return "可用";
  }

  function profileLocale(profile) {
    return [profile.language, profile.region].filter(Boolean).join(" / ") || "未设置";
  }

  function createViewModel(state) {
    const query = state.profileQuery.toLocaleLowerCase("zh-CN");
    const templates = state.templates
      .filter(function (template) { return compatibleTemplate(template, state.draft.mode); })
      .map(function (template) {
        return {
          id: String(template.id || ""),
          label: [template.name || "未命名评论树", "版本 " + Number(template.revision || 1)].join(" · "),
          revision: Number(template.revision || 1),
        };
      });
    const selected = new Set(state.draft.profile_refs);
    const profileRows = state.profiles
      .filter(function (profile) {
        const text = [profile.display_profile, profile.language, profile.region]
          .filter(Boolean)
          .join(" ")
          .toLocaleLowerCase("zh-CN");
        return !query || text.includes(query);
      })
      .map(function (profile) {
        return {
          key: String(profile.profile_ref || ""),
          display: profile.display_profile || "未命名 Profile",
          locale: profileLocale(profile),
          status: profileStatus(profile),
          checked: selected.has(profile.profile_ref),
        };
      });
    const required = state.preview.status === "ready" ? state.preview.requiredCount : null;
    const eligible = state.preview.status === "ready" ? state.preview.eligibleCount : null;
    return {
      templates,
      templateEmpty: templates.length === 0,
      profileRows,
      manual: state.draft.selection_mode === "manual",
      required,
      eligible,
      selected: state.draft.profile_refs.length,
      shortage: required === null ? null : Math.max(0, required - state.draft.profile_refs.length),
    };
  }

  function createConsoleCommentCampaignCreate(options) {
    const opts = options || {};
    if (typeof opts.requestJson !== "function") throw new TypeError("requestJson is required");
    const location = opts.location || {assign: function () {}};
    const renderView = typeof opts.render === "function" ? opts.render : function () {};
    const state = {
      draft: {...EMPTY_DRAFT, profile_refs: []},
      templates: [],
      profiles: [],
      profileMeta: {stale: false, last_synced_at: null, safe_reason: null},
      profileQuery: "",
      preview: {
        version: 0,
        inputKey: "",
        status: "idle",
        requiredCount: 0,
        eligibleCount: 0,
        error: "",
      },
      fieldErrors: {},
      loading: false,
      submitting: false,
      initialized: false,
      modeTouched: false,
      templateLoading: false,
      error: "",
    };

    function render() {
      renderView(state, createViewModel(state));
    }

    function selectedTemplate() {
      return state.templates.find(function (template) {
        return String(template.id || "") === state.draft.template_id;
      }) || null;
    }

    function invalidatePreview() {
      state.preview = {
        version: state.preview.version + 1,
        inputKey: "",
        status: "idle",
        requiredCount: 0,
        eligibleCount: 0,
        error: "",
      };
    }

    function applyTemplates(templates) {
      state.templates = Array.isArray(templates) ? templates : [];
      if (!state.modeTouched) {
        state.draft.mode = firstAvailableMode(state.templates, state.draft.mode);
      }
      const template = selectedTemplate();
      if (!compatibleTemplate(template, state.draft.mode)) {
        state.draft.template_id = "";
        state.draft.template_revision = null;
        state.draft.profile_refs = [];
        invalidatePreview();
        return false;
      }
      const revision = Number(template.revision);
      if (revision === Number(state.draft.template_revision)) return false;
      state.draft.template_revision = revision;
      state.draft.profile_refs = [];
      invalidatePreview();
      return state.draft.selection_mode === "automatic";
    }

    async function refreshTemplates() {
      if (state.templateLoading) return false;
      state.templateLoading = true;
      state.error = "";
      render();
      try {
        const result = await opts.requestJson(API + "/comment-templates", "GET");
        if (!result || result.status !== 200) throw new Error("评论树加载失败");
        const shouldRefreshPreview = applyTemplates(envelopeData(result));
        if (shouldRefreshPreview) await refreshSelectionPreview();
        return true;
      } catch (error) {
        state.error = error && error.message ? error.message : "评论树加载失败";
        return false;
      } finally {
        state.templateLoading = false;
        render();
      }
    }

    async function init() {
      if (state.loading) return false;
      state.loading = true;
      state.error = "";
      render();
      try {
        const results = await Promise.all([
          opts.requestJson(API + "/comment-templates", "GET"),
          opts.requestJson(API + "/comment-profile-metadata", "GET"),
        ]);
        if (!results.every(function (result) { return result && result.status === 200; })) {
          throw new Error("创建页面数据加载失败");
        }
        applyTemplates(envelopeData(results[0]));
        state.profiles = Array.isArray(envelopeData(results[1])) ? envelopeData(results[1]) : [];
        state.profileMeta = {
          ...state.profileMeta,
          ...((results[1].data && results[1].data.meta) || {}),
        };
        state.initialized = true;
        return true;
      } catch (error) {
        state.error = error && error.message ? error.message : "创建页面数据加载失败";
        return false;
      } finally {
        state.loading = false;
        render();
      }
    }

    async function refreshSelectionPreview() {
      const template = selectedTemplate();
      if (!compatibleTemplate(template, state.draft.mode)) {
        invalidatePreview();
        render();
        return false;
      }
      const version = state.preview.version + 1;
      const key = previewKey(state.draft);
      state.preview = {
        version,
        inputKey: key,
        status: "loading",
        requiredCount: 0,
        eligibleCount: 0,
        error: "",
      };
      render();
      let result;
      try {
        result = await opts.requestJson(
          API + "/comment-profile-selection/preview",
          "POST",
          {
            template_id: state.draft.template_id,
            template_revision: state.draft.template_revision,
            mode: state.draft.mode,
          },
        );
      } catch (error) {
        result = {status: 0, data: {error: {message: error && error.message ? error.message : "请求失败"}}};
      }
      if (state.preview.version !== version || previewKey(state.draft) !== key) return false;
      if (!result || result.status !== 200) {
        const error = result && result.data && result.data.error;
        state.preview = {
          ...state.preview,
          status: "error",
          error: (error && (error.message || error.code)) || "无法预览 Profile 选择",
        };
        if (state.draft.selection_mode === "automatic") state.draft.profile_refs = [];
        render();
        return false;
      }
      const preview = envelopeData(result) || {};
      state.preview = {
        ...state.preview,
        status: "ready",
        requiredCount: Number(preview.required_count) || 0,
        eligibleCount: Number(preview.eligible_count) || 0,
        error: "",
      };
      if (state.draft.selection_mode === "automatic") {
        state.draft.profile_refs = (Array.isArray(preview.profiles) ? preview.profiles : [])
          .map(function (profile) { return profile && profile.profile_ref; })
          .filter(function (value, index, values) {
            return typeof value === "string" && value && values.indexOf(value) === index;
          });
      }
      render();
      return true;
    }

    async function updateDraft(field, value) {
      state.error = "";
      delete state.fieldErrors[field];
      if (["name", "target_reference", "batch_size"].includes(field)) {
        state.draft[field] = String(value == null ? "" : value);
        render();
        return true;
      }
      if (field === "mode") {
        if (!MODES.includes(value)) return false;
        state.modeTouched = true;
        state.draft.mode = value;
        if (!compatibleTemplate(selectedTemplate(), value)) {
          state.draft.template_id = "";
          state.draft.template_revision = null;
        }
      } else if (field === "template_id") {
        state.draft.template_id = String(value || "");
        const template = selectedTemplate();
        state.draft.template_revision = template ? Number(template.revision) : null;
      } else if (field === "selection_mode") {
        if (!["automatic", "manual"].includes(value)) return false;
        state.draft.selection_mode = value;
      } else {
        return false;
      }
      state.draft.profile_refs = [];
      invalidatePreview();
      render();
      return compatibleTemplate(selectedTemplate(), state.draft.mode)
        ? refreshSelectionPreview()
        : false;
    }

    function setProfileQuery(value) {
      state.profileQuery = String(value || "").trim().toLocaleLowerCase("zh-CN");
      render();
      return true;
    }

    function toggleProfile(profileRef, checked) {
      if (state.draft.selection_mode !== "manual") return false;
      if (!state.profiles.some(function (profile) { return profile.profile_ref === profileRef; })) return false;
      const refs = state.draft.profile_refs.slice();
      if (checked && !refs.includes(profileRef)) refs.push(profileRef);
      state.draft.profile_refs = checked
        ? refs
        : refs.filter(function (value) { return value !== profileRef; });
      state.error = "";
      delete state.fieldErrors.profile_refs;
      render();
      return true;
    }

    async function submit() {
      if (state.submitting) return false;
      state.fieldErrors = validateDraft(state.draft, {
        templates: state.templates,
        profiles: state.profiles,
        preview: state.preview,
      });
      if (Object.keys(state.fieldErrors).length) {
        state.error = "请检查表单中的必填信息。";
        render();
        return false;
      }
      state.submitting = true;
      state.error = "";
      render();
      try {
        const result = await opts.requestJson(
          API + "/comment-campaigns",
          "POST",
          buildCreatePayload(state.draft),
        );
        if (result && result.status === 201) {
          location.assign("/console/actions");
          return true;
        }
        const error = result && result.data && result.data.error;
        const fallback = {
          403: "当前会话无权创建 Campaign。",
          422: "Campaign 配置无效，请检查表单。",
          503: "Campaign 服务暂不可用，请稍后重试。",
        };
        state.error = (error && (error.message || error.code)) ||
          fallback[result && result.status] ||
          "创建 Campaign 失败，请重试。";
        return false;
      } catch (_) {
        state.error = "请求失败，请重试。";
        return false;
      } finally {
        state.submitting = false;
        render();
      }
    }

    return {
      state,
      init,
      render,
      refreshTemplates,
      updateDraft,
      setProfileQuery,
      toggleProfile,
      refreshSelectionPreview,
      submit,
      location,
    };
  }

  function makeOption(document, value, label, selected) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    option.selected = selected;
    return option;
  }

  function renderDom(document, state, model, controller) {
    const byId = function (id) { return document.getElementById(id); };
    const rootNode = byId("console-comment-campaign-create");
    if (rootNode) rootNode.dataset.state = state.loading ? "loading" : (state.error ? "error" : "ready");
    const values = {
      "campaign-name": state.draft.name,
      "campaign-target-reference": state.draft.target_reference,
      "campaign-mode": state.draft.mode,
      "campaign-batch-size": state.draft.batch_size,
    };
    Object.keys(values).forEach(function (id) {
      const node = byId(id);
      if (node && node.value !== String(values[id])) node.value = String(values[id]);
      if (node) node.disabled = state.loading || state.submitting;
    });
    const template = byId("campaign-template");
    if (template) {
      template.replaceChildren(makeOption(document, "", "请选择评论树", !state.draft.template_id));
      model.templates.forEach(function (item) {
        template.append(makeOption(document, item.id, item.label, item.id === state.draft.template_id));
      });
      template.value = state.draft.template_id;
      template.disabled = state.loading || state.templateLoading || state.submitting;
    }
    const templateRefresh = byId("campaign-template-refresh");
    if (templateRefresh) templateRefresh.disabled = state.loading || state.templateLoading || state.submitting;
    const templateEmpty = byId("campaign-template-empty");
    if (templateEmpty) templateEmpty.hidden = !model.templateEmpty;
    const automatic = byId("campaign-selection-automatic");
    const manual = byId("campaign-selection-manual");
    if (automatic) automatic.checked = !model.manual;
    if (manual) manual.checked = model.manual;
    [automatic, manual].forEach(function (node) { if (node) node.disabled = state.loading || state.submitting; });
    const countValues = {
      "campaign-required-count": model.required,
      "campaign-eligible-count": model.eligible,
      "campaign-selected-count": model.selected,
      "campaign-shortage-count": model.shortage,
    };
    Object.keys(countValues).forEach(function (id) {
      const node = byId(id);
      if (node) node.textContent = countValues[id] === null ? "—" : String(countValues[id]);
    });
    const searchWrap = byId("campaign-profile-search-wrap");
    const tableWrap = byId("campaign-profile-table-wrap");
    const empty = byId("campaign-profile-empty");
    if (searchWrap) searchWrap.hidden = !model.manual;
    if (tableWrap) tableWrap.hidden = !model.manual || model.profileRows.length === 0;
    if (empty) empty.hidden = !model.manual || model.profileRows.length > 0;
    const search = byId("campaign-profile-search");
    if (search && search.value !== state.profileQuery) search.value = state.profileQuery;
    const body = byId("campaign-profile-body");
    if (body) {
      let focusedCheckboxIndex = -1;
      if (document.activeElement && body.contains(document.activeElement)) {
        focusedCheckboxIndex = Array.from(body.querySelectorAll('input[type="checkbox"]'))
          .indexOf(document.activeElement);
      }
      const checkboxes = [];
      body.replaceChildren();
      model.profileRows.forEach(function (row) {
        const tr = document.createElement("tr");
        const choose = document.createElement("td");
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = row.checked;
        checkbox.disabled = state.submitting;
        checkbox.setAttribute("aria-label", "选择 " + row.display);
        checkbox.addEventListener("change", function () { controller.toggleProfile(row.key, checkbox.checked); });
        checkboxes.push(checkbox);
        choose.append(checkbox);
        [row.display, row.locale, row.status].forEach(function (text) {
          const cell = document.createElement("td");
          cell.textContent = text;
          tr.append(cell);
        });
        tr.prepend(choose);
        body.append(tr);
      });
      if (focusedCheckboxIndex >= 0 && checkboxes[focusedCheckboxIndex]) {
        checkboxes[focusedCheckboxIndex].focus();
      }
    }
    const submit = byId("campaign-create-submit");
    if (submit) {
      submit.disabled = state.loading || state.submitting;
      submit.textContent = state.submitting ? "正在创建…" : "创建 Campaign";
    }
    const fieldNodes = {
      name: "campaign-name",
      target_reference: "campaign-target-reference",
      mode: "campaign-mode",
      template_id: "campaign-template",
      batch_size: "campaign-batch-size",
    };
    Object.keys(fieldNodes).forEach(function (field) {
      const control = byId(fieldNodes[field]);
      const error = byId(fieldNodes[field] + "-error");
      if (control) control.setAttribute("aria-invalid", String(Boolean(state.fieldErrors[field])));
      if (error) {
        error.textContent = state.fieldErrors[field] || "";
        error.hidden = !state.fieldErrors[field];
      }
    });
    const profileError = byId("campaign-profile-error");
    if (profileError) {
      profileError.textContent = state.fieldErrors.profile_refs || state.fieldErrors.preview || "";
      profileError.hidden = !profileError.textContent;
    }
    const status = byId("campaign-create-status");
    if (status) {
      status.textContent = state.error || state.preview.error || "";
      status.className = "console-status" + (state.error || state.preview.error ? " error" : "");
    }
  }

  function bindDom(document, controller) {
    const byId = function (id) { return document.getElementById(id); };
    const form = byId("campaign-create-form");
    if (form) form.addEventListener("submit", function (event) { event.preventDefault(); controller.submit(); });
    [
      ["campaign-name", "name", "input"],
      ["campaign-target-reference", "target_reference", "input"],
      ["campaign-batch-size", "batch_size", "input"],
      ["campaign-mode", "mode", "change"],
      ["campaign-template", "template_id", "change"],
    ].forEach(function (binding) {
      const node = byId(binding[0]);
      if (node) node.addEventListener(binding[2], function () { controller.updateDraft(binding[1], node.value); });
    });
    ["automatic", "manual"].forEach(function (mode) {
      const node = byId("campaign-selection-" + mode);
      if (node) node.addEventListener("change", function () {
        if (node.checked) controller.updateDraft("selection_mode", mode);
      });
    });
    const search = byId("campaign-profile-search");
    if (search) search.addEventListener("input", function () { controller.setProfileQuery(search.value); });
    const templateRefresh = byId("campaign-template-refresh");
    if (templateRefresh) templateRefresh.addEventListener("click", function () { controller.refreshTemplates(); });
  }

  async function requestJson(win, url, method, body) {
    const verb = String(method || "GET").toUpperCase();
    const options = {method: verb, credentials: "same-origin"};
    if (body !== undefined && verb !== "GET" && verb !== "HEAD") {
      options.headers = {"Content-Type": "application/json"};
      options.body = JSON.stringify(body);
    }
    const response = await win.fetch(url, options);
    let data;
    try {
      data = await response.json();
    } catch (_) {
      data = {error: {code: "invalid_response", message: "服务返回格式无效"}};
    }
    return {status: response.status, data};
  }

  function boot(win) {
    const browser = win || root;
    const document = browser && browser.document;
    if (!document || !document.getElementById("console-comment-campaign-create")) return null;
    if (browser.__consoleCommentCampaignCreateController) return browser.__consoleCommentCampaignCreateController;
    let controller;
    controller = createConsoleCommentCampaignCreate({
      requestJson: requestJson.bind(null, browser),
      location: browser.location,
      render: function (state, model) { renderDom(document, state, model, controller); },
    });
    bindDom(document, controller);
    browser.__consoleCommentCampaignCreateController = controller;
    controller.init();
    return controller;
  }

  return {
    createConsoleCommentCampaignCreate,
    validateDraft,
    buildCreatePayload,
    boot,
  };
});
