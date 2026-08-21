(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.TikTokStatsUI = api;
})(typeof globalThis === "undefined" ? this : globalThis, function () {
  "use strict";

  const SORTS = new Set(["posts", "likes", "views", "comments"]);
  const STATUSES = new Set(["all", "enabled", "disabled"]);
  const BASELINES = new Set(["all", "ready", "first_day", "missing_previous", "incomplete", "missing", "missing_end", "incomplete_end", "incomplete_previous"]);
  const PAGE_SIZES = new Set([20, 50, 100, 200]);
  const POST_SORTS = new Set(["published", "views", "likes", "comments"]);
  const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

  function defaultState(today) {
    return {mode: "single", start: today, end: today, q: "", status: "all", baseline: "all", sort: "posts", direction: "desc", page: 1, pageSize: 50, view: "table"};
  }

  function validDate(value) {
    if (!ISO_DATE.test(value || "")) return false;
    const date = new Date(`${value}T00:00:00Z`);
    return !Number.isNaN(date.valueOf()) && date.toISOString().slice(0, 10) === value;
  }

  function positiveInt(value, fallback) {
    const parsed = Number(value);
    return Number.isInteger(parsed) && parsed > 0 ? parsed : fallback;
  }

  function restoreState(search, today) {
    const defaults = defaultState(today);
    const values = new URLSearchParams(String(search || "").replace(/^\?/, ""));
    const mode = values.get("mode") === "range" ? "range" : "single";
    const start = validDate(values.get("start")) ? values.get("start") : defaults.start;
    const end = mode === "single" ? start : (validDate(values.get("end")) ? values.get("end") : defaults.end);
    const pageSize = positiveInt(values.get("page_size"), defaults.pageSize);
    const state = {
      mode,
      start,
      end,
      q: (values.get("q") || "").slice(0, 200),
      status: STATUSES.has(values.get("status")) ? values.get("status") : defaults.status,
      baseline: BASELINES.has(values.get("baseline")) ? values.get("baseline") : defaults.baseline,
      sort: SORTS.has(values.get("sort")) ? values.get("sort") : defaults.sort,
      direction: values.get("direction") === "asc" ? "asc" : "desc",
      page: positiveInt(values.get("page"), defaults.page),
      pageSize: PAGE_SIZES.has(pageSize) ? pageSize : defaults.pageSize,
      view: "table",
    };
    const requestedView = values.get("view");
    const accountId = positiveInt(values.get("account_id"), 0);
    if (requestedView === "detail" && accountId) {
      state.view = "detail";
      state.accountId = accountId;
      state.focusDate = validDate(values.get("focus_date")) ? values.get("focus_date") : "";
      state.from = values.get("from") === "trends" ? "trends" : "table";
      if (state.from === "trends") {
        state.trendMetric = SORTS.has(values.get("metric")) ? values.get("metric") : "posts";
        state.trendPage = positiveInt(values.get("trend_page"), 1);
      }
    } else if (requestedView === "trends" || requestedView === "trend") {
      state.view = "trends";
      state.trendMetric = SORTS.has(values.get("metric")) ? values.get("metric") : "posts";
      state.trendPage = positiveInt(values.get("trend_page"), 1);
    }
    return state;
  }

  function stateToSearch(state) {
    const values = new URLSearchParams();
    values.set("mode", state.mode);
    values.set("start", state.start);
    values.set("end", state.mode === "single" ? state.start : state.end);
    if (state.q) values.set("q", state.q);
    values.set("status", state.status);
    values.set("baseline", state.baseline);
    values.set("sort", state.sort);
    values.set("direction", state.direction);
    values.set("page", String(state.page));
    values.set("page_size", String(state.pageSize));
    values.set("view", state.view);
    if (state.view === "trends") {
      values.set("metric", SORTS.has(state.trendMetric) ? state.trendMetric : "posts");
      values.set("trend_page", String(positiveInt(state.trendPage, 1)));
    } else if (state.view === "detail") {
      values.set("account_id", String(state.accountId));
      if (state.focusDate) values.set("focus_date", state.focusDate);
      values.set("from", state.from === "trends" ? "trends" : "table");
      if (state.from === "trends") {
        values.set("metric", SORTS.has(state.trendMetric) ? state.trendMetric : "posts");
        values.set("trend_page", String(positiveInt(state.trendPage, 1)));
      }
    }
    return `?${values.toString()}`;
  }

  function validateDates(state) {
    if (!validDate(state.start) || !validDate(state.mode === "single" ? state.start : state.end)) return "请选择有效日期";
    if (state.mode === "range" && state.start > state.end) return "开始日期不能晚于结束日期";
    return "";
  }

  function formatMetric(value) {
    if (value === null || value === undefined) return {text: "暂无", kind: "unavailable"};
    const number = Number(value);
    if (number > 0) return {text: `+${number.toLocaleString("zh-CN")}`, kind: "positive"};
    if (number < 0) return {text: number.toLocaleString("zh-CN"), kind: "negative"};
    return {text: "0", kind: "zero"};
  }

  function formatMatrixCell(value) {
    if (value === null || value === undefined) return {text: "暂无数据", kind: "unavailable", label: "暂无数据，不可用"};
    const number = Number(value);
    if (number > 0) return {text: `+${number.toLocaleString("zh-CN")}`, kind: "positive", label: `增加 ${number.toLocaleString("zh-CN")}`};
    if (number < 0) return {text: number.toLocaleString("zh-CN"), kind: "negative", label: `减少 ${Math.abs(number).toLocaleString("zh-CN")}`};
    return {text: "0", kind: "zero", label: "0"};
  }

  function sortPosts(posts, sort, direction) {
    const field = ({published: "created_at", views: "view_count", likes: "like_count", comments: "comment_count"})[sort] || "created_at";
    const factor = direction === "asc" ? 1 : -1;
    return [...(posts || [])].sort((left, right) => {
      const a = left[field];
      const b = right[field];
      if (a == null && b == null) return String(left.video_id || "").localeCompare(String(right.video_id || ""));
      if (a == null) return 1;
      if (b == null) return -1;
      if (a === b) return String(left.video_id || "").localeCompare(String(right.video_id || ""));
      return (a < b ? -1 : 1) * factor;
    });
  }

  function buildChartModel(series, metric) {
    const selected = SORTS.has(metric) ? metric : "views";
    const labels = {posts: "作品", likes: "点赞", views: "浏览", comments: "评论"};
    const points = (series || []).map((row) => ({date: row.business_date, value: row[`${selected}_delta`] == null ? null : Number(row[`${selected}_delta`])}));
    const available = points.filter((point) => point.value !== null).map((point) => point.value);
    const first = points[0] && points[0].date;
    const last = points.at(-1) && points.at(-1).date;
    return {
      metric: selected,
      points,
      min: available.length ? Math.min(...available) : null,
      max: available.length ? Math.max(...available) : null,
      label: `${labels[selected]}每日变化${first ? `，${first}${last && last !== first ? ` 至 ${last}` : ""}` : "，暂无数据"}`,
    };
  }

  function createSafeRenderer(document) {
    function cell(value, className) {
      const node = document.createElement("td");
      node.className = className || "";
      node.textContent = value === null || value === undefined ? "" : String(value);
      return node;
    }
    function metricCell(value) {
      const metric = formatMetric(value);
      return cell(metric.text, `metric metric-${metric.kind}`);
    }
    function totalCell(value) {
      if (value === null || value === undefined) return cell("暂无", "metric metric-unavailable");
      return cell(Number(value).toLocaleString("zh-CN"), "metric metric-total");
    }
    function accountRow(row, href, onOpen) {
      const tr = document.createElement("tr");
      tr.setAttribute("tabindex", "0");
      tr.setAttribute("role", "link");
      tr.setAttribute("aria-label", `查看账号 ${row.username || "未命名账号"} 的详情`);
      tr.append(cell(row.username || "未命名账号", "account-name"));
      tr.append(metricCell(row.posts_delta));
      tr.append(metricCell(row.likes_delta));
      tr.append(metricCell(row.views_delta));
      tr.append(metricCell(row.comments_delta));
      tr.append(totalCell(row.posts_total));
      tr.append(totalCell(row.likes_total));
      tr.append(totalCell(row.views_total));
      tr.append(totalCell(row.comments_total));
      tr.append(cell(baselineLabel(row.baseline_status), "baseline-state"));
      const action = document.createElement("td");
      const link = document.createElement("a");
      link.textContent = "查看详情";
      link.setAttribute("href", href || `/tiktok-stats?view=detail&account_id=${encodeURIComponent(String(row.account_id))}`);
      if (onOpen && link.addEventListener) link.addEventListener("click", (event) => { if (event && event.preventDefault) event.preventDefault(); onOpen(); });
      action.append(link);
      tr.append(action);
      if (onOpen && tr.addEventListener) {
        tr.addEventListener("click", (event) => { if (!event || !event.target || String(event.target.tagName || event.target.tag || "").toLowerCase() !== "a") onOpen(); });
        tr.addEventListener("keydown", (event) => { if (event && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); onOpen(); } });
      }
      return tr;
    }
    function postRow(post) {
      const tr = document.createElement("tr");
      tr.className = post.is_deleted ? "suspected-deleted" : "";
      tr.append(cell(post.description || "（无文案）", "post-description"));
      tr.append(cell(post.created_at || "暂无数据", post.created_at ? "" : "metric-unavailable"));
      tr.append(totalCell(post.view_count));
      tr.append(totalCell(post.like_count));
      tr.append(totalCell(post.comment_count));
      const deletion = post.is_deleted ? `疑似已删除；发现时间：${post.deleted_detected_at || "暂无数据"}` : "正常";
      tr.append(cell(deletion, post.is_deleted ? "deletion-warning" : ""));
      return tr;
    }
    function trendCell(value, account, businessDate, onOpen) {
      const metric = formatMatrixCell(value);
      const td = cell(metric.text, `metric metric-${metric.kind} trend-cell`);
      td.setAttribute("aria-label", `${businessDate}，账号 ${account.username || "未命名账号"}，${metric.label}`);
      if (onOpen) {
        td.setAttribute("tabindex", "0");
        td.setAttribute("role", "link");
        if (td.addEventListener) {
          td.addEventListener("click", onOpen);
          td.addEventListener("keydown", (event) => { if (event && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); onOpen(); } });
        }
      }
      return td;
    }
    return {accountRow, cell, metricCell, totalCell, postRow, trendCell};
  }

  function baselineLabel(value) {
    return ({ready: "完整", first_day: "首日数据", missing_previous: "缺少前日", incomplete: "未完整", missing: "暂无数据", missing_end: "结束日缺失", incomplete_end: "结束日未完整", incomplete_previous: "前日未完整"})[value] || "暂无数据";
  }

  function apiError(payload, status) {
    const message = payload && payload.error && payload.error.message;
    return new Error(message || `请求失败（${status}）`);
  }

  function createTikTokStatsController(dependencies) {
    const request = dependencies.request;
    const render = dependencies.render || (() => {});
    const history = dependencies.history || {replaceState() {}};
    const location = dependencies.location || {pathname: "/tiktok-stats", search: ""};
    const today = dependencies.today || (() => new Date().toISOString().slice(0, 10));
    const schedule = dependencies.setTimeout || setTimeout;
    const cancelSchedule = dependencies.clearTimeout || clearTimeout;
    const Abort = dependencies.AbortController || (typeof AbortController === "function" ? AbortController : null);
    let searchTimer = null;
    let requestSequence = 0;
    let activeAbort = null;
    let internalNavigation = false;

    const state = {
      query: restoreState(location.search, today()),
      summary: null,
      table: null,
      detail: null,
      trend: null,
      postSort: {sort: "published", direction: "desc"},
      accounts: [],
      existingCandidates: [],
      loading: false,
      stale: false,
      error: "",
      accountError: "",
      importError: "",
      validation: "",
      importResult: null,
      settings: {cookieInput: "", cookieStatus: null, serviceStatus: null, operation: ""},
    };

    async function readJson(url, init) {
      const response = await request(url, init || {});
      const payload = await response.json();
      if (!response.ok) throw apiError(payload, response.status);
      return payload;
    }

    function filterParams(query) {
      const values = new URLSearchParams();
      if (query.mode === "single") values.set("date", query.start);
      else {
        values.set("start_date", query.start);
        values.set("end_date", query.end);
      }
      if (query.q) values.set("query", query.q);
      if (query.status !== "all") values.set("status", query.status);
      if (query.baseline !== "all") values.set("baseline_status", query.baseline);
      return values;
    }

    function syncUrl(method) {
      const name = method === "push" && typeof history.pushState === "function" ? "pushState" : "replaceState";
      history[name]({}, "", `${location.pathname || "/tiktok-stats"}${stateToSearch(state.query)}`);
    }

    function cancelActiveRequest() {
      requestSequence += 1;
      if (activeAbort) activeAbort.abort();
      activeAbort = null;
    }

    function startRequest() {
      cancelActiveRequest();
      activeAbort = Abort ? new Abort() : null;
      return {sequence: requestSequence, init: activeAbort ? {signal: activeAbort.signal} : {}};
    }

    function validRequest(sequence, error) {
      return sequence === requestSequence && !(error && error.name === "AbortError");
    }

    async function refreshDashboard() {
      cancelActiveRequest();
      const validation = validateDates(state.query);
      state.validation = validation;
      if (validation) {
        state.loading = false;
        state.error = "";
        render("validation", {message: validation, state});
        return false;
      }
      render("validation", {message: "", state});
      activeAbort = Abort ? new Abort() : null;
      const sequence = requestSequence;
      state.loading = true;
      state.error = "";
      render("loading", {state});
      const filters = filterParams(state.query);
      const tableParams = new URLSearchParams(filters);
      tableParams.set("sort", `${state.query.sort}_delta`);
      tableParams.set("direction", state.query.direction);
      tableParams.set("page", String(state.query.page));
      tableParams.set("page_size", String(state.query.pageSize));
      const init = activeAbort ? {signal: activeAbort.signal} : {};
      try {
        const [summary, table] = await Promise.all([
          readJson(`/api/tiktok-stats/summary?${filters.toString()}`, init),
          readJson(`/api/tiktok-stats/table?${tableParams.toString()}`, init),
        ]);
        if (sequence !== requestSequence) return false;
        state.summary = summary;
        state.table = table;
        state.loading = false;
        state.stale = false;
        render("dashboard", {summary, table, state});
        return true;
      } catch (error) {
        if (!validRequest(sequence, error)) return false;
        state.loading = false;
        state.stale = Boolean(state.table);
        const reason = error && error.message ? error.message : "加载失败，请稍后重试";
        state.error = state.stale ? `加载失败，正在显示上次结果。${reason}` : reason;
        render("error", {message: state.error, state});
        return false;
      }
    }

    async function loadDetail() {
      cancelActiveRequest();
      state.detail = null;
      state.stale = false;
      render("detail-reset", {state});
      const validation = validateDates(state.query);
      state.validation = validation;
      if (validation || !positiveInt(state.query.accountId, 0)) {
        state.loading = false;
        state.error = validation || "请选择有效账号";
        render("validation", {message: validation, state});
        render("detail-error", {message: state.error, state});
        return false;
      }
      render("validation", {message: "", state});
      const {sequence, init} = startRequest();
      state.loading = true;
      state.error = "";
      render("detail-loading", {state});
      const params = new URLSearchParams({start_date: state.query.start, end_date: state.query.mode === "single" ? state.query.start : state.query.end});
      try {
        const detail = await readJson(`/api/tiktok-stats/accounts/${encodeURIComponent(String(state.query.accountId))}/detail?${params.toString()}`, init);
        if (sequence !== requestSequence) return false;
        state.detail = detail;
        state.loading = false;
        state.stale = false;
        render("detail", {detail, state});
        return true;
      } catch (error) {
        if (!validRequest(sequence, error)) return false;
        state.loading = false;
        state.stale = Boolean(state.detail);
        state.error = error && error.message ? error.message : "账号详情加载失败，请稍后重试";
        render("detail-error", {message: state.error, state});
        return false;
      }
    }

    async function loadTrends() {
      cancelActiveRequest();
      state.trend = null;
      state.stale = false;
      render("trend-reset", {state});
      const validation = validateDates(state.query);
      state.validation = validation;
      if (validation) {
        state.loading = false;
        state.error = validation;
        render("validation", {message: validation, state});
        render("trend-error", {message: validation, state});
        return false;
      }
      render("validation", {message: "", state});
      const {sequence, init} = startRequest();
      state.loading = true;
      state.error = "";
      render("trend-loading", {state});
      const params = new URLSearchParams({
        metric: `${SORTS.has(state.query.trendMetric) ? state.query.trendMetric : "posts"}_delta`,
        start_date: state.query.start,
        end_date: state.query.mode === "single" ? state.query.start : state.query.end,
        page: String(positiveInt(state.query.trendPage, 1)),
        page_size: String(state.query.pageSize),
      });
      if (state.query.q) params.set("query", state.query.q);
      try {
        const trend = await readJson(`/api/tiktok-stats/trends?${params.toString()}`, init);
        if (sequence !== requestSequence) return false;
        state.trend = trend;
        state.loading = false;
        state.stale = false;
        render("trend", {trend, state});
        return true;
      } catch (error) {
        if (!validRequest(sequence, error)) return false;
        state.loading = false;
        state.stale = Boolean(state.trend);
        state.error = error && error.message ? error.message : "趋势加载失败，请稍后重试";
        render("trend-error", {message: state.error, state});
        return false;
      }
    }

    function refreshCurrentView() {
      if (state.query.view === "detail") return loadDetail();
      if (state.query.view === "trends") return loadTrends();
      return refreshDashboard();
    }

    async function loadAccounts(existingQuery) {
      const suffix = existingQuery ? `?existing_query=${encodeURIComponent(existingQuery)}` : "";
      state.accountError = "";
      try {
        const payload = await readJson(`/api/tiktok-stats/accounts${suffix}`);
        state.accounts = payload.accounts || [];
        state.existingCandidates = payload.existing_candidates || [];
        render("accounts", {accounts: state.accounts, existingCandidates: state.existingCandidates, state});
        return payload;
      } catch (error) {
        state.accountError = error && error.message ? error.message : "账号读取失败，请稍后重试";
        render("accounts-error", {message: state.accountError, state});
        throw error;
      }
    }

    async function init() {
      syncUrl();
      await refreshCurrentView();
      return state;
    }

    async function updateQuery(patch) {
      state.query = {...state.query, ...patch};
      if (state.query.mode === "single") state.query.end = state.query.start;
      syncUrl();
      render("query", {query: state.query, state});
      if (state.query.view === "trends") state.query.trendPage = positiveInt(state.query.trendPage, 1);
      return refreshCurrentView();
    }

    async function navigate(patch, method) {
      state.query = {...state.query, ...patch};
      if (state.query.mode === "single") state.query.end = state.query.start;
      syncUrl(method || "push");
      render("query", {query: state.query, state});
      return refreshCurrentView();
    }

    function openDetail(accountId, focusDate) {
      const id = positiveInt(accountId, 0);
      if (!id) return Promise.reject(new Error("请选择有效账号"));
      const origin = state.query.view === "trends" ? "trends" : "table";
      internalNavigation = true;
      return navigate({view: "detail", accountId: id, focusDate: validDate(focusDate) ? focusDate : "", from: origin}, "push");
    }

    function showTrends() {
      internalNavigation = true;
      return navigate({view: "trends", trendMetric: SORTS.has(state.query.trendMetric) ? state.query.trendMetric : "posts", trendPage: positiveInt(state.query.trendPage, 1)}, "push");
    }

    function showTable() {
      internalNavigation = true;
      return navigate({view: "table"}, "push");
    }

    function updateTrend(patch) {
      const next = {...patch};
      if (next.trendMetric && !SORTS.has(next.trendMetric)) next.trendMetric = "posts";
      next.view = "trends";
      return navigate(next, "replace");
    }

    function setPostSort(sort, direction) {
      state.postSort = {sort: POST_SORTS.has(sort) ? sort : "published", direction: direction === "asc" ? "asc" : "desc"};
      if (state.detail) render("detail", {detail: state.detail, state});
      return state.postSort;
    }

    function handlePop(search) {
      internalNavigation = false;
      cancelActiveRequest();
      state.query = restoreState(search, today());
      render("query", {query: state.query, state});
      return refreshCurrentView();
    }

    function backToPrevious() {
      if (internalNavigation && typeof history.back === "function") {
        history.back();
        return Promise.resolve(true);
      }
      internalNavigation = false;
      const target = state.query.from === "trends" ? "trends" : "table";
      const patch = {view: target};
      if (target === "trends") {
        patch.trendMetric = SORTS.has(state.query.trendMetric) ? state.query.trendMetric : "posts";
        patch.trendPage = positiveInt(state.query.trendPage, 1);
      }
      return navigate(patch, "replace");
    }

    function search(value) {
      if (searchTimer !== null) cancelSchedule(searchTimer);
      searchTimer = schedule(() => {
        searchTimer = null;
        const patch = {q: String(value || "").trim(), page: 1};
        if (state.query.view === "trends") patch.trendPage = 1;
        return updateQuery(patch);
      }, 300);
    }

    async function submitImport(url, body) {
      state.importError = "";
      try {
        const payload = await readJson(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
        state.importResult = payload;
        render("import-result", {result: payload, state});
        await loadAccounts();
        await refreshDashboard();
        return payload;
      } catch (error) {
        state.importError = error && error.message ? error.message : "导入失败，请稍后重试";
        render("import-error", {message: state.importError, state});
        throw error;
      }
    }

    function importText(text) {
      return submitImport("/api/tiktok-stats/accounts", {text: String(text || "")});
    }

    function importExisting(candidateIds) {
      return submitImport("/api/tiktok-stats/accounts/from-existing", {candidate_ids: [...candidateIds]});
    }

    async function openSettings() {
      state.settings.cookieInput = "";
      state.settings.operation = "";
      try {
        const [cookie, service] = await Promise.all([
          readJson("/api/tiktok-stats/settings/cookie"),
          readJson("/api/tiktok-stats/status"),
        ]);
        state.settings.cookieStatus = cookie.status || null;
        state.settings.serviceStatus = service;
        render("settings", {settings: state.settings, state});
        return state.settings;
      } catch (error) {
        state.settings.operation = "设置读取失败，请稍后重试";
        render("settings", {settings: state.settings, state});
        throw error;
      }
    }

    async function saveCookie(value) {
      state.settings.cookieInput = "";
      try {
        const payload = await readJson("/api/tiktok-stats/settings/cookie", {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({cookie: String(value || "")})});
        state.settings.cookieStatus = payload.status || null;
        state.settings.cookieInput = "";
        state.settings.operation = "Cookie 已安全保存";
        render("settings", {settings: state.settings, state});
        return payload;
      } catch (error) {
        state.settings.cookieInput = "";
        state.settings.operation = "Cookie 保存失败，请稍后重试";
        render("settings", {settings: state.settings, state});
        throw error;
      }
    }

    async function validateCookie() {
      state.settings.operation = "正在验证…";
      render("settings", {settings: state.settings, state});
      try {
        const payload = await readJson("/api/tiktok-stats/settings/cookie/validate", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
        state.settings.cookieStatus = payload.status || null;
        state.settings.operation = payload.status && payload.status.state === "valid" ? "Cookie 有效" : "Cookie 验证未通过";
        render("settings", {settings: state.settings, state});
        return payload;
      } catch (error) {
        state.settings.operation = "Cookie 验证失败，请稍后重试";
        render("settings", {settings: state.settings, state});
        throw error;
      }
    }

    async function runNow(runType) {
      if (!new Set(["incremental", "full"]).has(runType)) throw new Error("不支持的采集类型");
      state.settings.operation = "任务正在加入队列…";
      render("settings", {settings: state.settings, state});
      try {
        const payload = await readJson("/api/tiktok-stats/runs", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({run_type: runType})});
        state.settings.operation = "采集任务已加入队列";
        render("settings", {settings: state.settings, state});
        return payload;
      } catch (error) {
        state.settings.operation = error && error.message ? error.message : "任务提交失败，请稍后重试";
        render("settings", {settings: state.settings, state});
        throw error;
      }
    }

    return {state, init, refreshDashboard, loadDetail, loadTrends, refreshCurrentView, loadAccounts, updateQuery, updateTrend, openDetail, showTrends, showTable, backToPrevious, handlePop, setPostSort, search, importText, importExisting, openSettings, saveCookie, validateCookie, runNow};
  }

  function boot(document, window) {
    const root = document.querySelector("#tiktok-stats-app");
    if (!root) return null;
    const safe = createSafeRenderer(document);
    const byId = (id) => document.getElementById(id);
    const setText = (id, value) => { const node = byId(id); if (node) node.textContent = value == null ? "" : String(value); };
    const setHidden = (id, hidden) => { const node = byId(id); if (node) node.hidden = Boolean(hidden); };
    const render = (event, payload) => {
      const state = payload.state;
      const status = byId("page-status");
      if (status) {
        if (["loading", "detail-loading", "trend-loading"].includes(event)) status.textContent = "正在加载数据…";
        else if (["error", "accounts-error", "detail-error", "trend-error"].includes(event)) status.textContent = payload.message;
        else if (["dashboard", "detail", "trend", "validation"].includes(event)) status.textContent = "";
        status.className = ["error", "accounts-error", "detail-error", "trend-error"].includes(event) ? "page-status error" : "page-status";
      }
      if (event === "query") { syncForm(state.query); showView(state.query.view); }
      if (event === "dashboard") {
        showView("table");
        setText("date-error", "");
        const summary = payload.summary || {};
        setText("summary-accounts", summary.account_count == null ? "—" : summary.account_count);
        for (const metric of ["posts", "likes", "views", "comments"]) {
          const formatted = formatMetric(summary[`${metric}_delta`]);
          const node = byId(`summary-${metric}`);
          if (node) { node.textContent = formatted.text; node.className = `summary-value metric-${formatted.kind}`; }
        }
        const body = byId("stats-table-body");
        if (body) {
          body.replaceChildren();
          (payload.table.rows || []).forEach((row) => {
            const next = {...state.query, view: "detail", accountId: row.account_id, focusDate: "", from: "table"};
            body.append(safe.accountRow(row, `${window.location.pathname || "/tiktok-stats"}${stateToSearch(next)}`, () => safely(() => controller.openDetail(row.account_id))));
          });
        }
        const empty = byId("table-empty");
        if (empty) empty.hidden = Boolean((payload.table.rows || []).length);
        setText("page-number", `第 ${payload.table.page || 1} / ${payload.table.total_pages || 1} 页`);
        const previous = byId("page-previous");
        const next = byId("page-next");
        if (previous) previous.disabled = (payload.table.page || 1) <= 1;
        if (next) next.disabled = (payload.table.page || 1) >= (payload.table.total_pages || 1);
      }
      if (event === "detail-reset" || event === "detail-loading") { showView("detail"); clearDetail(); }
      if (event === "trend-reset" || event === "trend-loading") { showView("trends"); clearTrend(); }
      if (event === "detail") { showView("detail"); renderDetail(payload.detail, state); }
      if (event === "trend") { showView("trends"); renderTrend(payload.trend, state); }
      if (event === "validation") setText("date-error", payload.message);
      if (event === "accounts") renderCandidates(payload.existingCandidates || []);
      if (event === "accounts") setText("import-error", "");
      if (event === "accounts-error" || event === "import-error") setText("import-error", payload.message);
      if (event === "import-result") renderImportResult(payload.result);
      if (event === "settings") renderSettings(payload.settings);
    };
    const controller = createTikTokStatsController({request: (url, init) => window.fetch(url, init), render, history: window.history, location: window.location});

    function showView(view) {
      setHidden("summary-section", view !== "table");
      setHidden("table-view", view !== "table");
      setHidden("detail-view", view !== "detail");
      setHidden("trend-view", view !== "trends");
      const tableButton = byId("show-table-view");
      const trendButton = byId("show-trends-view");
      if (tableButton) tableButton.setAttribute("aria-pressed", view === "table" ? "true" : "false");
      if (trendButton) trendButton.setAttribute("aria-pressed", view === "trends" ? "true" : "false");
    }

    function clearDetail() {
      setText("detail-title", "账号详情");
      setText("detail-identity", "");
      for (const id of ["detail-totals", "detail-chart", "detail-posts-body", "detail-runs", "detail-errors"]) byId(id)?.replaceChildren();
      setHidden("detail-chart-empty", true);
      setHidden("detail-posts-empty", true);
      setHidden("detail-runs-empty", true);
      setHidden("detail-errors-empty", true);
    }

    function clearTrend() {
      byId("trend-head")?.replaceChildren();
      byId("trend-body")?.replaceChildren();
      setHidden("trend-empty", true);
      setText("trend-page-number", "第 1 / 1 页");
    }

    function renderDetail(detail, state) {
      const account = detail.account || {};
      setText("detail-title", `@${account.username || "未命名账号"}`);
      setText("detail-identity", `${account.status === "disabled" ? "已停用" : "正在跟踪"}${state.query.focusDate ? ` · 已定位 ${state.query.focusDate}` : ""}`);
      const totals = byId("detail-totals");
      if (totals) {
        totals.replaceChildren();
        const labels = {posts: "作品总量", likes: "点赞总量", views: "浏览总量", comments: "评论总量"};
        Object.keys(labels).forEach((metric) => {
          const card = document.createElement("article");
          card.className = "summary-card";
          const label = document.createElement("span");
          label.textContent = labels[metric];
          const value = document.createElement("strong");
          const total = detail.current_totals && detail.current_totals[metric];
          value.textContent = total == null ? "暂无数据" : Number(total).toLocaleString("zh-CN");
          if (total == null) value.className = "metric-unavailable";
          card.append(label); card.append(value); totals.append(card);
        });
      }
      renderChart(detail.daily_series || [], byId("detail-chart-metric")?.value || "views", state.query.focusDate);
      const posts = sortPosts(detail.posts || [], state.postSort.sort, state.postSort.direction);
      const body = byId("detail-posts-body");
      if (body) { body.replaceChildren(); posts.forEach((post) => body.append(safe.postRow(post))); }
      setHidden("detail-posts-empty", posts.length > 0);
      for (const sort of POST_SORTS) {
        const head = byId(`post-head-${sort}`);
        if (head) head.setAttribute("aria-sort", state.postSort.sort === sort ? (state.postSort.direction === "asc" ? "ascending" : "descending") : "none");
      }
      renderHistory("detail-runs", "detail-runs-empty", detail.runs || [], (item) => `#${item.run_id} · ${item.status || "状态未知"} · ${item.started_at || "时间未知"}`);
      renderHistory("detail-errors", "detail-errors-empty", detail.errors || [], (item) => `#${item.run_id} · ${item.code || "采集异常"}：${item.message || "暂无说明"}`);
    }

    function renderHistory(listId, emptyId, items, label) {
      const list = byId(listId);
      if (list) {
        list.replaceChildren();
        items.forEach((item) => { const node = document.createElement("li"); node.textContent = label(item); list.append(node); });
      }
      setHidden(emptyId, items.length > 0);
    }

    function renderChart(series, metric, focusDate) {
      const host = byId("detail-chart");
      if (!host) return;
      host.replaceChildren();
      const model = buildChartModel(series, metric);
      host.setAttribute("aria-label", `${model.label}${focusDate ? `，关注日期 ${focusDate}` : ""}`);
      const available = model.points.filter((point) => point.value !== null);
      setHidden("detail-chart-empty", available.length > 0);
      if (!available.length) return;
      const svg = document.createElement("svg");
      svg.setAttribute("viewBox", "0 0 720 220");
      svg.setAttribute("role", "img");
      svg.setAttribute("aria-label", model.label);
      const title = document.createElement("title"); title.textContent = model.label; svg.append(title);
      const range = model.max === model.min ? 1 : model.max - model.min;
      const step = model.points.length > 1 ? 660 / (model.points.length - 1) : 0;
      let pathValue = "";
      model.points.forEach((point, index) => {
        if (point.value === null) return;
        const x = 30 + index * step;
        const y = 180 - ((point.value - model.min) / range) * 140;
        pathValue += `${pathValue && model.points[index - 1] && model.points[index - 1].value !== null ? " L" : " M"} ${x} ${y}`;
        const circle = document.createElement("circle");
        circle.setAttribute("cx", String(x)); circle.setAttribute("cy", String(y)); circle.setAttribute("r", point.date === focusDate ? "6" : "4");
        circle.setAttribute("aria-label", `${point.date}，${formatMatrixCell(point.value).label}`); svg.append(circle);
        const label = document.createElement("text"); label.setAttribute("x", String(x)); label.setAttribute("y", "207"); label.textContent = point.date.slice(5); svg.append(label);
      });
      const path = document.createElement("path"); path.setAttribute("d", pathValue.trim()); svg.append(path);
      host.append(svg);
    }

    function renderTrend(trend, state) {
      const accounts = trend.accounts || [];
      const head = byId("trend-head");
      if (head) {
        head.replaceChildren();
        const row = document.createElement("tr");
        const dateHead = document.createElement("th"); dateHead.setAttribute("scope", "col"); dateHead.textContent = "日期"; row.append(dateHead);
        accounts.forEach((account) => { const th = document.createElement("th"); th.setAttribute("scope", "col"); th.textContent = `@${account.username || "未命名账号"}`; row.append(th); });
        head.append(row);
      }
      const body = byId("trend-body");
      if (body) {
        body.replaceChildren();
        (trend.rows || []).forEach((matrixRow) => {
          const tr = document.createElement("tr");
          const dateCell = document.createElement("th"); dateCell.setAttribute("scope", "row"); dateCell.textContent = matrixRow.business_date; tr.append(dateCell);
          accounts.forEach((account) => tr.append(safe.trendCell((matrixRow.values || {})[String(account.account_id)], account, matrixRow.business_date, () => safely(() => controller.openDetail(account.account_id, matrixRow.business_date)))));
          body.append(tr);
        });
      }
      setHidden("trend-empty", accounts.length > 0);
      setText("trend-page-number", `第 ${trend.page || 1} / ${trend.total_pages || 1} 页`);
      const previous = byId("trend-previous"); const next = byId("trend-next");
      if (previous) previous.disabled = (trend.page || 1) <= 1;
      if (next) next.disabled = (trend.page || 1) >= (trend.total_pages || 1);
      const metric = byId("trend-metric"); if (metric) metric.value = state.query.trendMetric || "posts";
    }

    function syncForm(query) {
      for (const [id, value] of [["date-mode", query.mode], ["date-start", query.start], ["date-end", query.end], ["account-search", query.q], ["account-status", query.status], ["baseline-status", query.baseline], ["sort-metric", query.sort], ["sort-direction", query.direction], ["page-size", String(query.pageSize)]]) {
        const node = byId(id); if (node) node.value = value;
      }
      const range = byId("range-end-wrap"); if (range) range.hidden = query.mode !== "range";
      const trendMetric = byId("trend-metric"); if (trendMetric) trendMetric.value = query.trendMetric || "posts";
    }
    function renderCandidates(items) {
      const list = byId("existing-candidates");
      if (!list) return;
      list.replaceChildren();
      items.forEach((item) => {
        const label = document.createElement("label");
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.value = String(item.candidate_id);
        label.append(checkbox);
        const name = document.createElement("span");
        name.textContent = `@${item.username}`;
        label.append(name);
        list.append(label);
      });
    }
    function renderImportResult(result) {
      const list = byId("import-results");
      if (!list) return;
      list.replaceChildren();
      const labels = {added: "已新增", existing: "已存在", reactivated: "已重新启用", invalid: "无效"};
      (result.items || []).forEach((item) => {
        const li = document.createElement("li");
        li.textContent = `${item.value || "空值"}：${labels[item.status] || item.status}`;
        list.append(li);
      });
    }
    function renderSettings(settings) {
      const input = byId("cookie-input"); if (input) input.value = "";
      const status = settings.cookieStatus || {};
      setText("cookie-state", status.configured ? "已安全保存" : "未配置");
      const validationLabel = status.state === "valid" ? "验证通过" : status.state === "invalid" ? "验证未通过" : "";
      setText("cookie-validation", [status.checked_at, validationLabel].filter(Boolean).join(" · "));
      const service = settings.serviceStatus || {};
      setText("scraper-status", service.scraper && service.scraper.running ? "运行中" : "未运行");
      setText("worker-status", service.worker && service.worker.running ? "运行中" : "未运行");
      setText("settings-message", settings.operation || "");
    }
    function queryFromForm(resetPage) {
      return controller.updateQuery({mode: byId("date-mode").value, start: byId("date-start").value, end: byId("date-mode").value === "single" ? byId("date-start").value : byId("date-end").value, status: byId("account-status").value, baseline: byId("baseline-status").value, sort: byId("sort-metric").value, direction: byId("sort-direction").value, pageSize: Number(byId("page-size").value), page: resetPage ? 1 : controller.state.query.page});
    }
    const safely = async (operation) => { try { return await operation(); } catch (_error) { return false; } };
    ["date-mode", "date-start", "date-end", "account-status", "baseline-status", "sort-metric", "sort-direction", "page-size"].forEach((id) => byId(id)?.addEventListener("change", () => safely(() => queryFromForm(true))));
    byId("account-search")?.addEventListener("input", (event) => controller.search(event.currentTarget.value));
    byId("page-previous")?.addEventListener("click", () => safely(() => controller.updateQuery({page: Math.max(1, controller.state.query.page - 1)})));
    byId("page-next")?.addEventListener("click", () => safely(() => controller.updateQuery({page: controller.state.query.page + 1})));
    byId("show-table-view")?.addEventListener("click", () => safely(() => controller.showTable()));
    byId("show-trends-view")?.addEventListener("click", () => safely(() => controller.showTrends()));
    byId("detail-back")?.addEventListener("click", () => safely(() => controller.backToPrevious()));
    byId("trend-metric")?.addEventListener("change", (event) => safely(() => controller.updateTrend({trendMetric: event.currentTarget.value, trendPage: 1})));
    byId("trend-previous")?.addEventListener("click", () => safely(() => controller.updateTrend({trendPage: Math.max(1, (controller.state.query.trendPage || 1) - 1)})));
    byId("trend-next")?.addEventListener("click", () => safely(() => controller.updateTrend({trendPage: (controller.state.query.trendPage || 1) + 1})));
    byId("detail-chart-metric")?.addEventListener("change", (event) => { if (controller.state.detail) renderChart(controller.state.detail.daily_series || [], event.currentTarget.value, controller.state.query.focusDate); });
    for (const sort of POST_SORTS) {
      byId(`sort-post-${sort}`)?.addEventListener("click", () => {
        const current = controller.state.postSort;
        const direction = current.sort === sort && current.direction === "desc" ? "asc" : "desc";
        controller.setPostSort(sort, direction);
      });
    }
    byId("open-import")?.addEventListener("click", () => { byId("import-dialog")?.showModal(); return safely(() => controller.loadAccounts()); });
    byId("close-import")?.addEventListener("click", () => byId("import-dialog")?.close());
    byId("paste-import-form")?.addEventListener("submit", (event) => { event.preventDefault(); return safely(() => controller.importText(byId("import-text").value)); });
    byId("existing-import-form")?.addEventListener("submit", (event) => { event.preventDefault(); const ids = [...event.currentTarget.querySelectorAll('input[type="checkbox"]:checked')].map((node) => node.value); if (!ids.length) { setText("import-error", "请至少选择一个账号"); return Promise.resolve(false); } return safely(() => controller.importExisting(ids)); });
    byId("open-settings")?.addEventListener("click", () => { byId("settings-dialog")?.showModal(); return safely(() => controller.openSettings()); });
    byId("close-settings")?.addEventListener("click", () => byId("settings-dialog")?.close());
    byId("cookie-form")?.addEventListener("submit", (event) => { event.preventDefault(); const input = byId("cookie-input"); const value = input.value; input.value = ""; return safely(() => controller.saveCookie(value)); });
    byId("validate-cookie")?.addEventListener("click", () => safely(() => controller.validateCookie()));
    byId("run-incremental")?.addEventListener("click", () => safely(() => controller.runNow("incremental")));
    byId("run-full")?.addEventListener("click", () => safely(() => controller.runNow("full")));
    if (typeof window.addEventListener === "function") window.addEventListener("popstate", () => safely(() => controller.handlePop(window.location.search)));
    syncForm(controller.state.query);
    showView(controller.state.query.view);
    safely(() => controller.init());
    return controller;
  }

  if (typeof document !== "undefined" && typeof window !== "undefined") {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => boot(document, window));
    else boot(document, window);
  }

  return {createTikTokStatsController, restoreState, stateToSearch, validateDates, formatMetric, formatMatrixCell, sortPosts, buildChartModel, createSafeRenderer, boot};
});
