(function (root, factory) {
  "use strict";
  const exported = factory(root);
  if (typeof module === "object" && module.exports) module.exports = exported;
  if (root && root.document) {
    let controller = null;
    root.BrowserV2UI = {
      createBrowserV2UI: exported.createBrowserV2UI,
      stageLabel: exported.stageLabel,
      init: function () {
        if (!controller) controller = exported.createBrowserV2UI(exported.browserV2Dependencies(root));
        return controller.init();
      },
    };
    root.addEventListener("DOMContentLoaded", function () { root.BrowserV2UI.init(); }, {once: true});
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function (root) {
  "use strict";

  const API_PREFIX = "/api/browser-v2";
  const VIEWS = ["center", "elements", "strategies", "history", "settings"];
  const ACTIVE_JOB = new Set(["queued", "running", "cancelling"]);
  const ACTIVE_PICKER = new Set(["selection_ready", "waiting_for_selection"]);
  const STAGES = {
    queued: "等待排队", starting: "正在启动浏览器", window_tile: "正在排列窗口", navigating: "正在打开页面",
    readiness: "正在等待页面就绪", executing: "正在执行动作", capturing: "正在保存结果",
    closing: "正在关闭浏览器", completed: "已完成", cancelled: "已取消", failed: "执行失败",
    cleanup_blocked: "关闭待处理", service_restarted: "服务重启中断", selection_ready: "可保存当前点选", waiting_for_selection: "等待页面点选",
  };
  const ACTIONS = {
    move: {label: "移动"}, scroll: {label: "切换视频"}, click: {label: "点击元素"}, input: {label: "键盘输入"}, wait: {label: "等待"},
  };
  const STRATEGY_DEFINITION_FIELDS = [
    "target_url", "ready_element_id", "readiness_timeout_seconds",
    "run_mode", "loop_duration_minutes", "actions",
  ];

  function clone(value) { return value === undefined ? undefined : JSON.parse(JSON.stringify(value)); }
  function strategyDraft(record) {
    const draft = clone(record || {});
    if (draft.definition && typeof draft.definition === "object") return draft;
    const definition = {};
    STRATEGY_DEFINITION_FIELDS.forEach(function (field) { definition[field] = draft[field]; delete draft[field]; });
    if (!Array.isArray(definition.actions)) definition.actions = [];
    draft.definition = definition;
    return draft;
  }
  function listOf(value, keys) {
    if (Array.isArray(value)) return value;
    for (const key of keys || []) if (Array.isArray(value && value[key])) return value[key];
    return [];
  }
  function identifier(value) { return value && (value.id || value.element_id || value.strategy_id || value.job_id || value.session_id); }
  function pickerSelectionKey(selection) {
    if (!selection) return "";
    const fields = ["actionable_ancestor_fingerprint", "original_fingerprint", "unique_css", "relative_xpath"];
    for (const field of fields) if (selection[field]) return field + ":" + String(selection[field]);
    return JSON.stringify([selection.tag || "", selection.role || "", selection.name || "", selection.text_preview || ""]);
  }
  function profileToken(profile) { return profile && profile.profile_token; }
  function safeEvidencePath(value) {
    return typeof value === "string" && /^evidence\/[A-Za-z0-9][A-Za-z0-9_.-]*\.png$/.test(value) ? value : "";
  }
  function stageLabel(stage) { return STAGES[stage] || (stage ? "处理中" : "尚未开始"); }
  function errorMessage(result, fallback) {
    const error = result && result.data && result.data.error;
    if (error && typeof error === "object" && typeof error.message === "string") return error.message;
    if (typeof error === "string") return error;
    return (result && result.data && result.data.message) || fallback;
  }
  function apiPath(path) {
    const text = String(path || "");
    if (!text.startsWith(API_PREFIX + "/") && text !== API_PREFIX) throw new Error("V2 页面只能调用 V2 接口");
    return text;
  }
  function actionTemplate(type, id) {
    const source = ACTIONS[type];
    if (!source) throw new Error("不支持的动作类型");
    const actionId = id || ("action_" + Date.now());
    if (type === "move") return {id: actionId, type, element_id: "", duration_seconds: [0.2, 0.5]};
    if (type === "scroll") return {id: actionId, type, direction: "down", distance_pixels: [120, 120], count: [1, 2], interval_seconds: [0.2, 0.5]};
    if (type === "click") return {id: actionId, type, element_id: "", button: "left", click_count: 1, hold_seconds: [0.05, 0.1], after_seconds: [0.3, 0.6]};
    if (type === "input") return {id: actionId, type, element_id: "", content_source: "fixed", fixed_text: "", content_library_id: "", interval_ms: [40, 120]};
    return {id: actionId, type, duration_seconds: [1, 1]};
  }
  function newStrategy(id) {
    return {id: id || ("strategy_" + Date.now()), localNew: true, name: "新策略", enabled: true, definition: {target_url: "https://www.tiktok.com/", ready_element_id: "", readiness_timeout_seconds: 15, run_mode: "once", loop_duration_minutes: null, actions: []}};
  }

  function browserV2Dependencies(win) {
    return {
      requestJson: async function (url, method, body) {
        apiPath(url);
        const response = await win.fetch(url, {
          method: method || "GET",
          headers: body === undefined ? {} : {"Content-Type": "application/json"},
          body: body === undefined ? undefined : JSON.stringify(body),
          credentials: "same-origin",
        });
        let data = {};
        try { data = await response.json(); } catch (_) { data = {error: "服务返回格式错误"}; }
        return {status: response.status, data: data};
      },
      setTimeout: win.setTimeout.bind(win), clearTimeout: win.clearTimeout.bind(win),
      addUnload: function (handler) { win.addEventListener("beforeunload", handler); },
      removeUnload: function (handler) { win.removeEventListener("beforeunload", handler); },
      storage: win.localStorage,
      document: win.document,
    };
  }

  function createBrowserV2UI(dependencies) {
    const deps = dependencies || {};
    const state = {
      view: "center", profiles: [], profilesAvailable: false, elements: [], contentLibraries: [], strategies: [], history: [], job: null,
      picker: null, pickerProfileToken: "", renderedPickerSelectionKey: "", repickTarget: null, draft: null,
      submitting: false, error: "", status: "准备加载", timer: null,
      batchSize: 3, initialized: false,
    };
    let actionSequence = 0;

    function doc() { return deps.document; }
    function el(selector) { return doc() && doc().querySelector(selector); }
    function all(selector) { return doc() ? Array.from(doc().querySelectorAll(selector)) : []; }
    function node(tag, text, className) {
      const child = doc().createElement(tag);
      if (className) child.className = className;
      if (text !== undefined && text !== null) child.textContent = String(text);
      return child;
    }
    function clear(element) { if (element) element.replaceChildren(); }
    function button(text, className, handler) {
      const child = node("button", text, className || "v2-button"); child.type = "button";
      if (handler) child.addEventListener("click", handler); return child;
    }
    function setMessage(error, status) {
      state.error = error || ""; state.status = status || state.status;
      const errorNode = el("#v2-page-error"); const statusNode = el("#v2-live-status");
      if (errorNode) errorNode.textContent = state.error;
      if (statusNode) statusNode.textContent = state.status;
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
    function selectedProfiles() {
      return all("#v2-profile-list input:checked").map(function (input) { return input.value; });
    }
    function setAllProfiles(checked) { all("#v2-profile-list input[type='checkbox']").forEach(function (input) { input.checked = checked; }); return true; }
    function activeJob() { return state.job && ACTIVE_JOB.has(state.job.status); }
    function activePicker() { return state.picker && ACTIVE_PICKER.has(state.picker.status); }
    function stopPolling() { if (state.timer !== null) { deps.clearTimeout(state.timer); state.timer = null; } }
    function syncPolling() {
      stopPolling();
      if (!activeJob() && !activePicker()) return;
      state.timer = deps.setTimeout(async function () {
        state.timer = null;
        await refreshActive();
        syncPolling();
      }, 1000);
    }
    async function refreshActive() {
      const reads = [];
      if (activeJob()) reads.push(request(API_PREFIX + "/jobs/" + encodeURIComponent(identifier(state.job)), "GET").then(function (result) {
        if (success(result, [200])) state.job = result.data.job || result.data;
        else setMessage(errorMessage(result, "读取任务失败"));
      }));
      if (activePicker()) reads.push(request(API_PREFIX + "/picker/" + encodeURIComponent(identifier(state.picker) || state.picker.session_id), "GET").then(function (result) {
        if (success(result, [200])) state.picker = result.data.picker || result.data;
        else setMessage(errorMessage(result, "读取点选状态失败"));
      }));
      await Promise.all(reads);
      renderProfiles(); renderJob(); renderPicker(); setMessage(state.error, state.status);
    }
    async function load(path, target, keys, quiet) {
      const result = await request(path, "GET");
      if (!success(result, [200])) {
        const message = errorMessage(result, "加载失败");
        if (!quiet) setMessage(message);
        return {ok: false, error: message};
      }
      state[target] = listOf(result.data, keys); return {ok: true, error: ""};
    }
    async function init() {
      if (state.initialized) return state;
      const stored = Number(deps.storage && deps.storage.getItem("browser-v2-batch-size"));
      if (Number.isInteger(stored) && stored >= 1 && stored <= 8) state.batchSize = stored;
      const results = await Promise.all([
        load(API_PREFIX + "/profiles", "profiles", ["profiles"], true), load(API_PREFIX + "/elements", "elements", ["elements"], true),
        load(API_PREFIX + "/content-libraries", "contentLibraries", ["content_libraries", "libraries"], true),
        load(API_PREFIX + "/strategies", "strategies", ["strategies"], true), load(API_PREFIX + "/history", "history", ["history", "jobs"], true),
      ]);
      state.initialized = true; state.profilesAvailable = results[0].ok;
      if (!results[0].ok) {
        setMessage("无法读取 AdsPower Profile，请确认 AdsPower 已启动及 API Key 正确", "部分可用：AdsPower 未连接");
      } else if (results.some(function (result) { return !result.ok; })) {
        setMessage(results.filter(function (result) { return !result.ok; }).map(function (result) { return result.error; }).join("；"), "部分可用");
      } else {
        setMessage("", "就绪");
      }
      render();
      if (deps.addUnload) deps.addUnload(stopPolling); return state;
    }
    function switchView(view) { if (!VIEWS.includes(view)) return false; state.view = view; render(); return true; }
    function profileName(profile) { return profile && (profile.display_id || profile.name || profile.label || profile.profile_no || profileToken(profile)); }
    function optionList(select, items, selected, labeler) {
      if (!select) return;
      clear(select); const empty = node("option", "请选择"); empty.value = ""; select.append(empty);
      items.forEach(function (item) { const opt = node("option", labeler(item)); opt.value = identifier(item); opt.selected = opt.value === selected; select.append(opt); });
    }
    function renderProfiles() {
      const profileList = el("#v2-profile-list"); if (!profileList) return;
      clear(profileList);
      state.profiles.forEach(function (profile) {
        const label = node("label"); const checkbox = node("input"); checkbox.type = "checkbox";
        checkbox.value = profileToken(profile); label.append(checkbox, node("span", profileName(profile))); profileList.append(label);
      });
      const pickerSelect = el("#v2-picker-profile");
      const selectedPickerProfile = state.pickerProfileToken || (pickerSelect && pickerSelect.value) || "";
      if (selectedPickerProfile) state.pickerProfileToken = selectedPickerProfile;
      clear(pickerSelect); const empty = node("option", "请选择"); empty.value = ""; pickerSelect.append(empty);
      state.profiles.forEach(function (profile) { const option = node("option", profileName(profile)); option.value = profileToken(profile); option.selected = option.value === state.pickerProfileToken; pickerSelect.append(option); });
      const validationSelect = el("#v2-element-validate-profile");
      if (validationSelect) {
        clear(validationSelect); const validationEmpty = node("option", "请选择校验 Profile"); validationEmpty.value = ""; validationSelect.append(validationEmpty);
        state.profiles.forEach(function (profile) { const option = node("option", profileName(profile)); option.value = profileToken(profile); validationSelect.append(option); });
      }
      const unavailable = !state.profilesAvailable, runStart = el("#v2-run-start"), pickerStart = el("#v2-picker-start");
      if (runStart) runStart.disabled = unavailable || state.submitting || activeJob();
      if (pickerStart) pickerStart.disabled = unavailable || state.submitting || activePicker();
      if (pickerSelect) pickerSelect.disabled = unavailable || activePicker();
      if (validationSelect) validationSelect.disabled = unavailable;
    }
    function renderRunStrategies() { optionList(el("#v2-run-strategy"), state.strategies, "", function (item) { return item.name || identifier(item); }); }
    function renderJob() {
      const metrics = el("#v2-job-summary"), list = el("#v2-job-profiles"), empty = el("#v2-job-empty"), cancel = el("#v2-run-cancel");
      if (!metrics || !list) return;
      clear(metrics); clear(list); if (cancel) cancel.disabled = !activeJob();
      if (!state.job) { if (empty) empty.hidden = false; return; } if (empty) empty.hidden = true;
      const data = state.job.summary || state.job;
      [["总数", data.total || data.total_profiles || 0], ["剩余", data.remaining || data.pending || 0], ["当前批次", data.current_batch || data.batch || "—"], ["成功", data.succeeded || data.success || 0], ["失败", data.failed || 0]].forEach(function (pair) {
        const metric = node("div", undefined, "v2-metric"); metric.append(node("span", pair[0]), node("strong", pair[1])); metrics.append(metric);
      });
      listOf(state.job.results || state.job.profiles, ["items"]).forEach(function (item) {
        const close = typeof item.close_confirmed === "boolean" ? (item.close_confirmed ? "已确认关闭" : "未确认关闭") : "";
        const text = [profileName(item), stageLabel(item.stage || item.status), item.error_summary || item.error_code || "", close].filter(Boolean).join(" · "); list.append(node("li", text));
      });
      listOf(state.job.actions, ["items"]).forEach(function (result) {
        const item = node("li", [result.display_id || "Profile", "动作 " + result.action_index, result.action_type, stageLabel(result.status), result.error_code || ""].filter(Boolean).join(" · "));
        const evidence = safeEvidencePath(result.evidence_path); if (evidence) { item.append(" · "); const link = node("a", "查看截图", "v2-screenshot"); link.href = evidence; link.target = "_blank"; link.rel = "noopener"; item.append(link); } list.append(item);
      });
    }
    function beginRepick(item) {
      state.repickTarget = {id: identifier(item), revision: item.revision, name: item.name, purpose: item.purpose, kind: item.kind};
      switchView("elements"); setMessage("重新点选：请选择 Profile，启动点选器并点选替代元素。"); return true;
    }
    async function savePickerElement(name, purpose, kind) {
      if (!state.picker || !state.picker.selection || !name || !String(name).trim()) { setMessage("请先点选元素并填写名称"); return false; }
      const body = {name: String(name).trim(), purpose: purpose, kind: kind};
      if (state.repickTarget) { body.element_id = state.repickTarget.id; body.expected_revision = state.repickTarget.revision; }
      const session = state.picker.session_id || identifier(state.picker), result = await request(API_PREFIX + "/picker/" + encodeURIComponent(session) + "/save", "POST", body);
      if (!success(result, [200, 201])) { setMessage(errorMessage(result, "保存元素失败")); return false; }
      const wasRepick = Boolean(state.repickTarget); state.repickTarget = null;
      state.picker = {...state.picker, status: "waiting_for_selection", selection: null};
      state.renderedPickerSelectionKey = "";
      await load(API_PREFIX + "/elements", "elements", ["elements"]); setMessage(wasRepick ? "元素已重新点选" : "元素已保存，可继续点选"); render(); return true;
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
      [selection].forEach(function () {
        const form = node("form", undefined, "v2-candidate-form"); form.noValidate = true;
        const name = node("input"); name.required = true; name.placeholder = "元素名称";
        const purpose = node("select"); ["action", "readiness"].forEach(function (value) { const opt = node("option", value); opt.value = value; purpose.append(opt); });
        const kind = node("select"); ["click", "input", "generic"].forEach(function (value) { const opt = node("option", value); opt.value = value; kind.append(opt); });
        if (state.repickTarget) { name.value = state.repickTarget.name; name.readOnly = true; purpose.value = state.repickTarget.purpose; purpose.disabled = true; kind.value = state.repickTarget.kind; kind.disabled = true; }
        const save = button("保存元素", "v2-button primary", async function () {
          await savePickerElement(name.value, purpose.value, kind.value);
        });
        form.append(name, purpose, kind, save); candidates.append(form);
      });
    }
    function elementById(id) { return state.elements.find(function (item) { return identifier(item) === id; }); }
    function renderElements() {
      const list = el("#v2-elements-list"), empty = el("#v2-elements-empty"); clear(list); if (empty) empty.hidden = state.elements.length > 0;
      state.elements.forEach(function (item) {
        const row = node("article", undefined, "v2-row"), main = node("div", undefined, "v2-row-main"), controls = node("div", undefined, "v2-row-actions");
        main.append(node("strong", item.name || identifier(item)), node("div", [item.purpose, item.kind, item.status === "disabled" ? "已停用" : "已启用"].filter(Boolean).join(" · "), "v2-row-meta"));
        const validate = button("校验", "v2-button v2-element-validate", function () { validateElement(item); });
        validate.disabled = !state.profilesAvailable;
        controls.append(
          button("改名", "v2-button", function () { renameElement(item); }), button("重新点选", "v2-button", function () { beginRepick(item); }),
          button(item.status === "disabled" ? "启用" : "停用", "v2-button", function () { setElementEnabled(item, item.status === "disabled"); }), validate,
          button("删除", "v2-button danger", function () { deleteElement(item); })
        ); row.append(main, controls); list.append(row);
      });
    }
    async function renameElement(item) {
      const name = root.prompt ? root.prompt("元素名称", item.name || "") : ""; if (!name || !name.trim()) return;
      const result = await request(API_PREFIX + "/elements/" + encodeURIComponent(identifier(item)), "PUT", {expected_revision: item.revision, name: name.trim()});
      if (!success(result, [200])) { setMessage(errorMessage(result, "改名失败")); return; }
      await load(API_PREFIX + "/elements", "elements", ["elements"]); setMessage("元素已改名"); render();
    }
    async function setElementEnabled(item, enabled) {
      const result = await request(API_PREFIX + "/elements/" + encodeURIComponent(identifier(item)), "PUT", {expected_revision: item.revision, status: enabled ? "active" : "disabled"});
      if (!success(result, [200])) { setMessage(errorMessage(result, "更新元素失败")); return; }
      await load(API_PREFIX + "/elements", "elements", ["elements"]); setMessage(enabled ? "元素已启用" : "元素已停用"); render();
    }
    async function validateElement(item) {
      if (!state.profilesAvailable) { setMessage("AdsPower 未连接，暂时无法执行 Profile 操作"); return false; }
      const profileToken = el("#v2-element-validate-profile").value;
      if (!profileToken) { setMessage("请选择校验 Profile"); return; }
      const result = await request(API_PREFIX + "/elements/" + encodeURIComponent(identifier(item)) + "/validate", "POST", {profile_token: profileToken});
      setMessage(success(result, [200, 202]) ? "元素校验已提交" : errorMessage(result, "元素校验失败")); render();
    }
    async function deleteElement(item) {
      const result = await request(API_PREFIX + "/elements/" + encodeURIComponent(identifier(item)), "DELETE", {expected_revision: item.revision});
      if (!success(result, [200, 204])) { setMessage(errorMessage(result, "删除元素失败")); return; }
      await load(API_PREFIX + "/elements", "elements", ["elements"]); setMessage("元素已删除"); render();
    }
    function eligibleElements(purpose, kinds) {
      return state.elements.filter(function (item) {
        return item.status === "active" && item.purpose === purpose && (!kinds || kinds.includes(item.kind));
      });
    }
    function rangeText(value) { return Array.isArray(value) ? value.join("-") : String(value || ""); }
    function actionCard(action, index) {
      const card = node("li", undefined, "v2-action-card"), header = node("header"), title = node("strong", ACTIONS[action.type].label), controls = node("div", undefined, "v2-row-actions");
      controls.append(button("上移", "v2-button", function () { moveAction(index, -1); }), button("下移", "v2-button", function () { moveAction(index, 1); }), button("删除", "v2-button danger", function () { removeAction(index); })); header.append(title, controls); card.append(header);
      function field(labelText, property, type) { const label = node("label", labelText), input = node(type === "textarea" ? "textarea" : "input"); if (type !== "textarea") input.type = type || "text"; input.value = Array.isArray(action[property]) ? rangeText(action[property]) : (action[property] == null ? "" : action[property]); input.addEventListener("input", function () { action[property] = input.value; }); label.append(input); card.append(label); }
      function elementField(kinds) { const label = node("label", "目标元素"), select = node("select"); optionList(select, eligibleElements("action", kinds), action.element_id, function (item) { return item.name || identifier(item); }); select.addEventListener("change", function () { action.element_id = select.value; }); label.append(select); card.append(label); }
      if (action.type === "move") { elementField(); field("移动秒数范围，例如 0.2-0.5", "duration_seconds"); }
      if (action.type === "scroll") { const label = node("label", "方向"), select = node("select"); ["down", "up"].forEach(function (value) { const option = node("option", value === "down" ? "向下" : "向上"); option.value = value; option.selected = action.direction === value; select.append(option); }); select.addEventListener("change", function () { action.direction = select.value; }); label.append(select); card.append(label); field("视频切换次数范围，例如 1-2", "count"); card.append(node("p", "每次发送一个方向键，并在确认视频切换后继续。", "v2-help")); field("切换完成后的间隔秒数范围，例如 0.2-0.5", "interval_seconds"); }
      if (action.type === "click") { elementField(["click", "generic"]); const label = node("label", "鼠标按键"), select = node("select"); ["left", "middle", "right"].forEach(function (value) { const option = node("option", value); option.value = value; option.selected = action.button === value; select.append(option); }); select.addEventListener("change", function () { action.button = select.value; }); label.append(select); card.append(label); field("点击次数", "click_count", "number"); field("按住秒数范围，例如 0.05-0.1", "hold_seconds"); field("点击后等待范围，例如 0.3-0.6", "after_seconds"); }
      if (action.type === "input") {
        const inputElements = eligibleElements("action", ["input"]);
        elementField(["input"]);
        if (!inputElements.length) card.append(node("p", "暂无可用输入元素。请用点选器选择真实 <input>、<textarea> 或 [contenteditable=true]，并保存为 kind=input。", "v2-help"));

        const sourceLabel = node("label", "文案来源"), sourceSelect = node("select");
        [["fixed", "固定文案"], ["library", "文案库随机抽取"]].forEach(function (pair) {
          const option = node("option", pair[1]); option.value = pair[0]; option.selected = (action.content_source || "fixed") === pair[0]; sourceSelect.append(option);
        });
        sourceLabel.append(sourceSelect); card.append(sourceLabel);

        const fixedLabel = node("label", "输入文案"), fixedText = node("textarea");
        fixedText.value = action.fixed_text || "";
        fixedText.addEventListener("input", function () { action.fixed_text = fixedText.value; });
        fixedLabel.append(fixedText); card.append(fixedLabel);

        const libraryLabel = node("label", "选择文案库"), librarySelect = node("select");
        const emptyLibrary = node("option", "请选择文案库"); emptyLibrary.value = ""; librarySelect.append(emptyLibrary);
        state.contentLibraries.forEach(function (library) {
          const count = Number(library.copy_count) || 0;
          const option = node("option", (library.name || library.id) + " (" + count + " 条)");
          option.value = library.id; option.disabled = count <= 0; option.selected = option.value === action.content_library_id; librarySelect.append(option);
        });
        librarySelect.addEventListener("change", function () { action.content_library_id = librarySelect.value; });
        libraryLabel.append(librarySelect); card.append(libraryLabel);

        function syncContentSource() {
          const source = sourceSelect.value === "library" ? "library" : "fixed";
          action.content_source = source; fixedLabel.hidden = source !== "fixed"; libraryLabel.hidden = source !== "library";
        }
        sourceSelect.value = action.content_source === "library" ? "library" : "fixed";
        sourceSelect.addEventListener("change", function () {
          if (sourceSelect.value === "library") action.fixed_text = "";
          else action.content_library_id = "";
          syncContentSource();
        });
        syncContentSource();
        field("输入间隔毫秒范围，例如 40-120", "interval_ms");
      }
      if (action.type === "wait") field("等待秒数范围，例如 1-2", "duration_seconds");
      return card;
    }
    function renderStrategies() {
      if (!doc()) return;
      const list = el("#v2-strategy-list"), editor = el("#v2-strategy-editor"), empty = el("#v2-strategy-empty"), actionList = el("#v2-action-list"); clear(list); clear(actionList);
      state.strategies.forEach(function (item) { const row = node("article", undefined, "v2-row"), main = node("div", item.name || identifier(item), "v2-row-main"); row.append(main, button("编辑", "v2-button", function () { state.draft = strategyDraft(item); render(); })); list.append(row); });
      if (!state.draft) { if (editor) editor.hidden = true; if (empty) empty.hidden = false; return; }
      if (editor) editor.hidden = false; if (empty) empty.hidden = true;
      const definition = state.draft.definition; const name = el("#v2-strategy-name"), target = el("#v2-strategy-target-url"), ready = el("#v2-strategy-ready-element"), timeout = el("#v2-strategy-readiness-timeout"), mode = el("#v2-strategy-run-mode"), minutes = el("#v2-strategy-minutes"), enabled = el("#v2-strategy-enabled"), wrap = el("#v2-strategy-minutes-wrap");
      if (name) name.value = state.draft.name || ""; if (target) target.value = definition.target_url; optionList(ready, eligibleElements("readiness"), definition.ready_element_id, function (item) { return item.name || identifier(item); }); if (timeout) timeout.value = definition.readiness_timeout_seconds; if (mode) mode.value = definition.run_mode; if (minutes) minutes.value = definition.loop_duration_minutes ? rangeText(definition.loop_duration_minutes) : ""; if (enabled) enabled.checked = state.draft.enabled !== false; if (wrap) wrap.hidden = definition.run_mode !== "duration";
      definition.actions.forEach(function (action, index) { actionList.append(actionCard(action, index)); });
    }
    function nextActionId(actions) {
      const used = new Set((actions || []).map(function (item) { return item && item.id; }).filter(Boolean));
      let candidate;
      do { candidate = "action_" + (++actionSequence); } while (used.has(candidate));
      return candidate;
    }
    function addAction(type) {
      if (!state.draft) return false;
      const actions = state.draft.definition.actions;
      actions.push(actionTemplate(type, nextActionId(actions)));
      renderStrategies();
      return true;
    }
    function moveAction(index, delta) { const actions = state.draft && state.draft.definition.actions, target = index + delta; if (!actions || target < 0 || target >= actions.length) return false; const item = actions.splice(index, 1)[0]; actions.splice(target, 0, item); renderStrategies(); return true; }
    function removeAction(index) { if (!state.draft) return false; state.draft.definition.actions.splice(index, 1); renderStrategies(); return true; }
    function renderHistory() {
      const list = el("#v2-history-list"), empty = el("#v2-history-empty"); clear(list); if (empty) empty.hidden = state.history.length > 0;
      const records = state.history.flatMap(function (job) { const jobId = job.id || job.job_id, profiles = listOf(job.profiles, ["items"]).map(function (profile) { return {...profile, job_id: jobId}; }), actions = listOf(job.actions, ["items"]).map(function (action) { return {...action, job_id: jobId}; }); return profiles.concat(actions).length ? profiles.concat(actions) : [job]; });
      records.forEach(function (record) { const row = node("article", undefined, "v2-row"), main = node("div", undefined, "v2-row-main"), meta = node("div", undefined, "v2-row-meta"), close = typeof record.close_confirmed === "boolean" ? (record.close_confirmed ? "已确认关闭" : "未确认关闭") : ""; const switchProgress = record.action_type === "scroll" && Number.isInteger(record.completed_switches) && Number.isInteger(record.requested_switches) ? "视频切换 " + record.completed_switches + "/" + record.requested_switches : ""; main.append(node("strong", "任务 " + (record.job_id || identifier(record) || "—"))); meta.textContent = ["Profile: " + (record.display_id || record.profile || "—"), "阶段: " + stageLabel(record.stage || record.status), record.action_index == null ? "" : "动作: " + record.action_index, record.action_type || "", switchProgress, record.error_code || record.error_summary || "", close].filter(Boolean).join(" · "); main.append(meta); row.append(main); const evidence = safeEvidencePath(record.evidence_path); if (evidence) { const link = node("a", "查看截图", "v2-screenshot"); link.href = evidence; link.target = "_blank"; link.rel = "noopener"; row.append(link); } list.append(row); });
    }
    function render() {
      if (!doc()) return;
      all("[data-panel]").forEach(function (panel) { panel.hidden = panel.dataset.panel !== state.view; });
      all(".v2-tab").forEach(function (tab) { if (tab.dataset.view === state.view) tab.setAttribute("aria-current", "page"); else tab.removeAttribute("aria-current"); });
      const batch = el("#v2-run-batch-size"), setting = el("#v2-setting-batch-size"); if (batch) batch.value = state.batchSize; if (setting) setting.value = state.batchSize;
      renderProfiles(); renderRunStrategies(); renderJob(); renderPicker(); renderElements(); renderStrategies(); renderHistory(); setMessage(state.error, state.status);
    }
    async function startJob() {
      if (!state.profilesAvailable) { setMessage("AdsPower 未连接，暂时无法执行 Profile 操作"); return false; }
      if (state.submitting || activeJob()) return false;
      const strategy = el("#v2-run-strategy").value, profiles = selectedProfiles(), batchSize = Number(el("#v2-run-batch-size").value);
      if (!strategy || !profiles.length || !Number.isInteger(batchSize) || batchSize < 1 || batchSize > 8) { setMessage("请选择策略、至少一个 Profile，并填写 1 到 8 的批次数量"); return false; }
      state.submitting = true; const start = el("#v2-run-start"); if (start) start.disabled = true;
      const result = await request(API_PREFIX + "/jobs", "POST", {strategy_id: strategy, profile_tokens: profiles, batch_size: batchSize}); state.submitting = false;
      if (!success(result, [202])) { setMessage(errorMessage(result, "启动任务失败")); render(); return false; }
      state.job = typeof result.data === "string" ? {id: result.data, status: "queued"} : (result.data.job || (result.data.job_id ? {id: result.data.job_id, status: "queued"} : result.data)); setMessage("任务已开始"); render(); syncPolling(); return true;
    }
    async function cancelJob() { if (!activeJob()) return false; const result = await request(API_PREFIX + "/jobs/" + encodeURIComponent(identifier(state.job)) + "/cancel", "POST", {}); if (!success(result, [202, 200])) { setMessage(errorMessage(result, "取消任务失败")); return false; } state.job = (result.data && result.data.job) || {...state.job, status: "cancelling"}; setMessage("正在取消任务"); render(); syncPolling(); return true; }
    async function startPicker() { if (!state.profilesAvailable) { setMessage("AdsPower 未连接，暂时无法执行 Profile 操作"); return false; } if (activePicker() || state.submitting) return false; const profile = el("#v2-picker-profile").value, url = el("#v2-picker-url").value.trim(); if (!profile || !url) { setMessage("请选择测试 Profile 并填写网址"); return false; } state.pickerProfileToken = profile; state.submitting = true; const result = await request(API_PREFIX + "/picker/start", "POST", {profile_token: profile, target_url: url}); state.submitting = false; if (!success(result, [202])) { setMessage(errorMessage(result, "启动点选器失败")); render(); return false; } state.picker = typeof result.data === "string" ? {id: result.data, status: "waiting_for_selection", profile_token: profile} : (result.data.picker || result.data); setMessage("点选器已启动"); render(); syncPolling(); return true; }
    async function finishPicker(cancel) { if (!state.picker) return false; const session = state.picker.session_id || identifier(state.picker), suffix = cancel ? "/cancel" : "/finish"; const result = await request(API_PREFIX + "/picker/" + encodeURIComponent(session) + suffix, "POST", {}); if (!success(result, [200, 202])) { setMessage(errorMessage(result, "结束点选失败")); return false; } state.picker = result.data.picker || result.data || {...state.picker, status: cancel ? "cancelled" : "completed"}; state.repickTarget = null; setMessage(cancel ? "点选已取消" : "点选已完成"); render(); syncPolling(); return true; }
    function parseRange(value, label, integer) {
      const values = (Array.isArray(value) ? value : String(value).split("-")).map(Number);
      if (values.length !== 2 || !values.every(Number.isFinite) || values[0] > values[1] || (integer && !values.every(Number.isInteger))) throw new Error(label + "格式应为最小值-最大值");
      return values;
    }
    function serializeAction(action) {
      if (action.type === "move") return {id: action.id, type: "move", element_id: action.element_id, duration_seconds: parseRange(action.duration_seconds, "移动秒数范围")};
      if (action.type === "scroll") return {id: action.id, type: "scroll", direction: action.direction, distance_pixels: [120, 120], count: parseRange(action.count, "视频切换次数范围", true), interval_seconds: parseRange(action.interval_seconds, "视频切换间隔范围")};
      if (action.type === "click") return {id: action.id, type: "click", element_id: action.element_id, button: action.button, click_count: Number(action.click_count), hold_seconds: parseRange(action.hold_seconds, "按住秒数范围"), after_seconds: parseRange(action.after_seconds, "点击后等待范围")};
      if (action.type === "input") { const source = action.content_source === "library" ? "library" : "fixed"; return {id: action.id, type: "input", element_id: action.element_id, content_source: source, fixed_text: source === "fixed" ? (action.fixed_text || "") : "", content_library_id: source === "library" ? (action.content_library_id || "") : "", interval_ms: parseRange(action.interval_ms, "输入间隔范围", true)}; }
      return {id: action.id, type: "wait", duration_seconds: parseRange(action.duration_seconds, "等待秒数范围")};
    }
    function serializeDefinition() {
      const source = state.draft.definition, runMode = el("#v2-strategy-run-mode").value, targetUrl = el("#v2-strategy-target-url").value.trim(), readyElementId = el("#v2-strategy-ready-element").value, timeout = Number(el("#v2-strategy-readiness-timeout").value);
      if (!targetUrl.startsWith("https://") || !readyElementId || !Number.isFinite(timeout) || timeout < 0.1 || timeout > 600) throw new Error("请填写 HTTPS 目标网址、就绪元素和有效超时秒数");
      return {target_url: targetUrl, ready_element_id: readyElementId, readiness_timeout_seconds: timeout, run_mode: runMode, loop_duration_minutes: runMode === "duration" ? parseRange(el("#v2-strategy-minutes").value, "分钟范围") : null, actions: source.actions.map(serializeAction)};
    }
    async function saveStrategy() {
      if (!state.draft) return false;
      const name = el("#v2-strategy-name").value.trim(); if (!name) { setMessage("请填写策略名称"); return false; }
      let definition; try { definition = serializeDefinition(); } catch (error) { setMessage(error.message); return false; }
      const isNew = state.draft.localNew === true;
      const body = isNew ? {id: state.draft.id, name: name, definition: definition, enabled: el("#v2-strategy-enabled").checked} : {expected_revision: state.draft.revision, name: name, definition: definition, enabled: el("#v2-strategy-enabled").checked};
      const method = isNew ? "POST" : "PUT", path = isNew ? API_PREFIX + "/strategies" : API_PREFIX + "/strategies/" + encodeURIComponent(state.draft.id); const result = await request(path, method, body);
      if (!success(result, [200, 201])) { setMessage(errorMessage(result, "保存策略失败")); return false; }
      await load(API_PREFIX + "/strategies", "strategies", ["strategies"]); state.draft = strategyDraft(result.data || state.strategies.find(function (item) { return item.name === name; }) || state.draft); state.draft.localNew = false; setMessage("策略已保存"); render(); return true;
    }
    async function deleteStrategy() { if (!state.draft || state.draft.localNew) { state.draft = null; render(); return true; } const result = await request(API_PREFIX + "/strategies/" + encodeURIComponent(state.draft.id), "DELETE", {expected_revision: state.draft.revision}); if (!success(result, [200, 204])) { setMessage(errorMessage(result, "删除策略失败")); return false; } await load(API_PREFIX + "/strategies", "strategies", ["strategies"]); state.draft = null; setMessage("策略已删除"); render(); return true; }
    function wire() {
      all(".v2-tab").forEach(function (tab) { tab.addEventListener("click", function () { switchView(tab.dataset.view); }); });
      el("#v2-run-form")?.addEventListener("submit", function (event) { event.preventDefault(); startJob(); }); el("#v2-run-cancel")?.addEventListener("click", cancelJob);
      el("#v2-profile-select-all")?.addEventListener("click", function () { setAllProfiles(true); }); el("#v2-profile-clear-all")?.addEventListener("click", function () { setAllProfiles(false); });
      el("#v2-picker-form")?.addEventListener("submit", function (event) { event.preventDefault(); startPicker(); }); el("#v2-picker-finish")?.addEventListener("click", function () { finishPicker(false); }); el("#v2-picker-cancel")?.addEventListener("click", function () { finishPicker(true); });
      el("#v2-picker-profile")?.addEventListener("change", function () { state.pickerProfileToken = el("#v2-picker-profile").value; });
      el("#v2-strategy-new")?.addEventListener("click", function () { state.draft = newStrategy(); render(); }); el("#v2-action-palette")?.addEventListener("click", function (event) { const target = event.target.closest("[data-action-type]"); if (target) addAction(target.dataset.actionType); });
      el("#v2-strategy-name")?.addEventListener("input", function () { if (state.draft) state.draft.name = el("#v2-strategy-name").value; });
      el("#v2-strategy-target-url")?.addEventListener("input", function () { if (state.draft) state.draft.definition.target_url = el("#v2-strategy-target-url").value; });
      el("#v2-strategy-ready-element")?.addEventListener("change", function () { if (state.draft) state.draft.definition.ready_element_id = el("#v2-strategy-ready-element").value; });
      el("#v2-strategy-readiness-timeout")?.addEventListener("input", function () { if (state.draft) state.draft.definition.readiness_timeout_seconds = el("#v2-strategy-readiness-timeout").value; });
      el("#v2-strategy-minutes")?.addEventListener("input", function () { if (state.draft) state.draft.definition.loop_duration_minutes = el("#v2-strategy-minutes").value; });
      el("#v2-strategy-enabled")?.addEventListener("change", function () { if (state.draft) state.draft.enabled = el("#v2-strategy-enabled").checked; });
      el("#v2-strategy-run-mode")?.addEventListener("change", function () { if (!state.draft) return; state.draft.definition.run_mode = el("#v2-strategy-run-mode").value; const wrap = el("#v2-strategy-minutes-wrap"); if (wrap) wrap.hidden = state.draft.definition.run_mode !== "duration"; }); el("#v2-strategy-form")?.addEventListener("submit", function (event) { event.preventDefault(); saveStrategy(); });
      el("#v2-strategy-copy")?.addEventListener("click", function () { if (!state.draft) return; state.draft = {...clone(state.draft), id: "strategy_" + Date.now(), localNew: true, revision: undefined, name: (state.draft.name || "策略") + " 副本"}; render(); }); el("#v2-strategy-delete")?.addEventListener("click", deleteStrategy);
      el("#v2-history-refresh")?.addEventListener("click", async function () { await load(API_PREFIX + "/history", "history", ["history", "jobs"]); setMessage("历史已刷新"); render(); });
      el("#v2-settings-save")?.addEventListener("click", function () { const value = Number(el("#v2-setting-batch-size").value); if (!Number.isInteger(value) || value < 1 || value > 8) { setMessage("默认批次必须在 1 到 8 之间"); return; } state.batchSize = value; if (deps.storage) deps.storage.setItem("browser-v2-batch-size", String(value)); setMessage("本机偏好已保存"); render(); });
    }
    wire();
    return {state: state, init: init, request: request, switchView: switchView, startJob: startJob, cancelJob: cancelJob, startPicker: startPicker, finishPicker: finishPicker, beginRepick: beginRepick, savePickerElement: savePickerElement, setAllProfiles: setAllProfiles, saveStrategy: saveStrategy, addAction: addAction, moveAction: moveAction, removeAction: removeAction, syncPolling: syncPolling, stopPolling: stopPolling, render: render};
  }
  return {API_PREFIX: API_PREFIX, ACTIONS: ACTIONS, apiPath: apiPath, stageLabel: stageLabel, safeEvidencePath: safeEvidencePath, actionTemplate: actionTemplate, newStrategy: newStrategy, browserV2Dependencies: browserV2Dependencies, createBrowserV2UI: createBrowserV2UI};
});
