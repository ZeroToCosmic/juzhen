/*
 * Small, page-local picker overlay.  It observes only the user's pointer path;
 * it never enumerates the document or reads page storage/content wholesale.
 */
(function (global) {
  "use strict";

  const ACTIONABLE = "button, a, input, textarea, select, [role='button'], [contenteditable='true']";
  const EDITABLE_DESCENDANTS = "input, textarea, [contenteditable]";
  const SAFE_ATTRIBUTES = [
    "data-e2e", "data-testid", "data-test", "data-qa", "aria-label", "role", "name", "placeholder", "id", "contenteditable",
  ];
  const PICKER_UI_ATTRIBUTE = "data-execution-v2-picker-ui";

  function isPickerOwned(path) {
    return (path || []).some((node) => node && node.nodeType === 1
      && typeof node.getAttribute === "function" && node.getAttribute(PICKER_UI_ATTRIBUTE));
  }

  function escapeCss(value) {
    if (global.CSS && typeof global.CSS.escape === "function") return global.CSS.escape(String(value));
    return String(value).replace(/[^a-zA-Z0-9_-]/g, "\\\\$&");
  }

  function isRealEditable(node) {
    if (!node || node.nodeType !== 1) return false;
    const tag = String(node.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea") return true;
    if (typeof node.getAttribute !== "function") return false;
    const value = node.getAttribute("contenteditable");
    return value !== null && String(value).toLowerCase() !== "false";
  }

  function uniqueEditableDescendant(node) {
    if (!node || typeof node.querySelectorAll !== "function") return null;
    const matches = Array.from(node.querySelectorAll(EDITABLE_DESCENDANTS)).filter(isRealEditable);
    return matches.length === 1 ? matches[0] : null;
  }

  function resolveEditableTarget(path) {
    const elements = (path || []).filter((node) => node && node.nodeType === 1);
    for (const node of elements) if (isRealEditable(node)) return node;
    for (const node of elements.slice(0, 6)) {
      const tag = String(node.tagName || "").toLowerCase();
      if (tag === "body" || tag === "html") break;
      const descendant = uniqueEditableDescendant(node);
      if (descendant) return descendant;
    }
    return null;
  }

  function resolveActionable(path) {
    const editable = resolveEditableTarget(path);
    if (editable) return editable;
    for (const node of path || []) {
      if (node && node.nodeType === 1 && typeof node.matches === "function" && node.matches(ACTIONABLE)) return node;
    }
    return (path || []).find((node) => node && node.nodeType === 1) || null;
  }

  function safeAttributes(element) {
    const result = {};
    for (const name of SAFE_ATTRIBUTES) {
      const value = element && typeof element.getAttribute === "function" ? element.getAttribute(name) : null;
      if (typeof value === "string" && (value || name === "contenteditable") && value.length <= 160) result[name] = value;
    }
    return result;
  }

  function uniqueCss(document, element, attrs) {
    if (!document || !element || typeof document.querySelectorAll !== "function") return "";
    const candidates = [];
    if (attrs.id && /^[A-Za-z][A-Za-z0-9_-]{0,79}$/.test(attrs.id)) candidates.push(`#${escapeCss(attrs.id)}`);
    for (const name of ["data-e2e", "data-testid", "data-test", "data-qa", "aria-label"]) {
      if (attrs[name]) candidates.push(`[${name}=${JSON.stringify(attrs[name])}]`);
    }
    for (const selector of candidates) {
      try {
        const matches = document.querySelectorAll(selector);
        if (matches.length === 1 && matches[0] === element) return selector;
      } catch (_) { /* Ignore invalid page-provided values. */ }
    }
    const generator = global.__executionV2UniqueSelector;
    if (typeof generator === "function") {
      try {
        const selector = generator(element, {
          selectorTypes: ["data-e2e", "data-testid", "data-test", "data-qa", "attribute:aria-label", "id", "name", "class", "tag", "nth-child"],
        });
        const root = typeof element.getRootNode === "function" ? element.getRootNode() : document;
        const matches = root && typeof root.querySelectorAll === "function" ? root.querySelectorAll(selector) : [];
        if (selector && matches.length === 1 && matches[0] === element) return selector;
      } catch (_) { /* A page can reject a generated selector; strict dry-run decides saving. */ }
    }
    return "";
  }

  function xpathLiteral(value) {
    const normalized = String(value);
    if (!normalized.includes("'")) return `'${normalized}'`;
    if (!normalized.includes('"')) return `"${normalized}"`;
    return `concat(${normalized.split("'").map((part) => `'${part}'`).join(', "\'", ')})`;
  }

  function relativeXpath(element, attrs) {
    const tag = String((element && element.tagName) || "*").toLowerCase() || "*";
    for (const name of ["data-e2e", "data-testid", "data-test", "data-qa", "aria-label", "id", "name", "placeholder"]) {
      if (attrs[name]) return `//${tag}[@${name}=${xpathLiteral(attrs[name])}]`;
    }
    return "";
  }

  function fingerprint(element, attrs) {
    const tag = String((element && element.tagName) || "").toLowerCase();
    return [tag, attrs["data-e2e"] || attrs.id || attrs["aria-label"] || ""].join(":").slice(0, 220);
  }

  function capture(document, original, actionable) {
    const attrs = safeAttributes(actionable);
    const box = actionable && typeof actionable.getBoundingClientRect === "function"
      ? actionable.getBoundingClientRect() : { x: 0, y: 0, width: 0, height: 0 };
    const name = attrs["aria-label"] || String((actionable && actionable.innerText) || "").trim().slice(0, 160);
    return {
      type: "selection",
      tag: String((original && original.tagName) || "").toLowerCase(),
      original_tag: String((original && original.tagName) || "").toLowerCase(),
      actionable_tag: String((actionable && actionable.tagName) || "").toLowerCase(),
      attributes: attrs,
      role: attrs.role || "",
      name,
      text_preview: String((actionable && actionable.innerText) || "").trim().slice(0, 240),
      frame_path: [],
      original_fingerprint: fingerprint(original, safeAttributes(original)),
      actionable_ancestor_fingerprint: fingerprint(actionable, attrs),
      bounding_box: { x: box.x || 0, y: box.y || 0, width: box.width || 0, height: box.height || 0 },
      unique_css: uniqueCss(document, actionable, attrs),
      relative_xpath: relativeXpath(actionable, attrs),
    };
  }

  function createPickerOverlay(options) {
    const document = options.document;
    const emit = options.emit || function () {};
    let installed = false;
    let marker = null;
    let toolbar = null;
    let modeButtons = [];
    let modeStatus = null;
    let mode = "select";
    let active = null;
    const clearHighlight = () => {
      active = null;
      if (marker) marker.style.display = "none";
    };
    const setMode = (next) => {
      if (next !== "select" && next !== "interact") return false;
      mode = next;
      if (mode === "interact") clearHighlight();
      for (const button of modeButtons) {
        button.setAttribute("aria-pressed", String(button.getAttribute("data-picker-mode") === mode));
      }
      if (modeStatus) modeStatus.textContent = mode === "select"
        ? "当前：选择元素" : "当前：操作页面，可能触发真实行为";
      return true;
    };
    const pickerNode = (tag, ownedValue) => {
      const item = document.createElement(tag);
      item.setAttribute(PICKER_UI_ATTRIBUTE, ownedValue);
      return item;
    };
    const createToolbar = () => {
      const root = pickerNode("div", "toolbar");
      root.setAttribute("role", "toolbar");
      root.setAttribute("aria-label", "点选器模式");
      Object.assign(root.style, {
        position: "fixed", top: "12px", right: "12px", zIndex: "2147483647",
        display: "flex", gap: "6px", alignItems: "center", padding: "8px",
        background: "#111827", color: "#fff", borderRadius: "8px", font: "13px sans-serif",
      });
      const select = pickerNode("button", "control");
      select.type = "button"; select.setAttribute("data-picker-mode", "select"); select.textContent = "选择元素";
      const interact = pickerNode("button", "control");
      interact.type = "button"; interact.setAttribute("data-picker-mode", "interact"); interact.textContent = "操作页面";
      const status = pickerNode("span", "status"); status.setAttribute("data-picker-status", "true");
      for (const [button, value] of [[select, "select"], [interact, "interact"]]) {
        button.addEventListener("click", (event) => {
          event.preventDefault(); event.stopPropagation(); setMode(value);
        });
      }
      modeButtons = [select, interact]; modeStatus = status;
      root.append(select, interact, status);
      return root;
    };
    const move = (event) => {
      const path = typeof event.composedPath === "function" ? event.composedPath() : [event.target];
      if (mode !== "select" || isPickerOwned(path)) { clearHighlight(); return; }
      active = resolveActionable(path);
      if (marker && active && typeof active.getBoundingClientRect === "function") {
        const box = active.getBoundingClientRect();
        Object.assign(marker.style, { display: "block", left: `${box.x}px`, top: `${box.y}px`, width: `${box.width}px`, height: `${box.height}px` });
      }
    };
    const click = (event) => {
      const path = typeof event.composedPath === "function" ? event.composedPath() : [event.target];
      if (mode !== "select" || isPickerOwned(path)) return;
      const actionable = resolveActionable(path);
      if (!actionable || actionable.isConnected === false) return;
      active = actionable;
      event.preventDefault();
      event.stopPropagation();
      emit(capture(document, path[0] || actionable, actionable));
    };
    const keydown = (event) => {
      if (event.key === "F2") {
        event.preventDefault();
        event.stopPropagation();
        setMode(mode === "select" ? "interact" : "select");
        return;
      }
      if (event.key !== "Escape") return;
      event.preventDefault();
      emit({ type: "cancel" });
      api.uninstall();
    };
    const api = {
      install() {
        if (installed) return;
        installed = true;
        mode = "select";
        marker = document.createElement("div");
        marker.setAttribute("aria-hidden", "true");
        marker.setAttribute(PICKER_UI_ATTRIBUTE, "marker");
        Object.assign(marker.style, { position: "fixed", pointerEvents: "none", zIndex: "2147483647", border: "2px solid #4f46e5", background: "rgba(79,70,229,.12)", display: "none" });
        document.body.appendChild(marker);
        try { toolbar = createToolbar(); document.body.appendChild(toolbar); }
        catch (_) { toolbar = null; modeButtons = []; modeStatus = null; }
        setMode("select");
        document.addEventListener("pointermove", move, true);
        document.addEventListener("click", click, true);
        document.addEventListener("keydown", keydown, true);
      },
      uninstall() {
        if (!installed) return;
        installed = false;
        document.removeEventListener("pointermove", move, true);
        document.removeEventListener("click", click, true);
        document.removeEventListener("keydown", keydown, true);
        if (marker && typeof marker.remove === "function") marker.remove();
        if (toolbar && typeof toolbar.remove === "function") toolbar.remove();
        marker = null;
        toolbar = null;
        modeButtons = [];
        modeStatus = null;
        mode = "select";
        active = null;
      },
      mode() { return mode; },
      highlighted() { return active; },
    };
    return api;
  }

  if (global.document) {
    global.__executionV2Picker = global.__executionV2Picker || createPickerOverlay({
      document: global.document,
      emit: (payload) => {
        if (typeof global.__executionV2PickerEvent === "function") global.__executionV2PickerEvent(payload);
      },
    });
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { createPickerOverlay, resolveActionable, resolveEditableTarget, uniqueCss, xpathLiteral };
  }
})(typeof window !== "undefined" ? window : globalThis);
