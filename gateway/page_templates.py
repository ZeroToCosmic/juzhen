"""Inline page HTML templates (migrated from gateway/app.py)."""

DASHBOARD_PAGE_HTML = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="csrf-token" content="{{ csrf_token }}">
  <script src="{{ url_for('static', filename='management_fetch.js') }}"></script>
  <title>代理会话控制台</title>
  <style>
    :root {
      color-scheme: light;
      font-family: Arial, sans-serif;
      background: #f4f6f8;
      color: #1f2933;
    }
    body {
      margin: 0;
      min-height: 100vh;
    }
    .layout {
      display: grid;
      grid-template-columns: 280px minmax(0, 1fr);
      min-height: 100vh;
    }
    aside {
      background: #15202b;
      color: white;
      padding: 24px 18px;
    }
    h1 {
      font-size: 22px;
      margin: 0 0 22px;
      line-height: 1.25;
    }
    .nav {
      display: grid;
      gap: 8px;
    }
    .nav button {
      width: 100%;
      min-height: 44px;
      border: 1px solid rgba(255, 255, 255, 0.14);
      border-radius: 6px;
      padding: 0 12px;
      text-align: left;
      color: white;
      background: rgba(255, 255, 255, 0.06);
      cursor: pointer;
      font: inherit;
    }
    .nav button.active {
      background: #2f9b73;
      border-color: #2f9b73;
    }
    main {
      padding: 26px;
      overflow: auto;
    }
    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 18px;
    }
    h2 {
      margin: 0;
      font-size: 22px;
    }
    .panel {
      display: none;
      border: 1px solid #d7dde5;
      border-radius: 8px;
      background: white;
      padding: 20px;
    }
    .panel.active {
      display: block;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 14px;
    }
    label {
      display: grid;
      gap: 6px;
      font-size: 13px;
      font-weight: 700;
    }
    label.wide {
      grid-column: 1 / -1;
    }
    input, textarea {
      box-sizing: border-box;
      width: 100%;
      border: 1px solid #bac3cf;
      border-radius: 6px;
      padding: 9px 10px;
      font: inherit;
      background: #fff;
    }
    textarea {
      min-height: 160px;
      resize: vertical;
      font-family: Consolas, monospace;
      font-size: 13px;
    }
    .actions {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-top: 16px;
      flex-wrap: wrap;
    }
    .primary {
      min-height: 40px;
      border: 0;
      border-radius: 6px;
      padding: 0 16px;
      font: inherit;
      font-weight: 700;
      color: white;
      background: #176b4d;
      cursor: pointer;
    }
    .secondary {
      min-height: 40px;
      border: 1px solid #bac3cf;
      border-radius: 6px;
      padding: 0 16px;
      font: inherit;
      font-weight: 700;
      color: #1f2933;
      background: white;
      cursor: pointer;
    }
    .status-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
    }
    .status-item {
      border: 1px solid #d7dde5;
      border-radius: 8px;
      padding: 14px;
      background: #fbfcfd;
    }
    .status-label {
      font-size: 12px;
      color: #627080;
      margin-bottom: 6px;
    }
    .status-value {
      font-size: 18px;
      font-weight: 800;
    }
    .ok {
      color: #176b4d;
    }
    .warn {
      color: #985f0d;
    }
    pre {
      box-sizing: border-box;
      width: 100%;
      min-height: 90px;
      overflow: auto;
      border: 1px solid #d7dde5;
      border-radius: 8px;
      background: #101820;
      color: #e8eef4;
      padding: 14px;
      font-size: 13px;
      line-height: 1.45;
    }
    code {
      display: block;
      overflow-wrap: anywhere;
      border: 1px solid #d7dde5;
      border-radius: 8px;
      background: #f7f9fb;
      padding: 12px;
      font-family: Consolas, monospace;
      font-size: 13px;
    }
    @media (max-width: 780px) {
      .layout {
        grid-template-columns: 1fr;
      }
      aside {
        position: static;
      }
    }
  </style>
</head>
<body>
  <div class="layout">
    <aside>
      <h1>代理会话控制台</h1>
      <div class="nav">
        <button class="active" data-panel="status">1. 状态检查</button>
        <button data-panel="settings">2. 集中配置</button>
        <button data-panel="ip">3. 检测 IP</button>
        <button data-panel="buffer">4. 发布到 Buffer</button>
        <button data-panel="browser">5. 浏览器接管</button>
      </div>
    </aside>
    <main>
      <div class="topbar">
        <h2 id="panel-title">状态检查</h2>
        <button class="secondary" id="refresh-status" type="button">刷新</button>
      </div>

      <section class="panel active" id="panel-status">
        <div class="status-grid">
          <div class="status-item">
            <div class="status-label">服务</div>
            <div class="status-value ok" id="status-service">运行中</div>
          </div>
          <div class="status-item">
            <div class="status-label">代理</div>
            <div class="status-value" id="status-proxy">检查中</div>
          </div>
          <div class="status-item">
            <div class="status-label">外部服务</div>
            <div class="status-value" id="status-services">检查中</div>
          </div>
          <div class="status-item">
            <div class="status-label">浏览器</div>
            <div class="status-value" id="status-browser">检查中</div>
          </div>
        </div>
      </section>

      <section class="panel" id="panel-settings">
        <form id="settings-form">
          <div class="grid">
            <label>代理主机<input name="proxy.host" autocomplete="off"></label>
            <label>代理端口<input name="proxy.port" autocomplete="off"></label>
            <label>代理用户名<input name="proxy.username" autocomplete="off"></label>
            <label>代理密码<input name="proxy.password" type="password" autocomplete="off"></label>
            <label>代理协议<select name="proxy_pool.protocol"><option value="socks5">SOCKS5</option><option value="http">HTTP / HTTPS</option></select></label>
            <label class="wide">批量代理 IP 池<textarea name="proxy_pool.raw" placeholder="203.0.113.10:1080:example_user:example_password"></textarea></label>
            <label>IP 信息接口<input name="services.ipinfo_url" autocomplete="off"></label>
            <label>Buffer GraphQL 接口<input name="services.buffer_graphql_url" autocomplete="off"></label>
            <label>R2 Account Token<input name="r2.account_token" type="password" autocomplete="off"></label>
            <label>R2 Account ID<input name="r2.account_id" autocomplete="off"></label>
            <label>R2 访问密钥 ID<input name="r2.access_key_id" autocomplete="off"></label>
            <label>R2 秘密访问密钥<input name="r2.secret_access_key" type="password" autocomplete="off"></label>
            <label>R2 Bucket<input name="r2.bucket" autocomplete="off"></label>
            <label>R2 接口地址<input name="r2.endpoint_url" autocomplete="off"></label>
            <label>R2 公开访问域名<input name="r2.public_base_url" autocomplete="off"></label>
            <label>R2 文件前缀<input name="r2.prefix" autocomplete="off"></label>
            <label>IP 检测超时秒数<input name="timeouts.ip_check_seconds" type="number" min="1" step="1"></label>
            <label>Buffer 发布超时秒数<input name="timeouts.buffer_publish_seconds" type="number" min="1" step="1"></label>
            <label>发布队列间隔秒数<input name="publish_queue.interval_seconds" type="number" min="1" step="1"></label>
            <label>CDP 地址<input name="browser.cdp_url" autocomplete="off"></label>
            <label>任务目标<input name="browser.task_goal" autocomplete="off"></label>
            <label>默认模型 ID<input name="models.default_model_id" autocomplete="off"></label>
            <label>模型 ID<input name="models.items.0.id" autocomplete="off"></label>
            <label>模型供应商<select name="models.items.0.provider"><option value="grok">Grok</option><option value="deepseek">DeepSeek</option><option value="glm">GLM</option><option value="qwen">Qwen</option><option value="gpt">GPT</option></select></label>
            <label>模型接口地址<input name="models.items.0.base_url" autocomplete="off"></label>
            <label>模型 API Key<input name="models.items.0.api_key" type="password" autocomplete="off"></label>
            <label>模型名称<input name="models.items.0.model" autocomplete="off"></label>
            <label>调用模式<select name="models.items.0.mode"><option value="responses">Responses</option><option value="chat">Chat Completions</option></select></label>
          </div>
          <div class="cards pool-stats">
            <div class="card"><div class="label">IP池总数</div><div class="value" id="proxy-pool-total">0</div></div>
            <div class="card"><div class="label">已分配</div><div class="value" id="proxy-pool-assigned">0</div></div>
            <div class="card"><div class="label">剩余</div><div class="value" id="proxy-pool-remaining">0</div></div>
          </div>
          <div class="pool-list" id="proxy-pool-list"></div>
          <div class="actions">
            <button class="primary" type="submit">保存配置</button>
            <a class="secondary" href="/settings">高级配置</a>
            <span id="settings-status"></span>
          </div>
        </form>
      </section>

      <section class="panel" id="panel-ip">
        <form id="ip-form">
          <div class="grid">
            <label>账号 ID<input id="ip-account-id" autocomplete="off"></label>
          </div>
          <div class="actions">
            <button class="primary" type="submit">检测 IP</button>
          </div>
        </form>
        <pre id="ip-result">{}</pre>
      </section>

      <section class="panel" id="panel-buffer">
        <form id="buffer-form">
          <div class="grid">
            <label>账号 ID<input id="buffer-account-id" autocomplete="off"></label>
            <label>访问令牌<input id="buffer-access-token" type="password" autocomplete="off"></label>
          </div>
          <label style="margin-top:14px">发布内容 JSON<textarea id="buffer-payload">{"text":"hello","profile_ids":["profile-id"]}</textarea></label>
          <div class="actions">
            <button class="primary" type="submit">发布到 Buffer</button>
          </div>
        </form>
        <pre id="buffer-result">{}</pre>
      </section>

      <section class="panel" id="panel-browser">
        <div class="grid">
          <label>CDP 地址<input id="browser-cdp-url" autocomplete="off"></label>
          <label>任务目标<input id="browser-task-goal" autocomplete="off"></label>
        </div>
        <div class="actions">
          <button class="secondary" id="browser-sync" type="button">使用已保存配置</button>
        </div>
      </section>
    </main>
  </div>

  <script>
    const panels = [...document.querySelectorAll(".panel")];
    const navButtons = [...document.querySelectorAll(".nav button")];
    const panelTitle = document.querySelector("#panel-title");
    const numberFields = new Set([
      "timeouts.ip_check_seconds",
      "timeouts.buffer_publish_seconds",
      "publish_queue.interval_seconds",
    ]);
    let currentSettings = {};
    let settingsLoaded = false;

    function showPanel(name) {
      panels.forEach((panel) => panel.classList.toggle("active", panel.id === `panel-${name}`));
      navButtons.forEach((button) => button.classList.toggle("active", button.dataset.panel === name));
      panelTitle.textContent = navButtons.find((button) => button.dataset.panel === name).textContent.replace(/^\\d+\\.\\s*/, "");
    }

    function setNested(target, path, value) {
      const parts = path.split(".");
      let current = target;
      for (let index = 0; index < parts.length - 1; index += 1) {
        const part = parts[index];
        const nextPart = parts[index + 1];
        current[part] ||= /^\\d+$/.test(nextPart) ? [] : {};
        current = current[part];
      }
      const last = parts.at(-1);
      current[last] = value;
    }

    function getNested(target, path) {
      return path.split(".").reduce((current, part) => current?.[part], target);
    }

    function setStatus(id, value) {
      const element = document.querySelector(id);
      element.textContent = value ? "就绪" : "待配置";
      element.classList.toggle("ok", value);
      element.classList.toggle("warn", !value);
    }

    async function refreshStatus() {
      const response = await fetch("/api/status");
      const status = await response.json();
      document.querySelector("#status-service").textContent = status.service.running ? "运行中" : "已停止";
      setStatus("#status-proxy", status.config.proxy_configured);
      setStatus("#status-services", status.config.services_configured);
      setStatus("#status-browser", status.config.browser_configured);
    }

    async function loadSettings() {
      const response = await fetch("/api/settings");
      currentSettings = await response.json();
      for (const element of document.querySelector("#settings-form").elements) {
        if (!element.name) continue;
        element.value = getNested(currentSettings, element.name) ?? "";
        if (getNested(currentSettings._secrets_configured || {}, element.name)) {
          element.placeholder = "已配置，留空保持不变";
        }
      }
      document.querySelector("#browser-cdp-url").value = currentSettings.browser?.cdp_url ?? "";
      document.querySelector("#browser-task-goal").value = currentSettings.browser?.task_goal ?? "";
    }

    async function saveSettings(event) {
      event.preventDefault();
      const settings = {};
      for (const element of event.currentTarget.elements) {
        if (!element.name) continue;
        const value = numberFields.has(element.name) ? Number(element.value) : element.value;
        setNested(settings, element.name, value);
      }
      const response = await fetch("/api/settings", {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(settings),
      });
      document.querySelector("#settings-status").textContent = response.ok ? "已保存" : "保存失败";
      await loadSettings();
      await refreshStatus();
    }

    async function postJson(url, body) {
      return requestJson(url, "POST", body);
    }

    async function requestJson(url, method, body) {
      const response = await fetch(url, {
        method,
        headers: {"Content-Type": "application/json"},
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const data = await response.json();
      return {status: response.status, data};
    }

    async function checkIp(event) {
      event.preventDefault();
      const accountId = document.querySelector("#ip-account-id").value;
      const result = await postJson("/check_ip", {account_id: accountId});
      document.querySelector("#ip-result").textContent = JSON.stringify(result, null, 2);
    }

    async function publishBuffer(event) {
      event.preventDefault();
      const accountId = document.querySelector("#buffer-account-id").value;
      const accessToken = document.querySelector("#buffer-access-token").value;
      let payload;
      try {
        payload = JSON.parse(document.querySelector("#buffer-payload").value);
      } catch (error) {
        document.querySelector("#buffer-result").textContent = JSON.stringify({error: "发布内容 JSON 格式无效"}, null, 2);
        return;
      }
      const result = await postJson("/publish/buffer", {
        account_id: accountId,
        access_token: accessToken,
        payload,
      });
      document.querySelector("#buffer-result").textContent = JSON.stringify(result, null, 2);
    }

    navButtons.forEach((button) => button.addEventListener("click", () => showPanel(button.dataset.panel)));
    document.querySelector("#refresh-status").addEventListener("click", refreshStatus);
    document.querySelector("#settings-form").addEventListener("submit", saveSettings);
    document.querySelector("#ip-form").addEventListener("submit", checkIp);
    document.querySelector("#buffer-form").addEventListener("submit", publishBuffer);
    document.querySelector("#browser-sync").addEventListener("click", () => loadSettings());
    loadSettings().then(refreshStatus);
  </script>
</body>
</html>
"""

SETTINGS_PAGE_HTML = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="csrf-token" content="{{ csrf_token }}">
  <script src="{{ url_for('static', filename='management_fetch.js') }}"></script>
  <title>配置</title>
  <style>
    :root {
      color-scheme: light;
      font-family: Arial, sans-serif;
      background: #f6f7f9;
      color: #20242a;
    }
    body {
      margin: 0;
      padding: 32px;
    }
    main {
      max-width: 920px;
      margin: 0 auto;
    }
    h1 {
      font-size: 28px;
      margin: 0 0 24px;
    }
    section {
      border-top: 1px solid #d8dde5;
      padding: 20px 0;
    }
    h2 {
      font-size: 18px;
      margin: 0 0 16px;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 14px;
    }
    label {
      display: grid;
      gap: 6px;
      font-size: 13px;
      font-weight: 600;
    }
    input {
      box-sizing: border-box;
      width: 100%;
      min-height: 38px;
      border: 1px solid #b8c0cc;
      border-radius: 6px;
      padding: 8px 10px;
      font: inherit;
      background: white;
    }
    button {
      min-height: 40px;
      border: 0;
      border-radius: 6px;
      padding: 0 18px;
      font: inherit;
      font-weight: 700;
      background: #176b4d;
      color: white;
      cursor: pointer;
    }
    .actions {
      display: flex;
      align-items: center;
      gap: 12px;
      padding-top: 20px;
    }
    #status {
      min-height: 20px;
      font-size: 13px;
    }
  </style>
</head>
<body>
  <main>
    <h1>配置</h1>
    <form id="settings-form">
      <section>
        <h2>代理</h2>
        <div class="grid">
          <label>代理主机<input name="proxy.host" autocomplete="off"></label>
          <label>代理端口<input name="proxy.port" autocomplete="off"></label>
          <label>代理用户名<input name="proxy.username" autocomplete="off"></label>
          <label>代理密码<input name="proxy.password" type="password" autocomplete="off"></label>
          <label>代理协议<select name="proxy_pool.protocol"><option value="socks5">SOCKS5</option><option value="http">HTTP / HTTPS</option></select></label>
          <label class="wide">批量代理 IP 池<textarea name="proxy_pool.raw" placeholder="203.0.113.10:1080:example_user:example_password"></textarea></label>
        </div>
      </section>
      <section>
        <h2>外部服务</h2>
        <div class="grid">
          <label>IP 信息接口<input name="services.ipinfo_url" autocomplete="off"></label>
          <label>Buffer GraphQL 接口<input name="services.buffer_graphql_url" autocomplete="off"></label>
          <label>R2 Account Token<input name="r2.account_token" type="password" autocomplete="off"></label>
          <label>R2 Account ID<input name="r2.account_id" autocomplete="off"></label>
          <label>R2 访问密钥 ID<input name="r2.access_key_id" autocomplete="off"></label>
          <label>R2 秘密访问密钥<input name="r2.secret_access_key" type="password" autocomplete="off"></label>
          <label>R2 Bucket<input name="r2.bucket" autocomplete="off"></label>
          <label>R2 接口地址<input name="r2.endpoint_url" autocomplete="off"></label>
          <label>R2 公开访问域名<input name="r2.public_base_url" autocomplete="off"></label>
          <label>R2 文件前缀<input name="r2.prefix" autocomplete="off"></label>
        </div>
      </section>
      <section>
        <h2>超时</h2>
        <div class="grid">
          <label>IP 检测超时秒数<input name="timeouts.ip_check_seconds" type="number" min="1" step="1"></label>
          <label>Buffer 发布超时秒数<input name="timeouts.buffer_publish_seconds" type="number" min="1" step="1"></label>
          <label>发布队列间隔秒数<input name="publish_queue.interval_seconds" type="number" min="1" step="1"></label>
        </div>
      </section>
      <section>
        <h2>浏览器</h2>
        <p class="muted">AdsPower API 地址建议填写 http://local.adspower.net:50325；AdsPower 模式下请留空手动 CDP 地址。每次执行策略前都会重新打开默认网址并清理旧 Tab。</p>
        <div class="grid">
          <label>AdsPower API 地址<input name="adspower.base_url" autocomplete="off" placeholder="http://local.adspower.net:50325"></label>
          <label>手动 CDP 地址（AdsPower 模式下请留空）<input name="browser.cdp_url" autocomplete="off" placeholder="ws://127.0.0.1:9222/devtools/browser/..."></label>
          <label>任务目标<input name="browser.task_goal" autocomplete="off"></label>
          <label>浏览器默认网址<input name="browser.default_url" autocomplete="off" value="https://www.tiktok.com/" placeholder="https://www.tiktok.com/"></label>
        </div>
      </section>
      <div class="actions">
        <button type="submit">保存</button>
        <span id="status" role="status"></span>
      </div>
    </form>
  </main>
  <script>
    const form = document.querySelector("#settings-form");
    const status = document.querySelector("#status");
    const numberFields = new Set([
      "timeouts.ip_check_seconds",
      "timeouts.buffer_publish_seconds",
      "publish_queue.interval_seconds",
    ]);

    function setNested(target, path, value) {
      const parts = path.split(".");
      let current = target;
      for (let index = 0; index < parts.length - 1; index += 1) {
        const part = parts[index];
        const nextPart = parts[index + 1];
        current[part] ||= /^\\d+$/.test(nextPart) ? [] : {};
        current = current[part];
      }
      current[parts.at(-1)] = value;
    }

    function getNested(target, path) {
      return path.split(".").reduce((current, part) => current?.[part], target);
    }

    let settingsLoaded = false;

    async function loadSettings() {
      try {
        const response = await fetch("/api/settings");
        if (!response.ok) throw new Error("settings request failed");
        const settings = await response.json();
        for (const element of form.elements) {
          if (!element.name) continue;
          element.value = getNested(settings, element.name) ?? "";
          if (getNested(settings._secrets_configured || {}, element.name)) {
            element.placeholder = "已配置，留空保持不变";
          }
        }
        settingsLoaded = true;
      } catch (_error) {
        settingsLoaded = false;
        status.textContent = "配置读取失败，暂未保存";
      }
    }

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!settingsLoaded) {
        status.textContent = "配置尚未加载成功，暂未保存";
        return;
      }
      const settings = {};
      for (const element of form.elements) {
        if (!element.name) continue;
        const value = numberFields.has(element.name)
          ? Number(element.value)
          : element.value;
        setNested(settings, element.name, value);
      }
      const response = await fetch("/api/settings", {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(settings),
      });
      status.textContent = response.ok ? "已保存" : "保存失败";
    });

    loadSettings();
  </script>
</body>
</html>
"""

CONTROL_PAGE_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="csrf-token" content="{{ csrf_token }}">
  <script src="{{ url_for('static', filename='management_fetch.js') }}"></script>
  <title>自动化主控台</title>
  <style>
    :root {
      color-scheme: light;
      --app-bg: #f5f5f7;
      --surface: rgba(255, 255, 255, 0.82);
      --surface-solid: #ffffff;
      --surface-soft: #fbfbfd;
      --line: rgba(60, 60, 67, 0.14);
      --line-strong: rgba(60, 60, 67, 0.22);
      --text: #1d1d1f;
      --muted: #6e6e73;
      --accent: #147a5f;
      --accent-soft: rgba(20, 122, 95, 0.12);
      --warning-soft: #fff4df;
      --shadow-soft: 0 18px 50px rgba(15, 23, 42, 0.08);
      font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", "Microsoft YaHei", sans-serif;
      background: var(--app-bg);
      color: var(--text);
    }
    body {
      margin: 0;
      min-height: 100vh;
      background:
        radial-gradient(circle at top left, rgba(255, 255, 255, 0.9), transparent 34%),
        linear-gradient(135deg, #f5f5f7 0%, #eef1f6 100%);
    }
    .shell {
      display: grid;
      grid-template-columns: 268px minmax(0, 1fr);
      min-height: 100vh;
    }
    aside {
      position: sticky;
      top: 0;
      height: 100vh;
      box-sizing: border-box;
      background: rgba(247, 247, 249, 0.78);
      color: var(--text);
      padding: 24px 16px;
      border-right: 1px solid var(--line);
      backdrop-filter: blur(22px);
    }
    h1 {
      font-size: 21px;
      margin: 0 0 20px;
      line-height: 1.25;
      letter-spacing: 0;
    }
    nav {
      display: grid;
      gap: 6px;
    }
    nav button, nav .nav-link {
      width: 100%;
      min-height: 42px;
      border: 1px solid transparent;
      border-radius: 12px;
      padding: 0 12px;
      text-align: left;
      color: #3a3a3c;
      background: transparent;
      cursor: pointer;
      font: inherit;
      font-weight: 650;
      transition: all 160ms ease;
      display: flex;
      align-items: center;
      text-decoration: none;
      box-sizing: border-box;
    }
    nav button:hover, nav .nav-link:hover {
      background: rgba(0, 0, 0, 0.04);
    }
    nav button.active {
      background: rgba(255, 255, 255, 0.92);
      border-color: var(--line);
      box-shadow: 0 8px 24px rgba(15, 23, 42, 0.07);
      color: var(--accent);
    }
    main {
      padding: 28px;
      overflow: auto;
    }
    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 20px;
    }
    h2 {
      margin: 0;
      font-size: 28px;
      letter-spacing: 0;
    }
    .cards {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 14px;
      margin-bottom: 18px;
    }
    .card, .panel {
      border: 1px solid var(--line);
      border-radius: 18px;
      background: var(--surface);
      box-shadow: 0 18px 50px rgba(15, 23, 42, 0.08);
      backdrop-filter: blur(18px);
    }
    .card {
      padding: 16px;
    }
    .pool-stats {
      margin-top: 12px;
    }
    .pool-list {
      grid-column: 1 / -1;
      display: grid;
      gap: 8px;
      margin-top: 10px;
    }
    .proxy-pool-controls {
      display: grid;
      grid-template-columns: minmax(220px, 1fr) minmax(140px, 180px) auto auto;
      gap: 10px;
      align-items: end;
      margin-top: 14px;
    }
    .pool-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 9px 10px;
      background: var(--surface-soft);
      font-size: 13px;
    }
    .pool-row code {
      display: inline;
      width: auto;
      border: 0;
      border-radius: 0;
      background: transparent;
      color: var(--text);
      padding: 0;
      white-space: normal;
    }
    .badge {
      border-radius: 999px;
      padding: 4px 9px;
      background: var(--accent-soft);
      color: var(--accent);
      font-weight: 800;
      white-space: nowrap;
    }
    .badge.warn {
      background: var(--warning-soft);
      color: #9b5b15;
    }
    .label {
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 5px;
    }
    .value {
      font-size: 18px;
      font-weight: 800;
    }
    .ok { color: var(--accent); }
    .warn { color: #9b5b15; }
    .bad { color: #ad2f2f; }
    .panel {
      display: none;
      padding: 22px;
    }
    .panel.active {
      display: block;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
      gap: 12px;
    }
    label {
      display: grid;
      gap: 6px;
      font-size: 13px;
      font-weight: 650;
      color: #3a3a3c;
    }
    label.wide {
      grid-column: 1 / -1;
    }
    input, select, textarea {
      box-sizing: border-box;
      width: 100%;
      border: 1px solid var(--line-strong);
      border-radius: 12px;
      padding: 10px 12px;
      font: inherit;
      background: rgba(255, 255, 255, 0.86);
      color: var(--text);
      transition: all 160ms ease;
    }
    input:focus, select:focus, textarea:focus {
      outline: 0;
      border-color: rgba(20, 122, 95, 0.45);
      box-shadow: 0 0 0 4px rgba(20, 122, 95, 0.12);
    }
    textarea {
      min-height: 130px;
      resize: vertical;
      font-family: Consolas, monospace;
      font-size: 13px;
    }
    .actions {
      display: flex;
      align-items: center;
      gap: 9px;
      margin-top: 16px;
      flex-wrap: wrap;
    }
    .table-actions {
      display: flex;
      align-items: center;
      gap: 8px;
      white-space: nowrap;
    }
    .table-actions button {
      min-width: 58px;
      justify-content: center;
      padding: 0 10px;
    }
    button, a.button {
      min-height: 38px;
      border-radius: 999px;
      padding: 0 14px;
      font: inherit;
      font-weight: 650;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.9);
      color: var(--text);
      box-shadow: 0 4px 14px rgba(15, 23, 42, 0.04);
      transition: all 160ms ease;
    }
    button:hover, a.button:hover {
      transform: translateY(-1px);
      border-color: var(--line-strong);
      box-shadow: 0 10px 26px rgba(15, 23, 42, 0.08);
    }
    button.primary, a.primary {
      border-color: var(--accent);
      background: var(--accent);
      color: white;
    }
    pre, code {
      box-sizing: border-box;
      width: 100%;
      overflow: auto;
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 14px;
      background: #1f2937;
      color: #eaf1f8;
      padding: 12px;
      font-size: 13px;
      line-height: 1.45;
    }
    code {
      display: block;
      white-space: pre-wrap;
    }
    .flow {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 10px;
      margin-bottom: 16px;
    }
    .step {
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 12px;
      background: var(--surface-soft);
      font-weight: 650;
      text-align: center;
    }
    .table-wrap {
      overflow: auto;
      margin-top: 14px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    th, td {
      border-bottom: 1px solid var(--line);
      padding: 10px 9px;
      text-align: left;
      vertical-align: top;
    }
    th {
      color: var(--muted);
      font-size: 12px;
    }
    .chip-list {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .chip {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 3px 8px;
      background: var(--surface-soft);
      white-space: nowrap;
    }
    .muted {
      color: var(--muted);
    }
    .content-section + .content-section {
      border-top: 1px solid var(--line);
      margin-top: 18px;
      padding-top: 18px;
    }
    .settings-sections {
      display: grid;
      gap: 18px;
    }
    .settings-group {
      border-top: 1px solid var(--line);
      padding-top: 16px;
    }
    .settings-group:first-child {
      border-top: 0;
      padding-top: 0;
    }
    .settings-group-header {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
      flex-wrap: wrap;
    }
    .settings-group-header h3 {
      margin: 0;
    }
    .content-toolbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
    }
    .content-toolbar h3,
    .dialog-header h3 {
      margin: 0;
    }
    .compact-actions,
    .content-heading {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    #panel-strategies {
      max-width: 1180px;
    }
    #panel-strategies > .settings-group {
      border-top: 0;
      padding-top: 0;
    }
    #panel-strategies .grid:has(> label.wide:only-child) {
      display: none;
    }
    .browser-element-row,
    .browser-pattern-card,
    .browser-strategy-card,
    .browser-block-card {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 10px 0;
      border-bottom: 1px solid var(--line);
      flex-wrap: wrap;
    }
    .browser-element-row code {
      flex: 1 1 180px;
      min-width: 0;
      border: 0;
      padding: 0;
      background: transparent;
      overflow-wrap: anywhere;
      font-family: inherit;
      font-weight: 800;
      color: #0b5d43;
    }
    .browser-pattern-card > div:first-child,
    .browser-strategy-card > div:first-child,
    .browser-block-card > div:first-child {
      display: grid;
      gap: 4px;
      min-width: 240px;
    }
    .browser-pattern-card .muted,
    .browser-strategy-card .muted,
    .browser-block-card .muted {
      font-size: 12px;
      font-weight: 400;
    }
    .browser-library-empty {
      padding: 14px 0;
    }
    .browser-strategy-section {
      display: grid;
      gap: 12px;
    }
    .browser-strategy-list,
    .browser-pattern-list,
    .browser-strategy-actions {
      display: grid;
      gap: 10px;
    }
    .browser-strategy-editor {
      border: 1px solid var(--line);
      border-radius: 16px;
      background: #fff;
      padding: 16px;
      display: grid;
      gap: 16px;
      box-shadow: 0 10px 30px rgba(15, 23, 42, 0.04);
    }
    .browser-block-palette {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
      gap: 10px;
    }
    .browser-block-palette button {
      min-height: 48px;
    }
    .browser-element-row-title {
      display: grid;
      gap: 4px;
      flex: 1 1 220px;
      min-width: 0;
    }
    .browser-element-row-title strong {
      color: #0b5d43;
      font-size: 15px;
    }
    .content-metrics {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      margin-top: 12px;
      border-top: 1px solid var(--line);
      border-bottom: 1px solid var(--line);
    }
    .content-metric {
      padding: 11px 12px;
    }
    .content-metric + .content-metric {
      border-left: 1px solid var(--line);
    }
    .brand-folder-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
      gap: 10px;
      margin-top: 12px;
    }
    .brand-folder {
      min-height: 104px;
      padding: 13px;
      text-align: left;
      display: grid;
      align-content: space-between;
      gap: 14px;
      border-color: var(--line);
      background: var(--surface-soft);
    }
    .brand-folder:hover {
      border-color: rgba(20, 122, 95, 0.32);
      background: rgba(20, 122, 95, 0.08);
    }
    .brand-folder-name {
      font-size: 15px;
      font-weight: 800;
      overflow-wrap: anywhere;
    }
    .folder-mark {
      margin-right: 6px;
      color: #9b6a12;
    }
    .brand-folder-meta {
      color: var(--muted);
      font-size: 12px;
      font-weight: 400;
      line-height: 1.45;
    }
    .content-empty {
      grid-column: 1 / -1;
      border: 1px dashed var(--line-strong);
      padding: 24px;
      text-align: center;
      color: var(--muted);
    }
    .copy-quick-form {
      display: grid;
      grid-template-columns: minmax(0, 2fr) minmax(180px, 1fr) auto;
      gap: 10px;
      align-items: end;
      margin-top: 12px;
    }
    .copy-quick-form textarea {
      min-height: 72px;
    }
    .icon-button {
      width: 38px;
      padding: 0;
      justify-content: center;
    }
    .is-hidden {
      display: none !important;
    }
    dialog {
      box-sizing: border-box;
      width: min(560px, calc(100% - 32px));
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 16px;
      color: #18212f;
      background: white;
    }
    dialog::backdrop {
      background: rgba(23, 32, 51, 0.28);
    }
    .dialog-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 12px;
    }
    .dialog-actions {
      display: flex;
      justify-content: flex-end;
      gap: 8px;
      margin-top: 14px;
      flex-wrap: wrap;
    }
    .import-result {
      margin-top: 12px;
      padding: 10px;
      border-left: 3px solid #287a63;
      background: var(--surface-soft);
      font-size: 13px;
    }
    .import-result details {
      margin-top: 8px;
    }
    .import-result ul {
      margin: 8px 0 0;
      padding-left: 20px;
    }
    @media (max-width: 780px) {
      .shell { grid-template-columns: 1fr; }
      aside { position: static; }
      .brand-folder-grid,
      .copy-quick-form {
        grid-template-columns: 1fr;
      }
      .content-toolbar {
        align-items: flex-start;
      }
      .proxy-pool-controls {
        grid-template-columns: 1fr;
      }
      .content-metrics {
        grid-template-columns: 1fr;
      }
      .content-metric + .content-metric {
        border-left: 0;
        border-top: 1px solid #d7dee8;
      }
    }
  </style>
  <link rel="stylesheet" href="{{ url_for('static', filename='dashboard_shell.css') }}">
  <link rel="stylesheet" href="{{ url_for('static', filename='selector_probe.css') }}">
  <script src="{{ url_for('static', filename='dashboard_navigation.js') }}"></script>
</head>
<body>
  <div class="dashboard-shell">
    {% include '_dashboard_sidebar.html' %}
    <main class="dashboard-main">
      <div class="topbar">
        <h2 id="title">集中配置</h2>
        <button id="refresh" type="button">刷新状态</button>
      </div>

      <div class="cards">
        <div class="card"><div class="label">Flask 网关</div><div class="value ok" id="status-service">运行中</div></div>
        <div class="card"><div class="label">代理配置</div><div class="value" id="status-proxy">检查中</div></div>
        <div class="card"><div class="label">服务接口</div><div class="value" id="status-services">检查中</div></div>
        <div class="card"><div class="label">浏览器配置</div><div class="value" id="status-browser">检查中</div></div>
      </div>

      <section class="panel active" id="panel-settings">
        <form id="settings-form">
          <div class="settings-sections">
            <section class="settings-group" data-settings-group="proxy">
              <div class="settings-group-header">
                <h3>代理与 IP 池</h3>
                <span class="muted">统一管理单个代理和批量代理池</span>
              </div>
              <div class="grid">
                <label>代理主机<input name="proxy.host" autocomplete="off"></label>
                <label>代理端口<input name="proxy.port" autocomplete="off"></label>
                <label>代理用户名<input name="proxy.username" autocomplete="off"></label>
                <label>代理密码<input name="proxy.password" type="password" autocomplete="off"></label>
                <label>代理协议<select name="proxy_pool.protocol"><option value="socks5">SOCKS5</option><option value="http">HTTP / HTTPS</option></select></label>
                <label class="wide">批量代理 IP 池<textarea name="proxy_pool.raw" placeholder="203.0.113.10:1080:example_user:example_password"></textarea></label>
              </div>
            </section>
            <section class="settings-group" data-settings-group="services">
              <div class="settings-group-header">
                <h3>服务接口与超时</h3>
                <span class="muted">外部接口地址和请求等待时间</span>
              </div>
              <div class="grid">
                <label>IP 信息接口<input name="services.ipinfo_url" autocomplete="off"></label>
                <label>Buffer GraphQL 接口<input name="services.buffer_graphql_url" autocomplete="off"></label>
                <label>IP 检测超时秒数<input name="timeouts.ip_check_seconds" type="number" min="1" step="1"></label>
                <label>Buffer 超时秒数<input name="timeouts.buffer_publish_seconds" type="number" min="1" step="1"></label>
              </div>
            </section>
            <section class="settings-group" data-settings-group="storage">
              <div class="settings-group-header">
                <h3>R2 存储</h3>
                <span class="muted">视频内容库和公开访问配置</span>
              </div>
              <div class="grid">
                <label>R2 Account Token<input name="r2.account_token" type="password" autocomplete="off"></label>
                <label>R2 Account ID<input name="r2.account_id" autocomplete="off"></label>
                <label>R2 访问密钥 ID<input name="r2.access_key_id" autocomplete="off"></label>
                <label>R2 秘密访问密钥<input name="r2.secret_access_key" type="password" autocomplete="off"></label>
                <label>R2 Bucket<input name="r2.bucket" autocomplete="off"></label>
                <label>R2 接口地址<input name="r2.endpoint_url" autocomplete="off"></label>
                <label>R2 公开访问域名<input name="r2.public_base_url" autocomplete="off"></label>
                <label>R2 文件前缀<input name="r2.prefix" autocomplete="off"></label>
              </div>
            </section>
            <section class="settings-group" data-settings-group="browser">
              <div class="settings-group-header">
                <h3>浏览器接管</h3>
                <span class="muted">AdsPower API 建议填写 http://local.adspower.net:50325；AdsPower 模式下请留空手动 CDP 地址。</span>
              </div>
              <div class="grid">
                <label>手动 CDP 地址（AdsPower 模式下请留空）<input name="browser.cdp_url" autocomplete="off" placeholder="ws://127.0.0.1:9222/devtools/browser/..."></label>
                <label>任务目标<input name="browser.task_goal" autocomplete="off"></label>
                <label>浏览器默认网址<input name="browser.default_url" autocomplete="off" value="https://www.tiktok.com/" placeholder="https://www.tiktok.com/"></label>
              </div>
              <p class="muted">每次执行策略前都会重新打开默认网址并清理旧 Tab，避免在旧页面或 about:blank 上继续运行。</p>
            </section>
            <section class="settings-group" data-settings-group="adspower">
              <div class="settings-group-header">
                <h3>AdsPower 浏览器</h3>
                <span class="muted">本地 API 地址和授权 Key</span>
              </div>
              <div class="grid">
                <label>AdsPower API 地址<input name="adspower.base_url" autocomplete="off" placeholder="http://local.adspower.net:50325"></label>
                <label>AdsPower API Key<input name="adspower.api_key" type="password" autocomplete="off"></label>
                <label>默认分组 ID<input name="adspower.default_group_id" autocomplete="off"></label>
              </div>
            </section>
            <section class="settings-group" data-settings-group="models">
              <div class="settings-group-header">
                <h3>模型接入</h3>
                <span class="muted">当前默认模型和接口凭据</span>
              </div>
              <div class="grid">
                <label>默认模型 ID<input name="models.default_model_id" autocomplete="off"></label>
                <label>模型 ID<input name="models.items.0.id" autocomplete="off"></label>
                <label>模型供应商<select id="model-provider" name="models.items.0.provider"></select></label>
                <label>预设模型<select id="model-preset-model"></select></label>
                <label>模型接口地址<input name="models.items.0.base_url" autocomplete="off"></label>
                <label>模型 API Key<input name="models.items.0.api_key" type="password" autocomplete="off"></label>
                <label id="model-custom-name-field" hidden>手工模型名称<input id="model-custom-name" name="models.items.0.model" autocomplete="off"></label>
                <label>调用模式<select name="models.items.0.mode"><option value="responses">Responses</option><option value="chat">Chat Completions</option></select></label>
                <label>启用模型<select name="models.items.0.enabled"><option value="true">启用</option><option value="false">停用</option></select></label>
              </div>
              <div class="actions">
                <button id="model-presets-refresh" type="button">刷新模型预设</button>
                <span id="model-presets-status" class="muted"></span>
              </div>
            </section>
            <section class="settings-group" data-settings-group="publishing">
              <div class="settings-group-header">
                <h3>发布队列</h3>
                <span class="muted">后台调度节奏</span>
              </div>
              <div class="grid">
                <label>发布队列间隔秒数<input name="publish_queue.interval_seconds" type="number" min="1" step="1"></label>
                <label>启用自动采样<select name="publish_sampling.enabled"><option value="true">启用</option><option value="false">停用</option></select></label>
                <label>自动采样间隔秒数<input name="publish_sampling.interval_seconds" type="number" min="30" step="1"></label>
                <label>采样等待小时数<input name="publish_sampling.min_age_hours" type="number" min="0" step="1"></label>
              </div>
            </section>
          </div>
          <div class="cards pool-stats">
            <div class="card"><div class="label">IP池总数</div><div class="value" id="proxy-pool-total">0</div></div>
            <div class="card"><div class="label">已分配</div><div class="value" id="proxy-pool-assigned">0</div></div>
            <div class="card"><div class="label">剩余</div><div class="value" id="proxy-pool-remaining">0</div></div>
          </div>
          <div class="actions">
            <button class="primary" id="settings-save" type="submit">保存配置</button>
            <button id="settings-restore-latest" type="button" hidden>恢复最近备份</button>
            <button id="proxy-pool-open" type="button">查看代理 IP 池</button>
            <a class="button" href="/settings" target="_blank">高级配置页</a>
            <span id="settings-health-status" class="muted"></span>
            <span id="settings-output"></span>
          </div>
        </form>
      </section>

      <section class="panel" id="panel-accounts">
        <div class="grid">
          <input id="buffer-manual-account-id" type="hidden">
          <label>Buffer 账号名<input id="buffer-manual-account-name" autocomplete="off"></label>
          <label>Buffer Token<input id="buffer-manual-token" type="password" autocomplete="off" placeholder="编辑时留空表示不修改"></label>
          <label>Buffer API<input id="buffer-manual-api" autocomplete="off" placeholder="https://api.buffer.com"></label>
          <label class="wide">导入 Buffer 账号
            <textarea id="buffer-import-text" placeholder="account_name,buffer_token,buffer_api&#10;Brand One,token_xxx,https://api.buffer.com"></textarea>
          </label>
          <label>表格导入<input id="buffer-import-file" type="file" accept=".csv,.tsv,.txt"></label>
        </div>
        <div class="actions">
          <button class="primary" id="buffer-submit-account" type="button">提交账号</button>
          <button id="buffer-cancel-edit" type="button">取消编辑</button>
          <button id="accounts-sync-selected" type="button">同步选中</button>
          <button id="accounts-sync-all" type="button">同步全部</button>
          <span id="accounts-save-status" class="muted"></span>
        </div>
        <div class="cards pool-stats">
          <div class="card"><div class="label">Buffer 账号</div><div class="value" id="accounts-total">0</div></div>
          <div class="card"><div class="label">可用账号</div><div class="value" id="accounts-available">0</div></div>
          <div class="card"><div class="label">TikTok Profile</div><div class="value" id="accounts-profiles">0</div></div>
        </div>
        <h3>可用账号列表</h3>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th><input id="accounts-select-all" type="checkbox" aria-label="全选账号"></th>
                <th>账号</th>
                <th>TikTok Profile IDs</th>
                <th>Token</th>
                <th>Buffer API</th>
                <th>同步状态</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody id="accounts-body"></tbody>
          </table>
        </div>
      </section>

      <section class="panel" id="panel-proxy-config">
        <div class="content-toolbar">
          <div>
            <h3>代理 IP 池</h3>
            <span class="muted">集中配置页只显示统计，这里查看完整 IP 列表</span>
          </div>
          <button id="proxy-pool-refresh" type="button">刷新 IP 池</button>
        </div>
        <div class="proxy-pool-controls">
          <label>搜索 IP / 端口 / 用户名<input id="proxy-pool-search" autocomplete="off"></label>
          <label>每页数量<select id="proxy-pool-page-size"><option value="50">50</option><option value="100">100</option><option value="200">200</option></select></label>
          <div class="table-actions">
            <button id="proxy-pool-prev" type="button">上一页</button>
            <button id="proxy-pool-next" type="button">下一页</button>
          </div>
          <span class="muted" id="proxy-pool-page-meta"></span>
        </div>
        <div class="pool-list" id="proxy-pool-list"></div>
        <div class="actions" style="margin-top:0">
          <button class="primary" id="proxy-refresh-accounts" type="button">刷新账号明细</button>
          <span class="muted">引用账号花名册中已经同步好的 TikTok Profile 明细</span>
        </div>
        <h3>账号代理分配</h3>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>账号</th>
                <th>TikTok Profile IDs</th>
                <th>当前代理</th>
                <th>分配模式</th>
                <th>手动代理</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody id="proxy-assignment-body"></tbody>
          </table>
        </div>
      </section>

      <section class="panel" id="panel-content">
        <div class="content-section content-video-section">
          <div class="content-toolbar">
            <div>
              <h3>视频内容库</h3>
              <span id="content-video-status" class="muted"></span>
            </div>
            <div class="compact-actions">
              <button id="content-refresh-videos" type="button">刷新数量</button>
              <button class="primary" id="content-sync-videos" type="button">同步 R2</button>
            </div>
          </div>
          <div class="content-metrics">
            <div class="content-metric"><div class="label">视频总数</div><div class="value" id="video-total">0</div></div>
            <div class="content-metric"><div class="label">可用视频</div><div class="value" id="video-available">0</div></div>
            <div class="content-metric"><div class="label">已使用</div><div class="value" id="video-used">0</div></div>
          </div>
        </div>

        <div class="content-section" id="content-brand-overview">
          <div class="content-toolbar">
            <div>
              <h3>品牌文案库</h3>
              <span class="muted" id="content-brand-total">0 个品牌</span>
            </div>
            <div class="compact-actions">
              <button id="content-create-brand" type="button">+ 新建品牌</button>
              <button class="primary" id="content-open-import" type="button">↑ 导入表格</button>
            </div>
          </div>
          <div class="brand-folder-grid" id="content-brand-grid"></div>
        </div>

        <div class="content-section is-hidden" id="content-brand-detail">
          <div class="content-toolbar">
            <div class="content-heading">
              <button class="icon-button" id="content-back-brands" type="button" aria-label="返回品牌列表">←</button>
              <div>
                <h3 id="content-current-brand-name">品牌文案</h3>
                <span class="muted" id="content-copy-status"></span>
              </div>
            </div>
            <button id="content-rename-brand" type="button">✎ 重命名</button>
          </div>
          <div class="copy-quick-form">
            <label>正文<textarea id="content-copy-body" placeholder="输入该品牌要发布的正文"></textarea></label>
            <label>Tag<input id="content-copy-tags" autocomplete="off" placeholder="#tag1 #tag2"></label>
            <button class="primary" id="content-add-copy" type="button">添加文案</button>
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>正文</th><th>Tag</th><th>创建时间</th></tr></thead>
              <tbody id="content-copy-body-list"></tbody>
            </table>
          </div>
        </div>

        <dialog id="content-import-dialog">
          <form id="content-import-form">
            <div class="dialog-header">
              <h3>导入品牌文案</h3>
              <button class="icon-button" id="content-close-import" type="button" aria-label="关闭">×</button>
            </div>
            <label>表格文件<input id="content-import-file" name="file" type="file" accept=".xlsx,.csv,.tsv" required></label>
            <div class="import-result is-hidden" id="content-import-result"></div>
            <div class="dialog-actions">
              <button id="content-cancel-import" type="button">取消</button>
              <button class="primary" id="content-submit-import" type="submit">提交导入</button>
            </div>
          </form>
        </dialog>

        <dialog id="content-brand-dialog">
          <form id="content-brand-form">
            <div class="dialog-header">
              <h3>新建品牌</h3>
              <button class="icon-button js-close-brand-dialog" type="button" aria-label="关闭">×</button>
            </div>
            <label>品牌名称<input id="content-brand-name" autocomplete="off" required></label>
            <span class="bad" id="content-brand-status"></span>
            <div class="dialog-actions">
              <button class="js-close-brand-dialog" type="button">取消</button>
              <button class="primary" type="submit">创建品牌</button>
            </div>
          </form>
        </dialog>

        <dialog id="content-rename-dialog">
          <form id="content-rename-form">
            <div class="dialog-header">
              <h3>重命名品牌</h3>
              <button class="icon-button js-close-rename-dialog" type="button" aria-label="关闭">×</button>
            </div>
            <label>品牌名称<input id="content-rename-name" autocomplete="off" required></label>
            <span class="bad" id="content-rename-status"></span>
            <div class="dialog-actions">
              <button class="js-close-rename-dialog" type="button">取消</button>
              <button class="primary" type="submit">保存名称</button>
            </div>
          </form>
        </dialog>
      </section>

      <section class="panel" id="panel-publish">
        <h3>手动测试发布</h3>
        <div class="grid">
          <label>账号<select id="publish-account-id"></select></label>
          <label>TikTok Profile ID<select id="publish-profile-id"></select></label>
          <label>视频<select id="publish-video-id"></select></label>
          <label>品牌<select id="publish-brand-id"></select></label>
          <label>文案<select id="publish-copy-id"></select></label>
          <label>发布日期<input id="publish-manual-date" type="date"></label>
          <label>发布时间<input id="publish-manual-time" type="time"></label>
        </div>
        <div class="actions">
          <button class="primary" id="publish-manual-test" type="button">手动测试</button>
          <span id="publish-queue-status" class="muted"></span>
        </div>

        <h3>批量创建发布</h3>
        <div class="actions">
          <button id="publish-batch-create" type="button">批量创建</button>
          <button id="publish-refresh-batches" type="button">刷新批量任务</button>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>创建时间</th>
                <th>发布时间</th>
                <th>账号数</th>
                <th>创建</th>
                <th>跳过</th>
                <th>品牌</th>
                <th>状态</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody id="publish-batch-runs-body"></tbody>
          </table>
        </div>

        <h3>每日定时发布</h3>
        <div class="actions">
          <button id="publish-save-daily-schedule" type="button">保存每日定时发布</button>
          <button id="publish-refresh-daily-schedules" type="button">刷新定时任务</button>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>起始日期</th>
                <th>每日时间</th>
                <th>账号数</th>
                <th>品牌</th>
                <th>启用</th>
                <th>更新时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody id="publish-daily-schedules-body"></tbody>
          </table>
        </div>

        <dialog id="publish-batch-dialog">
          <form id="publish-batch-form">
            <div class="dialog-header">
              <h3>批量创建发布任务</h3>
              <button class="icon-button" id="publish-batch-close" type="button" aria-label="关闭">×</button>
            </div>
            <div class="grid">
              <label>发布日期<input id="publish-batch-date" type="date" required></label>
              <label>发布时间<input id="publish-batch-time" type="time" required></label>
            </div>
            <div class="content-toolbar">
              <span class="muted">选择需要发布的账号</span>
              <label class="inline-control"><input id="publish-batch-select-all" type="checkbox"> 全选</label>
            </div>
            <div class="table-wrap batch-account-box">
              <table>
                <thead><tr><th>选择</th><th>账号</th><th>TikTok Profile</th></tr></thead>
                <tbody id="publish-batch-account-list"></tbody>
              </table>
            </div>
            <div class="dialog-actions">
              <button id="publish-batch-cancel" type="button">取消</button>
              <button class="primary" type="submit">创建任务</button>
            </div>
          </form>
        </dialog>

        <dialog id="publish-daily-dialog">
          <form id="publish-daily-form">
            <div class="dialog-header">
              <h3>每日定时发布</h3>
              <button class="icon-button" id="publish-daily-close" type="button" aria-label="关闭">×</button>
            </div>
            <div class="grid">
              <label>起始日期<input id="publish-daily-start-date" type="date" required></label>
              <label>每日时间<input id="publish-daily-time-input" type="time" required></label>
            </div>
            <div class="content-toolbar">
              <span class="muted">选择参与每日发布的账号</span>
              <label class="inline-control"><input id="publish-daily-select-all" type="checkbox"> 全选</label>
            </div>
            <div class="table-wrap batch-account-box">
              <table>
                <thead><tr><th>选择</th><th>账号</th><th>TikTok Profile</th></tr></thead>
                <tbody id="publish-daily-account-list"></tbody>
              </table>
            </div>
            <div class="dialog-actions">
              <button id="publish-daily-cancel" type="button">取消</button>
              <button class="primary" type="submit">保存定时任务</button>
            </div>
          </form>
        </dialog>
      </section>

      <section class="panel" id="panel-publish-results">
        <h3>发布结果管理</h3>
        <div class="grid">
          <label>生成日期<input id="publish-filter-date" autocomplete="off" placeholder="2026-07-10"></label>
          <label>状态<select id="publish-filter-status"><option value="">全部</option><option value="success">成功</option><option value="failed">失败</option><option value="pending">待发布</option></select></label>
          <label>清理早于日期<input id="publish-cleanup-date" autocomplete="off" placeholder="2026-07-01"></label>
        </div>
        <div class="actions">
          <button id="publish-refresh-results" type="button">刷新结果</button>
          <button id="publish-cleanup-logs" type="button">清理旧日志</button>
          <span id="publish-results-status" class="muted"></span>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>账号</th>
                <th>TikTok Profile ID</th>
                <th>预计发布时间</th>
                <th>代理IP</th>
                <th>文案</th>
                <th>状态</th>
                <th>TikTok链接/失败原因</th>
              </tr>
            </thead>
            <tbody id="publish-results-body"></tbody>
          </table>
        </div>
      </section>

      <section class="panel" id="panel-browser">
        <div class="grid">
          <p class="muted wide">默认网址请在“集中配置 → 浏览器接管”中设置。</p>
        </div>
        <div class="actions">
          <button class="secondary" id="adspower-refresh-windows" type="button">读取 AdsPower 窗口</button>
          <button class="primary" id="adspower-open-tile" type="button">打开窗口</button>
          <select id="browser-execute-strategy"><option value="">选择执行策略</option></select>
          <button class="secondary" id="browser-execute-strategy-button" type="button">执行选中策略</button>
          <span id="browser-operation-status" class="muted"></span>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>选择</th><th>窗口编号</th><th>Profile ID</th><th>名称</th><th>分组</th><th>账号</th></tr></thead>
            <tbody id="adspower-window-list"></tbody>
          </table>
        </div>
      </section>

      {% include '_selector_probe_console.html' %}
    </main>
  </div>

  <script src="/static/selector_inventory_ui.js"></script>
  <script src="/static/selector_probe_ui.js"></script>
  <script>
    const panels = [...document.querySelectorAll(".panel")];
    const dashboardNavLinks = [...document.querySelectorAll(".dashboard-nav-link[data-panel]")];
    const title = document.querySelector("#title");
    const numberFields = new Set(["timeouts.ip_check_seconds", "timeouts.buffer_publish_seconds", "publish_queue.interval_seconds", "publish_sampling.interval_seconds", "publish_sampling.min_age_hours"]);
    const booleanFields = new Set(["publish_sampling.enabled", "models.items.0.enabled"]);
    let modelPresets = {};
    const state = {
      accounts: [],
      videos: [],
      brands: [],
      contentCopyItems: [],
      publishCopyItems: [],
      activeBrandId: "",
      editingBatchRunId: "",
      editingDailyScheduleId: "",
      proxyPoolPage: 1,
      proxyPoolPageCount: 1,
    };
    let currentSettings = {};
    let settingsLoaded = false;

    const dashboardNavigation = window.DashboardNavigation.createDashboardNavigation({
      window,
      panels,
      links: dashboardNavLinks,
      title,
    });

    function showPanel(name) {
      return dashboardNavigation.showPanel(name);
    }

    function render(target, value) {
      const element = document.querySelector(target);
      if (element) element.textContent = JSON.stringify(value, null, 2);
    }

    function getNested(target, path) {
      return path.split(".").reduce((current, part) => current?.[part], target);
    }

    function setNested(target, path, value) {
      const parts = path.split(".");
      let current = target;
      for (let index = 0; index < parts.length - 1; index += 1) {
        const part = parts[index];
        const nextPart = parts[index + 1];
        current[part] ||= /^\d+$/.test(nextPart) ? [] : {};
        current = current[part];
      }
      current[parts.at(-1)] = value;
    }

    function paintStatus(id, enabled) {
      const element = document.querySelector(id);
      element.textContent = enabled ? "就绪" : "待配置";
      element.classList.toggle("ok", enabled);
      element.classList.toggle("warn", !enabled);
    }

    function parseProxyPoolText(raw) {
      return (raw || "")
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter(Boolean)
        .map((line) => {
          const [host = "", port = "", username = ""] = line.split(":");
          return {host, port, username, assigned: false};
        });
    }

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, (char) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      }[char]));
    }

    function renderProxyPoolStatus(summary) {
      document.querySelector("#proxy-pool-total").textContent = summary.total ?? 0;
      document.querySelector("#proxy-pool-assigned").textContent = summary.assigned ?? 0;
      document.querySelector("#proxy-pool-remaining").textContent = summary.remaining ?? 0;
      state.proxyPoolPage = summary.page ?? state.proxyPoolPage;
      state.proxyPoolPageCount = summary.page_count ?? 1;
      const meta = document.querySelector("#proxy-pool-page-meta");
      if (meta) {
        meta.textContent = `第 ${state.proxyPoolPage} / ${state.proxyPoolPageCount} 页，匹配 ${summary.filtered_total ?? summary.total ?? 0} 条`;
      }
      const prev = document.querySelector("#proxy-pool-prev");
      const next = document.querySelector("#proxy-pool-next");
      if (prev) prev.disabled = state.proxyPoolPage <= 1;
      if (next) next.disabled = state.proxyPoolPage >= state.proxyPoolPageCount;
      const list = document.querySelector("#proxy-pool-list");
      if (!list) return;
      const items = summary.items || [];
      list.innerHTML = items.length
        ? items.map((item) => `
          <div class="pool-row">
            <code>${escapeHtml(item.host)}:${escapeHtml(item.port)}:${escapeHtml(item.username)}</code>
            <span class="badge ${item.assigned ? "" : "warn"}">${item.assigned ? "已分配" : "可用"}</span>
          </div>
        `).join("")
        : '<div class="pool-row"><code>暂无可用代理 IP</code><span class="badge warn">空</span></div>';
    }

    function previewProxyPoolFromInput() {
      const items = parseProxyPoolText(document.querySelector('[name="proxy_pool.raw"]').value);
      renderProxyPoolStatus({
        total: items.length,
        assigned: 0,
        remaining: items.length,
        filtered_total: items.length,
        page: 1,
        page_size: 50,
        page_count: Math.max(Math.ceil(items.length / 50), 1),
        items: items.slice(0, 50),
      });
    }

    async function refreshProxyPoolStatus(options = {}) {
      if (options.page) state.proxyPoolPage = options.page;
      const params = new URLSearchParams({
        page: String(state.proxyPoolPage || 1),
        page_size: document.querySelector("#proxy-pool-page-size")?.value || "50",
        search: document.querySelector("#proxy-pool-search")?.value.trim() || "",
      });
      const response = await fetch(`/api/proxy-pool/status?${params.toString()}`);
      if (!response.ok) {
        previewProxyPoolFromInput();
        return;
      }
      renderProxyPoolStatus(await response.json());
    }

    async function refreshStatus() {
      const response = await fetch("/api/status");
      const status = await response.json();
      paintStatus("#status-proxy", status.config.proxy_configured);
      paintStatus("#status-services", status.config.services_configured);
      paintStatus("#status-browser", status.config.browser_configured);
    }

    async function loadSettingsHealth() {
      const response = await fetch("/api/settings/status");
      if (!response.ok) throw new Error("settings status request failed");
      const health = await response.json();
      const saveButton = document.querySelector("#settings-save");
      const restoreButton = document.querySelector("#settings-restore-latest");
      document.querySelector("#settings-health-status").textContent = health.ok
        ? "配置正常"
        : (health.error || "配置文件无法读取");
      saveButton.disabled = !health.ok;
      restoreButton.hidden = health.ok || !health.backup_available;
      return health;
    }

    async function loadSettings() {
      settingsLoaded = false;
      const health = await loadSettingsHealth();
      if (!health.ok) {
        document.querySelector("#settings-output").textContent = "配置无法读取，暂不保存";
        return;
      }
      const response = await fetch("/api/settings");
      if (!response.ok) throw new Error("settings request failed");
      currentSettings = await response.json();
      for (const element of document.querySelector("#settings-form").elements) {
        if (!element.name) continue;
        const value = getNested(currentSettings, element.name);
        element.value = booleanFields.has(element.name) ? String(value ?? true) : (value ?? "");
        if (getNested(currentSettings._secrets_configured || {}, element.name)) {
          element.placeholder = "已配置，留空保持不变";
        }
      }
      await loadModelPresets();
      const browserCdpField = document.querySelector("#browser-cdp-url");
      const browserTaskField = document.querySelector("#browser-task-goal");
      if (browserCdpField) browserCdpField.value = currentSettings.browser?.cdp_url ?? "";
      if (browserTaskField) browserTaskField.value = currentSettings.browser?.task_goal || "随机刷3个视频并点赞";
      await refreshProxyPoolStatus();
      settingsLoaded = true;
    }

    function modelField(name) {
      return document.querySelector(`[name="models.items.0.${name}"]`);
    }

    function renderModelOptions(provider, selectedModel) {
      const preset = modelPresets[provider] || modelPresets.custom;
      const modelSelect = document.querySelector("#model-preset-model");
      const models = preset?.models || [];
      modelSelect.replaceChildren(
        ...models.map((model) => new Option(model, model)),
        new Option("自定义模型", "__custom__"),
      );
      modelSelect.value = models.includes(selectedModel)
        ? selectedModel
        : (models[0] || "__custom__");
    }

    function renderModelProviders(provider, selectedModel) {
      const providerSelect = document.querySelector("#model-provider");
      providerSelect.replaceChildren(
        ...Object.entries(modelPresets).map(([id, preset]) => new Option(preset.label, id)),
      );
      if (provider && !modelPresets[provider]) {
        providerSelect.add(new Option(provider, provider));
      }
      providerSelect.value = provider || "custom";
      renderModelOptions(providerSelect.value, selectedModel);
    }

    function renderModelPresetFallback(model) {
      modelPresets = {};
      const provider = model.provider || "custom";
      const providerLabel = provider === "custom" ? "自定义" : provider;
      const providerSelect = document.querySelector("#model-provider");
      providerSelect.replaceChildren(new Option(providerLabel, provider));
      providerSelect.value = provider;
      renderModelOptions(provider, model.model);
      document.querySelector("#model-custom-name-field").hidden = false;
    }

    function applyModelPreset() {
      const provider = modelField("provider").value;
      const preset = modelPresets[provider];
      const selectedModel = document.querySelector("#model-preset-model").value;
      const isCustomModel = !preset || provider === "custom" || selectedModel === "__custom__";
      const customName = document.querySelector("#model-custom-name");

      if (preset) {
        modelField("base_url").value = preset.base_url;
        modelField("mode").value = preset.mode;
      }
      document.querySelector("#model-custom-name-field").hidden = !isCustomModel;
      if (isCustomModel) {
        customName.value = "";
      } else {
        customName.value = selectedModel;
      }
    }

    async function loadModelPresets() {
      const model = currentSettings.models?.items?.[0] || {};
      const status = document.querySelector("#model-presets-status");
      try {
        const response = await fetch("/api/model-presets");
        if (!response.ok) throw new Error("model presets request failed");
        modelPresets = await response.json();
        renderModelProviders(model.provider, model.model);
        const selectedModel = document.querySelector("#model-preset-model").value;
        document.querySelector("#model-custom-name-field").hidden = !(
          model.provider === "custom" || selectedModel === "__custom__"
        );
        status.textContent = "";
        return true;
      } catch (_error) {
        renderModelPresetFallback(model);
        status.textContent = "模型预设加载失败，已切换手工配置；可刷新重试";
        return false;
      }
    }

    async function restoreLatestSettings() {
      const result = await postJson("/api/settings/restore-latest", {});
      document.querySelector("#settings-output").textContent = result.status === 200
        ? "已恢复最近备份"
        : (result.data.error || "恢复失败");
      if (result.status === 200) await loadSettings();
    }

    async function postJson(url, body) {
      const response = await fetch(url, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body),
      });
      const data = await response.json();
      return {status: response.status, data};
    }

    async function requestJson(url, method, body) {
      const response = await fetch(url, {
        method,
        headers: {"Content-Type": "application/json"},
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      const data = await response.json();
      return {status: response.status, data};
    }

    async function getNextAccount(target) {
      const response = await fetch("/api/account/next");
      const data = await response.json();
      if (data.ads_power_user_id) {
        const accountInput = document.querySelector("#publish-account-id");
        if (accountInput) accountInput.value = data.ads_power_user_id;
      }
    }

    async function refreshAccounts() {
      const response = await fetch("/api/accounts");
      const payload = await response.json();
      state.accounts = payload.accounts || [];
      renderAccounts(payload);
      renderPublishSelectors();
    }

    function shortId(value) {
      const text = String(value || "");
      return text.length > 14 ? `${text.slice(0, 6)}...${text.slice(-5)}` : text;
    }

    function compactDateTime(value) {
      const text = String(value || "").trim();
      if (!text) return "";
      const date = (text.match(/^(\d{4}-\d{2}-\d{2})/) || [])[1] || "";
      const time = (text.match(/T(\d{2}:\d{2})/) || text.match(/\s(\d{2}:\d{2})/) || [])[1] || "";
      return [date, time].filter(Boolean).join(" ") || text;
    }

    function renderAccounts(payload = {}) {
      const profileCount = state.accounts.reduce((total, account) => total + (account.buffer_profile_ids || []).length, 0);
      document.querySelector("#accounts-total").textContent = payload.count ?? state.accounts.length;
      document.querySelector("#accounts-available").textContent = payload.available_count ?? state.accounts.filter((account) => (account.buffer_profile_ids || []).length).length;
      document.querySelector("#accounts-profiles").textContent = profileCount;
      const body = document.querySelector("#accounts-body");
      body.innerHTML = state.accounts.length
        ? state.accounts.map((account) => {
          const profileHtml = (account.buffer_profile_ids || []).length
            ? `<div class="chip-list">${account.buffer_profile_ids.map((id) => `<span class="chip" title="${escapeHtml(id)}">${escapeHtml(shortId(id))}</span>`).join("")}</div>`
            : '<span class="muted">未发现 TikTok profile</span>';
          const syncStatus = account.last_channel_sync_error
            ? `<span class="bad">${escapeHtml(account.last_channel_sync_error)}</span>`
            : (account.last_channel_sync_at ? "已同步" : "待同步");
          return `
            <tr>
              <td><input class="account-select" type="checkbox" value="${escapeHtml(account.id)}" aria-label="选择 ${escapeHtml(account.account_name || account.id)}"></td>
              <td>${escapeHtml(account.account_name || account.buffer_account_id || account.id)}</td>
              <td>${profileHtml}</td>
              <td>${escapeHtml(account.buffer_token || "")}</td>
              <td>${escapeHtml(account.buffer_api || "")}</td>
              <td>${syncStatus}</td>
              <td>
                <button type="button" data-account-id="${escapeHtml(account.id)}" class="js-discover-account">同步</button>
                <button type="button" data-account-id="${escapeHtml(account.id)}" class="js-edit-account">编辑</button>
              </td>
            </tr>
          `;
        }).join("")
        : '<tr><td colspan="6" class="muted">暂无账号</td></tr>';
      renderProxyAssignments();
    }

    function renderProfileChips(account) {
      return (account.buffer_profile_ids || []).length
        ? `<div class="chip-list">${account.buffer_profile_ids.map((id) => `<span class="chip" title="${escapeHtml(id)}">${escapeHtml(shortId(id))}</span>`).join("")}</div>`
        : '<span class="muted">未发现 TikTok profile</span>';
    }

    function renderProxyAssignments() {
      const body = document.querySelector("#proxy-assignment-body");
      body.innerHTML = state.accounts.length
        ? state.accounts.map((account) => `
            <tr>
              <td>${escapeHtml(account.account_name || account.buffer_account_id || account.id)}</td>
              <td>${renderProfileChips(account)}</td>
              <td>${account.proxy_session ? `<code>${escapeHtml(account.proxy_session)}</code>` : '<span class="muted">未分配</span>'}</td>
              <td>
                <select class="proxy-mode" aria-label="代理分配模式">
                  <option value="auto">自动分配</option>
                  <option value="manual">手动代理</option>
                </select>
              </td>
              <td><input class="proxy-manual-value" autocomplete="off" placeholder="203.0.113.8:9000:user:pass"></td>
              <td><button type="button" data-account-id="${escapeHtml(account.id)}" class="js-assign-proxy">保存代理</button></td>
            </tr>
          `).join("")
        : '<tr><td colspan="6" class="muted">请先在账号花名册提交账号并同步绑定账号</td></tr>';
    }

    async function assignProxyForAccount(button) {
      const row = button.closest("tr");
      const mode = row.querySelector(".proxy-mode").value;
      const proxy = row.querySelector(".proxy-manual-value").value.trim();
      await postJson("/api/accounts/proxy", {
        account_id: button.dataset.accountId,
        mode,
        proxy,
      });
      await refreshAccounts();
      await refreshProxyPoolStatus();
    }

    async function refreshContentVideos() {
      const response = await fetch("/api/content/videos");
      const payload = await response.json();
      state.videos = payload.videos || [];
      document.querySelector("#video-total").textContent = payload.video_count ?? 0;
      document.querySelector("#video-available").textContent = payload.available_count ?? 0;
      document.querySelector("#video-used").textContent = payload.used_count ?? 0;
      renderPublishSelectors();
    }

    async function syncContentVideos() {
      const result = await postJson("/api/content/videos/sync", {});
      document.querySelector("#content-video-status").textContent = result.status === 200 ? "已同步" : (result.data.error || "同步失败");
      await refreshContentVideos();
    }

    async function refreshBrands() {
      const publishSelect = document.querySelector("#publish-brand-id");
      const selectedPublishBrand = publishSelect.value;
      const response = await fetch("/api/content/brands");
      const payload = await response.json();
      state.brands = payload.brands || [];
      const options = state.brands.map((brand) => `<option value="${escapeHtml(brand.id)}">${escapeHtml(brand.name)}</option>`).join("");
      publishSelect.innerHTML = options;
      if (state.brands.some((brand) => brand.id === selectedPublishBrand)) {
        publishSelect.value = selectedPublishBrand;
      }
      renderBrandFolders();
      await refreshPublishCopyItems();

      const activeBrand = state.brands.find((brand) => brand.id === state.activeBrandId);
      if (activeBrand) {
        document.querySelector("#content-current-brand-name").textContent = activeBrand.name;
        await refreshContentCopyItems();
      } else if (state.activeBrandId) {
        showBrandOverview();
      }
      renderPublishSelectors();
    }

    function formatContentDate(value) {
      if (!value) return "尚未更新";
      const date = new Date(value);
      return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      });
    }

    function renderBrandFolders() {
      const grid = document.querySelector("#content-brand-grid");
      document.querySelector("#content-brand-total").textContent = `${state.brands.length} 个品牌`;
      grid.innerHTML = state.brands.length
        ? state.brands.map((brand) => `
          <button class="brand-folder js-open-brand" type="button" data-brand-id="${escapeHtml(brand.id)}">
            <span class="brand-folder-name"><span class="folder-mark" aria-hidden="true">▰</span>${escapeHtml(brand.name)}</span>
            <span class="brand-folder-meta">${brand.copy_count || 0} 条文案<br>${escapeHtml(formatContentDate(brand.updated_at))}</span>
          </button>
        `).join("")
        : '<div class="content-empty">暂无品牌</div>';
    }

    function showBrandOverview() {
      state.activeBrandId = "";
      state.contentCopyItems = [];
      document.querySelector("#content-brand-overview").classList.remove("is-hidden");
      document.querySelector("#content-brand-detail").classList.add("is-hidden");
    }

    async function openBrandFolder(brandId) {
      const brand = state.brands.find((item) => item.id === brandId);
      if (!brand) return;
      state.activeBrandId = brandId;
      document.querySelector("#content-brand-overview").classList.add("is-hidden");
      document.querySelector("#content-brand-detail").classList.remove("is-hidden");
      document.querySelector("#content-current-brand-name").textContent = brand.name;
      document.querySelector("#content-copy-status").textContent = "";
      await refreshContentCopyItems();
    }

    async function fetchCopyItems(brandId) {
      if (!brandId) return [];
      const response = await fetch(`/api/content/brands/${encodeURIComponent(brandId)}/copy`);
      const payload = await response.json();
      return payload.items || [];
    }

    async function refreshContentCopyItems() {
      state.contentCopyItems = await fetchCopyItems(state.activeBrandId);
      renderCopyTable();
    }

    async function refreshPublishCopyItems() {
      state.publishCopyItems = await fetchCopyItems(document.querySelector("#publish-brand-id").value);
      renderPublishSelectors();
    }

    async function createContentBrand(event) {
      event.preventDefault();
      const brand = document.querySelector("#content-brand-name").value.trim();
      const result = await postJson("/api/content/brands", {brand});
      const status = document.querySelector("#content-brand-status");
      if (result.status !== 200) {
        status.textContent = result.data.error || "创建失败";
        return;
      }
      status.textContent = "";
      document.querySelector("#content-brand-name").value = "";
      document.querySelector("#content-brand-dialog").close();
      await refreshBrands();
    }

    async function addContentCopy() {
      const brandId = state.activeBrandId;
      const body = document.querySelector("#content-copy-body").value.trim();
      const tags = document.querySelector("#content-copy-tags").value.trim();
      const result = await postJson("/api/content/copy", {brand_id: brandId, body, tags});
      const status = document.querySelector("#content-copy-status");
      if (result.status !== 200) {
        status.textContent = result.data.error || "添加失败";
        return;
      }
      status.textContent = "文案已添加";
      document.querySelector("#content-copy-body").value = "";
      document.querySelector("#content-copy-tags").value = "";
      await refreshBrands();
    }

    function renderCopyTable() {
      const body = document.querySelector("#content-copy-body-list");
      body.innerHTML = state.contentCopyItems.length
        ? state.contentCopyItems.map((item) => `
          <tr>
            <td>${escapeHtml(item.body)}</td>
            <td>${escapeHtml((item.tags || []).join(" "))}</td>
            <td>${escapeHtml(item.created_at || "")}</td>
          </tr>
        `).join("")
        : '<tr><td colspan="3" class="muted">暂无文案</td></tr>';
    }

    function openImportDialog() {
      document.querySelector("#content-import-form").reset();
      document.querySelector("#content-import-result").classList.add("is-hidden");
      document.querySelector("#content-import-result").innerHTML = "";
      document.querySelector("#content-import-dialog").showModal();
    }

    function closeImportDialog() {
      document.querySelector("#content-import-dialog").close();
    }

    async function submitCopyImport(event) {
      event.preventDefault();
      const file = document.querySelector("#content-import-file").files[0];
      if (!file) return;
      const submit = document.querySelector("#content-submit-import");
      const result = document.querySelector("#content-import-result");
      const formData = new FormData();
      formData.append("file", file);
      submit.disabled = true;
      submit.textContent = "正在导入";

      try {
        const response = await fetch("/api/content/copy/import", {
          method: "POST",
          body: formData,
        });
        const payload = await response.json();
        result.classList.remove("is-hidden");
        if (!response.ok) {
          result.innerHTML = `<span class="bad">${escapeHtml(payload.error || "导入失败")}</span>`;
          return;
        }
        const errorRows = (payload.errors || []).map((item) =>
          `<li>第 ${item.row} 行：${escapeHtml(item.error)}</li>`
        ).join("");
        result.innerHTML = `
          <div>新增 ${payload.created} 条，跳过重复 ${payload.duplicates} 条，失败 ${payload.failed} 条，新建品牌 ${payload.brands_created} 个</div>
          ${errorRows ? `<details><summary>查看失败明细</summary><ul>${errorRows}</ul></details>` : ""}
        `;
        await refreshBrands();
      } finally {
        submit.disabled = false;
        submit.textContent = "提交导入";
      }
    }

    function openRenameDialog() {
      const brand = state.brands.find((item) => item.id === state.activeBrandId);
      document.querySelector("#content-rename-name").value = brand?.name || "";
      document.querySelector("#content-rename-status").textContent = "";
      document.querySelector("#content-rename-dialog").showModal();
    }

    async function renameActiveBrand(event) {
      event.preventDefault();
      const name = document.querySelector("#content-rename-name").value.trim();
      const response = await fetch(`/api/content/brands/${encodeURIComponent(state.activeBrandId)}`, {
        method: "PATCH",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({name}),
      });
      const payload = await response.json();
      if (!response.ok) {
        document.querySelector("#content-rename-status").textContent = payload.error || "重命名失败";
        return;
      }
      document.querySelector("#content-rename-dialog").close();
      await refreshBrands();
    }

    function renderPublishSelectors() {
      const accountSelect = document.querySelector("#publish-account-id");
      const selectedAccountId = accountSelect.value;
      accountSelect.innerHTML = state.accounts.map((account) => {
        const id = account.ads_power_user_id || account.id;
        return `<option value="${escapeHtml(id)}">${escapeHtml(account.account_name || id)}</option>`;
      }).join("");
      if (selectedAccountId) accountSelect.value = selectedAccountId;

      const account = state.accounts.find((item) => (item.ads_power_user_id || item.id) === accountSelect.value) || state.accounts[0] || {};
      document.querySelector("#publish-profile-id").innerHTML = (account.buffer_profile_ids || []).map((id) => `<option value="${escapeHtml(id)}">${escapeHtml(id)}</option>`).join("");
      document.querySelector("#publish-video-id").innerHTML = state.videos.filter((video) => !video.used).map((video) => `<option value="${escapeHtml(video.id)}">${escapeHtml(video.key || video.id)}</option>`).join("");
      document.querySelector("#publish-copy-id").innerHTML = state.publishCopyItems.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(shortId(item.body || item.id))}</option>`).join("");
      renderBatchAccountList();
      renderDailyAccountList();
    }

    function combinedDateTime(dateSelector, timeSelector) {
      const date = document.querySelector(dateSelector).value;
      const time = document.querySelector(timeSelector).value;
      return date && time ? `${date}T${time}:00+08:00` : "";
    }

    function splitScheduledAt(value) {
      const match = String(value || "").match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/);
      return match ? {date: match[1], time: match[2]} : {date: "", time: ""};
    }

    function checkedValues(className, values) {
      const selected = new Set(values || []);
      document.querySelectorAll(`.${className}`).forEach((input) => {
        input.checked = selected.has(input.value);
      });
    }

    async function manualPublishTest() {
      const result = await postJson("/api/publish/queue/manual-test", {
        account_id: document.querySelector("#publish-account-id").value,
        profile_id: document.querySelector("#publish-profile-id").value,
        video_id: document.querySelector("#publish-video-id").value,
        brand_id: document.querySelector("#publish-brand-id").value,
        copy_id: document.querySelector("#publish-copy-id").value,
        scheduled_at: combinedDateTime("#publish-manual-date", "#publish-manual-time"),
      });
      document.querySelector("#publish-queue-status").textContent = result.status === 200 ? "手动测试已创建" : (result.data.error || "创建失败");
      await refreshContentVideos();
      await refreshPublishResults();
    }

    function openBatchPublishDialog() {
      state.editingBatchRunId = "";
      document.querySelector("#publish-batch-form").reset();
      renderBatchAccountList();
      document.querySelector("#publish-batch-dialog").showModal();
    }

    function closeBatchPublishDialog() {
      document.querySelector("#publish-batch-dialog").close();
    }

    function renderBatchAccountList() {
      renderPublishAccountChecks("#publish-batch-account-list", "publish-batch-account");
    }

    function renderDailyAccountList() {
      renderPublishAccountChecks("#publish-daily-account-list", "publish-daily-account");
    }

    function renderPublishAccountChecks(selector, className) {
      const body = document.querySelector(selector);
      if (!body) return;
      const accounts = state.accounts.filter((account) => (account.buffer_profile_ids || []).length);
      body.innerHTML = accounts.length
        ? accounts.map((account) => {
          const id = account.ads_power_user_id || account.id;
          const profiles = (account.buffer_profile_ids || []).join(", ");
          return `
            <tr>
              <td><input class="${className}" type="checkbox" value="${escapeHtml(id)}"></td>
              <td>${escapeHtml(account.account_name || id)}</td>
              <td>${escapeHtml(profiles)}</td>
            </tr>
          `;
        }).join("")
        : '<tr><td colspan="3" class="muted">暂无可用账号</td></tr>';
    }

    async function submitBatchPublish(event) {
      event.preventDefault();
      const accountIds = [...document.querySelectorAll(".publish-batch-account:checked")].map((input) => input.value);
      const payload = {
        account_ids: accountIds,
        brand_id: document.querySelector("#publish-brand-id").value,
        scheduled_at: combinedDateTime("#publish-batch-date", "#publish-batch-time"),
      };
      const result = state.editingBatchRunId
        ? await requestJson(`/api/publish/queue/batches/${encodeURIComponent(state.editingBatchRunId)}`, "PATCH", payload)
        : await postJson("/api/publish/queue/batch", payload);
      const data = result.data || {};
      document.querySelector("#publish-queue-status").textContent = result.status === 200
        ? (state.editingBatchRunId ? "批量任务已更新" : `已创建 ${data.created ?? 0} 条任务，跳过 ${data.skipped ?? 0} 条`)
        : (data.error || "创建失败");
      if (result.status === 200) {
        state.editingBatchRunId = "";
        closeBatchPublishDialog();
      }
      await refreshContentVideos();
      await refreshPublishResults();
      await refreshBatchRuns();
    }

    function openDailyScheduleDialog() {
      state.editingDailyScheduleId = "";
      document.querySelector("#publish-daily-form").reset();
      renderDailyAccountList();
      document.querySelector("#publish-daily-dialog").showModal();
    }

    function closeDailyScheduleDialog() {
      document.querySelector("#publish-daily-dialog").close();
    }

    async function submitDailySchedule(event) {
      event.preventDefault();
      const accountIds = [...document.querySelectorAll(".publish-daily-account:checked")].map((input) => input.value);
      const payload = {
        enabled: true,
        start_date: document.querySelector("#publish-daily-start-date").value,
        time: document.querySelector("#publish-daily-time-input").value,
        brand_id: document.querySelector("#publish-brand-id").value,
        account_ids: accountIds,
      };
      const result = state.editingDailyScheduleId
        ? await requestJson(`/api/publish/schedule/daily/${encodeURIComponent(state.editingDailyScheduleId)}`, "PATCH", payload)
        : await postJson("/api/publish/schedule/daily", payload);
      document.querySelector("#publish-queue-status").textContent = result.status === 200
        ? (state.editingDailyScheduleId ? "定时任务已更新" : "每日定时发布已保存")
        : "保存失败";
      if (result.status === 200) {
        state.editingDailyScheduleId = "";
        closeDailyScheduleDialog();
      }
      await refreshDailySchedules();
    }

    async function refreshBatchRuns() {
      const response = await fetch("/api/publish/queue/batches", {method: "GET"});
      const payload = await response.json();
      const body = document.querySelector("#publish-batch-runs-body");
      body.innerHTML = (payload.runs || []).length
        ? payload.runs.map((run) => `
          <tr>
            <td>${escapeHtml(compactDateTime(run.created_at))}</td>
            <td>${escapeHtml(compactDateTime(run.scheduled_at))}</td>
            <td>${escapeHtml((run.account_ids || []).length)}</td>
            <td>${escapeHtml(run.created ?? 0)}</td>
            <td>${escapeHtml(run.skipped ?? 0)}</td>
            <td>${escapeHtml(run.brand_id || "")}</td>
            <td>${escapeHtml(run.status || "")}</td>
            <td class="table-actions">
              <button class="js-edit-batch-run" type="button" data-run='${escapeHtml(JSON.stringify(run))}'>编辑</button>
              <button class="js-delete-batch-run" type="button" data-run-id="${escapeHtml(run.id || "")}">删除</button>
            </td>
          </tr>
        `).join("")
        : '<tr><td colspan="8" class="muted">暂无批量任务</td></tr>';
    }

    async function refreshDailySchedules() {
      const response = await fetch("/api/publish/schedule/daily", {method: "GET"});
      const payload = await response.json();
      const body = document.querySelector("#publish-daily-schedules-body");
      body.innerHTML = (payload.schedules || []).length
        ? payload.schedules.map((schedule) => `
          <tr>
            <td>${escapeHtml(schedule.start_date || "")}</td>
            <td>${escapeHtml(schedule.time || "")}</td>
            <td>${escapeHtml(schedule.account_count ?? (schedule.account_ids || []).length)}</td>
            <td>${escapeHtml(schedule.brand_id || "")}</td>
            <td>${schedule.enabled ? "启用" : "停用"}</td>
            <td>${escapeHtml(compactDateTime(schedule.updated_at))}</td>
            <td class="table-actions">
              <button class="js-edit-daily-schedule" type="button" data-schedule='${escapeHtml(JSON.stringify(schedule))}'>编辑</button>
              <button class="js-delete-daily-schedule" type="button" data-schedule-id="${escapeHtml(schedule.id || "")}">删除</button>
            </td>
          </tr>
        `).join("")
        : '<tr><td colspan="7" class="muted">暂无定时任务</td></tr>';
    }

    function editBatchRun(run) {
      state.editingBatchRunId = run.id || "";
      renderBatchAccountList();
      const parts = splitScheduledAt(run.scheduled_at);
      document.querySelector("#publish-batch-date").value = parts.date;
      document.querySelector("#publish-batch-time").value = parts.time;
      if (run.brand_id) document.querySelector("#publish-brand-id").value = run.brand_id;
      checkedValues("publish-batch-account", run.account_ids || []);
      document.querySelector("#publish-batch-dialog").showModal();
    }

    async function deleteBatchRun(runId) {
      const result = await requestJson(`/api/publish/queue/batches/${encodeURIComponent(runId)}`, "DELETE");
      document.querySelector("#publish-queue-status").textContent = result.status === 200 ? "批量任务已删除" : (result.data.error || "删除失败");
      await refreshBatchRuns();
    }

    function editDailySchedule(schedule) {
      state.editingDailyScheduleId = schedule.id || "";
      renderDailyAccountList();
      document.querySelector("#publish-daily-start-date").value = schedule.start_date || "";
      document.querySelector("#publish-daily-time-input").value = schedule.time || "";
      if (schedule.brand_id) document.querySelector("#publish-brand-id").value = schedule.brand_id;
      checkedValues("publish-daily-account", schedule.account_ids || []);
      document.querySelector("#publish-daily-dialog").showModal();
    }

    async function deleteDailySchedule(scheduleId) {
      const result = await requestJson(`/api/publish/schedule/daily/${encodeURIComponent(scheduleId)}`, "DELETE");
      document.querySelector("#publish-queue-status").textContent = result.status === 200 ? "定时任务已删除" : (result.data.error || "删除失败");
      await refreshDailySchedules();
    }

    function publishFilterQuery() {
      const params = new URLSearchParams();
      const date = document.querySelector("#publish-filter-date").value.trim();
      const status = document.querySelector("#publish-filter-status").value;
      if (date) params.set("date", date);
      if (status) params.set("status", status);
      return params.toString();
    }

    async function refreshPublishResults() {
      const query = publishFilterQuery();
      const response = await fetch(`/api/publish/results${query ? `?${query}` : ""}`);
      const payload = await response.json();
      const body = document.querySelector("#publish-results-body");
      body.innerHTML = (payload.tasks || []).length
        ? payload.tasks.map((task) => `
          <tr>
            <td>${escapeHtml(task.account_id || "")}</td>
            <td>${escapeHtml(task.profile_id || "")}</td>
            <td>${escapeHtml(compactDateTime(task.scheduled_at))}</td>
            <td>${escapeHtml(task.proxy_display || "")}</td>
            <td>${escapeHtml(task.copy_text || "")}</td>
            <td>${escapeHtml(task.status || "")}</td>
            <td>${task.tiktok_url ? `<a href="${escapeHtml(task.tiktok_url)}" target="_blank">${escapeHtml(shortId(task.tiktok_url))}</a>` : escapeHtml(task.error || "")}</td>
          </tr>
        `).join("")
        : '<tr><td colspan="7" class="muted">暂无发布结果</td></tr>';
    }

    async function cleanupPublishLogs() {
      const beforeDate = document.querySelector("#publish-cleanup-date").value.trim();
      const result = await postJson("/api/publish/logs/cleanup", {before_date: beforeDate});
      document.querySelector("#publish-results-status").textContent = `已清理 ${result.data.deleted ?? 0} 条旧日志`;
      await refreshPublishResults();
    }

    function preserveLoadedModelItems(settings, loadedSettings) {
      const editedItems = settings.models?.items;
      const loadedItems = loadedSettings.models?.items;
      if (!Array.isArray(editedItems) || !editedItems.length || !Array.isArray(loadedItems)) {
        return settings;
      }
      settings.models.items = [
        editedItems[0],
        ...loadedItems.slice(1).map((item) => ({...item})),
      ];
      return settings;
    }

    async function saveSettings(event) {
      event.preventDefault();
      if (!settingsLoaded) {
        document.querySelector("#settings-output").textContent = "配置尚未加载成功，暂未保存";
        return;
      }
      const health = await loadSettingsHealth();
      if (!health.ok) {
        settingsLoaded = false;
        document.querySelector("#settings-output").textContent = "配置无法读取，暂不保存";
        return;
      }
      const settings = {};
      for (const element of event.currentTarget.elements) {
        if (!element.name) continue;
        const value = booleanFields.has(element.name)
          ? element.value === "true"
          : (numberFields.has(element.name) ? Number(element.value) : element.value);
        setNested(settings, element.name, value);
      }
      preserveLoadedModelItems(settings, currentSettings);
      const response = await fetch("/api/settings", {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(settings),
      });
      document.querySelector("#settings-output").textContent = response.ok ? "已保存" : "保存失败";
      await loadSettings();
      await refreshStatus();
      await refreshProxyPoolStatus();
    }

    async function submitBufferAccount() {
      const accountId = document.querySelector("#buffer-manual-account-id").value.trim();
      const accountName = document.querySelector("#buffer-manual-account-name").value.trim();
      const bufferToken = document.querySelector("#buffer-manual-token").value.trim();
      const bufferApi = document.querySelector("#buffer-manual-api").value.trim();
      const rawText = document.querySelector("#buffer-import-text").value;
      if (rawText.trim()) {
        const result = await postJson("/api/accounts/import", {raw_text: rawText});
        if (result.status >= 400) {
          document.querySelector("#accounts-save-status").textContent = result.data?.error || "保存失败";
          return;
        }
      } else {
        const result = await postJson("/api/accounts/save", {
          id: accountId,
          account_name: accountName,
          buffer_token: bufferToken,
          buffer_api: bufferApi,
        });
        if (result.status >= 400) {
          document.querySelector("#accounts-save-status").textContent = result.data?.error || "保存失败";
          return;
        }
      }
      document.querySelector("#buffer-manual-account-id").value = "";
      document.querySelector("#buffer-manual-token").value = "";
      document.querySelector("#buffer-import-text").value = "";
      document.querySelector("#buffer-submit-account").textContent = "提交账号";
      document.querySelector("#accounts-save-status").textContent = "已保存";
      await refreshProxyPoolStatus();
      await refreshAccounts();
    }

    function editBufferAccount(accountId) {
      const account = state.accounts.find((item) => String(item.id) === String(accountId));
      if (!account) return;
      document.querySelector("#buffer-manual-account-id").value = account.id || "";
      document.querySelector("#buffer-manual-account-name").value = account.account_name || "";
      document.querySelector("#buffer-manual-token").value = "";
      document.querySelector("#buffer-manual-api").value = account.buffer_api || "";
      document.querySelector("#buffer-import-text").value = "";
      document.querySelector("#buffer-submit-account").textContent = "保存修改";
      document.querySelector("#accounts-save-status").textContent = "编辑中：Token 留空表示不修改";
    }

    function cancelBufferAccountEdit() {
      document.querySelector("#buffer-manual-account-id").value = "";
      document.querySelector("#buffer-manual-account-name").value = "";
      document.querySelector("#buffer-manual-token").value = "";
      document.querySelector("#buffer-manual-api").value = "";
      document.querySelector("#buffer-import-text").value = "";
      document.querySelector("#buffer-submit-account").textContent = "提交账号";
      document.querySelector("#accounts-save-status").textContent = "";
    }

    async function syncAccounts(accountIds) {
      if (!accountIds.length) return;
      for (const accountId of accountIds) {
        await postJson("/api/accounts/discover", {accountId});
      }
      await refreshAccounts();
      await refreshProxyPoolStatus();
    }

    async function syncSelectedAccounts() {
      const accountIds = [...document.querySelectorAll(".account-select:checked")].map((input) => input.value);
      await syncAccounts(accountIds);
    }

    async function syncAllAccounts() {
      await postJson("/api/accounts/discover", {});
      await refreshAccounts();
      await refreshProxyPoolStatus();
    }

    async function loadBufferImportFile(event) {
      const file = event.target.files && event.target.files[0];
      if (!file) return;
      document.querySelector("#buffer-import-text").value = await file.text();
    }

    async function checkIp(event) {
      event.preventDefault();
      const accountId = document.querySelector("#ip-account-id").value;
      render("#proxy-output", await postJson("/check_ip", {account_id: accountId}));
    }

    async function publishBuffer(event) {
      event.preventDefault();
      let payload;
      try {
        payload = JSON.parse(document.querySelector("#buffer-payload").value);
      } catch (error) {
        render("#proxy-output", {error: "发布内容 JSON 格式无效"});
        return;
      }
      render("#proxy-output", await postJson("/publish/buffer", {
        account_id: document.querySelector("#buffer-account-id").value,
        access_token: document.querySelector("#buffer-access-token").value,
        payload,
      }));
    }

    async function refreshAdsPowerWindows() {
      const response = await fetch("/api/browser/adspower-windows");
      const data = await response.json();
      const rows = data.windows || [];
      document.querySelector("#adspower-window-list").innerHTML = rows.length
        ? rows.map((item) => `
            <tr>
              <td><input type="checkbox" class="adspower-select" data-profile-id="${escapeHtml(item.profile_id || "")}" data-profile-no="${escapeHtml(item.profile_no || "")}" data-profile-name="${escapeHtml(item.name || "")}"></td>
              <td>${escapeHtml(item.profile_no || "")}</td>
              <td>${escapeHtml(item.profile_id || "")}</td>
              <td>${escapeHtml(item.name || "")}</td>
              <td>${escapeHtml(item.group_name || "")}</td>
              <td>${escapeHtml(item.username || "")}</td>
            </tr>
          `).join("")
        : `<tr><td colspan="6">${escapeHtml(data.error || "暂无窗口")}</td></tr>`;
      document.querySelector("#browser-operation-status").textContent = rows.length ? `已读取 ${rows.length} 个 AdsPower 窗口` : (data.error || "暂无可用窗口");
    }

    function renderBrowserWindowOperation(result) {
      const data = result.data || {};
      if (result.status === 400) {
        document.querySelector("#browser-operation-status").textContent = data.error || "请求无效";
        return;
      }
      const rows = [];
      (data.results || []).forEach((item) => rows.push(
        `<tr><td>${escapeHtml(item.profile_id || "")}</td><td colspan="4">${escapeHtml(`${item.stage || "session"}：${item.status === "started" ? "窗口已启动并完成平铺" : item.error || "窗口启动失败"}`)}</td></tr>`
      ));
      (data.navigation || []).forEach((item) => rows.push(
        `<tr><td>${escapeHtml(item.profile_id || "")}</td><td colspan="4">${escapeHtml(`${item.stage || "navigate"}：${item.status === "ok" ? `已打开 ${item.url}，关闭 ${item.closed_tabs} 个其他 Tab` : item.error || "网址同步失败"}`)}</td></tr>`
      ));
      (data.layout?.missing || []).forEach((item) => rows.push(
        `<tr><td colspan="5">${escapeHtml(item)}</td></tr>`
      ));
      document.querySelector("#adspower-window-list").innerHTML = rows.join("");
      const failed = [...(data.results || []), ...(data.navigation || [])].filter((item) => item.status === "failed").length;
      document.querySelector("#browser-operation-status").innerHTML = failed
        ? `部分窗口失败：${failed} 个，详情请查询 <a href="/api/browser/logs" target="_blank" rel="noopener">/api/browser/logs</a>`
        : "窗口操作已提交，详情已保存到后台日志";
    }

    async function openAndTileAdsPowerWindows() {
      const selected = [...document.querySelectorAll(".adspower-select:checked")].map((element) => ({
        profile_id: element.dataset.profileId,
        profile_no: element.dataset.profileNo,
        name: element.dataset.profileName,
      }));
      if (selected.length < 1 || selected.length > 8) {
        document.querySelector("#browser-operation-status").textContent = "请选择 1 到 8 个浏览器窗口";
        return;
      }
      const result = await postJson("/api/browser/open-tile", {windows: selected});
      renderBrowserWindowOperation(result);
    }

    function selectedBrowserWindows() {
      return [...document.querySelectorAll(".adspower-select:checked")].map((element) => ({
        profile_id: element.dataset.profileId,
        profile_no: element.dataset.profileNo,
        name: element.dataset.profileName,
      }));
    }

    async function executeBrowserStrategy() {
      const result = await postJson("/api/browser/execute-strategy", {
        strategy_id: document.querySelector("#browser-execute-strategy").value,
        windows: selectedBrowserWindows(),
        metadata: {},
      });
      if (result.status === 400) {
        document.querySelector("#browser-operation-status").textContent = result.data.error || "请求无效";
        return;
      }
      (result.data.results || []).forEach((item) => {
        item.actions ||= [];
      });
      const rows = (result.data.results || []).map((item) =>
        `<tr><td>${escapeHtml(item.profile_id || "")}</td><td colspan="4">${escapeHtml(`${item.stage || "execute"}：${item.status === "ok" ? `策略执行完成：${item.actions.length} 个动作` : item.error || "执行失败"}`)}</td></tr>`
      );
      document.querySelector("#adspower-window-list").innerHTML = rows.join("");
      const failed = (result.data.results || []).filter((item) => item.status === "failed").length;
      document.querySelector("#browser-operation-status").innerHTML = failed
        ? `部分窗口失败：${failed} 个，详情请查询 <a href="/api/browser/logs" target="_blank" rel="noopener">/api/browser/logs</a>`
        : "执行策略已完成，详情已保存到后台日志";
    }

    dashboardNavigation.start();
    document.querySelector("#refresh").addEventListener("click", refreshStatus);
    document.querySelector("#buffer-submit-account").addEventListener("click", submitBufferAccount);
    document.querySelector("#buffer-cancel-edit").addEventListener("click", cancelBufferAccountEdit);
    document.querySelector("#accounts-sync-selected").addEventListener("click", syncSelectedAccounts);
    document.querySelector("#accounts-sync-all").addEventListener("click", syncAllAccounts);
    document.querySelector("#accounts-select-all").addEventListener("change", (event) => {
      document.querySelectorAll(".account-select").forEach((input) => {
        input.checked = event.target.checked;
      });
    });
    document.querySelector("#buffer-import-file").addEventListener("change", loadBufferImportFile);
    document.querySelector("#accounts-body").addEventListener("click", async (event) => {
      const editButton = event.target.closest(".js-edit-account");
      if (editButton) {
        editBufferAccount(editButton.dataset.accountId);
        return;
      }
      const button = event.target.closest(".js-discover-account");
      if (!button) return;
      await postJson("/api/accounts/discover", {accountId: button.dataset.accountId});
      await refreshAccounts();
      await refreshProxyPoolStatus();
    });
    document.querySelector("#proxy-refresh-accounts").addEventListener("click", refreshAccounts);
    document.querySelector("#proxy-pool-open").addEventListener("click", () => showPanel("proxy-config"));
    document.querySelector("#proxy-pool-refresh").addEventListener("click", refreshProxyPoolStatus);
    document.querySelector("#proxy-pool-search").addEventListener("input", () => refreshProxyPoolStatus({page: 1}));
    document.querySelector("#proxy-pool-page-size").addEventListener("change", () => refreshProxyPoolStatus({page: 1}));
    document.querySelector("#proxy-pool-prev").addEventListener("click", () => refreshProxyPoolStatus({page: Math.max(state.proxyPoolPage - 1, 1)}));
    document.querySelector("#proxy-pool-next").addEventListener("click", () => refreshProxyPoolStatus({page: Math.min(state.proxyPoolPage + 1, state.proxyPoolPageCount)}));
    document.querySelector("#proxy-assignment-body").addEventListener("click", async (event) => {
      const button = event.target.closest(".js-assign-proxy");
      if (!button) return;
      await assignProxyForAccount(button);
    });
    modelField("provider").addEventListener("change", (event) => {
      renderModelOptions(event.currentTarget.value);
      applyModelPreset();
    });
    document.querySelector("#model-preset-model").addEventListener("change", applyModelPreset);
    document.querySelector("#model-presets-refresh").addEventListener("click", loadModelPresets);
    document.querySelector("#settings-form").addEventListener("submit", saveSettings);
    document.querySelector("#settings-restore-latest").addEventListener("click", restoreLatestSettings);
    document.querySelector("#adspower-refresh-windows").addEventListener("click", refreshAdsPowerWindows);
    document.querySelector("#adspower-open-tile").addEventListener("click", openAndTileAdsPowerWindows);
    document.querySelector("#browser-execute-strategy-button").addEventListener("click", executeBrowserStrategy);
    /*
      const button = event.target.closest(".browser-copy-ws");
      if (!button) return;
      try {
        await navigator.clipboard.writeText(button.dataset.ws || "");
        const original = button.textContent;
        button.textContent = "已复制";
        window.setTimeout(() => { button.textContent = original; }, 1200);
      } catch (_error) {
        button.textContent = "复制失败";
      }
    });
    */
    refreshAdsPowerWindows();
    document.querySelector('[name="proxy_pool.raw"]').addEventListener("input", previewProxyPoolFromInput);
    document.querySelector("#content-sync-videos").addEventListener("click", syncContentVideos);
    document.querySelector("#content-refresh-videos").addEventListener("click", refreshContentVideos);
    document.querySelector("#content-create-brand").addEventListener("click", () => {
      document.querySelector("#content-brand-status").textContent = "";
      document.querySelector("#content-brand-dialog").showModal();
    });
    document.querySelector("#content-brand-form").addEventListener("submit", createContentBrand);
    document.querySelectorAll(".js-close-brand-dialog").forEach((button) => {
      button.addEventListener("click", () => document.querySelector("#content-brand-dialog").close());
    });
    document.querySelector("#content-brand-grid").addEventListener("click", async (event) => {
      const folder = event.target.closest(".js-open-brand");
      if (folder) await openBrandFolder(folder.dataset.brandId);
    });
    document.querySelector("#content-back-brands").addEventListener("click", showBrandOverview);
    document.querySelector("#content-rename-brand").addEventListener("click", openRenameDialog);
    document.querySelector("#content-rename-form").addEventListener("submit", renameActiveBrand);
    document.querySelectorAll(".js-close-rename-dialog").forEach((button) => {
      button.addEventListener("click", () => document.querySelector("#content-rename-dialog").close());
    });
    document.querySelector("#content-open-import").addEventListener("click", openImportDialog);
    document.querySelector("#content-close-import").addEventListener("click", closeImportDialog);
    document.querySelector("#content-cancel-import").addEventListener("click", closeImportDialog);
    document.querySelector("#content-import-form").addEventListener("submit", submitCopyImport);
    document.querySelector("#content-add-copy").addEventListener("click", addContentCopy);
    document.querySelector("#publish-account-id").addEventListener("change", renderPublishSelectors);
    document.querySelector("#publish-brand-id").addEventListener("change", refreshPublishCopyItems);
    document.querySelector("#publish-manual-test").addEventListener("click", manualPublishTest);
    document.querySelector("#publish-batch-create").addEventListener("click", openBatchPublishDialog);
    document.querySelector("#publish-batch-form").addEventListener("submit", submitBatchPublish);
    document.querySelector("#publish-batch-close").addEventListener("click", closeBatchPublishDialog);
    document.querySelector("#publish-batch-cancel").addEventListener("click", closeBatchPublishDialog);
    document.querySelector("#publish-batch-select-all").addEventListener("change", (event) => {
      document.querySelectorAll(".publish-batch-account").forEach((input) => {
        input.checked = event.target.checked;
      });
    });
    document.querySelector("#publish-refresh-batches").addEventListener("click", refreshBatchRuns);
    document.querySelector("#publish-batch-runs-body").addEventListener("click", async (event) => {
      const editButton = event.target.closest(".js-edit-batch-run");
      if (editButton) {
        editBatchRun(JSON.parse(editButton.dataset.run || "{}"));
        return;
      }
      const deleteButton = event.target.closest(".js-delete-batch-run");
      if (deleteButton) await deleteBatchRun(deleteButton.dataset.runId);
    });
    document.querySelector("#publish-save-daily-schedule").addEventListener("click", openDailyScheduleDialog);
    document.querySelector("#publish-refresh-daily-schedules").addEventListener("click", refreshDailySchedules);
    document.querySelector("#publish-daily-form").addEventListener("submit", submitDailySchedule);
    document.querySelector("#publish-daily-close").addEventListener("click", closeDailyScheduleDialog);
    document.querySelector("#publish-daily-cancel").addEventListener("click", closeDailyScheduleDialog);
    document.querySelector("#publish-daily-select-all").addEventListener("change", (event) => {
      document.querySelectorAll(".publish-daily-account").forEach((input) => {
        input.checked = event.target.checked;
      });
    });
    document.querySelector("#publish-daily-schedules-body").addEventListener("click", async (event) => {
      const editButton = event.target.closest(".js-edit-daily-schedule");
      if (editButton) {
        editDailySchedule(JSON.parse(editButton.dataset.schedule || "{}"));
        return;
      }
      const deleteButton = event.target.closest(".js-delete-daily-schedule");
      if (deleteButton) await deleteDailySchedule(deleteButton.dataset.scheduleId);
    });
    document.querySelector("#publish-refresh-results").addEventListener("click", refreshPublishResults);
    document.querySelector("#publish-cleanup-logs").addEventListener("click", cleanupPublishLogs);
    document.querySelector("#ip-form")?.addEventListener("submit", checkIp);
    document.querySelector("#buffer-form")?.addEventListener("submit", publishBuffer);
    document.querySelector("#browser-sync")?.addEventListener("click", loadSettings);
    document.querySelector("#browser-cdp-url")?.addEventListener("input", (event) => {
    });
    document.querySelector("#browser-task-goal")?.addEventListener("input", (event) => {
    });

    loadSettings()
      .then(refreshStatus)
      .then(refreshAccounts)
      .then(refreshContentVideos)
      .then(refreshBrands)
      .then(refreshPublishResults)
      .then(refreshBatchRuns)
      .then(refreshDailySchedules);
  </script>
  <script src="/static/browser_strategy_ui.js" onload="window.BrowserStrategyUI.init()"></script>
</body>
</html>
"""
