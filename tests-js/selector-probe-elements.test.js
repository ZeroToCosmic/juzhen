const assert = require("node:assert/strict");
const test = require("node:test");

const {
  buildElementQuery,
  canCreateElement,
  createSelectorProbeUI,
  elementRequestStatusText,
  renderElementDirectory,
  renderElementDetail,
  renderOverview,
  renderValidationMatrix,
  sanitizeElementDetail,
  sanitizeProbeSuggestion,
  sanitizeStructuredLocators,
  selectOverviewElements,
  selectorProbeDependencies,
  serializeSemanticContract,
  serializeElementFilters,
  summarizeValidation,
  migrationSafetyCopy,
} = require("../gateway/static/selector_probe_ui");

function response(data) {
  return {status: 200, data};
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return {promise, resolve};
}

function node(ownerDocument) {
  return {
    ownerDocument,
    children: [],
    dataset: {},
    attributes: {},
    hidden: false,
    textContent: "",
    append(...children) {
      this.children.push(...children);
    },
    replaceChildren(...children) {
      this.children = children;
    },
    setAttribute(name, value) {
      this.attributes[name] = String(value);
    },
  };
}

function fakeDocument(ids) {
  const nodes = new Map();
  const document = {
    createElement() {
      return node(document);
    },
    querySelector(selector) {
      return selector.startsWith("#") ? nodes.get(selector.slice(1)) || null : null;
    },
  };
  ids.forEach((id) => nodes.set(id, node(document)));
  return {document, nodes};
}

test("overview selects five elements by unhealthy priority without fixed IDs", () => {
  const items = [
    {id: "healthy", published_status: "healthy"},
    {id: "draft", published_status: "healthy", draft_status: "draft"},
    {id: "lkg", published_status: "using_lkg"},
    {id: "failed", published_status: "failed"},
    {id: "unavailable", published_status: "probe_unavailable"},
    {id: "old", published_status: "healthy"},
  ];

  assert.deepEqual(
    selectOverviewElements(items).map((item) => item.id),
    ["failed", "lkg", "draft", "unavailable", "old"],
  );
});

test("element query bounds page size and encodes every server filter", () => {
  assert.equal(
    buildElementQuery({
      page: 2,
      pageSize: 50,
      search: "评论 入口",
      status: "failed",
      source: "automatic",
      scope: "active_video",
      referenced: "yes",
    }),
    "?page=2&page_size=50&search=%E8%AF%84%E8%AE%BA%20%E5%85%A5%E5%8F%A3&status=failed&source=automatic&scope=active_video&referenced=yes",
  );
  assert.equal(buildElementQuery({page: -2, pageSize: 500}), "?page=1&page_size=20");
});

test("filter serializer maps the dependency control to referenced API values", () => {
  assert.deepEqual(
    serializeElementFilters({
      search: "  评论入口  ",
      status: "using_lkg",
      source: "legacy_manual",
      scope: "active_video",
      dependency: "no",
    }),
    {
      search: "评论入口",
      status: "using_lkg",
      source: "legacy_manual",
      scope: "active_video",
      referenced: "no",
    },
  );
});

test("element directory uses server order and server pagination unchanged", async () => {
  const requests = [];
  const ui = createSelectorProbeUI({
    requestJson: async (url, method) => {
      requests.push({url, method});
      if (url === "/api/auth/session") {
        return response({role: "administrator"});
      }
      if (url.endsWith("/status")) return response({revision: 1});
      return response({
        items: [
          {id: "server-second", published_status: "healthy"},
          {id: "server-first", published_status: "failed"},
        ],
        page: 3,
        page_size: 50,
        total: 121,
        revision: 7,
      });
    },
    render() {},
    setInterval: () => 1,
    clearInterval() {},
  });

  await ui.init();
  ui.state.elements.page = 3;
  ui.state.elements.pageSize = 50;
  await ui.activateTab("elements");

  assert.deepEqual(
    ui.state.elements.items.map((item) => item.id),
    ["server-second", "server-first"],
  );
  assert.equal(ui.state.elements.page, 3);
  assert.equal(ui.state.elements.pageSize, 50);
  assert.equal(ui.state.elements.total, 121);
  assert.deepEqual(requests.at(-1), {
    url: "/api/selector-probe/elements?page=3&page_size=50",
    method: "GET",
  });
});

test("search waits 300ms and aborts the stale element request", async () => {
  const timers = [];
  const requests = [];
  const aborts = [];
  const first = deferred();
  const ui = createSelectorProbeUI({
    requestJson: (url, _method, _body, options) => {
      requests.push({url, signal: options?.signal});
      if (url.endsWith("/status")) return Promise.resolve(response({}));
      if (url === "/api/auth/session") {
        return Promise.resolve(response({role: "operator"}));
      }
      return requests.filter((item) => item.url.includes("/elements?")).length === 1
        ? first.promise
        : Promise.resolve(response({
          items: [{id: "latest"}],
          page: 1,
          page_size: 20,
          total: 1,
          revision: 2,
        }));
    },
    createAbortController: () => {
      const signal = {};
      return {
        signal,
        abort: () => aborts.push(signal),
      };
    },
    setTimeout: (callback, milliseconds) => {
      timers.push({callback, milliseconds});
      return timers.length;
    },
    clearTimeout() {},
    setInterval: () => 1,
    clearInterval() {},
    render() {},
  });

  await ui.init();
  const pending = ui.activateTab("elements");
  ui.updateElementFilters({search: "评论"}, {debounce: true});

  assert.equal(aborts.length, 1);
  assert.equal(timers.at(-1).milliseconds, 300);
  assert.equal(requests.filter((item) => item.url.includes("/elements?")).length, 1);

  await timers.at(-1).callback();
  first.resolve(response({
    items: [{id: "stale"}],
    page: 1,
    page_size: 20,
    total: 1,
    revision: 1,
  }));
  await pending;

  assert.equal(ui.state.elements.items[0].id, "latest");
  assert.match(requests.at(-1).url, /search=%E8%AF%84%E8%AE%BA/);
});

test("summary navigation applies its matching element filter", async () => {
  const requests = [];
  const ui = createSelectorProbeUI({
    requestJson: async (url) => {
      requests.push(url);
      if (url === "/api/auth/session") return response({role: "operator"});
      if (url.endsWith("/status")) return response({});
      return response({items: [], page: 1, page_size: 20, total: 0, revision: 1});
    },
    render() {},
    setInterval: () => 1,
    clearInterval() {},
  });
  await ui.init();

  await ui.activateSummary("elements", {status: "failed"});

  assert.equal(ui.state.activeTab, "elements");
  assert.equal(ui.state.elements.filters.status, "failed");
  assert.match(requests.at(-1), /status=failed/);
});

test("overview renderer keeps five priorities and only safe event fields", () => {
  const {document, nodes} = fakeDocument([
    "selector-overview-priority",
    "selector-overview-events",
    "selector-overview-version",
    "selector-overview-last-validation",
    "selector-overview-next-run",
    "selector-overview-gates",
    "selector-overview-alerts",
    "selector-overview-element-counts",
    "selector-probe-overview-health",
  ]);
  renderOverview(document, {
    overview: {
      registry: {
        available: true,
        active_version: "sel-9",
        bundle_hash: `sha256:${"b".repeat(64)}`,
      },
      latest_run: {
        id: 9,
        status: "completed",
        finished_at: "2026-07-29T03:01:00+08:00",
      },
      priority_elements: Array.from({length: 8}, (_, index) => ({
        id: `item-${index}`,
        display_name: `元素 ${index}`,
        published_status: index === 0 ? "failed" : "healthy",
      })),
      recent_events: [{
        type: "publish",
        summary: "版本已发布",
        occurred_at: "2026-07-29T03:00:00+08:00",
        raw_selector: "#secret",
        model_output: "secret",
      }],
    },
  });

  assert.equal(nodes.get("selector-overview-priority").children.length, 5);
  assert.equal(nodes.get("selector-overview-version").textContent, "sel-9");
  assert.equal(
    nodes.get("selector-overview-last-validation").textContent,
    "2026-07-29T03:01:00+08:00",
  );
  assert.match(
    nodes.get("selector-probe-overview-health").textContent,
    /最近探针正常/,
  );
  const eventText = nodes.get("selector-overview-events").children
    .flatMap((item) => item.children)
    .map((item) => item.textContent)
    .join(" ");
  assert.match(eventText, /版本已发布/);
  assert.doesNotMatch(eventText, /#secret|model_output|secret/);
});

test("directory renderer shows add control only to administrators", () => {
  for (const role of ["administrator", "operator"]) {
    const {document, nodes} = fakeDocument([
      "selector-element-add",
      "selector-element-counts",
      "selector-element-rows",
      "selector-element-page-meta",
      "selector-element-prev",
      "selector-element-next",
      "selector-element-page-size",
    ]);
    const state = {
      session: {role},
      elements: {
        items: [],
        page: 1,
        pageSize: 20,
        total: 0,
        revision: 1,
        filters: {},
      },
      overview: null,
    };

    renderElementDirectory(document, state);

    assert.equal(nodes.get("selector-element-add").hidden, !canCreateElement({role}));
  }
});

test("basic wizard serializes semantic fields and no selector code", () => {
  const payload = serializeSemanticContract({
    displayName: "分享入口",
    intent: "open current video share panel",
    requiredState: "feed_ready",
    scope: "active_video",
    probeAction: "open_read_only",
    xpath: "/html/body/button",
    javascript: "alert(1)",
  });

  assert.deepEqual(Object.keys(payload).sort(), [
    "display_name",
    "intent",
    "probe_action",
    "required_state",
    "scope",
  ]);
  assert.equal("xpath" in payload, false);
  assert.equal("javascript" in payload, false);
});

test("readonly locator sanitizer rejects executable and absolute selectors", () => {
  assert.throws(
    () => sanitizeStructuredLocators([
      {id: "x", type: "xpath", value: "/html/body/button", enabled: true},
    ], {editable: true}),
    /absolute_xpath_not_allowed/,
  );
  assert.throws(
    () => sanitizeStructuredLocators([
      {id: "x", type: "css", value: "javascript:alert(1)", enabled: true},
    ], {editable: true}),
    /executable_selector_not_allowed/,
  );
  assert.deepEqual(
    sanitizeStructuredLocators([
      {
        id: "role-1",
        type: "role",
        role: "button",
        name: "Share",
        name_mode: "exact",
        enabled: true,
      },
    ], {editable: true})[0],
    {
      id: "role-1",
      type: "role",
      role: "button",
      name: "Share",
      name_mode: "exact",
      enabled: true,
    },
  );
});

test("validation matrix requires two masked profiles and two fresh rounds", () => {
  const result = summarizeValidation([
    {profile_mask: "***3A7F", round: 1, status: "passed"},
    {profile_mask: "***3A7F", round: 2, status: "passed"},
    {profile_mask: "***91C2", round: 1, status: "passed"},
    {profile_mask: "***91C2", round: 2, status: "passed"},
  ]);

  assert.deepEqual(result, {profiles: 2, rounds: 2, publishable: true});
  assert.equal(
    summarizeValidation([
      {profile_mask: "***3A7F", round: 1, status: "passed"},
      {profile_mask: "***3A7F", round: 2, status: "passed"},
      {profile_mask: "full-profile-id", round: 1, status: "passed"},
      {profile_mask: "full-profile-id", round: 2, status: "passed"},
    ]).publishable,
    false,
  );
});

test("detail renderer exposes four safe sections and a masked 2x2 matrix", () => {
  const {document, nodes} = fakeDocument([
    "selector-element-detail-title",
    "selector-element-detail-alias",
    "selector-element-detail-status",
    "selector-element-detail-version",
    "selector-element-detail-dependencies",
    "selector-element-detail-last-validation",
    "selector-element-detail-evidence",
    "selector-element-detail-candidates",
    "selector-element-detail-repairs",
    "selector-element-detail-history",
    "selector-element-validation-matrix",
  ]);
  const rounds = [
    {profile_mask: "***3A7F", round: 1, status: "passed", match_count: 1},
    {profile_mask: "***3A7F", round: 2, status: "passed", match_count: 1},
    {profile_mask: "***91C2", round: 1, status: "passed", match_count: 1},
    {profile_mask: "***91C2", round: 2, status: "passed", match_count: 1},
  ];
  const detail = sanitizeElementDetail({
    id: "element-share",
    display_name: "分享入口",
    published_status: "healthy",
    active_version: "sel-9",
    dependency_count: 2,
    last_validated_at: "2026-07-29T03:01:00+08:00",
    evidence: {status: "passed", rounds},
    candidates: [{id: "draft", type: "css", value: ".draft-only", enabled: true}],
    candidate_comparison: {
      active: [{id: "role", type: "role", role: "button", name: "Share", name_mode: "exact", enabled: true}],
      deterministic: [{id: "attr", type: "attribute", name: "data-e2e", value: "share-icon", enabled: true}],
      repaired: [{id: "css", type: "css", value: "button[data-e2e='share-icon']", enabled: true}],
    },
    repairs: Array.from({length: 4}, (_, index) => ({
      attempt: index + 1,
      previous_method: "role",
      failure_code: "not_found",
      new_method: "attribute",
      validation_result: "passed",
    })),
    history: [{version_id: "sel-9", status: "published", published_at: "2026-07-29"}],
    raw_dom: "never-render",
  });

  renderElementDetail(document, detail);
  renderValidationMatrix(document, rounds);

  assert.equal(nodes.get("selector-element-detail-title").textContent, "分享入口");
  assert.equal(nodes.get("selector-element-detail-evidence").children.length, 1);
  assert.equal(nodes.get("selector-element-detail-candidates").children.length, 3);
  assert.equal(nodes.get("selector-element-detail-repairs").children.length, 3);
  assert.equal(nodes.get("selector-element-detail-history").children.length, 1);
  assert.equal(nodes.get("selector-element-validation-matrix").children.length, 4);
  const encoded = JSON.stringify(
    Array.from(nodes.values()).map((item) => item.textContent),
  );
  assert.doesNotMatch(encoded, /never-render/);
});

test("probe suggestion and detail drop all raw capture and model-output fields", () => {
  const suggestion = sanitizeProbeSuggestion({
    role: "button",
    name: "Share",
    stable_attributes: [{name: "data-e2e", value: "share-icon"}],
    candidates: [{id: "role", type: "role", role: "button", name: "Share", name_mode: "exact", enabled: true}],
    llm_used: true,
    rejected_methods: [{method: "absolute_xpath", code: "unsafe_method"}],
    raw_dom: "<button>secret</button>",
    ax_tree: {secret: true},
    prompt: "secret prompt",
    model_output: "secret answer",
  });
  const detail = sanitizeElementDetail({
    id: "element-share",
    display_name: "分享入口",
    evidence: {profile_mask: "***3A7F", raw_dom: "secret"},
    candidates: suggestion.candidates,
    candidate_comparison: {
      active: suggestion.candidates,
      deterministic: [],
      repaired: [],
    },
    repairs: Array.from({length: 5}, (_, index) => ({
      attempt: index + 1,
      failure_code: "not_found",
      prompt_version: "repair-v1",
      model_id: "gpt-safe",
      prompt: "secret",
      model_output: "secret",
    })),
    raw_dom: "secret",
    ax_tree: "secret",
    prompt: "secret",
    model_output: "secret",
  });

  assert.deepEqual(Object.keys(suggestion).sort(), [
    "candidates",
    "llm_used",
    "name",
    "rejected_methods",
    "role",
    "stable_attributes",
    "warnings",
  ]);
  assert.equal(detail.repairs.length, 3);
  const encoded = JSON.stringify({suggestion, detail});
  assert.doesNotMatch(encoded, /raw_dom|ax_tree|secret prompt|secret answer|model_output/);
});

test("administrator wizard posts semantic draft then polls durable request detail", async () => {
  const requests = [];
  const timers = [];
  let requestPoll = 0;
  const ui = createSelectorProbeUI({
    requestJson: async (url, method, body) => {
      requests.push({url, method, body});
      if (url === "/api/auth/session") return response({role: "administrator"});
      if (url.endsWith("/status")) return response({});
      if (method === "POST" && url === "/api/selector-probe/elements") {
        return {status: 201, data: {
          id: "element-share",
          display_name: "分享入口",
          revision: 5,
          draft_revision: 99,
          candidates: [],
        }};
      }
      if (method === "POST" && url.endsWith("/probe")) {
        return {status: 202, data: {
          request_id: "request-probe-1",
          request_type: "probe",
          element_id: "element-share",
          expected_revision: 6,
          status: "accepted",
        }};
      }
      if (url === "/api/selector-probe/element-requests/request-probe-1") {
        requestPoll += 1;
        return response(requestPoll === 1
          ? {request_id: "request-probe-1", request_type: "probe", status: "processing"}
          : {
            request_id: "request-probe-1",
            request_type: "probe",
            status: "completed",
            result: {
              suggestion: {
                role: "button",
                name: "Share",
                stable_attributes: [{name: "data-e2e", value: "share-icon"}],
                candidates: [],
                llm_used: false,
              },
              raw_dom: "secret",
            },
          });
      }
      if (method === "GET" && url === "/api/selector-probe/elements/element-share") {
        return response({
          id: "element-share",
          display_name: "分享入口",
          revision: 7,
          candidate_comparison: {
            active: [],
            deterministic: [],
            repaired: [],
          },
          repairs: [],
          history: [],
        });
      }
      return response({});
    },
    setTimeout: (callback, milliseconds) => {
      timers.push({callback, milliseconds});
      return timers.length;
    },
    clearTimeout() {},
    setInterval: () => 1,
    clearInterval() {},
    render() {},
  });
  await ui.init();

  assert.equal(ui.openElementWizard(), true);
  await ui.createElementDraft({
    displayName: "分享入口",
    intent: "open current video share panel",
    requiredState: "feed_ready",
    scope: "active_video",
    probeAction: "open_read_only",
  });
  await ui.requestElementProbe();

  assert.equal(ui.state.selected.step, 2);
  assert.equal(timers.at(-1).milliseconds, 1000);
  assert.equal(await ui.requestElementValidation(), false);
  assert.equal(
    requests.filter((item) => item.url.endsWith("/validate")).length,
    0,
  );
  await timers.at(-1).callback();
  assert.equal(ui.state.selected.suggestion.role, "button");
  assert.equal(ui.state.selected.detail.revision, 7);
  assert.equal(ui.state.selected.validationReady, true);
  assert.doesNotMatch(JSON.stringify(ui.state.selected), /raw_dom|secret/);
  assert.deepEqual(requests.find((item) => item.url.endsWith("/probe")).body, {
    expected_revision: 5,
  });
  assert.deepEqual(requests.at(-1), {
    url: "/api/selector-probe/elements/element-share",
    method: "GET",
    body: undefined,
  });
});

test("draft candidates are never labeled active and missing comparison is explicit", () => {
  const {document, nodes} = fakeDocument([
    "selector-element-detail-title",
    "selector-element-detail-alias",
    "selector-element-detail-status",
    "selector-element-detail-version",
    "selector-element-detail-dependencies",
    "selector-element-detail-last-validation",
    "selector-element-detail-evidence",
    "selector-element-detail-candidates",
    "selector-element-detail-repairs",
    "selector-element-detail-history",
    "selector-element-validation-matrix",
  ]);
  renderElementDetail(document, {
    id: "element-draft",
    display_name: "草稿",
    candidates: [{id: "draft", type: "css", value: ".draft", enabled: true}],
  });

  const candidateText = nodes.get("selector-element-detail-candidates").children
    .map((item) => item.textContent || item.children.map((child) => child.textContent).join(" "))
    .join(" ");
  assert.doesNotMatch(candidateText, /Active/);
  assert.match(candidateText, /暂无证据/);
});

test("migration requires backend availability then PATCHes semantic contract before probe", async () => {
  const requests = [];
  const ui = createSelectorProbeUI({
    requestJson: async (url, method, body) => {
      requests.push({url, method, body});
      if (url === "/api/auth/session") return response({role: "administrator"});
      if (url.endsWith("/status")) return response({});
      if (method === "POST" && url.endsWith("/migrate")) {
        return response({
          id: "legacy-share",
          display_name: "历史分享",
          revision: 4,
          migration_available: false,
          management_source: "legacy_manual",
          contract: null,
        });
      }
      if (method === "PATCH" && url.endsWith("/draft")) {
        return response({
          id: "legacy-share",
          display_name: "历史分享",
          revision: 5,
          management_source: "legacy_manual",
          contract: body.contract,
        });
      }
      return response({});
    },
    setInterval: () => 1,
    clearInterval() {},
    render() {},
  });
  await ui.init();

  assert.equal(ui.openLegacyMigration({
    id: "legacy-share",
    revision: 0,
    management_source: "legacy_manual",
    migration_available: false,
  }), false);
  assert.equal(ui.openLegacyMigration({
    id: "legacy-share",
    display_name: "历史分享",
    revision: 0,
    management_source: "legacy_manual",
    migration_available: true,
  }), true);
  await ui.requestLegacyMigration();
  assert.deepEqual(
    requests.find((item) => item.url.endsWith("/migrate")).body,
    {expected_revision: 0},
  );
  assert.equal(ui.state.selected.kind, "wizard");
  assert.equal(ui.state.selected.step, 1);
  assert.equal(ui.state.selected.migrationDraft, true);

  await ui.createElementDraft({
    displayName: "历史分享",
    intent: "find share control",
    requiredState: "feed_ready",
    scope: "active_video",
    probeAction: "inspect_only",
  });

  const patch = requests.find((item) => item.method === "PATCH");
  assert.equal(patch.body.expected_revision, 4);
  assert.equal(patch.body.contract.intent, "find share control");
  assert.equal(ui.state.selected.step, 2);
  assert.equal(ui.state.selected.detail.revision, 5);
});

test("validation uses refreshed probe revision then replaces detail on terminal state", async () => {
  const requests = [];
  const ui = createSelectorProbeUI({
    requestJson: async (url, method, body) => {
      requests.push({url, method, body});
      if (url === "/api/auth/session") return response({role: "administrator"});
      if (url.endsWith("/status")) return response({});
      if (method === "POST" && url.endsWith("/validate")) {
        return {status: 202, data: {
          request_id: "request-validate-1",
          request_type: "validate",
          element_id: "element-share",
          expected_revision: 3,
          status: "accepted",
        }};
      }
      if (url.endsWith("/element-requests/request-validate-1")) {
        return response({
          request_id: "request-validate-1",
          request_type: "validate",
          element_id: "element-share",
          status: "completed",
          result: {
            validation: {
              status: "passed",
              rounds: [
                {profile_mask: "***3A7F", round: 1, status: "passed"},
              ],
            },
          },
        });
      }
      if (method === "GET" && url.endsWith("/elements/element-share")) {
        return response({
          id: "element-share",
          revision: 4,
          candidate_comparison: {active: [], deterministic: [], repaired: []},
        });
      }
      return response({});
    },
    setInterval: () => 1,
    clearInterval() {},
    render() {},
  });
  await ui.init();
  ui.openElementWizard();
  ui.state.selected.detail = sanitizeElementDetail({
    id: "element-share",
    revision: 2,
  });
  ui.state.selected.validationReady = true;
  ui.state.selected.probeCompletedRevision = 2;

  assert.equal(await ui.requestElementValidation(), true);
  assert.deepEqual(
    requests.find((item) => item.url.endsWith("/validate")).body,
    {expected_revision: 2},
  );
  assert.equal(ui.state.selected.detail.revision, 4);
  assert.equal(ui.state.selected.validationReady, false);
  assert.equal(ui.state.selected.step, 3);
  assert.equal(ui.state.selected.validation.status, "passed");
});

test("publishing remains non-terminal until atomic publish and reconciliation complete", async () => {
  const timers = [];
  const requests = [];
  let pollCount = 0;
  const ui = createSelectorProbeUI({
    requestJson: async (url, method, body) => {
      requests.push({url, method, body});
      if (url === "/api/auth/session") return response({role: "administrator"});
      if (url.endsWith("/status")) return response({});
      if (method === "POST" && url.endsWith("/validate")) {
        return {status: 202, data: {
          request_id: "request-publish-1",
          request_type: "validate",
          element_id: "element-share",
          expected_revision: 3,
          status: "accepted",
        }};
      }
      if (url.endsWith("/element-requests/request-publish-1")) {
        pollCount += 1;
        return response(pollCount === 1 ? {
          request_id: "request-publish-1",
          request_type: "validate",
          element_id: "element-share",
          status: "publishing",
        } : {
          request_id: "request-publish-1",
          request_type: "validate",
          element_id: "element-share",
          status: "completed",
          result: {validation: {status: "passed", rounds: []}},
        });
      }
      if (method === "GET" && url.endsWith("/elements/element-share")) {
        return response({
          id: "element-share",
          revision: 4,
          candidate_comparison: {active: [], deterministic: [], repaired: []},
        });
      }
      return response({});
    },
    setTimeout: (callback, milliseconds) => {
      timers.push({callback, milliseconds});
      return timers.length;
    },
    clearTimeout() {},
    setInterval: () => 1,
    clearInterval() {},
    render() {},
  });
  await ui.init();
  ui.openElementWizard();
  ui.state.selected.detail = sanitizeElementDetail({
    id: "element-share",
    revision: 2,
  });
  ui.state.selected.validationReady = true;
  ui.state.selected.probeCompletedRevision = 2;

  assert.equal(await ui.requestElementValidation(), true);
  assert.equal(ui.state.selected.request.status, "publishing");
  assert.equal(
    elementRequestStatusText(ui.state.selected.request),
    "validate · 原子发布/对账中",
  );
  assert.equal(timers.at(-1).milliseconds, 1000);
  assert.equal(
    requests.filter((item) => item.url.endsWith("/elements/element-share")).length,
    0,
  );
  await timers.at(-1).callback();
  assert.equal(ui.state.selected.request.status, "completed");
  assert.equal(ui.state.selected.detail.revision, 4);
});

test("late request poll cannot contaminate a closed or replacement workspace", async () => {
  const old = deferred();
  const aborts = [];
  const ui = createSelectorProbeUI({
    requestJson: async (url) => {
      if (url === "/api/auth/session") return response({role: "administrator"});
      if (url.endsWith("/status")) return response({});
      if (url.includes("/element-requests/")) return old.promise;
      return response({});
    },
    createAbortController: () => ({
      signal: {},
      abort: () => aborts.push("aborted"),
    }),
    setInterval: () => 1,
    clearInterval() {},
    render() {},
  });
  await ui.init();
  ui.openElementWizard();
  ui.state.selected.request = {
    request_id: "old-request",
    request_type: "probe",
    status: "processing",
  };
  const polling = ui.pollElementRequest("old-request");
  ui.closeElementWorkspace();
  ui.openElementWizard();
  old.resolve(response({
    request_id: "old-request",
    request_type: "probe",
    status: "completed",
    result: {suggestion: {role: "button"}},
  }));
  await polling;

  assert.equal(aborts.length, 1);
  assert.equal(ui.state.selected.suggestion, null);
  assert.equal(ui.state.selected.request, null);
});

test("native dialog cancel and close use unified workspace cleanup", () => {
  function fakeDialog() {
    const listeners = new Map();
    return {
      open: false,
      addEventListener(type, callback) {
        if (!listeners.has(type)) listeners.set(type, []);
        listeners.get(type).push(callback);
      },
      emit(type, event = {}) {
        (listeners.get(type) || []).forEach((callback) => callback(event));
      },
      showModal() {
        this.open = true;
      },
      close() {
        this.open = false;
        this.emit("close");
      },
    };
  }
  const wizard = fakeDialog();
  const migration = fakeDialog();
  const document = {
    visibilityState: "visible",
    querySelector(selector) {
      if (selector === "#selector-element-wizard") return wizard;
      if (selector === "#selector-element-migration-dialog") return migration;
      return null;
    },
    querySelectorAll() {
      return [];
    },
  };
  const dependencies = selectorProbeDependencies({
    document,
    fetch: async () => ({status: 200, json: async () => ({})}),
    setInterval: () => 1,
    clearInterval() {},
    setTimeout: () => 1,
    clearTimeout() {},
  });
  let cleanupCount = 0;
  const controller = {
    state: {
      activeTab: "elements",
      elements: {items: [], filters: {}},
      selected: {kind: "wizard", step: 1},
    },
    closeElementWorkspace() {
      cleanupCount += 1;
      this.state.selected = null;
    },
  };
  dependencies.render("shell", controller.state, controller);
  let prevented = false;
  wizard.emit("cancel", {preventDefault: () => { prevented = true; }});
  assert.equal(prevented, true);
  assert.equal(cleanupCount, 1);
  assert.equal(controller.state.selected, null);

  controller.state.selected = {kind: "migration"};
  migration.emit("close");
  assert.equal(cleanupCount, 2);
  assert.equal(controller.state.selected, null);
});

test("operators cannot open create/validate UI and closing clears temporary state", async () => {
  const ui = createSelectorProbeUI({
    requestJson: async (url) => (
      url === "/api/auth/session"
        ? response({role: "operator"})
        : response({})
    ),
    setInterval: () => 1,
    clearInterval() {},
    render() {},
  });
  await ui.init();

  assert.equal(ui.openElementWizard(), false);
  ui.state.selected = {
    kind: "detail",
    detail: {id: "element-1"},
    suggestion: {
      stable_attributes: [
        {name: "data-e2e", value: "ephemeral-selector-value"},
      ],
    },
  };
  assert.equal(await ui.requestElementValidation(), false);
  ui.closeElementWorkspace();

  assert.equal(ui.state.selected, null);
  assert.doesNotMatch(
    JSON.stringify(ui.snapshot()),
    /ephemeral-selector-value/,
  );
});

test("discovered candidate opens prefilled existing element wizard", async () => {
  const ui = createSelectorProbeUI({
    requestJson: async (url) => (
      url === "/api/auth/session"
        ? response({role: "administrator"})
        : response({items: [], page: 1, page_size: 20, total: 0})
    ),
    setInterval: () => 1,
    clearInterval() {},
    render() {},
  });
  await ui.init();

  ui.openDiscoveryCandidate({
    fingerprint: "sha256:safe",
    page_state: "comment_panel_open",
    scope: "visible_comment_panel",
    role: "textbox",
    name: "Add comment",
    attributes: {"data-e2e": "comment-input"},
    recommended_locators: [{
      id: "probe-input",
      type: "attribute",
      name: "data-e2e",
      value: "comment-input",
      enabled: true,
    }],
  });

  assert.equal(ui.state.selected.kind, "wizard");
  assert.equal(
    ui.state.selected.form.requiredState,
    "comment_panel_open",
  );
  assert.equal(ui.state.selected.form.scope, "visible_comment_panel");
  assert.deepEqual(ui.state.selected.form.acceptedRoles, ["textbox"]);
  assert.equal(
    ui.state.selected.suggestion.candidates[0].value,
    "comment-input",
  );
});

test("legacy migration copy promises non-destructive observe-only rollout", () => {
  const copy = migrationSafetyCopy();

  assert.match(copy, /保留当前 Locator/);
  assert.match(copy, /仅观察/);
  assert.match(copy, /策略依赖保持不变/);
  assert.match(copy, /不会自动开启强制执行/);
});
