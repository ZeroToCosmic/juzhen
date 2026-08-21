(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root?.document?.querySelector("#console-overview")) api.boot(root);
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function profileItems(result) {
    if (result.status !== "fulfilled") return [];
    const payload = result.value?.data || result.value || {};
    return Array.isArray(payload) ? payload : (payload.profiles || []);
  }

  function summarizeLocalRuntime(results, updatedAt) {
    const [gateway, profiles, collection] = results;
    const collectionValue = collection.status === "fulfilled" ? collection.value || {} : {};
    const collectionNote = collection.status === "fulfilled" ? "TikTok 采集服务" : "无法读取采集服务";
    return {
      gateway: {
        value: gateway.status === "fulfilled" ? "可达" : "不可用",
        note: gateway.status === "fulfilled" ? "本机接口响应正常" : "无法读取接口状态",
      },
      profiles: {
        value: profiles.status === "fulfilled" ? `${profileItems(profiles).length} 个` : "不可用",
        note: profiles.status === "fulfilled" ? "AdsPower Profile" : "无法读取 Profile",
      },
      scraper: {
        value: collection.status === "fulfilled" && collectionValue.scraper?.running ? "运行中" : collection.status === "fulfilled" ? "未运行" : "不可用",
        note: collectionNote,
      },
      worker: {
        value: collection.status === "fulfilled" && collectionValue.worker?.running ? "运行中" : collection.status === "fulfilled" ? "未运行" : "不可用",
        note: collection.status === "fulfilled" ? "本机采集调度" : "无法读取采集调度",
      },
      failed: results.filter((item) => item.status === "rejected").length,
      updatedAt,
    };
  }

  function boot(root) {
    const doc = root.document;
    const byId = (id) => doc.getElementById(id);
    const set = (id, value) => { const node = byId(id); if (node) node.textContent = value; };

    async function read(url) {
      const response = await root.fetch(url, {headers: {Accept: "application/json"}});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    }

    function renderRuntime(summary) {
      set("overview-runtime-gateway", summary.gateway.value);
      set("overview-runtime-gateway-note", summary.gateway.note);
      set("overview-runtime-profiles", summary.profiles.value);
      set("overview-runtime-profiles-note", summary.profiles.note);
      set("overview-runtime-scraper", summary.scraper.value);
      set("overview-runtime-scraper-note", summary.scraper.note);
      set("overview-runtime-worker", summary.worker.value);
      set("overview-runtime-worker-note", summary.worker.note);
      set("overview-runtime-updated", summary.updatedAt);
    }

    async function refresh() {
      const button = byId("overview-refresh");
      const status = byId("overview-status");
      if (button) button.disabled = true;
      if (status) { status.textContent = "正在刷新…"; status.classList.remove("error"); }
      const results = await Promise.allSettled([
        read("/api/status"),
        read("/api/browser-v2/profiles"),
        read("/api/tiktok-stats/status"),
      ]);
      const updatedAt = new Date().toLocaleString("zh-CN", {hour12: false}).slice(0, 16);
      const summary = summarizeLocalRuntime(results, updatedAt);
      renderRuntime(summary);
      if (status) {
        status.textContent = summary.failed ? `${summary.failed} 项状态暂时无法读取，其余状态不受影响。` : "状态已更新。";
        status.classList.toggle("error", summary.failed > 0);
      }
      if (button) button.disabled = false;
      return summary;
    }

    byId("overview-refresh")?.addEventListener("click", refresh);
    refresh();
    return {refresh};
  }

  return {boot, summarizeLocalRuntime};
});
