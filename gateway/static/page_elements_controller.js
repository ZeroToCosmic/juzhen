(function (root, factory) {
  "use strict";
  const exported = factory(root);
  if (typeof module === "object" && module.exports) module.exports = exported;
  if (root && !root.PageElementsController) root.PageElementsController = exported;
})(typeof globalThis !== "undefined" ? globalThis : this, function (root) {
  "use strict";

  const API_PREFIX = "/api/browser-v2";
  const ACTIVE_PICKER = new Set(["selection_ready", "waiting_for_selection"]);
  const STAGES = {
    selection_ready: "可保存当前点选", waiting_for_selection: "等待页面点选",
  };

  function clone(value) { return value === undefined ? undefined : JSON.parse(JSON.stringify(value)); }
  function listOf(value, keys) {
    if (Array.isArray(value)) return value;
    for (const key of keys || []) if (Array.isArray(value && value[key])) return value[key];
    return [];
  }
  function identifier(value) { return value && (value.id || value.element_id || value.session_id); }
  function profileToken(profile) { return profile && profile.profile_token; }
  function profileName(profile) { return profile && (profile.display_id || profile.name || profile.label || profile.profile_no || profileToken(profile)); }
  function pickerSelectionKey(selection) {
    if (!selection) return "";
    const fields = ["actionable_ancestor_fingerprint", "original_fingerprint", "unique_css", "relative_xpath"];
    for (const field of fields) if (selection[field]) return field + ":" + String(selection[field]);
    return JSON.stringify([selection.tag || "", selection.role || "", selection.name || "", selection.text_preview || ""]);
  }
  function stageLabel(stage) { return STAGES[stage] || (stage ? "处理中" : "尚未开始"); }
  function safeEvidencePath(value) {
    const text = String(value || "").replace(/^\\+/, "");
    const match = text.match(/^evidence\/([A-Fa-f0-9]{32})\.png$/);
    return match ? "/evidence/" + match[1].toLowerCase() + ".png" : "";
  }
  function displayValue(value) {
    return value === undefined || value === null || value === "" ? "—" : String(value);
  }
  function jsonValue(value) {
    if (value === undefined || value === null || value === "") return "—";
    try { return JSON.stringify(value, null, 2); } catch (_) { return "—"; }
  }
  function apiPath(path) {
    const text = String(path || "");
    if (!text.startsWith(API_PREFIX + "/") && text !== API_PREFIX) throw new Error("V2 页面只能调用 V2 接口");
    return text;
  }
  function errorMessage(result, fallback) {
    const error = result && result.data && result.data.error;
    if (error && typeof error === "object" && typeof error.message === "string") return error.message;
    if (typeof error === "string") return error;
    return (result && result.data && result.data.message) || fallback;
  }

  function createPageElementsController(options) {
    const deps = options || {};
    if (!deps.root || typeof deps.root.querySelector !== "function") throw new Error("页面元素控制器需要 root");
    if (typeof deps.requestJson !== "function") throw new Error("页面元素控制器需要 requestJson");
    const state = {
      profiles: [], elements: [], picker: null, pickerProfileToken: "", renderedPickerSelectionKey: "",
      repickTarget: null, profilesAvailable: false, elementsLoaded: false, submitting: false, timer: null,
      latestValidation: new Map(), filters: {query: "", kind: "", status: ""}, expandedId: "", initialized: false,
    };
    let unloadHandler = null;

    function el(selector) { return deps.root.querySelector(selector); }
    function all(selector) { return Array.from(deps.root.querySelectorAll ? deps.root.querySelectorAll(selector) : []); }
    const ownerDocument = deps.root.createElement ? deps.root : deps.root.ownerDocument;
    function node(tag, text, className) {
      const child = ownerDocument.createElement(tag);
      if (className) child.className = className;
      if (text !== undefined && text !== null) child.textContent = String(text);
      return child;
    }
    function clear(element) { if (element) element.replaceChildren(); }
    function button(text, className, handler) {
      const child = node("button", text, className || "v2-button"); child.type = "button";
      if (handler) child.addEventListener("click", handler); return child;
    }
    function emit(text, error) {
      if (typeof deps.onMessage === "function") deps.onMessage({text: text || "", error: Boolean(error)});
    }
    async function request(path, method, body) {
      try {
        const result = await deps.requestJson(apiPath(path), method || "GET", body);
        if (result && result.data && Object.prototype.hasOwnProperty.call(result.data, "data")) return {status: result.status, data: result.data.data};
        return result;
      }
      catch (error) { return {status: 0, data: {error: error && error.message ? error.message : "请求失败"}}; }
    }
    function success(result, expected) { return result && (expected || [200, 201, 202]).includes(result.status); }
    function activePicker() { return state.picker && ACTIVE_PICKER.has(state.picker.status); }
    function stale(result, fallback) {
      if (result && result.status === 409) { emit("元素版本已变化，请刷新后重试", true); return true; }
      if (!success(result, [200, 201, 202, 204])) emit(errorMessage(result, fallback), true);
      return false;
    }
    function stopPolling() {
      if (state.timer !== null) { deps.clearTimeout(state.timer); state.timer = null; }
    }
    function syncPolling() {
      stopPolling();
      if (!activePicker()) return;
      state.timer = deps.setTimeout(async function () {
        state.timer = null;
        await refreshPicker();
        syncPolling();
      }, 1000);
    }
    async function refreshPicker() {
      if (!activePicker()) return false;
      const session = state.picker.session_id || identifier(state.picker);
      const result = await request(API_PREFIX + "/picker/" + encodeURIComponent(session), "GET");
      if (success(result, [200])) {
        state.picker = result.data && (result.data.picker || result.data);
        renderPicker();
        return true;
      }
      emit(errorMessage(result, "读取点选状态失败"), true);
      return false;
    }
    async function loadProfiles(quiet) {
      const result = await request(API_PREFIX + "/profiles", "GET");
      if (!success(result, [200])) {
        state.profilesAvailable = false;
        if (!quiet) emit(errorMessage(result, "加载 Profile 失败"), true);
        return false;
      }
      state.profiles = listOf(result.data, ["profiles"]);
      state.profilesAvailable = true;
      return true;
    }
    function publishElements() {
      if (typeof deps.onElementsChanged === "function") deps.onElementsChanged(clone(state.elements));
    }
    async function loadElements(quiet) {
      const result = await request(API_PREFIX + "/elements", "GET");
      if (!success(result, [200])) {
        state.elementsLoaded = false;
        if (!quiet) emit(errorMessage(result, "加载元素失败"), true);
        return false;
      }
      state.elements = listOf(result.data, ["elements"]);
      state.elementsLoaded = true;
      publishElements();
      return true;
    }
    function renderProfiles() {
      const pickerSelect = el("#v2-picker-profile");
      const selectedPickerProfile = state.pickerProfileToken || (pickerSelect && pickerSelect.value) || "";
      if (selectedPickerProfile) state.pickerProfileToken = selectedPickerProfile;
      if (pickerSelect) {
        clear(pickerSelect); const empty = node("option", "请选择"); empty.value = ""; pickerSelect.append(empty);
        state.profiles.forEach(function (profile) { const option = node("option", profileName(profile)); option.value = profileToken(profile); option.selected = option.value === state.pickerProfileToken; pickerSelect.append(option); });
      }
      const validationSelect = el("#v2-element-validate-profile");
      if (validationSelect) {
        clear(validationSelect); const empty = node("option", "请选择校验 Profile"); empty.value = ""; validationSelect.append(empty);
        state.profiles.forEach(function (profile) { const option = node("option", profileName(profile)); option.value = profileToken(profile); validationSelect.append(option); });
      }
      const unavailable = !state.profilesAvailable;
      if (el("#v2-picker-start")) el("#v2-picker-start").disabled = unavailable || state.submitting || activePicker();
      if (pickerSelect) pickerSelect.disabled = unavailable || activePicker();
      if (validationSelect) validationSelect.disabled = unavailable;
    }
    function beginRepick(item) {
      state.repickTarget = {id: identifier(item), revision: item.revision, name: item.name, purpose: item.purpose, kind: item.kind};
      emit("重新点选：请选择 Profile，启动点选器并点选替代元素。");
      return true;
    }
    async function savePickerElement(name, purpose, kind) {
      if (!state.picker || !state.picker.selection || !name || !String(name).trim()) { emit("请先点选元素并填写名称", true); return false; }
      const body = {name: String(name).trim(), purpose: purpose, kind: kind};
      if (state.repickTarget) { body.element_id = state.repickTarget.id; body.expected_revision = state.repickTarget.revision; }
      const session = state.picker.session_id || identifier(state.picker);
      const result = await request(API_PREFIX + "/picker/" + encodeURIComponent(session) + "/save", "POST", body);
      if (!success(result, [200, 201])) { stale(result, "保存元素失败"); return false; }
      const wasRepick = Boolean(state.repickTarget); state.repickTarget = null;
      state.picker = {...state.picker, status: "waiting_for_selection", selection: null}; state.renderedPickerSelectionKey = "";
      await loadElements(false);
      emit(wasRepick ? "元素已重新点选" : "元素已保存，可继续点选"); render(); syncPolling();
      return true;
    }
    function renderPicker() {
      const stateNode = el("#v2-picker-state"), candidates = el("#v2-picker-candidates");
      const active = activePicker(), finish = el("#v2-picker-finish"), cancel = el("#v2-picker-cancel");
      if (finish) finish.disabled = !active;
      if (cancel) cancel.disabled = !active;
      if (!state.picker) { clear(candidates); state.renderedPickerSelectionKey = ""; if (stateNode) stateNode.textContent = "尚未开启点选器。"; return; }
      if (stateNode) stateNode.textContent = state.picker.error || (active ? "浏览器已打开，请在页面点选元素。" : stageLabel(state.picker.status));
      const selection = state.picker.selection;
      if (!selection) { clear(candidates); state.renderedPickerSelectionKey = ""; return; }
      const selectionKey = pickerSelectionKey(selection);
      if (selectionKey === state.renderedPickerSelectionKey && candidates && candidates.childElementCount) return;
      clear(candidates); state.renderedPickerSelectionKey = selectionKey;
      const form = node("form", undefined, "v2-candidate-form"); form.noValidate = true;
      const name = node("input"); name.required = true; name.placeholder = "元素名称";
      const purpose = node("select"); ["action", "readiness"].forEach(function (value) { const option = node("option", value); option.value = value; purpose.append(option); });
      const kind = node("select"); ["click", "input", "generic"].forEach(function (value) { const option = node("option", value); option.value = value; kind.append(option); });
      if (state.repickTarget) { name.value = state.repickTarget.name; name.readOnly = true; purpose.value = state.repickTarget.purpose; purpose.disabled = true; kind.value = state.repickTarget.kind; kind.disabled = true; }
      form.append(name, purpose, kind, button("保存元素", "v2-button primary", async function () { await savePickerElement(name.value, purpose.value, kind.value); }));
      if (candidates) candidates.append(form);
    }
    function elementById(id) { return state.elements.find(function (item) { return identifier(item) === id; }); }
    function visibleElements() {
      const query = String(state.filters.query || "").trim().toLowerCase();
      return state.elements.filter(function (item) {
        const definition = item.definition || {};
        const metadata = definition.diagnostic_metadata || {};
        const text = [item.name, item.purpose, item.kind, item.status, definition.url_pattern, metadata.text_preview, metadata.role].filter(Boolean).join(" ").toLowerCase();
        return (!query || text.includes(query)) && (!state.filters.kind || item.kind === state.filters.kind) && (!state.filters.status || item.status === state.filters.status);
      });
    }
    function detailRow(label, value) {
      const row = node("div", undefined, "page-elements-detail-row");
      row.append(node("span", label, "page-elements-detail-label"), node("pre", displayValue(value), "page-elements-detail-value"));
      return row;
    }
    function validationSummary(item) {
      const validation = state.latestValidation.get(identifier(item));
      if (!validation) return "本次打开页面尚未校验";
      if (validation.valid === true) return "校验通过";
      if (validation.valid === false) return "校验未通过";
      return "校验已提交";
    }
    function definitionDetails(item) {
      const definition = item.definition || {};
      const metadata = definition.diagnostic_metadata;
      const locators = Array.isArray(definition.locators) ? definition.locators : [];
      const validation = state.latestValidation.get(identifier(item));
      const details = node("div", undefined, "page-elements-detail");
      details.append(
        detailRow("元素 ID", identifier(item)),
        detailRow("版本", item.revision),
        detailRow("创建时间", item.created_at),
        detailRow("更新时间", item.updated_at),
        detailRow("所属页面 / URL 匹配", definition.url_pattern),
        detailRow("定位器数量", locators.length),
        detailRow("Frame 路径", Array.isArray(definition.frame_path) && definition.frame_path.length ? definition.frame_path.join(" › ") : "—"),
        detailRow("Locators", locators.length ? locators.map(function (locator) {
          if (locator && locator.type === "role") return "role=" + displayValue(locator.role) + " name=" + displayValue(locator.name) + " · priority=" + displayValue(locator.priority);
          return displayValue(locator && locator.type) + "=" + displayValue(locator && locator.value) + " · priority=" + displayValue(locator && locator.priority);
        }).join("\n") : "—"),
        detailRow("Diagnostic metadata", jsonValue(metadata)),
        detailRow("最近校验结果", validationSummary(item)),
        detailRow("最近校验详情", jsonValue(validation)),
      );
      const screenshot = safeEvidencePath(definition.screenshot_path);
      const screenshotRow = node("div", undefined, "page-elements-detail-row");
      screenshotRow.append(node("span", "截图", "page-elements-detail-label"));
      if (screenshot) { const link = node("a", screenshot, "page-elements-detail-value"); link.href = screenshot; link.target = "_blank"; link.rel = "noopener"; screenshotRow.append(link); }
      else screenshotRow.append(node("pre", "—", "page-elements-detail-value"));
      details.append(screenshotRow);
      return details;
    }
    function renderElements() {
      const list = el("#v2-elements-list"), empty = el("#v2-elements-empty");
      clear(list); const items = visibleElements(); if (empty) empty.hidden = items.length > 0;
      items.forEach(function (item) {
        const row = node("article", undefined, "v2-row"), main = node("div", undefined, "v2-row-main"), controls = node("div", undefined, "v2-row-actions");
        const locatorCount = Array.isArray(item.definition && item.definition.locators) ? item.definition.locators.length : 0;
        main.append(node("strong", item.name || identifier(item)), node("div", [item.purpose, item.kind, item.status === "disabled" ? "已停用" : "已启用", "版本 " + displayValue(item.revision), "定位器 " + locatorCount, validationSummary(item)].filter(Boolean).join(" · "), "v2-row-meta"));
        const validate = button("校验", "v2-button v2-element-validate", function () { validateElement(item); }); validate.disabled = !state.profilesAvailable;
        controls.append(button("详情", "v2-button", function () { toggleDetails(identifier(item)); }), button("改名", "v2-button", function () { renameElement(item); }), button("重新点选", "v2-button", function () { beginRepick(item); }), button(item.status === "disabled" ? "启用" : "停用", "v2-button", function () { setElementEnabled(item, item.status === "disabled"); }), validate, button("删除", "v2-button danger", function () { deleteElement(item); }));
        row.append(main, controls); list.append(row);
        if (state.expandedId === identifier(item)) row.append(definitionDetails(item));
      });
    }
    async function renameElement(item, nextName) {
      const name = nextName === undefined ? (deps.prompt ? deps.prompt("元素名称", item.name || "") : (root.prompt ? root.prompt("元素名称", item.name || "") : "")) : nextName;
      if (!name || !name.trim()) return false;
      const result = await request(API_PREFIX + "/elements/" + encodeURIComponent(identifier(item)), "PUT", {expected_revision: item.revision, name: name.trim()});
      if (!success(result, [200])) { stale(result, "改名失败"); return false; }
      await loadElements(false); emit("元素已改名"); render(); return true;
    }
    async function setElementEnabled(item, enabled) {
      const result = await request(API_PREFIX + "/elements/" + encodeURIComponent(identifier(item)), "PUT", {expected_revision: item.revision, status: enabled ? "active" : "disabled"});
      if (!success(result, [200])) { stale(result, "更新元素失败"); return false; }
      await loadElements(false); emit(enabled ? "元素已启用" : "元素已停用"); render(); return true;
    }
    async function validateElement(item) {
      if (!state.profilesAvailable) { emit("AdsPower 未连接，暂时无法执行 Profile 操作", true); return false; }
      const profileSelect = el("#v2-element-validate-profile"), profile = profileSelect && profileSelect.value;
      if (!profile) { emit("请选择校验 Profile", true); return false; }
      const result = await request(API_PREFIX + "/elements/" + encodeURIComponent(identifier(item)) + "/validate", "POST", {profile_token: profile});
      if (success(result, [200, 202])) { state.latestValidation.set(identifier(item), clone(result.data)); emit("元素校验已提交"); render(); return true; }
      stale(result, "元素校验失败"); return false;
    }
    async function deleteElement(item) {
      const confirmed = deps.confirm ? deps.confirm("确认删除此元素？") : (root.confirm ? root.confirm("确认删除此元素？") : false);
      if (!confirmed) return false;
      const result = await request(API_PREFIX + "/elements/" + encodeURIComponent(identifier(item)), "DELETE", {expected_revision: item.revision});
      if (!success(result, [200, 204])) { stale(result, "删除元素失败"); return false; }
      await loadElements(false); emit("元素已删除"); render(); return true;
    }
    function setFilters(filters) { state.filters = {...state.filters, ...(filters || {})}; renderElements(); return {...state.filters}; }
    function toggleDetails(elementId) { state.expandedId = state.expandedId === elementId ? "" : elementId; render(); return state.expandedId; }
    function hasActivePicker() { return Boolean(activePicker()); }
    function getElements() { return clone(state.elements); }
    function syncHostState(snapshot) {
      const source = snapshot || {};
      if (Array.isArray(source.profiles)) state.profiles = source.profiles;
      if (Array.isArray(source.elements)) state.elements = source.elements;
      if (Object.prototype.hasOwnProperty.call(source, "profilesAvailable")) state.profilesAvailable = Boolean(source.profilesAvailable);
      if (Object.prototype.hasOwnProperty.call(source, "picker")) state.picker = source.picker;
      if (Object.prototype.hasOwnProperty.call(source, "pickerProfileToken")) state.pickerProfileToken = source.pickerProfileToken || "";
      if (Object.prototype.hasOwnProperty.call(source, "repickTarget")) state.repickTarget = source.repickTarget;
      if (Object.prototype.hasOwnProperty.call(source, "submitting")) state.submitting = Boolean(source.submitting);
      return state;
    }
    function render() { renderProfiles(); renderPicker(); renderElements(); }
    async function startPicker() {
      if (!state.profilesAvailable) { emit("AdsPower 未连接，暂时无法执行 Profile 操作", true); return false; }
      if (activePicker() || state.submitting) return false;
      const select = el("#v2-picker-profile"), urlInput = el("#v2-picker-url");
      const profile = select && select.value, url = urlInput && urlInput.value.trim();
      if (!profile || !url) { emit("请选择测试 Profile 并填写网址", true); return false; }
      state.pickerProfileToken = profile; state.submitting = true;
      const result = await request(API_PREFIX + "/picker/start", "POST", {profile_token: profile, target_url: url}); state.submitting = false;
      if (!success(result, [202])) { stale(result, "启动点选器失败"); render(); return false; }
      state.picker = typeof result.data === "string" ? {id: result.data, status: "waiting_for_selection", profile_token: profile} : (result.data.picker || result.data);
      emit("点选器已启动"); render(); syncPolling(); return true;
    }
    async function finishPicker(cancel) {
      if (!state.picker) return false;
      const session = state.picker.session_id || identifier(state.picker), suffix = cancel ? "/cancel" : "/finish";
      const result = await request(API_PREFIX + "/picker/" + encodeURIComponent(session) + suffix, "POST", {});
      if (!success(result, [200, 202])) { stale(result, "结束点选失败"); return false; }
      state.picker = result.data && (result.data.picker || result.data) || {...state.picker, status: cancel ? "cancelled" : "completed"}; state.repickTarget = null;
      emit(cancel ? "点选已取消" : "点选已完成"); render(); syncPolling(); return true;
    }
    function wire() {
      el("#v2-picker-form")?.addEventListener("submit", function (event) { event.preventDefault(); startPicker(); });
      el("#v2-picker-finish")?.addEventListener("click", function () { finishPicker(false); });
      el("#v2-picker-cancel")?.addEventListener("click", function () { finishPicker(true); });
      el("#v2-picker-profile")?.addEventListener("change", function () { state.pickerProfileToken = el("#v2-picker-profile").value; });
      el("#v2-elements-search")?.addEventListener("input", function (event) { setFilters({query: event.target.value}); });
      el("#v2-elements-kind-filter")?.addEventListener("change", function (event) { setFilters({kind: event.target.value}); });
      el("#v2-elements-status-filter")?.addEventListener("change", function (event) { setFilters({status: event.target.value}); });
      unloadHandler = function (event) { if (activePicker()) { event.preventDefault(); event.returnValue = ""; return ""; } return undefined; };
      if (deps.addBeforeUnload) deps.addBeforeUnload(unloadHandler);
    }
    async function init() {
      if (state.initialized) return state;
      const [profilesLoaded, elementsLoaded] = await Promise.all([loadProfiles(true), loadElements(true)]);
      state.initialized = true; wire(); render(); syncPolling();
      if (!elementsLoaded) emit("元素库加载失败，请稍后重试。", true);
      else if (!profilesLoaded) emit("元素库已更新；Profile 加载失败，点选与校验暂不可用。", true);
      else emit("元素库已更新", false);
      return state;
    }
    function destroy() { stopPolling(); if (unloadHandler && deps.removeBeforeUnload) deps.removeBeforeUnload(unloadHandler); unloadHandler = null; }
    return {
      state, init, refresh: async function () { await loadProfiles(false); await loadElements(false); render(); return state; }, render, setFilters, toggleDetails,
      startPicker, finishPicker, savePickerElement, beginRepick, renameElement, setElementEnabled, validateElement, deleteElement,
      hasActivePicker, getElements, syncHostState, syncPolling, stopPolling, refreshPicker, destroy,
    };
  }

  return {createPageElementsController, ACTIVE_PICKER, safeEvidencePath};
});
