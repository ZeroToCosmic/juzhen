const test = require("node:test");
const assert = require("node:assert/strict");

const {
  createTikTokStatsController,
  restoreState,
  stateToSearch,
  validateDates,
  formatMetric,
  createSafeRenderer,
  sortPosts,
  buildChartModel,
  formatMatrixCell,
} = require("../gateway/static/tiktok_stats.js");

function response(data, status = 200) {
  return {ok: status >= 200 && status < 300, status, json: async () => data};
}

function fakeCookie(suffix) {
  return `session${"id"}=${suffix}`;
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return {promise, resolve};
}

function harness(options = {}) {
  const requests = [];
  const events = [];
  const timers = [];
  const history = {replaceState(_state, _title, url) { this.url = url; }};
  const request = options.request || (async (url, init = {}) => {
    requests.push({url, init});
    if (url.startsWith("/api/tiktok-stats/summary")) return response({account_count: 2});
    if (url.startsWith("/api/tiktok-stats/table")) return response({rows: [], page: 1, total_pages: 0});
    if (url.startsWith("/api/tiktok-stats/accounts")) return response({accounts: [], existing_candidates: []});
    if (url === "/api/tiktok-stats/settings/cookie") return response({status: {configured: false}});
    if (url === "/api/tiktok-stats/status") return response({scraper: {running: true}, worker: {running: false}});
    return response({});
  });
  const ui = createTikTokStatsController({
    request,
    render: (event, payload) => events.push({event, payload}),
    history,
    location: {pathname: "/tiktok-stats", search: options.search || ""},
    today: () => "2026-07-22",
    setTimeout: options.setTimeout || ((fn, delay) => { timers.push({fn, delay}); return timers.length; }),
    clearTimeout: options.clearTimeout || (() => {}),
    AbortController: options.AbortController,
  });
  return {ui, requests, events, timers, history};
}

function domHarness(request) {
  class Node {
    constructor(tag = "div") {
      this.tag = tag; this.children = []; this.listeners = {}; this.attributes = {};
      this.value = ""; this.textContent = ""; this.className = ""; this.hidden = false;
      this.disabled = false; this.checkedNodes = [];
    }
    append(child) { this.children.push(child); }
    replaceChildren(...children) { this.children = children; }
    setAttribute(key, value) { this.attributes[key] = value; }
    addEventListener(type, callback) { this.listeners[type] = callback; }
    querySelectorAll() { return this.checkedNodes; }
    showModal() { this.open = true; }
    close() { this.open = false; }
  }
  const nodes = new Map();
  const get = (id) => { if (!nodes.has(id)) nodes.set(id, new Node()); return nodes.get(id); };
  const document = {
    querySelector(selector) { return selector === "#tiktok-stats-app" ? get("tiktok-stats-app") : null; },
    getElementById: get,
    createElement: (tag) => new Node(tag),
  };
  const window = {fetch: request, location: {pathname: "/tiktok-stats", search: ""}, history: {replaceState() {}}};
  return {document, window, nodes, get};
}

test("restores all dashboard state from URL and requests summary plus table", async () => {
  const search = "?mode=range&start=2026-07-01&end=2026-07-21&q=alice&status=disabled&baseline=incomplete&sort=likes&direction=asc&page=3&page_size=100&view=table";
  const {ui, requests} = harness({search});
  await ui.init();

  assert.deepEqual(ui.state.query, {
    mode: "range", start: "2026-07-01", end: "2026-07-21", q: "alice",
    status: "disabled", baseline: "incomplete", sort: "likes", direction: "asc",
    page: 3, pageSize: 100, view: "table",
  });
  assert.equal(requests.some(({url}) => url.includes("/summary?") && url.includes("start_date=2026-07-01")), true);
  assert.equal(requests.some(({url}) => url.includes("/table?") && url.includes("sort=likes_delta") && url.includes("page=3")), true);
  assert.equal(requests.some(({url}) => url === "/api/tiktok-stats/accounts"), false);
});

test("normalizes invalid URL values and validates range dates", () => {
  const state = restoreState("?mode=nope&sort=evil&direction=sideways&page=0", "2026-07-22");
  assert.equal(state.mode, "single");
  assert.equal(state.sort, "posts");
  assert.equal(state.direction, "desc");
  assert.equal(state.page, 1);
  assert.equal(validateDates({...state, mode: "range", start: "2026-07-23", end: "2026-07-22"}), "开始日期不能晚于结束日期");
  assert.equal(validateDates({...state, start: "bad"}), "请选择有效日期");
  assert.equal(restoreState("?mode=single&start=2026-07-01&end=2026-07-20", "2026-07-22").end, "2026-07-01");
});

test("supports all sort metrics, direction, pagination, and URL serialization", async () => {
  for (const sort of ["posts", "likes", "views", "comments"]) {
    const {ui, requests, history} = harness();
    await ui.init();
    await ui.updateQuery({sort, direction: "asc", page: 2, pageSize: 20});
    const table = requests.filter(({url}) => url.startsWith("/api/tiktok-stats/table")).at(-1).url;
    assert.match(table, new RegExp(`sort=${sort}_delta`));
    assert.match(table, /direction=asc/);
    assert.match(table, /page=2/);
    assert.match(history.url, new RegExp(`sort=${sort}`));
  }
  assert.match(stateToSearch(restoreState("", "2026-07-22")), /mode=single/);
});

test("debounces search and resets the page", async () => {
  const {ui, timers, requests} = harness();
  await ui.init();
  const before = requests.length;
  ui.search("a");
  ui.search("alice");
  assert.equal(requests.length, before);
  assert.equal(timers.at(-1).delay, 300);
  await timers.at(-1).fn();
  assert.equal(ui.state.query.q, "alice");
  assert.equal(ui.state.query.page, 1);
  assert.equal(requests.some(({url}) => url.includes("query=alice")), true);
});

test("keeps loading/error states and suppresses stale dashboard responses", async () => {
  const oldSummary = deferred();
  const oldTable = deferred();
  let generation = 0;
  const request = (url) => {
    if (url.includes("/summary")) return generation++ === 0 ? oldSummary.promise : Promise.resolve(response({account_count: 9}));
    if (url.includes("/table")) return generation++ === 1 ? oldTable.promise : Promise.resolve(response({rows: [{username: "new"}], total_pages: 1}));
    return Promise.resolve(response({accounts: [], existing_candidates: []}));
  };
  const {ui, events} = harness({request});
  const first = ui.refreshDashboard();
  const second = ui.refreshDashboard();
  await second;
  oldSummary.resolve(response({account_count: 1}));
  oldTable.resolve(response({rows: [{username: "old"}], total_pages: 1}));
  await first;
  assert.equal(ui.state.summary.account_count, 9);
  assert.equal(ui.state.table.rows[0].username, "new");
  assert.equal(events.some(({event}) => event === "loading"), true);

  const failed = harness({request: async () => response({error: {message: "<script>bad</script>"}}, 500)});
  await failed.ui.refreshDashboard();
  assert.equal(failed.ui.state.error, "<script>bad</script>");
  assert.equal(failed.events.at(-1).event, "error");
});

test("marks an existing table stale when a refresh fails", async () => {
  let fail = false;
  const request = async (url) => {
    if (fail) return response({error: {message: "网络不可用"}}, 503);
    if (url.includes("/summary")) return response({account_count: 1});
    return response({rows: [{username: "kept"}], page: 1, total_pages: 1});
  };
  const {ui, events} = harness({request});
  await ui.refreshDashboard();
  fail = true;
  await ui.refreshDashboard();
  assert.equal(ui.state.stale, true);
  assert.equal(ui.state.table.rows[0].username, "kept");
  assert.match(events.at(-1).payload.message, /显示上次结果/);
});

test("an invalid date cancels the active generation and a later success clears validation", async () => {
  const oldSummary = deferred();
  const oldTable = deferred();
  let useOld = true;
  let aborted = 0;
  class FakeAbortController { constructor() { this.signal = {}; } abort() { aborted += 1; } }
  const request = (url) => {
    if (useOld) return url.includes("/summary") ? oldSummary.promise : oldTable.promise;
    return Promise.resolve(response(url.includes("/summary") ? {account_count: 2} : {rows: [], page: 1, total_pages: 0}));
  };
  const {ui, events} = harness({request, AbortController: FakeAbortController});
  const old = ui.refreshDashboard();
  await ui.updateQuery({mode: "range", start: "2026-07-23", end: "2026-07-22"});
  assert.equal(aborted, 1);
  oldSummary.resolve(response({account_count: 99}));
  oldTable.resolve(response({rows: [{username: "stale"}], page: 1, total_pages: 1}));
  await old;
  assert.equal(ui.state.summary, null);
  assert.equal(ui.state.table, null);

  useOld = false;
  await ui.updateQuery({start: "2026-07-21", end: "2026-07-22"});
  assert.equal(ui.state.validation, "");
  assert.equal(events.at(-1).event, "dashboard");
});

test("invalid dates end the visible loading state and a valid retry clears the date error immediately", async () => {
  const oldSummary = deferred();
  const oldTable = deferred();
  const newSummary = deferred();
  const newTable = deferred();
  let phase = "old";
  const {document, window, get} = domHarness(async (url) => {
    if (url === "/api/tiktok-stats/accounts") return response({accounts: [], existing_candidates: []});
    if (url.includes("/summary")) return phase === "old" ? oldSummary.promise : newSummary.promise;
    if (url.includes("/table")) return phase === "old" ? oldTable.promise : newTable.promise;
    return response({});
  });
  const ui = require("../gateway/static/tiktok_stats.js").boot(document, window);
  await Promise.resolve();
  assert.equal(ui.state.loading, true);
  assert.equal(get("page-status").textContent, "正在加载数据…");

  await ui.updateQuery({mode: "range", start: "2026-07-23", end: "2026-07-22"});
  assert.equal(ui.state.loading, false);
  assert.equal(get("page-status").textContent, "");
  assert.equal(get("date-error").textContent, "开始日期不能晚于结束日期");

  phase = "new";
  const valid = ui.updateQuery({start: "2026-07-21", end: "2026-07-22"});
  assert.equal(get("date-error").textContent, "");
  assert.equal(get("page-status").textContent, "正在加载数据…");
  newSummary.resolve(response({account_count: 0}));
  newTable.resolve(response({rows: [], page: 1, total_pages: 0}));
  await valid;
});

test("safe renderer uses text nodes for hostile usernames and errors", () => {
  const created = [];
  const document = {
    createElement(tag) {
      const node = {tag, children: [], attributes: {}, append(child) { this.children.push(child); }, setAttribute(k, v) { this.attributes[k] = v; }};
      created.push(node);
      return node;
    },
  };
  const renderer = createSafeRenderer(document);
  const row = renderer.accountRow({account_id: 7, username: "<img src=x onerror=alert(1)>", posts_delta: -2, likes_delta: null, posts_total: 12, likes_total: null});
  assert.equal(row.children[0].textContent, "<img src=x onerror=alert(1)>");
  assert.equal(row.children[1].textContent, "-2");
  assert.equal(row.children[1].className, "metric metric-negative");
  assert.equal(row.children[2].textContent, "暂无");
  assert.equal(row.children[2].className, "metric metric-unavailable");
  assert.equal(row.children[5].textContent, "12");
  assert.equal(row.children[6].textContent, "暂无");
  assert.equal(created.some((node) => node.tag === "script" || node.tag === "img"), false);
});

test("metric formatting distinguishes positive, zero, negative, and null", () => {
  assert.deepEqual(formatMetric(5), {text: "+5", kind: "positive"});
  assert.deepEqual(formatMetric(0), {text: "0", kind: "zero"});
  assert.deepEqual(formatMetric(-5), {text: "-5", kind: "negative"});
  assert.deepEqual(formatMetric(null), {text: "暂无", kind: "unavailable"});
});

test("pasted and selected imports wait for persistence then refetch", async () => {
  const calls = [];
  const request = async (url, init = {}) => {
    calls.push({url, init});
    if (url.endsWith("/from-existing")) return response({summary: {added: 1}, items: [{status: "added", value: "bob"}]});
    if (url === "/api/tiktok-stats/accounts" && init.method === "POST") return response({summary: {added: 1, invalid: 1}, items: [{status: "added", value: "alice"}, {status: "invalid", value: "bad!"}]});
    if (url.startsWith("/api/tiktok-stats/accounts")) return response({accounts: [], existing_candidates: []});
    if (url.startsWith("/api/tiktok-stats/summary")) return response({account_count: 1});
    if (url.startsWith("/api/tiktok-stats/table")) return response({rows: [], total_pages: 0});
    return response({});
  };
  const {ui} = harness({request});
  const pasted = await ui.importText("@alice\nbad!");
  const selected = await ui.importExisting(["candidate-1"]);
  assert.equal(pasted.items[1].status, "invalid");
  assert.equal(selected.items[0].status, "added");
  assert.deepEqual(JSON.parse(calls[0].init.body), {text: "@alice\nbad!"});
  assert.deepEqual(JSON.parse(calls.find(({url}) => url.endsWith("/from-existing")).init.body), {candidate_ids: ["candidate-1"]});
  assert.equal(calls.filter(({url}) => url === "/api/tiktok-stats/accounts" && !url.includes("from-existing")).length >= 2, true);
});

test("cookie is one-way, never enters state or URL, and operations use POST", async () => {
  const secret = fakeCookie("do-not-retain");
  const calls = [];
  const request = async (url, init = {}) => {
    calls.push({url, init});
    if (url === "/api/tiktok-stats/settings/cookie" && init.method === "PUT") return response({status: {configured: true, masked_hint: "sess…ain"}});
    if (url === "/api/tiktok-stats/settings/cookie") return response({status: {configured: true, state: "configured", masked_hint: "sess…ain"}});
    if (url.endsWith("/validate")) return response({status: {configured: true, state: "valid"}});
    if (url.endsWith("/runs")) return response({run: {run_id: "r1"}}, 202);
    if (url.endsWith("/status")) return response({scraper: {running: true}, worker: {running: false}});
    return response({accounts: [], existing_candidates: []});
  };
  const {ui, history} = harness({request});
  await ui.openSettings();
  assert.equal(ui.state.settings.cookieInput, "");
  await ui.saveCookie(secret);
  await ui.validateCookie();
  await ui.runNow("incremental");
  await ui.runNow("full");
  assert.equal(JSON.stringify(ui.state).includes(secret), false);
  assert.equal((history.url || "").includes(secret), false);
  assert.equal(ui.state.settings.cookieInput, "");
  assert.equal(calls.filter(({url, init}) => url.endsWith("/runs") && init.method === "POST").length, 2);
  assert.equal(calls.some(({url, init}) => url.endsWith("/validate") && init.method === "POST"), true);
});

test("cookie and manual-run failures leave clear safe feedback", async () => {
  const request = async () => response({error: {message: "服务暂时不可用 <b>稍后重试</b>"}}, 503);
  const {ui, events} = harness({request});
  await assert.rejects(() => ui.saveCookie(fakeCookie("never-store")), /服务暂时不可用/);
  assert.equal(ui.state.settings.cookieInput, "");
  assert.equal(ui.state.settings.operation, "Cookie 保存失败，请稍后重试");
  assert.equal(JSON.stringify(ui.state).includes("never-store"), false);
  assert.equal(events.at(-1).event, "settings");
  await assert.rejects(() => ui.validateCookie(), /服务暂时不可用/);
  assert.equal(ui.state.settings.operation, "Cookie 验证失败，请稍后重试");
  await assert.rejects(() => ui.runNow("full"), /服务暂时不可用/);
  assert.equal(ui.state.settings.operation, "服务暂时不可用 <b>稍后重试</b>");
});

test("real route CookieStatus fields render without invented hints", async () => {
  const routeStatus = {configured: true, state: "valid", message: "Cookie validation succeeded", checked_at: "2026-07-22T01:02:03Z"};
  const {document, window, get} = domHarness(async (url) => {
    if (url.includes("/summary")) return response({account_count: 0});
    if (url.includes("/table")) return response({rows: [], page: 1, total_pages: 0});
    if (url === "/api/tiktok-stats/accounts") return response({accounts: [], existing_candidates: []});
    if (url === "/api/tiktok-stats/settings/cookie") return response({status: routeStatus});
    if (url === "/api/tiktok-stats/status") return response({scraper: {running: true}, worker: {running: false}});
    return response({});
  });
  const ui = require("../gateway/static/tiktok_stats.js").boot(document, window);
  await ui.openSettings();
  assert.equal(get("cookie-state").textContent, "已安全保存");
  assert.match(get("cookie-validation").textContent, /2026-07-22T01:02:03Z/);
  assert.match(get("cookie-validation").textContent, /验证通过/);
  assert.equal(get("cookie-validation").textContent.includes("undefined"), false);
});

test("real DOM async handlers catch failures and show them in the correct visible area", async () => {
  let fail = false;
  const {document, window, get} = domHarness(async (url, init = {}) => {
    if (fail) return response({error: {message: "<img src=x> 请求失败"}}, 503);
    if (url.includes("/summary")) return response({account_count: 0});
    if (url.includes("/table")) return response({rows: [], page: 1, total_pages: 0});
    if (url === "/api/tiktok-stats/accounts") return response({accounts: [], existing_candidates: []});
    if (url === "/api/tiktok-stats/settings/cookie") return response({status: {configured: false, state: "missing", message: null, checked_at: null}});
    if (url === "/api/tiktok-stats/status") return response({scraper: {running: true}, worker: {running: true}});
    return response({});
  });
  const ui = require("../gateway/static/tiktok_stats.js").boot(document, window);
  await ui.init();
  fail = true;

  await get("open-import").listeners.click({currentTarget: get("open-import")});
  assert.equal(ui.state.accountError, "<img src=x> 请求失败");
  assert.equal(get("import-error").textContent, "<img src=x> 请求失败");
  assert.equal(get("page-status").textContent, "<img src=x> 请求失败");

  get("import-text").value = "alice";
  await get("paste-import-form").listeners.submit({preventDefault() {}, currentTarget: get("paste-import-form")});
  assert.equal(ui.state.importError, "<img src=x> 请求失败");
  assert.equal(get("import-error").textContent, "<img src=x> 请求失败");

  await get("open-settings").listeners.click({currentTarget: get("open-settings")});
  assert.equal(get("settings-message").textContent, "设置读取失败，请稍后重试");

  get("cookie-input").value = fakeCookie("not-state");
  await get("cookie-form").listeners.submit({preventDefault() {}, currentTarget: get("cookie-form")});
  await get("validate-cookie").listeners.click({currentTarget: get("validate-cookie")});
  await get("run-full").listeners.click({currentTarget: get("run-full")});
  assert.equal(JSON.stringify(ui.state).includes("not-state"), false);
  assert.notEqual(get("settings-message").textContent, "");
});

test("page source links safe static assets and dashboard navigation", () => {
  const fs = require("node:fs");
  const template = fs.readFileSync("gateway/templates/tiktok_stats.html", "utf8");
  const sidebar = fs.readFileSync("gateway/templates/_dashboard_sidebar.html", "utf8");
  const styles = fs.readFileSync("gateway/static/tiktok_stats.css", "utf8");
  assert.match(template, /tiktok_stats\.css/);
  assert.match(template, /dashboard_shell\.css/);
  assert.match(template, /meta name="csrf-token"/);
  assert.match(template, /management_fetch\.js/);
  assert.match(template, /tiktok_stats\.js/);
  assert.match(template, /id="tiktok-stats-app"/);
  assert.match(template, /dashboard-shell/);
  assert.match(template, /_dashboard_sidebar\.html/);
  assert.doesNotMatch(template, /class="site-header"/);
  assert.match(sidebar, /href=["']\/tiktok-stats["']/);
  assert.match(styles, /\.filter-grid\s*\{[^}]*repeat\(auto-fit,/s);
  assert.match(styles, /#range-end-wrap\[hidden\]\s*\{\s*display:\s*none\s*!important;/);
});

const DETAIL = {
  account: {id: 7, username: "alice", status: "enabled"},
  current_totals: {posts: 12, likes: 120, views: 1200, comments: 18},
  daily_series: [
    {business_date: "2026-07-20", posts_delta: 0, likes_delta: null, views_delta: -4, comments_delta: 2},
    {business_date: "2026-07-21", posts_delta: 1, likes_delta: 5, views_delta: 40, comments_delta: 0},
  ],
  posts: [
    {video_id: "old", description: "old", created_at: "2026-07-19T00:00:00Z", view_count: 99, like_count: 20, comment_count: 3, is_deleted: true, deleted_detected_at: "2026-07-22T03:00:00Z"},
    {video_id: "new", description: "new", created_at: "2026-07-21T00:00:00Z", view_count: 10, like_count: 2, comment_count: 1, is_deleted: false},
  ],
  runs: [{run_id: 3, status: "completed", started_at: "2026-07-22T03:00:00Z"}],
  errors: [{run_id: 2, code: "upstream", message: "timeout"}],
};

const TRENDS = {
  metric: "posts_delta",
  accounts: [{account_id: 7, username: "alice", status: "enabled"}],
  rows: [{business_date: "2026-07-21", values: {"7": -1}}],
  page: 1,
  page_size: 20,
  total_accounts: 1,
  total_pages: 1,
};

test("table to detail and direct detail URL preserve the full recoverable table state", async () => {
  const calls = [];
  const {ui, history} = harness({
    search: "?mode=range&start=2026-07-01&end=2026-07-21&q=ali&status=disabled&baseline=incomplete&sort=likes&direction=asc&page=3&page_size=20&view=table",
    request: async (url) => {
      calls.push(url);
      if (url.includes("/detail?")) return response(DETAIL);
      if (url.includes("/summary")) return response({account_count: 1});
      if (url.includes("/table")) return response({rows: [], page: 3, total_pages: 3});
      return response({accounts: [], existing_candidates: []});
    },
  });
  await ui.init();
  await ui.openDetail(7);
  assert.equal(ui.state.query.view, "detail");
  assert.equal(ui.state.query.accountId, 7);
  assert.match(history.url, /view=detail/);
  assert.match(history.url, /q=ali/);
  assert.match(history.url, /page=3/);
  assert.equal(calls.some((url) => url.includes("/accounts/7/detail?start_date=2026-07-01&end_date=2026-07-21")), true);

  const directCalls = [];
  const direct = harness({search: history.url.slice(history.url.indexOf("?")), request: async (url) => { directCalls.push(url); return url.includes("/detail?") ? response(DETAIL) : response({accounts: [], existing_candidates: []}); }});
  await direct.ui.init();
  assert.equal(direct.ui.state.query.view, "detail");
  assert.equal(direct.ui.state.query.accountId, 7);
  assert.equal(directCalls.filter((url) => url === "/api/tiktok-stats/accounts").length, 0);
  await direct.ui.backToPrevious();
  assert.equal(direct.ui.state.query.view, "table");
  assert.equal(direct.ui.state.query.q, "ali");
  assert.equal(direct.ui.state.query.page, 3);
});

test("detail exposes totals, chart labels, sorted posts, deletions, runs and safe errors", async () => {
  const {ui, events} = harness({search: "?view=detail&account_id=7&mode=range&start=2026-07-20&end=2026-07-21", request: async (url) => url.includes("/detail?") ? response(DETAIL) : response({accounts: [], existing_candidates: []})});
  await ui.init();
  assert.deepEqual(ui.state.detail.current_totals, DETAIL.current_totals);
  assert.equal(events.some(({event}) => event === "detail"), true);
  assert.deepEqual(sortPosts(DETAIL.posts, "views", "desc").map((post) => post.video_id), ["old", "new"]);
  assert.deepEqual(sortPosts(DETAIL.posts, "published", "desc").map((post) => post.video_id), ["new", "old"]);
  const chart = buildChartModel(DETAIL.daily_series, "likes");
  assert.equal(chart.points[0].value, null);
  assert.match(chart.label, /点赞/);
  assert.match(chart.label, /2026-07-20/);
  assert.equal(DETAIL.posts[0].is_deleted, true);
  assert.equal(DETAIL.posts[0].deleted_detected_at, "2026-07-22T03:00:00Z");
  assert.equal(ui.state.detail.errors[0].message, "timeout");
});

test("detail has loading, empty, error and stale-response suppression states", async () => {
  const old = deferred();
  const events = [];
  const {ui} = harness({request: (url) => {
    if (url.includes("/accounts/7/detail")) return old.promise;
    if (url.includes("/accounts/8/detail")) return Promise.resolve(response({...DETAIL, account: {id: 8, username: "new", status: "enabled"}}));
    return Promise.resolve(response({accounts: [], existing_candidates: []}));
  }});
  ui.subscribeForTest = (event, payload) => events.push({event, payload});
  const first = ui.openDetail(7);
  const second = ui.openDetail(8);
  await second;
  old.resolve(response({...DETAIL, account: {id: 7, username: "stale", status: "enabled"}}));
  await first;
  assert.equal(ui.state.detail.account.id, 8);

  const empty = harness({search: "?view=detail&account_id=7&start=2026-07-22", request: async (url) => url.includes("/detail?") ? response({...DETAIL, current_totals: null, daily_series: [], posts: [], runs: [], errors: []}) : response({accounts: [], existing_candidates: []})});
  await empty.ui.init();
  assert.equal(empty.ui.state.detail.posts.length, 0);

  const failed = harness({search: "?view=detail&account_id=7&start=2026-07-22", request: async (url) => url.includes("/detail?") ? response({error: {message: "safe failure"}}, 503) : response({accounts: [], existing_candidates: []})});
  await failed.ui.init();
  assert.equal(failed.ui.state.error, "safe failure");
  assert.equal(failed.events.some(({event}) => event === "detail-loading"), true);
  assert.equal(failed.events.some(({event}) => event === "detail-error"), true);
});

test("a failed new detail target clears the previous account from state and DOM", async () => {
  const {document, window, get} = domHarness(async (url) => {
    if (url.includes("/accounts/7/detail")) return response(DETAIL);
    if (url.includes("/accounts/8/detail")) return response({error: {message: "账号 B 读取失败"}}, 503);
    if (url.includes("/summary")) return response({account_count: 0});
    if (url.includes("/table")) return response({rows: [], page: 1, total_pages: 0});
    return response({});
  });
  const ui = require("../gateway/static/tiktok_stats.js").boot(document, window);
  await ui.openDetail(7);
  assert.equal(get("detail-title").textContent, "@alice");
  assert.equal(get("detail-totals").children.length, 4);
  await ui.openDetail(8);
  assert.equal(ui.state.detail, null);
  assert.notEqual(get("detail-title").textContent, "@alice");
  assert.equal(get("detail-totals").children.length, 0);
  assert.equal(get("page-status").textContent, "账号 B 读取失败");
});

test("an invalid detail date clears prior account state and DOM before validation", async () => {
  const {document, window, get} = domHarness(async (url) => {
    if (url.includes("/accounts/7/detail")) return response(DETAIL);
    if (url.includes("/summary")) return response({account_count: 0});
    if (url.includes("/table")) return response({rows: [], page: 1, total_pages: 0});
    return response({});
  });
  const ui = require("../gateway/static/tiktok_stats.js").boot(document, window);
  await ui.openDetail(7);
  assert.equal(get("detail-title").textContent, "@alice");
  await ui.updateQuery({mode: "range", start: "2026-07-23", end: "2026-07-22"});
  assert.equal(ui.state.detail, null);
  assert.equal(get("detail-totals").children.length, 0);
  assert.equal(get("detail-title").textContent, "账号详情");
  assert.equal(get("date-error").textContent, "开始日期不能晚于结束日期");
});

test("trend direct URL and view switch support four metrics, search, range and server pagination", async () => {
  const requests = [];
  const {ui, history} = harness({search: "?view=trends&mode=range&start=2026-07-01&end=2026-07-21&q=ali&metric=views&trend_page=2&page_size=20", request: async (url) => {
    requests.push(url);
    if (url.includes("/trends?")) return response({...TRENDS, metric: new URL(`http://x${url}`).searchParams.get("metric"), page: 2});
    return response({accounts: [], existing_candidates: []});
  }});
  await ui.init();
  assert.equal(ui.state.query.view, "trends");
  assert.equal(requests.filter((url) => url === "/api/tiktok-stats/accounts").length, 0);
  assert.equal(requests.some((url) => url.includes("metric=views_delta") && url.includes("query=ali") && url.includes("page=2") && url.includes("page_size=20")), true);
  for (const metric of ["posts", "likes", "views", "comments"]) await ui.updateTrend({trendMetric: metric, trendPage: 1});
  assert.equal(requests.filter((url) => url.includes("/trends?")).length, 5);
  assert.match(history.url, /metric=comments/);
});

test("a failed trend query clears an older matrix instead of showing mismatched data", async () => {
  let fail = false;
  const {ui} = harness({search: "?view=trends&mode=range&start=2026-07-01&end=2026-07-21&metric=posts", request: async (url) => {
    if (url.includes("/trends?") && fail) return response({error: {message: "趋势读取失败"}}, 503);
    if (url.includes("/trends?")) return response(TRENDS);
    return response({});
  }});
  await ui.init();
  assert.equal(ui.state.trend.metric, "posts_delta");
  fail = true;
  await ui.updateTrend({trendMetric: "likes"});
  assert.equal(ui.state.trend, null);
  assert.equal(ui.state.error, "趋势读取失败");
});

test("an invalid trend date clears prior matrix state and DOM before validation", async () => {
  const {document, window, get} = domHarness(async (url) => {
    if (url.includes("/trends?")) return response(TRENDS);
    if (url.includes("/summary")) return response({account_count: 0});
    if (url.includes("/table")) return response({rows: [], page: 1, total_pages: 0});
    return response({});
  });
  const ui = require("../gateway/static/tiktok_stats.js").boot(document, window);
  await ui.showTrends();
  assert.equal(get("trend-body").children.length, 1);
  await ui.updateQuery({mode: "range", start: "2026-07-23", end: "2026-07-22"});
  assert.equal(ui.state.trend, null);
  assert.equal(get("trend-body").children.length, 0);
  assert.equal(get("date-error").textContent, "开始日期不能晚于结束日期");
});

test("trend matrix keeps null, zero, positive and negative meanings", () => {
  assert.deepEqual(formatMatrixCell(null), {text: "暂无数据", kind: "unavailable", label: "暂无数据，不可用"});
  assert.deepEqual(formatMatrixCell(0), {text: "0", kind: "zero", label: "0"});
  assert.deepEqual(formatMatrixCell(4), {text: "+4", kind: "positive", label: "增加 4"});
  assert.deepEqual(formatMatrixCell(-3), {text: "-3", kind: "negative", label: "减少 3"});
});

test("trend cell opens corresponding account and focused date then explicit back restores trends", async () => {
  const {ui} = harness({search: "?view=trends&mode=range&start=2026-07-01&end=2026-07-21&q=ali&metric=comments&trend_page=4&page_size=20", request: async (url) => {
    if (url.includes("/trends?")) return response(TRENDS);
    if (url.includes("/detail?")) return response(DETAIL);
    return response({accounts: [], existing_candidates: []});
  }});
  await ui.init();
  await ui.openDetail(7, "2026-07-12");
  assert.equal(ui.state.query.view, "detail");
  assert.equal(ui.state.query.focusDate, "2026-07-12");
  assert.equal(ui.state.query.from, "trends");
  await ui.backToPrevious();
  assert.equal(ui.state.query.view, "trends");
  assert.equal(ui.state.query.trendMetric, "comments");
  assert.equal(ui.state.query.trendPage, 4);
  assert.equal(ui.state.query.q, "ali");
});

test("detail and trend renderers keep hostile API text as text only", () => {
  const created = [];
  const document = {createElement(tag) { const node = {tag, children: [], attributes: {}, textContent: "", className: "", append(child) { this.children.push(child); }, setAttribute(k, v) { this.attributes[k] = v; }, addEventListener() {}}; created.push(node); return node; }};
  const safe = createSafeRenderer(document);
  const post = safe.postRow({description: "<img src=x onerror=alert(1)>", created_at: null, view_count: null, like_count: 0, comment_count: -1, is_deleted: true, deleted_detected_at: "<script>bad</script>"});
  const trend = safe.trendCell(null, {account_id: 7, username: "<svg onload=alert(1)>"}, "2026-07-21", () => {});
  assert.equal(post.children[0].textContent, "<img src=x onerror=alert(1)>");
  assert.equal(post.children.at(-1).textContent.includes("<script>bad</script>"), true);
  assert.equal(trend.textContent, "暂无数据");
  assert.equal(trend.attributes["aria-label"].includes("<svg onload=alert(1)>"), true);
  assert.equal(created.some((node) => ["img", "script", "svg"].includes(node.tag)), false);
});

test("clicking any non-link part of an account row opens its detail", () => {
  const document = {createElement(tag) { return {tag, children: [], attributes: {}, listeners: {}, textContent: "", className: "", append(child) { this.children.push(child); }, setAttribute(k, v) { this.attributes[k] = v; }, addEventListener(type, handler) { this.listeners[type] = handler; }}; }};
  let opened = 0;
  const row = createSafeRenderer(document).accountRow({account_id: 7, username: "alice"}, "?view=detail", () => { opened += 1; });
  row.listeners.click({currentTarget: row, target: row.children[1]});
  assert.equal(opened, 1);
});

test("page includes accessible table, detail and trend view containers", () => {
  const fs = require("node:fs");
  const template = fs.readFileSync("gateway/templates/tiktok_stats.html", "utf8");
  assert.match(template, /id="show-table-view"/);
  assert.match(template, /id="show-trends-view"/);
  assert.match(template, /id="detail-view"/);
  assert.match(template, /id="trend-view"/);
  assert.match(template, /id="detail-chart"/);
  assert.match(template, /aria-live="polite"/);
});
