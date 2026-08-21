(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root?.document?.querySelector("#console-accounts-windows")) api.boot(root);
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const shortId = (value) => { const text = String(value || ""); return text.length > 18 ? `${text.slice(0, 8)}…${text.slice(-6)}` : text || "—"; };
  const timeText = (value) => value ? String(value).replace("T", " ").replace("Z", "").slice(0, 16) : "—";
  const proxyDisplay = (value) => {
    const parts = String(value || "").split(":");
    return parts.length >= 2 ? `${parts[0]}:${parts[1]}` : "未分配";
  };
  const searchAccounts = (items, query) => {
    const needle = String(query || "").trim().toLocaleLowerCase("zh-CN");
    if (!needle) return items;
    return items.filter((item) => [item.account_name, item.buffer_account_id, item.id, item.last_channel_sync_error, ...(item.buffer_profile_ids || [])]
      .some((value) => String(value || "").toLocaleLowerCase("zh-CN").includes(needle)));
  };

  function selectedWindows(windows, selectedIds) {
    const selected = new Set(selectedIds.map(String));
    return windows.filter((item) => selected.has(String(item.profile_id || item.profile_no || "")))
      .map((item) => ({profile_id: item.profile_id || "", profile_no: item.profile_no || ""}));
  }

  function boot(root) {
    const doc = root.document;
    const byId = (id) => doc.getElementById(id);
    const state = {accounts: [], windows: [], windowPage: 1, selectedWindowIds: new Set()};
    let version = 0;
    const submitting = new WeakSet();
    let openingWindows = false;

    async function request(url, options) {
      const response = await root.fetch(url, options);
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.error?.message || payload.error || `HTTP ${response.status}`);
      return payload;
    }
    const json = (url, method, body) => request(url, {method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    const set = (id, value) => { const node = byId(id); if (node) node.textContent = value; };

    function renderAccounts() {
      const query = byId("accounts-filter").elements.query.value;
      const items = searchAccounts(state.accounts, query);
      const body = byId("accounts-roster-body"); body.replaceChildren();
      items.forEach((item) => {
        const row = doc.createElement("tr");
        const select = doc.createElement("td"); const checkbox = doc.createElement("input"); checkbox.type = "checkbox"; checkbox.dataset.accountSelect = item.id; checkbox.setAttribute("aria-label", `选择 ${item.account_name || item.id}`); select.append(checkbox); row.append(select);
        const name = doc.createElement("td"); const main = doc.createElement("span"); main.className = "cell-main"; main.textContent = item.account_name || item.buffer_account_id || item.id; const sub = doc.createElement("span"); sub.className = "cell-sub"; sub.textContent = `Token ${item.buffer_token || "—"}`; name.append(main, sub); row.append(name);
        const profiles = doc.createElement("td"); profiles.textContent = (item.buffer_profile_ids || []).length ? (item.buffer_profile_ids || []).map(shortId).join("、") : "未同步"; row.append(profiles);
        const proxy = doc.createElement("td"); proxy.textContent = proxyDisplay(item.proxy_session); row.append(proxy);
        const sync = doc.createElement("td"); const badge = doc.createElement("span"); badge.className = `console-badge ${item.last_channel_sync_error ? "failed" : item.last_channel_sync_at ? "success" : "pending"}`; badge.textContent = item.last_channel_sync_error ? "同步失败" : item.last_channel_sync_at ? "已同步" : "待同步"; sync.append(badge); row.append(sync);
        const updated = doc.createElement("td"); updated.textContent = timeText(item.last_channel_sync_at); row.append(updated);
        const actions = doc.createElement("td"); actions.className = "action console-row-actions";
        [["同步", "sync"], ["代理", "proxy"], ["编辑", "edit"]].forEach(([label, action]) => { const button = doc.createElement("button"); button.type = "button"; button.className = "console-link"; button.textContent = label; button.dataset.accountAction = action; button.dataset.accountId = item.id; actions.append(button); });
        row.append(actions); body.append(row);
      });
      byId("accounts-roster-empty").hidden = items.length > 0;
    }

    function renderWindows() {
      const body = byId("windows-body"); body.replaceChildren();
      const pageSize = 12;
      const pages = Math.max(Math.ceil(state.windows.length / pageSize), 1);
      state.windowPage = Math.min(Math.max(state.windowPage, 1), pages);
      state.windows.slice((state.windowPage - 1) * pageSize, state.windowPage * pageSize).forEach((item) => {
        const row = doc.createElement("tr");
        const select = doc.createElement("td"); const checkbox = doc.createElement("input"); checkbox.type = "checkbox"; checkbox.dataset.windowSelect = item.profile_id || item.profile_no || ""; checkbox.setAttribute("aria-label", `选择 ${item.name || item.profile_no || "窗口"}`); select.append(checkbox); row.append(select);
        checkbox.checked = state.selectedWindowIds.has(checkbox.dataset.windowSelect);
        [item.profile_no || "—", shortId(item.profile_id), item.name || "—", item.group_name || "—", item.username || "—"].forEach((value) => { const cell = doc.createElement("td"); cell.textContent = value; row.append(cell); });
        body.append(row);
      });
      byId("windows-empty").hidden = state.windows.length > 0;
      set("windows-page-meta", `第 ${state.windowPage} / ${pages} 页 · 共 ${state.windows.length} 个窗口`);
      byId("windows-prev").disabled = state.windowPage <= 1;
      byId("windows-next").disabled = state.windowPage >= pages;
    }

    async function refresh() {
      const current = ++version; set("accounts-windows-status", "正在读取账号和窗口…");
      const calls = await Promise.allSettled([request("/api/accounts"), request("/api/browser/adspower-windows"), request("/api/proxy-pool/status?page=1&page_size=1")]);
      if (current !== version) return;
      const accounts = calls[0].status === "fulfilled" ? calls[0].value : {};
      const windows = calls[1].status === "fulfilled" ? calls[1].value : {};
      const proxy = calls[2].status === "fulfilled" ? calls[2].value : {};
      state.accounts = accounts.accounts || []; state.windows = windows.windows || [];
      const availableWindowIds = new Set(state.windows.map((item) => String(item.profile_id || item.profile_no || "")));
      state.selectedWindowIds.forEach((id) => { if (!availableWindowIds.has(id)) state.selectedWindowIds.delete(id); });
      const profiles = state.accounts.reduce((sum, item) => sum + (item.buffer_profile_ids || []).length, 0);
      set("accounts-count", String(accounts.count ?? state.accounts.length)); set("accounts-profile-count", String(profiles));
      set("accounts-available-note", `${accounts.available_count ?? state.accounts.filter((item) => (item.buffer_profile_ids || []).length).length} 个可用`);
      set("windows-count", String(windows.count ?? state.windows.length)); set("windows-status-note", calls[1].status === "fulfilled" ? "AdsPower 已连接" : "AdsPower 不可用");
      set("proxy-remaining", String(proxy.remaining ?? proxy.available ?? "—")); set("proxy-total-note", `共 ${proxy.total ?? "—"} 条`);
      renderAccounts(); renderWindows();
      const failures = calls.filter((item) => item.status === "rejected").length;
      set("accounts-windows-status", failures ? `部分数据不可用（${failures} 个接口）` : "账号与窗口已更新。");
      byId("accounts-windows-status").classList.toggle("error", failures > 0);
    }

    function openDialog(id) { const dialog = byId(id); if (typeof dialog.showModal === "function") dialog.showModal(); else dialog.open = true; }
    function closeDialog(id) { const dialog = byId(id); if (typeof dialog.close === "function") dialog.close(); else dialog.open = false; }
    function dialogStatus(form, value, error) { const node = form.querySelector("[data-dialog-status]"); node.textContent = value; node.classList.toggle("error", Boolean(error)); }
    function lockForm(form) {
      if (submitting.has(form)) return null;
      submitting.add(form); const button = form.querySelector('button[type="submit"]'); if (button) button.disabled = true;
      return () => { submitting.delete(form); if (button) button.disabled = false; };
    }

    function openAccount(item) {
      const form = byId("account-form"); form.reset();
      form.elements.id.value = item?.id || ""; form.elements.account_name.value = item?.account_name || ""; form.elements.buffer_api.value = item?.buffer_api || "";
      set("account-dialog-title", item ? "编辑 Buffer 账号" : "添加 Buffer 账号"); dialogStatus(form, ""); openDialog("account-dialog");
    }

    function openProxy(item) {
      const form = byId("proxy-form"); form.reset(); form.elements.account_id.value = item.id; set("proxy-dialog-title", `分配代理 · ${item.account_name || item.id}`); dialogStatus(form, ""); openDialog("proxy-dialog");
    }

    async function saveAccount(event) {
      event.preventDefault(); const form = event.currentTarget; const unlock = lockForm(form); if (!unlock) return;
      try { await json("/api/accounts/save", "POST", {id: form.elements.id.value, account_name: form.elements.account_name.value.trim(), buffer_token: form.elements.buffer_token.value, buffer_api: form.elements.buffer_api.value.trim()}); closeDialog("account-dialog"); await refresh(); }
      catch (error) { dialogStatus(form, error.message, true); }
      finally { unlock(); }
    }

    async function saveProxy(event) {
      event.preventDefault(); const form = event.currentTarget; const unlock = lockForm(form); if (!unlock) return;
      try { await json("/api/accounts/proxy", "POST", {account_id: form.elements.account_id.value, mode: form.elements.mode.value, proxy: form.elements.proxy.value.trim()}); closeDialog("proxy-dialog"); await refresh(); }
      catch (error) { dialogStatus(form, error.message, true); }
      finally { unlock(); }
    }

    async function syncIds(ids) {
      if (!ids.length) { set("accounts-windows-status", "请先选择至少一个账号。"); return; }
      set("accounts-windows-status", "正在同步账号…");
      const results = await Promise.allSettled(ids.map((id) => json("/api/accounts/discover", "POST", {accountId: id})));
      const failed = results.filter((item) => item.status === "rejected").length; await refresh();
      if (failed) { set("accounts-windows-status", `${failed} 个账号同步失败。`); byId("accounts-windows-status").classList.add("error"); }
    }

    byId("accounts-filter").elements.query.addEventListener("input", renderAccounts);
    byId("accounts-windows-refresh")?.addEventListener("click", refresh); byId("windows-refresh")?.addEventListener("click", refresh);
    byId("account-create-open")?.addEventListener("click", () => openAccount(null));
    doc.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => closeDialog(button.dataset.close)));
    byId("account-form").addEventListener("submit", saveAccount); byId("proxy-form").addEventListener("submit", saveProxy);
    byId("accounts-roster-body").addEventListener("click", async (event) => {
      const button = event.target.closest("[data-account-action]"); if (!button) return;
      const item = state.accounts.find((account) => String(account.id) === String(button.dataset.accountId)); if (!item) return;
      if (button.dataset.accountAction === "edit") openAccount(item);
      else if (button.dataset.accountAction === "proxy") openProxy(item);
      else await syncIds([item.id]);
    });
    byId("accounts-sync-selected")?.addEventListener("click", () => syncIds([...doc.querySelectorAll("[data-account-select]:checked")].map((item) => item.dataset.accountSelect)));
    byId("accounts-sync-all")?.addEventListener("click", async () => {
      set("accounts-windows-status", "正在同步全部账号…");
      try { await json("/api/accounts/discover", "POST", {}); await refresh(); }
      catch (error) { set("accounts-windows-status", error.message); byId("accounts-windows-status").classList.add("error"); }
    });
    byId("accounts-select-all")?.addEventListener("change", (event) => doc.querySelectorAll("[data-account-select]").forEach((item) => { item.checked = event.currentTarget.checked; }));
    byId("windows-body")?.addEventListener("change", (event) => { const input = event.target.closest("[data-window-select]"); if (input) { if (input.checked) state.selectedWindowIds.add(input.dataset.windowSelect); else state.selectedWindowIds.delete(input.dataset.windowSelect); } });
    byId("windows-select-all")?.addEventListener("change", (event) => doc.querySelectorAll("[data-window-select]").forEach((item) => { item.checked = event.currentTarget.checked; if (item.checked) state.selectedWindowIds.add(item.dataset.windowSelect); else state.selectedWindowIds.delete(item.dataset.windowSelect); }));
    byId("windows-prev")?.addEventListener("click", () => { state.windowPage -= 1; renderWindows(); });
    byId("windows-next")?.addEventListener("click", () => { state.windowPage += 1; renderWindows(); });
    byId("windows-open-tile")?.addEventListener("click", async () => {
      if (openingWindows) return;
      const ids = [...state.selectedWindowIds];
      const windows = selectedWindows(state.windows, ids); if (!windows.length) { set("accounts-windows-status", "请先选择至少一个窗口。"); return; }
      if (windows.length > 8) { set("accounts-windows-status", "每次最多打开 8 个窗口。"); byId("accounts-windows-status").classList.add("error"); return; }
      openingWindows = true; byId("windows-open-tile").disabled = true;
      try { await json("/api/browser/open-tile", "POST", {windows}); set("accounts-windows-status", `已提交 ${windows.length} 个窗口。`); }
      catch (error) { set("accounts-windows-status", error.message); byId("accounts-windows-status").classList.add("error"); }
      finally { openingWindows = false; byId("windows-open-tile").disabled = false; }
    });
    refresh();
    return {refresh, state};
  }

  return {boot, proxyDisplay, searchAccounts, selectedWindows, shortId, timeText};
});
