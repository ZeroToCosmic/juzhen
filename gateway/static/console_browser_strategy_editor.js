(function (root, factory) {
  "use strict";
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.ConsoleBrowserStrategyEditor = api;
  if (root && root.document && typeof root.document.addEventListener === "function") {
    if (root.document.readyState === "loading") {
      root.document.addEventListener("DOMContentLoaded", function () { api.boot(); }, {once: true});
    } else if (!root.__consoleBrowserStrategyEditorBooted) {
      root.__consoleBrowserStrategyEditorBooted = true;
      api.boot();
    }
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function (root) {
  "use strict";

  const core = (typeof module === "object" && module.exports)
    ? require("./browser_strategy_editor_core")
    : root.BrowserStrategyEditorCore;

  if (!core) throw new Error("BrowserStrategyEditorCore 未加载");

  const ACTIONS = core.ACTIONS;

  function canonicalEditUrl(strategyId) {
    return "/console/actions/browser-strategies/" + encodeURIComponent(String(strategyId)) + "/edit";
  }

  function elementId(item) {
    return item && (item.id || item.element_id || item.selector_id);
  }

  function listValue(value, keys) {
    if (Array.isArray(value)) return value;
    if (!value || typeof value !== "object") return [];
    for (const key of keys) if (Array.isArray(value[key])) return value[key];
    return [];
  }

  function unwrapStrategy(value) {
    if (!value || typeof value !== "object") return value;
    return value.strategy || value.data || value;
  }

  function defaultRequestJson(browserRoot) {
    return async function requestJson(url, method, body) {
      const fetcher = browserRoot && browserRoot.fetch;
      if (typeof fetcher !== "function") throw new Error("浏览器请求不可用");
      const verb = String(method || "GET").toUpperCase();
      const options = {method: verb, credentials: "same-origin"};
      if (body !== undefined && verb !== "GET" && verb !== "HEAD") {
        options.headers = {"Content-Type": "application/json"};
        options.body = JSON.stringify(body);
      }
      const response = await fetcher.call(browserRoot, url, options);
      let data = null;
      try { data = await response.json(); } catch (_) { data = null; }
      return {status: response.status, data};
    };
  }

  function createConsoleStrategyEditor(options) {
    const opts = options || {};
    const document = opts.document || (root && root.document);
    if (!document) throw new TypeError("document is required");
    const repository = opts.repository || core.createStrategyRepository(
      opts.requestJson || defaultRequestJson(opts.root || root),
    );
    const bootstrap = opts.bootstrap || readBootstrap(document);
    const history = opts.history || (opts.root || root).history;
    const location = opts.location || (opts.root || root).location;
    const confirm = opts.confirm || (opts.root || root).confirm || function () { return true; };
    const idFactory = opts.idFactory || function () { return "strategy_" + Date.now(); };
    const state = {
      mode: bootstrap && bootstrap.mode === "edit" ? "edit" : "new",
      strategyId: bootstrap && bootstrap.strategy_id ? String(bootstrap.strategy_id) : "",
      draft: null,
      elements: [],
      contentLibraries: [],
      status: "",
      saving: false,
      loading: false,
      loadFailed: false,
      initialized: false,
      bound: false,
      loadPromise: null,
    };

    function byId(id) { return document.getElementById(id); }
    function setStatus(message) {
      state.status = message || "";
      const node = byId("strategy-status");
      if (node) node.textContent = state.status;
    }
    function toggleClass(node, className, enabled) {
      if (!node) return;
      const classes = String(node.className || "").split(/\s+/).filter(Boolean);
      const index = classes.indexOf(className);
      if (enabled && index === -1) classes.push(className);
      if (!enabled && index !== -1) classes.splice(index, 1);
      node.className = classes.join(" ");
    }
    function applyEditorState() {
      const loading = state.loading;
      const disabled = state.loadFailed;
      const editor = byId("console-browser-strategy-editor");
      if (editor) {
        toggleClass(editor, "is-loading", loading);
        toggleClass(editor, "is-disabled", disabled);
        toggleClass(editor, "is-error", disabled);
        editor.setAttribute("data-loading", String(loading));
        editor.setAttribute("data-disabled", String(disabled));
        editor.setAttribute("data-state", disabled ? "error" : (loading ? "loading" : "ready"));
        editor.setAttribute("aria-disabled", String(disabled));
      }
      const formDisabled = loading || disabled;
      [
        "strategy-name", "strategy-target-url", "strategy-ready-element",
        "strategy-readiness-timeout", "strategy-run-mode", "strategy-minutes", "strategy-enabled",
      ].forEach(function (id) {
        const node = byId(id);
        if (node) node.disabled = formDisabled;
      });
      const palette = byId("strategy-action-palette");
      if (palette) {
        palette.disabled = formDisabled;
        palette.setAttribute("aria-disabled", String(formDisabled));
      }
      const save = byId("strategy-save");
      if (save) {
        save.textContent = disabled ? "重新加载" : "保存策略";
        save.disabled = loading || (!disabled && state.saving);
      }
      const copy = byId("strategy-copy");
      const remove = byId("strategy-delete");
      if (copy) copy.hidden = state.mode === "new" || loading || disabled;
      if (remove) remove.hidden = state.mode === "new" || loading || disabled;
      const status = byId("strategy-status");
      if (status) {
        toggleClass(status, "error", disabled);
        status.setAttribute("data-state", disabled ? "error" : "normal");
        status.setAttribute("aria-busy", String(loading));
      }
    }
    function rangeText(value) { return Array.isArray(value) ? value.join("-") : (value == null ? "" : String(value)); }
    function makeNode(tag, text, className) {
      const node = document.createElement(tag);
      if (text !== undefined && text !== null) node.textContent = String(text);
      if (className) node.className = className;
      return node;
    }
    function makeOption(value, label, selected, disabled) {
      const option = makeNode("option", label);
      option.value = value == null ? "" : String(value);
      option.selected = Boolean(selected);
      option.disabled = Boolean(disabled);
      return option;
    }
    function fillSelect(select, items, selected, emptyLabel) {
      if (!select) return;
      select.replaceChildren();
      if (emptyLabel) select.append(makeOption("", emptyLabel, !selected, false));
      (items || []).forEach(function (item) {
        const id = elementId(item);
        if (!id) return;
        select.append(makeOption(id, item.name || item.label || id, String(id) === String(selected), false));
      });
      select.value = selected || "";
    }
    function eligible(purpose, kinds) {
      return core.eligibleElements(state.elements, purpose, kinds);
    }
    function actionField(action, index, property, label, type, value) {
      const labelNode = makeNode("label");
      labelNode.append(makeNode("span", label));
      const input = makeNode(type === "textarea" ? "textarea" : "input", undefined, "console-control");
      if (type && type !== "textarea") input.type = type;
      input.value = value === undefined ? rangeText(action[property]) : value;
      input.dataset.actionIndex = String(index);
      input.dataset.actionField = property;
      labelNode.append(input);
      return labelNode;
    }
    function actionElementField(action, index, kinds) {
      const label = makeNode("label");
      label.append(makeNode("span", "目标元素"));
      const select = makeNode("select", undefined, "console-control");
      fillSelect(select, eligible("action", kinds), action.element_id, "请选择页面元素");
      select.dataset.actionIndex = String(index);
      select.dataset.actionField = "element_id";
      label.append(select);
      return label;
    }
    function actionSelect(action, index, property, labelText, values) {
      const label = makeNode("label");
      label.append(makeNode("span", labelText));
      const select = makeNode("select", undefined, "console-control");
      values.forEach(function (pair) {
        select.append(makeOption(pair[0], pair[1], String(pair[0]) === String(action[property]), false));
      });
      select.value = action[property];
      select.dataset.actionIndex = String(index);
      select.dataset.actionField = property;
      label.append(select);
      return label;
    }
    function actionCard(action, index) {
      const card = makeNode("li", undefined, "strategy-action-card");
      card.dataset.actionIndex = String(index);
      const header = makeNode("header", undefined, "console-section-head");
      header.append(makeNode("strong", ACTIONS[action.type] ? ACTIONS[action.type].label : action.type));
      const controls = makeNode("div", undefined, "console-actions");
      [["上移", "move-up", index === 0], ["下移", "move-down", index === state.draft.definition.actions.length - 1], ["删除", "remove", false]].forEach(function (item) {
        const button = makeNode("button", item[0], "console-button" + (item[1] === "remove" ? " danger" : ""));
        button.type = "button";
        button.dataset.actionCommand = item[1];
        button.dataset.actionIndex = String(index);
        button.disabled = item[2] || state.loading || state.loadFailed;
        controls.append(button);
      });
      header.append(controls);
      const fields = makeNode("div", undefined, "strategy-action-fields");
      card.append(header, fields);
      if (action.type === "move") {
        fields.append(actionElementField(action, index), actionField(action, index, "duration_seconds", "移动秒数范围，例如 0.2-0.5"));
      } else if (action.type === "scroll") {
        fields.append(actionSelect(action, index, "direction", "方向", [["down", "向下"], ["up", "向上"]]));
        fields.append(actionField(action, index, "count", "视频切换次数范围，例如 1-2"));
        fields.append(actionField(action, index, "interval_seconds", "切换完成后的间隔秒数范围，例如 0.2-0.5"));
      } else if (action.type === "click") {
        fields.append(actionElementField(action, index, ["click", "generic"]));
        fields.append(actionSelect(action, index, "button", "鼠标按键", [["left", "left"], ["middle", "middle"], ["right", "right"]]));
        fields.append(actionField(action, index, "click_count", "点击次数", "number"));
        fields.append(actionField(action, index, "hold_seconds", "按住秒数范围，例如 0.05-0.1"));
        fields.append(actionField(action, index, "after_seconds", "点击后等待范围，例如 0.3-0.6"));
      } else if (action.type === "input") {
        fields.append(actionElementField(action, index, ["input"]));
        const source = actionSelect(action, index, "content_source", "文案来源", [["fixed", "固定文案"], ["library", "文案库随机抽取"]]);
        fields.append(source);
        const fixed = actionField(action, index, "fixed_text", "输入文案", "textarea");
        const library = makeNode("label");
        library.append(makeNode("span", "选择文案库"));
        const librarySelect = makeNode("select", undefined, "console-control");
        librarySelect.append(makeOption("", "请选择文案库", !action.content_library_id, false));
        state.contentLibraries.forEach(function (item) {
          const count = Number(item.copy_count) || 0;
          librarySelect.append(makeOption(item.id, (item.name || item.id) + " (" + count + " 条)", item.id === action.content_library_id, count <= 0));
        });
        librarySelect.value = action.content_library_id || "";
        librarySelect.dataset.actionIndex = String(index);
        librarySelect.dataset.actionField = "content_library_id";
        library.append(librarySelect);
        fixed.hidden = action.content_source === "library";
        library.hidden = action.content_source !== "library";
        fields.append(fixed, library, actionField(action, index, "interval_ms", "输入间隔毫秒范围，例如 40-120"));
      } else if (action.type === "wait") {
        fields.append(actionField(action, index, "duration_seconds", "等待秒数范围，例如 1-2"));
      }
      return card;
    }
    function render() {
      applyEditorState();
      const draft = state.draft;
      const definition = draft && draft.definition;
      const name = byId("strategy-name");
      const targetUrl = byId("strategy-target-url");
      const ready = byId("strategy-ready-element");
      const timeout = byId("strategy-readiness-timeout");
      const runMode = byId("strategy-run-mode");
      const minutes = byId("strategy-minutes");
      const minutesWrap = byId("strategy-minutes-wrap");
      const enabled = byId("strategy-enabled");
      const revision = byId("strategy-revision");
      const list = byId("strategy-action-list");
      const empty = byId("strategy-action-empty");
      if (!draft || !definition) {
        setStatus(state.status);
        return;
      }
      if (name) name.value = draft.name || "";
      if (targetUrl) targetUrl.value = definition.target_url || "";
      fillSelect(ready, eligible("readiness"), definition.ready_element_id, "请选择页面元素");
      if (timeout) timeout.value = definition.readiness_timeout_seconds == null ? "" : definition.readiness_timeout_seconds;
      if (runMode) runMode.value = definition.run_mode || "once";
      if (minutes) minutes.value = rangeText(definition.loop_duration_minutes);
      if (minutesWrap) minutesWrap.hidden = definition.run_mode !== "duration";
      if (enabled) enabled.checked = draft.enabled !== false;
      if (revision) revision.textContent = draft.revision == null ? "未保存" : "版本 " + draft.revision;
      const copy = byId("strategy-copy");
      const remove = byId("strategy-delete");
      if (copy) copy.hidden = state.mode === "new" || state.loading || state.loadFailed;
      if (remove) remove.hidden = state.mode === "new" || state.loading || state.loadFailed;
      if (list) {
        list.replaceChildren();
        (definition.actions || []).forEach(function (action, index) { list.append(actionCard(action, index)); });
      }
      if (empty) empty.hidden = Boolean(definition.actions && definition.actions.length);
      setStatus(state.status);
    }
    function syncForm() {
      if (!state.draft) return;
      const definition = state.draft.definition;
      const value = function (id) { const node = byId(id); return node ? node.value : ""; };
      state.draft.name = value("strategy-name").trim();
      state.draft.enabled = Boolean(byId("strategy-enabled") && byId("strategy-enabled").checked);
      definition.target_url = value("strategy-target-url").trim();
      definition.ready_element_id = value("strategy-ready-element");
      definition.readiness_timeout_seconds = value("strategy-readiness-timeout");
      definition.run_mode = value("strategy-run-mode");
      definition.loop_duration_minutes = definition.run_mode === "duration" ? value("strategy-minutes") : null;
    }
    function handleActionField(event) {
      const target = event.target;
      if (!target || !target.dataset || target.dataset.actionField == null || !state.draft) return;
      const index = Number(target.dataset.actionIndex);
      const action = state.draft.definition.actions[index];
      if (!action) return;
      const field = target.dataset.actionField;
      action[field] = target.value;
      if (field === "content_source") {
        if (target.value === "library") action.fixed_text = "";
        else action.content_library_id = "";
        render();
      }
    }
    function handleActionCommand(event) {
      const target = event.target && (event.target.closest ? event.target.closest("[data-action-command]") : event.target);
      if (!target || !target.dataset || !state.draft) return;
      const index = Number(target.dataset.actionIndex);
      const command = target.dataset.actionCommand;
      if (command === "move-up") core.moveAction(state.draft, index, -1);
      if (command === "move-down") core.moveAction(state.draft, index, 1);
      if (command === "remove") core.removeAction(state.draft, index);
      render();
    }
    function bind() {
      if (state.bound) return;
      state.bound = true;
      const save = byId("strategy-save");
      if (save) save.addEventListener("click", function () {
        if (state.loadFailed) init();
        else saveStrategy();
      });
      const copy = byId("strategy-copy");
      if (copy) copy.addEventListener("click", function () { copyStrategy(); });
      const remove = byId("strategy-delete");
      if (remove) remove.addEventListener("click", function () { deleteStrategy(); });
      const palette = byId("strategy-action-palette");
      if (palette) palette.addEventListener("click", function (event) {
        const target = event.target && (event.target.closest ? event.target.closest("[data-action-type]") : event.target);
        if (!target || !target.dataset || !target.dataset.actionType || !state.draft) return;
        core.addAction(state.draft, target.dataset.actionType);
        render();
      });
      const actionList = byId("strategy-action-list");
      if (actionList) {
        actionList.addEventListener("input", handleActionField);
        actionList.addEventListener("change", function (event) { handleActionField(event); handleActionCommand(event); });
        actionList.addEventListener("click", handleActionCommand);
      }
      ["strategy-name", "strategy-target-url", "strategy-ready-element", "strategy-readiness-timeout", "strategy-run-mode", "strategy-minutes", "strategy-enabled"].forEach(function (id) {
        const node = byId(id);
        if (!node) return;
        node.addEventListener(node.type === "checkbox" || node.tagName === "SELECT" ? "change" : "input", function () { syncForm(); if (id === "strategy-run-mode") render(); });
      });
    }
    async function loadEditor() {
      state.loading = true;
      state.loadFailed = false;
      state.initialized = false;
      state.status = "正在加载策略…";
      render();
      bind();
      try {
        const dependencies = await repository.loadDependencies();
        state.elements = listValue(dependencies && dependencies[0], ["elements", "items"]);
        state.contentLibraries = listValue(dependencies && dependencies[1], ["content_libraries", "libraries", "items"]);
        if (state.mode === "edit") {
          state.draft = core.normalizeStrategyDraft(await repository.load(state.strategyId));
          state.draft.localNew = false;
        } else {
          state.draft = core.createStrategyDraft(idFactory());
        }
        state.initialized = true;
        state.loadFailed = false;
        state.status = "";
        render();
      } catch (error) {
        state.draft = null;
        state.initialized = false;
        state.loadFailed = true;
        state.status = "加载策略失败：" + ((error && error.message) || "请检查连接后重试");
        render();
      } finally {
        state.loading = false;
        render();
      }
      return state;
    }
    async function init() {
      bind();
      if (state.loading) return state.loadPromise || state;
      if (state.initialized && !state.loadFailed) return state;
      state.loadPromise = loadEditor();
      try {
        return await state.loadPromise;
      } finally {
        state.loadPromise = null;
      }
    }
    async function saveStrategy() {
      if (!state.draft || state.saving) return false;
      syncForm();
      if (!state.draft.name) { setStatus("请填写策略名称"); return false; }
      state.saving = true;
      render();
      try {
        const saved = unwrapStrategy(state.mode === "new" || state.draft.localNew
          ? await repository.create(state.draft)
          : await repository.update(state.draft));
        const next = core.normalizeStrategyDraft({...state.draft, ...(saved || {}), definition: (saved && saved.definition) || state.draft.definition});
        next.localNew = false;
        state.draft = next;
        state.strategyId = next.id;
        const wasNew = state.mode === "new";
        state.mode = "edit";
        state.status = "策略已保存";
        if (wasNew && history && typeof history.replaceState === "function") history.replaceState({}, "", canonicalEditUrl(next.id));
        render();
        return true;
      } catch (error) {
        state.status = error && error.code === "revision_conflict" ? "数据已更新，请重新加载" : (error && error.message) || "保存策略失败";
        render();
        return false;
      } finally {
        state.saving = false;
        render();
      }
    }
    async function copyStrategy() {
      if (!state.draft || state.saving || state.mode === "new") return false;
      syncForm();
      const copy = core.duplicateStrategyDraft(state.draft, idFactory());
      state.saving = true;
      render();
      try {
        const saved = unwrapStrategy(await repository.create(copy));
        state.draft = core.normalizeStrategyDraft({...copy, ...(saved || {}), definition: (saved && saved.definition) || copy.definition});
        state.draft.localNew = false;
        state.strategyId = state.draft.id;
        state.mode = "edit";
        state.status = "副本已创建";
        if (history && typeof history.replaceState === "function") history.replaceState({}, "", canonicalEditUrl(state.draft.id));
        render();
        return true;
      } catch (error) {
        state.status = (error && error.message) || "复制策略失败";
        render();
        return false;
      } finally {
        state.saving = false;
        render();
      }
    }
    async function deleteStrategy() {
      if (!state.draft || state.mode === "new") return false;
      if (!confirm("确定删除此浏览器策略吗？")) return false;
      try {
        await repository.remove(state.draft);
        if (location && typeof location.assign === "function") location.assign("/console/actions");
        return true;
      } catch (error) {
        state.status = (error && error.message) || "删除策略失败";
        render();
        return false;
      }
    }
    return {
      state,
      init,
      render,
      save: saveStrategy,
      saveStrategy,
      copy: copyStrategy,
      copyStrategy,
      remove: deleteStrategy,
      deleteStrategy,
      repository,
    };
  }

  function readBootstrap(document) {
    const node = document && document.getElementById("console-browser-strategy-bootstrap");
    if (!node) return {mode: "new", strategy_id: ""};
    try { return JSON.parse(node.textContent || node.innerText || "{}"); } catch (_) { return {mode: "new", strategy_id: ""}; }
  }

  function boot(options) {
    const opts = {...(options || {})};
    if (!opts.root) opts.root = root;
    if (!opts.document) opts.document = opts.root && opts.root.document;
    const controller = createConsoleStrategyEditor(opts);
    controller.init();
    return controller;
  }

  return {canonicalEditUrl, createConsoleStrategyEditor, boot};
});
