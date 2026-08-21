const { test } = require("node:test");
const assert = require("node:assert");

const fs = require("node:fs");
const path = require("node:path");

const bcsSource = fs.readFileSync(
  path.join(__dirname, "..", "gateway", "static", "bcs.js"),
  "utf-8"
);

function stubFetch(handler) {
  const calls = [];
  global.fetch = async (url, options) => {
    calls.push({ url, options });
    const body = handler(url, options);
    return {
      ok: body.ok !== false,
      status: body.status || 200,
      json: async () => body.json || {},
    };
  };
  return calls;
}

function createDom() {
  const elements = new Map();
  const registry = {};
  const document = {
    readyState: "complete",
    querySelector(selector) {
      return elements.get(selector) || null;
    },
    createElement(tag) {
      return {
        tagName: tag,
        textContent: "",
        innerHTML: "",
        className: "",
        _children: [],
        appendChild(child) {
          this._children.push(child);
        },
        addEventListener(type, fn) {
          (registry[tag + ":" + type] = registry[tag + ":" + type] || []).push(fn);
        },
      };
    },
    addEventListener() {},
  };
  function node(id) {
    if (!elements.has(id)) {
      elements.set(id, {
        textContent: "",
        innerHTML: "",
        _children: [],
        addEventListener() {},
        appendChild(child) {
          this._children.push(child);
        },
      });
    }
    return elements.get(id);
  }
  ["#dashboard-stats", "#dashboard-error", "#devices-body", "#devices-error",
    "#subtasks-body", "#tasks-error", "#task-result", "#task-form",
    "#panel-dashboard", "#panel-devices", "#panel-tasks"].forEach(node);
  return { document, node };
}

test("bcs.js dashboard renders summary cards via textContent", async () => {
  const { document, node } = createDom();
  global.document = document;
  const calls = stubFetch((url) => ({
    json: {
      tasks_today: 5,
      success_rate: 0.8,
      running_windows: 3,
      queued: 2,
      dlq: 1,
      online_devices: 2,
      total_devices: 3,
    },
  }));

  const run = new Function("window", bcsSource + "\n;window.__bcsStart && window.__bcsStart();");
  global.window = { BCS_CENTRAL_URL: "http://central" };
  try {
    run(global.window);
  } catch (error) {
    // start runs asynchronously; ignore sync errors from missing DOM pieces
  }
  await new Promise((resolve) => setTimeout(resolve, 20));

  assert.ok(calls.some((call) => call.url.endsWith("/api/central/dashboard/summary")));
  function collectText(n) {
    let text = n.textContent || "";
    (n._children || []).forEach(function (child) {
      text += collectText(child);
    });
    return text;
  }
  const rendered = collectText(node("#dashboard-stats"));
  assert.ok(rendered.includes("5"), "tasks_today rendered");
  assert.ok(rendered.includes("80.0%"), "success rate rendered");
});

test("bcs.js sends tenant header on requests", async () => {
  const { document } = createDom();
  global.document = document;
  global.window = { BCS_TENANT_ID: "tenant-xyz" };
  const calls = stubFetch(() => ({ json: { devices: [] } }));
  const run = new Function("window", bcsSource);
  run(global.window);
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.ok(calls.length > 0);
  assert.strictEqual(calls[0].options.headers["X-Tenant-ID"], "tenant-xyz");
});

test("bcs.js surfaces central errors in textContent", async () => {
  const { document, node } = createDom();
  global.document = document;
  global.window = {};
  stubFetch(() => ({ ok: false, status: 409, json: { detail: "stale generation" } }));
  const run = new Function("window", bcsSource);
  run(global.window);
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.ok(node("#dashboard-error").textContent.includes("stale generation"));
});

function stubWebSocketClass(handlers) {
  const sockets = [];
  global.WebSocket = function (url) {
    this.url = url;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;
    sockets.push(this);
    if (handlers.onConstruct) {
      handlers.onConstruct(this);
    }
  };
  global.WebSocket.prototype.close = function () {
    if (this.onclose) {
      this.onclose();
    }
  };
  return sockets;
}

test("bcs.js connects to ws with tenant and last_seq", async () => {
  const { document } = createDom();
  global.document = document;
  global.window = { BCS_TENANT_ID: "tenant-ws", BCS_CENTRAL_URL: "http://127.0.0.1:8000" };
  stubFetch(() => ({ json: { devices: [] } }));
  const sockets = stubWebSocketClass({});
  const run = new Function("window", bcsSource);
  run(global.window);
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.ok(sockets.length > 0);
  assert.ok(sockets[0].url.includes("/ws/events"));
  assert.ok(sockets[0].url.includes("tenant_id=tenant-ws"));
});

test("bcs.js refreshes dashboard on subtask.result event", async () => {
  const { document } = createDom();
  global.document = document;
  global.window = {};
  let summaryCalls = 0;
  stubFetch((url) => {
    if (url.endsWith("/api/central/dashboard/summary")) {
      summaryCalls += 1;
    }
    return { json: { tasks_today: 1, success_rate: null, running_windows: 0, queued: 0, dlq: 0, online_devices: 0, total_devices: 0 } };
  });
  const sockets = stubWebSocketClass({});
  const run = new Function("window", bcsSource);
  run(global.window);
  await new Promise((resolve) => setTimeout(resolve, 20));
  const before = summaryCalls;
  sockets[0].onmessage({ data: JSON.stringify({ seq: "1-1", type: "subtask.result", payload: { status: "SUCCESS" } }) });
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.ok(summaryCalls > before, "dashboard refreshed on event");
});

test("bcs.js records last_seq and reconnects on close", async () => {
  const { document } = createDom();
  global.document = document;
  global.window = {};
  stubFetch(() => ({ json: { devices: [] } }));
  const sockets = stubWebSocketClass({});
  const run = new Function("window", bcsSource);
  run(global.window);
  await new Promise((resolve) => setTimeout(resolve, 20));
  sockets[0].onmessage({ data: JSON.stringify({ seq: "7-0", type: "ping" }) });
  sockets[0].close();
  await new Promise((resolve) => setTimeout(resolve, 5200));
  assert.ok(sockets.length >= 2, "reconnected after close");
  assert.ok(
    sockets.some((socket) => socket.url.includes("last_seq=7-0")),
    "reconnect carries last_seq"
  );
});
