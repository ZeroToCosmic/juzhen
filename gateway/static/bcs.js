/* Business control system frontend (talks to the central service). */
(function () {
  "use strict";

  const CENTRAL_URL = window.BCS_CENTRAL_URL || "http://127.0.0.1:8000";
  const TENANT_ID = window.BCS_TENANT_ID || "tenant-default";

  function centralUrl(path) {
    return CENTRAL_URL + path;
  }

  function headers() {
    return { "X-Tenant-ID": TENANT_ID, "Content-Type": "application/json" };
  }

  async function requestJson(path, options) {
    const response = await fetch(centralUrl(path), Object.assign({ headers: headers() }, options));
    const body = await response.json().catch(function () { return {}; });
    if (!response.ok) {
      throw new Error(body.detail || ("central " + response.status));
    }
    return body;
  }

  function setText(selector, value) {
    const node = document.querySelector(selector);
    if (node) {
      node.textContent = String(value);
    }
  }

  function statusBadge(status) {
    const cls = { SUCCESS: "status-ok", ACTIVE: "status-ok", ONLINE: "status-ok",
      QUEUED: "status-warn", PENDING: "status-warn", MANUAL_VERIFIED: "status-warn",
      DLQ: "status-err", FAILED: "status-err", MISSED: "status-err", OFFLINE: "status-err",
      SUSPENDED: "status-err", CAPTCHA: "status-err" }[status] || "status-info";
    return '<span class="status-badge ' + cls + '">' + status + "</span>";
  }

  async function loadDashboard() {
    try {
      const summary = await requestJson("/api/central/dashboard/summary");
      const stats = [
        ["今日任务", summary.tasks_today],
        ["成功率", summary.success_rate === null ? "-" : (summary.success_rate * 100).toFixed(1) + "%"],
        ["执行中窗口", summary.running_windows],
        ["排队中", summary.queued],
        ["待人工处理", summary.dlq],
        ["在线设备", summary.online_devices + " / " + summary.total_devices],
      ];
      const container = document.querySelector("#dashboard-stats");
      container.textContent = "";
      stats.forEach(function (entry) {
        const card = document.createElement("div");
        card.className = "stat-card";
        const label = document.createElement("div");
        label.className = "label";
        label.textContent = entry[0];
        const value = document.createElement("div");
        value.className = "value";
        value.textContent = entry[1];
        card.appendChild(label);
        card.appendChild(value);
        container.appendChild(card);
      });
      setText("#dashboard-error", "");
    } catch (error) {
      setText("#dashboard-error", "看板加载失败: " + error.message);
    }
  }

  async function loadDevices() {
    try {
      const body = await requestJson("/api/central/devices");
      const tbody = document.querySelector("#devices-body");
      tbody.textContent = "";
      body.devices.forEach(function (device) {
        const row = document.createElement("tr");
        [device.device_id, statusBadge(device.status), device.agent_version,
          device.used_accounts + "/" + device.max_accounts,
          (device.capabilities && device.capabilities.actions || []).join(","),
          device.last_heartbeat_at || "-"].forEach(function (cell) {
          const td = document.createElement("td");
          td.innerHTML = cell;
          row.appendChild(td);
        });
        const action = document.createElement("td");
        const toggle = document.createElement("button");
        toggle.textContent = device.enabled ? "停用" : "启用";
        toggle.addEventListener("click", function () {
          requestJson("/api/central/devices/" + device.device_id, {
            method: "PATCH",
            body: JSON.stringify({ enabled: !device.enabled }),
          }).then(loadDevices).catch(function (error) {
            setText("#devices-error", error.message);
          });
        });
        action.appendChild(toggle);
        row.appendChild(action);
        tbody.appendChild(row);
      });
      setText("#devices-error", "");
    } catch (error) {
      setText("#devices-error", "设备列表加载失败: " + error.message);
    }
  }

  async function loadSubtaskCounts() {
    try {
      const body = await requestJson("/api/central/subtasks");
      const counts = {};
      body.subtasks.forEach(function (subtask) {
        counts[subtask.status] = (counts[subtask.status] || 0) + 1;
      });
      const tbody = document.querySelector("#subtasks-body");
      tbody.textContent = "";
      Object.keys(counts).forEach(function (status) {
        const row = document.createElement("tr");
        const tdStatus = document.createElement("td");
        tdStatus.innerHTML = statusBadge(status);
        const tdCount = document.createElement("td");
        tdCount.textContent = counts[status];
        row.appendChild(tdStatus);
        row.appendChild(tdCount);
        tbody.appendChild(row);
      });
    } catch (error) {
      setText("#tasks-error", "子任务加载失败: " + error.message);
    }
  }

  function wireTaskForm() {
    const form = document.querySelector("#task-form");
    if (!form) {
      return;
    }
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      const data = new FormData(form);
      let params = {};
      try {
        params = JSON.parse(data.get("params") || "{}");
      } catch (error) {
        setText("#task-result", "");
        setText("#tasks-error", "参数不是合法 JSON");
        return;
      }
      const accountIds = String(data.get("account_ids") || "")
        .split(",")
        .map(function (value) { return value.trim(); })
        .filter(Boolean);
      const payload = {
        task_type: data.get("task_type"),
        params: params,
        account_ids: accountIds,
        priority: data.get("priority"),
        strategy_version: "1.0.0",
      };
      requestJson("/api/central/tasks", {
        method: "POST",
        body: JSON.stringify(payload),
      }).then(function (body) {
        setText("#tasks-error", "");
        setText("#task-result", "任务已创建: " + body.task_id + "（子任务 " + body.subtask_count + " 个）");
        form.reset();
        loadSubtaskCounts();
      }).catch(function (error) {
        setText("#task-result", "");
        setText("#tasks-error", "创建失败: " + error.message);
      });
    });
  }

  let lastSeq = "";
  let wsRetryTimer = null;

  function refreshByEvent(message) {
    const type = message.type || "";
    if (type === "subtask.result" || type === "task.created" ||
        type === "task.started" || type === "task.missed" ||
        type === "account.status" || type === "account.circuit_broken" ||
        type === "account.probe_scheduled") {
      if (document.querySelector("#panel-dashboard")) {
        loadDashboard();
      }
      if (document.querySelector("#panel-tasks")) {
        loadSubtaskCounts();
      }
    }
  }

  function connectWebSocket() {
    if (typeof WebSocket === "undefined") {
      return;
    }
    try {
      const wsBase = CENTRAL_URL.replace(/^http/, "ws");
      const query = "tenant_id=" + encodeURIComponent(TENANT_ID) +
        (lastSeq ? "&last_seq=" + encodeURIComponent(lastSeq) : "");
      const socket = new WebSocket(wsBase + "/ws/events?" + query);
      socket.onmessage = function (event) {
        let message;
        try {
          message = JSON.parse(event.data);
        } catch (error) {
          return;
        }
        if (message.seq) {
          lastSeq = message.seq;
        }
        refreshByEvent(message);
      };
      socket.onclose = function () {
        wsRetryTimer = setTimeout(connectWebSocket, 5000);
      };
      socket.onerror = function () {
        socket.close();
      };
    } catch (error) {
      wsRetryTimer = setTimeout(connectWebSocket, 10000);
    }
  }

  function start() {
    wireTaskForm();
    if (document.querySelector("#panel-dashboard")) {
      loadDashboard();
    }
    if (document.querySelector("#panel-devices")) {
      loadDevices();
    }
    if (document.querySelector("#panel-tasks")) {
      loadSubtaskCounts();
    }
    connectWebSocket();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
