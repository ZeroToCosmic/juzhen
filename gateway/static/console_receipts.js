(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root?.document?.querySelector("#console-receipts")) api.boot(root);
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const LABELS = {
    browser: "浏览器执行", campaign: "评论 Campaign", publishing: "内容发布", succeeded: "成功", success: "成功",
    completed: "已完成", failed: "失败", cleanup_failed: "清理失败", running: "运行中", queued: "已排队", pending: "待处理",
    draft: "草稿", planned: "已规划", paused: "已暂停", cancelled: "已取消", published_unverified: "结果未验证",
    awaiting_campaign_approval: "待审批",
  };
  const SUCCESS = new Set(["success", "succeeded", "completed", "published_verified"]);
  const FAILED = new Set(["failed", "cleanup_failed", "published_unverified"]);
  const timeText = (value) => value ? String(value).replace("T", " ").replace("Z", "").slice(0, 16) : "—";

  function statusGroup(status) {
    if (SUCCESS.has(status)) return "success";
    if (FAILED.has(status)) return "failed";
    return "pending";
  }

  function normalizeBrowser(item) {
    const summary = item.summary || {};
    const status = Number(summary.failed || 0) > 0 ? "failed" : item.status || "pending";
    return {key: `browser:${item.id}`, id: String(item.id || ""), source: "browser", status, title: item.strategy_name || item.strategy_id || "浏览器执行任务", object: `${summary.total ?? (item.profiles || []).length} 个 Profile`, result: `${summary.succeeded || 0} 成功 / ${summary.failed || 0} 失败`, time: item.updated_at || item.created_at || item.started_at || "", raw: item};
  }

  function normalizeCampaign(item) {
    return {key: `campaign:${item.id}`, id: String(item.id || ""), source: "campaign", status: item.status || "draft", title: item.name || "评论 Campaign", object: `${item.assignment_count || 0} 个节点`, result: item.abnormal_assignment_count ? `${item.abnormal_assignment_count} 个异常` : item.awaiting_approval_count ? `${item.awaiting_approval_count} 个待审批` : "—", time: item.updated_at || item.created_at || "", raw: item};
  }

  function normalizePublish(item) {
    return {key: `publishing:${item.id}`, id: String(item.id || ""), source: "publishing", status: item.status || "pending", title: item.account_name || item.account_id || "发布任务", object: item.profile_id || "—", result: item.tiktok_url ? "已发布" : item.error || "—", time: item.updated_at || item.scheduled_at || item.created_at || "", raw: item};
  }

  function filterRecords(items, filters) {
    const query = String(filters.query || "").trim().toLocaleLowerCase("zh-CN");
    return items.filter((item) => {
      if (filters.source && item.source !== filters.source) return false;
      if (filters.status && statusGroup(item.status) !== filters.status) return false;
      return !query || [item.title, item.object, item.result, item.id].some((value) => String(value || "").toLocaleLowerCase("zh-CN").includes(query));
    });
  }

  function safeEvidencePath(value, source) {
    const match = /^evidence\/([0-9a-f]{32}\.png)$/.exec(String(value || "").replace(/\\/g, "/"));
    if (!match) return "";
    return source === "campaign" ? `/comment-campaign-evidence/${match[1]}` : `/evidence/${match[1]}`;
  }

  function boot(root) {
    const doc = root.document;
    const byId = (id) => doc.getElementById(id);
    const form = byId("receipts-filters");
    let records = [];
    let page = 1;
    let version = 0;
    let detailVersion = 0;

    async function read(url) {
      const response = await root.fetch(url, {headers: {Accept: "application/json"}});
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error?.message || payload.error || `HTTP ${response.status}`);
      return payload;
    }
    const set = (id, value) => { const node = byId(id); if (node) node.textContent = value; };

    function render() {
      const filtered = filterRecords(records, {query: form.elements.query.value, source: form.elements.source.value, status: form.elements.status.value});
      const pageSize = 20;
      const pages = Math.max(Math.ceil(filtered.length / pageSize), 1);
      page = Math.min(Math.max(page, 1), pages);
      const visible = filtered.slice((page - 1) * pageSize, page * pageSize);
      const body = byId("receipts-body"); body.replaceChildren();
      visible.forEach((item) => {
        const row = doc.createElement("tr");
        const record = doc.createElement("td"); const main = doc.createElement("span"); main.className = "cell-main"; main.textContent = item.title; const sub = doc.createElement("span"); sub.className = "cell-sub"; sub.textContent = item.id || "—"; record.append(main, sub); row.append(record);
        const source = doc.createElement("td"); source.textContent = LABELS[item.source] || item.source; row.append(source);
        const status = doc.createElement("td"); const badge = doc.createElement("span"); badge.className = `console-badge ${statusGroup(item.status)}`; badge.textContent = LABELS[item.status] || item.status || "—"; status.append(badge); row.append(status);
        [item.object, timeText(item.time), item.result].forEach((value, index) => { const cell = doc.createElement("td"); cell.textContent = value; if (index === 2) cell.className = "num"; row.append(cell); });
        const action = doc.createElement("td"); action.className = "action"; const button = doc.createElement("button"); button.className = "console-link"; button.type = "button"; button.dataset.receiptKey = item.key; button.textContent = "详情"; action.append(button); row.append(action); body.append(row);
      });
      byId("receipts-empty").hidden = visible.length > 0;
      set("receipts-page-meta", `第 ${page} / ${pages} 页 · 共 ${filtered.length} 条`);
      byId("receipts-prev").disabled = page <= 1; byId("receipts-next").disabled = page >= pages;
      set("receipts-total", String(records.length));
      set("receipts-success", String(records.filter((item) => statusGroup(item.status) === "success").length));
      set("receipts-pending", String(records.filter((item) => statusGroup(item.status) === "pending").length));
      set("receipts-failed", String(records.filter((item) => statusGroup(item.status) === "failed").length));
    }

    async function refresh() {
      const current = ++version; set("receipts-status", "正在读取本机回执…");
      const calls = await Promise.allSettled([read("/api/browser-v2/history?limit=200&offset=0"), read("/api/browser-v2/comment-campaigns?limit=200&offset=0"), read("/api/publish/results?page=1&page_size=100")]);
      if (current !== version) return;
      records = [
        ...(calls[0].status === "fulfilled" ? (calls[0].value.data || []).map(normalizeBrowser) : []),
        ...(calls[1].status === "fulfilled" ? (calls[1].value.data || []).map(normalizeCampaign) : []),
        ...(calls[2].status === "fulfilled" ? (calls[2].value.tasks || []).map(normalizePublish) : []),
      ].sort((a, b) => String(b.time).localeCompare(String(a.time)) || a.key.localeCompare(b.key));
      render(); const failed = calls.filter((item) => item.status === "rejected").length;
      set("receipts-status", failed ? `部分来源不可用（${failed} 个）` : "回执已更新。"); byId("receipts-status").classList.toggle("error", failed === 3);
    }

    function addSummary(label, value) {
      const box = doc.createElement("div"); const caption = doc.createElement("span"); caption.textContent = label; const strong = doc.createElement("strong"); strong.textContent = value ?? "—"; box.append(caption, strong); byId("receipts-detail-summary").append(box);
    }

    function addDetail(label, value, tone) {
      const row = doc.createElement("div"); row.className = "console-detail-row"; if (tone) row.dataset.tone = tone;
      const caption = doc.createElement("span"); caption.textContent = label; const content = doc.createElement("div"); content.textContent = value ?? "—"; row.append(caption, content); byId("receipts-detail-body").append(row);
    }

    function renderEvidence(paths, source) {
      const container = byId("receipts-evidence-list"); container.replaceChildren();
      [...new Set(paths.map((value) => safeEvidencePath(value, source)).filter(Boolean))].forEach((href, index) => {
        const link = doc.createElement("a"); link.className = "console-evidence-card"; link.href = href; link.target = "_blank"; link.rel = "noopener"; link.textContent = `查看证据 ${index + 1}`; container.append(link);
      });
      byId("receipts-evidence-section").hidden = container.children.length === 0;
    }

    async function showDetail(item) {
      const current = ++detailVersion;
      byId("receipts-list-view").hidden = true; byId("receipts-detail-view").hidden = false;
      set("receipts-detail-title", item.title); set("receipts-detail-subtitle", `${LABELS[item.source]} · ${item.id}`);
      byId("receipts-detail-summary").replaceChildren(); byId("receipts-detail-body").replaceChildren(); renderEvidence([], item.source);
      addSummary("状态", LABELS[item.status] || item.status); addSummary("对象", item.object); addSummary("时间", timeText(item.time)); addSummary("结果", item.result);
      if (item.source === "browser") {
        const raw = item.raw; (raw.profiles || []).forEach((profile) => addDetail(profile.display_id || "Profile", `${LABELS[profile.status] || profile.status || "—"} · ${profile.stage || "—"}`, statusGroup(profile.status)));
        (raw.actions || []).forEach((action) => addDetail(`动作 ${Number(action.action_index ?? action.index ?? 0) + 1}`, `${action.action_type || "—"} · ${LABELS[action.status] || action.status || "—"}${action.error_code ? ` · ${action.error_code}` : ""}`, statusGroup(action.status)));
        renderEvidence((raw.actions || []).map((action) => action.evidence_path), "browser");
      } else if (item.source === "publishing") {
        const raw = item.raw; addDetail("账号", raw.account_name || raw.account_id); addDetail("Profile", raw.profile_id); addDetail("计划时间", timeText(raw.scheduled_at)); addDetail("文案", raw.copy_text || "—");
        addDetail(raw.tiktok_url ? "发布链接" : "失败原因", raw.tiktok_url || raw.error || "—", statusGroup(raw.status));
      } else {
        set("receipts-status", "正在读取 Campaign 回执…");
        const base = `/api/browser-v2/comment-campaigns/${encodeURIComponent(item.id)}`;
        const calls = await Promise.allSettled([read(base), read(`${base}/receipts`), read(`${base}/attempts`)]);
        if (current !== detailVersion) return;
        const detail = calls[0].status === "fulfilled" ? calls[0].value.data || {} : {};
        const receipts = calls[1].status === "fulfilled" ? calls[1].value.data || [] : [];
        const attempts = calls[2].status === "fulfilled" ? calls[2].value.data || [] : [];
        (detail.assignments || []).forEach((assignment) => addDetail(assignment.display_profile || assignment.assignment_id || "节点", `${LABELS[assignment.status] || assignment.status || "—"}${assignment.error_summary ? ` · ${assignment.error_summary}` : ""}`, statusGroup(assignment.status)));
        receipts.forEach((receipt) => addDetail(`回执 ${receipt.assignment_id || receipt.receipt_id || ""}`, LABELS[receipt.status] || receipt.status || "—", statusGroup(receipt.status)));
        attempts.forEach((attempt) => addDetail(`尝试 ${attempt.attempt_no || ""} · ${attempt.stage || "—"}`, `${LABELS[attempt.status] || attempt.status || "—"}${attempt.error_summary ? ` · ${attempt.error_summary}` : ""}`, statusGroup(attempt.status)));
        renderEvidence(attempts.flatMap((attempt) => attempt.evidence_paths || []), "campaign");
        set("receipts-status", calls.some((call) => call.status === "rejected") ? "部分 Campaign 明细不可用。" : "Campaign 回执已读取。");
      }
    }

    form.addEventListener("input", () => { page = 1; render(); }); form.addEventListener("change", () => { page = 1; render(); });
    byId("receipts-reset")?.addEventListener("click", () => { form.reset(); page = 1; render(); }); byId("receipts-refresh")?.addEventListener("click", refresh);
    byId("receipts-prev")?.addEventListener("click", () => { page -= 1; render(); }); byId("receipts-next")?.addEventListener("click", () => { page += 1; render(); });
    byId("receipts-body")?.addEventListener("click", (event) => { const key = event.target.closest("[data-receipt-key]")?.dataset.receiptKey; const item = records.find((record) => record.key === key); if (item) showDetail(item); });
    byId("receipts-detail-back")?.addEventListener("click", () => { detailVersion += 1; byId("receipts-detail-view").hidden = true; byId("receipts-list-view").hidden = false; });
    refresh();
    return {refresh, showDetail};
  }

  return {boot, filterRecords, normalizeBrowser, normalizeCampaign, normalizePublish, safeEvidencePath, statusGroup, timeText};
});
