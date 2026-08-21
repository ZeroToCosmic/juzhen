(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root?.document?.querySelector("#console-publishing")) api.boot(root);
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const STATUS = {pending: "待发布", success: "成功", failed: "失败", created: "已创建", enabled: "启用", disabled: "停用"};
  const timeText = (value) => value ? String(value).replace("T", " ").replace("Z", "").slice(0, 16) : "—";
  const localDate = (date = new Date()) => {
    const offset = date.getTimezoneOffset() * 60000;
    return new Date(date.getTime() - offset).toISOString().slice(0, 10);
  };
  const accountId = (account) => String(account.ads_power_user_id || account.id || "");
  function safeTikTokUrl(value) {
    try {
      const url = new URL(String(value || ""));
      const host = url.hostname.toLowerCase();
      return url.protocol === "https:" && (host === "tiktok.com" || host.endsWith(".tiktok.com")) ? url.href : "";
    } catch (_) { return ""; }
  }

  function buildBatchPayload(form, selectedIds) {
    const value = (field) => String(field?.value ?? field ?? "");
    const date = value(form.date);
    const time = value(form.time);
    return {brand_id: value(form.brand_id), scheduled_at: date && time ? `${date}T${time}:00+08:00` : "", account_ids: [...selectedIds]};
  }

  function filterResults(items, filters) {
    const query = String(filters.query || "").trim().toLocaleLowerCase("zh-CN");
    return items.filter((item) => {
      if (filters.status && item.status !== filters.status) return false;
      if (!query) return true;
      return [item.account_name, item.account_id, item.profile_id, item.copy_text, item.error, item.tiktok_url]
        .some((value) => String(value || "").toLocaleLowerCase("zh-CN").includes(query));
    });
  }

  function boot(root) {
    const doc = root.document;
    const byId = (id) => doc.getElementById(id);
    const state = {accounts: [], videos: [], brands: [], results: [], batches: [], schedules: [], resultPage: 1, resultPages: 1, resultTotal: 0, resultSummary: {}};
    let requestVersion = 0;
    let copyVersion = 0;
    const submitting = new WeakSet();

    async function request(url, options) {
      const response = await root.fetch(url, options);
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error?.message || payload.error || `HTTP ${response.status}`);
      return payload;
    }
    const json = (url, method, body) => request(url, {method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    const set = (id, value) => { const node = byId(id); if (node) node.textContent = value; };
    const statusClass = (status) => status === "success" || status === "enabled" ? "success" : status === "failed" ? "failed" : "pending";

    function setOptions(select, items, value, label) {
      if (!select) return;
      const selected = select.value;
      select.replaceChildren(...items.map((item) => {
        const option = doc.createElement("option"); option.value = String(value(item)); option.textContent = String(label(item)); return option;
      }));
      if ([...select.options].some((option) => option.value === selected)) select.value = selected;
    }

    function renderAccountChecks(targetId, name) {
      const target = byId(targetId);
      target.replaceChildren();
      state.accounts.filter((item) => (item.buffer_profile_ids || []).length).forEach((account) => {
        const label = doc.createElement("label"); label.className = "console-check-row";
        const input = doc.createElement("input"); input.type = "checkbox"; input.name = name; input.value = accountId(account);
        const text = doc.createElement("span"); text.textContent = `${account.account_name || accountId(account)} · ${(account.buffer_profile_ids || []).length} Profile`;
        label.append(input, text); target.append(label);
      });
    }

    function renderSelectors() {
      [byId("publishing-batch-form"), byId("publishing-daily-form")].forEach((form) => setOptions(form?.elements.brand_id, state.brands, (item) => item.id, (item) => item.name));
      const manual = byId("publishing-manual-form");
      setOptions(manual?.elements.account_id, state.accounts.filter((item) => (item.buffer_profile_ids || []).length), accountId, (item) => item.account_name || accountId(item));
      setOptions(manual?.elements.video_id, state.videos.filter((item) => !item.used), (item) => item.id, (item) => item.key || item.id);
      setOptions(manual?.elements.brand_id, state.brands, (item) => item.id, (item) => item.name);
      syncManualProfiles();
      renderAccountChecks("publishing-batch-accounts", "account_ids");
      renderAccountChecks("publishing-daily-accounts", "account_ids");
    }

    function syncManualProfiles() {
      const form = byId("publishing-manual-form");
      const account = state.accounts.find((item) => accountId(item) === form.elements.account_id.value) || {};
      setOptions(form.elements.profile_id, account.buffer_profile_ids || [], (item) => item, (item) => item);
    }

    async function syncManualCopy() {
      const form = byId("publishing-manual-form");
      const brandId = form.elements.brand_id.value;
      const current = ++copyVersion;
      if (!brandId) { setOptions(form.elements.copy_id, [], (item) => item.id, (item) => item.body); return; }
      try {
        const payload = await request(`/api/content/brands/${encodeURIComponent(brandId)}/copy`);
        if (current !== copyVersion || form.elements.brand_id.value !== brandId) return;
        setOptions(form.elements.copy_id, payload.items || [], (item) => item.id, (item) => String(item.body || item.id).slice(0, 60));
      } catch (error) { set("publishing-status", error.message); }
    }

    function renderResults() {
      const rows = state.results;
      const body = byId("publishing-results-body"); body.replaceChildren();
      rows.forEach((item) => {
        const row = doc.createElement("tr");
        const values = [timeText(item.scheduled_at || item.created_at), item.account_name || item.account_id || "—", item.profile_id || "—"];
        values.forEach((value) => { const cell = doc.createElement("td"); cell.textContent = value; row.append(cell); });
        const content = doc.createElement("td");
        const main = doc.createElement("span"); main.className = "cell-main clamp-one"; main.textContent = item.copy_text || "—";
        const sub = doc.createElement("span"); sub.className = "cell-sub"; sub.textContent = item.video_id || ""; content.append(main, sub); row.append(content);
        const status = doc.createElement("td"); const badge = doc.createElement("span"); badge.className = `console-badge ${statusClass(item.status)}`; badge.textContent = STATUS[item.status] || item.status || "—"; status.append(badge); row.append(status);
        const result = doc.createElement("td");
        const safeUrl = safeTikTokUrl(item.tiktok_url);
        if (safeUrl) { const link = doc.createElement("a"); link.className = "console-link"; link.href = safeUrl; link.target = "_blank"; link.rel = "noopener"; link.textContent = "查看视频"; result.append(link); }
        else result.textContent = item.error || "—";
        row.append(result); body.append(row);
      });
      byId("publishing-results-empty").hidden = rows.length > 0;
      set("publishing-results-page-meta", `第 ${state.resultPage} / ${state.resultPages} 页 · 共 ${state.resultTotal} 条`);
      byId("publishing-results-prev").disabled = state.resultPage <= 1;
      byId("publishing-results-next").disabled = state.resultPage >= state.resultPages;
      set("publishing-total", String(state.resultSummary.task_count ?? state.resultTotal));
      set("publishing-success", String(state.resultSummary.success ?? 0));
      set("publishing-failed", String(state.resultSummary.failed ?? 0));
    }

    function brandName(id) { return state.brands.find((item) => String(item.id) === String(id))?.name || id || "—"; }

    function renderBatches() {
      const body = byId("publishing-batches-body"); body.replaceChildren();
      state.batches.slice(0, 20).forEach((item) => {
        const row = doc.createElement("tr");
        [timeText(item.created_at), timeText(item.scheduled_at), item.requested ?? 0, item.created ?? 0, item.skipped ?? 0, brandName(item.brand_id), STATUS[item.status] || item.status || "—"].forEach((value, index) => {
          const cell = doc.createElement("td"); cell.textContent = String(value); if ([2, 3, 4].includes(index)) cell.className = "num"; row.append(cell);
        });
        const action = doc.createElement("td"); action.className = "action"; const button = doc.createElement("button"); button.className = "console-link"; button.type = "button"; button.dataset.deleteBatch = item.id; button.textContent = "删除"; action.append(button); row.append(action); body.append(row);
      });
      byId("publishing-batches-empty").hidden = state.batches.length > 0;
    }

    function renderSchedules() {
      const body = byId("publishing-daily-body"); body.replaceChildren();
      state.schedules.forEach((item) => {
        const row = doc.createElement("tr");
        [`${item.start_date || "—"} ${item.time || ""}`, item.account_count ?? (item.account_ids || []).length, brandName(item.brand_id), item.enabled === false ? "配置停用" : "配置已保存"].forEach((value) => { const cell = doc.createElement("td"); cell.textContent = String(value); row.append(cell); });
        const action = doc.createElement("td"); action.className = "action"; const button = doc.createElement("button"); button.className = "console-link"; button.type = "button"; button.dataset.deleteSchedule = item.id; button.textContent = "删除"; action.append(button); row.append(action); body.append(row);
      });
      byId("publishing-daily-empty").hidden = state.schedules.length > 0;
    }

    async function refresh() {
      const current = ++requestVersion;
      set("publishing-status", "正在读取发布数据…");
      const filters = byId("publishing-filters").elements;
      const resultQuery = new URLSearchParams({page: String(state.resultPage), page_size: "50"});
      if (filters.date.value) resultQuery.set("date", filters.date.value);
      if (filters.status.value) resultQuery.set("status", filters.status.value);
      if (filters.query.value.trim()) resultQuery.set("query", filters.query.value.trim());
      const calls = await Promise.allSettled([
        request("/api/accounts"), request("/api/content/videos"), request("/api/content/brands"),
        request(`/api/publish/results?${resultQuery}`), request("/api/publish/queue/batches"), request("/api/publish/schedule/daily"),
      ]);
      if (current !== requestVersion) return;
      const value = (index, fallback) => calls[index].status === "fulfilled" ? calls[index].value : fallback;
      state.accounts = value(0, {}).accounts || [];
      const videos = value(1, {}); state.videos = videos.videos || [];
      state.brands = value(2, {}).brands || [];
      const resultPayload = value(3, {});
      state.results = resultPayload.tasks || [];
      state.resultPage = resultPayload.page || 1; state.resultPages = resultPayload.total_pages || 1; state.resultTotal = resultPayload.total || 0; state.resultSummary = resultPayload.summary || {};
      state.batches = value(4, {}).runs || [];
      state.schedules = value(5, {}).schedules || [];
      set("publishing-videos", String(videos.available_count ?? state.videos.filter((item) => !item.used).length));
      set("publishing-video-total", String(videos.video_count ?? state.videos.length));
      set("publishing-video-used", String(videos.used_count ?? state.videos.filter((item) => item.used).length));
      set("publishing-brand-total", String(state.brands.length));
      set("publishing-copy-total", String(state.brands.reduce((sum, item) => sum + Number(item.copy_count || 0), 0)));
      set("publishing-content-note", `${state.brands.length} 个品牌`);
      renderSelectors(); renderResults(); renderBatches(); renderSchedules();
      const failures = calls.filter((item) => item.status === "rejected").length;
      set("publishing-status", failures ? `部分数据不可用（${failures} 个接口）` : "发布数据已更新。");
      byId("publishing-status").classList.toggle("error", failures > 0);
    }

    function selected(form) { return [...form.querySelectorAll('input[name="account_ids"]:checked')].map((item) => item.value); }
    function dialogStatus(form, value, error) { const node = form.querySelector("[data-dialog-status]"); node.textContent = value; node.classList.toggle("error", Boolean(error)); }
    function lockForm(form) {
      if (submitting.has(form)) return null;
      submitting.add(form);
      const button = form.querySelector('button[type="submit"]');
      if (button) button.disabled = true;
      return () => { submitting.delete(form); if (button) button.disabled = false; };
    }
    function openDialog(id) { const dialog = byId(id); if (typeof dialog.showModal === "function") dialog.showModal(); else dialog.open = true; }
    function closeDialog(id) { const dialog = byId(id); if (typeof dialog.close === "function") dialog.close(); else dialog.open = false; }

    async function submitBatch(event) {
      event.preventDefault(); const form = event.currentTarget; const unlock = lockForm(form); if (!unlock) return;
      const payload = buildBatchPayload(form.elements, selected(form));
      if (!payload.account_ids.length) { dialogStatus(form, "至少选择一个账号。", true); unlock(); return; }
      try { const result = await json("/api/publish/queue/batch", "POST", payload); dialogStatus(form, `已创建 ${result.created || 0} 条，跳过 ${result.skipped || 0} 条。`); closeDialog("publishing-batch-dialog"); await refresh(); }
      catch (error) { dialogStatus(form, error.message, true); }
      finally { unlock(); }
    }

    async function submitDaily(event) {
      event.preventDefault(); const form = event.currentTarget; const unlock = lockForm(form); if (!unlock) return; const ids = selected(form);
      if (!ids.length) { dialogStatus(form, "至少选择一个账号。", true); unlock(); return; }
      try { await json("/api/publish/schedule/daily", "POST", {enabled: true, start_date: form.elements.start_date.value, time: form.elements.time.value, brand_id: form.elements.brand_id.value, account_ids: ids}); closeDialog("publishing-daily-dialog"); await refresh(); }
      catch (error) { dialogStatus(form, error.message, true); }
      finally { unlock(); }
    }

    async function submitManual(event) {
      event.preventDefault(); const form = event.currentTarget; const unlock = lockForm(form); if (!unlock) return;
      const payload = buildBatchPayload(form.elements, []);
      try { await json("/api/publish/queue/manual-test", "POST", {account_id: form.elements.account_id.value, profile_id: form.elements.profile_id.value, video_id: form.elements.video_id.value, brand_id: form.elements.brand_id.value, copy_id: form.elements.copy_id.value, scheduled_at: payload.scheduled_at}); closeDialog("publishing-manual-dialog"); await refresh(); }
      catch (error) { dialogStatus(form, error.message, true); }
      finally { unlock(); }
    }

    const filters = byId("publishing-filters"); filters.elements.date.value = localDate(); set("publishing-date-note", filters.elements.date.value);
    filters.elements.date.addEventListener("change", () => { state.resultPage = 1; set("publishing-date-note", filters.elements.date.value || "全部日期"); refresh(); });
    filters.elements.status.addEventListener("change", () => { state.resultPage = 1; refresh(); });
    let searchTimer;
    filters.elements.query.addEventListener("input", () => { root.clearTimeout(searchTimer); searchTimer = root.setTimeout(() => { state.resultPage = 1; refresh(); }, 250); });
    byId("publishing-results-prev")?.addEventListener("click", () => { state.resultPage -= 1; refresh(); });
    byId("publishing-results-next")?.addEventListener("click", () => { state.resultPage += 1; refresh(); });
    byId("publishing-filter-reset")?.addEventListener("click", () => { filters.reset(); filters.elements.date.value = localDate(); state.resultPage = 1; set("publishing-date-note", filters.elements.date.value); refresh(); });
    byId("publishing-refresh")?.addEventListener("click", refresh);
    byId("publishing-batch-open")?.addEventListener("click", () => openDialog("publishing-batch-dialog"));
    byId("publishing-manual-open")?.addEventListener("click", async () => { openDialog("publishing-manual-dialog"); await syncManualCopy(); });
    byId("publishing-daily-open")?.addEventListener("click", () => openDialog("publishing-daily-dialog"));
    doc.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => closeDialog(button.dataset.close)));
    doc.querySelectorAll('input[name="select_all"]').forEach((input) => input.addEventListener("change", () => input.form.querySelectorAll('input[name="account_ids"]').forEach((item) => { item.checked = input.checked; })));
    byId("publishing-manual-form").elements.account_id.addEventListener("change", syncManualProfiles);
    byId("publishing-manual-form").elements.brand_id.addEventListener("change", syncManualCopy);
    byId("publishing-batch-form").addEventListener("submit", submitBatch); byId("publishing-daily-form").addEventListener("submit", submitDaily); byId("publishing-manual-form").addEventListener("submit", submitManual);
    byId("publishing-sync-videos")?.addEventListener("click", async () => { try { await json("/api/content/videos/sync", "POST", {}); await refresh(); } catch (error) { set("publishing-status", error.message); } });
    byId("publishing-batches-body")?.addEventListener("click", async (event) => { const button = event.target.closest("[data-delete-batch]"); const id = button?.dataset.deleteBatch; if (id && (!root.confirm || root.confirm("仅删除批次记录，不会撤销已生成的发布任务。继续吗？"))) { button.disabled = true; try { await json(`/api/publish/queue/batches/${encodeURIComponent(id)}`, "DELETE", {}); await refresh(); } catch (error) { set("publishing-status", error.message); byId("publishing-status").classList.add("error"); } finally { button.disabled = false; } } });
    byId("publishing-daily-body")?.addEventListener("click", async (event) => { const button = event.target.closest("[data-delete-schedule]"); const id = button?.dataset.deleteSchedule; if (id && (!root.confirm || root.confirm("确认删除这个每日发布配置？"))) { button.disabled = true; try { await json(`/api/publish/schedule/daily/${encodeURIComponent(id)}`, "DELETE", {}); await refresh(); } catch (error) { set("publishing-status", error.message); byId("publishing-status").classList.add("error"); } finally { button.disabled = false; } } });
    refresh();
    return {refresh, renderResults, state};
  }

  return {accountId, boot, buildBatchPayload, filterResults, localDate, safeTikTokUrl, timeText};
});
