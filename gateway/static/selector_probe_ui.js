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

  const TABS = Object.freeze([
    Object.freeze({id: "overview", label: "总览"}),
    Object.freeze({id: "elements", label: "元素"}),
    Object.freeze({id: "gates", label: "策略闸门"}),
    Object.freeze({id: "runs", label: "探针运行"}),
    Object.freeze({id: "versions", label: "版本"}),
    Object.freeze({id: "alerts", label: "告警"}),
    Object.freeze({id: "settings", label: "设置"}),
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
      '[name="modelApiKey"]',
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

  const SAFE_PROBE_ACTIONS = new Set([
    "inspect_only",
    "open_read_only",
    "close_read_only",
  ]);
  const SAFE_LOCATOR_TYPES = new Set(["attribute", "role", "css", "xpath"]);
  const DETAIL_TABS = Object.freeze([
    "evidence",
    "candidates",
    "repairs",
    "history",
  ]);

  function semanticValue(form, camelName, snakeName) {
    return (
      filterValue(form, camelName)
      ?? filterValue(form, snakeName)
    );
  }

  function textList(value) {
    if (Array.isArray(value)) {
      return value.map((item) => String(item).trim()).filter(Boolean);
    }
    return String(value || "")
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
  }

  function serializeSemanticContract(form) {
    const probeAction = String(
      semanticValue(form, "probeAction", "probe_action") || "",
    ).trim();
    if (!SAFE_PROBE_ACTIONS.has(probeAction)) {
      throw new Error("unsafe_probe_action");
    }
    const payload = {
      display_name: String(
        semanticValue(form, "displayName", "display_name") || "",
      ).trim(),
      intent: String(semanticValue(form, "intent", "intent") || "").trim(),
      required_state: String(
        semanticValue(form, "requiredState", "required_state") || "",
      ).trim(),
      scope: String(semanticValue(form, "scope", "scope") || "").trim(),
      probe_action: probeAction,
    };
    const optionalLists = [
      ["acceptedRoles", "accepted_roles", "accepted_roles"],
      ["acceptedNames", "accepted_names", "accepted_names"],
      ["preferredAttributes", "preferred_attributes", "preferred_attributes"],
    ];
    for (const [camelName, snakeName, target] of optionalLists) {
      const selected = textList(semanticValue(form, camelName, snakeName));
      if (selected.length) payload[target] = selected;
    }
    const nameMode = String(
      semanticValue(form, "nameMode", "name_mode") || "",
    ).trim();
    if (nameMode) payload.name_mode = nameMode;
    const postcondition = String(
      semanticValue(form, "postcondition", "postcondition") || "",
    ).trim();
    if (postcondition) payload.postcondition = postcondition;
    return payload;
  }

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
      preflight_required: "切换 Enforce 前请先运行预检",
      preflight_failed: "Enforce 预检未通过",
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

  function sanitizeProbeSuggestion(raw) {
    const source = raw && typeof raw === "object" ? raw : {};
    const stableAttributes = Array.isArray(source.stable_attributes)
      ? source.stable_attributes.slice(0, 20).map((attribute) => ({
        name: safeText(attribute?.name, 128),
        value: safeText(attribute?.value, 240),
      })).filter((attribute) => attribute.name)
      : [];
    const rejectedMethods = Array.isArray(source.rejected_methods)
      ? source.rejected_methods.slice(0, 20).map((item) => ({
        method: safeCode(item?.method),
        code: safeCode(item?.code),
      })).filter((item) => item.method && item.code)
      : [];
    const warnings = Array.isArray(source.warnings)
      ? source.warnings.map(safeCode).filter(Boolean).slice(0, 20)
      : [];
    let candidates = [];
    try {
      candidates = sanitizeStructuredLocators(source.candidates, {editable: false});
    } catch (_error) {
      warnings.push("unsafe_candidate_omitted");
    }
    return {
      role: safeText(source.role, 64),
      name: safeText(source.name, 240),
      stable_attributes: stableAttributes,
      candidates,
      llm_used: source.llm_used === true,
      rejected_methods: rejectedMethods,
      warnings,
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

  function sanitizeRepairs(value) {
    if (!Array.isArray(value)) return [];
    return value.slice(0, 3).map((raw, index) => ({
      attempt: Number(raw?.attempt ?? raw?.attempt_number) || index + 1,
      previous_method: safeCode(raw?.previous_method),
      failure_code: safeCode(raw?.failure_code),
      match_count: Number.isInteger(raw?.match_count) ? raw.match_count : null,
      new_method: safeCode(raw?.new_method),
      prompt_version: safeText(raw?.prompt_version, 128),
      model_id: safeText(raw?.model_id, 128),
      validation_result: safeCode(
        raw?.validation_result || raw?.status || raw?.result,
      ),
    }));
  }

  function sanitizeCandidateGroup(value) {
    let locators = value;
    if (value && !Array.isArray(value) && typeof value === "object") {
      locators = value.locators;
    }
    if (
      Array.isArray(locators)
      && locators.length
      && locators.every(
        (item) => item && typeof item === "object" && Array.isArray(item.locators),
      )
    ) {
      locators = locators.flatMap((item) => item.locators);
    }
    try {
      return sanitizeStructuredLocators(locators, {editable: false});
    } catch (_error) {
      return [];
    }
  }

  function sanitizeElementDetail(raw) {
    const source = raw && typeof raw === "object" ? raw : {};
    const comparison = (
      source.candidate_comparison
      && typeof source.candidate_comparison === "object"
    ) ? source.candidate_comparison : {};
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
    const contract = source.contract && typeof source.contract === "object"
      ? {
        intent: safeText(source.contract.intent, 500),
        required_state: safeCode(source.contract.required_state),
        scope: safeCode(source.contract.scope),
        probe_action: safeCode(source.contract.probe_action),
      }
      : null;
    return {
      id: safeText(source.id, 128),
      display_name: safeText(source.display_name, 240),
      management_source: safeCode(source.management_source),
      published_status: safeCode(source.published_status),
      draft_status: safeCode(source.draft_status),
      scope: safeCode(source.scope),
      active_version: safeText(
        source.active_version || source.base_version_id,
        128,
      ),
      dependency_count: Number(source.dependency_count) || dependencies.length,
      last_validated_at: safeText(source.last_validated_at, 128),
      revision: (
        Number.isInteger(source.revision) && source.revision >= 0
          ? source.revision
          : 0
      ),
      migration_available: source.migration_available === true,
      contract,
      evidence: sanitizeEvidence(source.evidence),
      draft_candidates: sanitizeCandidateGroup(source.candidates),
      candidate_comparison: {
        active: sanitizeCandidateGroup(comparison.active),
        deterministic: sanitizeCandidateGroup(comparison.deterministic),
        repaired: sanitizeCandidateGroup(comparison.repaired),
      },
      repairs: sanitizeRepairs(source.repairs),
      history,
      dependencies,
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

  const DISCOVERY_ATTRIBUTE_NAMES = new Set([
    "data-e2e",
    "data-testid",
    "aria-label",
    "name",
    "placeholder",
    "contenteditable",
    "type",
    "id",
  ]);

  function sanitizeDiscovery(raw) {
    const source = raw && typeof raw === "object" ? raw : {};
    const attributes = {};
    if (source.attributes && typeof source.attributes === "object") {
      Object.entries(source.attributes).slice(0, 20).forEach(([key, value]) => {
        if (DISCOVERY_ATTRIBUTE_NAMES.has(key)) {
          attributes[key] = safeText(value, 160);
        }
      });
    }
    let recommendedLocators = [];
    try {
      recommendedLocators = sanitizeStructuredLocators(
        source.recommended_locators || [],
        {editable: false},
      );
    } catch (_error) {
      recommendedLocators = [];
    }
    return {
      fingerprint: safeText(source.fingerprint, 80),
      page_state: safeCode(source.page_state),
      scope: safeCode(source.scope),
      role: safeCode(source.role),
      name: safeText(source.name, 160),
      states: (
        source.states && typeof source.states === "object"
          ? Object.fromEntries(Object.entries(source.states).slice(0, 32))
          : {}
      ),
      attributes,
      actionable: source.actionable === true,
      profile_masks: safeStringList(source.profile_masks, 8)
        .filter(validProfileMask),
      profile_count: Math.max(Number(source.profile_count) || 0, 0),
      recommended_locators: recommendedLocators,
    };
  }

  const ACTIVE_RUN_STATUSES = new Set(["queued", "running"]);

  function runIsActive(raw) {
    return ACTIVE_RUN_STATUSES.has(safeCode(raw?.status));
  }

  function sanitizeRun(raw) {
    const source = raw && typeof raw === "object" ? raw : {};
    const profiles = Array.isArray(source.profiles)
      ? source.profiles.slice(0, 8).map((item) => ({
        profile_mask: safeText(item?.profile_mask, 16),
        status: safeCode(item?.status) || "unknown",
      })).filter((item) => validProfileMask(item.profile_mask))
      : [];
    const stages = Array.isArray(source.stages)
      ? source.stages.slice(0, 30).map((item) => ({
        name: safeCode(item?.name) || "unknown",
        ...safeOperationState(item),
      }))
      : [];
    const elements = Array.isArray(source.elements)
      ? source.elements.slice(0, 200).map((item) => ({
        alias: safeText(item?.alias || item?.element_id, 128),
        status: safeCode(item?.status) || "unknown",
        failure_class: safeCode(item?.failure_class),
        repair_attempt_count: Number.isInteger(item?.repair_attempt_count)
          ? Math.min(Math.max(item.repair_attempt_count, 0), 3)
          : 0,
      }))
      : [];
    const repairs = sanitizeRepairs(source.repairs);
    const retryDelay = Number(source.retry_delay_minutes);
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
      discoveries: Array.isArray(source.discoveries)
        ? source.discoveries.slice(0, 200).map(sanitizeDiscovery)
        : [],
      failed_aliases: safeStringList(source.failed_aliases, 200),
      repairs,
      publication: safeOperationState(source.publication),
      reconciliation: safeOperationState(source.reconciliation),
      cleanup: safeOperationState(source.cleanup),
      lease: safeOperationState(source.lease),
      failure_class: safeCode(source.failure_class),
      next_retry_at: safeText(source.next_retry_at, 128),
      retry_delay_minutes: [15, 30, 60].includes(retryDelay)
        ? retryDelay
        : null,
      active_version_before: safeText(source.active_version_before, 128),
      published_version_after: safeText(source.published_version_after, 128),
    };
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
      model_id: safeText(source.model_id, 128),
      prompt_version: safeText(source.prompt_version, 128),
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
      repairs: sanitizeRepairs(source.repairs),
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

  function sanitizePreflight(raw) {
    const source = raw && typeof raw === "object" ? raw : {};
    const checks = source.checks && typeof source.checks === "object"
      ? source.checks
      : {};
    const result = {
      status: safeCode(source.status) || "unknown",
      base_revision: (
        Number.isInteger(source.base_revision) && source.base_revision >= 0
          ? source.base_revision
          : null
      ),
      candidate_fingerprint: safeText(source.candidate_fingerprint, 128),
      preflight_token: safeText(source.preflight_token, 256),
      checks: {},
      checked_at: safeText(source.checked_at, 128),
    };
    for (const key of [
      "profiles",
      "redis_aof",
      "redis_eviction",
      "model",
      "webhook",
    ]) {
      result.checks[key] = safeCode(checks[key]) || "unknown";
    }
    return result;
  }

  function sanitizeSettings(raw) {
    const source = raw && typeof raw === "object" ? raw : {};
    const model = source.model && typeof source.model === "object"
      ? source.model
      : {};
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
      model: {
        id: safeText(model.id, 128),
        provider: safeCode(model.provider),
        mode: safeCode(model.mode),
        status: safeCode(model.status) || "unknown",
        api_key_set: model.api_key_set === true,
      },
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
      freshness_hours: settings.freshness_hours,
      retry_policy: clone(settings.retry_policy),
      profiles: settings.profiles.map((item) => ({
        profile_ref: item.profile_ref,
        dedicated_test: item.dedicated_test,
      })),
      model: {id: settings.model.id},
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

  function preflightPassed(raw) {
    const preflight = sanitizePreflight(raw);
    return (
      preflight.status === "passed"
      && [
        "profiles",
        "redis_aof",
        "redis_eviction",
        "model",
        "webhook",
      ].every((key) => preflight.checks[key] === "passed")
    );
  }

  function enforceSettingsReady(raw) {
    const settings = sanitizeSettings(raw);
    const profileRefs = new Set(
      settings.profiles.map((item) => item.profile_ref),
    );
    return (
      profileRefs.size >= 2
      && settings.profiles.every(
        (item) => item.dedicated_test && item.status === "healthy",
      )
      && settings.redis.aof_enabled
      && settings.redis.eviction_policy === "noeviction"
      && Boolean(settings.model.id)
      && settings.model.status === "passed"
      && settings.webhook.enabled
      && settings.webhook.status === "passed"
    );
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
    if (
      before.rollout_mode !== "enforce"
      && after.rollout_mode === "enforce"
    ) {
      if (!options?.preflight) {
        errors.push("preflight_required");
      } else if (
        !preflightPassed(options.preflight)
        || !enforceSettingsReady(after)
      ) {
        errors.push("preflight_failed");
      }
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

  function elementRequestStatusText(raw) {
    const requestType = safeCode(raw?.request_type) || "request";
    const status = safeCode(raw?.status) || "unknown";
    if (status === "publishing") {
      return `${requestType} · 原子发布/对账中`;
    }
    return `${requestType} · ${status}`;
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

  function migrationSafetyCopy() {
    return "保留当前 Locator；先进行仅观察运行；管理员确认语义契约；策略依赖保持不变；不会自动开启强制执行。";
  }

  function createSelectorProbeUI(dependencies) {
    const deps = dependencies || {};
    const state = {
      activeTab: "overview",
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
      settingsPreflight: null,
      settingsStatus: "",
      settingsProfileAdds: [],
      accounts: {items: [], error: "", loading: false},
      temporaryCredential: null,
      session: null,
      selected: null,
      operationWorkspace: null,
      pending: new Map(),
    };
    const generations = new Map();
    let initialized = false;
    let initPromise = null;
    let pollTimer = null;
    let elementSearchTimer = null;
    let elementRequestTimer = null;
    let workspaceGeneration = 0;
    let operationGeneration = 0;
    let runDetailTimer = null;
    let pendingSettingsSecrets = {};
    let pendingProfileAdds = [];
    let settingsPreflightBinding = null;
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
        const previousRevision = Number(state.settings?.revision);
        state.settings = Object.assign(sanitizeSettings(data), {revision});
        if (
          state.settingsDraft
          && Number.isInteger(state.settingsDraftBaseRevision)
          && state.settingsDraftBaseRevision !== revision
        ) {
          state.settingsDraftStale = true;
          state.settingsStatus = "settings_draft_stale_reload_required";
        }
        if (
          settingsPreflightBinding
          && Number.isFinite(previousRevision)
          && previousRevision !== revision
        ) {
          state.settingsPreflight = null;
          settingsPreflightBinding = null;
          if (!state.settingsDraftStale) {
            state.settingsStatus = "preflight_invalidated_revision_changed";
          }
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
      state.settingsPreflight = null;
      settingsPreflightBinding = null;
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
      state.selected = {
        resource: tabId,
        filters: clone(filters || {}),
      };
      if (tabId === "elements") {
        state.elements.filters = normalizedElementFilters(filters || {});
        state.elements.page = 1;
      } else if (state[tabId] && typeof state[tabId] === "object" && filters) {
        state[tabId].filters = Object.assign({}, filters);
      }
      return activateTab(tabId);
    }

    function invalidateElementWorkspace() {
      workspaceGeneration += 1;
      if (elementRequestTimer !== null && typeof deps.clearTimeout === "function") {
        deps.clearTimeout(elementRequestTimer);
      }
      elementRequestTimer = null;
      cancelPending("elementRequest");
      return workspaceGeneration;
    }

    function replaceElementWorkspace(value) {
      invalidateElementWorkspace();
      state.selected = value
        ? Object.assign({}, value, {workspaceGeneration})
        : null;
      return workspaceGeneration;
    }

    function currentElementWorkspace(generation, requestId) {
      if (
        destroyed
        || !state.selected
        || state.selected.workspaceGeneration !== generation
      ) return false;
      if (
        requestId
        && state.selected.request?.request_id !== requestId
      ) return false;
      return true;
    }

    function openElementWizard() {
      if (!canCreateElement(state.session)) return false;
      replaceElementWorkspace({
        kind: "wizard",
        step: 1,
        detail: null,
        suggestion: null,
        repairs: [],
        validation: null,
        request: null,
        validationReady: false,
        probeCompletedRevision: null,
        migrationDraft: false,
        error: "",
      });
      render("element-workspace");
      return true;
    }

    function openDiscoveryCandidate(raw) {
      if (!openElementWizard()) return false;
      const candidate = sanitizeDiscovery(raw);
      const label = candidate.name || candidate.role || "discovered element";
      state.selected.form = {
        displayName: label,
        intent: `locate ${label}`,
        requiredState: candidate.page_state,
        scope: candidate.scope,
        probeAction: (
          candidate.page_state === "feed_ready"
          && candidate.attributes["data-e2e"] === "comment-icon"
        ) ? "open_read_only" : "inspect_only",
        acceptedRoles: candidate.role ? [candidate.role] : [],
        acceptedNames: candidate.name ? [candidate.name] : [],
        preferredAttributes: Object.keys(candidate.attributes),
        nameMode: "exact",
        postcondition: (
          candidate.attributes["data-e2e"] === "comment-icon"
            ? "comment_panel_open"
            : ""
        ),
      };
      state.selected.suggestion = sanitizeProbeSuggestion({
        role: candidate.role,
        name: candidate.name,
        stable_attributes: Object.entries(candidate.attributes).map(
          ([name, value]) => ({name, value}),
        ),
        candidates: candidate.recommended_locators,
      });
      render("element-workspace");
      return true;
    }

    function closeElementWorkspace() {
      invalidateElementWorkspace();
      state.selected = null;
      render("element-workspace");
      return true;
    }

    function setElementWizardStep(step) {
      if (state.selected?.kind !== "wizard") return false;
      const selected = Math.min(Math.max(Number(step) || 1, 1), 3);
      if (selected === 3 && !canCreateElement(state.session)) return false;
      state.selected.step = selected;
      render("element-workspace");
      return true;
    }

    async function createElementDraft(form) {
      if (!canCreateElement(state.session) || state.selected?.kind !== "wizard") {
        return false;
      }
      const selected = state.selected;
      const generation = selected.workspaceGeneration;
      let payload;
      try {
        payload = serializeSemanticContract(form);
      } catch (error) {
        if (!currentElementWorkspace(generation)) return false;
        selected.error = error.message;
        render("element-workspace");
        return false;
      }
      const migrationDraft = selected.migrationDraft === true;
      const detail = selected.detail;
      const contract = {
        intent: payload.intent,
        required_state: payload.required_state,
        scope: payload.scope,
        probe_action: payload.probe_action,
        accepted_roles: payload.accepted_roles || ["button"],
        accepted_names: {
          mode: payload.name_mode || "exact",
          values: payload.accepted_names || [payload.display_name],
        },
        preferred_attributes: (
          payload.preferred_attributes || ["data-e2e", "aria-label"]
        ),
        postcondition: payload.postcondition || "",
      };
      const result = migrationDraft
        ? await request(
          `/api/selector-probe/elements/${encodeURIComponent(detail?.id || "")}/draft`,
          "PATCH",
          {expected_revision: detail?.revision, contract},
        )
        : await request("/api/selector-probe/elements", "POST", payload);
      if (!currentElementWorkspace(generation)) return false;
      if (
        (!migrationDraft && result.status !== 201)
        || (migrationDraft && result.status !== 200)
      ) {
        selected.error = errorMessage(result, "创建元素草稿失败");
        render("element-workspace");
        return false;
      }
      selected.detail = sanitizeElementDetail(result.data);
      selected.step = 2;
      selected.migrationDraft = false;
      selected.validationReady = false;
      selected.probeCompletedRevision = null;
      selected.error = "";
      render("element-workspace");
      return true;
    }

    function publicElementRequest(raw) {
      const source = raw && typeof raw === "object" ? raw : {};
      return {
        request_id: safeText(source.request_id, 128),
        request_type: safeCode(source.request_type),
        element_id: safeText(source.element_id, 128),
        status: safeCode(source.status) || "unknown",
        attempt_count: Number(source.attempt_count) || 0,
        error_code: safeCode(source.error_code),
      };
    }

    async function pollElementRequest(requestId, expectedWorkspaceGeneration) {
      const selectedRequest = safeText(requestId, 128);
      const generation = (
        expectedWorkspaceGeneration
        ?? state.selected?.workspaceGeneration
      );
      if (
        !selectedRequest
        || !currentElementWorkspace(generation, selectedRequest)
      ) return false;
      cancelPending("elementRequest");
      const requestGeneration = (generations.get("elementRequest") || 0) + 1;
      generations.set("elementRequest", requestGeneration);
      const controller = typeof deps.createAbortController === "function"
        ? deps.createAbortController()
        : null;
      state.pending.set("elementRequest", {
        generation: requestGeneration,
        controller,
        requestId: selectedRequest,
        workspaceGeneration: generation,
      });
      const result = await request(
        `/api/selector-probe/element-requests/${encodeURIComponent(selectedRequest)}`,
        "GET",
        undefined,
        controller ? {signal: controller.signal} : undefined,
      );
      if (
        generations.get("elementRequest") !== requestGeneration
        || !currentElementWorkspace(generation, selectedRequest)
      ) return false;
      const selected = state.selected;
      if (result.status !== 200) {
        state.pending.delete("elementRequest");
        selected.error = errorMessage(result, "读取请求状态失败");
        render("element-workspace");
        return false;
      }
      const publicRequest = publicElementRequest(result.data);
      if (publicRequest.request_id !== selectedRequest) {
        state.pending.delete("elementRequest");
        return false;
      }
      selected.request = publicRequest;
      if ([
        "pending",
        "processing",
        "retrying",
        "publishing",
      ].includes(publicRequest.status)) {
        state.pending.delete("elementRequest");
        if (typeof deps.setTimeout === "function") {
          elementRequestTimer = deps.setTimeout(
            () => {
              elementRequestTimer = null;
              return pollElementRequest(selectedRequest, generation);
            },
            1000,
          );
        }
        render("element-workspace");
        return true;
      }
      if (publicRequest.status === "completed") {
        if (publicRequest.request_type === "probe") {
          const probeResult = result.data?.result || {};
          const candidate = probeResult.candidate || {};
          const locators = Array.isArray(candidate.locators)
            ? candidate.locators
            : probeResult.suggestion?.candidates;
          const roleLocator = (locators || []).find(
            (locator) => locator?.type === "role",
          );
          selected.suggestion = sanitizeProbeSuggestion({
            ...(probeResult.suggestion || {}),
            role: probeResult.suggestion?.role || roleLocator?.role,
            name: probeResult.suggestion?.name || roleLocator?.name,
            stable_attributes: (
              probeResult.suggestion?.stable_attributes
              || (locators || [])
                .filter((locator) => locator?.type === "attribute")
                .map((locator) => ({
                  name: locator.name,
                  value: locator.value,
                }))
            ),
            candidates: locators,
            llm_used: (
              probeResult.suggestion?.llm_used === true
              || (probeResult.repairs || []).length > 0
            ),
          });
          selected.repairs = sanitizeRepairs(probeResult.repairs);
          selected.step = 2;
        } else if (publicRequest.request_type === "validate") {
          const validation = result.data?.result?.validation || result.data?.result || {};
          const rounds = Array.isArray(validation.rounds)
            ? validation.rounds.map(safeValidationRound).filter(Boolean)
            : [];
          selected.validation = {
            status: safeCode(validation.status) || "unknown",
            rounds,
            summary: summarizeValidation(rounds),
            version_id: safeText(
              validation.version_id
              || validation.new_version
              || validation.version,
              128,
            ),
          };
          selected.step = 3;
        }
      }
      const elementId = selected.detail?.id || publicRequest.element_id;
      if (!elementId) {
        state.pending.delete("elementRequest");
        selected.validationReady = false;
        selected.error = "请求完成，但无法刷新元素版本";
        render("element-workspace");
        return false;
      }
      const detailResult = await request(
        `/api/selector-probe/elements/${encodeURIComponent(elementId)}`,
        "GET",
        undefined,
        controller ? {signal: controller.signal} : undefined,
      );
      if (
        generations.get("elementRequest") !== requestGeneration
        || !currentElementWorkspace(generation, selectedRequest)
      ) return false;
      state.pending.delete("elementRequest");
      if (detailResult.status !== 200) {
        selected.validationReady = false;
        selected.error = errorMessage(detailResult, "刷新元素版本失败");
        render("element-workspace");
        return false;
      }
      selected.detail = sanitizeElementDetail(detailResult.data);
      selected.validationReady = (
        publicRequest.request_type === "probe"
        && publicRequest.status === "completed"
      );
      selected.probeCompletedRevision = selected.validationReady
        ? selected.detail.revision
        : null;
      if (publicRequest.status !== "completed") {
        selected.error = publicRequest.error_code || "元素请求未完成";
      }
      render("element-workspace");
      return true;
    }

    async function startElementRequest(requestType) {
      const selected = state.selected;
      const element = selected?.detail;
      if (!element?.id) return false;
      const generation = selected.workspaceGeneration;
      if (requestType === "validate" && !canCreateElement(state.session)) {
        return false;
      }
      if (
        requestType === "validate"
        && (
          selected.validationReady !== true
          || selected.probeCompletedRevision !== element.revision
        )
      ) return false;
      const expectedRevision = Number(element.revision);
      if (!Number.isInteger(expectedRevision) || expectedRevision < 0) return false;
      const result = await request(
        `/api/selector-probe/elements/${encodeURIComponent(element.id)}/${requestType}`,
        "POST",
        {expected_revision: expectedRevision},
      );
      if (!currentElementWorkspace(generation)) return false;
      if (result.status !== 202) {
        selected.error = errorMessage(result, `${requestType} 请求失败`);
        render("element-workspace");
        return false;
      }
      selected.request = publicElementRequest({
        ...result.data,
        request_type: requestType,
        element_id: element.id,
      });
      const managedRevision = Number(result.data?.expected_revision);
      if (Number.isInteger(managedRevision) && managedRevision >= 0) {
        selected.detail.revision = managedRevision;
      }
      selected.validationReady = false;
      selected.probeCompletedRevision = null;
      if (requestType === "validate") selected.step = 3;
      selected.error = "";
      render("element-workspace");
      return pollElementRequest(result.data.request_id, generation);
    }

    function requestElementProbe() {
      return startElementRequest("probe");
    }

    function requestElementValidation() {
      return startElementRequest("validate");
    }

    async function openElementDetail(elementId) {
      const id = safeText(elementId, 128);
      if (!id) return false;
      const generation = invalidateElementWorkspace();
      const result = await request(
        `/api/selector-probe/elements/${encodeURIComponent(id)}`,
        "GET",
      );
      if (destroyed || generation !== workspaceGeneration || result.status !== 200) {
        return false;
      }
      state.selected = {
        kind: "detail",
        activeDetailTab: "evidence",
        detail: sanitizeElementDetail(result.data),
        request: null,
        validation: null,
        validationReady: false,
        probeCompletedRevision: null,
        error: "",
        workspaceGeneration: generation,
      };
      render("element-workspace");
      return true;
    }

    function activateElementDetailTab(tabId) {
      if (!DETAIL_TABS.includes(tabId) || state.selected?.kind !== "detail") {
        return false;
      }
      state.selected.activeDetailTab = tabId;
      render("element-workspace");
      return true;
    }

    function openLegacyMigration(element) {
      if (!canCreateElement(state.session)) return false;
      const detail = sanitizeElementDetail(element);
      if (detail.migration_available !== true) return false;
      replaceElementWorkspace({
        kind: "migration",
        detail,
        safetyCopy: migrationSafetyCopy(),
        error: "",
      });
      render("element-workspace");
      return true;
    }

    async function requestLegacyMigration() {
      if (
        !canCreateElement(state.session)
        || state.selected?.kind !== "migration"
      ) return false;
      const selected = state.selected;
      const generation = selected.workspaceGeneration;
      const detail = selected.detail;
      const result = await request(
        `/api/selector-probe/elements/${encodeURIComponent(detail.id)}/migrate`,
        "POST",
        {expected_revision: detail.revision},
      );
      if (!currentElementWorkspace(generation)) return false;
      if (result.status !== 200) {
        selected.error = errorMessage(result, "加入自动管理失败");
        render("element-workspace");
        return false;
      }
      replaceElementWorkspace({
        kind: "wizard",
        step: 1,
        detail: sanitizeElementDetail(result.data),
        suggestion: null,
        repairs: [],
        request: null,
        validation: null,
        validationReady: false,
        probeCompletedRevision: null,
        migrationDraft: true,
        error: "",
      });
      render("element-workspace");
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
      state.activeTab = "runs";
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

    function writeOnlySettings(raw) {
      const source = raw && typeof raw === "object" ? raw : {};
      const result = {};
      for (const name of [
        "model_api_key",
        "redis_password",
        "webhook_signing_secret",
        "webhook_url",
      ]) {
        const value = safeText(source[name], 2000);
        if (value) result[name] = value;
      }
      return result;
    }

    async function requestSettingsPreflight(raw) {
      if (state.session?.role !== "administrator" || !state.settings) {
        return false;
      }
      if (state.settingsDraftStale) {
        state.settingsStatus = "settings_draft_stale_reload_required";
        render("settings");
        return false;
      }
      const candidate = sanitizeSettings(raw);
      const baseRevision = Number.isInteger(state.settingsDraftBaseRevision)
        ? state.settingsDraftBaseRevision
        : state.settings.revision;
      const candidateFingerprint = settingsFingerprint(candidate);
      state.settingsStatus = "preflight_running";
      render("settings");
      const result = await request(
        "/api/selector-probe/settings/preflight",
        "POST",
        {
          expected_revision: baseRevision,
          candidate_fingerprint: candidateFingerprint,
          settings: settingsMutationPayload(candidate),
        },
      );
      if (result.status !== 200) {
        state.settingsPreflight = null;
        settingsPreflightBinding = null;
        state.settingsStatus = operationError(
          result,
          "settings_preflight_unavailable",
        );
        render("settings");
        return false;
      }
      const preflight = sanitizePreflight(result.data);
      const validBinding = (
        preflight.base_revision === baseRevision
        && preflight.candidate_fingerprint === candidateFingerprint
        && Boolean(preflight.preflight_token)
        && Boolean(preflight.checked_at)
      );
      if (!validBinding) {
        state.settingsPreflight = null;
        settingsPreflightBinding = null;
        state.settingsStatus = "settings_preflight_binding_invalid";
        render("settings");
        return false;
      }
      state.settingsPreflight = preflight;
      settingsPreflightBinding = {
        baseRevision,
        candidateFingerprint,
        preflightToken: preflight.preflight_token,
        checkedAt: preflight.checked_at,
      };
      state.settingsStatus = state.settingsPreflight.status;
      render("settings");
      return preflightPassed(state.settingsPreflight);
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
      const preflightMatches = (
        settingsPreflightBinding
        && settingsPreflightBinding.baseRevision === state.settings.revision
        && settingsPreflightBinding.candidateFingerprint
          === settingsFingerprint(candidate)
        && settingsPreflightBinding.preflightToken
          === state.settingsPreflight?.preflight_token
        && settingsPreflightBinding.checkedAt
          === state.settingsPreflight?.checked_at
      );
      const validation = validateSettingsSave(
        state.settings,
        candidate,
        {
          reason: selectedReason,
          preflight: preflightMatches ? state.settingsPreflight : null,
        },
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
        if (
          candidate.rollout_mode === "enforce"
          && !validation.errors.includes("preflight_failed")
        ) validation.errors.push("preflight_failed");
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
        baseRevision: settingsPreflightBinding?.baseRevision
          ?? state.settingsDraftBaseRevision
          ?? state.settings.revision,
        candidateFingerprint: settingsPreflightBinding?.candidateFingerprint
          || settingsFingerprint(candidate),
        preflightToken: settingsPreflightBinding?.preflightToken || "",
        preflightCheckedAt: settingsPreflightBinding?.checkedAt || "",
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
      if (workspace.preflightToken) {
        body.preflight_token = workspace.preflightToken;
        body.candidate_fingerprint = workspace.candidateFingerprint;
        body.preflight_checked_at = workspace.preflightCheckedAt;
      }
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
      state.settingsPreflight = null;
      settingsPreflightBinding = null;
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
          "model_api_key",
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
      if (!TAB_IDS.has(tabId)) throw new Error(`unknown selector probe tab: ${tabId}`);
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
      stopRunDetailPolling();
      invalidateElementWorkspace();
      state.selected = null;
      state.operationWorkspace = null;
      state.temporaryCredential = null;
      state.settingsDraft = null;
      state.settingsDraftBaseRevision = null;
      state.settingsDraftStale = false;
      state.settingsPreflight = null;
      state.settingsStatus = "";
      settingsPreflightBinding = null;
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
      openElementWizard,
      openDiscoveryCandidate,
      closeElementWorkspace,
      setElementWizardStep,
      createElementDraft,
      requestElementProbe,
      requestElementValidation,
      pollElementRequest,
      openElementDetail,
      activateElementDetailTab,
      openLegacyMigration,
      requestLegacyMigration,
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
      requestSettingsPreflight,
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

  function locatorDescription(locator) {
    if (locator.type === "role") {
      return `Role ${locator.role || "未知"} · Name ${locator.name || "未设置"} · ${locator.name_mode || "exact"}`;
    }
    if (locator.type === "attribute") {
      return `属性 ${locator.name || "未知"}=${locator.value || "未设置"}`;
    }
    return `${String(locator.type || "locator").toUpperCase()} ${locator.value || "未设置"}`;
  }

  function renderLocatorRows(document, containerId, groups) {
    const container = document.querySelector(`#${containerId}`);
    if (!container) return;
    const rows = [];
    groups.forEach(({label, items}) => {
      (items || []).forEach((locator, index) => {
        const row = document.createElement("article");
        row.className = "selector-detail-row";
        row.append(
          createTextElement(document, "strong", `${label} ${index + 1}`),
          createTextElement(document, "span", locatorDescription(locator)),
          createTextElement(
            document,
            "span",
            locator.fallback ? "↪ 回退候选" : "● 主候选",
            "muted",
          ),
        );
        rows.push(row);
      });
    });
    if (!rows.length) {
      const empty = document.createElement("article");
      empty.className = "selector-detail-row";
      empty.textContent = "暂无证据";
      rows.push(empty);
    }
    container.replaceChildren(...rows);
  }

  function renderValidationMatrixInto(document, rounds, containerId) {
    const container = document.querySelector(`#${containerId}`);
    if (!container) return;
    const safeRounds = (Array.isArray(rounds) ? rounds : [])
      .map(safeValidationRound)
      .filter(Boolean);
    container.replaceChildren(
      ...(safeRounds.length ? safeRounds.map((round) => {
        const row = document.createElement("article");
        row.className = "selector-validation-cell";
        row.append(
          createTextElement(
            document,
            "strong",
            `${round.profile_mask} · 第 ${round.round} 轮`,
          ),
          createTextElement(
            document,
            "span",
            `${round.status === "passed" ? "● 通过" : "■ 未通过"} · 匹配 ${round.match_count ?? "—"}`,
          ),
          createTextElement(
            document,
            "span",
            [
              round.role_name_result,
              round.visibility_result,
              round.actionability_result,
              round.postcondition_result,
              typeof round.visible === "boolean"
                ? `可见 ${round.visible ? "通过" : "失败"}`
                : "",
              typeof round.in_viewport === "boolean"
                ? `视口内 ${round.in_viewport ? "通过" : "失败"}`
                : "",
              typeof round.actionable === "boolean"
                ? `可操作 ${round.actionable ? "通过" : "失败"}`
                : "",
              round.failure_code,
            ].filter(Boolean).join(" · "),
            "muted",
          ),
        );
        return row;
      }) : [createTextElement(document, "article", "暂无证据", "selector-validation-cell")]),
    );
  }

  function renderValidationMatrix(document, rounds) {
    renderValidationMatrixInto(
      document,
      rounds,
      "selector-element-validation-matrix",
    );
  }

  function renderElementDetail(document, detail) {
    const safe = sanitizeElementDetail(detail);
    setNodeText(document, "selector-element-detail-title", safe.display_name || "元素详情");
    setNodeText(document, "selector-element-detail-alias", safe.id);
    setNodeText(
      document,
      "selector-element-detail-status",
      `${safe.published_status || "unknown"}${safe.draft_status ? ` · ${safe.draft_status}` : ""}`,
    );
    setNodeText(
      document,
      "selector-element-detail-version",
      safe.active_version || "尚无版本",
    );
    setNodeText(
      document,
      "selector-element-detail-dependencies",
      safe.dependency_count,
    );
    setNodeText(
      document,
      "selector-element-detail-last-validation",
      safe.last_validated_at || "尚未验证",
    );
    renderLocatorRows(document, "selector-element-detail-evidence", [
      {
        label: "Active",
        items: safe.candidate_comparison.active,
      },
    ]);
    renderLocatorRows(document, "selector-element-detail-candidates", [
      {label: "Active", items: safe.candidate_comparison.active},
      {label: "Deterministic", items: safe.candidate_comparison.deterministic},
      {label: "Repaired", items: safe.candidate_comparison.repaired},
    ]);
    const repairs = document.querySelector("#selector-element-detail-repairs");
    if (repairs) {
      repairs.replaceChildren(
        ...(safe.repairs.length ? safe.repairs.map((repair) => {
          const row = document.createElement("article");
          row.className = "selector-detail-row";
          row.append(
            createTextElement(document, "strong", `修正尝试 ${repair.attempt}`),
            createTextElement(
              document,
              "span",
              `${repair.previous_method || "unknown"} → ${repair.new_method || "unknown"}`,
            ),
            createTextElement(
              document,
              "span",
              `${repair.failure_code || "无失败代码"} · 匹配 ${repair.match_count ?? "—"} · ${repair.validation_result || "未验证"}`,
              "muted",
            ),
            createTextElement(
              document,
              "span",
              `${repair.prompt_version || "无 Prompt 版本"} · ${repair.model_id || "无模型 ID"}`,
              "muted",
            ),
          );
          return row;
        }) : [createTextElement(document, "article", "暂无证据", "selector-detail-row")]),
      );
    }
    const history = document.querySelector("#selector-element-detail-history");
    if (history) {
      history.replaceChildren(
        ...(safe.history.length ? safe.history.map((version) => {
          const button = document.createElement("button");
          button.type = "button";
          button.className = "selector-detail-row";
          button.dataset.selectorSummaryTab = "versions";
          button.dataset.selectorSummaryFilter = "element_id";
          button.dataset.selectorSummaryValue = safe.id;
          button.textContent = `${version.version_id} · ${version.status || "unknown"} · ${version.published_at || "未发布"}`;
          return button;
        }) : [createTextElement(document, "article", "暂无证据", "selector-detail-row")]),
      );
    }
    renderValidationMatrix(document, safe.evidence.rounds);
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

  function runDescription(run) {
    const profiles = run.profiles.map(
      (profile) => `${profile.profile_mask} ${profile.status}`,
    ).join(" / ");
    const stages = run.stages.map(
      (stage) => `${stage.name} ${stage.status}`
        + `${stage.duration_ms !== null ? ` ${stage.duration_ms}ms` : ""}`,
    ).join(" / ");
    const elements = run.elements.map(
      (item) => `${item.alias} ${item.status}`
        + `${item.failure_class ? ` ${item.failure_class}` : ""}`
        + ` repairs=${item.repair_attempt_count}`,
    ).join(" / ");
    return [
      `${run.trigger || "unknown"} · actor ${run.trigger_actor || "系统"} · ${run.due_slot || "无 due slot"} · ${run.rollout_mode || "无 rollout"}`,
      profiles || "暂无 Profile 证据",
      run.rounds.length
        ? run.rounds.map(
          (round) => `${round.profile_mask} 第 ${round.round} 轮 ${round.status}`,
        ).join(" / ")
        : "暂无轮次证据",
      stages || "暂无阶段证据",
      elements || (
        run.failed_aliases.length
          ? `失败元素 ${run.failed_aliases.join(", ")}`
          : "暂无元素结果"
      ),
      run.repairs.length
        ? run.repairs.map(
          (repair) => `repair ${repair.attempt} · ${repair.failure_code || "unknown"} · ${repair.validation_result || "unknown"}`,
        ).join(" / ")
        : "repairs=0",
      operationStateText("publish", run.publication),
      operationStateText("reconcile", run.reconciliation),
      operationStateText("cleanup", run.cleanup),
      operationStateText("lease", run.lease),
      run.retry_delay_minutes
        ? `基础设施失败，${run.retry_delay_minutes} 分钟后重试`
        : (run.next_retry_at ? `下次重试 ${run.next_retry_at}` : ""),
    ].filter(Boolean);
  }

  function renderRuns(document, state) {
    const runs = (state?.runs?.items || []).map(sanitizeRun);
    const rows = document.querySelector("#selector-run-rows");
    if (rows) {
      rows.replaceChildren(
        ...(runs.length ? runs.map((run) => {
          const row = document.createElement("article");
          row.className = "selector-operation-row";
          row.dataset.runId = run.id;
          row.append(
            createTextElement(
              document,
              "strong",
              `${run.id || "未知运行"} · ${run.status}`,
            ),
            ...runDescription(run).map((line) => (
              createTextElement(document, "span", line, "muted")
            )),
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
      `model ${version.model_id || "无"} · prompt ${version.prompt_version || "无"}`,
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
      `repairs=${alert.repairs.length}`,
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
    replaceSimpleRows(document, "#selector-settings-model", [
      `${settings.model.id || "未选择"} · ${settings.model.provider || "unknown"}`,
      `${settings.model.mode || "unknown"} · ${settings.model.status}`,
      `API Key: ${settings.model.api_key_set ? "已设置" : "未设置"}`,
      "LLM 仅用于修正，不参与首轮确定性候选生成。",
    ]);
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
      "administrator: 读取、设置、预检、秘密清除、账户管理、Webhook 测试",
      "operator: 只读设置、立即探测、确认告警、Webhook synthetic 测试",
      "后端权限策略为最终依据。",
    ]);
    const save = document.querySelector("#selector-settings-save");
    const preflight = document.querySelector("#selector-settings-preflight");
    const reload = document.querySelector("#selector-settings-reload");
    const webhookTest = document.querySelector(
      "#selector-settings-webhook-test",
    );
    if (save) save.hidden = !permissions.canEdit;
    if (preflight) preflight.hidden = !permissions.canEdit;
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
          "modelApiKey",
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
        setValue("freshnessHours", settings.freshness_hours);
        setValue("modelId", settings.model.id);
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

  function renderElementWorkspace(document, state) {
    const selected = state?.selected;
    const directory = document.querySelector("#selector-element-directory");
    const detailView = document.querySelector("#selector-element-detail");
    const wizard = document.querySelector("#selector-element-wizard");
    const migration = document.querySelector(
      "#selector-element-migration-dialog",
    );
    const showingDetail = selected?.kind === "detail";
    if (directory) directory.hidden = showingDetail;
    if (detailView) detailView.hidden = !showingDetail;

    if (showingDetail) {
      renderElementDetail(document, selected.detail);
      const activeTab = DETAIL_TABS.includes(selected.activeDetailTab)
        ? selected.activeDetailTab
        : "evidence";
      document.querySelectorAll("[data-selector-detail-tab]").forEach((button) => {
        const active = button.dataset.selectorDetailTab === activeTab;
        button.classList.toggle("active", active);
        button.setAttribute("aria-pressed", String(active));
      });
      document.querySelectorAll("[data-selector-detail-panel]").forEach((panel) => {
        panel.hidden = panel.dataset.selectorDetailPanel !== activeTab;
      });
      const validate = document.querySelector("#selector-element-detail-validate");
      if (validate) {
        validate.hidden = !canCreateElement(state.session);
        validate.disabled = selected.validationReady !== true;
      }
      setNodeText(
        document,
        "selector-element-detail-error",
        selected.error || selected.request?.error_code || "",
      );
    }

    const showingWizard = selected?.kind === "wizard";
    if (wizard) {
      if (showingWizard && !wizard.open && typeof wizard.showModal === "function") {
        wizard.showModal();
      } else if (!showingWizard && wizard.open && typeof wizard.close === "function") {
        wizard.close();
      }
    }
    if (showingWizard) {
      const step = Math.min(Math.max(Number(selected.step) || 1, 1), 3);
      setNodeText(document, "selector-element-wizard-progress", `第 ${step} / 3 步`);
      document.querySelectorAll("[data-selector-wizard-step]").forEach((panel) => {
        panel.hidden = Number(panel.dataset.selectorWizardStep) !== step;
      });
      const back = document.querySelector("#selector-element-wizard-back");
      const next = document.querySelector("#selector-element-wizard-next");
      const probe = document.querySelector("#selector-element-wizard-probe");
      const validate = document.querySelector("#selector-element-wizard-validate");
      if (back) back.hidden = step === 1;
      if (next) next.hidden = step !== 1;
      if (probe) probe.hidden = step !== 2;
      if (validate) {
        validate.hidden = (
          !canCreateElement(state.session)
          || (step !== 2 && step !== 3)
        );
        validate.disabled = selected.validationReady !== true;
      }
      const wizardForm = document.querySelector(
        "#selector-element-wizard-form",
      );
      if (
        wizardForm
        && selected.form
        && wizardForm.dataset.discoveryGeneration
          !== String(selected.workspaceGeneration)
      ) {
        Object.entries(selected.form).forEach(([name, value]) => {
          const control = wizardForm.elements?.namedItem(name);
          if (control) {
            control.value = Array.isArray(value) ? value.join(", ") : value;
          }
        });
        wizardForm.dataset.discoveryGeneration = String(
          selected.workspaceGeneration,
        );
      }
      const suggestion = selected.suggestion || sanitizeProbeSuggestion({});
      setNodeText(document, "selector-wizard-role", suggestion.role || "等待探测");
      setNodeText(document, "selector-wizard-name", suggestion.name || "等待探测");
      setNodeText(
        document,
        "selector-wizard-llm-used",
        suggestion.llm_used ? "是（仅修复）" : "否",
      );
      replaceSimpleRows(
        document,
        "#selector-wizard-attributes",
        suggestion.stable_attributes.map(
          (attribute) => `${attribute.name}=${attribute.value}`,
        ),
      );
      renderLocatorRows(document, "selector-wizard-candidates", [
        {label: "候选", items: suggestion.candidates},
      ]);
      replaceSimpleRows(
        document,
        "#selector-wizard-rejected",
        suggestion.rejected_methods.map(
          (item) => `${item.method} · ${item.code}`,
        ).concat(suggestion.warnings),
      );
      renderValidationMatrixInto(
        document,
        selected.validation?.rounds || [],
        "selector-element-wizard-validation-matrix",
      );
      setNodeText(
        document,
        "selector-element-wizard-status",
        selected.error
        || (
          selected.request
            ? elementRequestStatusText(selected.request)
            : ""
        ),
      );
    } else {
      const form = document.querySelector("#selector-element-wizard-form");
      if (form && typeof form.reset === "function") form.reset();
      for (const id of [
        "#selector-wizard-attributes",
        "#selector-wizard-candidates",
        "#selector-wizard-rejected",
        "#selector-element-wizard-validation-matrix",
      ]) {
        const node = document.querySelector(id);
        if (node && typeof node.replaceChildren === "function") {
          node.replaceChildren();
        }
      }
    }

    const showingMigration = selected?.kind === "migration";
    if (migration) {
      if (
        showingMigration
        && !migration.open
        && typeof migration.showModal === "function"
      ) {
        migration.showModal();
      } else if (
        !showingMigration
        && migration.open
        && typeof migration.close === "function"
      ) {
        migration.close();
      }
    }
    if (showingMigration) {
      setNodeText(
        document,
        "selector-element-migration-copy",
        selected.safetyCopy || migrationSafetyCopy(),
      );
      setNodeText(
        document,
        "selector-element-migration-error",
        selected.error || "",
      );
    }
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
    if (workspace?.kind === "run-detail") {
      return runDescription(sanitizeRun(detail));
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
        alert.repairs.map(
          (item) => `repair ${item.attempt} · ${item.failure_code || "unknown"} · ${item.validation_result || "unknown"}`,
        ),
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
        `Profiles ${settings.profiles.map((item) => item.profile_mask).join(", ") || "无"}`,
        `Redis ${settings.redis.status} · AOF ${settings.redis.aof_enabled} · ${settings.redis.eviction_policy || "未确认"}`,
        `Model ${settings.model.id || "未设置"} · ${settings.model.status}`,
        `Webhook ${settings.webhook.enabled} · ${settings.webhook.status}`,
        workspace.outcome || "",
      ].filter(Boolean);
    }
    return [];
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
      const discoveryDetail = document.querySelector(
        "#selector-run-discoveries",
      );
      if (workspace.kind === "run-detail") {
        const run = sanitizeRun(workspace.detail);
        if (stageDetail) {
          stageDetail.replaceChildren(
            ...run.stages.map((stage) => createTextElement(
              document,
              "article",
              `${stage.name} · ${stage.status || "unknown"}${
                stage.round ? ` · 第 ${stage.round} 轮` : ""
              }${
                stage.attempt_count
                  ? ` · 尝试 ${stage.attempt_count}`
                  : ""
              }${
                stage.failure_code ? ` · ${stage.failure_code}` : ""
              }`,
              "selector-discovery-row",
            )),
          );
        }
        if (discoveryDetail) {
          discoveryDetail.replaceChildren(
            ...run.discoveries.map((candidate) => {
              const row = document.createElement("article");
              row.className = "selector-discovery-row";
              row.append(
                createTextElement(
                  document,
                  "strong",
                  `${candidate.page_state} · ${candidate.role} · ${
                    candidate.name || "unnamed"
                  }`,
                ),
                createTextElement(
                  document,
                  "span",
                  Object.entries(candidate.attributes)
                    .map(([key, value]) => `${key}=${value}`)
                    .join(" · ") || "无稳定属性",
                  "muted",
                ),
                createTextElement(
                  document,
                  "span",
                  candidate.recommended_locators.length
                    ? `定位路径：${candidate.recommended_locators
                      .map(locatorDescription)
                      .join("；")}`
                    : "定位路径：暂无安全候选",
                  "muted",
                ),
                createTextElement(
                  document,
                  "span",
                  `Profile ${candidate.profile_count} · ${
                    candidate.actionable ? "可操作" : "仅可见"
                  }`,
                  "muted",
                ),
              );
              if (canCreateElement(state.session)) {
                const add = document.createElement("button");
                add.type = "button";
                add.dataset.discoveryFingerprint = candidate.fingerprint;
                add.textContent = "加入元素目录";
                row.append(add);
              }
              return row;
            }),
          );
        }
      } else {
        stageDetail?.replaceChildren();
        discoveryDetail?.replaceChildren();
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
      document.querySelector("#selector-run-discoveries")?.replaceChildren();
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
      "#selector-element-wizard",
      "#selector-element-migration-dialog",
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
      document.querySelector("#selector-element-add")?.addEventListener(
        "click",
        () => controller.openElementWizard(),
      );
      document.querySelector("#selector-element-rows")?.addEventListener(
        "click",
        (event) => {
          const open = event.target.closest?.("[data-selector-element-id]");
          if (open) {
            controller.openElementDetail(open.dataset.selectorElementId);
            return;
          }
          const migrate = event.target.closest?.("[data-selector-migrate-id]");
          if (migrate) {
            const item = controller.state.elements.items.find(
              (entry) => entry.id === migrate.dataset.selectorMigrateId,
            );
            if (item) controller.openLegacyMigration(item);
          }
        },
      );
      const wizardForm = document.querySelector("#selector-element-wizard-form");
      document.querySelector("#selector-element-wizard-next")?.addEventListener(
        "click",
        () => controller.createElementDraft(wizardForm),
      );
      document.querySelector("#selector-element-wizard-back")?.addEventListener(
        "click",
        () => controller.setElementWizardStep(
          (controller.state.selected?.step || 1) - 1,
        ),
      );
      document.querySelector("#selector-element-wizard-probe")?.addEventListener(
        "click",
        () => controller.requestElementProbe(),
      );
      document.querySelector("#selector-element-wizard-validate")?.addEventListener(
        "click",
        () => controller.requestElementValidation(),
      );
      document.querySelector("#selector-element-detail-probe")?.addEventListener(
        "click",
        () => controller.requestElementProbe(),
      );
      document.querySelector("#selector-element-detail-validate")?.addEventListener(
        "click",
        () => controller.requestElementValidation(),
      );
      document.querySelector("#selector-element-detail-back")?.addEventListener(
        "click",
        () => controller.closeElementWorkspace(),
      );
      document.querySelectorAll("[data-selector-detail-tab]").forEach((button) => {
        button.addEventListener(
          "click",
          () => controller.activateElementDetailTab(
            button.dataset.selectorDetailTab,
          ),
        );
      });
      for (const id of [
        "#selector-element-wizard-close",
        "#selector-element-migration-close",
        "#selector-element-migration-cancel",
      ]) {
        document.querySelector(id)?.addEventListener(
          "click",
          () => controller.closeElementWorkspace(),
        );
      }
      const workspaceDialogs = [
        {
          selector: "#selector-element-wizard",
          kind: "wizard",
        },
        {
          selector: "#selector-element-migration-dialog",
          kind: "migration",
        },
      ];
      workspaceDialogs.forEach(({selector, kind}) => {
        const dialog = document.querySelector(selector);
        dialog?.addEventListener("cancel", (event) => {
          event.preventDefault();
          if (controller.state.selected?.kind === kind) {
            controller.closeElementWorkspace();
          }
        });
        dialog?.addEventListener("close", () => {
          if (controller.state.selected?.kind === kind) {
            controller.closeElementWorkspace();
          }
        });
      });
      document.querySelector("#selector-element-migration-confirm")?.addEventListener(
        "click",
        () => controller.requestLegacyMigration(),
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
      document.querySelector("#selector-element-detail-history")?.addEventListener(
        "click",
        summaryClick,
      );
      document.querySelector("#selector-run-now")?.addEventListener(
        "click",
        () => controller.requestRunNow(),
      );
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
      document.querySelector("#selector-run-discoveries")
        ?.addEventListener("click", (event) => {
          const button = event.target.closest?.(
            "[data-discovery-fingerprint]",
          );
          if (!button) return;
          const run = sanitizeRun(
            controller.state.operationWorkspace?.detail,
          );
          const candidate = run.discoveries.find(
            (item) => (
              item.fingerprint === button.dataset.discoveryFingerprint
            ),
          );
          if (candidate) controller.openDiscoveryCandidate(candidate);
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
          freshness_hours: Number(
            settingsFormControl("freshnessHours")?.value || 36,
          ),
          model: Object.assign({}, current.model, {
            id: settingsFormControl("modelId")?.value || "",
          }),
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
          model_api_key: settingsFormControl("modelApiKey")?.value || "",
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
      document.querySelector("#selector-settings-preflight")?.addEventListener(
        "click",
        () => controller.requestSettingsPreflight(settingsCandidateFromForm()),
      );
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
        renderElementWorkspace(document, state);
        renderGates(document, state);
        renderRuns(document, state);
        renderVersions(document, state);
        renderAlerts(document, state);
        renderSettings(document, state);
        renderAccountManagement(document, state);
        renderOperationWorkspace(document, state);
        renderTemporaryPassword(document, state);
        const runNow = document.querySelector("#selector-run-now");
        if (runNow) {
          runNow.hidden = !["administrator", "operator"].includes(
            state.session?.role,
          );
          runNow.disabled = (
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
        }
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
    buildElementQuery,
    canCreateElement,
    clearSettingsFormSecrets,
    createSelectorProbeUI,
    dangerousSettingsDiff,
    elementRequestStatusText,
    migrationSafetyCopy,
    normalizeTargetOrigin,
    parseProfileIds,
    manualResumeOutcome,
    operationConfirmationIsDangerous,
    renderAlerts,
    renderAccountManagement,
    renderOperationWorkspace,
    renderElementDirectory,
    renderElementDetail,
    renderGates,
    renderOverview,
    renderRuns,
    renderSettings,
    renderTemporaryPassword,
    renderValidationMatrix,
    renderVersions,
    sanitizeAlert,
    sanitizeAccount,
    sanitizeElementDetail,
    sanitizeGate,
    sanitizeProbeSuggestion,
    sanitizeDiscovery,
    sanitizeRun,
    sanitizeSettings,
    sanitizeStructuredLocators,
    sanitizeVersion,
    selectOverviewElements,
    serializeSemanticContract,
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
