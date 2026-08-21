(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root?.document?.querySelector("#console-actions")) api.boot(root);
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const LABELS = {
    strategy: "浏览器策略", campaign: "评论 Campaign", enabled: "可用", disabled: "已停用",
    draft: "草稿", planned: "已规划", awaiting_campaign_approval: "待审批", queued: "已排队",
    running: "运行中", paused: "已暂停", failed: "异常", completed: "已完成", cancelled: "已取消",
  };
  const ATTENTION = new Set(["awaiting_campaign_approval", "paused", "failed"]);
  const INACTIVE = new Set(["disabled", "completed", "cancelled"]);

  function timeText(value) {
    return value ? String(value).replace("T", " ").replace("Z", "").slice(0, 16) : "—";
  }

  function strategyEditorUrl(strategyId) {
    const id = String(strategyId || "");
    return id ? `/console/actions/browser-strategies/${encodeURIComponent(id)}/edit` : "";
  }

  function normalizeStrategy(item) {
    return {
      id: String(item.id || ""), name: item.name || "未命名策略", type: "strategy",
      status: item.enabled === false ? "disabled" : "enabled", size: Array.isArray(item.actions) ? item.actions.length : 0,
      updated_at: item.updated_at || item.created_at || "", source: "本机 · 浏览器策略库", href: strategyEditorUrl(item.id),
    };
  }

  function normalizeCampaign(item) {
    return {
      id: String(item.id || ""), name: item.name || "未命名 Campaign", type: "campaign", status: item.status || "draft",
      size: Number(item.assignment_count || 0), updated_at: item.updated_at || item.created_at || "",
      href: "/comment-campaigns", source: "本机 · 评论 Campaign 库",
    };
  }

  function statusGroup(status) {
    if (ATTENTION.has(status)) return "attention";
    if (INACTIVE.has(status)) return "inactive";
    return "active";
  }

  function filterActions(items, filters) {
    const query = String(filters.query || "").trim().toLocaleLowerCase("zh-CN");
    return items.filter((item) => {
      if (filters.type && item.type !== filters.type) return false;
      if (filters.status && statusGroup(item.status) !== filters.status) return false;
      return !query || `${item.name} ${item.id}`.toLocaleLowerCase("zh-CN").includes(query);
    });
  }

  function boot(root) {
    const doc = root.document;
    const byId = (id) => doc.getElementById(id);
    const form = byId("actions-filters");
    let items = [];
    let version = 0;

    async function read(url) {
      const response = await root.fetch(url, {headers: {Accept: "application/json"}});
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error?.message || payload.error || `HTTP ${response.status}`);
      return payload;
    }

    function set(id, value) { const node = byId(id); if (node) node.textContent = value; }

    function render() {
      const filters = {query: form.elements.query.value, type: form.elements.type.value, status: form.elements.status.value};
      const visible = filterActions(items, filters);
      const body = byId("actions-body");
      body.replaceChildren();
      visible.forEach((item) => {
        const row = doc.createElement("tr");
        const name = doc.createElement("td");
        const main = doc.createElement("span"); main.className = "cell-main"; main.textContent = item.name;
        const sub = doc.createElement("span"); sub.className = "cell-sub"; sub.textContent = item.id || "—";
        name.append(main, sub); row.append(name);
        const values = [LABELS[item.type] || item.type, LABELS[item.status] || item.status, String(item.size), timeText(item.updated_at), item.source];
        values.forEach((value, index) => { const cell = doc.createElement("td"); cell.textContent = value; if (index === 2) cell.className = "num"; row.append(cell); });
        row.children[2].className = `console-badge-cell ${statusGroup(item.status)}`;
        const action = doc.createElement("td"); action.className = "action";
        if (item.href) {
          const link = doc.createElement("a"); link.className = "console-link"; link.href = item.href; link.textContent = "维护"; action.append(link);
        } else {
          const unavailable = doc.createElement("span"); unavailable.className = "console-link muted"; unavailable.textContent = "不可维护"; action.append(unavailable);
        }
        row.append(action);
        body.append(row);
      });
      byId("actions-empty").hidden = visible.length > 0;
    }

    async function refresh() {
      const current = ++version;
      set("actions-status", "正在读取动作库…");
      const [strategies, campaigns] = await Promise.allSettled([
        read("/api/browser-v2/strategies"), read("/api/browser-v2/comment-campaigns?limit=200&offset=0"),
      ]);
      if (current !== version) return;
      const strategyItems = strategies.status === "fulfilled" ? (strategies.value.data || []).map(normalizeStrategy) : [];
      const campaignItems = campaigns.status === "fulfilled" ? (campaigns.value.data || []).map(normalizeCampaign) : [];
      items = [...strategyItems, ...campaignItems].sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)) || a.id.localeCompare(b.id));
      set("actions-total", String(items.length));
      set("actions-strategies", String(strategyItems.length));
      set("actions-campaigns", String(campaignItems.length));
      set("actions-attention", String(items.filter((item) => statusGroup(item.status) === "attention").length));
      set("actions-strategies-note", strategies.status === "fulfilled" ? "策略库已连接" : "策略库不可用");
      set("actions-campaigns-note", campaigns.status === "fulfilled" ? "Campaign 库已连接" : "Campaign 库不可用");
      const failures = [strategies, campaigns].filter((item) => item.status === "rejected").length;
      set("actions-status", failures ? `部分数据不可用（${failures} 个来源）；中控同步接口未配置。` : "动作库已更新；中控同步接口未配置。" );
      byId("actions-status").classList.toggle("error", failures === 2);
      render();
    }

    form.addEventListener("input", render);
    form.addEventListener("change", render);
    byId("actions-reset")?.addEventListener("click", () => { form.reset(); render(); });
    byId("actions-refresh")?.addEventListener("click", refresh);
    refresh();
    return {refresh, render};
  }

  return {boot, filterActions, normalizeCampaign, normalizeStrategy, strategyEditorUrl, statusGroup, timeText};
});
