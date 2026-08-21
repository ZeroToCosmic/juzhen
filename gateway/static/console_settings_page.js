(function (root, factory) {
  "use strict";
  const exported = factory();
  if (typeof module === "object" && module.exports) module.exports = exported;
  if (root && root.document && root.ConsoleSettings) exported.boot(root);
}(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const SECRET_PATHS = new Set([
    "proxy.password", "proxy_pool.raw", "r2.account_token", "r2.access_key_id",
    "r2.secret_access_key", "adspower.api_key",
  ]);

  function pathValue(value, path) {
    return String(path || "").split(".").filter(Boolean).reduce(
      (current, part) => current == null ? undefined : current[part], value,
    );
  }

  function stringValue(value) {
    return value === undefined || value === null ? "" : String(value);
  }

  function createConsoleSettingsPageAdapter(options = {}) {
    const root = options.root || options.document;
    const document = options.document || root?.ownerDocument || root?.document || root;
    const view = options.window || document?.defaultView || root;
    const scope = root?.querySelector ? root : document;
    const query = (selector) => scope?.querySelector?.(selector) || document?.querySelector?.(selector) || null;
    const queryAll = (selector) => Array.from(scope?.querySelectorAll?.(selector) || document?.querySelectorAll?.(selector) || []);
    const form = query("#console-settings-form");
    const controller = options.controller || (() => {
      if (!root?.ConsoleSettings?.createConsoleSettingsController && !document?.defaultView?.ConsoleSettings) {
        throw new TypeError("ConsoleSettings controller is required");
      }
      const api = root.ConsoleSettings || document.defaultView.ConsoleSettings;
      return api.createConsoleSettingsController({
        root: scope,
        requestJson: requestJson,
        getForm: () => form,
        confirm: (message) => view?.confirm ? view.confirm(message) : true,
        addBeforeUnload: (handler) => view?.addEventListener?.("beforeunload", handler),
        removeBeforeUnload: (handler) => view?.removeEventListener?.("beforeunload", handler),
      });
    })();
    let bound = false;
    let searchTimer = null;

    async function requestJson(url, method = "GET", body) {
      const fetcher = root.fetch || document.defaultView?.fetch;
      if (typeof fetcher !== "function") throw new TypeError("fetch is required");
      const response = await fetcher(url, {
        method,
        headers: method === "GET" ? {Accept: "application/json"} : {"Content-Type": "application/json", Accept: "application/json"},
        body: method === "GET" ? undefined : JSON.stringify(body || {}),
      });
      const data = await response.json().catch(() => ({}));
      return {status: response.status, data};
    }

    function element(tag, className, text) {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined) node.textContent = text;
      return node;
    }

    function label(text, field) {
      const wrapper = element("label", "console-settings-field");
      wrapper.append(element("span", "console-settings-field-label", text), field);
      return wrapper;
    }

    function inputField(type, name, value = "") {
      const field = element("input", "console-control");
      field.type = type;
      field.name = name;
      field.autocomplete = type === "password" ? "new-password" : "off";
      field.value = value;
      return field;
    }

    function selectField(name, values, current) {
      const field = element("select", "console-control");
      field.name = name;
      values.forEach(([value, text]) => {
        const option = element("option", "", text);
        option.value = value;
        field.append(option);
      });
      field.value = current || values[0]?.[0] || "";
      return field;
    }

    function makeModelRow(model, index) {
      const row = element("article", "console-settings-model-row");
      row.setAttribute("data-model-row", String(index));
      const heading = element("div", "console-settings-model-head");
      heading.append(element("strong", "", model.id || `模型 ${index + 1}`));
      heading.append(element("span", "console-badge inactive", model.enabled === false ? "停用" : "启用"));
      row.append(heading);

      const grid = element("div", "console-settings-grid");
      grid.append(label("模型 ID", inputField("text", `models.items.${index}.id`)));
      grid.append(label("供应商", inputField("text", `models.items.${index}.provider`)));
      grid.append(label("接口地址", inputField("text", `models.items.${index}.base_url`)));
      grid.append(label("模型名称", inputField("text", `models.items.${index}.model`)));
      grid.append(label("调用模式", selectField(`models.items.${index}.mode`, [["responses", "Responses"], ["chat", "Chat Completions"]])));
      const enabled = element("input", "console-settings-checkbox");
      enabled.type = "checkbox";
      enabled.name = `models.items.${index}.enabled`;
      const enabledLabel = element("label", "console-settings-checkbox-label");
      enabledLabel.append(enabled, element("span", "", "启用模型"));
      grid.append(enabledLabel);
      const key = inputField("password", `models.items.${index}.api_key`);
      const keyStatus = element("small", "console-settings-secret-status");
      keyStatus.setAttribute("data-model-secret-status", String(index));
      const keyWrapper = element("label", "console-settings-field");
      keyWrapper.append(element("span", "console-settings-field-label", "模型 API Key"), key, keyStatus);
      grid.append(keyWrapper);
      row.append(grid);
      return row;
    }

    function renderModels(snapshot) {
      const list = query("#console-settings-models");
      const empty = query("#console-settings-models-empty");
      if (!list) return;
      const items = snapshot.settings?.models?.items;
      list.replaceChildren();
      if (!Array.isArray(items) || !items.length) {
        if (empty) empty.hidden = false;
        return;
      }
      if (empty) empty.hidden = true;
      items.forEach((model, index) => list.append(makeModelRow(model || {}, index)));
    }

    function renderFields(snapshot) {
      const settings = snapshot.settings || {};
      const defaultModel = query("#console-settings-model-default");
      if (defaultModel) defaultModel.value = stringValue(pathValue(settings, "models.default_model_id"));
      Array.from(form?.elements || []).forEach((field) => {
        if (!field?.name) return;
        if (SECRET_PATHS.has(field.name) || field.name.endsWith(".api_key")) {
          field.value = "";
          return;
        }
        const value = pathValue(settings, field.name);
        if (field.type === "checkbox") field.checked = Boolean(value);
        else field.value = stringValue(value);
      });
      queryAll("[data-secret-status]").forEach((status) => {
        const path = status.getAttribute("data-secret-status");
        const configured = controller.secretConfigured(path);
        status.textContent = configured ? "已配置 · 留空保持" : "未配置";
      });
      queryAll("[data-model-secret-status]").forEach((status) => {
        const index = status.getAttribute("data-model-secret-status");
        status.textContent = controller.secretConfigured(`models.items.${index}.api_key`) ? "已配置 · 留空保持" : "未配置";
      });
    }

    function renderProxyPool(snapshot) {
      const pool = snapshot.proxyPool || {};
      const set = (id, value) => { const node = query(`#${id}`); if (node) node.textContent = stringValue(value); };
      set("console-settings-proxy-total", pool.total ?? 0);
      set("console-settings-proxy-assigned", pool.assigned ?? 0);
      set("console-settings-proxy-remaining", pool.remaining ?? 0);
      set("console-settings-proxy-meta", `第 ${pool.page || 1} / ${pool.pageCount || 1} 页 · 共 ${pool.total || 0} 条`);
      const body = query("#console-settings-proxy-body");
      if (!body) return;
      body.replaceChildren();
      (pool.items || []).forEach((item) => {
        const row = element("tr");
        [item.host, item.port, item.username].forEach((value) => row.append(element("td", "", stringValue(value || "—"))));
        const status = element("span", `console-badge ${item.assigned ? "active" : "inactive"}`, item.assigned ? "已分配" : "未分配");
        const cell = element("td"); cell.append(status); row.append(cell); body.append(row);
      });
      const empty = query("#console-settings-proxy-empty");
      if (empty) empty.hidden = Boolean((pool.items || []).length);
      const prev = query("#console-settings-proxy-prev");
      const next = query("#console-settings-proxy-next");
      if (prev) prev.disabled = Number(pool.page || 1) <= 1;
      if (next) next.disabled = Number(pool.page || 1) >= Number(pool.pageCount || 1);
    }

    function renderCookie(snapshot) {
      const status = snapshot.cookie || (controller.cookieStatus ? controller.cookieStatus() : {configured: false, valid: null, checkedAt: null});
      const configured = query("#console-settings-cookie-status");
      const valid = query("#console-settings-cookie-valid");
      if (configured) configured.textContent = status.configured ? "已配置" : "未配置";
      if (valid) {
        const result = status.valid === true ? "验证通过" : status.valid === false ? "验证失败" : "未验证";
        valid.textContent = status.checkedAt ? `${result} · ${status.checkedAt}` : result;
      }
    }

    function renderPresets(snapshot) {
      const note = query("#console-settings-presets");
      if (!note) return;
      const providers = snapshot.presets && typeof snapshot.presets === "object" ? Object.values(snapshot.presets).filter((item) => item && typeof item === "object" && !Array.isArray(item)) : [];
      note.textContent = providers.length ? `已加载 ${providers.length} 个模型服务预设` : "暂无可用模型预设";
    }

    function renderCategory(snapshot) {
      const category = snapshot.category || "network";
      queryAll("[data-settings-category]").forEach((tab) => {
        const active = tab.getAttribute("data-settings-category") === category;
        if (active) tab.setAttribute("aria-current", "page");
        else tab.removeAttribute("aria-current");
      });
      queryAll("[data-settings-panel]").forEach((panel) => {
        panel.hidden = panel.getAttribute("data-settings-panel") !== category;
      });
    }

    function render({hydrate = false} = {}) {
      const snapshot = controller.snapshot();
      renderCategory(snapshot);
      if (hydrate) {
        renderModels(snapshot);
        renderFields(snapshot);
      }
      renderProxyPool(snapshot);
      renderCookie(snapshot);
      renderPresets(snapshot);
      const dirty = query("#console-settings-dirty");
      if (dirty) dirty.textContent = snapshot.dirtySections?.length ? `已修改：${snapshot.dirtySections.join("、")}` : "未修改";
      const status = query("#console-settings-status");
      if (status) status.textContent = snapshot.message || (!snapshot.healthKnown && snapshot.loaded ? "配置健康状态未知，暂不能保存" : "");
      const error = query("#console-settings-error");
      if (error) error.textContent = snapshot.error || "";
      const health = query("#console-settings-health");
      if (health) {
        health.className = "console-settings-health" + (snapshot.healthKnown && snapshot.health?.ok === false ? " error" : "");
        if (!snapshot.loaded) health.textContent = "配置尚未加载。";
        else if (!snapshot.healthKnown) health.textContent = "配置健康状态未知，暂不能保存。";
        else if (snapshot.health?.ok === false) health.textContent = snapshot.health?.backup_available ? "配置文件异常，可恢复最近备份。" : "配置文件异常，当前没有可用备份。";
        else health.textContent = "配置健康正常。";
      }
      const save = query("#console-settings-save");
      if (save) save.disabled = Boolean(snapshot.saving || !snapshot.canSave || !snapshot.dirtySections?.length);
    }

    async function refreshProxy(page) {
      const search = query("#console-settings-proxy-search")?.value || "";
      const pageSize = Number(query("#console-settings-proxy-page-size")?.value || 50);
      await controller.refreshProxyPool({page, pageSize, search});
      render();
    }

    function sectionFor(field) {
      return String(field?.name || "").split(".")[0];
    }

    function bind() {
      if (bound) return;
      bound = true;
      queryAll("[data-settings-category]").forEach((tab) => tab.addEventListener("click", () => {
        controller.switchCategory(tab.getAttribute("data-settings-category"));
        render();
      }));
      form?.addEventListener("input", (event) => controller.markDirty(sectionFor(event.target)));
      form?.addEventListener("change", (event) => { controller.markDirty(sectionFor(event.target)); render(); });
      form?.addEventListener("submit", async (event) => {
        event.preventDefault();
        const saved = await controller.save();
        render({hydrate: saved});
      });
      query("#console-settings-refresh")?.addEventListener("click", async () => {
        await controller.reload();
        render({hydrate: true});
      });
      query("#console-settings-restore")?.addEventListener("click", async () => {
        const restored = await controller.restoreLatest();
        render({hydrate: restored});
      });
      query("#console-settings-presets-refresh")?.addEventListener("click", async () => {
        await controller.refreshPresets();
        render();
      });
      query("#console-settings-proxy-refresh")?.addEventListener("click", () => refreshProxy(1));
      query("#console-settings-proxy-search")?.addEventListener("input", () => {
        if (searchTimer) clearTimeout(searchTimer);
        searchTimer = setTimeout(() => refreshProxy(1), 220);
      });
      query("#console-settings-proxy-page-size")?.addEventListener("change", () => refreshProxy(1));
      query("#console-settings-proxy-prev")?.addEventListener("click", () => refreshProxy(Math.max(1, Number(controller.snapshot().proxyPool?.page || 1) - 1)));
      query("#console-settings-proxy-next")?.addEventListener("click", () => refreshProxy(Number(controller.snapshot().proxyPool?.page || 1) + 1));
      query("#console-settings-cookie-save")?.addEventListener("click", async () => {
        const field = query("#console-settings-cookie");
        await controller.saveCookie(field?.value || "");
        if (field) field.value = "";
        render();
      });
      query("#console-settings-cookie-validate")?.addEventListener("click", async () => { await controller.validateCookie(); render(); });
    }

    async function init() {
      bind();
      controller.setForm?.(form);
      await controller.init();
      render({hydrate: true});
      return true;
    }

    function destroy() {
      if (searchTimer) clearTimeout(searchTimer);
      controller.destroy?.();
    }

    return {bind, init, render, destroy, controller};
  }

  function boot(root) {
    const page = root.document.querySelector("#console-settings");
    if (!page || !root.ConsoleSettings) return null;
    const adapter = createConsoleSettingsPageAdapter({root: page, document: root.document});
    adapter.init();
    return adapter;
  }

  return {createConsoleSettingsPageAdapter, boot};
}));
