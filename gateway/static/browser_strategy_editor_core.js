(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.BrowserStrategyEditorCore = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const API_PREFIX = "/api/browser-v2";
  const ACTIONS = {
    move: {label: "移动"},
    scroll: {label: "切换视频"},
    click: {label: "点击元素"},
    input: {label: "键盘输入"},
    wait: {label: "等待"},
  };
  const STRATEGY_DEFINITION_FIELDS = [
    "target_url", "ready_element_id", "readiness_timeout_seconds",
    "run_mode", "loop_duration_minutes", "actions",
  ];
  let actionSequence = 0;

  class StrategyRequestError extends Error {
    constructor(code, message, status, cause) {
      super(message);
      this.name = "StrategyRequestError";
      this.code = code;
      this.status = status;
      if (cause !== undefined) this.cause = cause;
    }
  }

  function clone(value) {
    return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
  }

  function actionTemplate(type, id) {
    if (!ACTIONS[type]) throw new Error("不支持的动作类型");
    const actionId = id || ("action_" + Date.now());
    if (type === "move") return {id: actionId, type, element_id: "", duration_seconds: [0.2, 0.5]};
    if (type === "scroll") return {
      id: actionId, type, direction: "down", distance_pixels: [120, 120],
      count: [1, 2], interval_seconds: [0.2, 0.5],
    };
    if (type === "click") return {
      id: actionId, type, element_id: "", button: "left", click_count: 1,
      hold_seconds: [0.05, 0.1], after_seconds: [0.3, 0.6],
    };
    if (type === "input") return {
      id: actionId, type, element_id: "", content_source: "fixed", fixed_text: "",
      content_library_id: "", interval_ms: [40, 120],
    };
    return {id: actionId, type, duration_seconds: [1, 1]};
  }

  function createStrategyDraft(id, overrides) {
    const option = id && typeof id === "object" ? id : (overrides || {});
    const strategyId = typeof id === "string" ? id : option.id;
    const draft = {
      id: strategyId || ("strategy_" + Date.now()),
      localNew: true,
      name: "新策略",
      enabled: true,
      definition: {
        target_url: "https://www.tiktok.com/",
        ready_element_id: "",
        readiness_timeout_seconds: 15,
        run_mode: "once",
        loop_duration_minutes: null,
        actions: [],
      },
    };
    if (option && typeof option === "object") {
      Object.assign(draft, clone(option));
      draft.definition = Object.assign({}, draft.definition, clone(option.definition || {}));
      if (!Array.isArray(draft.definition.actions)) draft.definition.actions = [];
      draft.localNew = true;
    }
    return draft;
  }

  function normalizeStrategyDraft(record) {
    const draft = clone(record || {});
    if (!draft.definition || typeof draft.definition !== "object" || Array.isArray(draft.definition)) {
      const definition = {};
      STRATEGY_DEFINITION_FIELDS.forEach(function (field) {
        if (Object.prototype.hasOwnProperty.call(draft, field)) definition[field] = draft[field];
        delete draft[field];
      });
      draft.definition = definition;
    }
    if (!Array.isArray(draft.definition.actions)) draft.definition.actions = [];
    return draft;
  }

  function duplicateStrategyDraft(record, newId) {
    const copy = normalizeStrategyDraft(record);
    copy.id = newId || ("strategy_" + Date.now());
    copy.name = (copy.name || "策略") + " 副本";
    copy.localNew = true;
    delete copy.revision;
    return copy;
  }

  function actionsOf(target) {
    if (Array.isArray(target)) return target;
    if (!target || !target.definition) return null;
    if (!Array.isArray(target.definition.actions)) target.definition.actions = [];
    return target.definition.actions;
  }

  function nextActionId(actions) {
    const used = new Set((actions || []).map(function (item) { return item && item.id; }).filter(Boolean));
    let candidate;
    do {
      candidate = "action_" + (++actionSequence);
    } while (used.has(candidate));
    return candidate;
  }

  function addAction(target, type, id) {
    const actions = actionsOf(target);
    if (!actions) return false;
    actions.push(actionTemplate(type, id || nextActionId(actions)));
    return true;
  }

  function moveAction(target, index, delta) {
    const actions = actionsOf(target);
    const destination = Number(index) + Number(delta);
    if (!actions || !Number.isInteger(Number(index)) || !Number.isInteger(Number(delta)) || destination < 0 || destination >= actions.length) return false;
    const item = actions.splice(Number(index), 1)[0];
    actions.splice(destination, 0, item);
    return true;
  }

  function removeAction(target, index) {
    const actions = actionsOf(target);
    if (!actions || !Number.isInteger(Number(index)) || Number(index) < 0 || Number(index) >= actions.length) return false;
    actions.splice(Number(index), 1);
    return true;
  }

  function eligibleElements(first, second, third) {
    const elements = Array.isArray(first) ? first : (Array.isArray(third) ? third : []);
    const purpose = Array.isArray(first) ? second : first;
    const kinds = Array.isArray(first) ? third : second;
    return elements.filter(function (item) {
      if (!item || item.status !== "active" || item.purpose !== purpose) return false;
      if (!kinds) return true;
      return (Array.isArray(kinds) ? kinds : [kinds]).includes(item.kind);
    });
  }

  function parseRange(value, label, integer) {
    const values = (Array.isArray(value) ? value : String(value).split("-")).map(Number);
    if (values.length !== 2 || !values.every(Number.isFinite) || values[0] > values[1] || (integer && !values.every(Number.isInteger))) {
      throw new Error(label + "格式应为最小值-最大值");
    }
    return values;
  }

  function serializeAction(action) {
    if (!action || !ACTIONS[action.type]) throw new Error("不支持的动作类型");
    if (action.type === "move") return {
      id: action.id, type: "move", element_id: action.element_id,
      duration_seconds: parseRange(action.duration_seconds, "移动秒数范围"),
    };
    if (action.type === "scroll") return {
      id: action.id, type: "scroll", direction: action.direction,
      distance_pixels: [120, 120], count: parseRange(action.count, "视频切换次数范围", true),
      interval_seconds: parseRange(action.interval_seconds, "视频切换间隔范围"),
    };
    if (action.type === "click") return {
      id: action.id, type: "click", element_id: action.element_id, button: action.button,
      click_count: Number(action.click_count), hold_seconds: parseRange(action.hold_seconds, "按住秒数范围"),
      after_seconds: parseRange(action.after_seconds, "点击后等待范围"),
    };
    if (action.type === "input") {
      const source = action.content_source === "library" ? "library" : "fixed";
      return {
        id: action.id, type: "input", element_id: action.element_id, content_source: source,
        fixed_text: source === "fixed" ? (action.fixed_text || "") : "",
        content_library_id: source === "library" ? (action.content_library_id || "") : "",
        interval_ms: parseRange(action.interval_ms, "输入间隔范围", true),
      };
    }
    return {id: action.id, type: "wait", duration_seconds: parseRange(action.duration_seconds, "等待秒数范围")};
  }

  function serializeDefinition(definition) {
    const source = definition || {};
    const targetUrl = String(source.target_url || "").trim();
    const readyElementId = source.ready_element_id;
    const timeout = Number(source.readiness_timeout_seconds);
    if (!targetUrl.startsWith("https://") || !readyElementId || !Number.isFinite(timeout) || timeout < 0.1 || timeout > 600) {
      throw new Error("请填写 HTTPS 目标网址、就绪元素和有效超时秒数");
    }
    const runMode = source.run_mode;
    if (runMode !== "once" && runMode !== "duration") throw new Error("运行方式无效");
    const actions = Array.isArray(source.actions) ? source.actions : [];
    return {
      target_url: targetUrl,
      ready_element_id: readyElementId,
      readiness_timeout_seconds: timeout,
      run_mode: runMode,
      loop_duration_minutes: runMode === "duration" ? parseRange(source.loop_duration_minutes, "分钟范围") : null,
      actions: actions.map(serializeAction),
    };
  }

  function buildCreatePayload(draft) {
    const source = normalizeStrategyDraft(draft);
    return {
      id: source.id,
      name: source.name,
      definition: serializeDefinition(source.definition),
      enabled: source.enabled !== false,
    };
  }

  function buildUpdatePayload(draft) {
    const source = normalizeStrategyDraft(draft);
    return {
      expected_revision: source.revision,
      name: source.name,
      definition: serializeDefinition(source.definition),
      enabled: source.enabled !== false,
    };
  }

  function requestCode(status) {
    if (status === 409) return "revision_conflict";
    if (status === 404) return "not_found";
    if (status === 400 || status === 422) return "validation_failed";
    if (status === 0) return "network_failed";
    return "request_failed";
  }

  function responseMessage(result, fallback) {
    const data = result && result.data;
    const error = data && data.error;
    if (error && typeof error === "object" && typeof error.message === "string") return error.message;
    if (typeof error === "string") return error;
    if (data && typeof data.message === "string") return data.message;
    return fallback;
  }

  function createStrategyRepository(requestJson) {
    if (typeof requestJson !== "function") throw new TypeError("requestJson must be a function");

    async function call(url, method, body) {
      let result;
      try {
        result = await requestJson(url, method || "GET", body);
      } catch (error) {
        if (error instanceof StrategyRequestError) throw error;
        throw new StrategyRequestError("network_failed", error && error.message ? error.message : "请求失败", 0, error);
      }
      if (!result || ![200, 201, 204].includes(result.status)) {
        throw new StrategyRequestError(requestCode(result && result.status), responseMessage(result, "策略请求失败"), result && result.status);
      }
      const data = result.data;
      return data && typeof data === "object" && Object.prototype.hasOwnProperty.call(data, "data") ? data.data : data;
    }

    return {
      loadDependencies: function () {
        return Promise.all([
          call(API_PREFIX + "/elements", "GET"),
          call(API_PREFIX + "/content-libraries", "GET"),
        ]);
      },
      load: function (strategyId) {
        return call(API_PREFIX + "/strategies/" + encodeURIComponent(strategyId), "GET");
      },
      create: function (draft) {
        return call(API_PREFIX + "/strategies", "POST", buildCreatePayload(draft));
      },
      update: function (draft) {
        return call(API_PREFIX + "/strategies/" + encodeURIComponent(draft.id), "PUT", buildUpdatePayload(draft));
      },
      remove: function (draft) {
        return call(API_PREFIX + "/strategies/" + encodeURIComponent(draft.id), "DELETE", {expected_revision: draft.revision});
      },
    };
  }

  return {
    ACTIONS,
    StrategyRequestError,
    actionTemplate,
    normalizeStrategyDraft,
    createStrategyDraft,
    duplicateStrategyDraft,
    addAction,
    moveAction,
    removeAction,
    eligibleElements,
    serializeAction,
    serializeDefinition,
    buildCreatePayload,
    buildUpdatePayload,
    createStrategyRepository,
    parseRange,
  };
});
