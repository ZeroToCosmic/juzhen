(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root?.document?.querySelector("#console-collection-results")) api.boot(root);
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function formatNumber(value) {
    return value === null || value === undefined ? "—" : new Intl.NumberFormat("zh-CN").format(value);
  }

  function timeText(value) {
    return value ? String(value).replace("T", " ").replace("Z", "").slice(0, 16) : "—";
  }

  function buildVideoQuery(state) {
    const values = new URLSearchParams();
    for (const key of ["query", "account_id", "published_from", "published_to"]) {
      if (state[key]) values.set(key, state[key]);
    }
    values.set("sort", state.sort || "last_collected_at");
    values.set("direction", state.direction || "desc");
    values.set("page", String(state.page || 1));
    values.set("page_size", String(state.page_size || 50));
    return values.toString();
  }

  function boot(root) {
    const doc = root.document;
    const byId = (id) => doc.getElementById(id);
    const form = byId("results-filters");
    const state = {query: "", account_id: "", published_from: "", published_to: "", sort: "last_collected_at", direction: "desc", page: 1, page_size: 50};
    let requestVersion = 0;

    async function read(url) {
      const response = await root.fetch(url, {headers: {Accept: "application/json"}});
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error?.message || payload.error || `HTTP ${response.status}`);
      return payload;
    }

    function set(id, value) { const node = byId(id); if (node) node.textContent = value; }

    function videoUrl(row) {
      return row.username && row.video_id
        ? `https://www.tiktok.com/@${encodeURIComponent(row.username)}/video/${encodeURIComponent(row.video_id)}`
        : "";
    }

    function renderRows(rows) {
      const body = byId("results-body");
      body.replaceChildren();
      rows.forEach((item) => {
        const row = doc.createElement("tr");
        const video = doc.createElement("td");
        const title = doc.createElement("span"); title.className = "cell-main"; title.textContent = item.description || "未命名视频";
        const sub = doc.createElement("span"); sub.className = "cell-sub"; sub.textContent = item.video_id || "—";
        video.append(title, sub); row.append(video);
        const account = doc.createElement("td"); account.textContent = item.username ? `@${item.username}` : "—"; row.append(account);
        const published = doc.createElement("td"); published.textContent = timeText(item.published_at); row.append(published);
        for (const key of ["views", "likes", "comments"]) { const cell = doc.createElement("td"); cell.className = "num"; cell.textContent = formatNumber(item[key]); row.append(cell); }
        const collected = doc.createElement("td"); collected.textContent = timeText(item.last_collected_at); row.append(collected);
        const action = doc.createElement("td"); action.className = "action";
        const url = videoUrl(item);
        if (url) { const link = doc.createElement("a"); link.className = "console-link"; link.href = url; link.target = "_blank"; link.rel = "noopener"; link.textContent = "查看"; action.append(link); }
        row.append(action); body.append(row);
      });
      byId("results-empty").hidden = rows.length > 0;
    }

    function render(payload) {
      const summary = payload.summary || {};
      set("results-video-count", formatNumber(summary.video_count));
      set("results-total-views", formatNumber(summary.total_views));
      set("results-total-likes", formatNumber(summary.total_likes));
      set("results-total-comments", formatNumber(summary.total_comments));
      renderRows(payload.rows || []);
      set("results-page-meta", `第 ${payload.page || 1} / ${payload.total_pages || 1} 页 · 共 ${formatNumber(payload.total || 0)} 条`);
      byId("results-prev").disabled = (payload.page || 1) <= 1;
      byId("results-next").disabled = (payload.page || 1) >= (payload.total_pages || 1);
    }

    function updateSortHeaders() {
      const labels = {published_at: "发布时间", views: "播放", likes: "点赞", comments: "评论", last_collected_at: "最近采集"};
      doc.querySelectorAll("[data-sort]").forEach((button) => {
        const active = button.dataset.sort === state.sort;
        button.textContent = `${labels[button.dataset.sort]}${active ? (state.direction === "desc" ? " ↓" : " ↑") : ""}`;
        const header = button.closest("th");
        if (header) {
          if (active) header.setAttribute("aria-sort", state.direction === "desc" ? "descending" : "ascending");
          else header.removeAttribute("aria-sort");
        }
      });
    }

    async function load() {
      const version = ++requestVersion;
      updateSortHeaders();
      set("results-status", "正在读取数据…");
      try {
        const payload = await read(`/api/tiktok-stats/videos?${buildVideoQuery(state)}`);
        if (version !== requestVersion) return;
        render(payload);
        set("results-status", "数据已更新。");
        byId("results-status")?.classList.remove("error");
      } catch (error) {
        if (version !== requestVersion) return;
        set("results-status", error.message || "读取失败");
        byId("results-status")?.classList.add("error");
      }
    }

    async function loadAccounts() {
      try {
        const payload = await read("/api/tiktok-stats/accounts");
        const select = form.elements.account_id;
        (payload.accounts || []).forEach((account) => {
          const option = doc.createElement("option"); option.value = String(account.id); option.textContent = `账号：@${account.username}`; select.append(option);
        });
      } catch (_) { /* the video table remains usable without the account filter */ }
    }

    function syncFilters() {
      for (const key of ["query", "account_id", "published_from", "published_to"]) state[key] = form.elements[key].value.trim();
      state.page = 1;
      load();
    }

    form.addEventListener("submit", (event) => { event.preventDefault(); syncFilters(); });
    form.addEventListener("change", syncFilters);
    let searchTimer;
    form.elements.query.addEventListener("input", () => { root.clearTimeout(searchTimer); searchTimer = root.setTimeout(syncFilters, 250); });
    byId("results-reset")?.addEventListener("click", () => { form.reset(); Object.assign(state, {query: "", account_id: "", published_from: "", published_to: "", page: 1}); load(); });
    byId("results-refresh")?.addEventListener("click", load);
    byId("results-prev")?.addEventListener("click", () => { if (state.page > 1) { state.page -= 1; load(); } });
    byId("results-next")?.addEventListener("click", () => { state.page += 1; load(); });
    doc.querySelectorAll("[data-sort]").forEach((button) => button.addEventListener("click", () => {
      const sort = button.dataset.sort;
      state.direction = state.sort === sort && state.direction === "desc" ? "asc" : "desc";
      state.sort = sort; state.page = 1; load();
    }));

    loadAccounts();
    load();
    return {load, state};
  }

  return {boot, buildVideoQuery, formatNumber, timeText};
});
