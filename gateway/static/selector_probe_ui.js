(function (root, factory) {
  "use strict";
  const exported = factory();
  if (typeof module === "object" && module.exports) module.exports = exported;
  if (root && root.document) {
    let controller = null;
    root.SelectorProbeUI = {
      TABS: exported.TABS,
      createSelectorProbeUI: exported.createSelectorProbeUI,
      selectorProbeDependencies: exported.selectorProbeDependencies,
      getController: function () {
        if (!controller) {
          controller = exported.createSelectorProbeUI(
            exported.selectorProbeDependencies(root),
          );
        }
        return controller;
      },
      init: function () {
        return this.getController().init();
      },
    };
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const inventoryUI = (
    typeof globalThis !== "undefined" && globalThis.SelectorInventoryUI
  ) || (
    typeof require === "function"
      ? require("./selector_inventory_ui")
      : null
  );
  const TABS = Object.freeze([
    Object.freeze({id: "collect", label: "采集元素"}),
    Object.freeze({id: "managed", label: "已选元素"}),
    Object.freeze({id: "operations", label: "运行与告警"}),
    Object.freeze({id: "settings", label: "系统设置"}),
  ]);
  const TAB_IDS = new Set(TABS.map((tab) => tab.id));
  const LIST_RESOURCES = new Set([
    "elements",
    "gates",
    "runs",
    "versions",
    "alerts",
  ]);
  const AVAILABLE_ROUTES = Object.freeze({
    overview: "/api/selector-probe/status",
    elements: "/api/selector-probe/elements",
    gates: "/api/selector-probe/gates",
    runs: "/api/selector-probe/runs",
    versions: "/api/selector-probe/versions",
    alerts: "/api/selector-probe/alerts",
    settings: "/api/selector-probe/settings",
  });
  const POLL_INTERVAL_MS = 15000;
  const PICKER_POLL_INTERVAL_MS = 500;
  const PICKER_HIDDEN_POLL_INTERVAL_MS = 1500;
  const ELEMENT_SEARCH_DELAY_MS = 300;
  const ELEMENT_PAGE_SIZES = new Set([20, 50, 100]);
  const ELEMENT_REFERENCED_FILTERS = new Set(["all", "yes", "no"]);
  const ELEMENT_FILTER_DEFAULTS = Object.freeze({
    search: "",
    status: "all",
    source: "all",
    scope: "all",
    referenced: "all",
  });

  function tabIndexForKey(current, key, count) {
    const size = Math.max(Number(count) || 0, 0);
    if (!size) return -1;
    const index = Math.min(Math.max(Number(current) || 0, 0), size - 1);
    if (key === "ArrowRight") return (index + 1) % size;
    if (key === "ArrowLeft") return (index - 1 + size) % size;
    if (key === "Home") return 0;
    if (key === "End") return size - 1;
    return index;
  }

  function trappedFocusIndex(current, backwards, count) {
    const size = Math.max(Number(count) || 0, 0);
    if (!size) return -1;
    const index = Math.min(Math.max(Number(current) || 0, 0), size - 1);
    return backwards
      ? (index - 1 + size) % size
      : (index + 1) % size;
  }

  function stableFocusToken(element) {
    if (!element) return null;
    return {
      id: safeText(element.id, 128),
      dataset: Object.entries(element.dataset || {}).map(([key, value]) => [
        safeText(key, 128),
        safeText(value, 256),
      ]).filter(([key]) => Boolean(key)),
      name: safeText(element.name, 128),
      tagName: safeText(element.tagName, 32).toLowerCase(),
    };
  }

  function resolveFocusToken(document, token) {
    if (!document || !token) return null;
    if (token.id && typeof document.getElementById === "function") {
      const byId = document.getElementById(token.id);
      if (byId) return byId;
    }
    const dataset = Array.isArray(token.dataset) ? token.dataset : [];
    if (dataset.length) {
      const [key] = dataset[0];
      const attribute = key.replace(
        /[A-Z]/g,
        (letter) => `-${letter.toLowerCase()}`,
      );
      const matches = document.querySelectorAll?.(`[data-${attribute}]`) || [];
      const candidate = Array.from(matches).find(
        (node) => dataset.every(
          ([datasetKey, value]) => node.dataset?.[datasetKey] === value,
        ),
      );
      if (candidate) return candidate;
    }
    if (token.name) {
      const matches = document.querySelectorAll?.("[name]") || [];
      return Array.from(matches).find((node) => (
        node.name === token.name
        && (!token.tagName || String(node.tagName || "").toLowerCase()
          === token.tagName)
      )) || null;
    }
    return null;
  }

  function clearSettingsFormSecrets(document) {
    if (!document) return;
    for (const selector of [
      '[name="redisPassword"]',
      '[name="webhookSigningSecret"]',
      '[name="webhookUrl"]',
      '[name="profileAdd"]',
      '[name="reason"]',
      "#selector-operation-reason",
    ]) {
      const control = document.querySelector?.(selector);
      if (control && "value" in control) control.value = "";
    }
  }

  function visibleFocusCandidate(node, getStyle) {
    if (!node || node.hidden || node.disabled) return false;
    if (node.getAttribute?.("aria-hidden") === "true") return false;
    const hiddenAncestor = node.closest?.('[hidden], [aria-hidden="true"]');
    if (hiddenAncestor) return false;
    if (typeof getStyle === "function") {
      let current = node;
      while (current) {
        const style = getStyle(current);
        if (
          style
          && (
            style.display === "none"
            || style.visibility === "hidden"
            || style.visibility === "collapse"
          )
        ) return false;
        current = current.parentElement;
      }
    }
    return true;
  }

  function clone(value) {
    return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
  }

  function errorMessage(result, fallback) {
    const error = result?.data?.error;
    if (typeof error === "string") return error;
    if (error && typeof error.message === "string") return error.message;
    if (typeof result?.data?.code === "string") return result.data.code;
    return fallback;
  }

  function listState(filters) {
    const value = {
      items: [],
      page: 1,
      pageSize: 20,
      total: 0,
      revision: 0,
    };
    if (filters) value.filters = {};
    return value;
  }

  function filterValue(source, name) {
    if (source && source.elements && typeof source.elements.namedItem === "function") {
      return source.elements.namedItem(name)?.value;
    }
    return source ? source[name] : undefined;
  }

  function serializeElementFilters(form) {
    const referenced = String(
      filterValue(form, "referenced")
      || filterValue(form, "dependency")
      || "all",
    );
    return {
      search: String(filterValue(form, "search") || "").trim(),
      status: String(filterValue(form, "status") || "all"),
      source: String(filterValue(form, "source") || "all"),
      scope: String(filterValue(form, "scope") || "all"),
      referenced: ELEMENT_REFERENCED_FILTERS.has(referenced)
        ? referenced
        : "all",
    };
  }

  function encodeQueryValue(value) {
    return encodeURIComponent(String(value)).replace(/%20/g, "%20");
  }

  function buildElementQuery(value) {
    const input = value || {};
    const page = Math.max(Number.parseInt(input.page, 10) || 1, 1);
    const requestedSize = Number.parseInt(input.pageSize ?? input.page_size, 10);
    const pageSize = ELEMENT_PAGE_SIZES.has(requestedSize) ? requestedSize : 20;
    const filters = Object.assign(
      {},
      ELEMENT_FILTER_DEFAULTS,
      input.filters || {},
      input,
    );
    const parts = [
      `page=${page}`,
      `page_size=${pageSize}`,
    ];
    for (const key of ["search", "status", "source", "scope", "referenced"]) {
      const selected = String(filters[key] ?? "").trim();
      if (!selected || selected === "all") continue;
      if (key === "referenced" && !ELEMENT_REFERENCED_FILTERS.has(selected)) {
        continue;
      }
      parts.push(`${key}=${encodeQueryValue(selected)}`);
    }
    return `?${parts.join("&")}`;
  }

  function overviewPriority(item) {
    if (item?.published_status === "failed") return 1;
    if (item?.published_status === "using_lkg") return 2;
    if (item?.draft_status) return 3;
    if (item?.published_status === "probe_unavailable") return 4;
    return 5;
  }

  function selectOverviewElements(items) {
    return (Array.isArray(items) ? items : [])
      .map((item, index) => ({item, index, priority: overviewPriority(item)}))
      .sort((left, right) => {
        if (left.priority !== right.priority) return left.priority - right.priority;
        if (left.priority !== 5) return left.index - right.index;
        const leftTime = Date.parse(left.item?.last_validated_at || "");
        const rightTime = Date.parse(right.item?.last_validated_at || "");
        if (Number.isFinite(leftTime) && Number.isFinite(rightTime)) {
          return leftTime - rightTime;
        }
        if (Number.isFinite(leftTime)) return -1;
        if (Number.isFinite(rightTime)) return 1;
        return right.index - left.index;
      })
      .slice(0, 5)
      .map((entry) => entry.item);
  }

  function canCreateElement(session) {
    return session?.role === "administrator";
  }

  const SAFE_LOCATOR_TYPES = new Set(["attribute", "role", "css", "xpath"]);

  function safeText(value, maximum = 240) {
    return typeof value === "string" ? value.slice(0, maximum) : "";
  }

  function parseProfileIds(value) {
    const values = Array.isArray(value) ? value : [value];
    const unique = new Set();
    values.forEach((item) => {
      safeText(item, 32000).split(/\r?\n/).forEach((line) => {
        const profileId = safeText(line, 256).trim();
        if (profileId) unique.add(profileId);
      });
    });
    return Array.from(unique);
  }

  function normalizeTargetOrigin(value) {
    const input = safeText(value, 2000).trim();
    try {
      return new URL(input).origin;
    } catch (_error) {
      return input;
    }
  }

  function maskProfileId(value) {
    const profileId = safeText(value, 256).trim();
    return profileId ? `***${profileId.slice(-4)}` : "***";
  }

  function settingsStatusText(value) {
    const messages = {
      reason_required: "请填写危险变更原因",
      target_origin_invalid: "目标 Origin 必须是无账号密码的 HTTPS Origin",
      settings_draft_stale_reload_required: "设置已更新，请重新加载后再保存",
      settings_draft_editing: "设置草稿待保存",
      settings_save_failed: "设置保存失败，请重试",
      settings_saved: "设置已保存",
    };
    return safeText(value, 500)
      .split(",")
      .filter(Boolean)
      .map((code) => messages[code] || code)
      .join("；");
  }

  function safeCode(value) {
    const selected = safeText(value, 64);
    return /^[a-z0-9][a-z0-9_-]{0,63}$/i.test(selected) ? selected : "";
  }

  function sanitizeStructuredLocators(locators, options) {
    const editable = Boolean(options?.editable);
    if (!Array.isArray(locators)) return [];
    return locators.slice(0, 20).map((raw, index) => {
      if (!raw || typeof raw !== "object" || !SAFE_LOCATOR_TYPES.has(raw.type)) {
        throw new Error("unsupported_locator_type");
      }
      const base = {
        id: safeText(raw.id, 128) || `locator-${index + 1}`,
        type: raw.type,
      };
      if (raw.type === "attribute") {
        base.name = safeText(raw.name, 128);
        base.value = safeText(raw.value, 240);
      } else if (raw.type === "role") {
        base.role = safeText(raw.role, 64);
        base.name = safeText(raw.name, 240);
        base.name_mode = ["exact", "contains"].includes(raw.name_mode)
          ? raw.name_mode
          : "exact";
      } else {
        const value = safeText(raw.value, 500);
        if (/javascript\s*:/i.test(value)) {
          throw new Error("executable_selector_not_allowed");
        }
        if (raw.type === "xpath" && /^\s*\//.test(value)) {
          if (editable) throw new Error("absolute_xpath_not_allowed");
          base.value = "[历史 XPath 已保留]";
          base.safe_code = "legacy_absolute_xpath_retained";
        } else {
          base.value = value;
        }
      }
      base.enabled = raw.enabled !== false;
      if (typeof raw.fallback === "boolean") base.fallback = raw.fallback;
      return base;
    });
  }

  function validProfileMask(value) {
    return typeof value === "string" && /^\*\*\*.{4}$/u.test(value);
  }

  function safeValidationRound(raw) {
    if (!raw || typeof raw !== "object") return null;
    const profileMask = safeText(raw.profile_mask, 16);
    const round = Number(raw.round ?? raw.round_number);
    if (!validProfileMask(profileMask) || ![1, 2].includes(round)) return null;
    const result = {
      profile_mask: profileMask,
      round,
      status: safeCode(raw.status || raw.result) || "unknown",
    };
    for (const key of [
      "failure_code",
      "page_state",
      "role_name_result",
      "visibility_result",
      "actionability_result",
      "postcondition_result",
    ]) {
      const value = safeCode(raw[key]);
      if (value) result[key] = value;
    }
    if (Number.isInteger(raw.match_count) && raw.match_count >= 0) {
      result.match_count = raw.match_count;
    }
    for (const key of ["visible", "in_viewport", "actionable"]) {
      if (typeof raw[key] === "boolean") result[key] = raw[key];
    }
    return result;
  }

  function summarizeValidation(rounds) {
    const safeRounds = (Array.isArray(rounds) ? rounds : [])
      .map(safeValidationRound)
      .filter(Boolean);
    const profiles = new Map();
    safeRounds.forEach((round) => {
      if (!profiles.has(round.profile_mask)) profiles.set(round.profile_mask, new Map());
      profiles.get(round.profile_mask).set(round.round, round.status);
    });
    const roundNumbers = new Set(safeRounds.map((round) => round.round));
    const publishableProfiles = Array.from(profiles.values()).filter(
      (items) => items.get(1) === "passed" && items.get(2) === "passed",
    );
    return {
      profiles: profiles.size,
      rounds: roundNumbers.size,
      publishable: profiles.size >= 2 && publishableProfiles.length === profiles.size,
    };
  }

  function sanitizeEvidence(raw) {
    const source = raw && typeof raw === "object" ? raw : {};
    const directRounds = Array.isArray(source.rounds) ? source.rounds : [];
    return {
      status: safeCode(source.status) || "unknown",
      last_validated_at: safeText(source.last_validated_at, 128),
      rounds: directRounds.map(safeValidationRound).filter(Boolean).slice(0, 20),
    };
  }

  function sanitizeElementDetail(raw) {
    const source = raw && typeof raw === "object" ? raw : {};
    const manualDefinition = source.definition && typeof source.definition === "object"
      ? {
        page_key: safeText(source.definition.page_key, 120),
        target_origin: safeText(source.definition.target_origin, 255),
        url_pattern: safeText(source.definition.url_pattern, 2000),
        operation_steps: Array.isArray(source.definition.operation_steps)
          ? source.definition.operation_steps.slice(0, 20)
          : [],
        fingerprint: source.definition.fingerprint && typeof source.definition.fingerprint === "object"
          ? clone(source.definition.fingerprint)
          : {},
        locators: inventoryUI
          ? inventoryUI.sanitizeInventory([{
            selection_id: "detail",
            locators: source.definition.locators,
          }])[0]?.locators || []
          : [],
      }
      : null;
    const dependencies = Array.isArray(source.dependencies)
      ? source.dependencies.slice(0, 100).map((item) => ({
        strategy_id: safeText(item?.strategy_id, 128),
        strategy_name: safeText(item?.strategy_name, 128),
        action_id: safeText(item?.action_id, 128),
        action_type: safeText(item?.action_type, 64),
      }))
      : [];
    const history = Array.isArray(source.history)
      ? source.history.slice(0, 100).map((item) => ({
        version_id: safeText(item?.version_id || item?.id, 128),
        status: safeCode(item?.status),
        published_at: safeText(item?.published_at, 128),
        summary: safeText(item?.summary, 240),
      }))
      : [];
    return {
      id: safeText(source.id, 128),
      display_name: safeText(source.display_name, 240),
      status: safeCode(source.status),
      page_key: safeText(source.page_key, 120),
      dependency_count: Number(source.dependency_count) || dependencies.length,
      last_validated_at: safeText(source.last_validated_at, 128),
      revision: (
        Number.isInteger(source.revision) && source.revision >= 0
          ? source.revision
          : 0
      ),
      definition: manualDefinition,
      validation: sanitizeEvidence(source.validation),
      history,
      dependencies,
      alerts: Array.isArray(source.alerts) ? source.alerts.slice(0, 100).map((item) => ({
        id: safeText(item?.id, 128),
        status: safeCode(item?.status),
        failure_code: safeCode(item?.failure_code),
      })) : [],
      strategy_controls: source.strategy_controls && typeof source.strategy_controls === "object"
        ? {
          automatic_pause: source.strategy_controls.automatic_pause === true,
          manual_pause: source.strategy_controls.manual_pause === true,
        }
        : {},
    };
  }

  function safeStringList(value, maximum = 100) {
    return Array.isArray(value)
      ? value.slice(0, maximum).map((item) => safeText(item, 128)).filter(Boolean)
      : [];
  }

  function safeOperationState(value) {
    const source = value && typeof value === "object" ? value : {};
    return {
      status: safeCode(source.status) || "unknown",
      attempt_count: Number.isInteger(source.attempt_count)
        ? Math.max(source.attempt_count, 0)
        : 0,
      round: [1, 2].includes(source.round) ? source.round : null,
      failure_code: safeCode(source.failure_code),
      started_at: safeText(source.started_at, 128),
      finished_at: safeText(source.finished_at, 128),
      next_attempt_at: safeText(source.next_attempt_at, 128),
      duration_ms: Number.isFinite(source.duration_ms)
        ? Math.max(Number(source.duration_ms), 0)
        : null,
    };
  }

  function sanitizeGateReason(raw) {
    const source = raw && typeof raw === "object" ? raw : {};
    return {
      source: ["probe", "manual"].includes(source.source)
        ? source.source
        : "unknown",
      reason_code: safeCode(source.reason_code) || "unknown",
      aliases: safeStringList(source.aliases, 100),
      selector_version_id: safeText(source.selector_version_id, 128),
      affected_action_ids: safeStringList(
        source.affected_action_ids || source.action_ids,
        100,
      ),
      created_at: safeText(source.created_at, 128),
      actor: safeText(source.actor || source.created_by, 128),
    };
  }

  function sanitizeGate(raw) {
    const source = raw && typeof raw === "object" ? raw : {};
    const reasons = Array.isArray(source.reasons)
      ? source.reasons.slice(0, 100).map(sanitizeGateReason)
      : [];
    const effectiveStatus = ["active", "paused", "unmanaged"].includes(
      source.effective_status,
    )
      ? source.effective_status
      : (source.allowed === true ? "active" : "paused");
    return {
      strategy_id: safeText(source.strategy_id, 128),
      strategy_name: safeText(source.strategy_name, 240),
      effective_status: effectiveStatus,
      managed: source.managed !== false && effectiveStatus !== "unmanaged",
      revision: (
        Number.isInteger(source.revision) && source.revision >= 0
          ? source.revision
          : 0
      ),
      reasons,
      aliases: safeStringList(
        source.aliases || reasons.flatMap((item) => item.aliases),
        100,
      ),
      selector_version_id: safeText(source.selector_version_id, 128),
      affected_action_ids: safeStringList(source.affected_action_ids, 100),
      changed_at: safeText(source.changed_at || source.updated_at, 128),
      actor: safeText(source.actor || source.updated_by, 128),
    };
  }

  function sanitizePickerSession(raw) {
    const source = raw && typeof raw === "object" ? raw : {};
    const allowed = new Set([
      "starting", "ready", "selecting", "confirmed", "cancelled",
      "expired", "failed",
    ]);
    return {
      session_id: safeText(source.session_id, 64),
      status: allowed.has(source.status) ? source.status : "failed",
      profile_mask: safeText(source.profile_mask, 32),
      page_state: safeCode(source.page_state),
      inventory: inventoryUI
        ? inventoryUI.sanitizeInventory(source.inventory || [])
        : [],
      recorded_steps: Array.isArray(source.recorded_steps)
        ? source.recorded_steps.slice(0, 20).map((item) => ({
          sequence: Math.max(Number(item?.sequence) || 0, 0),
          locator: sanitizeStructuredLocators(
            item?.locator ? [item.locator] : [],
            {editable: false},
          )[0] || null,
          url_before: safeText(item?.url_before, 500),
          url_after: safeText(item?.url_after, 500),
        }))
        : [],
      truncated: source.truncated === true,
      selection_count: Math.min(Math.max(Number(source.selection_count) || 0, 0), 20),
      max_selections: 20,
      revision: Math.max(Number(source.revision) || 0, 0),
      cleanup: safeCode(source.cleanup),
      failure_code: safeCode(source.failure_code),
      expires_at: safeText(source.expires_at, 64),
    };
  }

  const ACTIVE_RUN_STATUSES = new Set(["queued", "running"]);

  function runIsActive(raw) {
    return ACTIVE_RUN_STATUSES.has(safeCode(raw?.status));
  }

  const USER_STAGE_STATUS_LABELS = Object.freeze({
    waiting: "等待执行",
    running: "运行中",
    skipped: "已跳过",
    success: "成功",
    failed: "失败",
  });
  const SUCCESS_OPERATION_STATUSES = new Set([
    "passed", "completed", "success", "succeeded", "published", "released",
  ]);
  const ACTIVE_OPERATION_STATUSES = new Set([
    "running", "processing", "publishing", "reconciling",
  ]);
  const SKIPPED_OPERATION_STATUSES = new Set(["skipped", "not_required"]);
  const FAILED_OPERATION_STATUSES = new Set([
    "failed", "error", "conflict", "dispatch_failed", "lease_busy",
    "publication_failed", "selector_validation_failed",
    "probe_cleanup_failed", "probe_lease_lost", "probe_safety_violation",
    "infrastructure_failed", "infrastructure_unavailable", "probe_unavailable",
  ]);

  function operationLifecycle(raw, options = {}) {
    const operation = safeOperationState(raw);
    if (options.skip === true) return "skipped";
    if (operation.failure_code || FAILED_OPERATION_STATUSES.has(operation.status)) {
      return "failed";
    }
    if (ACTIVE_OPERATION_STATUSES.has(operation.status)) return "running";
    if (SUCCESS_OPERATION_STATUSES.has(operation.status)) return "success";
    if (SKIPPED_OPERATION_STATUSES.has(operation.status)) {
      return "skipped";
    }
    return "waiting";
  }

  function aggregateLifecycle(values, active) {
    const statuses = values.filter(Boolean);
    if (statuses.includes("failed")) return "failed";
    if (statuses.includes("running")) return "running";
    if (statuses.length && statuses.every((value) => value === "skipped")) {
      return "skipped";
    }
    if (
      statuses.length
      && statuses.every((value) => ["success", "skipped"].includes(value))
    ) return "success";
    if (active && statuses.some((value) => value !== "waiting")) return "running";
    return "waiting";
  }

  function leaseAcquisitionLifecycle(raw) {
    const operation = safeOperationState(raw);
    if (operation.failure_code || FAILED_OPERATION_STATUSES.has(operation.status)) {
      return "failed";
    }
    if (["running", "held", "acquired", "released"].includes(operation.status)) {
      return "success";
    }
    return "waiting";
  }

  function leaseReleaseLifecycle(raw) {
    const operation = safeOperationState(raw);
    if (operation.failure_code || FAILED_OPERATION_STATUSES.has(operation.status)) {
      return "failed";
    }
    return operation.status === "released" ? "success" : "waiting";
  }

  function runStatusLifecycle(status) {
    const code = safeCode(status);
    if (ACTIVE_RUN_STATUSES.has(code)) {
      return code === "queued" ? "waiting" : "running";
    }
    if (code === "awaiting_element_selection") return "skipped";
    if (code === "completed") return "success";
    if (FAILED_OPERATION_STATUSES.has(code)) return "failed";
    return "waiting";
  }

  function stageSignals(run, names) {
    const allowed = new Set(names);
    return run.stages.filter((stage) => allowed.has(stage.name));
  }

  function roundStatusLabel(status) {
    return USER_STAGE_STATUS_LABELS[operationLifecycle({status})];
  }

  function sanitizeRun(raw) {
    const source = raw && typeof raw === "object" ? raw : {};
    const profiles = Array.isArray(source.profiles)
      ? source.profiles.slice(0, 8).map((item) => ({
        profile_mask: safeText(
          typeof item === "string" ? item : item?.profile_mask,
          16,
        ),
        status: safeCode(
          typeof item === "string" ? "" : item?.status,
        ) || "unknown",
      })).filter((item) => validProfileMask(item.profile_mask))
      : [];
    const stages = Array.isArray(source.stages)
      ? source.stages.slice(0, 30).map((item) => {
        const profileMask = safeText(item?.profile_mask, 16);
        return {
          name: safeCode(item?.name) || "unknown",
          ...safeOperationState(item),
          profile_mask: validProfileMask(profileMask) ? profileMask : "",
          summary: safeText(item?.summary, 240),
        };
      })
      : [];
    const elements = Array.isArray(source.elements)
      ? source.elements.slice(0, 200).map((item) => ({
        alias: safeText(item?.alias || item?.element_id, 128),
        status: safeCode(item?.status) || "unknown",
        failure_class: safeCode(item?.failure_class),
      }))
      : [];
    const retryDelay = Number(source.retry_delay_minutes);
    const failure = safeOperationState(source.failure);
    return {
      id: safeText(source.id || source.run_id || source.request_id, 128),
      status: safeCode(source.status) || "unknown",
      trigger: safeCode(source.trigger || source.trigger_type),
      trigger_actor: safeText(source.trigger_actor, 128),
      due_slot: safeText(source.due_slot || source.scheduled_for, 128),
      rollout_mode: safeCode(source.rollout_mode),
      started_at: safeText(source.started_at, 128),
      finished_at: safeText(source.finished_at, 128),
      profiles,
      rounds: Array.isArray(source.rounds)
        ? source.rounds.map(safeValidationRound).filter(Boolean).slice(0, 20)
        : [],
      stages,
      elements,
      failed_aliases: safeStringList(source.failed_aliases, 200),
      publication: safeOperationState(source.publication),
      reconciliation: safeOperationState(source.reconciliation),
      cleanup: safeOperationState(source.cleanup),
      lease: safeOperationState(source.lease),
      failure,
      failure_code: safeCode(source.failure_code) || failure.failure_code,
      failure_class: safeCode(source.failure_class),
      next_retry_at: safeText(source.next_retry_at, 128),
      retry_delay_minutes: [15, 30, 60].includes(retryDelay)
        ? retryDelay
        : null,
      active_version_before: safeText(source.active_version_before, 128),
      published_version_after: safeText(source.published_version_after, 128),
    };
  }

  const FAILURE_REASON_LABELS = Object.freeze({
    awaiting_element_selection: "暂无可验证元素。请先采集并保存元素，或重新绑定现有元素。",
    comment_panel_readiness_timeout: "评论区关键控件未在限定时间内就绪",
    comment_panel_element_missing: "评论区缺少输入框或提交按钮",
    comment_panel_snapshot_unstable: "评论区关键控件持续变化，无法确认稳定路径",
    probe_panel_check_failed: "系统无法安全检查评论区",
    element_candidate_not_found: "未找到可验证的元素候选",
    cdp_unavailable: "无法连接测试 Profile 浏览器",
    profile_cdp_collision: "两个测试 Profile 返回了同一个浏览器连接",
    probe_page_duplicate: "同一个测试 Profile 重复创建了探针页面",
    dispatch_failed: "探针任务未能启动",
    probe_dispatch_failed: "探针调度器执行失败",
    probe_store_unavailable: "探针运行记录存储暂时不可用",
    probe_dependency_unavailable: "探针依赖服务暂时不可用",
    probe_dispatch_timeout: "探针调度启动超时",
    probe_navigation_timeout: "打开 TikTok 页面超时",
    probe_navigation_failed: "打开 TikTok 页面失败",
    page_readiness_timeout: "TikTok 页面未在限定时间内完成加载",
    validate_active_unavailable: "无法读取或验证当前稳定选择器版本",
    candidate_validation_unavailable: "候选选择器验证服务不可用",
    full_validation_unavailable: "两个 Profile 两轮验证未完成",
    lease_busy: "已有探针正在运行",
    publication_failed: "验证结果未能发布",
    selector_validation_failed: "元素选择器验证失败",
    selector_unsafe: "已发现候选路径，但路径未通过安全规则",
  });

  function buildRunPresentation(raw) {
    const run = sanitizeRun(raw);
    const active = runIsActive(run);
    const stageStatus = (names) => aggregateLifecycle(
      stageSignals(run, names).map((stage) => operationLifecycle(stage)),
      active,
    );
    const environmentSignals = stageSignals(run, [
      "cdp_endpoint", "cdp_ready", "probe_page_open", "profile_start",
      "profile_binding", "profile_page_binding",
    ]).map((stage) => operationLifecycle(stage));
    if (run.lease.status !== "unknown") {
      environmentSignals.push(leaseAcquisitionLifecycle(run.lease));
    }
    const environmentStatus = aggregateLifecycle(environmentSignals, active);
    const pageStatus = stageStatus(["page_readiness"]);
    const elementStageEvidence = stageSignals(run, [
      "a11y_snapshot", "candidate_filter", "element_dry_run",
      "comment_panel_transition", "comment_panel_cleanup", "validate",
      "full_validation",
    ]);
    const failedElementEvidence = elementStageEvidence.find(
      (stage) => stage.failure_code || operationLifecycle(stage) === "failed",
    );
    const elementFailureResult = FAILURE_REASON_LABELS[
      failedElementEvidence?.failure_code
    ];
    const elementSignals = elementStageEvidence
      .map((stage) => operationLifecycle(stage)).concat(
      run.elements.map((item) => operationLifecycle({
        status: item.status,
        failure_code: item.failure_class,
      })),
    );
    const elementStatus = aggregateLifecycle(elementSignals, active);

    const profileMasks = Array.from(new Set(
      run.profiles.map((profile) => profile.profile_mask)
        .concat(run.rounds.map((round) => round.profile_mask))
        .filter(Boolean),
    ));
    const roundLines = profileMasks.map((profileMask) => {
      const rounds = run.rounds.filter(
        (round) => round.profile_mask === profileMask,
      );
      const line = [1, 2].map((number) => {
        const round = rounds.find((item) => item.round === number);
        return `第 ${number} 轮${round ? roundStatusLabel(round.status) : "等待执行"}`;
      }).join(" / ");
      return `${profileMask}：${line}`;
    });
    const roundStatuses = run.rounds.map((round) => operationLifecycle({
      status: round.status,
      failure_code: round.failure_code,
    }));
    let roundsStatus = aggregateLifecycle(roundStatuses, active);
    const profilesComplete = profileMasks.length >= 2 && profileMasks.every(
      (profileMask) => [1, 2].every((number) => run.rounds.some((round) => (
        round.profile_mask === profileMask
        && round.round === number
        && operationLifecycle({status: round.status}) === "success"
      ))),
    );
    if (profilesComplete) roundsStatus = "success";
    if (!run.rounds.length && !run.profiles.length) roundsStatus = "waiting";

    const observeOnly = run.rollout_mode === "observe";
    const publicationStatus = operationLifecycle(
      run.publication,
      {skip: observeOnly},
    );
    const reconciliationStatus = operationLifecycle(
      run.reconciliation,
      {skip: observeOnly},
    );
    const finalStatus = aggregateLifecycle([
      publicationStatus,
      reconciliationStatus,
      operationLifecycle(run.cleanup),
      leaseReleaseLifecycle(run.lease),
    ], false);

    const stages = [
      {
        id: "environment",
        title: "准备测试环境",
        purpose: "连接两个独立测试 Profile，取得运行锁并打开探针页面。",
        status: environmentStatus,
        result: run.profiles.length
          ? `已记录 ${run.profiles.length}/2 个测试 Profile`
          : "Profile 验证尚未开始",
      },
      {
        id: "page",
        title: "加载 TikTok 页面",
        purpose: "确认页面不是空白、登录、验证码或初始加载状态。",
        status: pageStatus,
        result: pageStatus === "success"
          ? "TikTok 页面已就绪"
          : "等待页面完成加载",
      },
      {
        id: "elements",
        title: "发现并验证元素",
        purpose: "使用已保存路径定位元素，并执行只读 Dry-Run。",
        status: elementStatus,
        result: elementFailureResult
          ? elementFailureResult
          : (run.elements.length
            ? `已记录 ${run.elements.length} 个元素结果`
            : "尚未产生元素验证结果"),
      },
      {
        id: "rounds",
        title: "两个 Profile 连续两轮确认",
        purpose: "排除单账号、单轮或临时网络状态造成的假成功。",
        status: roundsStatus,
        result: roundLines.length
          ? roundLines.join("；")
          : "稳定性验证尚未开始",
      },
      {
        id: "finalize",
        title: "发布结果并清理",
        purpose: "发布验证结果、同步受影响策略、关闭页面并释放运行锁。",
        status: finalStatus,
        result: observeOnly
          ? "观察模式，不发布；本次无需协调"
          : `发布${USER_STAGE_STATUS_LABELS[publicationStatus]}；策略协调${USER_STAGE_STATUS_LABELS[reconciliationStatus]}；清理${USER_STAGE_STATUS_LABELS[operationLifecycle(run.cleanup)]}`,
      },
    ].map((stage) => ({
      ...stage,
      statusLabel: USER_STAGE_STATUS_LABELS[stage.status],
    }));

    const overallStatus = runStatusLifecycle(run.status);
    const awaitingElementSelection = (
      run.status === "awaiting_element_selection"
    );
    if (overallStatus === "success" || awaitingElementSelection) {
      stages.forEach((stage, index) => {
        if (stage.status !== "waiting") return;
        stages[index] = {
          ...stage,
          status: "skipped",
          statusLabel: USER_STAGE_STATUS_LABELS.skipped,
          result: awaitingElementSelection
            ? "尚无已保存且可验证的元素，本步骤未启动"
            : "历史记录未保留此步骤明细",
        };
      });
    }
    const failedStage = stages.find((stage) => stage.status === "failed");
    const unrecordedFailure = (
      overallStatus === "failed" && !failedStage
    ) ? {
      id: "unrecorded_failure",
      title: "失败阶段未记录",
      purpose: "后端未保存可确认的失败阶段，不将故障归因到任一业务步骤。",
      status: "failed",
      statusLabel: USER_STAGE_STATUS_LABELS.failed,
      result: "请查看安全错误码和后台安全日志。",
    } : null;
    const currentStage = failedStage
      || unrecordedFailure
      || stages.find((stage) => stage.status === "running")
      || stages.find((stage) => stage.status === "waiting")
      || stages.at(-1);
    const failureStage = run.stages.find(
      (stage) => stage.failure_code || operationLifecycle(stage) === "failed",
    );
    const failureCode = failureStage?.failure_code
      || run.failure_code
      || run.failure.failure_code
      || run.failure_class
      || (overallStatus === "failed" ? run.status : "");
    const impactParts = [
      failureStage?.profile_mask,
      failureStage?.round ? `第 ${failureStage.round} 轮` : "",
      run.failed_aliases.length ? `元素 ${run.failed_aliases.join("、")}` : "",
    ].filter(Boolean);
    const failure = (failedStage || overallStatus === "failed") ? {
      reason: FAILURE_REASON_LABELS[failureCode] || "探针未完成当前步骤",
      impact: impactParts.join("；") || "等待系统确认",
      nextAction: run.next_retry_at
        ? `系统计划在 ${run.next_retry_at} 重试`
        : (run.active_version_before
          ? "继续使用上一稳定版，等待后续验证"
          : "等待系统重试或人工确认"),
    } : null;

    return {
      run,
      status: overallStatus,
      statusLabel: USER_STAGE_STATUS_LABELS[overallStatus],
      currentStage,
      completedStages: stages.filter(
        (stage) => ["success", "skipped"].includes(stage.status),
      ).length,
      stages,
      result: awaitingElementSelection
        ? FAILURE_REASON_LABELS.awaiting_element_selection
        : (failure?.reason || currentStage.result),
      failure,
    };
  }

  function runTechnicalLines(raw) {
    const run = sanitizeRun(raw);
    const stages = run.stages.map((stage) => [
      stage.name,
      stage.status,
      stage.profile_mask ? `Profile ${stage.profile_mask}` : "",
      stage.round ? `第 ${stage.round} 轮` : "",
      stage.attempt_count ? `尝试 ${stage.attempt_count}` : "",
      stage.duration_ms !== null ? `${stage.duration_ms}ms` : "",
      stage.failure_code || "",
    ].filter(Boolean).join(" · "));
    const operationLine = (label, value) => {
      const operation = safeOperationState(value);
      return [
        `${label}: ${operation.status}`,
        operation.failure_code,
        operation.duration_ms !== null ? `${operation.duration_ms}ms` : "",
        operation.attempt_count ? `尝试 ${operation.attempt_count}` : "",
      ].filter(Boolean).join(" · ");
    };
    const operations = [
      operationLine("publish", run.publication),
      operationLine("reconcile", run.reconciliation),
      operationLine("cleanup", run.cleanup),
      operationLine("lease", run.lease),
    ];
    return stages.concat(operations);
  }

  function sanitizeVersion(raw) {
    const source = raw && typeof raw === "object" ? raw : {};
    const rawDiff = source.diff && typeof source.diff === "object"
      ? source.diff
      : {};
    const changedElements = Array.isArray(rawDiff.changed_elements)
      ? rawDiff.changed_elements.slice(0, 200).map((item) => ({
        alias: safeText(item?.alias || item?.element_id, 128),
        change: safeCode(item?.change || item?.status),
        from_version: safeText(item?.from_version, 128),
        to_version: safeText(item?.to_version, 128),
      }))
      : [];
    const evidence = Array.isArray(source.evidence)
      ? source.evidence.map(safeValidationRound).filter(Boolean).slice(0, 20)
      : [];
    return {
      id: safeText(source.id || source.version_id, 128),
      status: safeCode(source.status) || "unknown",
      is_active: source.is_active === true || source.status === "active",
      is_lkg: source.is_lkg === true || source.status === "lkg",
      base_version_id: safeText(source.base_version_id, 128),
      bundle_hash: safeText(source.bundle_hash, 128),
      created_at: safeText(source.created_at, 128),
      validated_at: safeText(source.validated_at, 128),
      published_at: safeText(source.published_at, 128),
      changed_elements: changedElements,
      dependencies: Array.isArray(source.dependencies)
        ? source.dependencies.slice(0, 200).map((item) => ({
          strategy_id: safeText(item?.strategy_id, 128),
          strategy_name: safeText(item?.strategy_name, 240),
          aliases: safeStringList(item?.aliases, 100),
          action_ids: safeStringList(item?.action_ids, 100),
        }))
        : [],
      evidence,
      sqlite: safeOperationState(source.sqlite),
      outbox: safeOperationState(source.outbox),
      redis: safeOperationState(source.redis),
      lua: safeOperationState(source.lua),
      reconciliation: safeOperationState(source.reconciliation),
      prior_lkg_version: safeText(
        source.prior_lkg_version || rawDiff.prior_lkg_version,
        128,
      ),
      revision: (
        Number.isInteger(source.revision) && source.revision >= 0
          ? source.revision
          : 0
      ),
    };
  }

  function sanitizeAlert(raw) {
    const source = raw && typeof raw === "object" ? raw : {};
    const numericId = Number(source.id);
    const id = Number.isInteger(numericId) && numericId > 0 ? numericId : null;
    const retries = Array.isArray(source.retries)
      ? source.retries.slice(0, 20).map((item) => ({
        delay_minutes: [15, 30, 60].includes(Number(item?.delay_minutes))
          ? Number(item.delay_minutes)
          : null,
        status: safeCode(item?.status) || "unknown",
        next_attempt_at: safeText(item?.next_attempt_at, 128),
      }))
      : [];
    return {
      id,
      status: ["open", "acknowledged", "resolved"].includes(source.status)
        ? source.status
        : "open",
      severity: safeCode(source.severity) || "unknown",
      failure_class: safeCode(source.failure_class),
      occurrence_count: Number.isInteger(source.occurrence_count)
        ? Math.max(source.occurrence_count, 1)
        : 1,
      aliases: safeStringList(source.aliases, 100),
      strategy_ids: safeStringList(
        source.strategy_ids || source.strategies,
        100,
      ),
      active_version: safeText(source.active_version, 128),
      lkg_version: safeText(source.lkg_version, 128),
      retries,
      webhook: safeOperationState(source.webhook),
      gate_active: source.gate_active === true,
      screenshot_available: source.screenshot_available === true && id !== null,
      screenshot_url: source.screenshot_available === true && id !== null
        ? `/api/selector-probe/alerts/${id}/screenshot`
        : "",
      created_at: safeText(source.created_at, 128),
      updated_at: safeText(source.updated_at, 128),
      acknowledged_at: safeText(source.acknowledged_at, 128),
      resolved_at: safeText(source.resolved_at, 128),
      revision: (
        Number.isInteger(source.revision) && source.revision >= 0
          ? source.revision
          : 0
      ),
      timeline: Array.isArray(source.timeline)
        ? source.timeline.slice(0, 100).map((item) => ({
          event: safeCode(item?.event || item?.type),
          occurred_at: safeText(item?.occurred_at, 128),
          actor: safeText(item?.actor, 128),
        }))
        : [],
    };
  }

  function manualResumeOutcome(raw) {
    const gate = sanitizeGate(raw);
    const probeReasons = gate.reasons.filter((item) => item.source === "probe");
    if (probeReasons.length) {
      return `${gate.strategy_id} 的 manual 原因将被清除；probe 原因仍存在，策略仍将暂停。`;
    }
    return `${gate.strategy_id} 的 manual 原因将被清除；无其他原因时策略恢复执行。`;
  }

  function versionActions(raw) {
    const version = sanitizeVersion(raw);
    if (
      version.is_active
      || ["validated_pending", "pending", "failed", "conflict"].includes(
        version.status,
      )
    ) return [];
    if (version.is_lkg || version.status === "superseded") {
      return [{
        id: "rollback-validation",
        label: "基于此版本发起回滚验证",
      }];
    }
    return [];
  }

  function alertActionModel(raw) {
    const alert = sanitizeAlert(raw);
    return {
      acknowledge: {
        label: "确认告警",
        clears_gate: false,
        disabled: alert.status !== "open",
      },
      resolve: {
        label: "解决告警",
        clears_gate: false,
        disabled: alert.status === "resolved" || alert.gate_active,
      },
    };
  }

  function operationConfirmationIsDangerous(workspace) {
    if (!workspace) return false;
    if (workspace.kind === "gate-confirm") return workspace.action === "pause";
    if (workspace.kind === "alert-confirm") {
      return workspace.action === "resolve";
    }
    if (workspace.kind === "secret-clear-confirm") return true;
    if (workspace.kind === "settings-confirm") {
      return Array.isArray(workspace.dangerousChanges)
        && workspace.dangerousChanges.length > 0;
    }
    return false;
  }

  function sanitizeSettings(raw) {
    const source = raw && typeof raw === "object" ? raw : {};
    const redis = source.redis && typeof source.redis === "object"
      ? source.redis
      : {};
    const webhook = source.webhook && typeof source.webhook === "object"
      ? source.webhook
      : {};
    const retry = source.retry_policy && typeof source.retry_policy === "object"
      ? source.retry_policy
      : {};
    const profileMap = new Map();
    if (Array.isArray(source.profiles)) {
      source.profiles.slice(0, 20).forEach((item) => {
        const profileRef = safeText(item?.profile_ref, 128);
        const profileMask = safeText(item?.profile_mask, 16);
        if (!profileRef || !validProfileMask(profileMask)) return;
        profileMap.set(profileRef, {
          profile_ref: profileRef,
          profile_mask: profileMask,
          dedicated_test: item?.dedicated_test === true,
          status: safeCode(item?.status) || "unknown",
          last_checked_at: safeText(item?.last_checked_at, 128),
        });
      });
    }
    const profiles = Array.from(profileMap.values());
    return {
      revision: (
        Number.isInteger(source.revision) && source.revision >= 0
          ? source.revision
          : 0
      ),
      enabled: source.enabled === true,
      rollout_mode: ["observe", "publish", "enforce"].includes(
        source.rollout_mode,
      ) ? source.rollout_mode : "observe",
      schedule_time: /^\d{2}:\d{2}$/.test(source.schedule_time)
        ? source.schedule_time
        : "03:00",
      timezone: safeText(source.timezone, 64) || "Asia/Shanghai",
      target_origin: safeText(source.target_origin, 240),
      page_timeout_seconds: Number.isInteger(source.page_timeout_seconds)
        ? Math.min(Math.max(source.page_timeout_seconds, 10), 300)
        : 90,
      freshness_hours: Number.isInteger(source.freshness_hours)
        ? Math.max(source.freshness_hours, 0)
        : 36,
      site: safeCode(source.site) || "tiktok",
      environment: safeCode(source.environment) || "production",
      retry_policy: {
        delays_minutes: Array.isArray(retry.delays_minutes)
          ? retry.delays_minutes.filter(
            (item) => [15, 30, 60].includes(Number(item)),
          ).map(Number).slice(0, 3)
          : [15, 30, 60],
      },
      profiles,
      redis: {
        status: safeCode(redis.status) || "unknown",
        namespace: safeText(redis.namespace, 128),
        aof_enabled: redis.aof_enabled === true,
        eviction_policy: safeCode(redis.eviction_policy),
        password_set: redis.password_set === true,
        last_reconciled_at: safeText(redis.last_reconciled_at, 128),
      },
      webhook: {
        enabled: webhook.enabled === true,
        type: safeCode(webhook.type),
        url_display: safeText(webhook.url_display, 240),
        signing_secret_set: webhook.signing_secret_set === true,
        timeout_seconds: Number.isFinite(webhook.timeout_seconds)
          ? Math.max(Number(webhook.timeout_seconds), 0)
          : 0,
        retry_policy: safeText(webhook.retry_policy, 128),
        status: safeCode(webhook.status) || "unknown",
        last_delivery_at: safeText(webhook.last_delivery_at, 128),
      },
    };
  }

  function settingsPermissions(session) {
    const role = session?.role;
    const permissions = new Set(
      Array.isArray(session?.permissions) ? session.permissions : [],
    );
    return {
      canEdit: role === "administrator",
      canClearSecrets: role === "administrator",
      canManageAccounts: role === "administrator",
      canTestWebhook: (
        role === "administrator"
        || (role === "operator" && permissions.has("webhook:test"))
      ),
    };
  }

  function comparableSetting(value) {
    return JSON.stringify(value);
  }

  function settingsMutationPayload(raw) {
    const settings = sanitizeSettings(raw);
    return {
      enabled: settings.enabled,
      rollout_mode: settings.rollout_mode,
      schedule_time: settings.schedule_time,
      timezone: settings.timezone,
      target_origin: settings.target_origin,
      page_timeout_seconds: settings.page_timeout_seconds,
      freshness_hours: settings.freshness_hours,
      retry_policy: clone(settings.retry_policy),
      profiles: settings.profiles.map((item) => ({
        profile_ref: item.profile_ref,
        dedicated_test: item.dedicated_test,
      })),
      redis: {namespace: settings.redis.namespace},
      webhook: {
        enabled: settings.webhook.enabled,
        type: settings.webhook.type,
        timeout_seconds: settings.webhook.timeout_seconds,
        retry_policy: settings.webhook.retry_policy,
      },
    };
  }

  function settingsFingerprint(raw) {
    const input = comparableSetting(settingsMutationPayload(raw));
    let hash = 0x811c9dc5;
    for (let index = 0; index < input.length; index += 1) {
      hash ^= input.charCodeAt(index);
      hash = Math.imul(hash, 0x01000193);
    }
    return `fnv1a-${(hash >>> 0).toString(16).padStart(8, "0")}`;
  }

  function dangerousSettingsDiff(beforeRaw, afterRaw) {
    const before = sanitizeSettings(beforeRaw);
    const after = sanitizeSettings(afterRaw);
    const changed = [];
    if (before.enabled !== after.enabled) changed.push("enabled");
    if (before.rollout_mode !== after.rollout_mode) {
      changed.push("rollout_mode");
    }
    if (before.target_origin !== after.target_origin) {
      changed.push("target_origin");
    }
    if (
      comparableSetting(settingsMutationPayload(before).redis)
      !== comparableSetting(settingsMutationPayload(after).redis)
    ) changed.push("redis");
    if (
      comparableSetting(settingsMutationPayload(before).profiles)
      !== comparableSetting(settingsMutationPayload(after).profiles)
    ) changed.push("profiles");
    return changed;
  }

  function validateSettingsSave(beforeRaw, afterRaw, options) {
    const before = sanitizeSettings(beforeRaw);
    const after = sanitizeSettings(afterRaw);
    const changes = dangerousSettingsDiff(before, after);
    const errors = [];
    let targetOriginValid = false;
    try {
      const parsed = new URL(after.target_origin);
      targetOriginValid = (
        parsed.protocol === "https:"
        && parsed.origin === after.target_origin
        && !parsed.username
        && !parsed.password
      );
    } catch (_error) {
      targetOriginValid = false;
    }
    if (!targetOriginValid) errors.push("target_origin_invalid");
    if (changes.length && !safeText(options?.reason, 500).trim()) {
      errors.push("reason_required");
    }
    return {errors, dangerous_changes: changes};
  }

  function syntheticWebhookPayload(raw) {
    const settings = sanitizeSettings(raw);
    return {
      event: "selector_probe.webhook_test",
      environment: settings.environment,
      site: settings.site,
      synthetic: true,
    };
  }

  function sanitizeAccount(raw) {
    const source = raw && typeof raw === "object" ? raw : {};
    const id = Number(source.id);
    return {
      id: Number.isInteger(id) && id > 0 ? id : null,
      username: safeText(source.username, 128),
      role: ["administrator", "operator"].includes(source.role)
        ? source.role
        : "operator",
      enabled: source.enabled === true,
      must_change_password: source.must_change_password === true,
      locked_until: safeText(source.locked_until, 128),
      last_login_at: safeText(source.last_login_at, 128),
      created_at: safeText(source.created_at, 128),
      updated_at: safeText(source.updated_at, 128),
      revision: (
        Number.isInteger(source.revision) && source.revision > 0
          ? source.revision
          : 1
      ),
    };
  }

  function accountActionModel(raw, allRaw, session) {
    const account = sanitizeAccount(raw);
    const accounts = (Array.isArray(allRaw) ? allRaw : []).map(sanitizeAccount);
    const enabledAdministrators = accounts.filter(
      (item) => item.enabled && item.role === "administrator",
    );
    const lastAdministrator = (
      account.enabled
      && account.role === "administrator"
      && enabledAdministrators.length <= 1
    );
    const canManage = settingsPermissions(session).canManageAccounts;
    return {
      disable: {
        disabled: !canManage || !account.enabled || lastAdministrator,
      },
      enable: {
        disabled: !canManage || account.enabled,
      },
      demote: {
        disabled: !canManage || account.role !== "administrator" || lastAdministrator,
      },
      promote: {
        disabled: !canManage || account.role === "administrator",
      },
      resetPassword: {disabled: !canManage},
      revokeSessions: {disabled: !canManage},
      lastAdministrator,
    };
  }

  function createSelectorProbeUI(dependencies) {
    const deps = dependencies || {};
    const state = {
      activeTab: "collect",
      status: null,
      overview: null,
      elements: listState(true),
      gates: listState(),
      runs: listState(),
      versions: listState(),
      alerts: listState(),
      settings: null,
      settingsDraft: null,
      settingsDraftBaseRevision: null,
      settingsDraftStale: false,
      settingsStatus: "",
      settingsProfileAdds: [],
      accounts: {items: [], error: "", loading: false},
      temporaryCredential: null,
      session: null,
      picker: null,
      pendingRebindId: "",
      operationWorkspace: null,
      pending: new Map(),
    };
    const generations = new Map();
    let initialized = false;
    let initPromise = null;
    let pollTimer = null;
    let elementSearchTimer = null;
    let pickerTimer = null;
    let operationGeneration = 0;
    let runDetailTimer = null;
    let pendingSettingsSecrets = {};
    let pendingProfileAdds = [];
    let removeVisibilityListener = null;
    let destroyed = false;

    function render(view) {
      if (typeof deps.render === "function") {
        deps.render(view || state.activeTab, state, api);
      }
    }

    async function request(url, method, body, options) {
      try {
        return await deps.requestJson(url, method || "GET", body, options);
      } catch (error) {
        return {
          status: 0,
          data: {
            error: error && error.message ? error.message : "请求失败",
          },
        };
      }
    }

    function acceptRevision(resource, incoming) {
      const revision = Number(
        incoming && typeof incoming === "object"
          ? incoming.revision
          : incoming,
      );
      if (!Number.isFinite(revision)) return true;
      const current = Number(state[resource]?.revision || 0);
      return revision >= current;
    }

    function routeFor(resource) {
      const route = AVAILABLE_ROUTES[resource];
      if (!route || !LIST_RESOURCES.has(resource)) return route || null;
      const target = state[resource];
      if (resource === "elements") {
        return `${route}${buildElementQuery({
          page: target.page,
          pageSize: target.pageSize,
          filters: target.filters,
        })}`;
      }
      return `${route}?page=${target.page}&page_size=${target.pageSize}`;
    }

    function incomingRevision(resource, payload) {
      const explicit = Number(payload && payload.revision);
      if (Number.isFinite(explicit)) return explicit;
      const active = Number(payload && payload.active_revision);
      if (Number.isFinite(active)) return active;
      return Number(state[resource]?.revision || 0);
    }

    function applyPayload(resource, payload) {
      const data = payload && typeof payload === "object" ? payload : {};
      const revision = incomingRevision(resource, data);
      if (!acceptRevision(resource, revision)) return false;
      if (resource === "overview") {
        state.overview = Object.assign({}, clone(data), {revision});
        return true;
      }
      if (resource === "settings") {
        state.settings = Object.assign(sanitizeSettings(data), {revision});
        if (
          state.settingsDraft
          && Number.isInteger(state.settingsDraftBaseRevision)
          && state.settingsDraftBaseRevision !== revision
        ) {
          state.settingsDraftStale = true;
          state.settingsStatus = "settings_draft_stale_reload_required";
        }
        return true;
      }
      const target = state[resource];
      if (!target) return false;
      const pagination = data.pagination || {};
      const pageSize = Number(
        data.page_size
        || pagination.page_size
        || pagination.limit
        || target.pageSize,
      );
      const offset = Number(pagination.offset || 0);
      const items = Array.isArray(data.items) ? clone(data.items) : [];
      target.items = items;
      target.pageSize = Number.isFinite(pageSize) && pageSize > 0
        ? pageSize
        : target.pageSize;
      target.page = Number(data.page || pagination.page) || (
        Math.floor(offset / target.pageSize) + 1
      );
      target.total = Number(
        data.total ?? data.count ?? pagination.total ?? pagination.count,
      );
      if (!Number.isFinite(target.total)) target.total = items.length;
      if (data.counts && typeof data.counts === "object") {
        target.counts = clone(data.counts);
      }
      target.revision = revision;
      return true;
    }

    function stageSettingsDraft(raw) {
      if (state.session?.role !== "administrator" || !state.settings) {
        return false;
      }
      if (!state.settingsDraft) {
        state.settingsDraftBaseRevision = state.settings.revision;
      }
      state.settingsDraft = sanitizeSettings(raw);
      state.settingsDraft.revision = state.settingsDraftBaseRevision;
      state.settingsDraftStale = (
        state.settingsDraftBaseRevision !== state.settings.revision
      );
      state.settingsStatus = state.settingsDraftStale
        ? "settings_draft_stale_reload_required"
        : "settings_draft_editing";
      render("settings");
      return !state.settingsDraftStale;
    }

    async function reloadSettingsDraft() {
      if (state.session?.role !== "administrator") return false;
      pendingSettingsSecrets = {};
      pendingProfileAdds = [];
      state.settingsProfileAdds = [];
      state.settingsDraft = null;
      state.settingsDraftBaseRevision = null;
      state.settingsDraftStale = false;
      if (state.operationWorkspace?.kind === "settings-confirm") {
        state.operationWorkspace = null;
      }
      if (typeof deps.clearSettingsFormSecrets === "function") {
        deps.clearSettingsFormSecrets();
      }
      state.settingsStatus = "settings_reloading";
      render("settings");
      return refresh("settings");
    }

    function cancelPending(resource) {
      const current = state.pending.get(resource);
      if (current?.controller && typeof current.controller.abort === "function") {
        current.controller.abort();
      }
      state.pending.delete(resource);
      generations.set(resource, (generations.get(resource) || 0) + 1);
    }

    async function refresh(resource) {
      if (destroyed) return false;
      const focusToken = typeof deps.captureFocus === "function"
        ? deps.captureFocus()
        : null;
      const renderRefresh = () => {
        render(resource);
        if (focusToken && typeof deps.restoreFocus === "function") {
          deps.restoreFocus(focusToken);
        }
      };
      const route = routeFor(resource);
      if (!route) {
        state.status = null;
        renderRefresh();
        return true;
      }
      cancelPending(resource);
      const generation = (generations.get(resource) || 0) + 1;
      generations.set(resource, generation);
      const controller = typeof deps.createAbortController === "function"
        ? deps.createAbortController()
        : null;
      state.pending.set(resource, {generation, controller});
      state.status = {kind: "loading", message: "正在刷新"};
      renderRefresh();
      let result;
      try {
        result = await deps.requestJson(
          route,
          "GET",
          undefined,
          controller ? {signal: controller.signal} : undefined,
        );
      } catch (error) {
        result = {
          status: 0,
          data: {error: error && error.message ? error.message : "请求失败"},
        };
      }
      if (destroyed || generations.get(resource) !== generation) return false;
      state.pending.delete(resource);
      if (!result || result.status !== 200) {
        state.status = {
          kind: "error",
          message: result?.data?.error || "刷新失败",
        };
        renderRefresh();
        return false;
      }
      const accepted = applyPayload(resource, result.data);
      state.status = accepted
        ? {kind: "success", message: "已更新"}
        : {kind: "stale", message: "已忽略旧版本数据"};
      renderRefresh();
      return accepted;
    }

    function refreshCurrent() {
      if (state.activeTab === "collect") {
        return refresh("overview");
      }
      if (state.activeTab === "managed") return refresh("elements");
      if (state.activeTab === "operations") {
        return Promise.all([
          refresh("runs"), refresh("alerts"), refresh("gates"), refresh("versions"),
        ]).then((results) => results.every(Boolean));
      }
      return refresh(state.activeTab);
    }

    function normalizedElementFilters(filters) {
      return Object.assign(
        {},
        ELEMENT_FILTER_DEFAULTS,
        serializeElementFilters(filters || {}),
      );
    }

    function updateElementFilters(filters, options) {
      state.elements.filters = normalizedElementFilters(filters);
      state.elements.page = 1;
      cancelPending("elements");
      render("elements");
      if (elementSearchTimer !== null && typeof deps.clearTimeout === "function") {
        deps.clearTimeout(elementSearchTimer);
      }
      elementSearchTimer = null;
      if (options?.debounce && typeof deps.setTimeout === "function") {
        elementSearchTimer = deps.setTimeout(async function () {
          elementSearchTimer = null;
          return refresh("elements");
        }, ELEMENT_SEARCH_DELAY_MS);
        return Promise.resolve(false);
      }
      return refresh("elements");
    }

    function setElementPage(page) {
      const lastPage = Math.max(
        Math.ceil(state.elements.total / state.elements.pageSize),
        1,
      );
      state.elements.page = Math.min(
        Math.max(Number.parseInt(page, 10) || 1, 1),
        lastPage,
      );
      return refresh("elements");
    }

    function setElementPageSize(pageSize) {
      const selected = Number.parseInt(pageSize, 10);
      state.elements.pageSize = ELEMENT_PAGE_SIZES.has(selected) ? selected : 20;
      state.elements.page = 1;
      return refresh("elements");
    }

    function activateSummary(tabId, filters) {
      if (tabId === "elements") {
        state.elements.filters = normalizedElementFilters(filters || {});
        state.elements.page = 1;
      } else if (state[tabId] && typeof state[tabId] === "object" && filters) {
        state[tabId].filters = Object.assign({}, filters);
      }
      return activateTab(tabId);
    }

    function stopPickerPolling() {
      if (pickerTimer !== null && typeof deps.clearTimeout === "function") {
        deps.clearTimeout(pickerTimer);
      }
      pickerTimer = null;
    }

    function schedulePickerPoll() {
      stopPickerPolling();
      const session = state.picker?.session;
      if (
        destroyed
        || !session?.session_id
        || !["starting", "ready", "selecting"].includes(session.status)
        || typeof deps.setTimeout !== "function"
      ) return;
      const delay = deps.documentVisible?.() === false
        ? PICKER_HIDDEN_POLL_INTERVAL_MS
        : PICKER_POLL_INTERVAL_MS;
      pickerTimer = deps.setTimeout(pollLivePicker, delay);
    }

    async function openLivePicker() {
      if (!canCreateElement(state.session)) return false;
      stopPickerPolling();
      state.picker = {
        open: true,
        loading: true,
        profiles: [],
        profileRef: "",
        pageState: "feed_ready",
        session: null,
        excludedSelectionIds: [],
        selectedIds: [],
        names: {},
        filters: {type: "all", region: "all", locatable: false, search: ""},
        error: "",
      };
      render("picker");
      const result = await request(
        "/api/selector-probe/settings/profiles", "GET",
      );
      if (!state.picker?.open) return false;
      if (result.status !== 200) {
        state.picker.loading = false;
        state.picker.error = errorMessage(result, "读取测试 Profile 失败");
        render("picker");
        return false;
      }
      const profiles = Array.isArray(result.data?.items)
        ? result.data.items.filter((item) => (
          item?.dedicated_test === true
          && typeof item.profile_ref === "string"
          && item.profile_ref
        )).map((item) => ({
          profile_ref: safeText(item.profile_ref, 128),
          profile_mask: safeText(item.profile_mask, 32),
          status: safeCode(item.status),
        }))
        : [];
      state.picker.loading = false;
      state.picker.profiles = profiles;
      state.picker.profileRef = profiles[0]?.profile_ref || "";
      state.picker.error = profiles.length ? "" : "没有可用的独立测试 Profile";
      render("picker");
      return profiles.length > 0;
    }

    async function beginElementRebind(elementId) {
      if (!canCreateElement(state.session)) return false;
      const id = safeText(elementId, 128);
      if (!state.elements.items.some((item) => item.id === id)) return false;
      state.pendingRebindId = id;
      state.activeTab = "collect";
      const opened = await openLivePicker();
      if (!opened) state.pendingRebindId = "";
      return opened;
    }

    async function startLivePicker(form) {
      const picker = state.picker;
      if (!picker?.open || picker.session) return false;
      const profileRef = String(
        filterValue(form, "profileRef")
        || picker.profileRef || "",
      ).trim();
      const pageState = String(
        filterValue(form, "pageState")
        || picker.pageState || "feed_ready",
      ).trim();
      if (!profileRef || !["feed_ready", "comment_panel_open"].includes(pageState)) {
        picker.error = "请选择测试 Profile 和页面状态";
        render("picker");
        return false;
      }
      picker.loading = true;
      picker.error = "";
      picker.profileRef = profileRef;
      picker.pageState = pageState;
      render("picker");
      const result = await request(
        "/api/selector-probe/picker/start",
        "POST",
        {profile_ref: profileRef, page_state: pageState},
      );
      if (!state.picker?.open) return false;
      picker.loading = false;
      if (result.status !== 202) {
        picker.error = errorMessage(result, "启动实时拾取失败");
        render("picker");
        return false;
      }
      picker.session = sanitizePickerSession(result.data);
      render("picker");
      schedulePickerPoll();
      return true;
    }

    async function pollLivePicker() {
      pickerTimer = null;
      const picker = state.picker;
      const sessionId = picker?.session?.session_id;
      if (!picker?.open || !sessionId) return false;
      const result = await request(
        `/api/selector-probe/picker/${encodeURIComponent(sessionId)}`,
        "GET",
      );
      if (!state.picker?.open || state.picker.session?.session_id !== sessionId) {
        return false;
      }
      if (result.status !== 200) {
        picker.error = errorMessage(result, "读取拾取状态失败");
        render("picker");
        schedulePickerPoll();
        return false;
      }
      picker.session = sanitizePickerSession(result.data);
      if (picker.session.failure_code) picker.error = picker.session.failure_code;
      render("picker");
      schedulePickerPoll();
      return true;
    }

    function removeLivePickerSelection(selectionId) {
      const picker = state.picker;
      if (!picker?.session) return false;
      const selected = safeText(selectionId, 64);
      if (!selected) return false;
      picker.excludedSelectionIds = Array.from(new Set([
        ...(picker.excludedSelectionIds || []),
        selected,
      ]));
      render("picker");
      return true;
    }

    function setCollectorSelection(selectionId, selected) {
      const picker = state.picker;
      const id = safeText(selectionId, 64);
      if (!picker || !id) return false;
      const ids = new Set(picker.selectedIds || []);
      if (selected) ids.add(id);
      else ids.delete(id);
      picker.selectedIds = Array.from(ids).slice(0, 20);
      render("picker");
      return true;
    }

    function setCollectorName(selectionId, displayName) {
      const picker = state.picker;
      const id = safeText(selectionId, 64);
      if (!picker || !id) return false;
      picker.names[id] = safeText(displayName, 120);
      return true;
    }

    function setCollectorFilters(filters) {
      if (!state.picker) return false;
      state.picker.filters = Object.assign({}, state.picker.filters, filters || {});
      render("picker");
      return true;
    }

    function includedPickerSelections() {
      const picker = state.picker;
      const excluded = new Set(picker?.excludedSelectionIds || []);
      return (picker?.session?.inventory || picker?.session?.selections || []).filter(
        (item) => item.selection_id && !excluded.has(item.selection_id),
      );
    }

    async function confirmLivePicker(rawSelections) {
      const picker = state.picker;
      const session = picker?.session;
      let namedSelections;
      try {
        namedSelections = inventoryUI.serializeNamedSelections(rawSelections || []);
      } catch (_error) {
        namedSelections = [];
      }
      if (!session?.session_id || !namedSelections.length) {
        if (picker) picker.error = "请至少选择一个元素并填写不重复的名称";
        render("picker");
        return false;
      }
      const rebindId = state.pendingRebindId;
      if (rebindId && namedSelections.length !== 1) {
        picker.error = "重新绑定时只能选择一个元素";
        render("picker");
        return false;
      }
      const inventoryById = new Map(
        (session.inventory || []).map((item) => [item.selection_id, item]),
      );
      picker.loading = true;
      render("picker");
      const result = await request(
        `/api/selector-probe/picker/${encodeURIComponent(session.session_id)}/confirm`,
        "POST",
        {
          expected_revision: session.revision,
          selections: namedSelections,
        },
      );
      if (!state.picker?.open) return false;
      picker.loading = false;
      if (result.status !== 200) {
        picker.error = errorMessage(result, "确认拾取结果失败");
        render("picker");
        return false;
      }
      stopPickerPolling();
      const confirmed = sanitizePickerSession(result.data);
      const targetOrigin = state.settings?.target_origin || "https://www.tiktok.com";
      const recordedSteps = confirmed.recorded_steps.length
        ? confirmed.recorded_steps
        : session.recorded_steps;
      const failures = [];
      for (const named of namedSelections) {
        const item = inventoryById.get(named.selection_id);
        if (!item || !item.locators?.length) {
          failures.push(named.display_name);
          continue;
        }
        const definition = {
          page_key: `tiktok.${session.page_state || "page"}`,
          target_origin: targetOrigin,
          url_pattern: `${targetOrigin}/*`,
          operation_steps: recordedSteps.filter((step) => step.locator),
          fingerprint: {
            tag: item.tag,
            input_type: item.input_type,
            role: item.role,
            name: item.name,
            attributes: item.attributes,
            region: item.region,
            page_region: item.page_region,
            frame_key: item.frame_key,
            shadow: item.shadow,
          },
          locators: item.locators.map(({type, value}) => ({type, value})),
        };
        let saved;
        if (rebindId) {
          const existing = state.elements.items.find((entry) => entry.id === rebindId);
          saved = await request(
            `/api/selector-probe/elements/${encodeURIComponent(rebindId)}`,
            "PATCH",
            {
              operation: "rebind",
              definition,
              expected_revision: Number(existing?.revision),
            },
          );
          if (saved.status === 200 && named.display_name !== existing?.display_name) {
            saved = await request(
              `/api/selector-probe/elements/${encodeURIComponent(rebindId)}`,
              "PATCH",
              {
                operation: "rename",
                display_name: named.display_name,
                expected_revision: Number(saved.data?.revision),
              },
            );
          }
        } else {
          saved = await request("/api/selector-probe/elements", "POST", {
            display_name: named.display_name,
            ...definition,
          });
        }
        if (![200, 201].includes(saved.status)) failures.push(named.display_name);
      }
      state.picker = null;
      state.pendingRebindId = "";
      state.status = failures.length
        ? {kind: "error", message: `以下元素保存失败：${failures.join("、")}`}
        : {kind: "success", message: rebindId
          ? "元素已重新绑定，等待双 Profile 双轮验证"
          : `已保存 ${namedSelections.length} 个待验证元素`};
      state.activeTab = "managed";
      await refresh("elements");
      render("managed");
      return failures.length === 0;
    }

    async function cancelLivePicker() {
      const picker = state.picker;
      const session = picker?.session;
      stopPickerPolling();
      state.picker = null;
      state.pendingRebindId = "";
      render("picker");
      if (!session?.session_id || !["starting", "ready", "selecting"].includes(session.status)) {
        return true;
      }
      await request(
        `/api/selector-probe/picker/${encodeURIComponent(session.session_id)}/cancel`,
        "POST",
        {expected_revision: session.revision},
      );
      return true;
    }

    async function renameElement(elementId, displayName, expectedRevision) {
      if (!canCreateElement(state.session)) return false;
      const id = safeText(elementId, 128);
      const name = safeText(displayName, 120).trim();
      if (!id || !name) return false;
      const result = await request(
        `/api/selector-probe/elements/${encodeURIComponent(id)}`,
        "PATCH",
        {
          operation: "rename",
          display_name: name,
          expected_revision: Number(expectedRevision),
        },
      );
      if (result.status !== 200) {
        state.status = {kind: "error", message: errorMessage(result, "重命名失败")};
        render("managed");
        return false;
      }
      await refresh("elements");
      return true;
    }

    async function rebindElement(elementId, definition, expectedRevision) {
      if (!canCreateElement(state.session)) return false;
      const id = safeText(elementId, 128);
      if (!id || !definition || typeof definition !== "object") return false;
      const result = await request(
        `/api/selector-probe/elements/${encodeURIComponent(id)}`,
        "PATCH",
        {
          operation: "rebind",
          definition,
          expected_revision: Number(expectedRevision),
        },
      );
      if (result.status !== 200) {
        state.status = {kind: "error", message: errorMessage(result, "重新绑定失败")};
        render("managed");
        return false;
      }
      await refresh("elements");
      return true;
    }

    async function deleteElement(elementId, expectedRevision) {
      if (!canCreateElement(state.session)) return false;
      const id = safeText(elementId, 128);
      const summary = state.elements.items.find((item) => item.id === id);
      if (!id || Number(summary?.dependency_count) > 0) {
        state.status = {kind: "error", message: "该元素仍被策略引用，请先移除引用"};
        render("managed");
        return false;
      }
      const result = await request(
        `/api/selector-probe/elements/${encodeURIComponent(id)}`,
        "DELETE",
        {expected_revision: Number(expectedRevision)},
      );
      if (result.status !== 204) {
        state.status = {kind: "error", message: errorMessage(result, "删除元素失败")};
        render("managed");
        return false;
      }
      await refresh("elements");
      return true;
    }

    async function openManagedElementDetail(elementId) {
      const id = safeText(elementId, 128);
      if (!id) return false;
      const generation = replaceOperationWorkspace({
        kind: "element-detail",
        detail: sanitizeElementDetail({id}),
        error: "",
        busy: true,
      });
      const result = await request(
        `/api/selector-probe/elements/${encodeURIComponent(id)}`,
        "GET",
      );
      if (!currentOperation(generation, "element-detail")) return false;
      state.operationWorkspace.busy = false;
      if (result.status !== 200) {
        state.operationWorkspace.error = errorMessage(result, "元素详情不可用");
        render("operation-workspace");
        return false;
      }
      state.operationWorkspace.detail = sanitizeElementDetail(result.data);
      render("operation-workspace");
      return true;
    }

    function operationError(result, fallback) {
      const error = result?.data?.error;
      if (typeof error === "string") return safeCode(error) || fallback;
      if (error && typeof error === "object") {
        return safeCode(error.code) || fallback;
      }
      return safeCode(result?.data?.code) || fallback;
    }

    function idempotencyKey(action, target) {
      if (typeof deps.createIdempotencyKey === "function") {
        const injected = safeText(
          deps.createIdempotencyKey(action, target),
          128,
        );
        if (injected) return injected;
      }
      if (
        typeof globalThis !== "undefined"
        && globalThis.crypto
        && typeof globalThis.crypto.randomUUID === "function"
      ) {
        return `${action}-${globalThis.crypto.randomUUID()}`.slice(0, 128);
      }
      return `${action}-${safeCode(target) || "target"}-${Date.now()}-${Math.random()
        .toString(16)
        .slice(2)}`.slice(0, 128);
    }

    function stopRunDetailPolling() {
      if (
        runDetailTimer !== null
        && typeof deps.clearInterval === "function"
      ) {
        deps.clearInterval(runDetailTimer);
      }
      runDetailTimer = null;
    }

    function replaceOperationWorkspace(value) {
      stopRunDetailPolling();
      operationGeneration += 1;
      state.operationWorkspace = value
        ? Object.assign({}, value, {generation: operationGeneration})
        : null;
      render("operation-workspace");
      return operationGeneration;
    }

    function currentOperation(generation, kind) {
      return (
        !destroyed
        && state.operationWorkspace?.generation === generation
        && (!kind || state.operationWorkspace.kind === kind)
      );
    }

    function closeOperationWorkspace() {
      pendingSettingsSecrets = {};
      replaceOperationWorkspace(null);
      return true;
    }

    function confirmManualGate(raw, action) {
      if (
        state.session?.role !== "administrator"
        || !["pause", "resume"].includes(action)
      ) return false;
      const gate = sanitizeGate(raw);
      if (!gate.strategy_id) return false;
      const hasManual = gate.reasons.some((item) => item.source === "manual");
      if ((action === "pause" && hasManual) || (action === "resume" && !hasManual)) {
        return false;
      }
      replaceOperationWorkspace({
        kind: "gate-confirm",
        action,
        detail: gate,
        outcome: action === "resume"
          ? manualResumeOutcome(gate)
          : `${gate.strategy_id} 将立即增加 manual 原因并暂停。`,
        error: "",
        busy: false,
      });
      return true;
    }

    async function submitManualGate(reason) {
      const workspace = state.operationWorkspace;
      if (
        state.session?.role !== "administrator"
        || workspace?.kind !== "gate-confirm"
      ) return false;
      const selectedReason = safeText(reason, 500).trim();
      if (!selectedReason) {
        workspace.error = "reason_required";
        render("operation-workspace");
        return false;
      }
      const generation = workspace.generation;
      const action = workspace.action;
      const gate = workspace.detail;
      workspace.busy = true;
      workspace.error = "";
      render("operation-workspace");
      const result = await request(
        `/api/selector-probe/strategies/${encodeURIComponent(gate.strategy_id)}/${action}`,
        "POST",
        {
          reason: selectedReason,
          expected_revision: gate.revision,
          idempotency_key: idempotencyKey(action, gate.strategy_id),
        },
      );
      if (!currentOperation(generation, "gate-confirm")) return false;
      workspace.busy = false;
      if (result.status !== 200) {
        workspace.error = operationError(result, "gate_operation_failed");
        render("operation-workspace");
        return false;
      }
      const updated = sanitizeGate(result.data);
      replaceOperationWorkspace({
        kind: "gate-result",
        detail: updated,
        outcome: action === "resume"
          ? manualResumeOutcome(updated)
          : `${updated.strategy_id} 已增加 manual 原因；当前状态为 ${updated.effective_status}。`,
        error: "",
        busy: false,
      });
      await refresh("gates");
      return true;
    }

    async function openRunDetail(runId, context) {
      const id = safeText(runId, 128);
      if (!id) return false;
      const generation = replaceOperationWorkspace({
        kind: "run-detail",
        detail: sanitizeRun({
          id,
          status: context?.fallbackStatus || "loading",
        }),
        error: safeCode(context?.error),
        busy: true,
      });
      const result = await request(
        `/api/selector-probe/runs/${encodeURIComponent(id)}`,
        "GET",
      );
      if (!currentOperation(generation, "run-detail")) return false;
      state.operationWorkspace.busy = false;
      if (result.status !== 200) {
        state.operationWorkspace.error = (
          safeCode(context?.error)
          || operationError(result, "run_detail_unavailable")
        );
        render("operation-workspace");
        return false;
      }
      state.operationWorkspace.detail = sanitizeRun(result.data);
      state.operationWorkspace.error = safeCode(context?.error);
      state.operationWorkspace.busy = runIsActive(
        state.operationWorkspace.detail,
      );
      render("operation-workspace");
      if (
        state.operationWorkspace.busy
        && typeof deps.setInterval === "function"
      ) {
        runDetailTimer = deps.setInterval(async function () {
          if (!currentOperation(generation, "run-detail")) {
            stopRunDetailPolling();
            return;
          }
          const update = await request(
            `/api/selector-probe/runs/${encodeURIComponent(id)}`,
            "GET",
          );
          if (
            !currentOperation(generation, "run-detail")
            || update.status !== 200
          ) return;
          state.operationWorkspace.detail = sanitizeRun(update.data);
          state.operationWorkspace.busy = runIsActive(
            state.operationWorkspace.detail,
          );
          if (!state.operationWorkspace.busy) stopRunDetailPolling();
          render("operation-workspace");
        }, 1000);
      }
      return true;
    }

    async function requestRunNow(options) {
      if (!["administrator", "operator"].includes(state.session?.role)) {
        return false;
      }
      const retryRunId = safeText(options?.retryRunId, 128);
      const generation = replaceOperationWorkspace({
        kind: "run-request",
        detail: null,
        error: "",
        busy: true,
      });
      const payload = {
        idempotency_key: idempotencyKey(
          retryRunId ? "run-retry" : "run-now",
          retryRunId,
        ),
      };
      if (retryRunId) payload.retry_of_run_id = retryRunId;
      const result = await request(
        "/api/selector-probe/run-now",
        "POST",
        payload,
      );
      if (!currentOperation(generation, "run-request")) return false;
      const activeRunId = safeText(
        result.data?.active_run_id
        || result.data?.run_id
        || result.data?.request_id,
        128,
      );
      state.activeTab = "operations";
      if (result.status === 409 && operationError(result, "") === "probe_busy") {
        if (activeRunId) {
          return openRunDetail(activeRunId, {error: "probe_busy"});
        }
        state.operationWorkspace.busy = false;
        state.operationWorkspace.error = "probe_busy";
        render("operation-workspace");
        return false;
      }
      if (result.status !== 202) {
        state.operationWorkspace.busy = false;
        state.operationWorkspace.error = operationError(
          result,
          "run_request_failed",
        );
        render("operation-workspace");
        return false;
      }
      if (!activeRunId) {
        state.operationWorkspace.busy = false;
        state.operationWorkspace.error = "run_id_missing";
        render("operation-workspace");
        return false;
      }
      await refresh("runs");
      return openRunDetail(activeRunId, {fallbackStatus: "accepted"});
    }

    function requestRunRetry(runId) {
      return requestRunNow({retryRunId: runId});
    }

    async function openVersionDetail(versionId) {
      const id = safeText(versionId, 128);
      if (!id) return false;
      const generation = replaceOperationWorkspace({
        kind: "version-detail",
        detail: sanitizeVersion({id}),
        error: "",
        busy: true,
      });
      const detailResult = await request(
        `/api/selector-probe/versions/${encodeURIComponent(id)}`,
        "GET",
      );
      if (!currentOperation(generation, "version-detail")) return false;
      if (detailResult.status !== 200) {
        state.operationWorkspace.busy = false;
        state.operationWorkspace.error = operationError(
          detailResult,
          "version_detail_unavailable",
        );
        render("operation-workspace");
        return false;
      }
      const diffResult = await request(
        `/api/selector-probe/versions/${encodeURIComponent(id)}/diff`,
        "GET",
      );
      if (!currentOperation(generation, "version-detail")) return false;
      state.operationWorkspace.busy = false;
      const detail = Object.assign({}, detailResult.data);
      if (diffResult.status === 200) {
        detail.diff = diffResult.data;
      } else {
        state.operationWorkspace.error = operationError(
          diffResult,
          "version_diff_unavailable",
        );
      }
      state.operationWorkspace.detail = sanitizeVersion(detail);
      render("operation-workspace");
      return true;
    }

    function confirmRollbackValidation(raw) {
      if (state.session?.role !== "administrator") return false;
      const version = sanitizeVersion(raw);
      if (
        !version.id
        || !versionActions(version).some(
          (item) => item.id === "rollback-validation",
        )
      ) return false;
      replaceOperationWorkspace({
        kind: "rollback-confirm",
        detail: version,
        error: "",
        busy: false,
      });
      return true;
    }

    async function submitRollbackValidation(reason) {
      const workspace = state.operationWorkspace;
      if (
        state.session?.role !== "administrator"
        || workspace?.kind !== "rollback-confirm"
      ) return false;
      const selectedReason = safeText(reason, 500).trim();
      if (!selectedReason) {
        workspace.error = "reason_required";
        render("operation-workspace");
        return false;
      }
      const generation = workspace.generation;
      const sourceVersion = workspace.detail.id;
      workspace.busy = true;
      render("operation-workspace");
      const result = await request(
        `/api/selector-probe/versions/${encodeURIComponent(sourceVersion)}/rollback-validation`,
        "POST",
        {
          reason: selectedReason,
          idempotency_key: idempotencyKey(
            "rollback-validation",
            sourceVersion,
          ),
        },
      );
      if (!currentOperation(generation, "rollback-confirm")) return false;
      workspace.busy = false;
      if (result.status !== 202) {
        workspace.error = operationError(
          result,
          "rollback_validation_failed",
        );
        render("operation-workspace");
        return false;
      }
      workspace.kind = "rollback-result";
      workspace.draftVersion = safeText(result.data?.draft_version, 128);
      workspace.requestId = safeText(result.data?.request_id, 128);
      workspace.error = "";
      await refresh("versions");
      render("operation-workspace");
      return true;
    }

    function openAlertDetail(raw) {
      const alert = sanitizeAlert(raw);
      if (alert.id === null) return false;
      replaceOperationWorkspace({
        kind: "alert-detail",
        detail: alert,
        error: "",
        busy: false,
      });
      return true;
    }

    function confirmAlertAction(raw, action) {
      const alert = sanitizeAlert(raw);
      const model = alertActionModel(alert);
      const role = state.session?.role;
      if (
        alert.id === null
        || !["acknowledge", "resolve"].includes(action)
        || (action === "acknowledge" && !["administrator", "operator"].includes(role))
        || (action === "resolve" && role !== "administrator")
        || model[action].disabled
      ) return false;
      replaceOperationWorkspace({
        kind: "alert-confirm",
        action,
        detail: alert,
        outcome: action === "acknowledge"
          ? "仅记录告警归属；不会清除任何策略闸门。"
          : "仅在底层策略闸门不再生效时解决告警。",
        error: "",
        busy: false,
      });
      return true;
    }

    async function submitAlertAction(reason) {
      const workspace = state.operationWorkspace;
      if (workspace?.kind !== "alert-confirm") return false;
      const role = state.session?.role;
      if (
        (workspace.action === "acknowledge"
          && !["administrator", "operator"].includes(role))
        || (workspace.action === "resolve" && role !== "administrator")
      ) return false;
      const selectedReason = safeText(reason, 500).trim();
      if (workspace.action === "resolve" && !selectedReason) {
        workspace.error = "reason_required";
        render("operation-workspace");
        return false;
      }
      if (workspace.action === "resolve" && workspace.detail.gate_active) {
        workspace.error = "gate_still_active";
        render("operation-workspace");
        return false;
      }
      const generation = workspace.generation;
      const action = workspace.action;
      const alert = workspace.detail;
      const payload = {
        idempotency_key: idempotencyKey(action, String(alert.id)),
      };
      if (action === "resolve") {
        payload.reason = selectedReason;
        payload.expected_revision = alert.revision;
      }
      workspace.busy = true;
      render("operation-workspace");
      const result = await request(
        `/api/selector-probe/alerts/${alert.id}/${action}`,
        "POST",
        payload,
      );
      if (!currentOperation(generation, "alert-confirm")) return false;
      workspace.busy = false;
      if (result.status !== 200) {
        workspace.error = operationError(result, "alert_operation_failed");
        render("operation-workspace");
        return false;
      }
      const updated = sanitizeAlert(Object.assign({}, alert, result.data));
      replaceOperationWorkspace({
        kind: "alert-result",
        action,
        detail: updated,
        outcome: action === "acknowledge"
          ? "告警已确认；策略闸门保持不变。"
          : "告警已解决；未执行任何策略恢复动作。",
        error: "",
        busy: false,
      });
      await refresh("alerts");
      return true;
    }

    async function acknowledgeAlert(alertId) {
      const selectedId = Number(alertId);
      const alert = state.alerts.items.find(
        (item) => Number(item.id) === selectedId,
      );
      if (!alert || !confirmAlertAction(alert, "acknowledge")) return false;
      return submitAlertAction("");
    }

    function writeOnlySettings(raw) {
      const source = raw && typeof raw === "object" ? raw : {};
      const result = {};
      for (const name of [
        "redis_password",
        "webhook_signing_secret",
        "webhook_url",
      ]) {
        const value = safeText(source[name], 2000);
        if (value) result[name] = value;
      }
      return result;
    }

    function confirmSettingsSave(raw, reason, secrets) {
      if (state.session?.role !== "administrator" || !state.settings) {
        return false;
      }
      if (
        state.settingsDraftStale
        || (
          Number.isInteger(state.settingsDraftBaseRevision)
          && state.settingsDraftBaseRevision !== state.settings.revision
        )
      ) {
        state.settingsDraftStale = true;
        state.settingsStatus = "settings_draft_stale_reload_required";
        render("settings");
        return false;
      }
      const candidate = sanitizeSettings(raw);
      const selectedReason = safeText(reason, 500).trim();
      const validation = validateSettingsSave(
        state.settings,
        candidate,
        {reason: selectedReason},
      );
      const privateChanges = writeOnlySettings(secrets);
      if (
        pendingProfileAdds.length
        && !validation.dangerous_changes.includes("profiles")
      ) {
        validation.dangerous_changes.push("profiles");
        if (!selectedReason && !validation.errors.includes("reason_required")) {
          validation.errors.push("reason_required");
        }
      }
      const blockingErrors = validation.errors.filter(
        (error) => error !== "reason_required",
      );
      if (blockingErrors.length) {
        state.settingsStatus = blockingErrors.join(",");
        render("settings");
        return false;
      }
      pendingSettingsSecrets = privateChanges;
      state.settingsStatus = "";
      replaceOperationWorkspace({
        kind: "settings-confirm",
        detail: candidate,
        reason: selectedReason,
        requiresReason: validation.dangerous_changes.length > 0,
        dangerousChanges: validation.dangerous_changes,
        outcome: validation.dangerous_changes.length
          ? `二次确认危险变更：${validation.dangerous_changes.join(", ")}`
          : "确认保存设置。",
        error: "",
        busy: false,
        baseRevision: state.settingsDraftBaseRevision
          ?? state.settings.revision,
      });
      return true;
    }

    async function submitSettingsSave(reason) {
      const workspace = state.operationWorkspace;
      if (
        state.session?.role !== "administrator"
        || workspace?.kind !== "settings-confirm"
        || !state.settings
      ) return false;
      const selectedReason = safeText(
        reason === undefined ? workspace.reason : reason,
        500,
      ).trim();
      if (workspace.requiresReason && !selectedReason) {
        workspace.error = "reason_required";
        render("operation-workspace");
        return false;
      }
      workspace.reason = selectedReason;
      workspace.error = "";
      const generation = workspace.generation;
      const body = {
        expected_revision: workspace.baseRevision,
        reason: workspace.reason,
        idempotency_key: idempotencyKey("settings-save", "settings"),
        settings: settingsMutationPayload(workspace.detail),
      };
      const privateChanges = clone(pendingSettingsSecrets);
      if (pendingProfileAdds.length) {
        body.profile_changes = {add: clone(pendingProfileAdds)};
      }
      if (Object.keys(privateChanges).length) {
        body.secrets = privateChanges;
      }
      pendingSettingsSecrets = {};
      workspace.busy = true;
      render("operation-workspace");
      const result = await request(
        "/api/selector-probe/settings",
        "PATCH",
        body,
      );
      if (!currentOperation(generation, "settings-confirm")) return false;
      workspace.busy = false;
      if (result.status !== 200) {
        workspace.error = operationError(result, "settings_save_failed");
        render("operation-workspace");
        return false;
      }
      state.settings = sanitizeSettings(result.data);
      state.settingsDraft = null;
      state.settingsDraftBaseRevision = null;
      state.settingsDraftStale = false;
      pendingProfileAdds = [];
      state.settingsProfileAdds = [];
      state.settingsStatus = "settings_saved";
      workspace.kind = "settings-result";
      workspace.detail = state.settings;
      workspace.outcome = "设置已保存；服务端审计记录为最终依据。";
      render("settings");
      return true;
    }

    function syncProfileAdds() {
      state.settingsProfileAdds = pendingProfileAdds.map((profileId, index) => ({
        index,
        profile_mask: maskProfileId(profileId),
      }));
    }

    function stageProfileAdds(raw) {
      if (state.session?.role !== "administrator" || !state.settings) return 0;
      const before = pendingProfileAdds.length;
      pendingProfileAdds = parseProfileIds([
        ...pendingProfileAdds,
        ...parseProfileIds(raw),
      ]);
      syncProfileAdds();
      const added = pendingProfileAdds.length - before;
      state.settingsStatus = added
        ? `已暂存 ${added} 个 Profile；保存后生效`
        : "没有可新增的 Profile";
      render("settings");
      return added;
    }

    function importSelectedProfiles() {
      const selected = typeof deps.selectedAdsPowerProfileIds === "function"
        ? deps.selectedAdsPowerProfileIds()
        : [];
      return stageProfileAdds(selected);
    }

    function removeStagedProfile(index) {
      if (state.session?.role !== "administrator") return false;
      const selectedIndex = Number(index);
      if (
        !Number.isInteger(selectedIndex)
        || selectedIndex < 0
        || selectedIndex >= pendingProfileAdds.length
      ) return false;
      pendingProfileAdds.splice(selectedIndex, 1);
      syncProfileAdds();
      state.settingsStatus = "已从暂存列表移除";
      render("settings");
      return true;
    }

    function stageProfileRemoval(profileRef) {
      if (state.session?.role !== "administrator" || !state.settings) {
        return false;
      }
      const draft = sanitizeSettings(state.settingsDraft || state.settings);
      if (!state.settingsDraft) {
        state.settingsDraftBaseRevision = state.settings.revision;
      }
      if (state.settingsDraftBaseRevision !== state.settings.revision) {
        state.settingsDraftStale = true;
        state.settingsStatus = "settings_draft_stale_reload_required";
        render("settings");
        return false;
      }
      const selectedRef = safeText(profileRef, 128);
      if (
        !selectedRef
        || draft.profiles.length <= 2
        || !draft.profiles.some(
          (item) => item.profile_ref === selectedRef,
        )
      ) return false;
      draft.profiles = draft.profiles.filter(
        (item) => item.profile_ref !== selectedRef,
      );
      state.settingsDraft = draft;
      state.settingsDraft.revision = state.settingsDraftBaseRevision;
      state.settingsDraftStale = false;
      state.settingsStatus = "profile_removal_staged";
      render("settings");
      return true;
    }

    function confirmSecretClear(name, reason) {
      if (
        state.session?.role !== "administrator"
        || !state.settings
        || ![
          "redis_password",
          "webhook_signing_secret",
        ].includes(name)
      ) return false;
      const selectedReason = safeText(reason, 500).trim();
      if (!selectedReason) {
        state.settingsStatus = "reason_required";
        render("settings");
        return false;
      }
      pendingSettingsSecrets = {};
      replaceOperationWorkspace({
        kind: "secret-clear-confirm",
        secretName: name,
        reason: selectedReason,
        outcome: `将独立清除 ${name}；空白写入不会触发此操作。`,
        error: "",
        busy: false,
      });
      return true;
    }

    async function submitSecretClear() {
      const workspace = state.operationWorkspace;
      if (
        state.session?.role !== "administrator"
        || workspace?.kind !== "secret-clear-confirm"
        || !state.settings
      ) return false;
      const generation = workspace.generation;
      workspace.busy = true;
      render("operation-workspace");
      const result = await request(
        `/api/selector-probe/settings/secrets/${encodeURIComponent(workspace.secretName)}/clear`,
        "POST",
        {
          expected_revision: state.settings.revision,
          reason: workspace.reason,
          idempotency_key: idempotencyKey(
            "settings-secret-clear",
            workspace.secretName,
          ),
        },
      );
      if (!currentOperation(generation, "secret-clear-confirm")) return false;
      workspace.busy = false;
      if (result.status !== 200) {
        workspace.error = operationError(result, "secret_clear_failed");
        render("operation-workspace");
        return false;
      }
      state.settings = sanitizeSettings(result.data);
      workspace.kind = "settings-result";
      workspace.detail = state.settings;
      workspace.outcome = "秘密已清除。";
      render("settings");
      return true;
    }

    async function requestWebhookTest() {
      if (
        !state.settings
        || !settingsPermissions(state.session).canTestWebhook
      ) return false;
      state.settingsStatus = "webhook_test_running";
      render("settings");
      const result = await request(
        "/api/selector-probe/webhook-test",
        "POST",
        {
          idempotency_key: idempotencyKey("webhook-test", "synthetic"),
          payload: syntheticWebhookPayload(state.settings),
        },
      );
      if (result.status !== 202 && result.status !== 200) {
        state.settingsStatus = operationError(
          result,
          "webhook_test_unavailable",
        );
        render("settings");
        return false;
      }
      state.settingsStatus = safeCode(result.data?.status) || "accepted";
      render("settings");
      return true;
    }

    function upsertAccount(raw) {
      const account = sanitizeAccount(raw);
      if (account.id === null) return false;
      const index = state.accounts.items.findIndex(
        (item) => item.id === account.id,
      );
      if (index >= 0) state.accounts.items[index] = account;
      else state.accounts.items.push(account);
      return true;
    }

    async function loadAccounts() {
      if (state.session?.role !== "administrator") {
        state.accounts = {items: [], error: "forbidden", loading: false};
        render("settings");
        return false;
      }
      state.accounts.loading = true;
      state.accounts.error = "";
      render("settings");
      const result = await request("/api/admin/users", "GET");
      state.accounts.loading = false;
      if (result.status !== 200 || !Array.isArray(result.data?.users)) {
        state.accounts.error = operationError(
          result,
          "account_list_unavailable",
        );
        render("settings");
        return false;
      }
      state.accounts.items = result.data.users
        .map(sanitizeAccount)
        .filter((item) => item.id !== null);
      render("settings");
      return true;
    }

    function showTemporaryPassword(raw, account) {
      const password = typeof raw === "string" ? raw : "";
      if (!password) return false;
      state.temporaryCredential = {
        username: safeText(account?.username, 128),
        password,
      };
      render("temporary-password");
      return true;
    }

    function clearTemporaryPassword() {
      state.temporaryCredential = null;
      render("temporary-password");
      return true;
    }

    async function copyTemporaryPassword() {
      const password = state.temporaryCredential?.password;
      if (!password || typeof deps.copyText !== "function") return false;
      try {
        return await deps.copyText(password);
      } catch (_error) {
        return false;
      }
    }

    async function createAccount(username, role) {
      if (state.session?.role !== "administrator") return false;
      const selectedUsername = safeText(username, 128).trim();
      if (
        !selectedUsername
        || !["administrator", "operator"].includes(role)
      ) return false;
      const result = await request(
        "/api/admin/users",
        "POST",
        {username: selectedUsername, role},
      );
      if (result.status !== 201) {
        state.accounts.error = operationError(
          result,
          "account_create_failed",
        );
        render("settings");
        return false;
      }
      const account = sanitizeAccount(result.data?.user);
      upsertAccount(account);
      showTemporaryPassword(result.data?.temporary_password, account);
      return true;
    }

    async function updateAccount(userId, changes) {
      if (state.session?.role !== "administrator") return false;
      const id = Number(userId);
      const account = state.accounts.items.find((item) => item.id === id);
      if (!account) return false;
      const actions = accountActionModel(
        account,
        state.accounts.items,
        state.session,
      );
      const payload = {expected_revision: account.revision};
      if (typeof changes?.enabled === "boolean") {
        if (!changes.enabled && actions.disable.disabled) return false;
        if (changes.enabled && actions.enable.disabled) return false;
        payload.enabled = changes.enabled;
      }
      if (["administrator", "operator"].includes(changes?.role)) {
        if (
          changes.role === "operator"
          && actions.demote.disabled
        ) return false;
        if (
          changes.role === "administrator"
          && actions.promote.disabled
        ) return false;
        payload.role = changes.role;
      }
      if (Object.keys(payload).length === 1) return false;
      const result = await request(
        `/api/admin/users/${id}`,
        "PATCH",
        payload,
      );
      if (result.status !== 200) {
        state.accounts.error = operationError(
          result,
          "account_update_failed",
        );
        render("settings");
        return false;
      }
      upsertAccount(result.data?.user);
      render("settings");
      return true;
    }

    async function resetAccountPassword(userId) {
      if (state.session?.role !== "administrator") return false;
      const id = Number(userId);
      const account = state.accounts.items.find((item) => item.id === id);
      if (!account) return false;
      const result = await request(
        `/api/admin/users/${id}/reset-password`,
        "POST",
        {},
      );
      if (result.status !== 200) {
        state.accounts.error = operationError(
          result,
          "account_password_reset_failed",
        );
        render("settings");
        return false;
      }
      const updated = sanitizeAccount(result.data?.user);
      upsertAccount(updated);
      showTemporaryPassword(result.data?.temporary_password, updated);
      return true;
    }

    async function revokeAccountSessions(userId) {
      if (state.session?.role !== "administrator") return false;
      const id = Number(userId);
      const account = state.accounts.items.find((item) => item.id === id);
      if (!account) return false;
      const result = await request(
        `/api/admin/users/${id}/revoke-sessions`,
        "POST",
        {},
      );
      if (result.status !== 200) {
        state.accounts.error = operationError(
          result,
          "account_revoke_failed",
        );
        render("settings");
        return false;
      }
      upsertAccount(result.data?.user);
      render("settings");
      return true;
    }

    async function activateTab(tabId) {
      if (!TAB_IDS.has(tabId) && !AVAILABLE_ROUTES[tabId]) {
        throw new Error(`unknown selector probe tab: ${tabId}`);
      }
      state.activeTab = tabId;
      render("tab");
      const refreshed = await refreshCurrent();
      if (tabId === "settings" && state.session?.role === "administrator") {
        await loadAccounts();
      }
      return refreshed;
    }

    function documentIsVisible() {
      if (typeof deps.documentVisible === "function") {
        return deps.documentVisible();
      }
      if (typeof deps.isVisible === "function") return deps.isVisible();
      return true;
    }

    async function pollVisibleResources() {
      if (destroyed || !documentIsVisible()) return false;
      const resources = new Set(["overview", "gates", "alerts"]);
      if (state.activeTab === "managed") resources.add("elements");
      if (state.activeTab === "operations") {
        resources.add("runs");
        resources.add("versions");
      }
      if (AVAILABLE_ROUTES[state.activeTab]) resources.add(state.activeTab);
      await Promise.all(Array.from(resources).map((resource) => refresh(resource)));
      return true;
    }

    function startPolling() {
      if (pollTimer !== null || typeof deps.setInterval !== "function") return;
      pollTimer = deps.setInterval(pollVisibleResources, POLL_INTERVAL_MS);
      if (
        removeVisibilityListener === null
        && typeof deps.addVisibilityListener === "function"
      ) {
        removeVisibilityListener = deps.addVisibilityListener(async function () {
          if (documentIsVisible()) await pollVisibleResources();
        });
      }
    }

    function init() {
      if (initPromise) return initPromise;
      destroyed = false;
      initPromise = (async function () {
        if (!initialized) {
          initialized = true;
          render("shell");
        }
        try {
          const session = await deps.requestJson("/api/auth/session", "GET");
          state.session = session?.status === 200 ? clone(session.data) : null;
        } catch (_error) {
          state.session = null;
        }
        await refresh("overview");
        startPolling();
        return state;
      })().catch((error) => {
        initPromise = null;
        throw error;
      });
      return initPromise;
    }

    function destroy() {
      destroyed = true;
      stopPickerPolling();
      stopRunDetailPolling();
      state.selected = null;
      state.picker = null;
      state.pendingRebindId = "";
      state.operationWorkspace = null;
      state.temporaryCredential = null;
      state.settingsDraft = null;
      state.settingsDraftBaseRevision = null;
      state.settingsDraftStale = false;
      state.settingsStatus = "";
      pendingSettingsSecrets = {};
      pendingProfileAdds = [];
      state.settingsProfileAdds = [];
      if (pollTimer !== null && typeof deps.clearInterval === "function") {
        deps.clearInterval(pollTimer);
      }
      pollTimer = null;
      if (typeof removeVisibilityListener === "function") {
        removeVisibilityListener();
      }
      removeVisibilityListener = null;
      if (elementSearchTimer !== null && typeof deps.clearTimeout === "function") {
        deps.clearTimeout(elementSearchTimer);
      }
      elementSearchTimer = null;
      Array.from(state.pending.keys()).forEach(cancelPending);
      state.pending.clear();
      generations.clear();
      if (typeof deps.cleanupSensitiveUI === "function") {
        deps.cleanupSensitiveUI();
      }
    }

    function snapshot() {
      const value = {};
      Object.entries(state).forEach(([key, entry]) => {
        value[key] = key === "pending"
          ? Array.from(entry.keys())
          : clone(entry);
      });
      value.destroyed = destroyed;
      return value;
    }

    const api = {
      tabs: TABS.map((tab) => tab.id),
      state,
      init,
      activateTab,
      refreshCurrent,
      startPolling,
      pollVisibleResources,
      destroy,
      snapshot,
      acceptRevision,
      updateElementFilters,
      setElementPage,
      setElementPageSize,
      activateSummary,
      openLivePicker,
      openCollector: openLivePicker,
      beginElementRebind,
      startLivePicker,
      pollLivePicker,
      pollCollector: pollLivePicker,
      removeLivePickerSelection,
      setCollectorSelection,
      setCollectorName,
      setCollectorFilters,
      confirmLivePicker,
      confirmCollector: confirmLivePicker,
      cancelLivePicker,
      renameElement,
      rebindElement,
      deleteElement,
      openManagedElementDetail,
      closeOperationWorkspace,
      confirmManualGate,
      submitManualGate,
      openRunDetail,
      requestRunNow,
      requestRunRetry,
      openVersionDetail,
      confirmRollbackValidation,
      submitRollbackValidation,
      openAlertDetail,
      confirmAlertAction,
      submitAlertAction,
      acknowledgeAlert,
      stageSettingsDraft,
      reloadSettingsDraft,
      confirmSettingsSave,
      submitSettingsSave,
      stageProfileAdds,
      importSelectedProfiles,
      removeStagedProfile,
      stageProfileRemoval,
      confirmSecretClear,
      submitSecretClear,
      requestWebhookTest,
      loadAccounts,
      createAccount,
      updateAccount,
      resetAccountPassword,
      revokeAccountSessions,
      copyTemporaryPassword,
      clearTemporaryPassword,
    };
    return api;
  }

  function healthPresentation(overview) {
    if (!overview) return {kind: "unknown", text: "○ 状态未知"};
    const status = overview.health || overview.status;
    if (status === "healthy") return {kind: "healthy", text: "● 正常"};
    if (status === "warning") return {kind: "warning", text: "▲ 需关注"};
    if (status === "critical" || status === "failed") {
      return {kind: "critical", text: "■ 异常"};
    }
    if (overview.latest_run?.status === "failed") {
      return {kind: "critical", text: "■ 最近探针失败"};
    }
    if (overview.latest_run?.status === "completed") {
      return {kind: "healthy", text: "● 最近探针正常"};
    }
    if (overview.registry && overview.registry.available) {
      return {kind: "healthy", text: "● 注册表可用"};
    }
    return {kind: "warning", text: "▲ 尚无已发布版本"};
  }

  function setNodeText(document, id, value) {
    const node = document.querySelector(`#${id}`);
    if (node) node.textContent = String(value ?? "");
    return node;
  }

  function createTextElement(document, tagName, value, className) {
    const node = document.createElement(tagName);
    node.textContent = String(value ?? "");
    if (className) node.className = className;
    return node;
  }

  function displayTime(value, fallback) {
    return value ? String(value) : fallback;
  }

  function elementStatusText(item) {
    const published = {
      healthy: "● 正常",
      using_lkg: "▲ 使用上一稳定版",
      failed: "■ 失败",
      probe_unavailable: "◇ 探针不可用",
      disabled: "○ 已停用",
    };
    const draft = {
      draft: "✎ 草稿待验证",
      queued: "… 已排队",
      probing: "… 探测中",
      validating: "… 验证中",
    };
    const parts = [];
    if (published[item?.published_status]) {
      parts.push(published[item.published_status]);
    }
    if (item?.draft_status && draft[item.draft_status]) {
      parts.push(draft[item.draft_status]);
    }
    return parts.join(" / ") || "○ 未知";
  }

  function summaryButton(document, label, value, tab, filterName, filterValue) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "selector-summary-card";
    button.dataset.selectorSummaryTab = tab;
    if (filterName && filterValue) {
      button.dataset.selectorSummaryFilter = filterName;
      button.dataset.selectorSummaryValue = filterValue;
    }
    button.append(
      createTextElement(document, "span", label, "selector-summary-label"),
      createTextElement(document, "strong", value, "selector-summary-value"),
    );
    return button;
  }

  function overviewElements(overview) {
    if (Array.isArray(overview?.priority_elements)) {
      return overview.priority_elements;
    }
    if (Array.isArray(overview?.elements?.items)) return overview.elements.items;
    return [];
  }

  function renderOverview(document, state) {
    const overview = state?.overview || {};
    const health = healthPresentation(overview);
    const banner = setNodeText(
      document,
      "selector-probe-overview-health",
      `${health.text} · 当前发布状态`,
    );
    if (banner) banner.dataset.health = health.kind;
    const version = (
      overview.current_version
      || overview.active_version
      || overview.registry?.active_version
      || "尚无版本"
    );
    setNodeText(document, "selector-overview-version", version);
    setNodeText(
      document,
      "selector-overview-last-validation",
      displayTime(
        overview.last_successful_validation_at
        || overview.last_validation_at
        || overview.validation?.last_successful_at
        || (
          overview.latest_run?.status === "completed"
          ? overview.latest_run.finished_at
          : null
        ),
        "尚无成功验证",
      ),
    );
    setNodeText(
      document,
      "selector-overview-next-run",
      displayTime(overview.next_run_at, "每日 03:00 Asia/Shanghai"),
    );

    const elementCounts = overview.element_counts || overview.elements?.counts || {};
    setNodeText(
      document,
      "selector-overview-element-counts",
      `全部 ${elementCounts.all ?? elementCounts.total ?? 0} · 正常 ${elementCounts.healthy ?? 0} · LKG ${elementCounts.using_lkg ?? 0} · 失败 ${elementCounts.failed ?? 0}`,
    );
    const gates = overview.gate_counts || overview.gates || {};
    setNodeText(
      document,
      "selector-overview-gates",
      `自动暂停 ${gates.automatic ?? gates.probe ?? 0} · 人工暂停 ${gates.manual ?? 0}`,
    );
    const alerts = overview.alert_summary || overview.alerts || {};
    setNodeText(
      document,
      "selector-overview-alerts",
      `未关闭告警 ${alerts.open ?? alerts.unread ?? 0} · Webhook ${alerts.webhook_status || overview.webhook_status || "未知"}`,
    );

    const summaries = document.querySelector("#selector-overview-summaries");
    if (summaries) {
      summaries.replaceChildren(
        summaryButton(
          document,
          "当前版本",
          version,
          "versions",
          "status",
          "active",
        ),
        summaryButton(
          document,
          "动态元素",
          elementCounts.all ?? elementCounts.total ?? 0,
          "elements",
          "status",
          "all",
        ),
        summaryButton(
          document,
          "失败元素",
          elementCounts.failed ?? 0,
          "elements",
          "status",
          "failed",
        ),
        summaryButton(
          document,
          "策略闸门",
          (Number(gates.automatic ?? gates.probe ?? 0) + Number(gates.manual ?? 0)),
          "gates",
          "status",
          "paused",
        ),
        summaryButton(
          document,
          "未关闭告警",
          alerts.open ?? alerts.unread ?? 0,
          "alerts",
          "status",
          "open",
        ),
      );
    }

    const priority = document.querySelector("#selector-overview-priority");
    if (priority) {
      priority.replaceChildren(
        ...selectOverviewElements(overviewElements(overview)).map((item) => {
          const row = document.createElement("article");
          row.className = "selector-overview-list-item";
          row.append(
            createTextElement(
              document,
              "strong",
              item.display_name || item.id || "未命名元素",
            ),
            createTextElement(
              document,
              "span",
              `${elementStatusText(item)} · ${displayTime(item.last_validated_at, "尚未验证")}`,
              "muted",
            ),
          );
          return row;
        }),
      );
    }

    const events = document.querySelector("#selector-overview-events");
    if (events) {
      const safeEvents = Array.isArray(overview.recent_events)
        ? overview.recent_events.slice(0, 10)
        : [];
      events.replaceChildren(
        ...safeEvents.map((event) => {
          const row = document.createElement("article");
          row.className = "selector-overview-list-item";
          row.append(
            createTextElement(document, "strong", event.type || "事件"),
            createTextElement(document, "span", event.summary || "状态已更新"),
            createTextElement(
              document,
              "time",
              displayTime(event.occurred_at || event.created_at, "时间未知"),
              "muted",
            ),
          );
          return row;
        }),
      );
    }
  }

  function renderElementCounts(document, state) {
    const container = document.querySelector("#selector-element-counts");
    if (!container) return;
    const counts = state.elements?.counts || state.overview?.element_counts || {};
    const specs = [
      ["全部", "all", counts.all ?? counts.total ?? state.elements?.total ?? 0],
      ["正常", "healthy", counts.healthy ?? 0],
      ["使用 LKG", "using_lkg", counts.using_lkg ?? 0],
      ["草稿待验证", "draft", counts.draft ?? 0],
      ["失败", "failed", counts.failed ?? 0],
      ["已停用", "disabled", counts.disabled ?? 0],
    ];
    container.replaceChildren(
      ...specs.map(([label, status, count]) => (
        summaryButton(
          document,
          label,
          count,
          "elements",
          "status",
          status,
        )
      )),
    );
  }

  function renderElementDirectory(document, state) {
    const elements = state?.elements || listState(true);
    const addButton = document.querySelector("#selector-element-add");
    if (addButton) addButton.hidden = !canCreateElement(state?.session);
    const pickerButton = document.querySelector("#selector-element-picker");
    if (pickerButton) pickerButton.hidden = !canCreateElement(state?.session);
    renderElementCounts(document, state || {});

    const rows = document.querySelector("#selector-element-rows");
    if (rows) {
      rows.replaceChildren(
        ...(elements.items || []).map((item) => {
          const row = document.createElement("tr");
          const identity = document.createElement("td");
          identity.append(
            createTextElement(document, "strong", item.display_name || item.id),
            createTextElement(document, "span", item.scope || "page", "muted"),
          );
          const status = createTextElement(
            document,
            "td",
            elementStatusText(item),
          );
          const locator = createTextElement(
            document,
            "td",
            item.primary_locator_type || "尚未生成",
          );
          const dependencies = createTextElement(
            document,
            "td",
            item.dependency_count ?? 0,
          );
          const validated = createTextElement(
            document,
            "td",
            displayTime(item.last_validated_at, "尚未验证"),
          );
          const actions = document.createElement("td");
          const open = createTextElement(document, "button", "查看");
          open.type = "button";
          open.className = "secondary";
          open.dataset.selectorElementId = item.id;
          actions.append(open);
          if (
            item.migration_available === true
            && canCreateElement(state?.session)
          ) {
            const migrate = createTextElement(
              document,
              "button",
              "加入自动管理",
            );
            migrate.type = "button";
            migrate.className = "secondary";
            migrate.dataset.selectorMigrateId = item.id;
            actions.append(migrate);
          }
          row.append(identity, status, locator, dependencies, validated, actions);
          return row;
        }),
      );
    }

    const pageCount = Math.max(
      Math.ceil((elements.total || 0) / (elements.pageSize || 20)),
      1,
    );
    setNodeText(
      document,
      "selector-element-page-meta",
      `第 ${elements.page || 1} / ${pageCount} 页，共 ${elements.total || 0} 项`,
    );
    const previous = document.querySelector("#selector-element-prev");
    const next = document.querySelector("#selector-element-next");
    if (previous) previous.disabled = (elements.page || 1) <= 1;
    if (next) next.disabled = (elements.page || 1) >= pageCount;
    const size = document.querySelector("#selector-element-page-size");
    if (size) size.value = String(elements.pageSize || 20);

    const form = document.querySelector("#selector-element-filters");
    const filters = Object.assign({}, ELEMENT_FILTER_DEFAULTS, elements.filters || {});
    if (form?.elements && typeof form.elements.namedItem === "function") {
      for (const name of ["search", "status", "source", "scope"]) {
        const control = form.elements.namedItem(name);
        if (control) control.value = filters[name];
      }
      const dependency = (
        form.elements.namedItem("dependency")
        || form.elements.namedItem("referenced")
      );
      if (dependency) dependency.value = filters.referenced;
    }
  }

  function renderLivePicker(document, state) {
    const picker = state?.picker;
    const dialog = document.querySelector("#selector-element-picker-dialog");
    openDialogForState(dialog, Boolean(picker?.open));
    if (!picker?.open) return;
    const profile = document.querySelector("#selector-picker-profile");
    if (profile) {
      const current = picker.profileRef || profile.value;
      profile.replaceChildren(...(picker.profiles || []).map((item) => {
        const option = document.createElement("option");
        option.value = item.profile_ref;
        option.textContent = `${item.profile_mask || "***"} · ${item.status || "unknown"}`;
        return option;
      }));
      profile.value = current || picker.profiles?.[0]?.profile_ref || "";
      profile.disabled = Boolean(picker.session) || picker.loading === true;
    }
    const pageState = document.querySelector("#selector-picker-page-state");
    if (pageState) {
      pageState.value = picker.pageState || "feed_ready";
      pageState.disabled = Boolean(picker.session) || picker.loading === true;
    }
    const session = picker.session;
    const labels = {
      starting: "正在准备测试环境",
      ready: "页面已就绪，可以点击元素",
      selecting: "正在选择元素",
      confirmed: "已确认，正在清理",
      cancelled: "已取消",
      expired: "会话已超时",
      failed: "拾取失败",
    };
    setNodeText(
      document,
      "selector-picker-status",
      session ? (labels[session.status] || session.status) : (
        picker.loading ? "正在读取测试 Profile" : "请选择 Profile 和页面状态"
      ),
    );
    const inventory = session?.inventory || [];
    const selectedIds = new Set(picker.selectedIds || []);
    const selections = inventory.filter(
      (item) => selectedIds.has(item.selection_id),
    );
    setNodeText(
      document,
      "selector-picker-count",
      `${inventory.length} 个候选 · 已选 ${selections.length} / ${session?.max_selections || 20}`,
    );
    const inventoryList = document.querySelector("#selector-inventory-list");
    inventoryUI?.renderInventory(document, inventoryList, inventory, {
      filters: picker.filters,
      selectedIds: picker.selectedIds,
      names: picker.names,
    });
    const recordedSteps = document.querySelector("#selector-inventory-recorded-steps");
    recordedSteps?.replaceChildren(...(
      session?.recorded_steps?.length
        ? session.recorded_steps.map((step) => createTextElement(
          document,
          "article",
          `${step.sequence}. ${locatorDescription(step.locator || {})}`,
          "selector-detail-row",
        ))
        : [createTextElement(document, "p", "尚未记录点击步骤", "muted")]
    ));
    const locatorRows = document.querySelector("#selector-inventory-locators");
    locatorRows?.replaceChildren(...(
      selections.flatMap((item) => item.locators.map((locator) => createTextElement(
        document,
        "code",
        `${item.name || item.tag}: ${locator.type.toUpperCase()} ${locator.value}`,
        "selector-detail-row",
      )))
    ));
    setNodeText(
      document,
      "selector-inventory-selected-summary",
      selections.length
        ? `将保存 ${selections.length} 个元素；当前窗口已完成路径 Dry-Run。`
        : "勾选元素并填写自定义名称。",
    );
    const list = document.querySelector("#selector-picker-selections");
    if (list) {
      list.replaceChildren(...(
        selections.length
          ? selections.map((item, index) => {
            const row = document.createElement("article");
            row.className = "selector-picker-selection";
            const copy = document.createElement("div");
            copy.append(
              createTextElement(
                document,
                "strong",
                item.name || item.attributes?.["data-e2e"] || item.role || `元素 ${index + 1}`,
              ),
              createTextElement(
                document,
                "span",
                `${item.role || item.tag || "control"} · ${item.scope || "page"}`,
                "muted",
              ),
              createTextElement(
                document,
                "code",
                item.recommended_locators?.[0]
                  ? JSON.stringify(item.recommended_locators[0])
                  : "等待生成 Locator",
              ),
            );
            const remove = createTextElement(document, "button", "移除");
            remove.type = "button";
            remove.className = "secondary";
            remove.dataset.selectorPickerRemove = item.selection_id;
            row.append(copy, remove);
            return row;
          })
          : [createTextElement(
            document,
            "p",
            session ? "请在 AdsPower 窗口中点击要管理的元素" : "尚未启动",
            "muted",
          )]
      ));
    }
    const start = document.querySelector("#selector-picker-start");
    if (start) {
      start.hidden = Boolean(session);
      start.disabled = picker.loading === true || !(picker.profiles || []).length;
    }
    const confirm = document.querySelector("#selector-picker-confirm");
    if (confirm) {
      confirm.hidden = !session;
      confirm.disabled = (
        picker.loading === true
        || !["ready", "selecting"].includes(session?.status)
        || !selections.length
        || selections.some((item) => !safeText(picker.names[item.selection_id], 120))
      );
    }
    setNodeText(
      document,
      "selector-picker-error",
      picker.error || session?.failure_code || "",
    );
  }

  function locatorDescription(locator) {
    if (locator.type === "role") {
      return `Role ${locator.role || "未知"} · Name ${locator.name || "未设置"} · ${locator.name_mode || "exact"}`;
    }
    if (locator.type === "attribute") {
      return `属性 ${locator.name || "未知"}=${locator.value || "未设置"}`;
    }
    return `${String(locator.type || "locator").toUpperCase()} ${locator.value || "未设置"}`;
  }

  function operationSummary(document, label, value) {
    const card = document.createElement("article");
    card.className = "selector-summary-card";
    card.append(
      createTextElement(document, "span", label, "selector-summary-label"),
      createTextElement(document, "strong", value, "selector-summary-value"),
    );
    return card;
  }

  function renderGates(document, state) {
    const gates = (state?.gates?.items || []).map(sanitizeGate);
    const counts = {
      automatic: gates.filter((gate) => gate.reasons.some(
        (reason) => reason.source === "probe",
      )).length,
      manual: gates.filter((gate) => gate.reasons.some(
        (reason) => reason.source === "manual",
      )).length,
      healthy: gates.filter((gate) => gate.effective_status === "active").length,
      unmanaged: gates.filter((gate) => !gate.managed).length,
    };
    const summary = document.querySelector("#selector-gate-counts");
    summary?.replaceChildren(
      operationSummary(document, "自动暂停", counts.automatic),
      operationSummary(document, "人工暂停", counts.manual),
      operationSummary(document, "健康策略", counts.healthy),
      operationSummary(document, "未纳管", counts.unmanaged),
    );
    const rows = document.querySelector("#selector-gate-rows");
    if (rows) {
      rows.replaceChildren(
        ...(gates.length ? gates.map((gate) => {
          const row = document.createElement("article");
          row.className = "selector-operation-row";
          row.dataset.strategyId = gate.strategy_id;
          row.append(
            createTextElement(
              document,
              "strong",
              gate.strategy_name || gate.strategy_id || "未知策略",
            ),
            createTextElement(
              document,
              "span",
              `${gate.effective_status} · revision ${gate.revision}`,
              "muted",
            ),
            createTextElement(
              document,
              "span",
              `aliases ${gate.aliases.join(", ") || "无"}`
              + ` · version ${gate.selector_version_id || "无"}`
              + ` · actions ${gate.affected_action_ids.join(", ") || "无"}`
              + ` · changed ${gate.changed_at || "未知"}`
              + ` · actor ${gate.actor || "系统"}`,
              "muted",
            ),
          );
          const reasons = document.createElement("div");
          reasons.className = "selector-operation-stack";
          reasons.append(
            ...(gate.reasons.length ? gate.reasons.map((reason) => (
              createTextElement(
                document,
                "span",
                `${reason.source} · ${reason.reason_code}`
                + `${reason.aliases.length ? ` · ${reason.aliases.join(", ")}` : ""}`
                + `${reason.selector_version_id ? ` · ${reason.selector_version_id}` : ""}`
                + `${reason.affected_action_ids.length ? ` · actions ${reason.affected_action_ids.join(", ")}` : ""}`
                + `${reason.created_at ? ` · ${reason.created_at}` : ""}`
                + `${reason.actor ? ` · ${reason.actor}` : ""}`,
              )
            )) : [createTextElement(document, "span", "无生效原因", "muted")]),
          );
          row.append(reasons);
          if (state?.session?.role === "administrator" && gate.managed) {
            const hasManual = gate.reasons.some(
              (reason) => reason.source === "manual",
            );
            const action = document.createElement("button");
            action.type = "button";
            action.dataset.gateAction = hasManual ? "resume" : "pause";
            action.dataset.gateStrategyId = gate.strategy_id;
            if (!hasManual) action.className = "danger";
            action.textContent = hasManual ? "人工恢复" : "人工暂停";
            row.append(action);
          }
          return row;
        }) : [
          createTextElement(
            document,
            "article",
            "暂无策略闸门记录",
            "selector-probe-empty",
          ),
        ]),
      );
    }
    setNodeText(
      document,
      "selector-gate-status",
      gates.length ? `共 ${gates.length} 个策略` : "",
    );
  }

  function operationStateText(label, value) {
    const state = safeOperationState(value);
    return `${label}: ${state.status}`
      + `${state.duration_ms !== null ? ` · ${state.duration_ms}ms` : ""}`
      + `${state.attempt_count ? ` · ${state.attempt_count} 次` : ""}`;
  }

  function runSummaryLines(raw) {
    const presentation = buildRunPresentation(raw);
    const lines = [
      `状态：${presentation.statusLabel}`,
      `当前步骤：${presentation.currentStage.title}`,
      `进度：${presentation.completedStages}/5`,
      `结果：${presentation.result}`,
    ];
    if (presentation.failure) {
      lines.push(
        `失败原因：${presentation.failure.reason}`,
        `影响范围：${presentation.failure.impact}`,
        `系统下一步：${presentation.failure.nextAction}`,
      );
    }
    return lines;
  }

  function renderRuns(document, state) {
    const runs = (state?.runs?.items || []).map(sanitizeRun);
    const rows = document.querySelector("#selector-run-rows");
    if (rows) {
      rows.replaceChildren(
        ...(runs.length ? runs.map((run) => {
          const presentation = buildRunPresentation(run);
          const row = document.createElement("article");
          row.className = "selector-operation-row";
          row.dataset.runId = run.id;
          row.append(
            createTextElement(
              document,
              "strong",
              `运行 ${run.id || "未编号"}`,
            ),
            createTextElement(
              document,
              "span",
              `状态：${presentation.statusLabel}`,
              "muted",
            ),
            createTextElement(
              document,
              "span",
              `当前步骤：${presentation.currentStage.title}`,
              "muted",
            ),
            createTextElement(
              document,
              "span",
              `进度 ${presentation.completedStages}/5 · ${presentation.result}`,
              "muted",
            ),
          );
          const actions = document.createElement("div");
          actions.className = "selector-operation-actions";
          const detail = document.createElement("button");
          detail.type = "button";
          detail.dataset.runDetailId = run.id;
          detail.textContent = "查看运行";
          actions.append(detail);
          if (
            ["infrastructure_failed", "failed"].includes(run.status)
            && (run.retry_delay_minutes || run.next_retry_at)
          ) {
            const retry = document.createElement("button");
            retry.type = "button";
            retry.dataset.runRetryId = run.id;
            retry.textContent = "重新探测";
            actions.append(retry);
          }
          row.append(actions);
          return row;
        }) : [
          createTextElement(
            document,
            "article",
            "暂无探针运行记录",
            "selector-probe-empty",
          ),
        ]),
      );
    }
    setNodeText(
      document,
      "selector-run-status",
      runs.length ? `共 ${runs.length} 次运行` : "",
    );
  }

  function versionLifecycleText(version) {
    if (version.is_active) return "Active";
    if (version.is_lkg) return "LKG";
    const labels = {
      validated: "已验证",
      validated_pending: "待发布",
      pending: "待发布",
      superseded: "已取代",
      failed: "失败",
      conflict: "冲突",
    };
    return labels[version.status] || version.status || "未知";
  }

  function versionDescription(version) {
    return [
      `base ${version.base_version_id || "无"}`,
      `hash ${version.bundle_hash || "无"}`,
      `created ${version.created_at || "未知"} · validated ${version.validated_at || "未知"} · published ${version.published_at || "未发布"}`,
      `changed=${version.changed_elements.length} · dependencies=${version.dependencies.length}`,
      operationStateText("SQLite", version.sqlite),
      operationStateText("outbox", version.outbox),
      operationStateText("Redis", version.redis),
      operationStateText("Lua", version.lua),
      operationStateText("reconcile", version.reconciliation),
    ];
  }

  function renderVersions(document, state) {
    const versions = (state?.versions?.items || []).map(sanitizeVersion);
    const rows = document.querySelector("#selector-version-rows");
    if (rows) {
      rows.replaceChildren(
        ...(versions.length ? versions.map((version) => {
          const row = document.createElement("article");
          row.className = "selector-operation-row";
          row.dataset.versionId = version.id;
          row.append(
            createTextElement(
              document,
              "strong",
              `${version.id || "未知版本"} · ${versionLifecycleText(version)}`,
            ),
            ...versionDescription(version).map((line) => (
              createTextElement(document, "span", line, "muted")
            )),
          );
          const actions = document.createElement("div");
          actions.className = "selector-operation-actions";
          const detail = document.createElement("button");
          detail.type = "button";
          detail.dataset.versionDetailId = version.id;
          detail.textContent = "查看版本";
          actions.append(detail);
          if (state?.session?.role === "administrator") {
            versionActions(version).forEach((item) => {
              const action = document.createElement("button");
              action.type = "button";
              action.dataset.versionAction = item.id;
              action.dataset.versionId = version.id;
              action.textContent = item.label;
              actions.append(action);
            });
          }
          row.append(actions);
          return row;
        }) : [
          createTextElement(
            document,
            "article",
            "暂无选择器版本",
            "selector-probe-empty",
          ),
        ]),
      );
    }
    setNodeText(
      document,
      "selector-version-status",
      versions.length ? `共 ${versions.length} 个版本` : "",
    );
  }

  function alertDescription(alert) {
    return [
      `${alert.severity} · ${alert.failure_class || "unknown"} · occurrences=${alert.occurrence_count}`,
      `aliases ${alert.aliases.join(", ") || "无"}`,
      `strategies ${alert.strategy_ids.join(", ") || "无"}`,
      `Active ${alert.active_version || "无"} · LKG ${alert.lkg_version || "无"}`,
      alert.retries.length
        ? `retries ${alert.retries.map((item) => (
          `${item.delay_minutes || "?"}m ${item.status}`
        )).join(" / ")}`
        : "retries 无",
      `Webhook ${alert.webhook.status} · attempts=${alert.webhook.attempt_count}`,
      alert.gate_active ? "底层策略闸门仍生效" : "底层策略闸门未生效",
    ];
  }

  function renderAlerts(document, state) {
    const alerts = (state?.alerts?.items || []).map(sanitizeAlert);
    const summary = document.querySelector("#selector-alert-counts");
    summary?.replaceChildren(
      operationSummary(
        document,
        "Open",
        alerts.filter((item) => item.status === "open").length,
      ),
      operationSummary(
        document,
        "Acknowledged",
        alerts.filter((item) => item.status === "acknowledged").length,
      ),
      operationSummary(
        document,
        "Resolved",
        alerts.filter((item) => item.status === "resolved").length,
      ),
    );
    const rows = document.querySelector("#selector-alert-rows");
    if (rows) {
      rows.replaceChildren(
        ...(alerts.length ? alerts.map((alert) => {
          const row = document.createElement("article");
          row.className = "selector-operation-row";
          row.dataset.alertId = String(alert.id ?? "");
          row.append(
            createTextElement(
              document,
              "strong",
              `#${alert.id ?? "?"} · ${alert.status}`,
            ),
            ...alertDescription(alert).map((line) => (
              createTextElement(document, "span", line, "muted")
            )),
          );
          const actions = document.createElement("div");
          actions.className = "selector-operation-actions";
          const detail = document.createElement("button");
          detail.type = "button";
          detail.dataset.alertDetailId = String(alert.id ?? "");
          detail.textContent = "查看告警";
          actions.append(detail);
          const model = alertActionModel(alert);
          if (
            ["administrator", "operator"].includes(state?.session?.role)
            && !model.acknowledge.disabled
          ) {
            const acknowledge = document.createElement("button");
            acknowledge.type = "button";
            acknowledge.dataset.alertAction = "acknowledge";
            acknowledge.dataset.alertId = String(alert.id ?? "");
            acknowledge.textContent = model.acknowledge.label;
            actions.append(acknowledge);
          }
          if (state?.session?.role === "administrator") {
            const resolve = document.createElement("button");
            resolve.type = "button";
            resolve.dataset.alertAction = "resolve";
            resolve.dataset.alertId = String(alert.id ?? "");
            resolve.className = "danger";
            resolve.textContent = model.resolve.label;
            resolve.disabled = model.resolve.disabled;
            actions.append(resolve);
          }
          row.append(actions);
          return row;
        }) : [
          createTextElement(
            document,
            "article",
            "暂无告警",
            "selector-probe-empty",
          ),
        ]),
      );
    }
    setNodeText(
      document,
      "selector-alert-status",
      alerts.length ? `共 ${alerts.length} 条告警` : "",
    );
  }

  function renderSettings(document, state) {
    const settings = sanitizeSettings(state?.settingsDraft || state?.settings);
    const permissions = settingsPermissions(state?.session);
    replaceSimpleRows(document, "#selector-settings-basic", [
      `enabled: ${settings.enabled}`,
      `rollout: ${settings.rollout_mode}`,
      `schedule: ${settings.schedule_time} ${settings.timezone}`,
      `Origin: ${settings.target_origin || "未设置"}`,
      `页面等待: ${settings.page_timeout_seconds} 秒`,
      `retry: ${settings.retry_policy.delays_minutes.join("/")} 分钟`,
      `freshness: ${settings.freshness_hours} 小时`,
    ]);
    const profileRows = document.querySelector("#selector-settings-profiles");
    if (profileRows) {
      profileRows.replaceChildren(
        ...(settings.profiles.length ? settings.profiles.map((profile) => {
          const row = document.createElement("article");
          row.className = "selector-detail-row";
          row.append(
            createTextElement(
              document,
              "span",
              `${profile.profile_mask} · dedicated=${profile.dedicated_test}`
              + ` · ${profile.status}`
              + `${profile.last_checked_at ? ` · ${profile.last_checked_at}` : ""}`,
            ),
          );
          if (permissions.canEdit) {
            const remove = document.createElement("button");
            remove.type = "button";
            remove.dataset.settingsProfileRemove = profile.profile_ref;
            remove.textContent = "移除";
            remove.disabled = settings.profiles.length <= 2;
            row.append(remove);
          }
          return row;
        }) : [
          createTextElement(
            document,
            "article",
            "暂无脱敏测试 Profile",
            "selector-detail-row",
          ),
        ]),
      );
    }
    const stagedProfileRows = document.querySelector(
      "#selector-settings-profile-staged",
    );
    if (stagedProfileRows) {
      const stagedProfiles = Array.isArray(state?.settingsProfileAdds)
        ? state.settingsProfileAdds
        : [];
      stagedProfileRows.replaceChildren(
        ...stagedProfiles.map((profile) => {
          const row = document.createElement("article");
          row.className = "selector-detail-row";
          row.append(createTextElement(
            document,
            "span",
            `${profile.profile_mask} · 待保存`,
          ));
          if (permissions.canEdit) {
            const remove = document.createElement("button");
            remove.type = "button";
            remove.dataset.settingsProfileStageRemove = String(profile.index);
            remove.textContent = "移除";
            row.append(remove);
          }
          return row;
        }),
      );
    }
    replaceSimpleRows(document, "#selector-settings-redis", [
      `${settings.redis.status} · namespace ${settings.redis.namespace || "未设置"}`,
      `AOF: ${settings.redis.aof_enabled ? "通过" : "未确认"}`,
      `eviction: ${settings.redis.eviction_policy || "未确认"}`,
      `password: ${settings.redis.password_set ? "已设置" : "未设置"}`,
      `last reconcile: ${settings.redis.last_reconciled_at || "无"}`,
    ]);
    replaceSimpleRows(document, "#selector-settings-webhook", [
      `enabled: ${settings.webhook.enabled}`,
      `${settings.webhook.type || "unknown"} · ${settings.webhook.url_display || "未设置"}`,
      `signing secret: ${settings.webhook.signing_secret_set ? "已设置" : "未设置"}`,
      `timeout: ${settings.webhook.timeout_seconds}s · retry ${settings.webhook.retry_policy || "未设置"}`,
      `delivery: ${settings.webhook.status} · ${settings.webhook.last_delivery_at || "无"}`,
    ]);
    replaceSimpleRows(document, "#selector-settings-permissions", [
      "administrator: 读取、设置、秘密清除、账户管理、Webhook 测试",
      "operator: 只读设置、立即探测、确认告警、Webhook synthetic 测试",
      "后端权限策略为最终依据。",
    ]);
    const save = document.querySelector("#selector-settings-save");
    const reload = document.querySelector("#selector-settings-reload");
    const webhookTest = document.querySelector(
      "#selector-settings-webhook-test",
    );
    if (save) save.hidden = !permissions.canEdit;
    const obsoleteCheck = document.querySelector("#selector-settings-preflight");
    if (obsoleteCheck) obsoleteCheck.hidden = true;
    if (reload) {
      reload.hidden = !permissions.canEdit || state?.settingsDraftStale !== true;
    }
    if (webhookTest) webhookTest.hidden = !permissions.canTestWebhook;
    const settingsStatus = settingsStatusText(state?.settingsStatus || "");
    setNodeText(document, "selector-settings-status", settingsStatus);
    setNodeText(document, "selector-settings-save-status", settingsStatus);
    document.querySelectorAll("[data-selector-settings-editable]").forEach(
      (control) => {
        control.disabled = !permissions.canEdit;
      },
    );
    const form = document.querySelector("#selector-settings-form");
    if (form?.elements) {
      if (
        state?.settingsStatus === "settings_saved"
        && form.dataset.settingsRevision !== String(settings.revision)
      ) {
        form.dataset.dirty = "false";
        for (const name of [
          "redisPassword",
          "webhookSigningSecret",
          "webhookUrl",
          "profileAdd",
          "reason",
        ]) {
          const sensitive = form.elements.namedItem(name);
          if (sensitive) sensitive.value = "";
        }
      }
      if (form.dataset.dirty !== "true") {
        const setValue = (name, value) => {
          const control = form.elements.namedItem(name);
          if (control) control.value = String(value ?? "");
        };
        const setChecked = (name, value) => {
          const control = form.elements.namedItem(name);
          if (control) control.checked = value === true;
        };
        setChecked("enabled", settings.enabled);
        setValue("rolloutMode", settings.rollout_mode);
        setValue("scheduleTime", settings.schedule_time);
        setValue("targetOrigin", settings.target_origin);
        setValue("pageTimeoutSeconds", settings.page_timeout_seconds);
        setValue("freshnessHours", settings.freshness_hours);
        setValue("redisNamespace", settings.redis.namespace);
        setChecked("webhookEnabled", settings.webhook.enabled);
        setValue("webhookType", settings.webhook.type || "generic");
        form.dataset.settingsRevision = String(settings.revision);
      }
    }
  }

  function renderAccountManagement(document, state) {
    const permissions = settingsPermissions(state?.session);
    const add = document.querySelector("#selector-account-add");
    if (add) add.hidden = !permissions.canManageAccounts;
    const rows = document.querySelector("#selector-account-rows");
    if (!rows) return;
    if (!permissions.canManageAccounts) {
      rows.replaceChildren();
      setNodeText(document, "selector-account-status", "仅管理员可管理账户");
      return;
    }
    const accounts = (state?.accounts?.items || []).map(sanitizeAccount);
    rows.replaceChildren(
      ...(accounts.length ? accounts.map((account) => {
        const row = document.createElement("article");
        row.className = "selector-operation-row";
        row.dataset.accountId = String(account.id ?? "");
        row.append(
          createTextElement(
            document,
            "strong",
            `${account.username || "未知账户"} · ${account.role}`,
          ),
          createTextElement(
            document,
            "span",
            `${account.enabled ? "enabled" : "disabled"}`
            + ` · revision ${account.revision}`
            + ` · last login ${account.last_login_at || "无"}`
            + ` · must change password ${account.must_change_password}`,
            "muted",
          ),
        );
        const model = accountActionModel(account, accounts, state.session);
        const actions = document.createElement("div");
        actions.className = "selector-operation-actions";
        const specs = [
          [
            account.enabled ? "disable" : "enable",
            account.enabled ? "禁用" : "启用",
            account.enabled ? model.disable.disabled : model.enable.disabled,
          ],
          [
            account.role === "administrator" ? "demote" : "promote",
            account.role === "administrator" ? "降级为操作员" : "提升为管理员",
            account.role === "administrator"
              ? model.demote.disabled
              : model.promote.disabled,
          ],
          ["reset-password", "重置密码", model.resetPassword.disabled],
          ["revoke-sessions", "撤销会话", model.revokeSessions.disabled],
        ];
        specs.forEach(([action, label, disabled]) => {
          const button = document.createElement("button");
          button.type = "button";
          button.dataset.accountAction = action;
          button.dataset.accountId = String(account.id ?? "");
          button.textContent = label;
          button.disabled = disabled;
          actions.append(button);
        });
        row.append(actions);
        return row;
      }) : [
        createTextElement(
          document,
          "article",
          "暂无管理账户",
          "selector-probe-empty",
        ),
      ]),
    );
    setNodeText(
      document,
      "selector-account-status",
      state?.accounts?.error
      || (state?.accounts?.loading ? "正在加载账户" : `共 ${accounts.length} 个账户`),
    );
  }

  function replaceSimpleRows(document, selector, values) {
    const container = document.querySelector(selector);
    if (!container) return;
    container.replaceChildren(
      ...(values || []).map((value) => {
        const row = document.createElement("article");
        row.className = "selector-detail-row";
        row.textContent = String(value);
        return row;
      }),
    );
  }

  function openDialogForState(dialog, showing) {
    if (!dialog) return;
    if (showing && !dialog.open && typeof dialog.showModal === "function") {
      dialog.showModal();
    } else if (!showing && dialog.open && typeof dialog.close === "function") {
      dialog.close();
    }
  }

  function operationDetailLines(workspace) {
    const detail = workspace?.detail;
    if (workspace?.kind === "element-detail") {
      const element = sanitizeElementDetail(detail);
      const definition = element.definition || {};
      return [
        `名称：${element.display_name || element.id}`,
        `状态：${inventoryUI?.elementStatusText(element.status) || element.status || "未知"}`,
        `页面：${definition.page_key || element.page_key || "未知"}`,
        `路径：${(definition.locators || []).map(
          (item) => `${item.type.toUpperCase()} ${item.value}`,
        ).join("；") || "暂无"}`,
        `操作步骤：${definition.operation_steps?.length || 0}`,
        `策略依赖：${element.dependencies.map(
          (item) => item.strategy_name || item.strategy_id,
        ).join("、") || "无"}`,
        `最近验证：${element.last_validated_at || "无"}`,
        `历史版本：${element.history.length}`,
      ];
    }
    if (workspace?.kind === "run-detail") {
      return runSummaryLines(detail);
    }
    if (workspace?.kind === "version-detail") {
      const version = sanitizeVersion(detail);
      return versionDescription(version).concat(
        version.changed_elements.map(
          (item) => `diff ${item.alias} · ${item.change || "unknown"} · ${item.from_version || "无"} → ${item.to_version || "无"}`,
        ),
        version.dependencies.map(
          (item) => `dependency ${item.strategy_name || item.strategy_id} · aliases ${item.aliases.join(", ") || "无"} · actions ${item.action_ids.join(", ") || "无"}`,
        ),
        version.evidence.map(
          (round) => `evidence ${round.profile_mask} · 第 ${round.round} 轮 · ${round.status}`,
        ),
      );
    }
    if (
      workspace?.kind === "alert-detail"
      || workspace?.kind === "alert-result"
    ) {
      const alert = sanitizeAlert(detail);
      return alertDescription(alert).concat(
        alert.timeline.map(
          (item) => `timeline ${item.event || "unknown"} · ${item.occurred_at || "未知"} · ${item.actor || "系统"}`,
        ),
      );
    }
    if (workspace?.kind === "gate-result") {
      const gate = sanitizeGate(detail);
      return [
        `${gate.strategy_id} · ${gate.effective_status} · revision ${gate.revision}`,
        ...(gate.reasons.length ? gate.reasons.map(
          (reason) => `${reason.source} · ${reason.reason_code}`,
        ) : ["无生效原因"]),
        workspace.outcome || "",
      ].filter(Boolean);
    }
    if (workspace?.kind === "rollback-result") {
      return [
        `新草稿 ${workspace.draftVersion || "未返回"}`,
        `请求 ${workspace.requestId || "未返回"}`,
        "历史版本未被直接激活；新草稿必须重新完成双 Profile 双轮验证。",
      ];
    }
    if (workspace?.kind === "settings-result") {
      const settings = sanitizeSettings(detail);
      return [
        `revision ${settings.revision}`,
        `enabled ${settings.enabled} · rollout ${settings.rollout_mode}`,
        `schedule ${settings.schedule_time} ${settings.timezone}`,
        `Origin ${settings.target_origin || "未设置"}`,
        `页面等待 ${settings.page_timeout_seconds} 秒`,
        `Profiles ${settings.profiles.map((item) => item.profile_mask).join(", ") || "无"}`,
        `Redis ${settings.redis.status} · AOF ${settings.redis.aof_enabled} · ${settings.redis.eviction_policy || "未确认"}`,
        `Webhook ${settings.webhook.enabled} · ${settings.webhook.status}`,
        workspace.outcome || "",
      ].filter(Boolean);
    }
    return [];
  }

  function renderRunStageCards(document, container, presentation) {
    if (!container) return;
    container.replaceChildren(...presentation.stages.map((stage, index) => {
      const card = document.createElement("article");
      card.className = "selector-run-stage-card";
      card.dataset.stageStatus = stage.status;
      card.append(
        createTextElement(document, "span", `步骤 ${index + 1}`, "muted"),
        createTextElement(document, "strong", stage.title),
        createTextElement(document, "span", stage.purpose, "muted"),
        createTextElement(
          document,
          "span",
          stage.statusLabel,
          `selector-run-stage-status is-${stage.status}`,
        ),
        createTextElement(document, "span", stage.result),
      );
      return card;
    }));
  }

  function renderOperationWorkspace(document, state) {
    const workspace = state?.operationWorkspace;
    const confirmationKinds = new Set([
      "gate-confirm",
      "rollback-confirm",
      "alert-confirm",
      "settings-confirm",
      "secret-clear-confirm",
    ]);
    const detailKinds = new Set([
      "element-detail",
      "gate-result",
      "run-detail",
      "version-detail",
      "rollback-result",
      "alert-detail",
      "alert-result",
      "settings-result",
    ]);
    const confirmDialog = document.querySelector(
      "#selector-operation-confirm-dialog",
    );
    const detailDialog = document.querySelector(
      "#selector-operation-detail-dialog",
    );
    const showingConfirm = confirmationKinds.has(workspace?.kind);
    const showingDetail = detailKinds.has(workspace?.kind);
    openDialogForState(confirmDialog, showingConfirm);
    openDialogForState(detailDialog, showingDetail);

    if (showingConfirm) {
      let title = "确认操作";
      let target = "";
      let outcome = workspace.outcome || "";
      let needsReason = true;
      let submit = "确认";
      if (workspace.kind === "gate-confirm") {
        title = workspace.action === "pause" ? "人工暂停" : "人工恢复";
        target = `策略：${workspace.detail.strategy_id}`;
        submit = title;
      } else if (workspace.kind === "rollback-confirm") {
        title = "发起回滚验证";
        target = `源版本：${workspace.detail.id}`;
        outcome = "将复制为全新的草稿并重新验证；不会直接修改 Active Bundle。";
        submit = "创建验证草稿";
      } else if (workspace.kind === "alert-confirm") {
        title = workspace.action === "acknowledge" ? "确认告警" : "解决告警";
        target = `告警：#${workspace.detail.id}`;
        needsReason = workspace.action === "resolve";
        submit = title;
      } else if (workspace.kind === "settings-confirm") {
        title = "二次确认设置变更";
        target = `设置 revision：${state.settings?.revision ?? 0}`;
        outcome = workspace.outcome;
        needsReason = workspace.requiresReason === true;
        submit = "确认保存";
      } else if (workspace.kind === "secret-clear-confirm") {
        title = "独立清除秘密";
        target = `秘密：${workspace.secretName}`;
        outcome = workspace.outcome;
        needsReason = false;
        submit = "确认清除";
      }
      setNodeText(document, "selector-operation-confirm-title", title);
      setNodeText(document, "selector-operation-confirm-target", target);
      setNodeText(document, "selector-operation-confirm-outcome", outcome);
      setNodeText(
        document,
        "selector-operation-confirm-error",
        workspace.kind === "settings-confirm"
          ? settingsStatusText(workspace.error || "")
          : workspace.error || "",
      );
      setNodeText(
        document,
        "selector-operation-reason-text",
        workspace.kind === "settings-confirm" ? "危险变更原因" : "原因",
      );
      const reasonLabel = document.querySelector(
        "#selector-operation-reason-label",
      );
      const reason = document.querySelector("#selector-operation-reason");
      const submitButton = document.querySelector(
        "#selector-operation-confirm-submit",
      );
      if (reasonLabel) reasonLabel.hidden = !needsReason;
      if (reason) {
        reason.required = needsReason;
        if (
          reason.dataset.workspaceGeneration
          !== String(workspace.generation)
        ) {
          reason.value = workspace.reason || "";
          reason.dataset.workspaceGeneration = String(workspace.generation);
        }
      }
      if (submitButton) {
        submitButton.textContent = submit;
        submitButton.disabled = workspace.busy === true;
        submitButton.className = operationConfirmationIsDangerous(workspace)
          ? "danger"
          : "primary";
      }
    } else {
      const form = document.querySelector("#selector-operation-confirm-form");
      if (form && typeof form.reset === "function") form.reset();
    }

    if (showingDetail) {
      const titles = {
        "element-detail": "已选元素详情",
        "gate-result": "策略闸门结果",
        "run-detail": "探针运行详情",
        "version-detail": "版本详情",
        "rollback-result": "回滚验证请求",
        "alert-detail": "告警详情",
        "alert-result": "告警操作结果",
        "settings-result": "设置操作结果",
      };
      setNodeText(
        document,
        "selector-operation-detail-title",
        titles[workspace.kind] || "运维详情",
      );
      setNodeText(
        document,
        "selector-operation-detail-error",
        workspace.error || "",
      );
      replaceSimpleRows(
        document,
        "#selector-operation-detail-body",
        operationDetailLines(workspace),
      );
      const stageDetail = document.querySelector(
        "#selector-run-stage-detail",
      );
      const technicalDetail = document.querySelector(
        "#selector-run-technical-details",
      );
      const technicalLines = document.querySelector(
        "#selector-run-technical-lines",
      );
      if (workspace.kind === "run-detail") {
        const run = sanitizeRun(workspace.detail);
        const presentation = buildRunPresentation(run);
        renderRunStageCards(document, stageDetail, presentation);
        if (technicalDetail) {
          const changedRun = technicalDetail.dataset.runId !== run.id;
          technicalDetail.hidden = false;
          if (changedRun) technicalDetail.open = false;
          technicalDetail.dataset.runId = run.id;
        }
        if (technicalLines) {
          technicalLines.replaceChildren(
            ...runTechnicalLines(run).map((line) => createTextElement(
              document,
              "article",
              line,
              "selector-detail-row",
            )),
          );
        }
      } else {
        stageDetail?.replaceChildren();
        technicalLines?.replaceChildren();
        if (technicalDetail) {
          technicalDetail.hidden = true;
          technicalDetail.open = false;
          delete technicalDetail.dataset.runId;
        }
      }
      const actions = document.querySelector(
        "#selector-operation-detail-actions",
      );
      if (actions) {
        const buttons = [];
        if (workspace.kind === "run-detail") {
          const run = sanitizeRun(workspace.detail);
          if (
            ["infrastructure_failed", "failed"].includes(run.status)
            && (run.retry_delay_minutes || run.next_retry_at)
          ) {
            const retry = document.createElement("button");
            retry.type = "button";
            retry.dataset.runRetryId = run.id;
            retry.textContent = "重新探测";
            buttons.push(retry);
          }
        } else if (workspace.kind === "version-detail") {
          const version = sanitizeVersion(workspace.detail);
          if (state.session?.role === "administrator") {
            versionActions(version).forEach((item) => {
              const button = document.createElement("button");
              button.type = "button";
              button.dataset.versionAction = item.id;
              button.dataset.versionId = version.id;
              button.textContent = item.label;
              buttons.push(button);
            });
          }
        } else if (
          workspace.kind === "alert-detail"
          || workspace.kind === "alert-result"
        ) {
          const alert = sanitizeAlert(workspace.detail);
          const model = alertActionModel(alert);
          if (alert.screenshot_available) {
            const screenshot = document.createElement("a");
            screenshot.href = alert.screenshot_url;
            screenshot.target = "_blank";
            screenshot.rel = "noopener";
            screenshot.textContent = "查看脱敏现场截图";
            buttons.push(screenshot);
          }
          if (
            ["administrator", "operator"].includes(state.session?.role)
            && !model.acknowledge.disabled
          ) {
            const acknowledge = document.createElement("button");
            acknowledge.type = "button";
            acknowledge.dataset.alertAction = "acknowledge";
            acknowledge.dataset.alertId = String(alert.id ?? "");
            acknowledge.textContent = model.acknowledge.label;
            buttons.push(acknowledge);
          }
          if (state.session?.role === "administrator") {
            const resolve = document.createElement("button");
            resolve.type = "button";
            resolve.dataset.alertAction = "resolve";
            resolve.dataset.alertId = String(alert.id ?? "");
            resolve.className = "danger";
            resolve.textContent = model.resolve.label;
            resolve.disabled = model.resolve.disabled;
            buttons.push(resolve);
          }
        }
        actions.replaceChildren(...buttons);
      }
    } else {
      const body = document.querySelector("#selector-operation-detail-body");
      const actions = document.querySelector(
        "#selector-operation-detail-actions",
      );
      body?.replaceChildren();
      actions?.replaceChildren();
      document.querySelector("#selector-run-stage-detail")?.replaceChildren();
      document.querySelector("#selector-run-technical-lines")?.replaceChildren();
      const technicalDetail = document.querySelector(
        "#selector-run-technical-details",
      );
      if (technicalDetail) {
        technicalDetail.hidden = true;
        technicalDetail.open = false;
        delete technicalDetail.dataset.runId;
      }
    }
  }

  function renderTemporaryPassword(document, state) {
    const dialog = document.querySelector(
      "#selector-temporary-password-dialog",
    );
    const credential = state?.temporaryCredential;
    openDialogForState(dialog, Boolean(credential));
    setNodeText(
      document,
      "selector-temporary-password-user",
      credential ? `账户：${credential.username || "未知"}` : "",
    );
    setNodeText(
      document,
      "selector-temporary-password-value",
      credential?.password || "",
    );
  }

  function selectorProbeDependencies(win) {
    const document = win.document;
    let wired = false;
    const dialogReturnFocus = new WeakMap();
    const dialogSelectors = [
      "#selector-element-picker-dialog",
      "#selector-operation-confirm-dialog",
      "#selector-operation-detail-dialog",
      "#selector-temporary-password-dialog",
    ];

    function focusableNodes(dialog) {
      if (!dialog || typeof dialog.querySelectorAll !== "function") return [];
      return Array.from(dialog.querySelectorAll(
        'button:not([disabled]), [href], input:not([disabled]), '
        + 'select:not([disabled]), textarea:not([disabled]), '
        + '[tabindex]:not([tabindex="-1"])',
      )).filter((node) => visibleFocusCandidate(
        node,
        typeof win.getComputedStyle === "function"
          ? win.getComputedStyle.bind(win)
          : null,
      ));
    }

    function captureFocus() {
      const active = document.activeElement;
      if (
        !active
        || active === document.body
        || active === document.documentElement
      ) return null;
      return Object.assign(stableFocusToken(active), {lastFocused: active});
    }

    function restoreFocus(token) {
      if (!token) return;
      const current = document.activeElement;
      if (
        current
        && current !== document.body
        && current !== document.documentElement
        && current !== token.lastFocused
        && current.isConnected !== false
      ) return;
      const target = resolveFocusToken(document, token);
      if (target && typeof target.focus === "function") {
        target.focus();
        token.lastFocused = target;
      }
    }

    function wireDialogAccessibility(dialog) {
      if (!dialog) return;
      dialog.addEventListener("keydown", (event) => {
        if (event.key !== "Tab") return;
        const focusable = focusableNodes(dialog);
        if (!focusable.length) {
          event.preventDefault();
          return;
        }
        const current = focusable.indexOf(document.activeElement);
        const next = trappedFocusIndex(
          current < 0 ? (event.shiftKey ? 0 : focusable.length - 1) : current,
          event.shiftKey,
          focusable.length,
        );
        event.preventDefault();
        focusable[next].focus();
      });
      dialog.addEventListener("close", () => {
        const returnToken = dialogReturnFocus.get(dialog);
        dialogReturnFocus.delete(dialog);
        win.setTimeout(() => {
          const anotherDialogOpen = dialogSelectors.some((selector) => {
            const candidate = document.querySelector(selector);
            return candidate && candidate !== dialog && candidate.open;
          });
          if (
            !anotherDialogOpen
            && returnToken
          ) {
            const returnTarget = resolveFocusToken(document, returnToken);
            if (returnTarget && typeof returnTarget.focus === "function") {
              returnTarget.focus();
            }
          }
        }, 0);
      });
    }

    function cleanupSensitiveUI() {
      dialogSelectors.forEach((selector) => {
        const dialog = document.querySelector(selector);
        if (!dialog) return;
        dialogReturnFocus.delete(dialog);
        if (dialog.open && typeof dialog.close === "function") dialog.close();
      });
      clearSettingsFormSecrets(document);
      setText("#selector-temporary-password-user", "");
      setText("#selector-temporary-password-value", "");
      const settingsForm = document.querySelector("#selector-settings-form");
      if (settingsForm?.dataset) settingsForm.dataset.dirty = "false";
    }

    function setText(selector, value) {
      const node = document.querySelector(selector);
      if (node) node.textContent = String(value ?? "");
    }

    function wire(controller) {
      if (wired) return;
      wired = true;
      const tabButtons = Array.from(
        document.querySelectorAll("[data-selector-probe-tab]"),
      );
      tabButtons.forEach((button, index) => {
        button.addEventListener("click", () => {
          controller.activateTab(button.dataset.selectorProbeTab);
        });
        button.addEventListener("keydown", (event) => {
          if (["Enter", " "].includes(event.key)) {
            event.preventDefault();
            controller.activateTab(button.dataset.selectorProbeTab);
            return;
          }
          if (!["ArrowRight", "ArrowLeft", "Home", "End"].includes(event.key)) {
            return;
          }
          event.preventDefault();
          const target = tabButtons[
            tabIndexForKey(index, event.key, tabButtons.length)
          ];
          if (target && typeof target.focus === "function") target.focus();
        });
      });
      document.querySelector("#selector-probe-refresh")?.addEventListener(
        "click",
        () => controller.refreshCurrent(),
      );
      const filterForm = document.querySelector("#selector-element-filters");
      filterForm?.elements?.namedItem("search")?.addEventListener(
        "input",
        () => controller.updateElementFilters(
          serializeElementFilters(filterForm),
          {debounce: true},
        ),
      );
      filterForm?.querySelectorAll("select").forEach((control) => {
        control.addEventListener("change", () => controller.updateElementFilters(
          serializeElementFilters(filterForm),
        ));
      });
      document.querySelector("#selector-element-page-size")?.addEventListener(
        "change",
        (event) => controller.setElementPageSize(event.target.value),
      );
      document.querySelector("#selector-element-prev")?.addEventListener(
        "click",
        () => controller.setElementPage(controller.state.elements.page - 1),
      );
      document.querySelector("#selector-element-next")?.addEventListener(
        "click",
        () => controller.setElementPage(controller.state.elements.page + 1),
      );
      document.querySelector("#selector-element-picker")?.addEventListener(
        "click",
        () => controller.openLivePicker(),
      );
      const pickerForm = document.querySelector("#selector-picker-form");
      document.querySelector("#selector-picker-start")?.addEventListener(
        "click",
        () => controller.startLivePicker(pickerForm),
      );
      document.querySelector("#selector-picker-confirm")?.addEventListener(
        "click",
        () => controller.confirmCollector(
          (controller.state.picker?.selectedIds || []).map((selectionId) => ({
            selectionId,
            displayName: controller.state.picker?.names?.[selectionId] || "",
          })),
        ),
      );
      for (const id of ["#selector-picker-cancel", "#selector-picker-close"]) {
        document.querySelector(id)?.addEventListener(
          "click",
          () => controller.cancelLivePicker(),
        );
      }
      document.querySelector("#selector-picker-selections")?.addEventListener(
        "click",
        (event) => {
          const remove = event.target.closest?.("[data-selector-picker-remove]");
          if (remove) {
            controller.removeLivePickerSelection(
              remove.dataset.selectorPickerRemove,
            );
          }
        },
      );
      const inventoryList = document.querySelector("#selector-inventory-list");
      inventoryList?.addEventListener("change", (event) => {
        const checkbox = event.target.closest?.("[data-inventory-select]");
        if (checkbox) {
          controller.setCollectorSelection(
            checkbox.dataset.inventorySelect,
            checkbox.checked,
          );
        }
      });
      inventoryList?.addEventListener("input", (event) => {
        const input = event.target.closest?.("[data-inventory-name]");
        if (input) controller.setCollectorName(input.dataset.inventoryName, input.value);
      });
      const collectorFilters = () => controller.setCollectorFilters({
        type: document.querySelector("#selector-inventory-type")?.value || "all",
        region: document.querySelector("#selector-inventory-region")?.value || "all",
        locatable: document.querySelector("#selector-inventory-locatable")?.checked === true,
        search: document.querySelector("#selector-inventory-search")?.value || "",
      });
      for (const selector of [
        "#selector-inventory-type", "#selector-inventory-region",
        "#selector-inventory-locatable",
      ]) document.querySelector(selector)?.addEventListener("change", collectorFilters);
      document.querySelector("#selector-inventory-search")?.addEventListener(
        "input", collectorFilters,
      );
      document.querySelector("#selector-inventory-refresh")?.addEventListener(
        "click", () => controller.pollCollector(),
      );
      document.querySelector("#selector-managed-rebind-new")?.addEventListener(
        "click", async () => {
          await controller.activateTab("collect");
          controller.openCollector();
        },
      );
      const pickerDialog = document.querySelector(
        "#selector-element-picker-dialog",
      );
      pickerDialog?.addEventListener("cancel", (event) => {
        event.preventDefault();
        controller.cancelLivePicker();
      });
      document.querySelector("#selector-managed-elements")?.addEventListener(
        "click",
        async (event) => {
          const action = event.target.closest?.("[data-managed-action]");
          if (!action || action.disabled) return;
          const elementId = action.dataset.elementId;
          const item = controller.state.elements.items.find(
            (entry) => entry.id === elementId,
          );
          if (!item) return;
          if (action.dataset.managedAction === "detail") {
            controller.openManagedElementDetail(elementId);
          } else if (action.dataset.managedAction === "rename") {
            const name = win.prompt("新的自定义名称", item.display_name || "");
            if (name !== null) controller.renameElement(elementId, name, item.revision);
          } else if (action.dataset.managedAction === "rebind") {
            controller.beginElementRebind(elementId);
          } else if (action.dataset.managedAction === "delete") {
            if (win.confirm(`确定删除“${item.display_name || elementId}”吗？`)) {
              controller.deleteElement(elementId, item.revision);
            }
          } else if (action.dataset.managedAction === "screenshot") {
            const alert = controller.state.alerts.items.find(
              (entry) => (entry.aliases || []).includes(elementId),
            );
            if (alert) controller.openAlertDetail(alert);
            else {
              controller.state.status = {kind: "success", message: "该元素暂无现场截图"};
              controller.refreshCurrent();
            }
          }
        },
      );
      function summaryClick(event) {
        const target = event.target.closest?.("[data-selector-summary-tab]");
        if (!target) return;
        const filters = {};
        if (target.dataset.selectorSummaryFilter) {
          filters[target.dataset.selectorSummaryFilter] = (
            target.dataset.selectorSummaryValue
          );
        }
        controller.activateSummary(target.dataset.selectorSummaryTab, filters);
      }
      document.querySelector("#selector-probe-panel-overview")?.addEventListener(
        "click",
        summaryClick,
      );
      document.querySelector("#selector-element-counts")?.addEventListener(
        "click",
        summaryClick,
      );
      document.querySelectorAll(
        "#selector-probe-run-now, #selector-run-now",
      ).forEach((button) => {
        button.addEventListener("click", () => controller.requestRunNow());
      });
      document.querySelector("#selector-gate-rows")?.addEventListener(
        "click",
        (event) => {
          const action = event.target.closest?.("[data-gate-action]");
          if (!action) return;
          const gate = controller.state.gates.items.find(
            (item) => item.strategy_id === action.dataset.gateStrategyId,
          );
          if (gate) controller.confirmManualGate(gate, action.dataset.gateAction);
        },
      );
      function runClick(event) {
        const retry = event.target.closest?.("[data-run-retry-id]");
        if (retry) {
          controller.requestRunRetry(retry.dataset.runRetryId);
          return;
        }
        const detail = event.target.closest?.("[data-run-detail-id]");
        if (detail) controller.openRunDetail(detail.dataset.runDetailId);
      }
      document.querySelector("#selector-run-rows")?.addEventListener(
        "click",
        runClick,
      );
      function versionClick(event) {
        const action = event.target.closest?.("[data-version-action]");
        if (action) {
          const version = controller.state.versions.items.find(
            (item) => (
              String(item.id || item.version_id)
              === action.dataset.versionId
            ),
          ) || controller.state.operationWorkspace?.detail;
          if (
            action.dataset.versionAction === "rollback-validation"
            && version
          ) controller.confirmRollbackValidation(version);
          return;
        }
        const detail = event.target.closest?.("[data-version-detail-id]");
        if (detail) controller.openVersionDetail(detail.dataset.versionDetailId);
      }
      document.querySelector("#selector-version-rows")?.addEventListener(
        "click",
        versionClick,
      );
      function alertClick(event) {
        const action = event.target.closest?.("[data-alert-action]");
        if (action) {
          const alertId = Number(action.dataset.alertId);
          const alert = controller.state.alerts.items.find(
            (item) => Number(item.id) === alertId,
          ) || controller.state.operationWorkspace?.detail;
          if (alert) {
            controller.confirmAlertAction(alert, action.dataset.alertAction);
          }
          return;
        }
        const detail = event.target.closest?.("[data-alert-detail-id]");
        if (!detail) return;
        const alertId = Number(detail.dataset.alertDetailId);
        const alert = controller.state.alerts.items.find(
          (item) => Number(item.id) === alertId,
        );
        if (alert) controller.openAlertDetail(alert);
      }
      document.querySelector("#selector-alert-rows")?.addEventListener(
        "click",
        alertClick,
      );
      document.querySelector("#selector-operation-detail-actions")
        ?.addEventListener("click", (event) => {
          runClick(event);
          versionClick(event);
          alertClick(event);
        });
      const settingsForm = document.querySelector("#selector-settings-form");
      function settingsFormControl(name) {
        return settingsForm?.elements?.namedItem(name);
      }
      function settingsCandidateFromForm() {
        const current = sanitizeSettings(
          controller.state.settingsDraft || controller.state.settings,
        );
        return Object.assign({}, current, {
          enabled: settingsFormControl("enabled")?.checked === true,
          rollout_mode: settingsFormControl("rolloutMode")?.value || "observe",
          schedule_time: settingsFormControl("scheduleTime")?.value || "03:00",
          target_origin: normalizeTargetOrigin(
            settingsFormControl("targetOrigin")?.value || "",
          ),
          page_timeout_seconds: Number(
            settingsFormControl("pageTimeoutSeconds")?.value || 90,
          ),
          freshness_hours: Number(
            settingsFormControl("freshnessHours")?.value || 36,
          ),
          redis: Object.assign({}, current.redis, {
            namespace: settingsFormControl("redisNamespace")?.value || "",
          }),
          webhook: Object.assign({}, current.webhook, {
            enabled: settingsFormControl("webhookEnabled")?.checked === true,
            type: settingsFormControl("webhookType")?.value || "generic",
          }),
        });
      }
      function settingsSecretsFromForm() {
        return {
          redis_password: settingsFormControl("redisPassword")?.value || "",
          webhook_signing_secret: (
            settingsFormControl("webhookSigningSecret")?.value || ""
          ),
          webhook_url: settingsFormControl("webhookUrl")?.value || "",
        };
      }
      settingsForm?.addEventListener("input", () => {
        settingsForm.dataset.dirty = "true";
        controller.stageSettingsDraft(settingsCandidateFromForm());
      });
      document.querySelector("#selector-settings-save")?.addEventListener(
        "click",
        () => controller.confirmSettingsSave(
          settingsCandidateFromForm(),
          settingsFormControl("reason")?.value || "",
          settingsSecretsFromForm(),
        ),
      );
      document.querySelector("#selector-settings-reload")?.addEventListener(
        "click",
        () => {
          settingsForm.dataset.dirty = "false";
          controller.reloadSettingsDraft();
        },
      );
      document.querySelector("#selector-settings-webhook-test")
        ?.addEventListener("click", () => controller.requestWebhookTest());
      document.querySelector("#selector-settings-profile-stage")
        ?.addEventListener("click", () => {
          const input = settingsFormControl("profileAdd");
          if (controller.stageProfileAdds(input?.value || "") > 0 && input) {
            input.value = "";
          }
        });
      document.querySelector("#selector-settings-profile-import")
        ?.addEventListener("click", () => controller.importSelectedProfiles());
      settingsForm?.addEventListener("click", (event) => {
        const stagedRemove = event.target.closest?.(
          "[data-settings-profile-stage-remove]",
        );
        if (stagedRemove) {
          controller.removeStagedProfile(
            stagedRemove.dataset.settingsProfileStageRemove,
          );
          return;
        }
        const remove = event.target.closest?.(
          "[data-settings-profile-remove]",
        );
        if (remove && !remove.disabled) {
          settingsForm.dataset.dirty = "true";
          controller.stageProfileRemoval(
            remove.dataset.settingsProfileRemove,
          );
          return;
        }
        const clear = event.target.closest?.("[data-settings-secret-clear]");
        if (!clear) return;
        controller.confirmSecretClear(
          clear.dataset.settingsSecretClear,
          settingsFormControl("reason")?.value || "",
        );
      });
      const accountCreate = document.querySelector(
        "#selector-account-create-form",
      );
      document.querySelector("#selector-account-add")?.addEventListener(
        "click",
        () => {
          if (accountCreate) accountCreate.hidden = !accountCreate.hidden;
        },
      );
      document.querySelector("#selector-account-create-submit")
        ?.addEventListener("click", () => {
          const username = accountCreate?.querySelector(
            '[name="username"]',
          )?.value || "";
          const role = accountCreate?.querySelector(
            '[name="role"]',
          )?.value || "operator";
          controller.createAccount(username, role);
        });
      document.querySelector("#selector-account-rows")?.addEventListener(
        "click",
        (event) => {
          const action = event.target.closest?.("[data-account-action]");
          if (!action || action.disabled) return;
          const userId = Number(action.dataset.accountId);
          if (action.dataset.accountAction === "disable") {
            controller.updateAccount(userId, {enabled: false});
          } else if (action.dataset.accountAction === "enable") {
            controller.updateAccount(userId, {enabled: true});
          } else if (action.dataset.accountAction === "demote") {
            controller.updateAccount(userId, {role: "operator"});
          } else if (action.dataset.accountAction === "promote") {
            controller.updateAccount(userId, {role: "administrator"});
          } else if (action.dataset.accountAction === "reset-password") {
            controller.resetAccountPassword(userId);
          } else if (action.dataset.accountAction === "revoke-sessions") {
            controller.revokeAccountSessions(userId);
          }
        },
      );
      const operationConfirmForm = document.querySelector(
        "#selector-operation-confirm-form",
      );
      operationConfirmForm?.addEventListener("submit", (event) => {
        event.preventDefault();
        const reason = document.querySelector(
          "#selector-operation-reason",
        )?.value || "";
        const kind = controller.state.operationWorkspace?.kind;
        if (kind === "gate-confirm") {
          controller.submitManualGate(reason);
        } else if (kind === "rollback-confirm") {
          controller.submitRollbackValidation(reason);
        } else if (kind === "alert-confirm") {
          controller.submitAlertAction(reason);
        } else if (kind === "settings-confirm") {
          controller.submitSettingsSave(reason);
        } else if (kind === "secret-clear-confirm") {
          controller.submitSecretClear();
        }
      });
      for (const id of [
        "#selector-operation-confirm-close",
        "#selector-operation-confirm-cancel",
        "#selector-operation-detail-close",
      ]) {
        document.querySelector(id)?.addEventListener(
          "click",
          () => controller.closeOperationWorkspace(),
        );
      }
      const confirmDialog = document.querySelector(
        "#selector-operation-confirm-dialog",
      );
      const detailDialog = document.querySelector(
        "#selector-operation-detail-dialog",
      );
      confirmDialog?.addEventListener("cancel", (event) => {
        event.preventDefault();
        controller.closeOperationWorkspace();
      });
      confirmDialog?.addEventListener("close", () => {
        if ([
          "gate-confirm",
          "rollback-confirm",
          "alert-confirm",
          "settings-confirm",
          "secret-clear-confirm",
        ].includes(controller.state.operationWorkspace?.kind)) {
          controller.closeOperationWorkspace();
        }
      });
      detailDialog?.addEventListener("cancel", (event) => {
        event.preventDefault();
        controller.closeOperationWorkspace();
      });
      detailDialog?.addEventListener("close", () => {
        if ([
          "element-detail",
          "gate-result",
          "run-detail",
          "version-detail",
          "rollback-result",
          "alert-detail",
          "alert-result",
          "settings-result",
        ].includes(controller.state.operationWorkspace?.kind)) {
          controller.closeOperationWorkspace();
        }
      });
      for (const id of [
        "#selector-temporary-password-close",
        "#selector-temporary-password-done",
      ]) {
        document.querySelector(id)?.addEventListener(
          "click",
          () => controller.clearTemporaryPassword(),
        );
      }
      document.querySelector("#selector-temporary-password-copy")
        ?.addEventListener("click", () => controller.copyTemporaryPassword());
      const temporaryDialog = document.querySelector(
        "#selector-temporary-password-dialog",
      );
      temporaryDialog?.addEventListener("cancel", (event) => {
        event.preventDefault();
        controller.clearTemporaryPassword();
      });
      temporaryDialog?.addEventListener("close", () => {
        if (controller.state.temporaryCredential) {
          controller.clearTemporaryPassword();
        }
      });
      dialogSelectors.forEach((selector) => {
        wireDialogAccessibility(document.querySelector(selector));
      });
    }

    return {
      requestJson: async function (url, method, body, options) {
        const response = await win.fetch(url, {
          method: method || "GET",
          headers: {"Content-Type": "application/json"},
          body: body === undefined ? undefined : JSON.stringify(body),
          signal: options?.signal,
        });
        let data = {};
        try {
          data = await response.json();
        } catch (_error) {
          data = {error: "服务器返回无效响应"};
        }
        return {status: response.status, data};
      },
      setInterval: win.setInterval.bind(win),
      clearInterval: win.clearInterval.bind(win),
      setTimeout: win.setTimeout.bind(win),
      clearTimeout: win.clearTimeout.bind(win),
      copyText: async (value) => {
        if (typeof win.navigator?.clipboard?.writeText !== "function") {
          return false;
        }
        await win.navigator.clipboard.writeText(value);
        return true;
      },
      confirm: (message) => win.confirm(message),
      createAbortController: () => (
        typeof win.AbortController === "function"
          ? new win.AbortController()
          : null
      ),
      isVisible: () => document.visibilityState !== "hidden",
      documentVisible: () => document.visibilityState !== "hidden",
      addVisibilityListener: (callback) => {
        document.addEventListener("visibilitychange", callback);
        return () => document.removeEventListener(
          "visibilitychange",
          callback,
        );
      },
      captureFocus,
      restoreFocus,
      clearSettingsFormSecrets: () => clearSettingsFormSecrets(document),
      selectedAdsPowerProfileIds: () => (
        Array.from(document.querySelectorAll(".adspower-select:checked"))
          .map((element) => element.dataset?.profileId || "")
          .filter(Boolean)
      ),
      cleanupSensitiveUI,
      render: function (_view, state, controller) {
        const activeBeforeRender = document.activeElement;
        const dialogOpenBefore = new Map(dialogSelectors.map((selector) => {
          const dialog = document.querySelector(selector);
          return [dialog, Boolean(dialog?.open)];
        }));
        wire(controller);
        renderOverview(document, state);
        renderElementDirectory(document, state);
        inventoryUI?.renderManagedElements(
          document,
          document.querySelector("#selector-managed-elements"),
          state.elements?.items || [],
          {canEdit: state.session?.role === "administrator"},
        );
        renderLivePicker(document, state);
        renderGates(document, state);
        renderRuns(document, state);
        const latestRun = (state.runs?.items || [])[0] || {};
        renderRunStageCards(
          document,
          document.querySelector("#selector-run-current-steps"),
          buildRunPresentation(latestRun),
        );
        renderVersions(document, state);
        renderAlerts(document, state);
        renderSettings(document, state);
        renderAccountManagement(document, state);
        renderOperationWorkspace(document, state);
        renderTemporaryPassword(document, state);
        const runNowButtons = document.querySelectorAll(
          "#selector-probe-run-now, #selector-run-now",
        );
        const runNowHidden = !["administrator", "operator"].includes(
          state.session?.role,
        );
        const runNowDisabled = (
          (
            state.operationWorkspace?.kind === "run-request"
            && state.operationWorkspace.busy === true
          )
          || (
            state.operationWorkspace?.kind === "run-detail"
            && runIsActive(state.operationWorkspace.detail)
          )
          || (state.runs?.items || []).some(runIsActive)
        );
        runNowButtons.forEach((button) => {
          button.hidden = runNowHidden;
          button.disabled = runNowDisabled;
        });
        document.querySelectorAll("[data-selector-probe-tab]").forEach((button) => {
          const active = button.dataset.selectorProbeTab === state.activeTab;
          button.setAttribute("aria-selected", String(active));
          button.tabIndex = active ? 0 : -1;
        });
        document.querySelectorAll("[data-selector-probe-panel]").forEach((panel) => {
          const active = panel.dataset.selectorProbePanel === state.activeTab;
          panel.hidden = !active;
          panel.classList.toggle("is-active", active);
        });
        const health = healthPresentation(state.overview);
        const healthNode = document.querySelector("#selector-probe-health");
        if (healthNode) {
          healthNode.textContent = health.text;
          healthNode.dataset.health = health.kind;
        }
        setText(
          "#selector-probe-page-status",
          state.status ? state.status.message : "",
        );
        setText(
          "#selector-probe-unread-alerts",
          state.overview?.unread_alert_count || state.alerts?.unread || 0,
        );
        dialogOpenBefore.forEach((wasOpen, dialog) => {
          if (!dialog || wasOpen || !dialog.open) return;
          if (activeBeforeRender && !dialog.contains?.(activeBeforeRender)) {
            dialogReturnFocus.set(dialog, stableFocusToken(activeBeforeRender));
          }
          const first = focusableNodes(dialog)[0];
          if (first && typeof first.focus === "function") first.focus();
        });
      },
    };
  }

  return {
    TABS,
    accountActionModel,
    alertActionModel,
    buildRunPresentation,
    buildElementQuery,
    canCreateElement,
    clearSettingsFormSecrets,
    createSelectorProbeUI,
    dangerousSettingsDiff,
    normalizeTargetOrigin,
    parseProfileIds,
    manualResumeOutcome,
    operationConfirmationIsDangerous,
    renderAlerts,
    renderAccountManagement,
    renderOperationWorkspace,
    renderElementDirectory,
    renderLivePicker,
    renderGates,
    renderOverview,
    renderRunStageCards,
    renderRuns,
    renderSettings,
    renderTemporaryPassword,
    renderVersions,
    runTechnicalLines,
    sanitizeAlert,
    sanitizeAccount,
    sanitizeElementDetail,
    sanitizeGate,
    sanitizePickerSession,
    sanitizeRun,
    sanitizeSettings,
    sanitizeStructuredLocators,
    sanitizeVersion,
    selectOverviewElements,
    serializeElementFilters,
    selectorProbeDependencies,
    settingsFingerprint,
    settingsPermissions,
    settingsStatusText,
    stableFocusToken,
    summarizeValidation,
    runIsActive,
    syntheticWebhookPayload,
    tabIndexForKey,
    trappedFocusIndex,
    validateSettingsSave,
    versionActions,
    visibleFocusCandidate,
    resolveFocusToken,
  };
});
