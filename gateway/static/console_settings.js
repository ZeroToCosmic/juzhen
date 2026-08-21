"use strict";

(function (root, factory) {
  const exported = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = exported;
  } else if (root) {
    root.ConsoleSettings = exported;
  }
}(typeof self !== "undefined" ? self : typeof window !== "undefined" ? window : this, function () {
  const NUMBER_FIELDS = new Set([
    "timeouts.ip_check_seconds",
    "timeouts.buffer_publish_seconds",
    "publish_queue.interval_seconds",
    "publish_sampling.interval_seconds",
    "publish_sampling.min_age_hours",
  ]);
  const BOOLEAN_FIELDS = new Set([
    "publish_sampling.enabled",
    "models.items[].enabled",
  ]);
  const SECRET_KEYS = new Set([
    "password",
    "raw",
    "account_token",
    "access_key_id",
    "secret_access_key",
    "api_key",
    "cookie",
    "_secrets_configured",
  ]);
  const EDITABLE_TOP_LEVEL = new Set([
    "proxy",
    "proxy_pool",
    "services",
    "timeouts",
    "publish_queue",
    "publish_sampling",
    "browser",
    "adspower",
    "models",
    "r2",
  ]);

  function clone(value) {
    if (value === undefined) return undefined;
    return JSON.parse(JSON.stringify(value));
  }

  function pathParts(path) {
    return String(path || "")
      .replace(/\[([0-9]+)\]/g, ".$1")
      .split(".")
      .filter(Boolean);
  }

  function getPath(value, path) {
    return pathParts(path).reduce((current, part) => (
      current == null ? undefined : current[part]
    ), value);
  }

  function setPath(target, path, value) {
    const parts = pathParts(path);
    if (!parts.length) return;
    let cursor = target;
    parts.forEach((part, index) => {
      if (index === parts.length - 1) {
        cursor[part] = value;
        return;
      }
      const next = parts[index + 1];
      if (cursor[part] == null || typeof cursor[part] !== "object") {
        cursor[part] = /^\d+$/.test(next) ? [] : {};
      }
      cursor = cursor[part];
    });
  }

  function fieldType(path) {
    const normalized = path.replace(/\.items\.[0-9]+\./g, ".items[].");
    if (NUMBER_FIELDS.has(normalized)) return "number";
    if (BOOLEAN_FIELDS.has(normalized)) return "boolean";
    return "string";
  }

  function fieldValue(element, path) {
    if (fieldType(path) === "boolean") {
      if (element && element.type === "checkbox") return Boolean(element.checked);
      return element.value === true || element.value === "true" || element.value === "1";
    }
    if (fieldType(path) === "number") {
      if (element.value === "") return "";
      const number = Number(element.value);
      return Number.isFinite(number) ? number : element.value;
    }
    return element.value == null ? "" : String(element.value);
  }

  function deleteSecrets(value) {
    if (Array.isArray(value)) return value.map(deleteSecrets);
    if (!value || typeof value !== "object") return value;
    const result = {};
    Object.entries(value).forEach(([key, item]) => {
      if (!SECRET_KEYS.has(key)) result[key] = deleteSecrets(item);
    });
    return result;
  }

  function serializeDirtySections(form, loadedSettings, dirtySections) {
    const payload = {};
    const dirty = dirtySections instanceof Set ? dirtySections : new Set(dirtySections || []);
    dirty.forEach((section) => {
      if (!EDITABLE_TOP_LEVEL.has(section)) return;
      payload[section] = clone(loadedSettings && loadedSettings[section]) || {};
    });

    const elements = form && form.elements ? Array.from(form.elements) : [];
    elements.forEach((element) => {
      if (!element || !element.name) return;
      const parts = pathParts(element.name);
      const section = parts[0];
      if (!EDITABLE_TOP_LEVEL.has(section) || !dirty.has(section)) return;
      setPath(payload[section], parts.slice(1).join("."), fieldValue(element, element.name));
    });

    delete payload._secrets_configured;
    delete payload.selector_probe;
    return payload;
  }

  function mergeModelDrafts(loadedItems, drafts) {
    const existing = Array.isArray(loadedItems) ? loadedItems : [];
    const draftList = Array.isArray(drafts) ? drafts : [];
    const existingById = new Map(existing
      .filter((item) => item && item.id)
      .map((item) => [String(item.id), item]));

    return draftList.filter((item) => item && typeof item === "object").map((draft) => {
      const prior = existingById.get(String(draft.id));
      const merged = {...clone(prior || {}), ...clone(draft)};
      if ("enabled" in merged) {
        merged.enabled = merged.enabled === true || merged.enabled === "true" || merged.enabled === "1";
      }
      return merged;
    });
  }

  function secretConfigured(settings, path) {
    return Boolean(getPath(settings && settings._secrets_configured, path));
  }

  function statusOk(result) {
    return result && Number(result.status) >= 200 && Number(result.status) < 300;
  }

  function responseData(result) {
    return result && result.data !== undefined ? result.data : {};
  }

  function createConsoleSettingsController(options = {}) {
    if (typeof options.requestJson !== "function") {
      throw new TypeError("requestJson is required");
    }
    const state = {
      loaded: false,
      healthKnown: false,
      canSave: false,
      saving: false,
      settings: {},
      health: {},
      presets: {},
      cookie: {configured: false, valid: null},
      proxyPool: {page: 1, pageSize: 50, pageCount: 1, total: 0, assigned: 0, remaining: 0, items: []},
      dirtySections: new Set(),
      category: "network",
      message: "",
      error: "",
      destroyed: false,
    };
    let form = options.form || null;
    let unloadRegistered = false;

    const call = (url, method = "GET", body) => options.requestJson(url, method, body);
    const getForm = () => {
      if (typeof options.getForm === "function") return options.getForm();
      if (form) return form;
      if (options.root && typeof options.root.querySelector === "function") {
        return options.root.querySelector("form");
      }
      return null;
    };

    function beforeUnload(event) {
      if (!state.dirtySections.size) return undefined;
      if (event && typeof event.preventDefault === "function") event.preventDefault();
      if (event) event.returnValue = "";
      return "";
    }

    function setSettings(data) {
      state.settings = data && typeof data === "object" ? clone(data) : {};
      state.loaded = true;
    }

    function updateProxyPool(data) {
      const value = data && typeof data === "object" ? data : {};
      const page = Number(value.page || state.proxyPool.page || 1);
      const pageSize = Number(value.page_size || state.proxyPool.pageSize || 50);
      const total = Number(value.total || 0);
      state.proxyPool = {
        page,
        pageSize,
        pageCount: Math.max(1, Math.ceil(total / Math.max(pageSize, 1))),
        total,
        assigned: Number(value.assigned || 0),
        remaining: Number(value.remaining || 0),
        items: Array.isArray(value.items) ? clone(value.items) : [],
      };
    }

    async function refreshProxyPool({page = state.proxyPool.page, pageSize = state.proxyPool.pageSize, search = ""} = {}) {
      const query = `page=${encodeURIComponent(page)}&page_size=${encodeURIComponent(pageSize)}&search=${encodeURIComponent(search)}`;
      const result = await call(`/api/proxy-pool/status?${query}`);
      if (!statusOk(result)) {
        state.error = "代理池状态读取失败";
        return false;
      }
      updateProxyPool(responseData(result));
      state.error = "";
      return true;
    }

    async function reload() {
      state.error = "";
      const results = await Promise.allSettled([
        call("/api/settings"),
        call("/api/settings/status"),
        call("/api/model-presets"),
        call(`/api/proxy-pool/status?page=${state.proxyPool.page}&page_size=${state.proxyPool.pageSize}&search=`),
        call("/api/tiktok-stats/settings/cookie"),
      ]);
      const [settingsResult, healthResult, presetsResult, proxyResult, cookieResult] = results;

      if (settingsResult.status === "fulfilled" && statusOk(settingsResult.value)) {
        setSettings(responseData(settingsResult.value));
      } else {
        state.loaded = false;
        state.error = "配置读取失败";
      }
      if (healthResult.status === "fulfilled" && statusOk(healthResult.value)) {
        state.health = clone(responseData(healthResult.value));
        state.healthKnown = true;
      } else {
        state.health = {};
        state.healthKnown = false;
      }
      state.canSave = state.loaded && state.healthKnown && state.health.ok !== false;
      if (presetsResult.status === "fulfilled" && statusOk(presetsResult.value)) {
        state.presets = clone(responseData(presetsResult.value));
      }
      if (proxyResult.status === "fulfilled" && statusOk(proxyResult.value)) {
        updateProxyPool(responseData(proxyResult.value));
      }
      if (cookieResult.status === "fulfilled" && statusOk(cookieResult.value)) {
        const data = responseData(cookieResult.value);
        state.cookie = clone(data.status || data) || {configured: false, valid: null};
      }
      return state.loaded;
    }

    async function refreshPresets() {
      const result = await call("/api/model-presets");
      if (!statusOk(result)) {
        state.error = "模型预设读取失败";
        return false;
      }
      state.presets = clone(responseData(result));
      state.error = "";
      return true;
    }

    async function init() {
      if (!unloadRegistered && typeof options.addBeforeUnload === "function") {
        options.addBeforeUnload(beforeUnload);
        unloadRegistered = true;
      }
      state.destroyed = false;
      return reload();
    }

    function markDirty(section) {
      if (EDITABLE_TOP_LEVEL.has(section)) state.dirtySections.add(section);
      return state.dirtySections;
    }

    async function save() {
      if (state.saving || !state.loaded || !state.dirtySections.size) return false;
      state.saving = true;
      try {
        const healthResult = await call("/api/settings/status");
        if (!statusOk(healthResult) || responseData(healthResult).ok === false) {
          state.healthKnown = false;
          state.canSave = false;
          state.error = "配置健康状态不可用，暂不能保存";
          return false;
        }
        state.health = clone(responseData(healthResult));
        state.healthKnown = true;
        state.canSave = true;
        const payload = serializeDirtySections(getForm(), state.settings, state.dirtySections);
        const result = await call("/api/settings", "PUT", payload);
        if (!statusOk(result)) {
          state.error = "设置保存失败";
          return false;
        }
        setSettings(responseData(result));
        state.dirtySections.clear();
        state.message = "设置已保存";
        state.error = "";
        return true;
      } finally {
        state.saving = false;
      }
    }

    async function restoreLatest() {
      if (typeof options.confirm === "function" && !options.confirm("确认恢复最近的配置备份吗？")) return false;
      const result = await call("/api/settings/restore-latest", "POST", {});
      if (!statusOk(result)) {
        state.error = "恢复配置失败";
        return false;
      }
      state.dirtySections.clear();
      state.message = "配置已恢复，正在重新加载";
      await reload();
      return true;
    }

    async function saveCookie(value) {
      if (!String(value || "").trim()) return false;
      const result = await call("/api/tiktok-stats/settings/cookie", "PUT", {cookie: String(value).trim()});
      if (!statusOk(result)) {
        state.error = "Cookie 保存失败";
        return false;
      }
      const data = responseData(result);
      state.cookie = clone(data.status || data) || state.cookie;
      return true;
    }

    async function validateCookie() {
      const result = await call("/api/tiktok-stats/settings/cookie/validate", "POST", {});
      if (!statusOk(result)) {
        state.error = "Cookie 验证失败";
        return false;
      }
      const data = responseData(result);
      state.cookie = clone(data.status || data) || state.cookie;
      return true;
    }

    function snapshot() {
      return {
        loaded: state.loaded,
        healthKnown: state.healthKnown,
        canSave: state.canSave,
        saving: state.saving,
        settings: deleteSecrets(state.settings),
        health: deleteSecrets(state.health),
        presets: deleteSecrets(state.presets),
        proxyPool: deleteSecrets(state.proxyPool),
        dirtySections: Array.from(state.dirtySections).sort(),
        category: state.category,
        message: state.message,
        error: state.error,
        destroyed: state.destroyed,
      };
    }

    function switchCategory(category) {
      state.category = String(category || "network");
      return state.category;
    }

    function cookieStatus() {
      const status = state.cookie && typeof state.cookie === "object" ? state.cookie : {};
      const validationState = typeof status.state === "string" ? status.state : "";
      return {
        configured: Boolean(status.configured),
        valid: validationState === "valid" ? true : validationState === "invalid" ? false : null,
        state: validationState,
        checkedAt: typeof status.checked_at === "string" ? status.checked_at : null,
      };
    }

    function destroy() {
      if (unloadRegistered && typeof options.removeBeforeUnload === "function") {
        options.removeBeforeUnload(beforeUnload);
        unloadRegistered = false;
      }
      state.destroyed = true;
    }

    return {
      init,
      reload,
      refreshPresets,
      save,
      restoreLatest,
      refreshProxyPool,
      saveCookie,
      validateCookie,
      switchCategory,
      markDirty,
      cookieStatus,
      setForm(value) { form = value; },
      hasDirtyState() { return state.dirtySections.size > 0; },
      destroy,
      snapshot,
      secretConfigured(path) { return secretConfigured(state.settings, path); },
    };
  }

  return {
    serializeDirtySections,
    mergeModelDrafts,
    secretConfigured,
    createConsoleSettingsController,
  };
}));
