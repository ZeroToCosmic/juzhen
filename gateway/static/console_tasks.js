(function () {
  "use strict";
  const root = document.querySelector("#console-tasks");
  if (!root) return;
  const byId = (id) => document.getElementById(id);
  const timeText = (value) => value ? String(value).replace("T", " ").replace("Z", "").slice(0, 16) : "—";
  let version = 0;

  async function refresh() {
    const current = ++version;
    byId("tasks-status").textContent = "正在读取中控任务…";
    try {
      const response = await fetch("/console/api/tasks", {headers: {Accept: "application/json"}});
      if (!response.ok) throw new Error(`读取失败（${response.status}）`);
      const payload = await response.json();
      if (current !== version) return;
      const rows = payload.tasks || [];
      const body = byId("tasks-body"); body.replaceChildren();
      rows.forEach((item) => {
        const row = document.createElement("tr");
        [item.task_id || item.id || "—", item.task_type || item.executor_kind || "—", item.status || "—", timeText(item.updated_at || item.created_at)].forEach((value) => { const cell = document.createElement("td"); cell.textContent = value; row.append(cell); });
        body.append(row);
      });
      byId("tasks-empty").hidden = rows.length > 0;
      byId("tasks-status").textContent = payload.connected === false ? "本机尚未配置中控设备身份。" : "任务状态已更新。";
      byId("tasks-status").classList.remove("error");
    } catch (error) {
      if (current !== version) return;
      byId("tasks-status").textContent = error.message || "读取失败";
      byId("tasks-status").classList.add("error");
      byId("tasks-empty").hidden = false;
    }
  }
  byId("tasks-refresh")?.addEventListener("click", refresh);
  refresh();
})();
