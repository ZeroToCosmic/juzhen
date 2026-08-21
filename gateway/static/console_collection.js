(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root?.document?.querySelector("#console-collection")) api.boot(root);
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const timeText = (value) => value ? String(value).replace("T", " ").replace("Z", "").slice(0, 16) : "—";
  const runningText = (value) => value === true ? "运行中" : "未运行";
  const statusText = (value) => ({running: "运行中", completed: "已完成", partial: "部分完成", failed: "失败", enabled: "启用", disabled: "停用"})[value] || value || "—";
  const sourceText = (value) => ({manual: "本机手动", central: "中控下发", existing_accounts: "账号库"})[value] || value || "—";

  function boot(root) {
    const doc = root.document;
    const byId = (id) => doc.getElementById(id);
    const set = (id, value) => { const node = byId(id); if (node) node.textContent = value; };
    let requestVersion = 0;
    let submitting = false;

    async function read(url, options) {
      const response = await root.fetch(url, options);
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error?.message || payload.error || `HTTP ${response.status}`);
      return payload;
    }

    function renderAccounts(accounts) {
      const body = byId("collection-accounts-body");
      if (!body) return;
      body.replaceChildren();
      accounts.slice(0, 8).forEach((account) => {
        const row = doc.createElement("tr");
        [account.username || "—", sourceText(account.source), statusText(account.status), timeText(account.updated_at)].forEach((value) => {
          const cell = doc.createElement("td"); cell.textContent = value; row.append(cell);
        });
        body.append(row);
      });
      byId("collection-accounts-empty").hidden = accounts.length > 0;
    }

    function renderRuns(runs) {
      const body = byId("collection-runs-body");
      if (!body) return;
      body.replaceChildren();
      runs.forEach((run) => {
        const row = doc.createElement("tr");
        [timeText(run.started_at), run.run_type === "full" ? "完整校准" : "增量采集", statusText(run.status), timeText(run.finished_at)].forEach((value) => {
          const cell = doc.createElement("td"); cell.textContent = value; row.append(cell);
        });
        body.append(row);
      });
      byId("collection-runs-empty").hidden = runs.length > 0;
    }

    async function refresh() {
      const version = ++requestVersion;
      set("collection-status", "正在读取采集状态…");
      try {
        const [status, accountPayload, runPayload] = await Promise.all([
          read("/api/tiktok-stats/status"),
          read("/api/tiktok-stats/accounts"),
          read("/api/tiktok-stats/runs?page=1&page_size=20"),
        ]);
        if (version !== requestVersion) return;
        const accounts = accountPayload.accounts || [];
        const enabled = accounts.filter((account) => account.status === "enabled").length;
        const runs = runPayload.runs || [];
        set("collection-account-count", String(accounts.length));
        set("collection-account-note", `${enabled} 个启用`);
        set("collection-scraper", runningText(status.scraper?.running));
        set("collection-worker", runningText(status.worker?.running));
        set("collection-last-run", statusText(runs[0]?.status));
        set("collection-last-run-note", runs[0] ? timeText(runs[0].started_at) : "暂无记录");
        renderAccounts(accounts);
        renderRuns(runs);
        set("collection-status", "采集状态已更新。");
        byId("collection-status")?.classList.remove("error");
      } catch (error) {
        if (version !== requestVersion) return;
        set("collection-status", error.message || "读取失败");
        byId("collection-status")?.classList.add("error");
      }
    }

    async function dispatch(runType, button) {
      if (submitting) return;
      submitting = true;
      const runButtons = [byId("collection-run-incremental"), byId("collection-run-full")].filter(Boolean);
      runButtons.forEach((item) => { item.disabled = true; });
      set("collection-status", "正在提交本机调试任务…");
      try {
        await read("/api/tiktok-stats/runs", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({run_type: runType}),
        });
        set("collection-status", "调试任务已进入本机队列。");
        await refresh();
      } catch (error) {
        set("collection-status", error.message || "提交失败");
        byId("collection-status")?.classList.add("error");
      } finally {
        submitting = false;
        runButtons.forEach((item) => { item.disabled = false; });
      }
    }

    byId("collection-refresh")?.addEventListener("click", refresh);
    byId("collection-run-incremental")?.addEventListener("click", (event) => dispatch("incremental", event.currentTarget));
    byId("collection-run-full")?.addEventListener("click", (event) => dispatch("full", event.currentTarget));
    refresh();
    return {refresh};
  }

  return {boot, runningText, sourceText, statusText, timeText};
});
