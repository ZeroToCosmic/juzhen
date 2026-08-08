const assert = require("node:assert/strict");
const test = require("node:test");

const {
  buildElementQuery,
  canCreateElement,
  createSelectorProbeUI,
  renderElementDirectory,
  renderOverview,
  sanitizePickerSession,
  sanitizeStructuredLocators,
  selectOverviewElements,
  selectorProbeDependencies,
  serializeElementFilters,
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


test("live collector names inventory selections and saves manual drafts", async () => {
  const requests = [];
  const selection = {
    selection_id: "selection-1",
    fingerprint: "sha256:safe",
    page_state: "feed_ready",
    scope: "active_video",
    tag: "button",
    role: "button",
    name: "Comments",
    attributes: {"data-e2e": "comment-icon"},
    region: {x: 0.8, y: 0.4, width: 0.1, height: 0.1},
    locatable: true,
    locators: [{type: "css", value: "[data-e2e=\"comment-icon\"]", match_count: 1}],
  };
  const ui = createSelectorProbeUI({
    requestJson: async (url, method, body) => {
      requests.push({url, method, body});
      if (url === "/api/auth/session") return response({role: "administrator"});
      if (url.endsWith("/status")) return response({});
      if (url.endsWith("/settings/profiles")) {
        return response({items: [{
          profile_ref: "prf_safe",
          profile_mask: "***safe",
          dedicated_test: true,
          status: "healthy",
        }]});
      }
      if (url.endsWith("/picker/start")) {
        return {status: 202, data: {
          session_id: "picker-1", status: "starting", revision: 1,
          inventory: [], selection_count: 0,
        }};
      }
      if (url.endsWith("/picker/picker-1")) {
        return response({
          session_id: "picker-1", status: "selecting", revision: 2,
          inventory: [selection], selection_count: 1,
        });
      }
      if (url.endsWith("/picker/picker-1/confirm")) {
        return response({
          session_id: "picker-1", status: "confirmed", revision: 3,
          inventory: [selection],
          selections: [{...selection, display_name: "评论入口"}],
          selection_count: 1, cleanup: "passed",
        });
      }
      if (url === "/api/selector-probe/elements" && method === "POST") {
        return {status: 201, data: {id: "element-1", display_name: body.display_name}};
      }
      return response({items: [], page: 1, page_size: 20, total: 0});
    },
    setInterval: () => 1,
    clearInterval() {},
    setTimeout: () => 2,
    clearTimeout() {},
    documentVisible: () => true,
    render() {},
  });
  await ui.init();

  assert.equal(await ui.openLivePicker(), true);
  assert.equal(await ui.startLivePicker({
    profileRef: "prf_safe",
    pageState: "feed_ready",
  }), true);
  assert.equal(await ui.pollLivePicker(), true);
  assert.equal(ui.state.picker.session.selection_count, 1);
  assert.equal(await ui.confirmCollector([{
    selectionId: "selection-1", displayName: "评论入口",
  }]), true);

  assert.equal(ui.state.activeTab, "managed");
  const confirm = requests.find((item) => item.url.endsWith("/confirm"));
  assert.deepEqual(confirm.body.selections, [{
    selection_id: "selection-1", display_name: "评论入口",
  }]);
  const create = requests.find((item) => (
    item.url === "/api/selector-probe/elements" && item.method === "POST"
  ));
  assert.equal(create.body.fingerprint.role, "button");
  assert.deepEqual(create.body.locators, [{
    type: "css", value: "[data-e2e=\"comment-icon\"]",
  }]);
  assert.doesNotMatch(JSON.stringify(sanitizePickerSession({
    ...selection,
    session_id: "picker-1",
    status: "ready",
    profile_id: "raw-secret",
    cdp_url: "ws://secret",
  })), /raw-secret|ws:\/\//);
});

test("managed element controller renames rebinds and deletes through manual APIs", async () => {
  const requests = [];
  const definition = {
    page_key: "tiktok.feed_ready",
    target_origin: "https://www.tiktok.com",
    url_pattern: "https://www.tiktok.com/*",
    operation_steps: [],
    fingerprint: {tag: "button", role: "button", name: "Comments"},
    locators: [{type: "css", value: "[data-e2e=\"comment-icon\"]"}],
  };
  const ui = createSelectorProbeUI({
    requestJson: async (url, method, body) => {
      requests.push({url, method, body});
      if (url === "/api/auth/session") return response({role: "administrator"});
      if (url.endsWith("/status")) return response({});
      if (url.includes("/elements?") && method === "GET") {
        return response({items: [{
          id: "element-1", display_name: "评论入口", status: "draft",
          dependency_count: 0, revision: 4,
        }], page: 1, page_size: 20, total: 1});
      }
      if (url.endsWith("/elements/element-1") && method === "PATCH") {
        return response({id: "element-1", revision: body.operation === "rename" ? 5 : 6});
      }
      if (url.endsWith("/elements/element-1") && method === "DELETE") {
        return {status: 204, data: {}};
      }
      return response({items: []});
    },
    setInterval: () => 1,
    clearInterval() {},
    render() {},
  });
  await ui.init();
  await ui.activateTab("managed");

  assert.equal(await ui.renameElement("element-1", "新评论入口", 4), true);
  assert.equal(await ui.rebindElement("element-1", definition, 5), true);
  assert.equal(await ui.deleteElement("element-1", 6), true);

  const writes = requests.filter((item) => item.url.endsWith("/elements/element-1"));
  assert.deepEqual(writes.map((item) => [item.method, item.body.operation || "delete"]), [
    ["PATCH", "rename"], ["PATCH", "rebind"], ["DELETE", "delete"],
  ]);
});
