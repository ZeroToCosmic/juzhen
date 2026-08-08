(function (root, factory) {
  "use strict";
  const exported = factory();
  if (typeof module === "object" && module.exports) module.exports = exported;
  if (root) root.SelectorInventoryUI = exported;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const BUSINESS_STEPS = Object.freeze([
    Object.freeze({
      id: "prepare_environment",
      title: "准备测试环境",
      purpose: "启动两个独立测试 Profile，并确认各自绑定唯一页面。",
    }),
    Object.freeze({
      id: "open_and_replay",
      title: "打开页面并重放操作",
      purpose: "等待页面稳定，按保存顺序重放打开二级界面的点击步骤。",
    }),
    Object.freeze({
      id: "validate_elements",
      title: "验证已选元素",
      purpose: "两个 Profile 各运行两轮，检查保存路径唯一、可见且可操作。",
    }),
    Object.freeze({
      id: "protect_or_recover",
      title: "保护或恢复相关策略",
      purpose: "只暂停失效元素影响的策略；满足恢复条件后只清除自动暂停。",
    }),
    Object.freeze({
      id: "alert_and_cleanup",
      title: "发送告警并清理",
      purpose: "保存现场、发送告警并关闭探针页面、Profile 与租约。",
    }),
  ]);
  const LOCATOR_TYPES = new Set(["css", "xpath"]);
  const ATTRIBUTE_NAMES = new Set([
    "data-e2e", "data-testid", "aria-label", "name", "placeholder",
    "contenteditable", "type", "id",
  ]);

  function text(value, maximum) {
    const selected = typeof value === "string" ? value.trim() : "";
    return selected.slice(0, maximum);
  }

  function code(value, maximum = 64) {
    const selected = text(value, maximum);
    return /^[A-Za-z0-9_.:-]*$/.test(selected) ? selected : "";
  }

  function normalizedRegion(raw) {
    if (!raw || typeof raw !== "object") return null;
    return Object.fromEntries(["x", "y", "width", "height"].map((key) => [
      key,
      Math.min(Math.max(Number(raw[key]) || 0, 0), 1),
    ]));
  }

  function sanitizeLocators(raw) {
    if (!Array.isArray(raw)) return [];
    return raw.slice(0, 6).map((item) => {
      const type = code(item?.type, 16).toLowerCase();
      const value = text(item?.value, 2000);
      if (!LOCATOR_TYPES.has(type) || !value) return null;
      const locator = {type, value};
      if (Number.isInteger(item.match_count) && item.match_count >= 0) {
        locator.match_count = item.match_count;
      }
      return locator;
    }).filter(Boolean);
  }

  function sanitizeInventory(raw) {
    if (!Array.isArray(raw)) return [];
    return raw.slice(0, 500).map((item) => {
      const source = item && typeof item === "object" ? item : {};
      const attributes = {};
      if (source.attributes && typeof source.attributes === "object") {
        Object.entries(source.attributes).slice(0, 20).forEach(([key, value]) => {
          if (ATTRIBUTE_NAMES.has(key)) attributes[key] = text(value, 160);
        });
      }
      const locators = sanitizeLocators(
        source.locators || source.recommended_locators,
      );
      return {
        selection_id: text(source.selection_id, 64),
        tag: code(source.tag, 24).toLowerCase(),
        input_type: code(source.input_type, 32).toLowerCase(),
        text: text(source.text || source.text_summary, 160),
        role: text(source.role, 64),
        name: text(source.name, 160),
        attributes,
        region: normalizedRegion(source.region || source.bounding_box),
        page_region: code(source.page_region || source.position, 32),
        frame_key: text(source.frame_key, 128),
        shadow: source.shadow === true,
        locatable: source.locatable === true || locators.some(
          (locator) => locator.match_count === 1,
        ),
        visible: source.visible !== false,
        enabled: source.enabled !== false && source.disabled !== true,
        locators,
      };
    }).filter((item) => item.selection_id);
  }

  function itemType(item) {
    if (item.tag === "button" || item.role === "button") return "button";
    if (["input", "textarea", "select"].includes(item.tag)) return "input";
    if (item.tag === "a" || item.role === "link") return "link";
    return "region";
  }

  function filterInventory(raw, rawFilters) {
    const filters = rawFilters && typeof rawFilters === "object" ? rawFilters : {};
    const type = code(filters.type || "all", 16);
    const region = code(filters.region || "all", 32);
    const search = text(filters.search, 160).toLocaleLowerCase();
    const onlyLocatable = filters.locatable === true || filters.locatable === "yes";
    return sanitizeInventory(raw).filter((item) => {
      if (type && type !== "all" && itemType(item) !== type) return false;
      if (region && region !== "all" && item.page_region !== region) return false;
      if (onlyLocatable && !item.locatable) return false;
      if (!search) return true;
      return [
        item.tag, item.input_type, item.text, item.role, item.name,
        ...Object.entries(item.attributes).flat(),
        ...item.locators.flatMap((locator) => [locator.type, locator.value]),
      ].join(" ").toLocaleLowerCase().includes(search);
    });
  }

  function serializeNamedSelections(raw) {
    if (!Array.isArray(raw)) throw new TypeError("selections must be an array");
    const ids = new Set();
    const names = new Set();
    const result = raw.map((item) => {
      const selectionId = text(item?.selectionId ?? item?.selection_id, 64);
      const displayName = text(item?.displayName ?? item?.display_name, 120);
      const normalizedName = displayName.normalize("NFKC").toLocaleLowerCase();
      if (!selectionId || !displayName || ids.has(selectionId) || names.has(normalizedName)) {
        throw new Error("invalid_named_selection");
      }
      ids.add(selectionId);
      names.add(normalizedName);
      return {selection_id: selectionId, display_name: displayName};
    });
    if (!result.length || result.length > 20) throw new Error("invalid_named_selection");
    return result;
  }

  function elementStatusText(value) {
    return ({
      pending_rebind: "待重新绑定",
      draft: "待验证",
      validating: "验证中",
      healthy: "正常",
      degraded: "已切换备用路径",
      invalid: "失效",
      disabled: "已停用",
    })[code(value)] || "状态未知";
  }

  function businessRunSteps(raw) {
    const source = raw && typeof raw === "object" ? raw : {};
    const supplied = Array.isArray(source.stages)
      ? source.stages
      : source.stages && typeof source.stages === "object"
        ? Object.entries(source.stages).map(([id, value]) => ({id, ...(value || {})}))
        : [];
    const byId = new Map(supplied.map((item) => [code(item?.id || item?.name), item]));
    return BUSINESS_STEPS.map((definition) => {
      const item = byId.get(definition.id) || {};
      return {
        ...definition,
        status: code(item.status || item.result) || "waiting",
        detail: text(item.detail || item.message, 500),
      };
    });
  }

  function createText(document, tagName, value, className) {
    const node = document.createElement(tagName);
    node.textContent = String(value ?? "");
    if (className) node.className = className;
    return node;
  }

  function renderInventory(document, container, raw, options = {}) {
    if (!container || !document) return [];
    const selectedIds = new Set(options.selectedIds || []);
    const names = options.names || {};
    const rows = filterInventory(raw, options.filters).map((item) => {
      const row = document.createElement("article");
      row.className = "selector-inventory-item";
      row.dataset.selectionId = item.selection_id;
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = selectedIds.has(item.selection_id);
      checkbox.dataset.inventorySelect = item.selection_id;
      checkbox.setAttribute("aria-label", `选择 ${item.name || item.text || item.tag}`);
      const body = document.createElement("div");
      body.append(
        createText(document, "strong", `${item.tag || "element"} · ${item.text || "无文本"}`),
        createText(document, "span", `Role: ${item.role || "—"} · Name: ${item.name || "—"}`, "muted"),
        createText(document, "span", `位置: ${item.page_region || "未知"} · ${item.locatable ? "可定位" : "不可定位"}`, "muted"),
        createText(document, "code", item.locators[0]?.value || "暂无有效路径"),
      );
      const name = document.createElement("input");
      name.type = "text";
      name.maxLength = 120;
      name.placeholder = "自定义名称";
      name.value = text(names[item.selection_id], 120);
      name.dataset.inventoryName = item.selection_id;
      name.disabled = !checkbox.checked;
      name.setAttribute("aria-label", `为 ${item.name || item.tag} 设置名称`);
      row.append(checkbox, body, name);
      return row;
    });
    container.replaceChildren(...(rows.length ? rows : [
      createText(document, "p", "当前筛选条件下暂无元素", "selector-probe-empty"),
    ]));
    return rows;
  }

  function renderManagedElements(document, container, raw, options = {}) {
    if (!container || !document) return [];
    const canEdit = options.canEdit === true;
    const items = Array.isArray(raw) ? raw.slice(0, 500) : [];
    const cards = items.map((source) => {
      const item = source && typeof source === "object" ? source : {};
      const card = document.createElement("article");
      card.className = "selector-element-card";
      card.dataset.elementId = text(item.id, 128);
      card.append(
        createText(document, "strong", text(item.display_name, 120) || "未命名元素"),
        createText(document, "span", elementStatusText(item.status || item.published_status), "selector-element-status"),
        createText(document, "span", `页面: ${text(item.page_key, 120) || "—"}`, "muted"),
        createText(document, "code", text(item.primary_locator || item.locator, 500) || item.primary_locator_type || "暂无主路径"),
        createText(document, "span", `策略依赖 ${Math.max(Number(item.dependency_count) || 0, 0)} · 最近验证 ${text(item.last_validated_at, 64) || "无"}`, "muted"),
      );
      const actions = document.createElement("div");
      actions.className = "selector-element-actions";
      [
        ["detail", "详情"], ["rename", "重命名"], ["rebind", "重新绑定"],
        ["screenshot", "现场截图"], ["delete", "删除"],
      ].forEach(([action, label]) => {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = label;
        button.dataset.managedAction = action;
        button.dataset.elementId = card.dataset.elementId;
        if (["rename", "rebind", "delete"].includes(action)) button.hidden = !canEdit;
        if (action === "delete" && Number(item.dependency_count) > 0) {
          button.disabled = true;
          button.title = "请先移除策略引用";
        }
        actions.append(button);
      });
      card.append(actions);
      return card;
    });
    container.replaceChildren(...(cards.length ? cards : [
      createText(document, "p", "尚未选择元素", "selector-probe-empty"),
    ]));
    return cards;
  }

  return {
    BUSINESS_STEPS,
    sanitizeInventory,
    filterInventory,
    serializeNamedSelections,
    elementStatusText,
    businessRunSteps,
    renderInventory,
    renderManagedElements,
  };
});
