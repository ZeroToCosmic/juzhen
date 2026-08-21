(function (root, factory) {
  "use strict";
  const exported = factory(root);
  if (typeof module === "object" && module.exports) module.exports = exported;
  if (root && root.document) {
    let controller = null;
    root.BrowserStrategyUI = {
      createBrowserStrategyUI: exported.createBrowserStrategyUI,
      init: function () {
        if (!controller) controller = exported.createBrowserStrategyUI(exported.browserDependencies(root));
        return controller.init();
      },
    };
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function (root) {
  "use strict";

  const TYPES = ["move", "click", "scroll_up", "scroll_down", "keyboard_input", "pause"];
  const SCROLL_WHEEL_DELTA = 120;

  function clone(value) {
    return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
  }

  function errorMessage(result, fallback) {
    return result && result.data && result.data.error ? result.data.error : fallback;
  }

  function createBrowserStrategyUI(dependencies) {
    const deps = dependencies || {};
    const state = {
      elements: {},
      patterns: [],
      strategies: [],
      catalog: {},
      defaults: {},
      brands: [],
      view: "list",
      draft: null,
      dirty: false,
      saveMessage: "",
      reloadRequired: false,
      elementDialog: {open: false, error: "", draft: null},
      patternError: "",
      recording: null,
      recordingTimer: null,
    };
    const beforeUnload = function (event) {
      event.preventDefault();
      event.returnValue = "";
      return "";
    };
    let initPromise = null;
    let recordingGeneration = 0;
    let activeRecordingGeneration = 0;
    let recordingStartOperation = null;
    let recordingFinalizeOperation = null;

    function render(view) {
      if (typeof deps.render === "function") deps.render(view || "all", state, api);
    }

    async function request(url, method, body) {
      try {
        const result = await deps.requestJson(url, method || "GET", body);
        return result || {status: 0, data: {error: "请求失败"}};
      } catch (error) {
        return {status: 0, data: {error: error && error.message ? error.message : "请求失败"}};
      }
    }

    function init() {
      if (initPromise) return initPromise;
      initPromise = (async function () {
        if (deps.selectorProbe && typeof deps.selectorProbe.init === "function") {
          await deps.selectorProbe.init();
        }
        const specs = [
          ["/api/browser/elements", "elements", "elements"],
          ["/api/browser/patterns", "patterns", "patterns"],
          ["/api/browser/strategies", "strategies", "strategies"],
          ["/api/browser/action-catalog", "catalog", "catalog"],
          ["/api/content/brands", "brands", "brands"],
        ];
        for (const [url, target, key] of specs) {
          const result = await request(url, "GET");
          if (result.status !== 200) throw new Error(errorMessage(result, `加载失败：${url}`));
          state[target] = clone(result.data[key]);
          if (target === "catalog") state.defaults = clone(result.data.defaults || {});
        }
        state.view = "list";
        state.reloadRequired = false;
        render("all");
        return state;
      })().catch((error) => {
        initPromise = null;
        throw error;
      });
      return initPromise;
    }

    function syncElementOptions(selectors) {
      const options = [{value: "", label: "请选择元素"}].concat(
        Object.keys(state.elements).map((alias) => ({value: alias, label: alias})),
      );
      (selectors || []).forEach((select) => {
        const previous = select.value;
        if (typeof select.replaceChildren === "function" && select.ownerDocument) {
          const nodes = options.map((item) => {
            const option = select.ownerDocument.createElement("option");
            option.value = item.value;
            option.textContent = item.label;
            return option;
          });
          select.replaceChildren(...nodes);
        } else {
          select.options = clone(options);
        }
        select.value = options.some((item) => item.value === previous) ? previous : "";
      });
      return options;
    }

    function syncExecutionOptions(select) {
      if (!select) return [];
      const previous = select.value;
      const options = [{value: "", label: "选择执行策略", disabled: false}].concat(
        state.strategies.map((strategy) => ({
          value: strategy.id,
          label: strategy.name,
          disabled: strategy.status === "needs_repair",
        })),
      );
      if (typeof select.replaceChildren === "function" && select.ownerDocument) {
        select.replaceChildren(...options.map((item) => {
          const option = select.ownerDocument.createElement("option");
          option.value = item.value;
          option.textContent = item.label;
          option.disabled = item.disabled;
          return option;
        }));
      } else {
        select.options = clone(options);
      }
      select.value = options.some((item) => item.value === previous && !item.disabled) ? previous : "";
      return options;
    }

    function elementDefinition(value) {
      if (value && typeof value === "object" && !Array.isArray(value) && Array.isArray(value.locators)) {
        return {
          scope: ["page", "active_video", "visible_comment_panel"].includes(value.scope) ? value.scope : "page",
          locators: clone(value.locators),
        };
      }
      if (typeof value === "string") {
        return {scope: "page", locators: [{id: "legacy_xpath", type: "xpath", value, enabled: true, fallback: true}]};
      }
      return {scope: "page", locators: [{id: deps.nowId("locator"), type: "xpath", value: "", enabled: true}]};
    }

    function openElementDialog(input) {
      const source = typeof input === "string" ? {selector: input} : (input || {});
      state.elementDialog = {
        open: true,
        error: "",
        testResults: [],
        draft: {
          alias: String(source.alias || ""),
          originalAlias: String(source.originalAlias || ""),
          definition: elementDefinition(source.definition === undefined ? source.selector : source.definition),
        },
      };
      render("elements");
    }

    function currentElementDefinition() {
      const draft = state.elementDialog.draft;
      return draft && draft.definition;
    }

    function addElementLocator(candidate) {
      const definition = currentElementDefinition();
      if (!definition) return false;
      const next = Object.assign({id: deps.nowId("locator"), type: "xpath", value: "", enabled: true}, clone(candidate || {}));
      next.id = next.id || deps.nowId("locator");
      next.enabled = next.enabled !== false;
      definition.locators.push(next);
      render("elements");
      return next;
    }

    function updateElementLocator(locatorId, patch) {
      const definition = currentElementDefinition();
      if (!definition) return false;
      const index = definition.locators.findIndex((item) => item.id === locatorId);
      if (index < 0) return false;
      const current = definition.locators[index];
      const next = Object.assign({}, current, clone(patch || {}), {id: locatorId});
      if (patch && patch.type && patch.type !== current.type) {
        const shared = {id: locatorId, type: patch.type, enabled: current.enabled !== false};
        if (current.fallback === true) shared.fallback = true;
        if (patch.type === "attribute") Object.assign(shared, {name: "data-testid", value: ""});
        else if (patch.type === "role") Object.assign(shared, {role: "button", name: "", name_mode: "exact"});
        else Object.assign(shared, {value: ""});
        definition.locators[index] = shared;
      } else definition.locators[index] = next;
      render("elements");
      return true;
    }

    function moveElementLocator(locatorId, offset) {
      const definition = currentElementDefinition();
      if (!definition) return false;
      const index = definition.locators.findIndex((item) => item.id === locatorId);
      const destination = index + Number(offset);
      if (index < 0 || destination < 0 || destination >= definition.locators.length) return false;
      const [candidate] = definition.locators.splice(index, 1);
      definition.locators.splice(destination, 0, candidate);
      render("elements");
      return true;
    }

    function removeElementLocator(locatorId) {
      const definition = currentElementDefinition();
      if (!definition || definition.locators.length <= 1) return false;
      const next = definition.locators.filter((item) => item.id !== locatorId);
      if (next.length === definition.locators.length) return false;
      definition.locators = next;
      render("elements");
      return true;
    }

    function setElementScope(scope) {
      const definition = currentElementDefinition();
      if (!definition || !["page", "active_video", "visible_comment_panel"].includes(scope)) return false;
      definition.scope = scope;
      render("elements");
      return true;
    }

    function elementTestResultCode(elementResult, windowResult) {
      return String((elementResult && elementResult.code) || (windowResult && windowResult.code) || "");
    }

    async function testElementDraft(windows) {
      const draft = state.elementDialog.draft;
      if (!draft || !draft.alias) return false;
      const result = await request("/api/browser/elements/test", "POST", {
        windows: clone(windows === undefined ? deps.selectedBrowserWindows() : windows),
        elements: {[draft.alias]: clone(draft.definition)},
      });
      if (result.status !== 200) {
        state.elementDialog.error = errorMessage(result, "元素测试失败");
        render("elements");
        return false;
      }
      state.elementDialog.error = "";
      state.elementDialog.testResults = clone(result.data.results || []);
      render("elements");
      return true;
    }

    async function applyTikTokCommentTemplate(alias) {
      const draft = state.elementDialog.draft;
      if (!draft) return false;
      if (typeof deps.confirm === "function" && !deps.confirm("应用 TikTok 评论模板会替换当前草稿，是否继续？")) return false;
      const result = await request("/api/browser/elements/templates/tiktok-comment", "GET");
      if (result.status !== 200 || !result.data || !result.data.elements) {
        state.elementDialog.error = errorMessage(result, "模板加载失败");
        render("elements");
        return false;
      }
      const requestedAlias = alias || draft.alias;
      const entries = Object.entries(result.data.elements);
      const selected = entries.find(([name]) => name === requestedAlias) || entries[0];
      if (!selected) {
        state.elementDialog.error = "模板不包含元素定义";
        render("elements");
        return false;
      }
      draft.alias = requestedAlias || selected[0];
      draft.definition = elementDefinition(selected[1]);
      state.elementDialog.error = "";
      state.elementDialog.testResults = [];
      render("elements");
      return true;
    }

    async function saveElements(elements, renameFrom) {
      const body = {elements: clone(elements)};
      if (renameFrom) body.rename_from = renameFrom;
      const result = await request("/api/browser/elements", "PUT", body);
      if (result.status !== 200) {
        state.elementDialog.open = true;
        state.elementDialog.error = errorMessage(result, "元素保存失败");
        render("elements");
        return false;
      }
      state.elements = clone(result.data.elements);
      if (typeof deps.targetElementSelectors === "function") {
        syncElementOptions(deps.targetElementSelectors());
      }
      if (renameFrom) {
        const strategies = await request("/api/browser/strategies", "GET");
        if (strategies.status === 200) {
          state.strategies = clone(strategies.data.strategies);
          state.reloadRequired = false;
        } else {
          state.strategies = [];
          state.draft = null;
          state.view = "list";
          state.reloadRequired = true;
          state.saveMessage = "元素已保存，但策略状态加载失败；请刷新页面后继续";
        }
      }
      state.elementDialog = {open: false, error: "", draft: null, testResults: []};
      render(renameFrom ? "all" : "elements");
      return true;
    }

    async function deleteElement(alias) {
      const next = clone(state.elements);
      delete next[alias];
      return saveElements(next);
    }

    function createStrategy(name) {
      state.draft = {
        id: deps.nowId("strategy"),
        name: String(name || "新策略").trim() || "新策略",
        run_mode: "once",
        batch_size: 4,
        actions: [],
      };
      state.view = "editor";
      markDirty();
      render("editor");
      return state.draft;
    }

    function openStrategy(strategyId) {
      const strategy = state.strategies.find((item) => item.id === strategyId);
      if (!strategy) return false;
      state.draft = clone(strategy);
      state.view = "editor";
      clearDirty();
      state.saveMessage = "已保存";
      render("editor");
      return true;
    }

    function renameStrategy(name) {
      if (!state.draft) return false;
      state.draft.name = String(name || "").trim();
      markDirty();
      render("editor");
      return true;
    }

    function addBlock(type) {
      if (!state.draft) throw new Error("请先打开策略");
      if (!TYPES.includes(type) || !state.catalog[type] || !state.defaults[type]) throw new Error(`未知动作：${type}`);
      const action = {id: deps.nowId("action"), type, params: clone(state.defaults[type])};
      state.draft.actions.push(action);
      markDirty();
      render("editor");
      return action;
    }

    function findAction(actionId) {
      return state.draft && state.draft.actions.find((item) => item.id === actionId);
    }

    function parameterFields(type) {
      const fields = {
        move: [
          {name: "target_mode", label: "目标模式"}, {name: "element", label: "目标元素"},
          {name: "delta_viewport.0", label: "水平位移比例"}, {name: "delta_viewport.1", label: "垂直位移比例"},
          {name: "trajectory", label: "移动轨迹"}, {name: "duration_seconds.0", label: "持续秒数最小值"}, {name: "duration_seconds.1", label: "持续秒数最大值"},
        ],
        click: [
          {name: "element", label: "目标元素"}, {name: "button", label: "鼠标键"}, {name: "click_count", label: "点击次数"},
          {name: "hold_seconds.0", label: "按下秒数最小值"}, {name: "hold_seconds.1", label: "按下秒数最大值"}, {name: "trajectory", label: "接近轨迹"},
        ],
        scroll_up: [
          {name: "total_count.0", label: "最少切换视频数"},
          {name: "total_count.1", label: "最多切换视频数"},
          {name: "interval_seconds.0", label: "最小切换间隔秒数"},
          {name: "interval_seconds.1", label: "最大切换间隔秒数"},
        ],
        keyboard_input: [{name: "element", label: "输入元素"}, {name: "content.source", label: "内容来源"}, {name: "content.text", label: "固定文本"}, {name: "content.brand_id", label: "文案品牌"}, {name: "typing", label: "输入节奏"}, {name: "typing.interval_ms.0", label: "每字符间隔毫秒最小值"}, {name: "typing.interval_ms.1", label: "每字符间隔毫秒最大值"}],
        pause: [{name: "duration_seconds.0", label: "等待秒数最小值"}, {name: "duration_seconds.1", label: "等待秒数最大值"}],
      };
      fields.scroll_down = fields.scroll_up;
      return clone(fields[type] || []);
    }

    function parseScrollParameters(form, action) {
      function value(name, label) {
        const input = form && form.elements && form.elements.namedItem(name);
        const raw = input && input.value;
        if (raw === null || raw === undefined || String(raw).trim() === "") throw new Error(`${label}必须是数字`);
        const parsed = Number(raw);
        if (!Number.isFinite(parsed)) throw new Error(`${label}必须是数字`);
        return parsed;
      }
      function positiveVideoSwitch(name) {
        const parsed = value(name, "切换视频数");
        if (!Number.isInteger(parsed) || parsed < 1) throw new Error("切换视频数必须是正整数");
        return parsed;
      }
      const totalCount = [positiveVideoSwitch("total_count.0"), positiveVideoSwitch("total_count.1")];
      if (totalCount[0] > totalCount[1]) throw new Error("最少切换视频数不能大于最多切换视频数");
      const intervalSeconds = [
        value("interval_seconds.0", "最小切换间隔秒数"),
        value("interval_seconds.1", "最大切换间隔秒数"),
      ];
      if (intervalSeconds.some((item) => item < 0)) throw new Error("间隔秒数不能为负数");
      if (intervalSeconds[0] > intervalSeconds[1]) throw new Error("最小切换间隔秒数不能大于最大切换间隔秒数");
      const existingBurst = action && action.params && action.params.burst_count;
      return {
        distance: SCROLL_WHEEL_DELTA,
        total_count: totalCount,
        burst_count: clone(existingBurst === undefined ? [1, 1] : existingBurst),
        interval_seconds: intervalSeconds,
      };
    }

    function sanitizeActionParams(type, params) {
      const clean = clone(params);
      if (type === "move" && clean.target_mode === "viewport") clean.element = "";
      if (type === "keyboard_input" && clean.content) {
        if (clean.content.source === "fixed") clean.content.brand_id = "";
        if (clean.content.source === "generated_comment") clean.content.text = "";
      }
      return clean;
    }

    function updateBlock(actionId, params) {
      const action = findAction(actionId);
      if (!action) return false;
      action.params = sanitizeActionParams(action.type, params);
      markDirty();
      render("editor");
      return true;
    }

    function moveBlock(actionId, offset) {
      if (!state.draft) return false;
      const index = state.draft.actions.findIndex((item) => item.id === actionId);
      const destination = index + Number(offset);
      if (index < 0 || destination < 0 || destination >= state.draft.actions.length) return false;
      const [action] = state.draft.actions.splice(index, 1);
      state.draft.actions.splice(destination, 0, action);
      markDirty();
      render("editor");
      return true;
    }

    function deleteBlock(actionId) {
      if (!state.draft) return false;
      const next = state.draft.actions.filter((item) => item.id !== actionId);
      if (next.length === state.draft.actions.length) return false;
      state.draft.actions = next;
      markDirty();
      render("editor");
      return true;
    }

    function serializeStrategyForm() {
      if (!state.draft) return null;
      const strategy = {
        id: state.draft.id,
        name: String(state.draft.name || "").trim(),
        run_mode: state.draft.run_mode,
        batch_size: Number(state.draft.batch_size),
        actions: clone(state.draft.actions),
      };
      if (strategy.run_mode === "loop") strategy.loop_duration_minutes = clone(state.draft.loop_duration_minutes || [1, 1]);
      return strategy;
    }

    function markDirty() {
      if (!state.dirty && typeof deps.addBeforeUnload === "function") deps.addBeforeUnload(beforeUnload);
      state.dirty = true;
      state.saveMessage = "未保存";
      render("save");
    }

    function clearDirty() {
      if (state.dirty && typeof deps.removeBeforeUnload === "function") deps.removeBeforeUnload(beforeUnload);
      state.dirty = false;
    }

    async function saveStrategy() {
      if (state.reloadRequired) {
        state.saveMessage = "策略状态需要重新加载，请刷新页面后再保存";
        render("all");
        return false;
      }
      const strategy = serializeStrategyForm();
      if (!strategy) return false;
      const index = state.strategies.findIndex((item) => item.id === strategy.id);
      const proposed = clone(state.strategies);
      if (index < 0) proposed.push(strategy);
      else proposed[index] = strategy;
      const result = await request("/api/browser/strategies", "PUT", {strategies: proposed});
      if (result.status !== 200) {
        state.saveMessage = errorMessage(result, "策略保存失败");
        state.dirty = true;
        render("editor");
        return false;
      }
      state.strategies = clone(result.data.strategies);
      state.draft = clone(state.strategies.find((item) => item.id === strategy.id) || null);
      clearDirty();
      state.saveMessage = "已保存";
      render("all");
      return true;
    }

    async function deleteStrategy(strategyId) {
      if (typeof deps.confirm === "function" && !deps.confirm("确认删除这个策略？")) return false;
      const proposed = state.strategies.filter((item) => item.id !== strategyId);
      const result = await request("/api/browser/strategies", "PUT", {strategies: proposed});
      if (result.status !== 200) {
        state.saveMessage = errorMessage(result, "策略删除失败");
        render("all");
        return false;
      }
      state.strategies = clone(result.data.strategies);
      if (state.draft && state.draft.id === strategyId) {
        state.draft = null;
        state.view = "list";
        clearDirty();
      }
      render("all");
      return true;
    }

    async function savePatterns(patterns) {
      const result = await request("/api/browser/patterns", "PUT", {patterns: clone(patterns)});
      if (result.status !== 200) {
        state.patternError = errorMessage(result, "行为模式保存失败");
        render("patterns");
        return false;
      }
      state.patterns = clone(result.data.patterns);
      state.patternError = "";
      render("patterns");
      return true;
    }

    async function deletePattern(patternId) {
      if (typeof deps.confirm === "function" && !deps.confirm("确认删除这个行为模式？")) return false;
      return savePatterns(state.patterns.filter((item) => item.id !== patternId));
    }

    function scheduleRecordingPoll() {
      if (!state.recording || !state.recording.recording_id) return;
      const expectedRecordingId = state.recording.recording_id;
      const expectedGeneration = activeRecordingGeneration;
      state.recordingTimer = deps.setTimeout(function () { return pollRecording(expectedRecordingId, expectedGeneration); }, 500);
    }

    async function startRecording(type) {
      if (recordingStartOperation) return recordingStartOperation.promise;
      if (state.recording && state.recording.recording_id && !state.recording.sample) {
        state.recording.error = "请先结束或取消当前录制";
        render("patterns");
        return false;
      }
      const windows = deps.selectedBrowserWindows();
      if (!Array.isArray(windows) || windows.length !== 1) {
        state.recording = {type, error: "录制时只能选择 1 个已打开窗口", sample: null};
        render("patterns");
        return false;
      }
      const generation = ++recordingGeneration;
      activeRecordingGeneration = generation;
      state.recording = {type, status: "starting", error: "", sample: null};
      render("patterns");
      const operation = {generation, cancelRequested: false, promise: null};
      recordingStartOperation = operation;
      operation.promise = (async function () {
        const result = await request("/api/browser/pattern-recordings/start", "POST", {windows: clone(windows), type});
        const current = recordingStartOperation === operation
          && activeRecordingGeneration === generation;
        if (!current) {
          return false;
        }
        if (result.status !== 200) {
          activeRecordingGeneration = 0;
          state.recording = {type, error: errorMessage(result, "录制启动失败"), sample: null};
          render("patterns");
          return false;
        }
        state.recording = Object.assign({type, error: "", sample: null}, clone(result.data));
        if (!operation.cancelRequested) scheduleRecordingPoll();
        render("patterns");
        return true;
      })().finally(function () {
        if (recordingStartOperation === operation) recordingStartOperation = null;
      });
      return operation.promise;
    }

    function isCurrentRecording(recordingId, generation) {
      return Boolean(
        state.recording
        && state.recording.recording_id === recordingId
        && activeRecordingGeneration === generation
      );
    }

    async function pollRecording(expectedRecordingId, expectedGeneration) {
      if (!state.recording || !state.recording.recording_id || state.recording.sample) return false;
      const id = state.recording.recording_id;
      const generation = expectedGeneration || activeRecordingGeneration;
      if (expectedRecordingId && expectedRecordingId !== id) return false;
      if (!isCurrentRecording(id, generation)) return false;
      const result = await request(`/api/browser/pattern-recordings/${id}`, "GET");
      if (!isCurrentRecording(id, generation) || state.recording.sample) return false;
      if (result.status !== 200) {
        state.recording.error = errorMessage(result, "录制状态读取失败");
        render("patterns");
        return false;
      }
      Object.assign(state.recording, clone(result.data));
      if (result.data.status === "ready" || result.data.status === "recording") {
        scheduleRecordingPoll();
        render("patterns");
        return true;
      }
      if (result.data.status === "stopped") {
        return finalizeRecording(false, id, generation);
      }
      render("patterns");
      return true;
    }

    function finalizeRecording(discard, id, generation) {
      if (recordingFinalizeOperation
        && recordingFinalizeOperation.id === id
        && recordingFinalizeOperation.generation === generation) {
        if (discard) recordingFinalizeOperation.discard = true;
        return recordingFinalizeOperation.promise;
      }
      clearRecordingTimer();
      const operation = {id, generation, discard: Boolean(discard), promise: null};
      recordingFinalizeOperation = operation;
      operation.promise = (async function () {
        const finished = await request(`/api/browser/pattern-recordings/${id}/stop`, "POST", {});
        if (!isCurrentRecording(id, generation)) return finished.status === 200;
        if (finished.status !== 200) {
          state.recording.error = errorMessage(finished, operation.discard ? "录制取消失败" : "录制结束失败");
          render("patterns");
          return false;
        }
        if (operation.discard) {
          activeRecordingGeneration = 0;
          state.recording = null;
        } else {
          state.recording.sample = {type: finished.data.type || state.recording.type, data: clone(finished.data.sample)};
          state.recording.sampleCount = finished.data.sample && finished.data.sample.sample_count;
          state.recording.durationMs = finished.data.sample && finished.data.sample.total_duration_ms;
        }
        render("patterns");
        return true;
      })().finally(function () {
        if (recordingFinalizeOperation === operation) recordingFinalizeOperation = null;
      });
      return operation.promise;
    }

    function stopRecording() {
      if (!state.recording || !state.recording.recording_id) return Promise.resolve(false);
      return finalizeRecording(false, state.recording.recording_id, activeRecordingGeneration);
    }

    async function saveRecording(name) {
      const cleanName = String(name || "").trim();
      if (!state.recording || !state.recording.sample) return false;
      if (!cleanName) {
        state.recording.error = "请输入行为模式名称";
        render("patterns");
        return false;
      }
      const sample = state.recording.sample;
      const pattern = {id: deps.nowId("pattern"), name: cleanName, type: sample.type, data: clone(sample.data)};
      const saved = await savePatterns(state.patterns.concat([pattern]));
      if (saved) {
        clearRecordingTimer();
        state.recording = null;
        render("patterns");
      }
      return saved;
    }

    function clearRecordingTimer() {
      if (state.recordingTimer !== null && typeof deps.clearTimeout === "function") deps.clearTimeout(state.recordingTimer);
      state.recordingTimer = null;
    }

    async function cancelRecording() {
      if (recordingStartOperation && state.recording && !state.recording.recording_id) {
        const operation = recordingStartOperation;
        operation.cancelRequested = true;
        const started = await operation.promise;
        if (!started || !state.recording || !state.recording.recording_id) return false;
        const id = state.recording.recording_id;
        if (!isCurrentRecording(id, operation.generation)) return false;
        return finalizeRecording(true, id, operation.generation);
      }
      const active = state.recording && state.recording.recording_id && !state.recording.sample;
      const id = active ? state.recording.recording_id : null;
      if (id) {
        return finalizeRecording(true, id, activeRecordingGeneration);
      }
      clearRecordingTimer();
      activeRecordingGeneration = 0;
      state.recording = null;
      render("patterns");
      return true;
    }

    function returnToList() {
      if (state.dirty && typeof deps.confirm === "function" && !deps.confirm("策略尚未保存，确认返回？")) return false;
      clearDirty();
      state.draft = null;
      state.view = "list";
      render("all");
      return true;
    }

    const api = {
      selectorProbe: deps.selectorProbe || null,
      state, init, syncElementOptions, syncExecutionOptions, openElementDialog, saveElements, deleteElement,
      addElementLocator, updateElementLocator, moveElementLocator, removeElementLocator, setElementScope,
      elementTestResultCode, testElementDraft, applyTikTokCommentTemplate,
      createStrategy, openStrategy, renameStrategy, addBlock, updateBlock, moveBlock, deleteBlock,
      parameterFields, parseScrollParameters, sanitizeActionParams, serializeStrategyForm, markDirty, saveStrategy, deleteStrategy, savePatterns, deletePattern,
      startRecording, pollRecording, stopRecording, saveRecording, cancelRecording, returnToList,
    };
    return api;
  }

  function browserDependencies(win) {
    const document = win.document;
    let serial = 0;
    const deps = {
      requestJson: async function (url, method, body) {
        const requestMethod = String(method || "GET").toUpperCase();
        const requestUrl = new URL(url, win.location.href);
        const sameOrigin = requestUrl.origin === win.location.origin;
        const headers = {"Content-Type": "application/json"};
        if (
          sameOrigin
          && ["POST", "PUT", "PATCH", "DELETE"].includes(requestMethod)
        ) {
          const tokenNode = document.querySelector(
            'meta[name="csrf-token"]',
          );
          if (tokenNode && tokenNode.content) {
            headers["X-CSRF-Token"] = tokenNode.content;
          }
        }
        const response = await win.fetch(url, {
          method: requestMethod,
          headers,
          body: body === undefined ? undefined : JSON.stringify(body),
        });
        if (sameOrigin && response.status === 401) {
          win.location.assign("/login");
        }
        let data = {};
        try { data = await response.json(); } catch (_error) { data = {error: "服务器返回无效响应"}; }
        return {status: response.status, data};
      },
      selectedBrowserWindows: function () {
        if (typeof win.selectedBrowserWindows === "function") return win.selectedBrowserWindows();
        return Array.from(document.querySelectorAll(".adspower-select:checked")).map((element) => ({
          profile_id: element.dataset.profileId,
          profile_no: element.dataset.profileNo,
          name: element.dataset.profileName,
        }));
      },
      setTimeout: win.setTimeout.bind(win),
      clearTimeout: win.clearTimeout.bind(win),
      addBeforeUnload: (handler) => win.addEventListener("beforeunload", handler),
      removeBeforeUnload: (handler) => win.removeEventListener("beforeunload", handler),
      confirm: (message) => win.confirm(message),
      nowId: (prefix) => `${prefix}_${Date.now().toString(36)}_${(++serial).toString(36)}`,
      targetElementSelectors: () => document.querySelectorAll("[data-browser-element-select]"),
      selectorProbe: (
        win.SelectorProbeUI
        && typeof win.SelectorProbeUI.getController === "function"
      ) ? win.SelectorProbeUI.getController() : null,
    };
    deps.render = createDomRenderer(document, deps);
    return deps;
  }

  function setText(document, selector, value) {
    const node = document.querySelector(selector);
    if (node) node.textContent = value || "";
  }

  function createDomRenderer(document, deps) {
    let wired = false;
    let activeActionId = "";
    let api = null;

    function option(document, value, label, selected) {
      const node = document.createElement("option");
      node.value = value;
      node.textContent = label;
      node.selected = value === selected;
      return node;
    }

    function renderElements(state) {
      const list = document.querySelector("#browser-action-elements-list");
      if (!list) return;
      list.replaceChildren();
      const entries = Object.entries(state.elements);
      if (!entries.length) {
        const empty = document.createElement("div"); empty.className = "muted"; empty.textContent = "暂无元素，请新增网页元素。"; list.append(empty);
      }
      entries.forEach(([alias, selector]) => {
        const row = document.createElement("div"); row.className = "browser-element-row";
        const name = document.createElement("strong"); name.textContent = alias;
        const edit = document.createElement("button"); edit.type = "button"; edit.className = "secondary"; edit.textContent = "编辑";
        edit.addEventListener("click", () => api.openElementDialog({alias, definition: selector, originalAlias: alias}));
        const remove = document.createElement("button"); remove.type = "button"; remove.className = "secondary"; remove.textContent = "删除";
        remove.addEventListener("click", () => api.deleteElement(alias));
        row.append(name, edit, remove); list.append(row);
      });
      setText(document, "#browser-element-status", state.elementDialog.error);
      const dialog = document.querySelector("#browser-element-dialog");
      if (dialog && state.elementDialog.open && !dialog.open) {
        document.querySelector("#browser-element-alias").value = state.elementDialog.draft?.alias || "";
        const legacyXpath = document.querySelector("#browser-element-xpath");
        if (legacyXpath) legacyXpath.value = state.elementDialog.draft?.selector || "";
        dialog.showModal();
      } else if (dialog && !state.elementDialog.open && dialog.open) dialog.close();
      setText(document, "#browser-element-dialog-status", state.elementDialog.error);
      renderElementDialog(state);
    }

    function dialogField(text, value, fieldName, kind) {
      const label = document.createElement("label");
      label.textContent = text;
      const input = document.createElement(kind === "textarea" ? "textarea" : "input");
      input.dataset.locatorField = fieldName;
      if (kind === "textarea") { input.rows = 3; input.value = value || ""; }
      else { input.type = "text"; input.value = value || ""; }
      label.append(input);
      return label;
    }

    function renderElementDialog(state) {
      const dialog = document.querySelector("#browser-element-dialog");
      const draft = state.elementDialog.draft;
      if (!dialog || !draft) return;
      const alias = document.querySelector("#browser-element-alias");
      const scope = document.querySelector("#browser-element-scope");
      if (alias) alias.value = draft.alias || "";
      if (scope) scope.value = draft.definition.scope;
      const locators = document.querySelector("#browser-element-locators");
      if (locators) {
        locators.replaceChildren();
        draft.definition.locators.forEach((candidate, index) => {
          const card = document.createElement("section");
          card.className = "browser-element-locator";
          card.dataset.locatorId = candidate.id;
          const heading = document.createElement("div"); heading.className = "content-toolbar";
          const title = document.createElement("strong"); title.textContent = `候选 ${index + 1}`;
          const controls = document.createElement("div"); controls.className = "compact-actions";
          [["上移", "up"], ["下移", "down"], ["删除", "remove"]].forEach(([label, action]) => {
            const button = document.createElement("button"); button.type = "button"; button.className = "secondary";
            button.textContent = label; button.dataset.locatorAction = action; button.disabled = action === "remove" && draft.definition.locators.length === 1;
            controls.append(button);
          });
          heading.append(title, controls);
          const typeLabel = document.createElement("label"); typeLabel.textContent = "定位类型";
          const type = document.createElement("select"); type.dataset.locatorField = "type";
          [["attribute", "属性"], ["css", "CSS"], ["role", "角色"], ["xpath", "高级 XPath"]].forEach(([value, label]) => type.append(option(document, value, label, candidate.type)));
          typeLabel.append(type); card.append(heading, typeLabel);
          if (candidate.type === "attribute") card.append(dialogField("属性名", candidate.name, "name"), dialogField("属性值", candidate.value, "value"));
          else if (candidate.type === "role") {
            card.append(dialogField("角色", candidate.role, "role"), dialogField("名称", candidate.name, "name"));
            const modeLabel = document.createElement("label"); modeLabel.textContent = "名称匹配";
            const mode = document.createElement("select"); mode.dataset.locatorField = "name_mode";
            mode.append(option(document, "exact", "完全匹配", candidate.name_mode), option(document, "contains", "包含", candidate.name_mode));
            modeLabel.append(mode); card.append(modeLabel);
          } else card.append(dialogField(candidate.type === "xpath" ? "高级 XPath" : "CSS 选择器", candidate.value, "value", candidate.type === "xpath" ? "textarea" : "text"));
          const enabledLabel = document.createElement("label"); enabledLabel.textContent = "启用候选";
          const enabled = document.createElement("input"); enabled.type = "checkbox"; enabled.checked = candidate.enabled !== false; enabled.dataset.locatorField = "enabled";
          enabledLabel.append(enabled); card.append(enabledLabel); locators.append(card);
        });
      }
      const results = document.querySelector("#browser-element-test-results");
      if (results) {
        results.replaceChildren();
        (state.elementDialog.testResults || []).forEach((windowResult) => {
          (windowResult.elements || []).forEach((elementResult) => {
            const row = document.createElement("tr");
            [windowResult.profile_id || "", elementResult.alias || draft.alias, elementResult.status || windowResult.status || "", api.elementTestResultCode(elementResult, windowResult), elementResult.diagnostics ? JSON.stringify(elementResult.diagnostics) : ""].forEach((value) => {
              const cell = document.createElement("td"); cell.textContent = String(value); row.append(cell);
            });
            results.append(row);
          });
        });
      }
    }

    function renderPatterns(state) {
      const list = document.querySelector("#browser-pattern-list");
      if (!list) return;
      list.replaceChildren();
      if (!state.patterns.length) {
        const empty = document.createElement("div"); empty.className = "muted"; empty.textContent = "暂无行为模式。"; list.append(empty);
      }
      state.patterns.forEach((pattern) => {
        const row = document.createElement("div"); row.className = "browser-pattern-card";
        const text = document.createElement("div");
        const title = document.createElement("strong"); title.textContent = pattern.name;
        const info = document.createElement("span"); info.className = "muted"; info.textContent = ` ${pattern.type === "mouse" ? "鼠标轨迹" : "键盘节奏"} · ${pattern.data.sample_count} 个样本`;
        const remove = document.createElement("button"); remove.type = "button"; remove.className = "secondary"; remove.textContent = "删除";
        remove.addEventListener("click", () => api.deletePattern(pattern.id));
        text.append(title, info); row.append(text, remove); list.append(row);
      });
      let message = state.patternError || "";
      const recording = state.recording;
      if (recording) {
        message = recording.error || `录制状态：${recording.status || "准备中"}`;
        if (recording.sample) message = `录制完成：${recording.sampleCount || 0} 个样本，${recording.durationMs || 0} 毫秒`;
        const controls = document.createElement("div"); controls.className = "browser-pattern-recording";
        if (recording.sample) {
          const name = document.createElement("input"); name.placeholder = "行为模式名称"; name.autocomplete = "off";
          const save = document.createElement("button"); save.type = "button"; save.className = "primary"; save.textContent = "保存模式";
          save.addEventListener("click", () => api.saveRecording(name.value));
          controls.append(name, save);
        } else if (recording.recording_id) {
          const stop = document.createElement("button"); stop.type = "button"; stop.className = "secondary"; stop.textContent = "结束录制";
          stop.addEventListener("click", () => api.stopRecording()); controls.append(stop);
        }
        const cancel = document.createElement("button"); cancel.type = "button"; cancel.textContent = "取消"; cancel.addEventListener("click", () => api.cancelRecording());
        controls.append(cancel); list.append(controls);
      }
      setText(document, "#browser-pattern-status", message);
    }

    function renderStrategies(state) {
      const listView = document.querySelector("#browser-strategy-list-view");
      const editor = document.querySelector("#browser-strategy-editor-view");
      if (listView) listView.classList.toggle("is-hidden", state.view !== "list");
      if (editor) { editor.classList.toggle("is-hidden", state.view !== "editor"); editor.setAttribute("aria-hidden", state.view === "editor" ? "false" : "true"); }
      const list = document.querySelector("#browser-strategy-list");
      if (list) {
        list.replaceChildren();
        if (!state.strategies.length) {
          const empty = document.createElement("div"); empty.className = "muted"; empty.textContent = "暂无执行策略。"; list.append(empty);
        }
        state.strategies.forEach((strategy) => {
          const row = document.createElement("article"); row.className = "browser-strategy-card";
          const text = document.createElement("div");
          const title = document.createElement("strong"); title.textContent = strategy.name;
          const info = document.createElement("span"); info.className = "muted";
          info.textContent = ` ${strategy.run_mode === "loop" ? "循环" : "一次"} · ${strategy.actions.length} 个动作 · ${strategy.status === "needs_repair" ? "需要修复" : "可执行"}`;
          const open = document.createElement("button"); open.type = "button"; open.className = "secondary"; open.textContent = "打开配置";
          open.addEventListener("click", () => api.openStrategy(strategy.id));
          text.append(title, info); row.append(text, open); list.append(row);
        });
      }
      setText(document, "#browser-strategy-list-status", state.reloadRequired ? state.saveMessage : "");
      api.syncExecutionOptions(document.querySelector("#browser-execute-strategy"));
      renderEditor(state);
    }

    function renderEditor(state) {
      const draft = state.draft;
      if (!draft) return;
      document.querySelector("#browser-strategy-name").value = draft.name;
      document.querySelector("#browser-strategy-run-mode").value = draft.run_mode;
      document.querySelector("#browser-strategy-loop-minutes-min").value = (draft.loop_duration_minutes || [1, 1])[0];
      document.querySelector("#browser-strategy-loop-minutes-max").value = (draft.loop_duration_minutes || [1, 1])[1];
      document.querySelector("#browser-strategy-batch-size").value = draft.batch_size;
      setText(document, "#browser-strategy-save-state", state.saveMessage || (state.dirty ? "未保存" : "已保存"));
      const actions = document.querySelector("#browser-strategy-actions"); actions.replaceChildren();
      if (!draft.actions.length) {
        const empty = document.createElement("div"); empty.className = "muted"; empty.textContent = "选择上方积木后，动作才会出现在这里。"; actions.append(empty);
      }
      draft.actions.forEach((action, index) => {
        const row = document.createElement("article"); row.className = "browser-block-card";
        const title = document.createElement("strong"); title.textContent = `${index + 1}. ${state.catalog[action.type]?.label || action.type}`;
        const controls = document.createElement("div"); controls.className = "compact-actions";
        [["编辑", () => openParameters(state, action)], ["上移", () => api.moveBlock(action.id, -1)], ["下移", () => api.moveBlock(action.id, 1)], ["删除", () => api.deleteBlock(action.id)]].forEach(([label, handler]) => {
          const button = document.createElement("button"); button.type = "button"; button.className = "secondary"; button.textContent = label; button.addEventListener("click", handler); controls.append(button);
        });
        row.append(title, controls); actions.append(row);
      });
    }

    function field(document, labelText, name, value, type) {
      const label = document.createElement("label"); label.textContent = labelText;
      const input = document.createElement(type === "select" ? "select" : "input"); input.name = name;
      if (type !== "select") { input.type = type || "number"; input.value = value; }
      label.append(input); return {label, input};
    }

    function addRange(fields, label, name, values) {
      const first = field(document, `${label}最小值`, `${name}.0`, values[0]);
      const second = field(document, `${label}最大值`, `${name}.1`, values[1]);
      fields.append(first.label, second.label);
    }

    function addElementSelect(fields, label, name, selected, state) {
      const item = field(document, label, name, "", "select");
      item.input.dataset.browserElementSelect = "";
      item.input.append(option(document, "", "请选择元素", selected));
      Object.keys(state.elements).forEach((alias) => item.input.append(option(document, alias, alias, selected)));
      fields.append(item.label);
    }

    function addPatternSource(fields, label, name, selected, patternType, state) {
      const item = field(document, label, name, "", "select");
      item.input.append(option(document, "builtin:bezier", "内置拟人函数", `${selected.source}:${selected.id}`));
      state.patterns.filter((pattern) => pattern.type === patternType).forEach((pattern) => item.input.append(option(document, `pattern:${pattern.id}`, pattern.name, `${selected.source}:${selected.id}`)));
      fields.append(item.label);
    }

    function openParameters(state, action) {
      activeActionId = action.id;
      const fields = document.querySelector("#browser-block-parameter-fields"); fields.replaceChildren();
      const p = action.params;
      if (action.type === "move") {
        const mode = field(document, "目标模式", "target_mode", "", "select");
        mode.input.append(option(document, "element", "网页元素", p.target_mode), option(document, "viewport", "相对视口", p.target_mode)); fields.append(mode.label);
        addElementSelect(fields, "目标元素", "element", p.element, state);
        const horizontal = field(document, "水平位移比例", "delta_viewport.0", p.delta_viewport[0]);
        const vertical = field(document, "垂直位移比例", "delta_viewport.1", p.delta_viewport[1]);
        fields.append(horizontal.label, vertical.label);
        addPatternSource(fields, "移动轨迹", "trajectory", p.trajectory, "mouse", state); addRange(fields, "持续秒数", "duration_seconds", p.duration_seconds);
        const toggleMove = () => {
          const elementMode = mode.input.value === "element";
          mode.input.form.elements.namedItem("element").closest("label").hidden = !elementMode;
          horizontal.label.hidden = elementMode; vertical.label.hidden = elementMode;
        };
        mode.input.addEventListener("change", toggleMove); toggleMove();
      } else if (action.type === "click") {
        addElementSelect(fields, "目标元素", "element", p.element, state);
        const button = field(document, "鼠标键", "button", "", "select"); ["left", "middle", "right"].forEach((value) => button.input.append(option(document, value, value, p.button))); fields.append(button.label);
        fields.append(field(document, "点击次数", "click_count", p.click_count).label); addRange(fields, "按下秒数", "hold_seconds", p.hold_seconds); addPatternSource(fields, "接近轨迹", "trajectory", p.trajectory, "mouse", state);
      } else if (action.type === "scroll_up" || action.type === "scroll_down") {
        api.parameterFields(action.type).forEach((definition) => {
          const current = definition.name.split(".").reduce((value, key) => value && value[key], p);
          fields.append(field(document, definition.label, definition.name, current).label);
        });
      } else if (action.type === "keyboard_input") {
        addElementSelect(fields, "输入元素", "element", p.element, state);
        const source = field(document, "内容来源", "content.source", "", "select"); source.input.append(option(document, "fixed", "固定文本", p.content.source), option(document, "generated_comment", "文案品牌", p.content.source)); fields.append(source.label);
        const fixedText = field(document, "固定文本", "content.text", p.content.text, "text"); fields.append(fixedText.label);
        const brand = field(document, "文案品牌", "content.brand_id", "", "select"); brand.input.append(option(document, "", "全部品牌随机", p.content.brand_id)); state.brands.forEach((item) => brand.input.append(option(document, item.id, item.name, p.content.brand_id))); fields.append(brand.label);
        const timing = field(document, "输入节奏", "typing", "", "select"); timing.input.append(option(document, "builtin", "内置随机间隔", p.typing.source)); state.patterns.filter((item) => item.type === "keyboard").forEach((item) => timing.input.append(option(document, `pattern:${item.id}`, item.name, p.typing.source === "pattern" ? `pattern:${p.typing.id}` : "builtin"))); fields.append(timing.label);
        const intervalMin = field(document, "每字符间隔毫秒最小值", "typing.interval_ms.0", (p.typing.interval_ms || [50, 250])[0]);
        const intervalMax = field(document, "每字符间隔毫秒最大值", "typing.interval_ms.1", (p.typing.interval_ms || [50, 250])[1]); fields.append(intervalMin.label, intervalMax.label);
        const toggleKeyboard = () => {
          fixedText.label.hidden = source.input.value !== "fixed";
          brand.label.hidden = source.input.value !== "generated_comment";
          const builtin = timing.input.value === "builtin"; intervalMin.label.hidden = !builtin; intervalMax.label.hidden = !builtin;
        };
        source.input.addEventListener("change", toggleKeyboard); timing.input.addEventListener("change", toggleKeyboard); toggleKeyboard();
      } else addRange(fields, "等待秒数", "duration_seconds", p.duration_seconds);
      document.querySelector("#browser-block-parameter-title").textContent = `配置${state.catalog[action.type]?.label || action.type}`;
      document.querySelector("#browser-block-parameter-dialog").showModal();
    }

    function number(form, name) { return Number(form.elements.namedItem(name).value); }
    function range(form, name) { return [number(form, `${name}.0`), number(form, `${name}.1`)]; }

    function parseParameters(form, action) {
      if (action.type === "move") {
        const trajectory = form.elements.namedItem("trajectory").value.split(":");
        return {target_mode: form.elements.namedItem("target_mode").value, element: form.elements.namedItem("element").value, delta_viewport: range(form, "delta_viewport"), trajectory: {source: trajectory[0], id: trajectory[1]}, duration_seconds: range(form, "duration_seconds")};
      }
      if (action.type === "click") {
        const trajectory = form.elements.namedItem("trajectory").value.split(":");
        return {element: form.elements.namedItem("element").value, button: form.elements.namedItem("button").value, click_count: number(form, "click_count"), hold_seconds: range(form, "hold_seconds"), trajectory: {source: trajectory[0], id: trajectory[1]}};
      }
      if (action.type === "scroll_up" || action.type === "scroll_down") return api.parseScrollParameters(form, action);
      if (action.type === "keyboard_input") {
        const timing = form.elements.namedItem("typing").value;
        return {element: form.elements.namedItem("element").value, content: {source: form.elements.namedItem("content.source").value, text: form.elements.namedItem("content.text").value, brand_id: form.elements.namedItem("content.brand_id").value}, typing: timing === "builtin" ? {source: "builtin", interval_ms: range(form, "typing.interval_ms")} : {source: "pattern", id: timing.split(":")[1]}};
      }
      return {duration_seconds: range(form, "duration_seconds")};
    }

    function wire(state, controller) {
      if (wired) return; wired = true; api = controller;
      document.querySelector("#browser-element-add")?.addEventListener("click", () => api.openElementDialog());
      document.querySelector("#browser-element-form")?.addEventListener("submit", async (event) => {
        event.preventDefault(); const draft = state.elementDialog.draft || {};
        const alias = document.querySelector("#browser-element-alias").value.trim();
        const next = clone(state.elements); if (draft.originalAlias && draft.originalAlias !== alias) delete next[draft.originalAlias]; next[alias] = clone(draft.definition);
        await api.saveElements(next, draft.originalAlias && draft.originalAlias !== alias ? draft.originalAlias : undefined);
      });
      document.querySelector("#browser-element-scope")?.addEventListener("change", (event) => api.setElementScope(event.target.value));
      document.querySelector("#browser-element-add-locator")?.addEventListener("click", () => api.addElementLocator({type: "css", value: ""}));
      document.querySelector("#browser-element-template")?.addEventListener("click", () => api.applyTikTokCommentTemplate());
      document.querySelector("#browser-element-test")?.addEventListener("click", () => api.testElementDraft());
      document.querySelector("#browser-element-locators")?.addEventListener("click", (event) => {
        const button = event.target.closest("[data-locator-action]");
        if (!button) return;
        const id = button.closest("[data-locator-id]")?.dataset.locatorId;
        if (!id) return;
        if (button.dataset.locatorAction === "up") api.moveElementLocator(id, -1);
        else if (button.dataset.locatorAction === "down") api.moveElementLocator(id, 1);
        else api.removeElementLocator(id);
      });
      document.querySelector("#browser-element-locators")?.addEventListener("change", (event) => {
        const input = event.target;
        const fieldName = input.dataset.locatorField;
        const id = input.closest("[data-locator-id]")?.dataset.locatorId;
        if (!fieldName || !id) return;
        api.updateElementLocator(id, {[fieldName]: fieldName === "enabled" ? input.checked : input.value});
      });
      ["#browser-element-dialog-close", "#browser-element-dialog-cancel"].forEach((selector) => document.querySelector(selector)?.addEventListener("click", () => { state.elementDialog.open = false; state.elementDialog.error = ""; document.querySelector("#browser-element-dialog").close(); }));
      document.querySelector("#browser-pattern-record-mouse")?.addEventListener("click", () => api.startRecording("mouse"));
      document.querySelector("#browser-pattern-record-keyboard")?.addEventListener("click", () => api.startRecording("keyboard"));
      document.querySelector("#browser-strategy-create")?.addEventListener("click", () => api.createStrategy("新策略"));
      document.querySelector("#browser-strategy-back")?.addEventListener("click", () => api.returnToList());
      document.querySelector("#browser-strategy-rename")?.addEventListener("click", () => api.renameStrategy(document.querySelector("#browser-strategy-name").value));
      document.querySelector("#browser-strategy-save")?.addEventListener("click", () => api.saveStrategy());
      document.querySelector("#browser-strategy-delete")?.addEventListener("click", () => state.draft && api.deleteStrategy(state.draft.id));
      document.querySelector("#browser-strategy-run-mode")?.addEventListener("change", (event) => { state.draft.run_mode = event.target.value; if (event.target.value === "loop" && !state.draft.loop_duration_minutes) state.draft.loop_duration_minutes = [1, 1]; api.markDirty(); });
      [["#browser-strategy-loop-minutes-min", 0], ["#browser-strategy-loop-minutes-max", 1]].forEach(([selector, index]) => document.querySelector(selector)?.addEventListener("change", (event) => { state.draft.loop_duration_minutes ||= [1, 1]; state.draft.loop_duration_minutes[index] = Number(event.target.value); api.markDirty(); }));
      document.querySelector("#browser-strategy-batch-size")?.addEventListener("change", (event) => { state.draft.batch_size = Number(event.target.value); api.markDirty(); });
      document.querySelectorAll("#browser-block-palette [data-block-type]").forEach((button) => button.addEventListener("click", () => api.addBlock(button.dataset.blockType)));
      document.querySelector("#browser-block-parameter-form")?.addEventListener("submit", (event) => { event.preventDefault(); const action = state.draft && state.draft.actions.find((item) => item.id === activeActionId); if (!action) return; api.updateBlock(action.id, parseParameters(event.currentTarget, action)); document.querySelector("#browser-block-parameter-dialog").close(); });
      ["#browser-block-parameter-close", "#browser-block-parameter-cancel"].forEach((selector) => document.querySelector(selector)?.addEventListener("click", () => document.querySelector("#browser-block-parameter-dialog").close()));
    }

    return function (view, state, controller) {
      api = controller; wire(state, controller);
      if (view === "all" || view === "elements") renderElements(state);
      if (view === "all" || view === "patterns") renderPatterns(state);
      if (view === "all" || view === "editor" || view === "save") renderStrategies(state);
    };
  }

  return {createBrowserStrategyUI, browserDependencies};
});
