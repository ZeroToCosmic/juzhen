import asyncio
import copy
import hashlib
import inspect
import json
import logging
import os
import re
import random
import secrets
import subprocess
import threading
import time
from contextlib import contextmanager
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import requests
from pathlib import Path
from flask import Flask, abort, g, jsonify, render_template, render_template_string, request, send_from_directory, session
from werkzeug.exceptions import RequestEntityTooLarge

from adspower import AdsPowerController, AdsPowerError
from browser_public_identity import mask_profile_id

LOGGER = logging.getLogger(__name__)

from gateway.account_store import (
    account_summary,
    assign_proxy_session,
    get_assigned_proxy_sessions,
    get_buffer_account,
    get_next_account,
    save_buffer_account,
    update_account,
)
from gateway.auth_blueprint import (
    create_auth_blueprint,
    install_management_guard,
)
from gateway.auth_service import AuthService
from gateway.auth_store import AuthStore
from gateway.buffer_client import (
    extract_tiktok_url_from_buffer_payload,
    fetch_buffer_post,
    publish_to_buffer,
)
from gateway.buffer_discovery import discover_accounts, import_buffer_accounts
from gateway.config import load_proxy_config
from gateway.content_import import parse_copy_import
from gateway.content_store import (
    DEFAULT_CONTENT_DIR,
    add_copy_item,
    apply_copy_import,
    cleanup_publish_logs,
    compose_text,
    create_brand,
    delete_batch_publish_run,
    delete_daily_schedule,
    get_copy_item,
    get_video,
    list_batch_publish_runs,
    list_brands,
    list_copy_items,
    list_daily_schedules,
    mark_publish_sample_failure,
    mark_publish_sample_success,
    mark_tiktok_link_backfill_failure,
    mark_tiktok_link_backfill_success,
    mark_video_used,
    next_due_publish_sample,
    next_pending_publish_task,
    next_pending_tiktok_link_backfill,
    now_iso as content_now_iso,
    publish_stats,
    public_publish_tasks,
    rename_brand,
    save_batch_publish_run,
    save_daily_schedule,
    save_publish_task,
    sync_video_library,
    update_batch_publish_run,
    update_daily_schedule,
    update_publish_task,
    update_publish_metrics,
    unused_videos,
    video_summary,
)
from gateway.ip_checker import fetch_ip_info
from gateway.model_presets import public_model_presets
from gateway.management_db import open_management_db
from gateway.local_only import install_local_only_guard
from gateway.proxy import build_static_proxy_url, generate_proxy_url
from gateway.proxy_pool import parse_proxy_pool, proxy_pool_key, select_proxy_from_pool, summarize_proxy_pool
from gateway.r2_client import list_r2_video_objects
from gateway.settings_store import (
    get_config_health,
    load_settings,
    restore_latest_backup_preserving,
    save_settings,
    mutate_settings,
    update_settings as merge_saved_settings,
)
from gateway.session_key import load_or_create_session_key
from execution_v2.blueprint import create_browser_v2_blueprint
from comment_campaign.blueprint import create_comment_campaign_blueprint
from browser_strategy_config import (
    ACTION_CATALOG,
    DEFAULT_ACTION_PARAMS,
    element_references,
    load_or_migrate_strategy_state,
    normalize_block_strategies,
    normalize_elements,
    normalize_patterns,
    pattern_references,
)
from browser_element_resolver import LocatorResolutionError, inspect_element
from browser_element_schema import TIKTOK_COMMENT_TEMPLATE, normalize_element_definitions
from tiktok_stats.blueprint import (
    create_tiktok_stats_blueprint,
    default_query_factory,
    default_secret_store_factory,
    default_status_provider,
    default_store_factory,
    register_tiktok_stats_error_handler,
    unavailable_cookie_validator,
    unavailable_run_dispatcher,
)
from selector_probe.blueprint import (
    check_strategy_gate,
    create_selector_probe_blueprint,
    default_gate_service_factory as default_selector_probe_gate_service_factory,
    default_registry_factory as default_selector_probe_registry_factory,
    default_run_dispatcher as default_selector_probe_dispatcher,
    default_store_factory as default_selector_probe_store_factory,
)


def load_persisted_strategy_state() -> dict:
    """Load the version-3 strategy state and persist a legacy migration once."""

    result = {}

    def migrate(settings):
        browser, changed = load_or_migrate_strategy_state(settings.get("browser", {}))
        result["browser"] = browser
        if not changed:
            return None
        settings["browser"] = browser
        return settings

    mutate_settings(migrate)
    return result["browser"]


def mutate_persisted_strategy_state(mutator) -> dict:
    """Mutate normalized browser strategy state under the settings-store lock."""

    def update(settings):
        browser, _changed = load_or_migrate_strategy_state(settings.get("browser", {}))
        settings["browser"] = mutator(browser)
        return settings

    return mutate_settings(update)["browser"]


class _StrategyReferenceConflict(ValueError):
    def __init__(self, resource: str, references: list[dict]):
        super().__init__(f"{resource} is referenced by block strategies")
        self.references = references


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


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ACTIVE_BROWSER_SESSIONS = {}
ACTIVE_BROWSER_SESSIONS_LOCK = threading.Lock()
ACTIVE_PATTERN_RECORDINGS = {}
ACTIVE_PATTERN_RECORDINGS_LOCK = threading.Lock()
BROWSER_SESSION_LEASES = {}
BROWSER_PROFILE_SESSION_LOCKS = {}
BROWSER_PROFILE_SESSION_LOCKS_LOCK = threading.Lock()
BROWSER_PROFILE_EXECUTIONS = set()
BROWSER_PROFILE_EXECUTIONS_LOCK = threading.Lock()
BROWSER_LOG_PATH = PROJECT_ROOT / "logs" / "browser_operations.jsonl"
BROWSER_LOG_LOCK = threading.Lock()
BROWSER_BATCH_TASKS = {}
BROWSER_BATCH_TASKS_LOCK = threading.Lock()


def is_valid_browser_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and not re.search(r"\s", value)
    )


def sanitize_public_browser_url(value: str) -> str:
    parsed = urlsplit(value)
    hostname = parsed.hostname or ""
    host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = f"{host}:{parsed.port}" if parsed.port is not None else host
    path_segments = []
    redact_next = False
    for segment in parsed.path.split("/"):
        sensitive = is_sensitive_browser_key(segment)
        path_segments.append("[redacted]" if redact_next or sensitive else segment)
        redact_next = sensitive or segment.casefold() == "video"
    path = "/".join(path_segments)
    query = []
    for item_key, item_value in parse_qsl(parsed.query, keep_blank_values=True):
        if is_sensitive_browser_key(item_key):
            item_value = "[redacted]"
        query.append((item_key, item_value))
    fragment = sanitize_public_browser_fragment(parsed.fragment)
    return urlunsplit(
        (parsed.scheme, netloc, path, urlencode(query), fragment)
    )


def sanitize_public_browser_origin(value: str) -> str:
    parsed = urlsplit(sanitize_public_browser_url(value))
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


SENSITIVE_BROWSER_KEY_MARKERS = (
    "accesskey",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "session",
    "token",
)


def normalize_sensitive_browser_key(key: object) -> str:
    decoded = unquote(str(key)).lower()
    bracket_normalized = re.sub(r"\[([^\]]*)\]", r"_\1", decoded)
    return re.sub(r"[^a-z0-9]", "", bracket_normalized)


def is_sensitive_browser_key(key: object) -> bool:
    normalized = normalize_sensitive_browser_key(key)
    return any(marker in normalized for marker in SENSITIVE_BROWSER_KEY_MARKERS)


def is_sensitive_browser_payload_key(key: object, value: object) -> bool:
    normalized = normalize_sensitive_browser_key(key)
    if (
        normalized == "sessions"
        and isinstance(value, (list, tuple))
        and all(isinstance(item, dict) for item in value)
    ):
        return False
    return is_sensitive_browser_key(key)


SAFE_PUBLIC_CREDENTIAL_STATUSES = {
    "expired",
    "invalid",
    "missing",
    "not configured",
}
PUBLIC_CREDENTIAL_VALUE_PATTERN = (
    r'"[^"\r\n]*"|\'[^\'\r\n]*\'|not\s+configured|[^\s,;&]+'
)
PUBLIC_BROWSER_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(?<![a-z0-9_%\[\].-])"
    r"(?P<key>[a-z%][a-z0-9_%\[\].-]*"
    r"(?:[ \t]+[a-z0-9_%\[\].-]+){0,3})"
    r"[ \t]*(?P<separator>=|:)[ \t]*"
)
PUBLIC_BROWSER_SPACE_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(?<![a-z0-9_%\[\].-])"
    r"(?P<key>"
    r"access[ _.-]*key(?:[ _.-]*id)?|"
    r"api[ _.-]*key|authorization|cookie|credential|password|"
    r"secret|session(?:[ _.-]*id)?|token"
    r")(?P<separator>[ \t]+)"
)
PUBLIC_BROWSER_HEADER_PATTERN = re.compile(
    r"(?i)(?<![a-z0-9_%\[\].-])"
    r"(?P<key>cookie|authorization)"
    r"[ \t]*(?P<separator>:|=)[ \t]*"
)
SAFE_PUBLIC_DIAGNOSTIC_KEYS = {
    "actionid",
    "actionindex",
    "actiontype",
    "attempts",
    "currenturl",
    "error",
    "message",
    "outcome",
    "profileid",
    "reason",
    "retry",
    "stage",
    "status",
    "targeturl",
}


def is_safe_public_credential_value(value: str) -> bool:
    normalized = value.strip().strip("\"'").casefold()
    status = normalized
    scheme_and_status = normalized.split(None, 1)
    if normalized not in SAFE_PUBLIC_CREDENTIAL_STATUSES and len(
        scheme_and_status
    ) == 2 and re.fullmatch(
        r"[a-z][a-z0-9_-]*", scheme_and_status[0]
    ):
        status = scheme_and_status[1]
    return status in SAFE_PUBLIC_CREDENTIAL_STATUSES


def is_safe_public_header_value(value: str) -> bool:
    normalized = value.strip()
    if (
        len(normalized) >= 2
        and normalized[0] in "\"'"
        and normalized[-1] == normalized[0]
    ):
        normalized = normalized[1:-1].strip()
    return normalized.casefold() in SAFE_PUBLIC_CREDENTIAL_STATUSES


def _redact_public_browser_header_line(line: str) -> str:
    match = PUBLIC_BROWSER_HEADER_PATTERN.search(line)
    if match is None or is_safe_public_header_value(line[match.end() :]):
        return line
    return f"{line[:match.end()]}[redacted]"


def sanitize_public_browser_headers(value: str) -> str:
    sanitized = []
    for line in value.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        sanitized.extend(
            (_redact_public_browser_header_line(body), line[len(body) :])
        )
    if not sanitized and value == "":
        return ""
    return "".join(sanitized)


def redact_public_browser_credential(match) -> str:
    if is_safe_public_credential_value(match.group("value")):
        return match.group(0)
    return f"{match.group('prefix')}[redacted]"


def _trusted_public_assignment_boundary(matches, start_index):
    for match in matches[start_index + 1 :]:
        normalized = normalize_sensitive_browser_key(match.group("key"))
        if normalized in SAFE_PUBLIC_DIAGNOSTIC_KEYS:
            return match
    return None


def _redact_public_assignment_line(line: str) -> str:
    matches = sorted(
        [
            *PUBLIC_BROWSER_ASSIGNMENT_PATTERN.finditer(line),
            *PUBLIC_BROWSER_SPACE_ASSIGNMENT_PATTERN.finditer(line),
        ],
        key=lambda match: (match.start(), -match.end()),
    )
    matches = [
        match
        for index, match in enumerate(matches)
        if index == 0 or match.start() >= matches[index - 1].end()
    ]
    intervals = []
    for index, match in enumerate(matches):
        if not is_sensitive_browser_key(match.group("key")):
            continue
        boundary = _trusted_public_assignment_boundary(matches, index)
        boundary_start = boundary.start() if boundary is not None else len(line)
        gap = line[match.end() : boundary_start]
        structural_suffix = re.search(r"([,;]?[ \t]*)$", gap)
        redact_end = (
            match.end() + structural_suffix.start()
            if structural_suffix is not None
            else boundary_start
        )
        if redact_end <= match.end():
            continue
        raw_value = line[match.end() : redact_end]
        if is_safe_public_credential_value(raw_value):
            continue
        intervals.append((match.end(), redact_end))

    if not intervals:
        return line
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    result = []
    cursor = 0
    for start, end in merged:
        result.extend((line[cursor:start], "[redacted]"))
        cursor = end
    result.append(line[cursor:])
    return "".join(result)


def sanitize_public_browser_assignments(value: str) -> str:
    sanitized = []
    for line in value.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        sanitized.append(_redact_public_assignment_line(body))
        sanitized.append(line[len(body) :])
    if not sanitized and value == "":
        return ""
    return "".join(sanitized)


def _sanitize_public_fragment_assignment(value: str) -> str:
    for separator in ("=", ":"):
        if separator not in value:
            continue
        key, item_value = value.split(separator, 1)
        if is_sensitive_browser_key(key) and not is_safe_public_credential_value(
            item_value
        ):
            return f"{key}{separator}[redacted]"
        return value
    return value


def sanitize_public_browser_fragment(value: str) -> str:
    segments = []
    redact_next = False
    for segment in value.split("/"):
        if redact_next:
            segments.append("[redacted]")
            redact_next = False
            continue
        if "=" in segment or ":" in segment or "&" in segment:
            segments.append(
                "&".join(
                    _sanitize_public_fragment_assignment(item)
                    for item in segment.split("&")
                )
            )
            continue
        sensitive = is_sensitive_browser_key(segment)
        segments.append("[redacted]" if sensitive else segment)
        redact_next = sensitive
    return "/".join(segments)


def sanitize_public_browser_text(
    value: str, *, redact_urls: bool = True
) -> str:
    text = value
    if redact_urls:
        text = re.sub(
            r"\b(?:wss?|https?)://[^\s,;\)\]\}>\"']+",
            "[redacted-url]",
            text,
            flags=re.IGNORECASE,
        )
    text = re.sub(
        rf"(?i)(?P<prefix>\bbearer\s+)"
        rf"(?P<value>{PUBLIC_CREDENTIAL_VALUE_PATTERN})",
        redact_public_browser_credential,
        text,
    )
    text = sanitize_public_browser_headers(text)
    text = sanitize_public_browser_assignments(text)
    return re.sub(
        r"(?i)/devtools/browser/[^\s,;\)\]\}>\"']+",
        "[redacted-devtools-endpoint]",
        text,
    )


def public_browser_payload(value, key: str = ""):
    from gateway.browser_orchestrator import public_session_result

    if normalize_sensitive_browser_key(key) == "profileid":
        return mask_profile_id(value)
    if isinstance(value, dict):
        public = {}
        for item_key, item_value in value.items():
            if is_sensitive_browser_payload_key(item_key, item_value):
                normalized_key = normalize_sensitive_browser_key(item_key)
                normalized_value = (
                    str(item_value).strip().casefold()
                    if isinstance(item_value, str)
                    else ""
                )
                if (
                    normalized_key.endswith("status")
                    and normalized_value in SAFE_PUBLIC_CREDENTIAL_STATUSES
                ):
                    public[item_key] = normalized_value
                continue
            if item_key not in public_session_result({item_key: None}):
                continue
            public[item_key] = public_browser_payload(item_value, str(item_key))
        return public
    if isinstance(value, (list, tuple)):
        return [public_browser_payload(item) for item in value]
    if isinstance(value, BaseException):
        return public_browser_payload(str(value), key)
    if isinstance(value, str) and (
        key == "origin" or key.endswith("_origin")
    ):
        if is_valid_browser_url(value):
            return sanitize_public_browser_origin(value)
    if isinstance(value, str) and key in {"url", "target_url", "current_url"}:
        if is_valid_browser_url(value):
            return sanitize_public_browser_url(value)
    if isinstance(value, str):
        return sanitize_public_browser_text(value)
    return value


def sanitize_browser_log_file() -> None:
    """Rewrite legacy browser logs through the current public-data boundary."""

    if not BROWSER_LOG_PATH.exists():
        return
    temporary_path = BROWSER_LOG_PATH.with_name(
        f".{BROWSER_LOG_PATH.name}.{uuid4().hex}.tmp"
    )
    try:
        with BROWSER_LOG_LOCK:
            sanitized_lines = []
            for line in BROWSER_LOG_PATH.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    # A malformed legacy line cannot be proven safe to retain.
                    continue
                sanitized_lines.append(
                    json.dumps(public_browser_payload(entry), ensure_ascii=False)
                )
            payload = "\n".join(sanitized_lines)
            if payload:
                payload += "\n"
            temporary_path.write_text(payload, encoding="utf-8")
            os.replace(temporary_path, BROWSER_LOG_PATH)
    except OSError as error:
        LOGGER.warning("Browser log sanitization failed: %s", error)
    finally:
        temporary_path.unlink(missing_ok=True)


def record_browser_log(operation: str, payload: dict) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "operation": operation,
        "payload": public_browser_payload(payload),
    }
    try:
        BROWSER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with BROWSER_LOG_LOCK:
            with BROWSER_LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def get_adspower_base_url():
    return (
        load_settings()
        .get("adspower", {})
        .get("base_url", "http://local.adspower.net:50325")
    ).rstrip("/")


def get_adspower_headers():
    api_key = str(
        load_settings()
        .get("adspower", {})
        .get("api_key", "")
    ).strip() or os.getenv("ADSPOWER_API_KEY", "").strip()
    if not api_key:
        return None
    return {"Authorization": f"Bearer {api_key}"}


def selected_browser_sessions(selected):
    requested = selected or []
    requested_ids = []
    for item in requested:
        profile_id = item if isinstance(item, str) else item.get("profile_id")
        if profile_id:
            requested_ids.append(str(profile_id))
    if not requested_ids:
        with ACTIVE_BROWSER_SESSIONS_LOCK:
            requested_ids = list(ACTIVE_BROWSER_SESSIONS)
    requested_ids = list(dict.fromkeys(requested_ids))
    sessions = []
    for profile_id in requested_ids:
        with browser_profile_session_lock(profile_id):
            with ACTIVE_BROWSER_SESSIONS_LOCK:
                ws_url = ACTIVE_BROWSER_SESSIONS.get(profile_id)
                if ws_url and not _acquire_browser_session_use_locked(
                    profile_id, ws_url
                ):
                    ws_url = None
            sessions.append((profile_id, ws_url))
    return sessions


def release_selected_browser_sessions(sessions) -> None:
    for profile_id, ws_url in sessions:
        if ws_url:
            release_browser_session_use(profile_id, ws_url)


def normalize_selected_browser_profiles(selected) -> list[dict]:
    if not isinstance(selected, list) or not 1 <= len(selected) <= 8:
        raise ValueError("请选择 1 到 8 个浏览器窗口")
    profiles = []
    for item in selected:
        if isinstance(item, str):
            item = {"profile_id": item}
        if not isinstance(item, dict):
            raise ValueError("窗口选择格式无效")
        profile_id = str(item.get("profile_id") or "").strip()
        if not profile_id:
            raise ValueError("每个窗口都必须包含 profile_id")
        profiles.append(
            {
                "profile_id": profile_id,
                "profile_no": str(item.get("profile_no") or ""),
                "name": str(item.get("name") or ""),
            }
        )
    return profiles


class BrowserExecutionBusyError(RuntimeError):
    stage = "execution_busy"
    current_url = ""
    page_recoveries = ()

    def __init__(self, profile_id: str):
        self.reason = "profile already has a strategy execution in progress"
        super().__init__(self.reason)


@contextmanager
def browser_profile_execution_reservation(profile_id: str):
    profile_id = str(profile_id)
    with BROWSER_PROFILE_EXECUTIONS_LOCK:
        if profile_id in BROWSER_PROFILE_EXECUTIONS:
            raise BrowserExecutionBusyError(profile_id)
        BROWSER_PROFILE_EXECUTIONS.add(profile_id)
    try:
        yield
    finally:
        with BROWSER_PROFILE_EXECUTIONS_LOCK:
            BROWSER_PROFILE_EXECUTIONS.discard(profile_id)


@contextmanager
def browser_profile_session_lock(profile_id: str):
    with BROWSER_PROFILE_SESSION_LOCKS_LOCK:
        entry = BROWSER_PROFILE_SESSION_LOCKS.get(profile_id)
        if entry is None:
            entry = {"lock": threading.Lock(), "users": 0}
            BROWSER_PROFILE_SESSION_LOCKS[profile_id] = entry
        entry["users"] += 1
    try:
        with entry["lock"]:
            yield
    finally:
        with BROWSER_PROFILE_SESSION_LOCKS_LOCK:
            entry["users"] -= 1
            if (
                entry["users"] == 0
                and BROWSER_PROFILE_SESSION_LOCKS.get(profile_id) is entry
            ):
                BROWSER_PROFILE_SESSION_LOCKS.pop(profile_id, None)


def _acquire_browser_session_use_locked(profile_id: str, ws_url: str) -> bool:
    if ACTIVE_BROWSER_SESSIONS.get(profile_id) != ws_url:
        return False
    key = (profile_id, ws_url)
    lease = BROWSER_SESSION_LEASES.setdefault(
        key, {"users": 0, "close_requested": False}
    )
    lease["users"] += 1
    return True


def acquire_browser_session_use(profile_id: str, ws_url: str) -> bool:
    with browser_profile_session_lock(profile_id):
        with ACTIVE_BROWSER_SESSIONS_LOCK:
            return _acquire_browser_session_use_locked(profile_id, ws_url)


def _stop_browser_profile(profile_id: str) -> None:
    adspower_settings = load_settings().get("adspower", {})
    controller = AdsPowerController(
        base_url=adspower_settings.get("base_url") or get_adspower_base_url(),
        api_key=adspower_settings.get("api_key") or os.getenv("ADSPOWER_API_KEY", ""),
    )
    controller.stop_browser(profile_id)


def release_browser_session_use(
    profile_id: str,
    ws_url: str,
    *,
    request_close: bool = False,
    stop_browser=None,
) -> None:
    should_stop = False
    key = (profile_id, ws_url)
    with browser_profile_session_lock(profile_id):
        with ACTIVE_BROWSER_SESSIONS_LOCK:
            lease = BROWSER_SESSION_LEASES.get(key)
            if lease is None:
                return
            if request_close:
                lease["close_requested"] = True
            lease["users"] = max(int(lease.get("users", 0)) - 1, 0)
            if lease["users"] == 0:
                if (
                    lease.get("close_requested")
                    and ACTIVE_BROWSER_SESSIONS.get(profile_id) == ws_url
                ):
                    ACTIVE_BROWSER_SESSIONS.pop(profile_id, None)
                    should_stop = True
                BROWSER_SESSION_LEASES.pop(key, None)
        if should_stop:
            try:
                (stop_browser or _stop_browser_profile)(profile_id)
            except Exception as error:
                with ACTIVE_BROWSER_SESSIONS_LOCK:
                    ACTIVE_BROWSER_SESSIONS.setdefault(profile_id, ws_url)
                record_browser_log(
                    "session_stop_failed",
                    {"profile_id": profile_id, "error": str(error)},
                )


def release_browser_session_results(
    session_results: list[dict], *, request_close: bool = False
) -> None:
    for item in session_results:
        if item.get("status") == "ready" and item.get("ws_url"):
            release_browser_session_use(
                item["profile_id"],
                item["ws_url"],
                request_close=request_close,
            )


def ensure_browser_profile_sessions(
    profiles: list[dict], *, lease_sessions: bool = False
) -> tuple[list[dict], dict | None]:
    from browser_cdp import wait_for_cdp
    from gateway.browser_orchestrator import ensure_profile_session

    adspower_settings = load_settings().get("adspower", {})
    controller = AdsPowerController(
        base_url=adspower_settings.get("base_url") or get_adspower_base_url(),
        api_key=adspower_settings.get("api_key") or os.getenv("ADSPOWER_API_KEY", ""),
    )

    def ensure_one(profile):
        profile_id = profile["profile_id"]
        with browser_profile_session_lock(profile_id):
            with ACTIVE_BROWSER_SESSIONS_LOCK:
                current_ws = ACTIVE_BROWSER_SESSIONS.get(profile_id)
                current_lease = BROWSER_SESSION_LEASES.get(
                    (profile_id, current_ws), {}
                )
                current_in_use = bool(
                    current_ws and int(current_lease.get("users", 0)) > 0
                )
            if current_in_use:
                try:
                    current_ready = bool(wait_for_cdp(current_ws, timeout=30.0))
                except Exception:
                    current_ready = False
                result = {
                    "profile_id": profile_id,
                    "profile_no": str(profile.get("profile_no") or ""),
                    "name": str(profile.get("name") or ""),
                    "status": "ready" if current_ready else "failed",
                    "stage": "session_check" if current_ready else "session_busy",
                    "attempts": 0,
                    "ws_url": current_ws if current_ready else "",
                    "error": (
                        ""
                        if current_ready
                        else "当前窗口正被其他任务使用且 CDP 检查失败；为避免中断任务，未重启该窗口"
                    ),
                }
            else:
                result = ensure_profile_session(
                    profile,
                    current_ws,
                    controller,
                    lambda ws_url: wait_for_cdp(ws_url, timeout=30.0),
                    retries=3,
                    sleep_fn=time.sleep,
                )
            with ACTIVE_BROWSER_SESSIONS_LOCK:
                if result.get("status") == "ready":
                    ACTIVE_BROWSER_SESSIONS[profile_id] = result["ws_url"]
                    if lease_sessions:
                        _acquire_browser_session_use_locked(
                            profile_id, result["ws_url"]
                        )
                else:
                    if (
                        not current_in_use
                        and ACTIVE_BROWSER_SESSIONS.get(profile_id) == current_ws
                    ):
                        ACTIVE_BROWSER_SESSIONS.pop(profile_id, None)
            return result

    with ThreadPoolExecutor(max_workers=len(profiles)) as executor:
        session_results = list(executor.map(ensure_one, profiles))

    ready = [item for item in session_results if item.get("status") == "ready"]

    layout = None
    if ready:
        from window_tiler import tile_browser_windows

        hints = [
            {
                "profile_id": item["profile_id"],
                "profile_no": item.get("profile_no", ""),
                "name": item.get("name", ""),
                "ws_puppeteer": item["ws_url"],
            }
            for item in ready
        ]
        try:
            layout = tile_browser_windows(hints)
        except Exception as error:
            safe_error = public_browser_payload({"error": str(error)})["error"]
            layout = {
                "count": 0,
                "layout": [],
                "missing": [f"窗口平铺失败：{safe_error}"],
                "error": safe_error,
            }
    return session_results, layout


def browser_tile_error(
    layout: dict | None,
    profile_id: str,
    ready_profile_ids: list[str] | None = None,
) -> str:
    if not layout:
        return ""
    profile_ids = [
        str(item_id).strip()
        for item_id in (ready_profile_ids or [profile_id])
        if str(item_id).strip()
    ]

    def public_tile_message(value: object) -> str:
        message = str(value)
        for item_id in sorted(set(profile_ids), key=len, reverse=True):
            message = re.sub(
                rf"(?<![\w-]){re.escape(item_id)}(?![\w-])",
                lambda _match, masked=mask_profile_id(item_id): masked,
                message,
            )
        return public_browser_payload({"error": message})["error"]

    if layout.get("error"):
        return public_tile_message(layout["error"])
    errors = []
    for item in layout.get("missing") or []:
        message = str(item)
        matches = [item_id for item_id in profile_ids if message == item_id]
        if not matches:
            matches = [
                item_id
                for item_id in profile_ids
                if re.search(
                    rf"(?<![\w-]){re.escape(item_id)}(?![\w-])",
                    message,
                )
            ]
        if len(matches) != 1 or matches[0] == profile_id:
            safe_message = public_tile_message(message)
            errors.append(f"窗口平铺失败：{safe_message}")
    for item in layout.get("scale_results") or []:
        if not isinstance(item, dict) or item.get("status") != "failed":
            continue
        failed_profile_id = str(item.get("profile_id") or "").strip()
        if failed_profile_id in profile_ids:
            matches = [failed_profile_id]
        else:
            matches = []
        if len(matches) != 1 or matches[0] == profile_id:
            message = str(item.get("error") or failed_profile_id or "页面缩放失败")
            safe_message = public_tile_message(message)
            errors.append(f"窗口缩放失败：{safe_message}")
    return "；".join(errors)


class BrowserStageError(RuntimeError):
    def __init__(self, stage: str, target_url: str, reason: str, current_url: str = ""):
        super().__init__(reason)
        self.stage = stage
        self.target_url = target_url
        self.reason = reason
        self.current_url = current_url


def get_browser_target_url(payload: dict | None = None) -> str:
    browser_settings = load_settings().get("browser", {})
    requested_url = str((payload or {}).get("url") or "").strip()
    configured_url = str(browser_settings.get("default_url") or "").strip()
    return requested_url or configured_url or "https://www.tiktok.com/"


def get_async_playwright():
    try:
        from playwright.async_api import async_playwright
    except ImportError as error:  # pragma: no cover - dependency check handles this
        raise RuntimeError("Playwright is unavailable for element inspection") from error
    return async_playwright


async def _current_inspection_page(context):
    pages = []
    for page in list(getattr(context, "pages", [])):
        try:
            if not page.is_closed():
                pages.append(page)
        except Exception:
            continue
    if not pages:
        raise RuntimeError("no active page available for element inspection")
    for page in reversed(pages):
        try:
            if await page.evaluate("document.visibilityState") == "visible":
                return page
        except Exception:
            continue
    return pages[-1]


async def _inspect_browser_elements_on_cdp(ws_url: str, elements: dict) -> list[dict]:
    playwright = await get_async_playwright()().start()
    try:
        browser = await playwright.chromium.connect_over_cdp(ws_url, timeout=10_000)
        contexts = list(browser.contexts)
        if not contexts:
            raise RuntimeError("no operable browser context for element inspection")
        page = await _current_inspection_page(contexts[0])
        results = []
        for alias, definition in elements.items():
            try:
                results.append(await inspect_element(page, alias, definition))
            except LocatorResolutionError as error:
                results.append(
                    {
                        "status": "error",
                        "code": error.code,
                        "alias": alias,
                        "scope": definition["scope"],
                        "diagnostics": error.diagnostics,
                    }
                )
            except Exception:
                results.append(
                    {
                        "status": "error",
                        "code": "element_inspection_failed",
                        "alias": alias,
                        "scope": definition["scope"],
                        "diagnostics": {},
                    }
                )
        return results
    finally:
        await playwright.stop()


def inspect_browser_elements_on_cdp(ws_url: str, elements: dict) -> list[dict]:
    """Inspect the currently active CDP page without browser actions or navigation."""

    return asyncio.run(_inspect_browser_elements_on_cdp(ws_url, elements))


_PUBLIC_LOCATOR_ERROR_CODES = {
    "element_alias_missing",
    "element_candidate_ambiguous",
    "element_candidate_not_found",
    "element_inspection_failed",
    "element_not_actionable",
    "element_postcondition_not_observed",
    "element_resolution_failed",
    "element_scope_not_found",
}
_PUBLIC_LOCATOR_TYPES = {"attribute", "css", "role", "xpath"}
_PUBLIC_VIDEO_SWITCH_ERROR_CODES = {
    "video_switch_closed_target",
    "video_switch_interval_failed",
    "video_switch_not_observed",
    "video_switch_recovery_failed",
    "video_switch_state_capture_failed",
    "video_switch_timeout",
}
_PUBLIC_DIAGNOSTIC_COUNT_KEYS = {
    "actionable_count",
    "article_count",
    "center_intersection_count",
    "container_count",
    "input_count",
    "matching_article_id_count",
    "panel_count",
    "raw_count",
    "usable_input_count",
    "usable_panel_count",
    "visible_article_count",
    "visible_container_count",
    "visible_count",
    "visible_input_count",
    "visible_panel_count",
}
_PUBLIC_DIAGNOSTIC_PHASES = {
    "editable_check",
    "inspection",
    "locator_query",
    "scope_query",
}


def _safe_inspection_diagnostics(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    safe = {}
    for key, item in value.items():
        if (
            key in _PUBLIC_DIAGNOSTIC_COUNT_KEYS
            and isinstance(item, int)
            and not isinstance(item, bool)
        ):
            safe[key] = max(item, 0)
    candidates = value.get("candidates")
    if isinstance(candidates, list):
        public_candidates = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            public_candidate = {}
            candidate_id = candidate.get("id")
            if isinstance(candidate_id, str) and re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", candidate_id
            ):
                public_candidate["id"] = _public_identifier(
                    candidate_id, "candidate"
                )
            candidate_type = candidate.get("type")
            if candidate_type in _PUBLIC_LOCATOR_TYPES:
                public_candidate["type"] = candidate_type
            for count_key in ("raw_count", "visible_count", "actionable_count"):
                count = candidate.get(count_key)
                if isinstance(count, int) and not isinstance(count, bool):
                    public_candidate[count_key] = max(count, 0)
            public_candidates.append(public_candidate)
        safe["candidates"] = public_candidates
    return safe


def public_element_inspection(result: object, alias: str, definition: dict) -> dict:
    raw = result if isinstance(result, dict) else {}
    public = {
        "status": "ok" if raw.get("status") == "ok" else "error",
        "alias": alias,
        "scope": definition["scope"],
        "diagnostics": _safe_inspection_diagnostics(raw.get("diagnostics")),
    }
    if public["status"] == "ok":
        candidate = raw.get("candidate")
        if isinstance(candidate, dict):
            candidate_id = candidate.get("id")
            candidate_type = candidate.get("type")
            if (
                isinstance(candidate_id, str)
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", candidate_id)
                and candidate_type in _PUBLIC_LOCATOR_TYPES
            ):
                public["candidate"] = {
                    "id": _public_identifier(candidate_id, "candidate"),
                    "type": candidate_type,
                }
    else:
        code = raw.get("code")
        public["code"] = (
            code
            if code in _PUBLIC_LOCATOR_ERROR_CODES
            else "element_inspection_failed"
        )
    return public


_PUBLIC_STRATEGY_SCOPES = {"active_video", "page", "visible_comment_panel"}
_PUBLIC_ACTION_TYPES = {
    "move",
    "click",
    "scroll_up",
    "scroll_down",
    "keyboard_input",
    "pause",
}
_PUBLIC_EXECUTION_STAGES = {
    "cleanup",
    "close_other_tabs",
    "connect",
    "execute_actions",
    "execution_busy",
    "navigate",
    "prepare_page",
    "session_check",
    "session_start",
    "session_busy",
    "start_browser",
    "tile",
    "validation",
    "wait_for_cdp",
}
_PUBLIC_RECOVERY_STATUSES = {"failed", "recovered"}
_PUBLIC_RECOVERY_OUTCOMES = {
    "not_retried",
    "recovered",
    "replacement_not_found",
    "retry_failed",
}
_PUBLIC_CLOSURE_TYPES = {
    "browser_closed",
    "browser_disconnected",
    "context_closed",
    "page_closed",
    "target_closed",
    "target_detached",
}
_PUBLIC_CLOSURE_REASONS = {
    "browser disconnected",
    "browser has been closed",
    "closed target",
    "context closed",
    "page closed",
    "target closed",
    "target detached",
    "target page, context or browser has been closed",
}
_PUBLIC_FAILURE_MESSAGES = {
    "CDP not ready",
    "CDP wait failed",
    "navigation blew up",
    "navigation failed",
    "navigation settled on unexpected URL: about:blank",
    "runtime failed",
}


def _public_identifier(value: object, prefix: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if prefix == "profile":
        return mask_profile_id(value)
    decoded = unquote(text).casefold()
    normalized = normalize_sensitive_browser_key(text)
    forbidden = (
        is_sensitive_browser_key(text)
        or any(
            marker in normalized
            for marker in (
                "outerhtml",
                "selector",
                "contenteditable",
                "privatecomment",
                "commentcontent",
                "endpoint",
                "devtoolsbrowser",
            )
        )
        or normalized.startswith(("css", "xpath"))
        or any(
            marker in decoded
            for marker in (
                "css=",
                "xpath=",
                "//",
                "ws://",
                "wss://",
                "http://",
                "https://",
                "<",
                ">",
                "[",
                "]",
            )
        )
    )
    if (
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", text)
        and not forbidden
    ):
        return text
    return f"{prefix}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}"


def _public_nonnegative_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _public_nonnegative_number(value: object) -> int | float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _public_http_url(value: object) -> str:
    text = str(value or "").strip()
    return sanitize_public_browser_url(text) if is_valid_browser_url(text) else ""


def _public_origin(value: object) -> str:
    text = str(value or "").strip()
    return sanitize_public_browser_origin(text) if is_valid_browser_url(text) else ""


def _masked_public_switch_identity(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        return ""
    if re.fullmatch(r"[0-9a-f]{12}", value):
        return value
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _canonical_element(action: dict, elements: dict) -> tuple[str, dict | None]:
    params = action.get("params") if isinstance(action.get("params"), dict) else {}
    alias = str(params.get("element") or "")
    definition = elements.get(alias) if isinstance(elements, dict) else None
    return alias, definition if isinstance(definition, dict) else None


def _public_strategy_locator(
    value: object,
    action: dict,
    elements: dict,
) -> dict:
    if not isinstance(value, dict):
        return {}
    alias, definition = _canonical_element(action, elements)
    if not alias or definition is None:
        return {}
    scope = value.get("scope")
    candidate_id = value.get("candidate_id")
    candidate_type = value.get("candidate_type")
    candidates = definition.get("locators")
    if (
        scope != definition.get("scope")
        or scope not in _PUBLIC_STRATEGY_SCOPES
        or not isinstance(candidate_id, str)
        or candidate_type not in _PUBLIC_LOCATOR_TYPES
        or not isinstance(candidates, list)
        or not any(
            isinstance(candidate, dict)
            and candidate.get("id") == candidate_id
            and candidate.get("type") == candidate_type
            for candidate in candidates
        )
    ):
        return {}
    return {
        "scope": scope,
        "candidate_id": _public_identifier(candidate_id, "candidate"),
        "candidate_type": candidate_type,
    }


def _public_strategy_switches(value: object) -> list[dict]:
    if not isinstance(value, (list, tuple)):
        return []
    switches = []
    for item in value:
        if not isinstance(item, dict):
            continue
        before = _masked_public_switch_identity(item.get("from"))
        after = _masked_public_switch_identity(item.get("to"))
        wheel_events = item.get("wheel_events")
        if (
            not before
            or not after
            or isinstance(wheel_events, bool)
            or not isinstance(wheel_events, int)
            or wheel_events < 0
        ):
            continue
        switches.append(
            {
                "from": before,
                "to": after,
                "wheel_events": max(wheel_events, 0),
            }
        )
    return switches


def public_strategy_action_result(
    value: object,
    action: dict,
    action_index: int,
    cycle: int,
    elements: dict,
) -> dict:
    raw = value if isinstance(value, dict) else {}
    action_type = action.get("type")
    public = {
        "action_id": _public_identifier(action.get("id"), "action"),
        "action_index": action_index,
        "cycle": cycle,
        "type": action_type,
        "status": "ok",
    }
    alias, _definition = _canonical_element(action, elements)
    if alias:
        public["element"] = _public_identifier(alias, "element")

    if action_type in {"scroll_up", "scroll_down"}:
        requested = _public_nonnegative_int(raw.get("requested_switches"))
        completed = _public_nonnegative_int(raw.get("completed_switches"))
        wheel_events = _public_nonnegative_int(raw.get("wheel_events"))
        if (
            requested is None
            or completed is None
            or wheel_events is None
            or completed > requested
        ):
            return {}
        public.update(
            {
                "requested_switches": requested,
                "completed_switches": completed,
                "wheel_events": wheel_events,
                "switches": _public_strategy_switches(raw.get("switches")),
            }
        )
        for field in ("count", "distance"):
            item = _public_nonnegative_int(raw.get(field))
            if item is not None:
                public[field] = item
        return public

    if action_type in {"move", "click", "keyboard_input"}:
        locator = _public_strategy_locator(raw.get("locator"), action, elements)
        if locator:
            public["locator"] = locator
    if action_type in {"move", "pause"}:
        duration = _public_nonnegative_number(raw.get("duration_seconds"))
        if duration is not None:
            public["duration_seconds"] = duration
    if action_type == "move" and raw.get("trajectory_source") in {
        "ghost-cursor",
        "recorded-pattern",
    }:
        public["trajectory_source"] = raw["trajectory_source"]
    if action_type == "click":
        params = action.get("params") if isinstance(action.get("params"), dict) else {}
        public["button"] = params.get("button", "left")
        click_count = _public_nonnegative_int(raw.get("click_count"))
        hold_seconds = _public_nonnegative_number(raw.get("hold_seconds"))
        if click_count is not None:
            public["click_count"] = click_count
        if hold_seconds is not None:
            public["hold_seconds"] = hold_seconds
        if raw.get("postcondition") in {"not_configured", "observed"}:
            public["postcondition"] = raw["postcondition"]
        if raw.get("trajectory_source") in {"ghost-cursor", "recorded-pattern"}:
            public["trajectory_source"] = raw["trajectory_source"]
    return public


def _public_strategy_stage(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    stage = value.get("stage")
    status = value.get("status")
    if stage not in _PUBLIC_EXECUTION_STAGES or status not in {"failed", "ok"}:
        return {}
    public = {"stage": stage, "status": status}
    closed_tabs = _public_nonnegative_int(value.get("closed_tabs"))
    if closed_tabs is not None:
        public["closed_tabs"] = closed_tabs
    for key in ("target_url", "current_url"):
        url = _public_http_url(value.get(key))
        if url:
            public[key] = url
    return public


def _canonical_action_occurrence(
    value: object,
    strategy: dict,
) -> tuple[dict, int, int] | None:
    if not isinstance(value, dict):
        return None
    actions = strategy.get("actions")
    if not isinstance(actions, list):
        return None
    action_index = _public_nonnegative_int(value.get("action_index"))
    raw_cycle = value.get("cycle")
    cycle = (
        1
        if strategy.get("run_mode") == "once" and raw_cycle in (None, 1)
        else _public_nonnegative_int(raw_cycle)
    )
    if (
        action_index is None
        or action_index < 1
        or action_index > len(actions)
        or cycle is None
        or cycle < 1
    ):
        return None
    action = actions[action_index - 1]
    if (
        not isinstance(action, dict)
        or value.get("action_id") != action.get("id")
        or value.get("type", value.get("action_type")) != action.get("type")
    ):
        return None
    return action, action_index, cycle


def _public_strategy_recovery(
    value: object,
    strategy: dict,
    *,
    expected_action: tuple[dict, int, int] | None = None,
) -> dict:
    if not isinstance(value, dict):
        return {}
    legacy_reason = value.get("reason")
    if (
        not any(
            key in value for key in ("action_id", "action_index", "action_type", "cycle")
        )
        and legacy_reason in _PUBLIC_CLOSURE_TYPES
    ):
        return {"reason": legacy_reason}
    occurrence = _canonical_action_occurrence(value, strategy)
    if occurrence is None and expected_action is not None:
        expected, expected_index, expected_cycle = expected_action
        supplied_index = value.get("action_index")
        supplied_cycle = value.get("cycle")
        supplied_type = value.get("action_type", value.get("type"))
        if (
            value.get("action_id") == expected.get("id")
            and supplied_index in (None, expected_index)
            and supplied_cycle in (None, expected_cycle)
            and supplied_type in (None, expected.get("type"))
        ):
            occurrence = expected_action
    if occurrence is None:
        return {}
    action, action_index, cycle = occurrence
    if expected_action is not None and (
        action_index != expected_action[1] or cycle != expected_action[2]
    ):
        return {}
    public = {
        "action_id": _public_identifier(action.get("id"), "action"),
        "action_index": action_index,
        "action_type": action.get("type"),
        "cycle": cycle,
    }
    profile_id = _public_identifier(value.get("profile_id"), "profile")
    if profile_id:
        public["profile_id"] = profile_id
    for key in ("old_page_origin", "new_page_origin"):
        origin = _public_origin(value.get(key))
        if origin:
            public[key] = origin
    closure_type = value.get("closure_type")
    if closure_type in _PUBLIC_CLOSURE_TYPES:
        public["closure_type"] = closure_type
    closure_reason = value.get("closure_reason")
    if closure_reason in _PUBLIC_CLOSURE_REASONS:
        public["closure_reason"] = closure_reason
    if isinstance(value.get("replacement_found"), bool):
        public["replacement_found"] = value["replacement_found"]
    retry = _public_nonnegative_int(value.get("retry"))
    if retry is not None:
        public["retry"] = retry
    if value.get("status") in _PUBLIC_RECOVERY_STATUSES:
        public["status"] = value["status"]
    if value.get("outcome") in _PUBLIC_RECOVERY_OUTCOMES:
        public["outcome"] = value["outcome"]
    return public


def _public_strategy_recoveries(
    value: object,
    strategy: dict,
    *,
    expected_action: tuple[dict, int, int] | None = None,
) -> list[dict]:
    if not isinstance(value, (list, tuple)):
        return []
    public = []
    for item in value:
        recovery = _public_strategy_recovery(
            item,
            strategy,
            expected_action=expected_action,
        )
        if recovery:
            public.append(recovery)
    return public


def public_strategy_execution_result(
    result: object,
    strategy: dict,
    elements: dict | None = None,
) -> dict:
    raw = result if isinstance(result, dict) else {}
    elements = elements if isinstance(elements, dict) else {}
    public = {}
    if raw.get("status") == "ok":
        public["status"] = "ok"
    cycles = _public_nonnegative_int(raw.get("cycles"))
    if cycles is not None:
        public["cycles"] = cycles
    sampled_duration = _public_nonnegative_number(raw.get("sampled_duration_minutes"))
    if sampled_duration is not None:
        public["sampled_duration_minutes"] = sampled_duration
    for key in ("target_url", "current_url"):
        url = _public_http_url(raw.get(key))
        if url:
            public[key] = url
    for key in ("closed_tabs", "verified_interactions"):
        item = _public_nonnegative_int(raw.get(key))
        if item is not None:
            public[key] = item
    if isinstance(raw.get("stages"), (list, tuple)):
        public["stages"] = [
            stage
            for item in raw["stages"]
            if (stage := _public_strategy_stage(item))
        ]
    if "page_recoveries" in raw:
        public["page_recoveries"] = _public_strategy_recoveries(
            raw["page_recoveries"],
            strategy,
        )

    actions = raw.get("actions")
    if isinstance(actions, (list, tuple)):
        canonical_actions = strategy.get("actions")
        expected = []
        if (
            isinstance(canonical_actions, list)
            and cycles is not None
            and cycles > 0
        ):
            expected = [
                (cycle, action_index, action)
                for cycle in range(1, cycles + 1)
                for action_index, action in enumerate(canonical_actions, start=1)
            ]
        valid = len(actions) == len(expected)
        if valid:
            for raw_action, (cycle, action_index, action) in zip(actions, expected):
                if (
                    not isinstance(raw_action, dict)
                    or raw_action.get("action_id") != action.get("id")
                    or raw_action.get("type") != action.get("type")
                    or raw_action.get("action_index") != action_index
                    or raw_action.get("cycle") != cycle
                    or raw_action.get("status") != "ok"
                ):
                    valid = False
                    break
        projected_actions = []
        if valid:
            for raw_action, (cycle, action_index, action) in zip(actions, expected):
                projected = public_strategy_action_result(
                    raw_action,
                    action,
                    action_index,
                    cycle,
                    elements,
                )
                if not projected:
                    valid = False
                    break
                projected_actions.append(projected)
        public["actions"] = projected_actions if valid else []
    return public


def _public_locator_diagnostics(value: object, definition: dict) -> dict:
    if not isinstance(value, dict):
        return {}
    public = {
        key: max(item, 0)
        for key, item in value.items()
        if key in _PUBLIC_DIAGNOSTIC_COUNT_KEYS
        and isinstance(item, int)
        and not isinstance(item, bool)
    }
    if value.get("phase") in _PUBLIC_DIAGNOSTIC_PHASES:
        public["phase"] = value["phase"]
    if value.get("container_box") == "missing":
        public["container_box"] = "missing"
    if value.get("scope_target") in {
        "missing_id",
        "page",
        "visible_comment_panel",
    }:
        public["scope_target"] = value["scope_target"]
    candidates = value.get("candidates")
    canonical = {
        (candidate.get("id"), candidate.get("type"))
        for candidate in definition.get("locators", [])
        if isinstance(candidate, dict)
    }
    if isinstance(candidates, list):
        public_candidates = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            identity = (candidate.get("id"), candidate.get("type"))
            if identity not in canonical:
                continue
            item = {
                "id": _public_identifier(identity[0], "candidate"),
                "type": identity[1],
            }
            for key in ("raw_count", "visible_count", "actionable_count"):
                count = _public_nonnegative_int(candidate.get(key))
                if count is not None:
                    item[key] = count
            public_candidates.append(item)
        public["candidates"] = public_candidates
    candidate_identity = (value.get("candidate_id"), value.get("candidate_type"))
    if candidate_identity in canonical:
        public["candidate_id"] = _public_identifier(
            candidate_identity[0], "candidate"
        )
        public["candidate_type"] = candidate_identity[1]
    timeout_seconds = _public_nonnegative_number(value.get("timeout_seconds"))
    if timeout_seconds is not None:
        public["timeout_seconds"] = timeout_seconds
    return public


def _public_strategy_locator_error(
    value: object,
    action: dict,
    elements: dict,
) -> dict:
    if not isinstance(value, dict):
        return {}
    alias, definition = _canonical_element(action, elements)
    code = value.get("code")
    source_alias = value.get("alias")
    if (
        not alias
        or definition is None
        or not (
            source_alias == alias
            or (
                source_alias == ""
                and code
                in {"element_resolution_failed", "element_scope_not_found"}
            )
        )
        or value.get("scope") != definition.get("scope")
    ):
        return {}
    if code not in _PUBLIC_LOCATOR_ERROR_CODES:
        return {}
    public = {
        "code": code,
        "alias": _public_identifier(alias, "element"),
        "scope": definition["scope"],
    }
    diagnostics = _public_locator_diagnostics(value.get("diagnostics"), definition)
    if diagnostics:
        public["diagnostics"] = diagnostics
    return public


def _canonical_failure_occurrence(
    error: BaseException,
    strategy: dict,
) -> tuple[dict, int, int] | None:
    actions = strategy.get("actions")
    action_index = _public_nonnegative_int(getattr(error, "action_index", None))
    if (
        not isinstance(actions, list)
        or action_index is None
        or action_index < 1
        or action_index > len(actions)
    ):
        return None
    action = actions[action_index - 1]
    if (
        not isinstance(action, dict)
        or getattr(error, "action_id", None) != action.get("id")
        or getattr(error, "action_type", None) != action.get("type")
    ):
        return None
    raw_cycle = getattr(error, "cycle", None)
    if strategy.get("run_mode") == "once" and raw_cycle in (None, 1):
        cycle = 1
    else:
        cycle = _public_nonnegative_int(raw_cycle)
        if cycle is None or cycle < 1:
            return None
    return action, action_index, cycle


def _public_completed_strategy_actions(
    value: object,
    strategy: dict,
    elements: dict,
    failure: tuple[dict, int, int] | None,
) -> list[dict]:
    if not isinstance(value, (list, tuple)) or failure is None:
        return []
    failure_cycle, failure_index = failure[2], failure[1]
    previous = (0, 0)
    public_actions = []
    for item in value:
        occurrence = _canonical_action_occurrence(item, strategy)
        if occurrence is None or not isinstance(item, dict) or item.get("status") != "ok":
            return []
        action, action_index, cycle = occurrence
        position = (cycle, action_index)
        if position >= (failure_cycle, failure_index) or position <= previous:
            return []
        public_action = public_strategy_action_result(
            item,
            action,
            action_index,
            cycle,
            elements,
        )
        if not public_action:
            return []
        public_actions.append(public_action)
        previous = position
    return public_actions


def _public_failure_message(error: BaseException, stage: str, code: str = "") -> str:
    if code:
        return code
    raw_reason = str(getattr(error, "reason", error))
    if raw_reason in _PUBLIC_FAILURE_MESSAGES:
        return raw_reason
    reason = raw_reason.casefold()
    if "browser disconnected" in reason:
        return "browser disconnected"
    if stage == "execution_busy":
        return "execution_busy"
    return f"{stage}_failed"


def _public_strategy_gate_reasons(value: object) -> list[dict]:
    if not isinstance(value, (list, tuple)):
        return []
    public = []
    for reason in value:
        if not isinstance(reason, dict):
            continue
        source = reason.get("source")
        reason_code = reason.get("reason_code")
        if (
            source not in {"manual", "probe"}
            or not isinstance(reason_code, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason_code)
        ):
            continue
        aliases = reason.get("aliases")
        item = {
            "source": source,
            "reason_code": reason_code,
            "aliases": [
                _public_identifier(alias, "element")
                for alias in aliases
                if isinstance(alias, str) and alias
            ][:256]
            if isinstance(aliases, (list, tuple))
            else [],
        }
        selector_version_id = reason.get("selector_version_id")
        if isinstance(selector_version_id, str) and len(selector_version_id) <= 128:
            item["selector_version_id"] = _public_identifier(
                selector_version_id,
                "selector-version",
            )
        created_at = reason.get("created_at")
        if isinstance(created_at, str) and re.fullmatch(
            r"[0-9T:+\-Z.]{1,64}",
            created_at,
        ):
            item["created_at"] = created_at
        public.append(item)
    return public


def public_strategy_failure_result(
    *,
    profile_id: object,
    attempts: object,
    target_url: object,
    error: BaseException,
    strategy: dict,
    elements: dict,
    default_stage: str = "execute_actions",
) -> dict:
    raw_stage = getattr(error, "stage", default_stage)
    stage = raw_stage if raw_stage in _PUBLIC_EXECUTION_STAGES else default_stage
    public = {
        "profile_id": _public_identifier(profile_id, "profile"),
        "status": "failed",
        "stage": stage,
        "error": _public_failure_message(error, stage),
        "reason": _public_failure_message(error, stage),
    }
    safe_attempts = _public_nonnegative_int(attempts)
    if safe_attempts is not None:
        public["attempts"] = safe_attempts
    safe_target_url = _public_http_url(target_url)
    if safe_target_url:
        public["target_url"] = safe_target_url
    safe_current_url = _public_http_url(getattr(error, "current_url", ""))
    public["current_url"] = safe_current_url
    if getattr(error, "code", "") == "strategy_paused_during_execution":
        public.update(
            {
                "code": "strategy_paused_during_execution",
                "error": "strategy_paused_during_execution",
                "reason": "strategy_paused_during_execution",
                "gate_reasons": _public_strategy_gate_reasons(
                    getattr(error, "reasons", None)
                ),
            }
        )

    occurrence = _canonical_failure_occurrence(error, strategy)
    if occurrence is not None:
        action, action_index, cycle = occurrence
        public.update(
            {
                "action_id": _public_identifier(action.get("id"), "action"),
                "action_index": action_index,
                "action_type": action.get("type"),
                "cycle": cycle,
            }
        )
        code = getattr(error, "code", None)
        if action.get("type") in {"scroll_up", "scroll_down"}:
            requested = _public_nonnegative_int(
                getattr(error, "requested_switches", None)
            )
            completed = _public_nonnegative_int(
                getattr(error, "completed_switches", None)
            )
            wheel_events = _public_nonnegative_int(
                getattr(error, "wheel_events", None)
            )
            if (
                code in _PUBLIC_VIDEO_SWITCH_ERROR_CODES
                and requested is not None
                and completed is not None
                and wheel_events is not None
                and completed <= requested
            ):
                public.update(
                    {
                        "code": code,
                        "requested_switches": requested,
                        "completed_switches": completed,
                        "wheel_events": wheel_events,
                        "switches": _public_strategy_switches(
                            getattr(error, "switches", None)
                        ),
                    }
                )
        elif action.get("type") in {"move", "click", "keyboard_input"}:
            locator = _public_strategy_locator_error(
                getattr(error, "locator", None),
                action,
                elements,
            )
            if locator and code == locator["code"]:
                public["code"] = code
                public["locator"] = locator
        if public.get("code"):
            public["error"] = public["code"]
            public["reason"] = public["code"]

    recoveries = _public_strategy_recoveries(
        getattr(error, "page_recoveries", None),
        strategy,
        expected_action=occurrence,
    )
    if recoveries:
        public["page_recoveries"] = recoveries
    if hasattr(error, "completed_actions"):
        public["actions"] = _public_completed_strategy_actions(
            getattr(error, "completed_actions"),
            strategy,
            elements,
            occurrence,
        )
    return public


def _public_stored_locator(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    scope = value.get("scope")
    candidate_type = value.get("candidate_type")
    candidate_id = _public_identifier(value.get("candidate_id"), "candidate")
    if (
        scope in _PUBLIC_STRATEGY_SCOPES
        and candidate_type in _PUBLIC_LOCATOR_TYPES
        and candidate_id
    ):
        return {
            "scope": scope,
            "candidate_id": candidate_id,
            "candidate_type": candidate_type,
        }
    code = value.get("code")
    alias = _public_identifier(value.get("alias"), "element")
    if (
        code in _PUBLIC_LOCATOR_ERROR_CODES
        and scope in _PUBLIC_STRATEGY_SCOPES
        and alias
    ):
        public = {"code": code, "alias": alias, "scope": scope}
        diagnostics = value.get("diagnostics")
        if isinstance(diagnostics, dict):
            safe_diagnostics = {
                key: item
                for key, item in diagnostics.items()
                if key in _PUBLIC_DIAGNOSTIC_COUNT_KEYS
                and _public_nonnegative_int(item) is not None
            }
            if diagnostics.get("phase") in _PUBLIC_DIAGNOSTIC_PHASES:
                safe_diagnostics["phase"] = diagnostics["phase"]
            candidate_id = _public_identifier(
                diagnostics.get("candidate_id"), "candidate"
            )
            candidate_type = diagnostics.get("candidate_type")
            if candidate_id and candidate_type in _PUBLIC_LOCATOR_TYPES:
                safe_diagnostics["candidate_id"] = candidate_id
                safe_diagnostics["candidate_type"] = candidate_type
            timeout_seconds = _public_nonnegative_number(
                diagnostics.get("timeout_seconds")
            )
            if timeout_seconds is not None:
                safe_diagnostics["timeout_seconds"] = timeout_seconds
            if safe_diagnostics:
                public["diagnostics"] = safe_diagnostics
        return public
    return {}


def _public_stored_action(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    action_type = value.get("type")
    action_id = _public_identifier(value.get("action_id"), "action")
    action_index = _public_nonnegative_int(value.get("action_index"))
    cycle = _public_nonnegative_int(value.get("cycle"))
    if (
        action_type not in _PUBLIC_ACTION_TYPES
        or value.get("status") != "ok"
        or not action_id
        or action_index is None
        or action_index < 1
        or cycle is None
        or cycle < 1
    ):
        return {}
    public = {
        "action_id": action_id,
        "action_index": action_index,
        "cycle": cycle,
        "type": action_type,
        "status": "ok",
    }
    element = _public_identifier(value.get("element"), "element")
    if element:
        public["element"] = element
    for key in (
        "requested_switches",
        "completed_switches",
        "wheel_events",
        "count",
        "distance",
        "click_count",
    ):
        item = _public_nonnegative_int(value.get(key))
        if item is not None:
            public[key] = item
    for key in ("duration_seconds", "hold_seconds"):
        item = _public_nonnegative_number(value.get(key))
        if item is not None:
            public[key] = item
    if value.get("button") in {"left", "middle", "right"}:
        public["button"] = value["button"]
    if value.get("postcondition") in {"not_configured", "observed"}:
        public["postcondition"] = value["postcondition"]
    if value.get("trajectory_source") in {"ghost-cursor", "recorded-pattern"}:
        public["trajectory_source"] = value["trajectory_source"]
    locator = _public_stored_locator(value.get("locator"))
    if locator:
        public["locator"] = locator
    if action_type in {"scroll_up", "scroll_down"}:
        public["switches"] = _public_strategy_switches(value.get("switches"))
    return public


def _public_stored_recovery(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    if value.get("reason") in _PUBLIC_CLOSURE_TYPES:
        return {"reason": value["reason"]}
    action_id = _public_identifier(value.get("action_id"), "action")
    action_type = value.get("action_type")
    action_index = _public_nonnegative_int(value.get("action_index"))
    cycle = _public_nonnegative_int(value.get("cycle"))
    if (
        not action_id
        or action_type not in _PUBLIC_ACTION_TYPES
        or action_index is None
        or action_index < 1
        or cycle is None
        or cycle < 1
    ):
        return {}
    public = {
        "action_id": action_id,
        "action_index": action_index,
        "action_type": action_type,
        "cycle": cycle,
    }
    profile_id = _public_identifier(value.get("profile_id"), "profile")
    if profile_id:
        public["profile_id"] = profile_id
    for key in ("old_page_origin", "new_page_origin"):
        origin = _public_origin(value.get(key))
        if origin:
            public[key] = origin
    if value.get("closure_type") in _PUBLIC_CLOSURE_TYPES:
        public["closure_type"] = value["closure_type"]
    if value.get("closure_reason") in _PUBLIC_CLOSURE_REASONS:
        public["closure_reason"] = value["closure_reason"]
    if isinstance(value.get("replacement_found"), bool):
        public["replacement_found"] = value["replacement_found"]
    retry = _public_nonnegative_int(value.get("retry"))
    if retry is not None:
        public["retry"] = retry
    if value.get("status") in _PUBLIC_RECOVERY_STATUSES:
        public["status"] = value["status"]
    if value.get("outcome") in _PUBLIC_RECOVERY_OUTCOMES:
        public["outcome"] = value["outcome"]
    return public


def _public_stored_error(value: object, stage: str) -> str:
    text = str(value or "")
    if (
        text in _PUBLIC_LOCATOR_ERROR_CODES
        or text in _PUBLIC_VIDEO_SWITCH_ERROR_CODES
    ):
        return text
    if "browser disconnected" in text.casefold():
        return "browser disconnected"
    if stage == "navigate" and text == "navigation blew up":
        return text
    if text in {"execution_busy", "batch_task_failed"}:
        return text
    return f"{stage}_failed"


def public_browser_batch_result(value: object) -> dict:
    raw = value if isinstance(value, dict) else {}
    stage = (
        raw.get("stage")
        if raw.get("stage") in _PUBLIC_EXECUTION_STAGES
        else "execute_actions"
    )
    status = raw.get("status") if raw.get("status") in {"ok", "failed"} else "failed"
    public = {
        "profile_id": _public_identifier(raw.get("profile_id"), "profile"),
        "status": status,
        "stage": stage,
    }
    for key in ("attempts", "cycles", "closed_tabs", "verified_interactions"):
        item = _public_nonnegative_int(raw.get(key))
        if item is not None:
            public[key] = item
    duration = _public_nonnegative_number(raw.get("sampled_duration_minutes"))
    if duration is not None:
        public["sampled_duration_minutes"] = duration
    strategy_id = _public_identifier(raw.get("strategy_id"), "strategy")
    if strategy_id:
        public["strategy_id"] = strategy_id
    if raw.get("run_mode") in {"once", "loop"}:
        public["run_mode"] = raw["run_mode"]
    for key in ("target_url", "current_url"):
        url = _public_http_url(raw.get(key))
        if url:
            public[key] = url
    if status == "failed":
        gate_paused = (
            raw.get("code") or raw.get("error") or raw.get("reason")
        ) == "strategy_paused_during_execution"
        error = _public_stored_error(
            raw.get("code") or raw.get("error") or raw.get("reason"),
            stage,
        )
        if gate_paused:
            error = "strategy_paused_during_execution"
        public["error"] = error
        public["reason"] = error
        if gate_paused:
            public["code"] = error
            public["gate_reasons"] = _public_strategy_gate_reasons(
                raw.get("gate_reasons")
            )
        action_id = _public_identifier(raw.get("action_id"), "action")
        action_type = raw.get("action_type")
        action_index = _public_nonnegative_int(raw.get("action_index"))
        cycle = _public_nonnegative_int(raw.get("cycle"))
        if (
            action_id
            and action_type in _PUBLIC_ACTION_TYPES
            and action_index is not None
            and action_index > 0
            and cycle is not None
            and cycle > 0
        ):
            public.update(
                {
                    "action_id": action_id,
                    "action_index": action_index,
                    "action_type": action_type,
                    "cycle": cycle,
                }
            )
            if (
                error in _PUBLIC_LOCATOR_ERROR_CODES
                or error in _PUBLIC_VIDEO_SWITCH_ERROR_CODES
            ):
                public["code"] = error
            for key in (
                "requested_switches",
                "completed_switches",
                "wheel_events",
            ):
                item = _public_nonnegative_int(raw.get(key))
                if item is not None:
                    public[key] = item
            if action_type in {"scroll_up", "scroll_down"}:
                public["switches"] = _public_strategy_switches(raw.get("switches"))
            locator = _public_stored_locator(raw.get("locator"))
            if locator:
                public["locator"] = locator
    actions = raw.get("actions")
    if isinstance(actions, (list, tuple)):
        public["actions"] = [
            action for item in actions if (action := _public_stored_action(item))
        ]
    recoveries = raw.get("page_recoveries")
    if isinstance(recoveries, (list, tuple)):
        public["page_recoveries"] = [
            recovery
            for item in recoveries
            if (recovery := _public_stored_recovery(item))
        ]
    stages = raw.get("stages")
    if isinstance(stages, (list, tuple)):
        public["stages"] = [
            stage_item
            for item in stages
            if (stage_item := _public_strategy_stage(item))
        ]
    return public


def public_browser_batch_task(value: object) -> dict:
    raw = value if isinstance(value, dict) else {}
    public = {}
    task_id = _public_identifier(raw.get("id"), "task")
    if task_id:
        public["id"] = task_id
    if raw.get("status") in {
        "queued",
        "running",
        "delayed_gate",
        "completed",
        "failed",
    }:
        public["status"] = raw["status"]
    strategy_id = _public_identifier(raw.get("strategy_id"), "strategy")
    if strategy_id:
        public["strategy_id"] = strategy_id
    for key in (
        "batch_size",
        "total_windows",
        "total_batches",
        "current_batch",
        "completed_batches",
        "processed_windows",
        "failed_windows",
    ):
        item = _public_nonnegative_int(raw.get(key))
        if item is not None:
            public[key] = item
    for key in ("created_at", "finished_at"):
        item = raw.get(key)
        if isinstance(item, str) and re.fullmatch(r"[0-9T:+\-Z.]{1,64}", item):
            public[key] = item
    target_url = _public_http_url(raw.get("target_url"))
    if target_url:
        public["target_url"] = target_url
    if raw.get("error"):
        public["error"] = "batch_task_failed"
    results = raw.get("results")
    public["results"] = (
        [public_browser_batch_result(item) for item in results]
        if isinstance(results, (list, tuple))
        else []
    )
    return public


def prepare_browser_page(ws_url: str, target_url: str) -> dict:
    from browser_cdp import navigate_and_close_other_tabs, wait_for_cdp

    try:
        wait_for_cdp(ws_url, timeout=5.0)
    except BrowserStageError:
        raise
    except Exception as error:
        raise BrowserStageError(
            stage="wait_for_cdp",
            target_url=target_url,
            reason=str(error),
        ) from error
    try:
        navigation = navigate_and_close_other_tabs(ws_url, target_url)
    except BrowserStageError:
        raise
    except Exception as error:
        raise BrowserStageError(
            stage="navigate",
            target_url=target_url,
            reason=str(error),
        ) from error
    current_url = str(navigation.get("current_url") or "").strip()
    if not current_url or current_url == "about:blank":
        raise BrowserStageError(
            stage="navigate",
            target_url=target_url,
            reason=f"目标页准备失败：导航后页面仍为 {current_url or '空地址'}",
            current_url=current_url,
        )
    return {
        "target_url": target_url,
        "current_url": current_url,
        "closed_tabs": int(navigation.get("closed_tabs") or 0),
        "stages": [
            {"stage": "wait_for_cdp", "status": "ok"},
            {
                "stage": "close_other_tabs",
                "status": "ok",
                "closed_tabs": int(navigation.get("closed_tabs") or 0),
            },
            {
                "stage": "navigate",
                "status": "ok",
                "target_url": target_url,
                "current_url": current_url,
            },
        ],
    }


def sanitize_adspower_profile(profile):
    return {
        "profile_id": str(profile.get("profile_id") or profile.get("user_id") or ""),
        "profile_no": str(profile.get("profile_no") or profile.get("serial_number") or ""),
        "name": str(profile.get("name") or ""),
        "group_name": str(profile.get("group_name") or ""),
        "username": str(profile.get("username") or ""),
    }


def fetch_adspower_windows():
    response = requests.get(
        f"{get_adspower_base_url()}/api/v1/user/list",
        params={"page": 1, "page_size": 200},
        headers=get_adspower_headers(),
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") not in {None, 0}:
        raise RuntimeError(
            f"AdsPower request failed: {payload.get('msg') or payload.get('message') or payload.get('code')}"
        )
    data = payload.get("data", payload)
    windows = [
        sanitize_adspower_profile(profile)
        for profile in data.get("list", [])
    ]
    return {"count": len(windows), "windows": windows}


def collect_strategy_comments(data_dir, brand_id=""):
    if brand_id:
        return list_copy_items(data_dir, brand_id)
    comments = []
    for brand in list_brands(data_dir):
        comments.extend(list_copy_items(data_dir, brand.get("id", "")))
    return comments


def strategy_comment_texts(values):
    """Return usable text from the existing copy-library item shapes."""

    texts = []
    for item in values or []:
        if isinstance(item, dict):
            value = item.get("body") or item.get("text") or item.get("content") or ""
            tags = item.get("tags") or []
            if tags:
                value = f"{value}\n\n{' '.join(str(tag) for tag in tags)}"
        else:
            value = item
        value = str(value or "").strip()
        if value:
            texts.append(value)
    return texts


def build_strategy_text_resolver(data_dir, *, rng=None):
    """Resolve fixed text or a random existing copy-library item per input block."""

    rng = rng or random.Random()
    cache = {}

    def resolve(action):
        content = action.get("params", {}).get("content", {})
        if content.get("source") == "fixed":
            return str(content.get("text") or "")
        if content.get("source") != "generated_comment":
            raise ValueError("keyboard content source is invalid")
        brand_id = str(content.get("brand_id") or "").strip()
        if brand_id not in cache:
            cache[brand_id] = strategy_comment_texts(
                collect_strategy_comments(data_dir, brand_id)
            )
        if not cache[brand_id]:
            raise ValueError("内容管理中没有可用文案")
        return rng.choice(cache[brand_id])

    return resolve


def build_execution_v2_content_library_provider(data_dir):
    """Expose existing copy-library metadata through the closed V2 contract."""

    async def provide():
        brands = await asyncio.to_thread(list_brands, data_dir)
        return [
            {
                "id": str(item.get("id") or "").strip(),
                "name": str(item.get("name") or "").strip(),
                "copy_count": item.get("copy_count", 0),
            }
            for item in brands
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        ]

    return provide


def build_execution_v2_text_resolver(data_dir, *, rng=None):
    """Select one existing library item independently for each V2 input action."""

    from execution_v2.actions import ActionExecutionError

    rng = rng or random.Random()

    async def resolve(action):
        library_id = str(action.get("content_library_id") or "").strip()
        if not library_id:
            raise ActionExecutionError("content_library_unavailable")
        values = await asyncio.to_thread(list_copy_items, data_dir, library_id)
        texts = strategy_comment_texts(values)
        if not texts:
            raise ActionExecutionError("content_library_unavailable")
        return rng.choice(texts)

    return resolve


def update_browser_batch_task(task_id, **updates):
    with BROWSER_BATCH_TASKS_LOCK:
        task = BROWSER_BATCH_TASKS.get(task_id)
        if task is None:
            return None
        task.update(updates)
        return dict(task)


def browser_strategy_gate_check(app, strategy_id, _action=None):
    failed_dependencies = app.config.get(
        "SELECTOR_PROBE_DEPENDENCY_SYNC_FAILED",
        set(),
    )
    if strategy_id in failed_dependencies:
        return {
            "strategy_id": strategy_id,
            "allowed": False,
            "effective_status": "paused",
            "reasons": [
                {
                    "source": "probe",
                    "reason_code": "dependency_index_unavailable",
                    "aliases": [],
                    "selector_version_id": "",
                }
            ],
        }
    return check_strategy_gate(
        app.config["SELECTOR_PROBE_GATE_SERVICE_FACTORY"],
        strategy_id,
    )


def _close_app_gate_service(service):
    first_error = None
    seen = set()
    resources = (
        (service,)
        if callable(getattr(service, "close", None))
        else (
            getattr(service, "redis", None),
            getattr(service, "store", None),
        )
    )
    for resource in resources:
        if resource is None or id(resource) in seen:
            continue
        seen.add(id(resource))
        close = getattr(resource, "close", None)
        if not callable(close):
            continue
        try:
            close()
        except BaseException as error:
            if first_error is None:
                first_error = error
    if first_error is not None:
        raise first_error


def _dependency_aware_gate_factory(factory):
    migration_lock = threading.Lock()
    migrated = False

    def open_service():
        nonlocal migrated
        service = factory()
        rebuild = getattr(service, "rebuild_dependencies", None)
        if not callable(rebuild) or migrated:
            return service
        try:
            strategies = load_persisted_strategy_state()[
                "block_strategies"
            ]
            with migration_lock:
                if not migrated:
                    rebuild(strategies)
                    migrated = True
            return service
        except BaseException:
            try:
                _close_app_gate_service(service)
            except BaseException:
                pass
            raise

    return open_service


def _rebuild_strategy_dependencies(app, strategies):
    service = app.config["SELECTOR_PROBE_GATE_SERVICE_FACTORY"]()
    try:
        rebuild = getattr(service, "rebuild_dependencies", None)
        if not callable(rebuild):
            raise RuntimeError("gate service cannot rebuild dependencies")
        rebuild(strategies)
    except BaseException:
        try:
            _close_app_gate_service(service)
        except BaseException:
            pass
        raise
    _close_app_gate_service(service)


def _strategy_ids(strategies):
    return {
        str(strategy.get("id") or "")
        for strategy in strategies
        if isinstance(strategy, dict) and strategy.get("id")
    }


def _rollback_strategy_dependencies(app, previous, candidate):
    affected = _strategy_ids(previous) | _strategy_ids(candidate)
    try:
        _rebuild_strategy_dependencies(app, previous)
    except Exception:
        app.config.setdefault(
            "SELECTOR_PROBE_DEPENDENCY_SYNC_FAILED",
            set(),
        ).update(affected)
        return False
    app.config.setdefault(
        "SELECTOR_PROBE_DEPENDENCY_SYNC_FAILED",
        set(),
    ).difference_update(affected)
    return True


def _strategy_gate_error(strategy_id, decision):
    from browser_strategy_runtime import StrategyPausedError

    return StrategyPausedError(
        strategy_id,
        "",
        0,
        decision.get("reasons", []),
        [],
    )


def _run_prepared_strategy_with_gate(
    runner,
    args,
    gate_check,
    on_action_dispatch=None,
):
    try:
        parameters = inspect.signature(runner).parameters.values()
        parameter_names = {parameter.name for parameter in parameters}
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
    except (TypeError, ValueError):
        parameter_names = set()
        accepts_kwargs = True
    kwargs = {}
    if "gate_check" in parameter_names or accepts_kwargs:
        kwargs["gate_check"] = gate_check
    if (
        on_action_dispatch is not None
        and (
            "on_action_dispatch" in parameter_names
            or accepts_kwargs
        )
    ):
        kwargs["on_action_dispatch"] = on_action_dispatch
    return runner(*args, **kwargs)


def _record_browser_batch_task(task_id):
    with BROWSER_BATCH_TASKS_LOCK:
        stored_task = dict(BROWSER_BATCH_TASKS.get(task_id, {}))
    record_browser_log(
        "batch_task",
        public_browser_payload(
            {
                "task_id": task_id,
                "results": public_browser_batch_task(stored_task)["results"],
            }
        ),
    )


def run_browser_batch_task(app, task_id, profiles, batch_size, strategy, target_url):
    from browser_strategy_runtime import (
        build_batches,
        run_prepared_block_strategy_on_cdp,
    )

    browser = load_persisted_strategy_state()
    elements = browser["action_elements"]
    patterns = browser["interaction_patterns"]
    batches = build_batches(profiles, batch_size)
    strategy_id = str(strategy.get("id") or "").strip()
    action_execution_started = threading.Event()
    action_execution_completed = threading.Event()
    gate_check = lambda strategy_id, action=None: browser_strategy_gate_check(
        app,
        strategy_id,
        action,
    )
    initial_gate = (
        gate_check(strategy_id)
        if strategy_id
        else {"allowed": True, "reasons": []}
    )
    if initial_gate.get("allowed") is not True:
        update_browser_batch_task(
            task_id,
            status="delayed_gate",
            gate_reasons=initial_gate.get("reasons", []),
        )
        _record_browser_batch_task(task_id)
        return
    update_browser_batch_task(task_id, status="running", total_batches=len(batches))
    all_results = []

    try:
        for index, batch in enumerate(batches, start=1):
            current_gate = (
                gate_check(strategy_id)
                if strategy_id
                else {"allowed": True, "reasons": []}
            )
            if current_gate.get("allowed") is not True:
                execution_is_terminal = (
                    action_execution_started.is_set()
                    or action_execution_completed.is_set()
                )
                update_browser_batch_task(
                    task_id,
                    status=(
                        "failed"
                        if execution_is_terminal
                        else "delayed_gate"
                    ),
                    error=(
                        "strategy_paused_during_execution"
                        if execution_is_terminal
                        else ""
                    ),
                    gate_reasons=current_gate.get("reasons", []),
                    finished_at=(
                        content_now_iso()
                        if execution_is_terminal
                        else None
                    ),
                    action_execution_started=(
                        action_execution_started.is_set()
                    ),
                    action_execution_completed=(
                        action_execution_completed.is_set()
                    ),
                )
                _record_browser_batch_task(task_id)
                return
            update_browser_batch_task(
                task_id,
                current_batch=index,
                status="running",
            )
            session_results, layout = ensure_browser_profile_sessions(
                batch, lease_sessions=True
            )
            successful = [
                item for item in session_results if item.get("status") == "ready"
            ]
            ready_profile_ids = [item["profile_id"] for item in successful]

            def run_one(item):
                try:
                    tile_error = browser_tile_error(
                        layout, item["profile_id"], ready_profile_ids
                    )
                    if tile_error:
                        return {
                            "profile_id": item["profile_id"],
                            "status": "failed",
                            "stage": "tile",
                            "error": tile_error,
                        }
                    with browser_profile_execution_reservation(
                        item["profile_id"]
                    ):
                        reserved_gate = (
                            gate_check(strategy_id)
                            if strategy_id
                            else {"allowed": True, "reasons": []}
                        )
                        if reserved_gate.get("allowed") is not True:
                            raise _strategy_gate_error(
                                strategy_id,
                                reserved_gate,
                            )

                        def runtime_gate_check(
                            checked_strategy_id,
                            action=None,
                        ):
                            return gate_check(
                                checked_strategy_id,
                                action,
                            )

                        def on_action_dispatch(
                            _checked_strategy_id,
                            _action,
                        ):
                            action_execution_started.set()

                        raw_result = _run_prepared_strategy_with_gate(
                            run_prepared_block_strategy_on_cdp,
                            (
                                item["ws_url"],
                                target_url,
                                strategy,
                                elements,
                                patterns,
                                build_strategy_text_resolver(
                                    app.config["CONTENT_DATA_DIR"]
                                ),
                            ),
                            runtime_gate_check,
                            on_action_dispatch,
                        )
                        if (
                            isinstance(raw_result, dict)
                            and raw_result.get("actions")
                        ):
                            action_execution_completed.set()
                        result = public_strategy_execution_result(
                            raw_result,
                            strategy,
                            elements,
                        )
                    return {
                        **result,
                        "profile_id": item["profile_id"],
                        "status": "ok",
                        "stage": "execute_actions",
                    }
                except Exception as error:
                    if getattr(error, "completed_actions", None):
                        action_execution_completed.set()
                    return public_strategy_failure_result(
                        profile_id=item["profile_id"],
                        attempts=item.get("attempts", 0),
                        target_url=target_url,
                        error=error,
                        strategy=strategy,
                        elements=elements,
                    )

            try:
                with ThreadPoolExecutor(max_workers=max(len(successful), 1)) as executor:
                    batch_results = (
                        list(executor.map(run_one, successful)) if successful else []
                    )
            finally:
                release_browser_session_results(
                    session_results, request_close=True
                )
            batch_results.extend(
                {
                    "profile_id": item.get("profile_id", ""),
                    "status": "failed",
                    "error": item.get("error", "窗口启动失败"),
                }
                for item in session_results
                if item.get("status") != "ready"
            )
            batch_results = [
                public_browser_batch_result(item) for item in batch_results
            ]
            all_results.extend(batch_results)
            update_browser_batch_task(
                task_id,
                completed_batches=index,
                processed_windows=len(all_results),
                failed_windows=len([item for item in all_results if item.get("status") == "failed"]),
                results=list(all_results),
                action_execution_started=action_execution_started.is_set(),
                action_execution_completed=action_execution_completed.is_set(),
            )
            if any(
                item.get("code")
                == "strategy_paused_during_execution"
                for item in batch_results
            ):
                execution_is_terminal = (
                    action_execution_started.is_set()
                    or action_execution_completed.is_set()
                )
                update_browser_batch_task(
                    task_id,
                    status=(
                        "failed"
                        if execution_is_terminal
                        else "delayed_gate"
                    ),
                    error=(
                        "strategy_paused_during_execution"
                        if execution_is_terminal
                        else ""
                    ),
                    finished_at=(
                        content_now_iso()
                        if execution_is_terminal
                        else None
                    ),
                )
                _record_browser_batch_task(task_id)
                return
        update_browser_batch_task(task_id, status="completed", finished_at=content_now_iso())
    except Exception:
        update_browser_batch_task(
            task_id,
            status="failed",
            error="batch_task_failed",
            results=[public_browser_batch_result(item) for item in all_results],
        )
    _record_browser_batch_task(task_id)


def build_direct_agent_command(payload):
    profile_id = str(payload.get("profile_id") or "").strip()
    profile_no = str(payload.get("profile_no") or "").strip()
    if not profile_id and not profile_no:
        raise ValueError("profile_id or profile_no is required")

    try:
        max_steps = int(payload.get("max_steps") or 10)
    except (TypeError, ValueError) as error:
        raise ValueError("max_steps must be between 1 and 50") from error

    if max_steps < 1 or max_steps > 50:
        raise ValueError("max_steps must be between 1 and 50")

    command = ["npm", "run", "direct-agent", "--"]
    if profile_id:
      command.extend(["--profile-id", profile_id])
    else:
      command.extend(["--profile-no", profile_no])

    url = str(payload.get("url") or "").strip()
    if url:
        command.extend(["--url", url])

    command.extend(["--max-steps", str(max_steps)])

    if payload.get("close_after_run") is False:
        command.append("--no-close")

    return command


def build_search_agent_command(payload):
    url = str(payload.get("url") or "").strip()
    search_xpath = str(payload.get("search_xpath") or "").strip()
    if not url:
        raise ValueError("url is required")
    if not search_xpath:
        raise ValueError("search_xpath is required")

    command = ["npm", "run", "search-agent", "--"]

    profile_ids = str(payload.get("profile_ids") or "").strip()
    profile_nos = str(payload.get("profile_nos") or "").strip()
    if profile_ids:
        command.extend(["--profile-ids", profile_ids])
    elif profile_nos:
        command.extend(["--profile-nos", profile_nos])

    command.extend(["--url", url])

    login_check_xpath = str(payload.get("login_check_xpath") or "").strip()
    if login_check_xpath:
        command.extend(["--login-check-xpath", login_check_xpath])

    command.extend(["--search-xpath", search_xpath])

    query = str(payload.get("query") or "").strip()
    if query:
        command.extend(["--query", query])

    strategy = str(payload.get("strategy") or "rotate").strip()
    if strategy:
        command.extend(["--strategy", strategy])

    if payload.get("close_after_run") is False:
        command.append("--no-close")

    return command


def normalize_execution_strategy(strategy):
    if not isinstance(strategy, dict):
        raise ValueError("strategy must be an object")

    normalized = {
        "id": str(strategy.get("id") or "").strip(),
        "label": str(strategy.get("label") or "").strip(),
        "mouseMoves": int(strategy.get("mouseMoves", 0)),
        "clicks": int(strategy.get("clicks", 0)),
        "scrolls": int(strategy.get("scrolls", 0)),
        "moveSteps": _normalize_number_range(strategy.get("moveSteps"), "moveSteps"),
        "pauseMs": _normalize_number_range(strategy.get("pauseMs"), "pauseMs"),
        "scrollDelta": _normalize_number_range(strategy.get("scrollDelta"), "scrollDelta"),
        "text_prompt": str(
            strategy.get("text_prompt")
            or strategy.get("textPrompt")
            or ""
        ).strip(),
    }
    if not normalized["id"]:
        raise ValueError("strategy id is required")
    if normalized["mouseMoves"] < 0 or normalized["clicks"] < 0 or normalized["scrolls"] < 0:
        raise ValueError("strategy counts must be zero or greater")
    return normalized


def _normalize_number_range(value, field_name):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{field_name} must be a two-item list")
    lower = int(value[0])
    upper = int(value[1])
    if lower < 0 or upper < lower:
        raise ValueError(f"{field_name} range is invalid")
    return [lower, upper]


def normalize_execution_strategies(payload):
    items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    return {"items": [normalize_execution_strategy(item) for item in items]}


def save_execution_strategies(payload):
    normalized = normalize_execution_strategies(payload)
    settings = load_settings()
    settings["execution_strategies"] = normalized
    save_settings(settings)
    return normalized


def select_model_for_generation(settings):
    models = settings.get("models", {})
    target_id = str(models.get("default_model_id") or "").strip()
    enabled_models = [
        item for item in models.get("items", [])
        if item.get("enabled", True)
    ]
    for item in enabled_models:
        if not target_id or item.get("id") == target_id:
            return item
    if enabled_models:
        return enabled_models[0]
    raise ValueError("No enabled model is configured")


def build_strategy_generation_prompt(user_prompt):
    return (
        "请生成浏览器执行策略 JSON 数组。只返回 JSON，不要 Markdown。"
        "每个策略必须包含 id, label, mouseMoves, clicks, scrolls, "
        "moveSteps, pauseMs, scrollDelta, text_prompt。"
        "moveSteps/pauseMs/scrollDelta 必须是两个数字组成的数组。"
        f"\n需求：{user_prompt or '生成三种自然的人类浏览策略'}"
    )


def request_model_text(model_config, prompt):
    base_url = str(model_config.get("base_url") or "").rstrip("/")
    api_key = str(model_config.get("api_key") or "").strip()
    model_name = str(model_config.get("model") or "").strip()
    mode = str(model_config.get("mode") or "chat").strip()
    if not base_url or not api_key or not model_name:
        raise ValueError("model base_url, api_key, and model are required")

    headers = {"Authorization": f"Bearer {api_key}"}
    if mode == "responses":
        response = requests.post(
            f"{base_url}/responses",
            json={
                "model": model_name,
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": prompt}],
                    }
                ],
            },
            headers=headers,
            timeout=60,
        )
    else:
        response = requests.post(
            f"{base_url}/chat/completions",
            json={
                "model": model_name,
                "messages": [{"role": "user", "content": prompt}],
            },
            headers=headers,
            timeout=60,
        )
    response.raise_for_status()
    return extract_model_text(response.json())


def extract_model_text(payload):
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]

    choices = payload.get("choices") or []
    if choices:
        message = choices[0].get("message", {})
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict)
            )

    output = payload.get("output") or []
    texts = []
    for item in output:
        for part in item.get("content", []):
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
    return "\n".join(texts)


def parse_strategy_json_from_text(text):
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("model returned empty content")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\[[\s\S]*\]", raw)
        if not match:
            raise ValueError("model did not return a JSON array")
        parsed = json.loads(match.group(0))
    return normalize_execution_strategies(parsed)


def generate_execution_strategies(prompt):
    settings = load_settings()
    model = select_model_for_generation(settings)
    text = request_model_text(model, build_strategy_generation_prompt(prompt))
    return parse_strategy_json_from_text(text)


def build_proxy_url_from_session(account_id, proxy_session):
    items = parse_proxy_pool(proxy_session)
    protocol = load_settings().get("proxy_pool", {}).get("protocol", "socks5")
    return build_static_proxy_url(items[0], protocol)


def get_proxy_url_for_account(db_path, account_id):
    account = get_buffer_account(db_path, account_id)
    proxy_session = account.get("proxy_session") if account else ""
    if proxy_session:
        try:
            return build_proxy_url_from_session(account_id, proxy_session)
        except ValueError:
            pass

    return generate_proxy_url(account_id)


def create_publish_task(
    *,
    data_dir,
    db_path,
    account_id,
    profile_id,
    video_id,
    brand_id,
    copy_id=None,
    scheduled_at="",
    publish_now=True,
):
    account = get_buffer_account(db_path, account_id)
    video = get_video(data_dir, video_id)
    copy_item = get_copy_item(data_dir, brand_id, copy_id)
    if account is None:
        raise ValueError("account not found")
    if video is None or video.get("used"):
        raise ValueError("video is not available")
    if copy_item is None:
        raise ValueError("copy item not found")

    text = compose_text(copy_item)
    payload = {
        "text": text,
        "profile_ids": [profile_id],
        "media": {"link": video.get("url", "")},
    }
    if scheduled_at:
        payload["scheduled_at"] = scheduled_at

    proxy_url = get_proxy_url_for_account(db_path, account_id)
    task = {
        "account_id": account_id,
        "profile_id": profile_id,
        "video_id": video_id,
        "video_url": video.get("url", ""),
        "brand_id": brand_id,
        "copy_id": copy_item.get("id", ""),
        "scheduled_at": scheduled_at,
        "proxy_session": account.get("proxy_session", ""),
        "copy_text": text,
        "status": "pending",
        "views": 0,
        "comments": 0,
    }

    if not publish_now:
        mark_video_used(data_dir, video_id)
        return save_publish_task(data_dir, task)

    try:
        result = publish_to_buffer(proxy_url, account.get("buffer_token", ""), payload)
        task["status"] = "success" if result.get("success", True) else "failed"
        task["buffer_response"] = result
        task["buffer_update_id"] = result.get("update_id", "")
        task["tiktok_url"] = result.get("tiktok_url", "")
    except requests.exceptions.RequestException as error:
        task["status"] = "failed"
        task["error"] = str(error)

    if task["status"] == "success":
        mark_video_used(data_dir, video_id)
    return save_publish_task(data_dir, task)


def execute_next_publish_task(data_dir, db_path):
    task = next_pending_publish_task(data_dir)
    if task is None:
        return {"processed": False, "task": None}

    update_publish_task(
        data_dir,
        task["id"],
        {"status": "processing", "started_at": content_now_iso()},
    )
    account = get_buffer_account(db_path, task.get("account_id", ""))
    if account is None:
        updated = update_publish_task(
            data_dir,
            task["id"],
            {
                "status": "failed",
                "error": "account not found",
                "finished_at": content_now_iso(),
            },
        )
        return {"processed": True, "task": updated}

    payload = {
        "text": task.get("copy_text", ""),
        "profile_ids": [task.get("profile_id", "")],
        "media": {"link": task.get("video_url", "")},
    }
    if task.get("scheduled_at"):
        payload["scheduled_at"] = task.get("scheduled_at")

    try:
        result = publish_to_buffer(
            get_proxy_url_for_account(db_path, task.get("account_id", "")),
            account.get("buffer_token", ""),
            payload,
        )
        updates = {
            "status": "success" if result.get("success", True) else "failed",
            "buffer_response": result,
            "buffer_update_id": result.get("update_id", ""),
            "tiktok_url": result.get("tiktok_url", ""),
            "finished_at": content_now_iso(),
        }
        if updates["status"] == "failed":
            updates["error"] = result.get("error", "Buffer request failed")
    except requests.exceptions.RequestException as error:
        updates = {
            "status": "failed",
            "error": str(error),
            "finished_at": content_now_iso(),
        }

    return {"processed": True, "task": update_publish_task(data_dir, task["id"], updates)}


def build_tiktok_sampler_command(task):
    selector = str(
        task.get("ads_power_profile_id")
        or task.get("account_id")
        or task.get("profile_id")
        or ""
    ).strip()
    if not selector:
        raise ValueError("profile_id is required for sampling")
    url = str(task.get("tiktok_url") or "").strip()
    if not url:
        raise ValueError("tiktok_url is required for sampling")
    return [
        "npm",
        "run",
        "tiktok-sampler",
        "--",
        "--profile-id",
        selector,
        "--url",
        url,
    ]


def buffer_post_id_for_task(task):
    if task.get("buffer_update_id"):
        return str(task.get("buffer_update_id"))
    response = task.get("buffer_response") or {}
    if isinstance(response, dict):
        if response.get("update_id"):
            return str(response.get("update_id"))
        update_ids = response.get("update_ids") or []
        if update_ids:
            return str(update_ids[0])
    return ""


def execute_next_tiktok_link_backfill(data_dir, db_path):
    task = next_pending_tiktok_link_backfill(data_dir)
    if task is None:
        return {"processed": False, "task": None}

    account = get_buffer_account(db_path, task.get("account_id", ""))
    if account is None:
        return {
            "processed": True,
            "task": mark_tiktok_link_backfill_failure(
                data_dir,
                task["id"],
                "account not found",
            ),
        }

    try:
        buffer_payload = fetch_buffer_post(
            account.get("buffer_token", ""),
            buffer_post_id_for_task(task),
            get_proxy_url_for_account(db_path, task.get("account_id", "")),
        )
        tiktok_url = extract_tiktok_url_from_buffer_payload(buffer_payload)
        if not tiktok_url:
            return {
                "processed": True,
                "task": mark_tiktok_link_backfill_failure(
                    data_dir,
                    task["id"],
                    "Buffer post does not include a TikTok URL yet",
                ),
            }
        return {
            "processed": True,
            "task": mark_tiktok_link_backfill_success(data_dir, task["id"], tiktok_url),
        }
    except requests.exceptions.RequestException as error:
        return {
            "processed": True,
            "task": mark_tiktok_link_backfill_failure(data_dir, task["id"], str(error)),
        }


def execute_next_publish_sample(data_dir, min_age_hours=24):
    task = next_due_publish_sample(data_dir, min_age_hours=min_age_hours)
    if task is None:
        return {"processed": False, "task": None}

    try:
        command = build_tiktok_sampler_command(task)
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=180,
            shell=False,
        )
        if completed.returncode != 0:
            error = (completed.stderr or completed.stdout or "sampler failed").strip()
            return {
                "processed": True,
                "task": mark_publish_sample_failure(data_dir, task["id"], error),
            }
        metrics = json.loads(completed.stdout or "{}")
        return {
            "processed": True,
            "task": mark_publish_sample_success(data_dir, task["id"], metrics),
        }
    except (json.JSONDecodeError, subprocess.SubprocessError, ValueError) as error:
        return {
            "processed": True,
            "task": mark_publish_sample_failure(data_dir, task["id"], str(error)),
        }


def enrich_publish_stats_with_accounts(stats, db_path):
    accounts = account_summary(db_path).get("accounts", [])
    account_names = {}
    profile_names = {}
    for account in accounts:
        display_name = account.get("account_name") or account.get("buffer_account_id") or account.get("id") or ""
        for key in (
            account.get("id"),
            account.get("ads_power_user_id"),
            account.get("buffer_account_id"),
        ):
            if key:
                account_names[str(key)] = display_name
        for profile_id in account.get("buffer_profile_ids") or []:
            profile_names[str(profile_id)] = display_name
        for channel in account.get("buffer_channels") or []:
            channel_id = channel.get("id")
            channel_name = (
                channel.get("descriptor")
                or channel.get("name")
                or channel.get("serviceUsername")
                or display_name
            )
            if channel_id:
                profile_names[str(channel_id)] = str(channel_name)

    for item in stats.get("items", []):
        account_name = account_names.get(str(item.get("account_id") or ""), item.get("account_id", ""))
        item["account_name"] = account_name
        item["tiktok_account_name"] = (
            tiktok_username_from_url(item.get("tiktok_url", ""))
            or profile_names.get(str(item.get("profile_id") or ""), "")
            or account_name
        )
    return stats


def tiktok_username_from_url(url):
    match = re.search(r"(?:^|/)@([^/?#]+)", str(url or ""))
    if not match:
        return ""
    username = match.group(1).strip()
    return f"@{username}" if username else ""


def publish_sampling_options():
    settings = load_settings()
    sampling = settings.get("publish_sampling", {})
    interval = sampling.get("interval_seconds", 300)
    min_age_hours = sampling.get("min_age_hours", 24)
    try:
        interval = max(int(interval or 300), 30)
    except (TypeError, ValueError):
        interval = 300
    try:
        min_age_hours = max(int(24 if min_age_hours is None else min_age_hours), 0)
    except (TypeError, ValueError):
        min_age_hours = 24
    return {
        "enabled": sampling.get("enabled", True) is not False,
        "interval_seconds": interval,
        "min_age_hours": min_age_hours,
    }


def execute_publish_sampling_tick(data_dir, db_path):
    options = publish_sampling_options()
    if not options["enabled"]:
        return {"enabled": False, "backfill": None, "sample": None, "options": options}
    return {
        "enabled": True,
        "options": options,
        "backfill": execute_next_tiktok_link_backfill(data_dir, db_path),
        "sample": execute_next_publish_sample(
            data_dir,
            min_age_hours=options["min_age_hours"],
        ),
    }


_publish_worker_started = False
_publish_sampling_worker_started = False


def public_settings(settings: dict) -> dict:
    """Return settings safe to place in API responses and the browser DOM."""

    public = copy.deepcopy(settings)
    public["_secrets_configured"] = {
        "proxy": {
            "password": bool(settings.get("proxy", {}).get("password")),
        },
        "proxy_pool": {
            "raw": bool(settings.get("proxy_pool", {}).get("raw")),
        },
        "r2": {
            key: bool(settings.get("r2", {}).get(key))
            for key in ("account_token", "access_key_id", "secret_access_key")
        },
        "adspower": {
            "api_key": bool(settings.get("adspower", {}).get("api_key")),
        },
        "models": {
            "items": [
                {"api_key": bool(item.get("api_key"))}
                for item in settings.get("models", {}).get("items", [])
                if isinstance(item, dict)
            ],
        },
        "selector_probe": {
            "webhook": {
                "signing_secret": bool(
                    settings.get("selector_probe", {})
                    .get("webhook", {})
                    .get("signing_secret")
                ),
            },
        },
    }
    public.pop("selector_probe", None)
    public.get("proxy", {})["password"] = ""
    proxy_pool = public.get("proxy_pool", {})
    proxy_pool["raw"] = ""
    proxy_pool["items"] = []
    r2 = public.get("r2", {})
    for key in ("account_token", "access_key_id", "secret_access_key"):
        r2[key] = ""
    public.get("adspower", {})["api_key"] = ""
    for item in public.get("models", {}).get("items", []):
        if isinstance(item, dict):
            item["api_key"] = ""
    return public


def start_publish_queue_worker(app):
    global _publish_worker_started
    if _publish_worker_started:
        return
    _publish_worker_started = True

    def worker_loop():
        while True:
            settings = load_settings()
            interval = settings.get("publish_queue", {}).get("interval_seconds", 8)
            try:
                interval = max(int(interval or 8), 1)
            except (TypeError, ValueError):
                interval = 8

            try:
                execute_next_publish_task(
                    app.config["CONTENT_DATA_DIR"],
                    app.config["ACCOUNTS_DB_PATH"],
                )
                execute_next_tiktok_link_backfill(
                    app.config["CONTENT_DATA_DIR"],
                    app.config["ACCOUNTS_DB_PATH"],
                )
                execute_next_publish_sample(app.config["CONTENT_DATA_DIR"])
            except Exception:
                pass

            time.sleep(interval)

    thread = threading.Thread(target=worker_loop, name="publish-queue-worker", daemon=True)
    thread.start()


def start_publish_sampling_worker(app):
    global _publish_sampling_worker_started
    if _publish_sampling_worker_started:
        return
    _publish_sampling_worker_started = True

    def worker_loop():
        while True:
            options = publish_sampling_options()
            try:
                execute_publish_sampling_tick(
                    app.config["CONTENT_DATA_DIR"],
                    app.config["ACCOUNTS_DB_PATH"],
                )
            except Exception:
                pass

            time.sleep(options["interval_seconds"])

    thread = threading.Thread(target=worker_loop, name="publish-sampling-worker", daemon=True)
    thread.start()


def select_publish_accounts(accounts, account_ids):
    if not account_ids:
        return accounts

    by_id = {}
    for account in accounts:
        for key in (
            account.get("id"),
            account.get("ads_power_user_id"),
            account.get("buffer_account_id"),
        ):
            if key:
                by_id[str(key)] = account

    selected = []
    for account_id in account_ids:
        account = by_id.get(str(account_id))
        if account and account not in selected:
            selected.append(account)
    return selected


def _local_direct_mode_enabled(config: dict | None) -> bool:
    """Config wins; environment accepts only explicit local-direct values."""

    if config is not None and "LOCAL_DIRECT_MODE" in config:
        value = config["LOCAL_DIRECT_MODE"]
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().casefold() in {"1", "true", "yes", "on"}
        return False
    return os.getenv("LOCAL_DIRECT_MODE", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    local_direct_mode = _local_direct_mode_enabled(config)
    explicit_gate_factory = bool(
        config and "SELECTOR_PROBE_GATE_SERVICE_FACTORY" in config
    )
    app.config["PROXY_CONFIG"] = load_proxy_config()
    app.config.setdefault("ACCOUNTS_DB_PATH", "accounts.db")
    app.config.setdefault("CONTENT_DATA_DIR", DEFAULT_CONTENT_DIR)
    app.config.setdefault("MAX_CONTENT_LENGTH", 10 * 1024 * 1024)
    stats_root = Path(__file__).resolve().parents[1] / "data" / "stats"
    app.config.setdefault("TIKTOK_STATS_DB_PATH", stats_root / "tiktok_stats.db")
    app.config.setdefault("TIKTOK_STATS_COOKIE_PATH", stats_root / "tiktok_cookie.json")
    app.config.setdefault("TIKTOK_STATS_QUERY_FACTORY", default_query_factory)
    app.config.setdefault("TIKTOK_STATS_STORE_FACTORY", default_store_factory)
    app.config.setdefault("TIKTOK_STATS_SECRET_STORE_FACTORY", default_secret_store_factory)
    app.config.setdefault("TIKTOK_STATS_COOKIE_VALIDATOR", unavailable_cookie_validator)
    app.config.setdefault("TIKTOK_STATS_RUN_DISPATCHER", unavailable_run_dispatcher)
    app.config.setdefault("TIKTOK_STATS_STATUS_PROVIDER", default_status_provider)
    app.config.setdefault(
        "SELECTOR_PROBE_STORE_FACTORY",
        default_selector_probe_store_factory,
    )
    app.config.setdefault(
        "SELECTOR_PROBE_REGISTRY_FACTORY",
        default_selector_probe_registry_factory,
    )
    app.config.setdefault(
        "SELECTOR_PROBE_RUN_DISPATCHER",
        default_selector_probe_dispatcher,
    )
    if config:
        app.config.update(config)
    app.config["LOCAL_DIRECT_MODE"] = local_direct_mode
    app.config.setdefault("SERVER_PORT", 5000)
    app.config.setdefault(
        "EXECUTION_V2_DB_PATH",
        Path(__file__).resolve().parents[1]
        / "data"
        / "execution_v2"
        / "execution_v2.db",
    )
    app.config.setdefault(
        "EXECUTION_V2_EVIDENCE_DIR",
        Path(__file__).resolve().parents[1] / "data" / "execution_v2" / "evidence",
    )
    app.config.setdefault("EXECUTION_V2_SERVICE_FACTORY", None)
    app.config.setdefault(
        "COMMENT_CAMPAIGN_DB_URL",
        "sqlite:///data/comment_campaign/comment_campaign.db",
    )
    app.config.setdefault(
        "COMMENT_CAMPAIGN_EVIDENCE_DIR",
        "data/comment_campaign/evidence",
    )
    app.config.setdefault(
        "COMMENT_CAMPAIGN_REDIS_URL",
        os.getenv(
            "COMMENT_CAMPAIGN_REDIS_URL",
            os.getenv("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0"),
        ),
    )
    app.config.setdefault("COMMENT_CAMPAIGN_SERVICE_FACTORY", None)
    if not local_direct_mode:
        app.config.setdefault("MANAGEMENT_STATE_DIR", Path("data"))
        app.config.setdefault(
            "MANAGEMENT_DB_PATH",
            Path(app.config["MANAGEMENT_STATE_DIR"]) / "management.db",
        )
    app.config.update(
        SECRET_KEY=(
            secrets.token_urlsafe(48)
            if local_direct_mode
            else load_or_create_session_key(
                Path(app.config["MANAGEMENT_STATE_DIR"]) / "session.key"
            )
        ),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=bool(
            app.config.get("PUBLIC_ORIGIN_HTTPS", False)
        ),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    )
    app.config.setdefault(
        "SELECTOR_PROBE_DEPENDENCY_SYNC_FAILED",
        set(),
    )
    if explicit_gate_factory:
        base_gate_factory = app.config[
            "SELECTOR_PROBE_GATE_SERVICE_FACTORY"
        ]
    else:
        selector_probe_store_factory = app.config[
            "SELECTOR_PROBE_STORE_FACTORY"
        ]

        def base_gate_factory():
            return default_selector_probe_gate_service_factory(
                store_factory=selector_probe_store_factory,
            )

    app.config["SELECTOR_PROBE_GATE_SERVICE_FACTORY"] = (
        _dependency_aware_gate_factory(base_gate_factory)
    )
    app.config.setdefault(
        "TIKTOK_STATS_EXISTING_ACCOUNTS_DB_PATH", app.config["ACCOUNTS_DB_PATH"]
    )

    if not local_direct_mode:
        def management_auth_service_factory():
            service = g.get("management_auth_service")
            if service is None:
                connection = open_management_db(
                    Path(app.config["MANAGEMENT_DB_PATH"])
                )
                g.management_auth_connection = connection
                service = AuthService(AuthStore(connection))
                g.management_auth_service = service
            return service

        @app.teardown_appcontext
        def close_management_auth_connection(_error):
            connection = g.pop("management_auth_connection", None)
            g.pop("management_auth_service", None)
            if connection is not None:
                connection.close()

        app.extensions["management_auth_service_factory"] = (
            management_auth_service_factory
        )
    register_tiktok_stats_error_handler(app)
    if local_direct_mode:
        install_local_only_guard(app)

        @app.before_request
        def ensure_local_direct_csrf():
            csrf_token = session.get("csrf_token")
            if not isinstance(csrf_token, str) or not csrf_token:
                session["csrf_token"] = secrets.token_urlsafe(32)
    else:
        app.register_blueprint(
            create_auth_blueprint(management_auth_service_factory)
        )
        install_management_guard(app, management_auth_service_factory)

    execution_v2_lock = threading.Lock()

    def execution_v2_service_factory():
        service = app.extensions.get("execution_v2_service")
        if service is None:
            with execution_v2_lock:
                service = app.extensions.get("execution_v2_service")
                if service is None:
                    configured_factory = app.config["EXECUTION_V2_SERVICE_FACTORY"]
                    if configured_factory is None:
                        from execution_v2.service import create_default_execution_v2_service

                        adspower_settings = load_settings().get("adspower", {})
                        controller = AdsPowerController(
                            base_url=(
                                adspower_settings.get("base_url")
                                or os.getenv("ADSPOWER_BASE_URL")
                            ),
                            api_key=(
                                adspower_settings.get("api_key")
                                or os.getenv("ADSPOWER_API_KEY", "")
                            ),
                        )
                        service = create_default_execution_v2_service(
                            db_path=app.config["EXECUTION_V2_DB_PATH"],
                            evidence_dir=app.config["EXECUTION_V2_EVIDENCE_DIR"],
                            controller=controller,
                            content_library_provider=(
                                build_execution_v2_content_library_provider(
                                    app.config["CONTENT_DATA_DIR"]
                                )
                            ),
                            text_resolver=build_execution_v2_text_resolver(
                                app.config["CONTENT_DATA_DIR"]
                            ),
                        )
                    else:
                        service = configured_factory()
                    app.extensions["execution_v2_service"] = service
        return service

    def close_execution_v2_service():
        with execution_v2_lock:
            service = app.extensions.pop("execution_v2_service", None)
            close = getattr(service, "close", None)
            if callable(close):
                close()

    app.extensions["execution_v2_service_factory"] = execution_v2_service_factory
    app.extensions["execution_v2_close"] = close_execution_v2_service
    app.register_blueprint(create_browser_v2_blueprint(execution_v2_service_factory))

    comment_campaign_lock = threading.Lock()

    def comment_campaign_service_factory():
        service = app.extensions.get("comment_campaign_service")
        if service is None:
            with comment_campaign_lock:
                service = app.extensions.get("comment_campaign_service")
                if service is None:
                    configured_factory = app.config["COMMENT_CAMPAIGN_SERVICE_FACTORY"]
                    if configured_factory is None:
                        from comment_campaign.service import (
                            create_default_comment_campaign_service,
                        )
                        from comment_campaign.queueing import QueueCoordinator

                        adspower_settings = load_settings().get("adspower", {})
                        controller = AdsPowerController(
                            base_url=(
                                adspower_settings.get("base_url")
                                or os.getenv("ADSPOWER_BASE_URL")
                            ),
                            api_key=(
                                adspower_settings.get("api_key")
                                or os.getenv("ADSPOWER_API_KEY", "")
                            ),
                        )
                        health_controller = AdsPowerController(
                            base_url=(
                                adspower_settings.get("base_url")
                                or os.getenv("ADSPOWER_BASE_URL")
                            ),
                            api_key=(
                                adspower_settings.get("api_key")
                                or os.getenv("ADSPOWER_API_KEY", "")
                            ),
                            timeout=1.0,
                            max_retries=1,
                            retry_delay=0,
                        )

                        def adspower_probe():
                            list_one = getattr(health_controller, "list_profiles", None)
                            if callable(list_one):
                                list_one(page=1, page_size=1)
                                return []
                            list_all = getattr(
                                health_controller, "list_all_profiles", None
                            )
                            if not callable(list_all):
                                raise RuntimeError("adspower probe unavailable")
                            try:
                                list_all(max_profiles=1)
                            except TypeError:
                                list_all()
                            return []

                        def profile_provider():
                            list_all = getattr(controller, "list_all_profiles", None)
                            if callable(list_all):
                                profiles = list_all()
                            else:
                                profiles = controller.list_profiles()
                            return [
                                {
                                    "id": str(item.get("id") or ""),
                                    "name": str(item.get("name") or ""),
                                    "status": str(item.get("status") or ""),
                                }
                                for item in profiles
                                if isinstance(item, dict)
                            ]

                        def content_resolver(library_id):
                            return [
                                {
                                    "content_item_id": str(item.get("id") or ""),
                                    "text": compose_text(item),
                                }
                                for item in list_copy_items(
                                    app.config["CONTENT_DATA_DIR"], library_id
                                )
                                if isinstance(item, dict)
                            ]

                        def publish_result_resolver(reference):
                            for item in public_publish_tasks(
                                app.config["CONTENT_DATA_DIR"]
                            ):
                                if (
                                    str(item.get("id") or "") == reference
                                    and item.get("status") == "success"
                                    and str(item.get("tiktok_url") or "").strip()
                                ):
                                    return str(item.get("tiktok_url") or "")
                            return ""

                        def comment_settings_provider():
                            settings = load_settings()
                            campaign = settings.get("comment_campaign", {})
                            return campaign if isinstance(campaign, dict) else {}

                        def comment_settings_updater(expected_revision, bindings):
                            from comment_campaign.errors import RevisionConflictError
                            from comment_campaign.errors import CampaignValidationError
                            from execution_v2.store import ExecutionStore

                            required_kinds = {
                                "entry_element_id": "click",
                                "input_element_id": "input",
                                "submit_element_id": "click",
                                "account_element_id": "click",
                            }
                            element_store = ExecutionStore(
                                app.config["EXECUTION_V2_DB_PATH"]
                            )
                            try:
                                element_store.initialize()
                                for name, kind in required_kinds.items():
                                    element = element_store.get_element(bindings[name])
                                    if (
                                        not isinstance(element, dict)
                                        or element.get("status") != "active"
                                        or element.get("kind") != kind
                                    ):
                                        raise CampaignValidationError("comment_panel_not_ready")
                            finally:
                                # ExecutionStore currently opens short-lived SQLite sessions per
                                # call.  Keep this defensive close so a future persistent store
                                # implementation cannot leak from a settings request.
                                close = getattr(element_store, "close", None)
                                if callable(close):
                                    close()

                            result = {}

                            def persist(settings):
                                campaign = settings.get("comment_campaign", {})
                                campaign = dict(campaign) if isinstance(campaign, dict) else {}
                                current = campaign.get("revision", 1)
                                current = current if type(current) is int and current >= 1 else 1
                                if current != expected_revision:
                                    raise RevisionConflictError("comment-settings")
                                campaign["element_bindings"] = dict(bindings)
                                campaign["revision"] = current + 1
                                settings["comment_campaign"] = campaign
                                result.update(campaign)
                                return settings

                            mutate_settings(persist)
                            return result

                        service = create_default_comment_campaign_service(
                            database_url=app.config["COMMENT_CAMPAIGN_DB_URL"],
                            profile_provider=profile_provider,
                            content_resolver=content_resolver,
                            publish_result_resolver=publish_result_resolver,
                            queue_coordinator=QueueCoordinator.from_url(
                                app.config["COMMENT_CAMPAIGN_REDIS_URL"]
                            ),
                            settings_provider=comment_settings_provider,
                            adspower_probe=adspower_probe,
                            settings_updater=comment_settings_updater,
                        )
                    else:
                        service = configured_factory()
                    app.extensions["comment_campaign_service"] = service
        return service

    def close_comment_campaign_service():
        with comment_campaign_lock:
            service = app.extensions.pop("comment_campaign_service", None)
            close = getattr(service, "close", None)
            if callable(close):
                close()

    app.extensions["comment_campaign_service_factory"] = (
        comment_campaign_service_factory
    )
    app.extensions["comment_campaign_close"] = close_comment_campaign_service
    app.register_blueprint(
        create_comment_campaign_blueprint(comment_campaign_service_factory)
    )
    app.register_blueprint(create_tiktok_stats_blueprint())
    app.register_blueprint(
        create_selector_probe_blueprint(
            store_factory=app.config["SELECTOR_PROBE_STORE_FACTORY"],
            registry_factory=app.config[
                "SELECTOR_PROBE_REGISTRY_FACTORY"
            ],
            gate_service_factory=app.config[
                "SELECTOR_PROBE_GATE_SERVICE_FACTORY"
            ],
            run_dispatcher=app.config[
                "SELECTOR_PROBE_RUN_DISPATCHER"
            ],
        )
    )
    sanitize_browser_log_file()

    @app.after_request
    def sanitize_browser_api_response(response):
        if (
            request.path.startswith("/api/browser/")
            and request.path != "/api/browser/adspower-windows"
            and response.is_json
        ):
            payload = response.get_json(silent=True)
            if payload is not None:
                response.set_data(app.json.dumps(public_browser_payload(payload)))
                response.mimetype = "application/json"
        return response

    @app.errorhandler(RequestEntityTooLarge)
    def upload_too_large(_error):
        return jsonify({"error": "导入文件不能超过 10 MB"}), 413

    @app.get("/")
    def dashboard_page():
        return render_template_string(
            CONTROL_PAGE_HTML,
            active_nav="settings",
            csrf_token=session["csrf_token"],
        )

    @app.get("/browser-v2")
    def browser_v2_page():
        return render_template("browser_v2.html")

    @app.get("/comment-campaigns")
    def comment_campaign_page():
        return render_template(
            "comment_campaign.html",
            active_nav="comment-campaign",
            csrf_token=session["csrf_token"],
        )

    @app.get("/evidence/<filename>")
    def execution_v2_evidence(filename: str):
        if re.fullmatch(r"[0-9a-f]{32}\.png", filename) is None:
            abort(404)
        return send_from_directory(
            app.config["EXECUTION_V2_EVIDENCE_DIR"], filename
        )

    @app.get("/comment-campaign-evidence/<filename>")
    def comment_campaign_evidence(filename: str):
        if re.fullmatch(r"[0-9a-f]{32}\.png", filename) is None:
            abort(404)
        evidence_dir = Path(app.config["COMMENT_CAMPAIGN_EVIDENCE_DIR"]).resolve()
        candidate = evidence_dir / filename
        if (
            candidate.is_symlink()
            or not candidate.is_file()
            or candidate.resolve().parent != evidence_dir
        ):
            abort(404)
        response = send_from_directory(evidence_dir, filename)
        response.cache_control.no_store = True
        return response

    @app.get("/ping")
    def ping():
        return jsonify({"status": "ok"})

    @app.get("/api/status")
    def get_status():
        settings = load_settings()
        proxy = settings["proxy"]
        proxy_pool = settings.get("proxy_pool", {})
        services = settings["services"]
        browser = settings["browser"]
        adspower = settings.get("adspower", {})
        single_proxy_configured = all(
            proxy.get(key)
            for key in ("host", "port", "username", "password")
        )
        proxy_pool_configured = bool(proxy_pool.get("items"))

        return jsonify(
            {
                "service": {"running": True},
                "config": {
                    "proxy_configured": single_proxy_configured
                    or proxy_pool_configured,
                    "services_configured": bool(
                        services.get("ipinfo_url")
                        and services.get("buffer_graphql_url")
                    ),
                    "browser_configured": bool(
                        browser.get("cdp_url")
                        or (
                            adspower.get("base_url")
                            and browser.get("default_url")
                        )
                    ),
                },
                "browser": {
                    "cdp_url": browser.get("cdp_url", ""),
                    "task_goal": browser.get("task_goal", ""),
                },
            }
        )

    @app.post("/api/browser/sync-tabs")
    def sync_browser_tabs_route():
        payload = request.get_json(silent=True) or {}
        url = str(payload.get("url") or "").strip()
        if not url:
            return jsonify({"error": "url 不能为空"}), 400
        sessions = selected_browser_sessions(payload.get("windows"))
        if not sessions or any(not ws_url for _profile_id, ws_url in sessions):
            release_selected_browser_sessions(sessions)
            return jsonify({"error": "没有找到已打开窗口，请先打开并平铺窗口"}), 400

        def sync_one(item):
            profile_id, ws_url = item
            try:
                from browser_cdp import navigate_and_close_other_tabs

                return {"profile_id": profile_id, "status": "ok", **navigate_and_close_other_tabs(ws_url, url)}
            except Exception as error:
                return {"profile_id": profile_id, "status": "failed", "error": str(error)}

        try:
            with ThreadPoolExecutor(max_workers=len(sessions)) as executor:
                results = list(executor.map(sync_one, sessions))
        finally:
            release_selected_browser_sessions(sessions)
        response_payload = {"url": url, "results": results}
        record_browser_log("sync_tabs", response_payload)
        return jsonify(response_payload)

    def inspect_browser_elements_response(payload, *, use_saved_elements: bool = False):
        elements = payload.get("elements") if isinstance(payload, dict) else None
        if not isinstance(elements, dict) and use_saved_elements:
            elements = load_settings().get("browser", {}).get("action_elements", {})
        try:
            elements = normalize_element_definitions(elements)
        except (TypeError, ValueError):
            return jsonify({"error": "invalid element definitions"}), 400
        if not elements:
            return jsonify({"error": "at least one element definition is required"}), 400
        try:
            sessions = selected_browser_sessions(payload.get("windows"))
        except (AttributeError, TypeError):
            return jsonify({"error": "invalid browser window selection"}), 400
        if not sessions or any(not ws_url for _profile_id, ws_url in sessions):
            release_selected_browser_sessions(sessions)
            return jsonify({"error": "没有找到已打开窗口，请先打开并平铺窗口"}), 400

        def inspect_one(item):
            profile_id, ws_url = item
            try:
                inspected = inspect_browser_elements_on_cdp(ws_url, elements)
                if not isinstance(inspected, list):
                    inspected = []
                return {
                    "profile_id": profile_id,
                    "status": "ok",
                    "elements": [
                        public_element_inspection(
                            inspected[index]
                            if index < len(inspected)
                            else {"status": "error"},
                            alias,
                            definition,
                        )
                        for index, (alias, definition) in enumerate(elements.items())
                    ],
                }
            except Exception:
                return {
                    "profile_id": profile_id,
                    "status": "failed",
                    "code": "element_inspection_failed",
                    "elements": [
                        public_element_inspection(
                            {"status": "error"}, alias, definition
                        )
                        for alias, definition in elements.items()
                    ],
                }

        try:
            results = [inspect_one(item) for item in sessions]
        finally:
            release_selected_browser_sessions(sessions)
        response_payload = {"results": results}
        record_browser_log("inspect_elements", response_payload)
        return jsonify(response_payload)

    @app.post("/api/browser/elements/test")
    def test_browser_elements_route():
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"error": "element inspection payload must be a JSON object"}), 400
        return inspect_browser_elements_response(payload)

    @app.post("/api/browser/read-elements")
    def read_browser_elements_route():
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"error": "element inspection payload must be a JSON object"}), 400
        return inspect_browser_elements_response(payload, use_saved_elements=True)

    @app.post("/api/browser/execute-strategy")
    def execute_browser_strategy_route():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "请求格式无效，必须是 JSON 对象"}), 400
        try:
            browser = load_persisted_strategy_state()
        except (TypeError, ValueError) as error:
            return jsonify({"error": str(error)}), 400
        elements = browser["action_elements"]
        patterns = browser["interaction_patterns"]
        strategy_id = str(payload.get("strategy_id") or "")
        target_url = get_browser_target_url(payload)
        if not is_valid_browser_url(target_url):
            return jsonify({"error": "url must be a valid http:// or https:// URL"}), 400
        strategy = next(
            (
                item
                for item in browser["block_strategies"]
                if item.get("id") == strategy_id
            ),
            None,
        )
        if not strategy:
            return jsonify({"error": "执行策略不存在，请到执行策略模块保存后再试"}), 400
        if strategy.get("status") == "needs_repair":
            detail = "; ".join(strategy.get("repair_errors") or [])
            return jsonify({"error": f"strategy needs repair before execution: {detail}"}), 400
        initial_gate = browser_strategy_gate_check(app, strategy["id"])
        if initial_gate.get("allowed") is not True:
            return jsonify(
                {
                    "error": "strategy_paused",
                    "code": "strategy_paused",
                    "strategy_id": _public_identifier(
                        strategy["id"],
                        "strategy",
                    ),
                    "reasons": initial_gate.get("reasons", []),
                }
            ), 409
        try:
            profiles = normalize_selected_browser_profiles(payload.get("windows"))
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
        session_results, _layout = ensure_browser_profile_sessions(
            profiles, lease_sessions=True
        )
        ready_profile_ids = [
            item["profile_id"]
            for item in session_results
            if item.get("status") == "ready"
        ]

        def execute_one(item):
            profile_id = item["profile_id"]
            attempts = item["attempts"]
            if item.get("status") != "ready":
                return {
                    "profile_id": profile_id,
                    "status": "failed",
                    "stage": item["stage"],
                    "attempts": attempts,
                    "target_url": target_url,
                    "error": item.get("error", ""),
                }
            tile_error = browser_tile_error(
                _layout, profile_id, ready_profile_ids
            )
            if tile_error:
                return {
                    "profile_id": profile_id,
                    "status": "failed",
                    "stage": "tile",
                    "attempts": attempts,
                    "target_url": target_url,
                    "error": tile_error,
                }
            ws_url = item["ws_url"]
            try:
                from browser_strategy_runtime import (
                    run_prepared_block_strategy_on_cdp,
                )

                with browser_profile_execution_reservation(profile_id):
                    gate_check = (
                        lambda checked_strategy_id, action=None:
                        browser_strategy_gate_check(
                            app,
                            checked_strategy_id,
                            action,
                        )
                    )
                    reserved_gate = gate_check(strategy["id"])
                    if reserved_gate.get("allowed") is not True:
                        raise _strategy_gate_error(
                            strategy["id"],
                            reserved_gate,
                        )
                    result = public_strategy_execution_result(
                        _run_prepared_strategy_with_gate(
                            run_prepared_block_strategy_on_cdp,
                            (
                                ws_url,
                                target_url,
                                strategy,
                                elements,
                                patterns,
                                build_strategy_text_resolver(
                                    app.config["CONTENT_DATA_DIR"]
                                ),
                            ),
                            gate_check,
                        ),
                        strategy,
                        elements,
                    )
                return {
                    **result,
                    "profile_id": profile_id,
                    "status": "ok",
                    "stage": "execute_actions",
                    "attempts": attempts,
                    "target_url": target_url,
                }
            except Exception as error:
                return public_strategy_failure_result(
                    profile_id=profile_id,
                    attempts=attempts,
                    target_url=target_url,
                    error=error,
                    strategy=strategy,
                    elements=elements,
                )

        try:
            with ThreadPoolExecutor(max_workers=len(session_results)) as executor:
                results = list(executor.map(execute_one, session_results))
        finally:
            release_browser_session_results(session_results)
        response_payload = public_browser_payload(
            {
                "task_id": uuid4().hex,
                "strategy_id": _public_identifier(strategy_id, "strategy"),
                "results": results,
            }
        )
        record_browser_log("execute_strategy", response_payload)
        return jsonify(response_payload)

    @app.get("/settings")
    def settings_page():
        return render_template_string(
            SETTINGS_PAGE_HTML,
            csrf_token=session["csrf_token"],
        )

    @app.get("/api/settings")
    def get_settings():
        return jsonify(public_settings(load_settings()))

    @app.get("/api/model-presets")
    def get_model_presets():
        return jsonify(public_model_presets())

    @app.get("/api/settings/status")
    def get_settings_status():
        return jsonify(get_config_health())

    @app.post("/api/settings/restore-latest")
    def restore_latest_settings():
        try:
            settings = restore_latest_backup_preserving(
                ("selector_probe", "models", "adspower")
            )
        except FileNotFoundError as error:
            return jsonify({"error": str(error)}), 404
        return jsonify({"settings": public_settings(settings), "status": get_config_health()})

    @app.route("/api/settings", methods=["PUT", "POST"])
    def update_settings():
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"error": "settings payload must be a JSON object"}), 400
        payload.pop("_secrets_configured", None)
        if "selector_probe" in payload:
            return jsonify(
                {
                    "code": "selector_probe_settings_managed",
                    "error": (
                        "selector_probe settings must be changed through "
                        "the selector-probe API"
                    ),
                }
            ), 409
        shared_probe_fields = {"models", "adspower"} & set(payload)
        current_probe = load_settings().get("selector_probe", {})
        if (
            shared_probe_fields
            and isinstance(current_probe, dict)
            and current_probe.get("enabled") is True
        ):
            return jsonify(
                {
                    "code": "selector_probe_settings_managed",
                    "error": (
                        "models and adspower settings used by an enabled "
                        "selector probe require the selector-probe API"
                    ),
                }
            ), 409
        try:
            if shared_probe_fields:
                with default_selector_probe_store_factory() as store:
                    store.bump_resource_revision("settings")
            updated = merge_saved_settings(payload)
            return jsonify(public_settings(updated))
        except ValueError as error:
            status_code = 409 if "配置文件无法读取" in str(error) else 400
            return jsonify({"error": str(error)}), status_code

    @app.get("/api/browser/elements")
    def get_browser_elements():
        try:
            return jsonify({"elements": load_persisted_strategy_state()["action_elements"]})
        except (TypeError, ValueError) as error:
            return jsonify({"error": str(error)}), 400

    @app.get("/api/browser/elements/templates/tiktok-comment")
    def get_tiktok_comment_element_template():
        return jsonify({"elements": copy.deepcopy(TIKTOK_COMMENT_TEMPLATE)})

    @app.get("/api/browser/action-catalog")
    def get_browser_action_catalog():
        return jsonify(
            {
                "catalog": copy.deepcopy(ACTION_CATALOG),
                "defaults": copy.deepcopy(DEFAULT_ACTION_PARAMS),
            }
        )

    @app.put("/api/browser/elements")
    def save_browser_elements():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "elements payload must be a JSON object"}), 400
        try:
            def update(browser):
                elements = normalize_elements(payload.get("elements"))
                previous_elements = browser["action_elements"]
                strategies = browser["block_strategies"]
                rename_from = payload.get("rename_from")
                renamed_to = None
                if rename_from is not None:
                    if not isinstance(rename_from, str) or not rename_from.strip():
                        raise ValueError("rename_from must be a non-empty element alias")
                    rename_from = rename_from.strip()
                    if rename_from not in previous_elements:
                        raise ValueError("rename_from must identify an existing element")
                    if rename_from in elements:
                        raise ValueError("renamed element alias must replace rename_from")
                    additions = [alias for alias in elements if alias not in previous_elements]
                    if len(additions) != 1:
                        raise ValueError("element rename must add exactly one replacement alias")
                    renamed_to = additions[0]
                    strategies = copy.deepcopy(strategies)
                    for strategy in strategies:
                        for action in strategy["actions"]:
                            params = action["params"]
                            if params.get("element") == rename_from:
                                params["element"] = renamed_to

                removed = set(previous_elements) - set(elements)
                if rename_from is not None:
                    removed.discard(rename_from)
                references = [
                    reference
                    for alias in sorted(removed)
                    for reference in element_references(browser["block_strategies"], alias)
                ]
                if references:
                    raise _StrategyReferenceConflict("element", references)
                browser["strategy_schema_version"] = 3
                browser["action_elements"] = elements
                if renamed_to is not None:
                    browser["block_strategies"] = strategies
                return browser

            browser = mutate_persisted_strategy_state(update)
            return jsonify({"elements": browser["action_elements"]})
        except _StrategyReferenceConflict as error:
            return jsonify({"error": str(error), "references": error.references}), 409
        except (TypeError, ValueError) as error:
            return jsonify({"error": str(error)}), 400

    @app.get("/api/browser/patterns")
    def get_browser_patterns():
        try:
            return jsonify({"patterns": load_persisted_strategy_state()["interaction_patterns"]})
        except (TypeError, ValueError) as error:
            return jsonify({"error": str(error)}), 400

    @app.put("/api/browser/patterns")
    def save_browser_patterns():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "patterns payload must be a JSON object"}), 400
        try:
            def update(browser):
                patterns = normalize_patterns(payload.get("patterns"))
                previous_ids = {pattern["id"] for pattern in browser["interaction_patterns"]}
                current_ids = {pattern["id"] for pattern in patterns}
                references = [
                    reference
                    for pattern_id in sorted(previous_ids - current_ids)
                    for reference in pattern_references(browser["block_strategies"], pattern_id)
                ]
                if references:
                    raise _StrategyReferenceConflict("pattern", references)
                normalize_block_strategies(
                    browser["block_strategies"],
                    browser["action_elements"],
                    patterns,
                    allow_repair=True,
                )
                browser["strategy_schema_version"] = 3
                browser["interaction_patterns"] = patterns
                return browser

            browser = mutate_persisted_strategy_state(update)
            return jsonify({"patterns": browser["interaction_patterns"]})
        except _StrategyReferenceConflict as error:
            return jsonify({"error": str(error), "references": error.references}), 409
        except (TypeError, ValueError) as error:
            return jsonify({"error": str(error)}), 400

    @app.post("/api/browser/pattern-recordings/start")
    def start_browser_pattern_recording():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "请求格式无效，必须是 JSON 对象"}), 400
        selected = payload.get("windows")
        if not isinstance(selected, list) or len(selected) != 1:
            return jsonify({"error": "录制时必须且只能选择 1 个已打开窗口"}), 400
        pattern_type = str(payload.get("type") or "").strip()
        if pattern_type not in {"mouse", "keyboard"}:
            return jsonify({"error": "录制类型必须是 mouse 或 keyboard"}), 400
        try:
            profile = normalize_selected_browser_profiles(selected)[0]
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
        profile_id = profile["profile_id"]
        sessions = selected_browser_sessions([profile_id])
        if len(sessions) != 1 or not sessions[0][1]:
            release_selected_browser_sessions(sessions)
            return jsonify({"error": "没有找到选中的已打开窗口，请先打开窗口"}), 400
        _leased_profile_id, ws_url = sessions[0]
        recording_id = uuid4().hex
        reservation = {
            "profile_id": profile_id,
            "ws_url": ws_url,
            "type": pattern_type,
            "_state": "preparing",
        }
        try:
            from browser_pattern_recorder import finish_recording, prepare_recording

            with ACTIVE_PATTERN_RECORDINGS_LOCK:
                if any(
                    item.get("profile_id") == profile_id
                    for item in ACTIVE_PATTERN_RECORDINGS.values()
                ):
                    return jsonify({"error": "该窗口已有进行中的行为录制"}), 409
                ACTIVE_PATTERN_RECORDINGS[recording_id] = reservation
            prepared = prepare_recording(ws_url, recording_id, pattern_type)
            with ACTIVE_PATTERN_RECORDINGS_LOCK:
                commit_won = ACTIVE_PATTERN_RECORDINGS.get(recording_id) is reservation
                if commit_won:
                    ACTIVE_PATTERN_RECORDINGS[recording_id] = {
                        "profile_id": profile_id,
                        "ws_url": ws_url,
                        "type": pattern_type,
                    }
            if not commit_won:
                try:
                    finish_recording(ws_url, recording_id)
                except Exception:
                    pass
                raise RuntimeError("录制上下文已失效")
            return jsonify({**prepared, "profile_id": profile_id})
        except (TypeError, ValueError, RuntimeError) as error:
            with ACTIVE_PATTERN_RECORDINGS_LOCK:
                if ACTIVE_PATTERN_RECORDINGS.get(recording_id) is reservation:
                    ACTIVE_PATTERN_RECORDINGS.pop(recording_id, None)
            return jsonify({"error": str(error)}), 400
        finally:
            release_selected_browser_sessions(sessions)

    def pattern_recording_context(recording_id):
        with ACTIVE_PATTERN_RECORDINGS_LOCK:
            context = ACTIVE_PATTERN_RECORDINGS.get(recording_id)
            return copy.deepcopy(context) if context else None

    def forget_pattern_recording(recording_id, expected=None):
        with ACTIVE_PATTERN_RECORDINGS_LOCK:
            if expected is None or ACTIVE_PATTERN_RECORDINGS.get(recording_id) == expected:
                ACTIVE_PATTERN_RECORDINGS.pop(recording_id, None)

    @app.get("/api/browser/pattern-recordings/<recording_id>")
    def get_browser_pattern_recording(recording_id):
        context = pattern_recording_context(recording_id)
        if context is None:
            return jsonify({"error": "录制上下文已失效"}), 409
        profile_id = context["profile_id"]
        ws_url = context["ws_url"]
        if not acquire_browser_session_use(profile_id, ws_url):
            forget_pattern_recording(recording_id, context)
            return jsonify({"error": "录制上下文已失效"}), 409
        try:
            from browser_pattern_recorder import read_recording

            return jsonify({**read_recording(ws_url, recording_id), "profile_id": profile_id})
        except Exception:
            forget_pattern_recording(recording_id, context)
            return jsonify({"error": "录制上下文已失效"}), 409
        finally:
            release_browser_session_use(profile_id, ws_url)

    @app.post("/api/browser/pattern-recordings/<recording_id>/stop")
    def stop_browser_pattern_recording(recording_id):
        context = pattern_recording_context(recording_id)
        if context is None:
            return jsonify({"error": "录制上下文已失效"}), 409
        profile_id = context["profile_id"]
        ws_url = context["ws_url"]
        if not acquire_browser_session_use(profile_id, ws_url):
            forget_pattern_recording(recording_id, context)
            return jsonify({"error": "录制上下文已失效"}), 409
        try:
            from browser_pattern_recorder import finish_recording

            return jsonify({**finish_recording(ws_url, recording_id), "profile_id": profile_id})
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
        except Exception:
            return jsonify({"error": "录制上下文已失效"}), 409
        finally:
            forget_pattern_recording(recording_id, context)
            release_browser_session_use(profile_id, ws_url)

    @app.get("/api/browser/strategies")
    def get_browser_strategies():
        try:
            return jsonify({"strategies": load_persisted_strategy_state()["block_strategies"]})
        except (TypeError, ValueError) as error:
            return jsonify({"error": str(error)}), 400

    @app.put("/api/browser/strategies")
    def save_browser_strategies():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "strategies payload must be a JSON object"}), 400
        dependency_swap = {
            "attempted": False,
            "applied": False,
            "previous": [],
            "candidate": [],
        }
        try:
            def update(browser):
                dependency_swap["previous"] = copy.deepcopy(
                    browser["block_strategies"]
                )
                browser["strategy_schema_version"] = 3
                browser["block_strategies"] = normalize_block_strategies(
                    payload.get("strategies"),
                    browser["action_elements"],
                    browser["interaction_patterns"],
                )
                dependency_swap["candidate"] = copy.deepcopy(
                    browser["block_strategies"]
                )
                dependency_swap["attempted"] = True
                _rebuild_strategy_dependencies(
                    app,
                    browser["block_strategies"],
                )
                dependency_swap["applied"] = True
                return browser

            browser = mutate_persisted_strategy_state(update)
            app.config[
                "SELECTOR_PROBE_DEPENDENCY_SYNC_FAILED"
            ].difference_update(
                _strategy_ids(dependency_swap["previous"])
                | _strategy_ids(dependency_swap["candidate"])
            )
            return jsonify({"strategies": browser["block_strategies"]})
        except (TypeError, ValueError) as error:
            if dependency_swap["attempted"]:
                _rollback_strategy_dependencies(
                    app,
                    dependency_swap["previous"],
                    dependency_swap["candidate"],
                )
                return jsonify(
                    {"error": "dependency_index_unavailable"}
                ), 503
            return jsonify({"error": str(error)}), 400
        except Exception:
            if dependency_swap["attempted"]:
                _rollback_strategy_dependencies(
                    app,
                    dependency_swap["previous"],
                    dependency_swap["candidate"],
                )
            return jsonify(
                {
                    "error": (
                        "strategy_save_unavailable"
                        if dependency_swap["applied"]
                        else "dependency_index_unavailable"
                    )
                }
            ), 503

    @app.route("/api/browser/action-config", methods=["GET", "PUT"])
    @app.route("/api/browser/auto-strategies", methods=["GET", "PUT"])
    @app.post("/api/browser/auto-strategies/generate")
    def retired_legacy_strategy_api():
        return jsonify({
            "error": (
                "旧策略接口已停用；请使用 /api/browser/elements 管理元素，"
                "并使用 /api/browser/strategies 管理统一积木策略"
            )
        }), 410

    @app.post("/api/browser/batch-tasks")
    def start_browser_batch_task_route():
        from browser_strategy_runtime import build_batches

        payload = request.get_json(silent=True) or {}
        strategy_id = str(payload.get("strategy_id") or "")
        try:
            browser = load_persisted_strategy_state()
            strategy = next(
                item
                for item in browser["block_strategies"]
                if item.get("id") == strategy_id
            )
            if strategy.get("status") == "needs_repair":
                detail = "; ".join(strategy.get("repair_errors") or [])
                raise ValueError(f"strategy needs repair before execution: {detail}")
        except StopIteration:
            return jsonify({"error": "执行策略不存在"}), 404
        except (TypeError, ValueError) as error:
            return jsonify({"error": str(error)}), 400
        initial_gate = browser_strategy_gate_check(app, strategy["id"])
        if initial_gate.get("allowed") is not True:
            return jsonify(
                {
                    "error": "strategy_paused",
                    "code": "strategy_paused",
                    "strategy_id": _public_identifier(
                        strategy["id"],
                        "strategy",
                    ),
                    "reasons": initial_gate.get("reasons", []),
                }
            ), 409
        try:
            batch_size = int(payload.get("batch_size", strategy.get("batch_size", 4)))
            build_batches([], batch_size)
        except (TypeError, ValueError) as error:
            return jsonify({"error": str(error)}), 400

        profiles = payload.get("windows")
        if not profiles:
            try:
                profiles = fetch_adspower_windows().get("windows", [])
            except (requests.RequestException, RuntimeError) as error:
                return jsonify({"error": str(error)}), 502
        if not isinstance(profiles, list) or not profiles:
            return jsonify({"error": "没有读取到可执行的 AdsPower 窗口"}), 400
        normalized_profiles = []
        for item in profiles:
            if isinstance(item, str):
                item = {"profile_id": item}
            if not isinstance(item, dict) or not str(item.get("profile_id") or "").strip():
                return jsonify({"error": "窗口必须包含 profile_id"}), 400
            normalized_profiles.append({**item, "profile_id": str(item["profile_id"]).strip()})

        target_url = str(
            payload.get("url")
            or load_settings().get("browser", {}).get("default_url")
            or "https://www.tiktok.com/"
        ).strip()
        if not is_valid_browser_url(target_url):
            return jsonify({"error": "url must be a valid http:// or https:// URL"}), 400

        from browser_strategy_runtime import build_batches

        batches = build_batches(normalized_profiles, batch_size)
        task_id = f"browser-batch-{uuid4().hex[:12]}"
        task = {
            "id": task_id,
            "status": "queued",
            "strategy_id": strategy_id,
            "batch_size": batch_size,
            "total_windows": len(normalized_profiles),
            "total_batches": len(batches),
            "completed_batches": 0,
            "processed_windows": 0,
            "failed_windows": 0,
            "created_at": content_now_iso(),
            "results": [],
        }
        with BROWSER_BATCH_TASKS_LOCK:
            BROWSER_BATCH_TASKS[task_id] = task
        threading.Thread(
            target=run_browser_batch_task,
            args=(app, task_id, normalized_profiles, batch_size, strategy, target_url),
            name=f"browser-batch-{task_id}",
            daemon=True,
        ).start()
        public_task = public_browser_batch_task(task)
        record_browser_log("batch_task_created", public_task)
        return jsonify(public_task), 202

    @app.get("/api/browser/batch-tasks")
    def list_browser_batch_tasks_route():
        with BROWSER_BATCH_TASKS_LOCK:
            tasks = [dict(item) for item in BROWSER_BATCH_TASKS.values()]
        tasks.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return jsonify(
            {
                "count": len(tasks),
                "tasks": [public_browser_batch_task(item) for item in tasks],
            }
        )

    @app.get("/api/browser/batch-tasks/<task_id>")
    def get_browser_batch_task_route(task_id):
        with BROWSER_BATCH_TASKS_LOCK:
            task = BROWSER_BATCH_TASKS.get(task_id)
            if task is not None:
                return jsonify(public_browser_batch_task(task))
        return jsonify({"error": "批量任务不存在"}), 404

    @app.post("/api/browser/direct-agent")
    def start_direct_agent_route():
        payload = request.get_json(silent=True) or {}
        try:
            command = build_direct_agent_command(payload)
        except ValueError as error:
            return jsonify({"error": str(error)}), 400

        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
        return jsonify(
            {
                "status": "started",
                "pid": process.pid,
                "command": command,
            }
        ), 202

    @app.get("/api/browser/adspower-windows")
    def adspower_windows_route():
        try:
            return jsonify(fetch_adspower_windows())
        except requests.exceptions.RequestException as error:
            return jsonify({"error": str(error), "count": 0, "windows": []}), 502
        except RuntimeError as error:
            return jsonify({"error": str(error), "count": 0, "windows": []}), 502

    @app.get("/api/browser/logs")
    def browser_logs_route():
        try:
            limit = min(max(int(request.args.get("limit", 100)), 1), 500)
        except (TypeError, ValueError):
            limit = 100
        if not BROWSER_LOG_PATH.exists():
            return jsonify({"count": 0, "logs": [], "path": str(BROWSER_LOG_PATH)})
        try:
            lines = BROWSER_LOG_PATH.read_text(encoding="utf-8").splitlines()[-limit:]
            logs = [json.loads(line) for line in lines if line.strip()]
        except (OSError, json.JSONDecodeError) as error:
            return jsonify({"error": str(error), "count": 0, "logs": []}), 500
        logs = public_browser_payload(logs)
        return jsonify({"count": len(logs), "logs": logs, "path": str(BROWSER_LOG_PATH)})

    @app.get("/api/browser/sessions")
    def browser_sessions_route():
        with ACTIVE_BROWSER_SESSIONS_LOCK:
            sessions = [
                {
                    "profile_id": profile_id,
                    "status": "active",
                }
                for profile_id, ws_url in ACTIVE_BROWSER_SESSIONS.items()
                if ws_url
            ]
        return jsonify({"count": len(sessions), "sessions": sessions})

    @app.post("/api/browser/open-tile")
    def open_and_tile_browsers_route():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "请求格式无效，必须是 JSON 对象"}), 400
        try:
            profiles = normalize_selected_browser_profiles(payload.get("windows"))
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
        target_url = get_browser_target_url(payload)
        if not is_valid_browser_url(target_url):
            return jsonify({"error": "url must be a valid http:// or https:// URL"}), 400

        session_results, layout = ensure_browser_profile_sessions(
            profiles, lease_sessions=True
        )
        successful = [item for item in session_results if item.get("status") == "ready"]
        ready_profile_ids = [item["profile_id"] for item in successful]
        tile_errors = {
            profile_id: browser_tile_error(layout, profile_id, ready_profile_ids)
            for profile_id in ready_profile_ids
        }
        results = [
            {
                "profile_id": item["profile_id"],
                "profile_no": item.get("profile_no", ""),
                "name": item.get("name", ""),
                "status": (
                    "started"
                    if item.get("status") == "ready"
                    and not tile_errors.get(item["profile_id"], "")
                    else "failed"
                ),
                "stage": (
                    "tile"
                    if item.get("status") == "ready"
                    and tile_errors.get(item["profile_id"], "")
                    else item["stage"]
                ),
                "attempts": item["attempts"],
                "target_url": target_url,
                "error": (
                    tile_errors.get(item["profile_id"], "")
                    if item.get("status") == "ready"
                    else item.get("error", "")
                ),
            }
            for item in session_results
        ]

        def prepare_one(item):
            failure = None
            for attempt in range(1, 4):
                try:
                    prepared = prepare_browser_page(item["ws_url"], target_url)
                    return {
                        "profile_id": item["profile_id"],
                        "status": "ok",
                        "stage": "navigate",
                        "attempts": attempt,
                        "target_url": target_url,
                        "url": target_url,
                        "current_url": prepared["current_url"],
                        "closed_tabs": prepared["closed_tabs"],
                    }
                except BrowserStageError as error:
                    failure = {
                        "profile_id": item["profile_id"],
                        "status": "failed",
                        "stage": error.stage,
                        "attempts": attempt,
                        "target_url": error.target_url,
                        "error": error.reason,
                    }
                except Exception as error:
                    failure = {
                        "profile_id": item["profile_id"],
                        "status": "failed",
                        "stage": "navigate",
                        "attempts": attempt,
                        "target_url": target_url,
                        "error": str(error),
                    }
                if attempt < 3:
                    time.sleep(2)
            return failure

        navigation = []
        tiled = [
            item
            for item in successful
            if not tile_errors.get(item["profile_id"], "")
        ]
        try:
            if tiled:
                with ThreadPoolExecutor(max_workers=len(tiled)) as executor:
                    navigation = list(executor.map(prepare_one, tiled))
        finally:
            release_browser_session_results(session_results)

        response_payload = public_browser_payload({
            "task_id": uuid4().hex,
            "requested": len(profiles),
            "started": len(successful),
            "failed": len(profiles) - len(successful),
            "url": target_url,
            "results": results,
            "layout": layout,
            "navigation": navigation,
        })
        record_browser_log("open_tile", response_payload)
        return jsonify(response_payload)

    @app.post("/api/browser/search-agent")
    def start_search_agent_route():
        payload = request.get_json(silent=True) or {}
        try:
            command = build_search_agent_command(payload)
        except ValueError as error:
            return jsonify({"error": str(error)}), 400

        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
        return jsonify(
            {
                "status": "started",
                "pid": process.pid,
                "command": command,
            }
        ), 202

    @app.get("/api/execution-strategies")
    def get_execution_strategies_route():
        return jsonify(load_settings().get("execution_strategies", {"items": []}))

    @app.route("/api/execution-strategies", methods=["PUT", "POST"])
    def save_execution_strategies_route():
        payload = request.get_json(silent=True) or {}
        try:
            return jsonify(save_execution_strategies(payload))
        except (TypeError, ValueError) as error:
            return jsonify({"error": str(error)}), 400

    @app.post("/api/execution-strategies/generate")
    def generate_execution_strategies_route():
        payload = request.get_json(silent=True) or {}
        try:
            return jsonify(generate_execution_strategies(payload.get("prompt", "")))
        except (json.JSONDecodeError, requests.exceptions.RequestException, ValueError) as error:
            return jsonify({"error": str(error)}), 400

    @app.get("/api/proxy-pool/status")
    def proxy_pool_status():
        settings = load_settings()
        assigned_sessions = get_assigned_proxy_sessions(app.config["ACCOUNTS_DB_PATH"])
        try:
            page = max(int(request.args.get("page", 1)), 1)
            page_size = min(max(int(request.args.get("page_size", 50)), 1), 200)
        except (TypeError, ValueError):
            page = 1
            page_size = 50
        return jsonify(
            summarize_proxy_pool(
                settings.get("proxy_pool", {}).get("items", []),
                assigned_sessions,
                page=page,
                page_size=page_size,
                search=request.args.get("search", ""),
            )
        )

    @app.get("/api/accounts")
    def accounts_route():
        return jsonify(account_summary(app.config["ACCOUNTS_DB_PATH"]))

    @app.post("/api/accounts/save")
    def save_account_route():
        payload = request.get_json(silent=True) or {}
        try:
            return jsonify(
                {
                    "account": save_buffer_account(
                        app.config["ACCOUNTS_DB_PATH"],
                        payload,
                    ),
                    **account_summary(app.config["ACCOUNTS_DB_PATH"]),
                }
            )
        except ValueError as error:
            return jsonify({"error": str(error)}), 400

    @app.get("/api/account/next")
    def next_account():
        account = get_next_account(app.config["ACCOUNTS_DB_PATH"])

        if account is None:
            return jsonify({"error": "no available account"}), 404

        return jsonify(account)

    @app.post("/api/account/update")
    def update_account_route():
        payload = request.get_json(silent=True) or {}
        ads_power_user_id = payload.get("ads_power_user_id")
        result = payload.get("result")

        if not ads_power_user_id or result not in {"success", "failed", "banned", "abnormal"}:
            return (
                jsonify({"error": "ads_power_user_id and valid result are required"}),
                400,
            )

        updated_account = update_account(
            app.config["ACCOUNTS_DB_PATH"],
            ads_power_user_id,
            result,
        )

        if updated_account is None:
            return jsonify({"error": "account not found"}), 404

        return jsonify(updated_account)

    @app.post("/api/accounts/discover")
    def discover_accounts_route():
        payload = request.get_json(silent=True) or {}
        try:
            return jsonify(
                discover_accounts(
                    app.config["ACCOUNTS_DB_PATH"],
                    payload.get("accountId"),
                )
            )
        except RuntimeError as error:
            return jsonify({"error": str(error)}), 400

    @app.post("/api/accounts/import")
    def import_accounts_route():
        payload = request.get_json(silent=True) or {}
        accounts = payload.get("accounts") or []
        if payload.get("buffer_token"):
            accounts = [
                *accounts,
                {
                    "account_name": payload.get("account_name", ""),
                    "buffer_token": payload.get("buffer_token", ""),
                    "buffer_api": payload.get("buffer_api", ""),
                },
            ]
        try:
            return jsonify(
                import_buffer_accounts(
                    app.config["ACCOUNTS_DB_PATH"],
                    accounts=accounts,
                    raw_text=payload.get("raw_text", ""),
                )
            )
        except RuntimeError as error:
            return jsonify({"error": str(error)}), 400

    @app.post("/api/accounts/proxy")
    def assign_account_proxy_route():
        payload = request.get_json(silent=True) or {}
        account_id = payload.get("account_id")
        mode = payload.get("mode")

        if not account_id or mode not in {"auto", "manual"}:
            return jsonify({"error": "account_id and valid mode are required"}), 400

        if mode == "auto":
            settings = load_settings()
            account = get_buffer_account(app.config["ACCOUNTS_DB_PATH"], account_id)
            current_proxy_session = account.get("proxy_session") if account else ""
            proxy_pool = settings.get("proxy_pool", {}).get("items", [])
            pool_sessions = {proxy_pool_key(item) for item in proxy_pool}
            assigned_sessions = set(
                get_assigned_proxy_sessions(app.config["ACCOUNTS_DB_PATH"])
            )
            if current_proxy_session in pool_sessions:
                proxy_session = current_proxy_session
            else:
                available_proxies = [
                    item
                    for item in proxy_pool
                    if proxy_pool_key(item) not in assigned_sessions
                ]
                selected_proxy = select_proxy_from_pool(
                    available_proxies,
                    account_id,
                )
                if selected_proxy is None:
                    return jsonify({"error": "no available proxy in pool"}), 400
                proxy_session = proxy_pool_key(selected_proxy)
        else:
            try:
                proxy_session = proxy_pool_key(
                    parse_proxy_pool(payload.get("proxy", ""))[0]
                )
            except (IndexError, ValueError) as error:
                return jsonify({"error": str(error) or "proxy is required"}), 400

        account = assign_proxy_session(
            app.config["ACCOUNTS_DB_PATH"],
            account_id,
            proxy_session,
        )
        if account is None:
            return jsonify({"error": "account not found"}), 404

        return jsonify(
            {
                "account": account,
                **account_summary(app.config["ACCOUNTS_DB_PATH"]),
            }
        )

    @app.post("/api/content/videos/sync")
    def sync_content_videos_route():
        try:
            videos = list_r2_video_objects(load_settings())
            return jsonify(sync_video_library(app.config["CONTENT_DATA_DIR"], videos))
        except (ValueError, requests.exceptions.RequestException) as error:
            return jsonify({"error": str(error)}), 400

    @app.get("/api/content/videos")
    def content_videos_route():
        return jsonify(video_summary(app.config["CONTENT_DATA_DIR"]))

    @app.get("/api/content/brands")
    def content_brands_route():
        return jsonify({"brands": list_brands(app.config["CONTENT_DATA_DIR"])})

    @app.post("/api/content/brands")
    def create_content_brand_route():
        payload = request.get_json(silent=True) or {}
        try:
            brand = create_brand(app.config["CONTENT_DATA_DIR"], payload.get("brand", ""))
            return jsonify({"brand": brand, "brands": list_brands(app.config["CONTENT_DATA_DIR"])})
        except ValueError as error:
            return jsonify({"error": str(error)}), 400

    @app.patch("/api/content/brands/<brand_id>")
    def rename_content_brand_route(brand_id):
        payload = request.get_json(silent=True) or {}
        try:
            brand = rename_brand(
                app.config["CONTENT_DATA_DIR"],
                brand_id,
                payload.get("name", ""),
            )
        except ValueError as error:
            return jsonify({"error": str(error)}), 400

        if brand is None:
            return jsonify({"error": "品牌不存在"}), 404
        return jsonify(
            {
                "brand": brand,
                "brands": list_brands(app.config["CONTENT_DATA_DIR"]),
            }
        )

    @app.post("/api/content/copy")
    def add_content_copy_route():
        payload = request.get_json(silent=True) or {}
        try:
            item = add_copy_item(
                app.config["CONTENT_DATA_DIR"],
                payload.get("brand_id", ""),
                payload.get("body", ""),
                payload.get("tags", []),
            )
        except ValueError as error:
            return jsonify({"error": str(error)}), 400

        items = list_copy_items(app.config["CONTENT_DATA_DIR"], payload.get("brand_id", ""))
        return jsonify({"item": item, "copy_count": len(items), "items": items})

    @app.post("/api/content/copy/import")
    def import_content_copy_route():
        upload = request.files.get("file")
        if upload is None or not upload.filename:
            return jsonify({"error": "请选择导入文件"}), 400

        try:
            parsed = parse_copy_import(upload.filename, upload.stream)
            result = apply_copy_import(app.config["CONTENT_DATA_DIR"], parsed)
        except ValueError as error:
            return jsonify({"error": str(error)}), 400

        return jsonify(
            {
                **result,
                "brands": list_brands(app.config["CONTENT_DATA_DIR"]),
            }
        )

    @app.get("/api/content/brands/<brand_id>/copy")
    def content_copy_route(brand_id):
        items = list_copy_items(app.config["CONTENT_DATA_DIR"], brand_id)
        return jsonify({"brand_id": brand_id, "copy_count": len(items), "items": items})

    @app.post("/api/publish/queue/manual-test")
    def manual_publish_test_route():
        payload = request.get_json(silent=True) or {}
        try:
            task = create_publish_task(
                data_dir=app.config["CONTENT_DATA_DIR"],
                db_path=app.config["ACCOUNTS_DB_PATH"],
                account_id=payload.get("account_id", ""),
                profile_id=payload.get("profile_id", ""),
                video_id=payload.get("video_id", ""),
                brand_id=payload.get("brand_id", ""),
                copy_id=payload.get("copy_id", ""),
                scheduled_at=payload.get("scheduled_at", ""),
            )
            if task.get("status") == "failed":
                return jsonify({"task": task, "error": task.get("error", "")}), 502
            return jsonify({"task": task})
        except ValueError as error:
            return jsonify({"error": str(error)}), 400

    @app.post("/api/publish/queue/batch")
    def batch_publish_queue_route():
        payload = request.get_json(silent=True) or {}
        brand_id = payload.get("brand_id", "")
        scheduled_at = payload.get("scheduled_at", "")
        account_ids = payload.get("account_ids") or []
        accounts = select_publish_accounts(
            account_summary(app.config["ACCOUNTS_DB_PATH"]).get("available_accounts", []),
            account_ids,
        )
        videos = unused_videos(app.config["CONTENT_DATA_DIR"])

        tasks = []
        requested = len(accounts)
        for account, video in zip(accounts, videos):
            profile_ids = account.get("buffer_profile_ids") or []
            if not profile_ids:
                continue
            try:
                tasks.append(
                    create_publish_task(
                        data_dir=app.config["CONTENT_DATA_DIR"],
                        db_path=app.config["ACCOUNTS_DB_PATH"],
                        account_id=account.get("ads_power_user_id") or account.get("id"),
                        profile_id=profile_ids[0],
                        video_id=video.get("id", ""),
                        brand_id=brand_id,
                        scheduled_at=scheduled_at,
                        publish_now=False,
                    )
                )
            except ValueError as error:
                return jsonify({"error": str(error)}), 400

        skipped = max(requested - len(tasks), 0)
        response = {
            "requested": requested,
            "created": len(tasks),
            "skipped": skipped,
            "tasks": tasks,
        }
        if skipped:
            response["skipped_reason"] = "not enough unused videos"
        response["run"] = save_batch_publish_run(
            app.config["CONTENT_DATA_DIR"],
            {
                "scheduled_at": scheduled_at,
                "brand_id": brand_id,
                "account_ids": [
                    account.get("ads_power_user_id") or account.get("id")
                    for account in accounts
                ],
                "requested": requested,
                "created": len(tasks),
                "skipped": skipped,
                "status": "created",
            },
        )
        return jsonify(response)

    @app.get("/api/publish/queue/batches")
    def batch_publish_runs_route():
        runs = list_batch_publish_runs(app.config["CONTENT_DATA_DIR"])
        return jsonify({"count": len(runs), "runs": runs})

    @app.post("/api/publish/queue/process-one")
    def process_one_publish_task_route():
        return jsonify(
            execute_next_publish_task(
                app.config["CONTENT_DATA_DIR"],
                app.config["ACCOUNTS_DB_PATH"],
            )
        )

    @app.patch("/api/publish/queue/batches/<run_id>")
    def update_batch_publish_run_route(run_id):
        payload = request.get_json(silent=True) or {}
        run = update_batch_publish_run(
            app.config["CONTENT_DATA_DIR"],
            run_id,
            payload,
        )
        if run is None:
            return jsonify({"error": "batch run not found"}), 404
        return jsonify({"run": run})

    @app.delete("/api/publish/queue/batches/<run_id>")
    def delete_batch_publish_run_route(run_id):
        deleted = delete_batch_publish_run(app.config["CONTENT_DATA_DIR"], run_id)
        if not deleted:
            return jsonify({"error": "batch run not found"}), 404
        return jsonify({"deleted": True})

    @app.get("/api/publish/results")
    def publish_results_route():
        tasks = public_publish_tasks(
            app.config["CONTENT_DATA_DIR"],
            date=request.args.get("date"),
            status=request.args.get("status"),
        )
        return jsonify({"count": len(tasks), "tasks": tasks})

    @app.get("/api/publish/stats")
    def publish_stats_route():
        stats = publish_stats(
            app.config["CONTENT_DATA_DIR"],
            date=request.args.get("date"),
            status=request.args.get("status"),
            sort=request.args.get("sort"),
        )
        return jsonify(enrich_publish_stats_with_accounts(stats, app.config["ACCOUNTS_DB_PATH"]))

    @app.post("/api/publish/auto-sample-tick")
    def publish_auto_sample_tick_route():
        return jsonify(
            execute_publish_sampling_tick(
                app.config["CONTENT_DATA_DIR"],
                app.config["ACCOUNTS_DB_PATH"],
            )
        )

    @app.post("/api/publish/sample-next")
    def publish_sample_next_route():
        try:
            min_age_hours = int(request.args.get("min_age_hours", 24))
        except (TypeError, ValueError):
            min_age_hours = 24
        return jsonify(
            execute_next_publish_sample(
                app.config["CONTENT_DATA_DIR"],
                min_age_hours=max(min_age_hours, 0),
            )
        )

    @app.post("/api/publish/backfill-link-next")
    def publish_backfill_link_next_route():
        return jsonify(
            execute_next_tiktok_link_backfill(
                app.config["CONTENT_DATA_DIR"],
                app.config["ACCOUNTS_DB_PATH"],
            )
        )

    @app.post("/api/publish/logs/cleanup")
    def cleanup_publish_logs_route():
        payload = request.get_json(silent=True) or {}
        return jsonify(
            cleanup_publish_logs(
                app.config["CONTENT_DATA_DIR"],
                payload.get("before_date", ""),
            )
        )

    @app.post("/api/publish/schedule/daily")
    def daily_publish_schedule_route():
        payload = request.get_json(silent=True) or {}
        return jsonify(
            {
                "schedule": save_daily_schedule(
                    app.config["CONTENT_DATA_DIR"],
                    payload,
                )
            }
        )

    @app.get("/api/publish/schedule/daily")
    def daily_publish_schedules_route():
        schedules = list_daily_schedules(app.config["CONTENT_DATA_DIR"])
        return jsonify({"count": len(schedules), "schedules": schedules})

    @app.patch("/api/publish/schedule/daily/<schedule_id>")
    def update_daily_publish_schedule_route(schedule_id):
        payload = request.get_json(silent=True) or {}
        schedule = update_daily_schedule(
            app.config["CONTENT_DATA_DIR"],
            schedule_id,
            payload,
        )
        if schedule is None:
            return jsonify({"error": "schedule not found"}), 404
        return jsonify({"schedule": schedule})

    @app.delete("/api/publish/schedule/daily/<schedule_id>")
    def delete_daily_publish_schedule_route(schedule_id):
        deleted = delete_daily_schedule(app.config["CONTENT_DATA_DIR"], schedule_id)
        if not deleted:
            return jsonify({"error": "schedule not found"}), 404
        return jsonify({"deleted": True})

    @app.post("/api/publish/results/metrics")
    def publish_metrics_route():
        payload = request.get_json(silent=True) or {}
        task = update_publish_metrics(
            app.config["CONTENT_DATA_DIR"],
            payload.get("task_id", ""),
            payload.get("views", 0),
            payload.get("comments", 0),
            payload.get("tiktok_url", ""),
        )
        if task is None:
            return jsonify({"error": "task not found"}), 404
        return jsonify({"task": task})

    @app.post("/check_ip")
    def check_ip():
        payload = request.get_json(silent=True) or {}
        account_id = payload.get("account_id")

        if not account_id:
            return jsonify({"error": "account_id is required"}), 400

        proxy_url = get_proxy_url_for_account(
            app.config["ACCOUNTS_DB_PATH"],
            account_id,
        )

        try:
            return jsonify(fetch_ip_info(proxy_url))
        except requests.RequestException:
            return jsonify({"error": "failed to fetch ip info through proxy"}), 502

    @app.post("/publish/buffer")
    def publish_buffer():
        request_payload = request.get_json(silent=True) or {}
        account_id = request_payload.get("account_id")
        access_token = request_payload.get("access_token")
        payload = request_payload.get("payload")

        if not account_id or payload is None:
            return (
                jsonify({"error": "account_id, access_token, and payload are required"}),
                400,
            )

        account = get_buffer_account(app.config["ACCOUNTS_DB_PATH"], account_id)
        if account:
            access_token = account.get("buffer_token") or access_token
            profile_ids = account.get("buffer_profile_ids") or []
            if profile_ids:
                payload = {**payload, "profile_ids": profile_ids}

        if not access_token:
            return (
                jsonify({"error": "access_token is required for this account"}),
                400,
            )

        proxy_url = get_proxy_url_for_account(
            app.config["ACCOUNTS_DB_PATH"],
            account_id,
        )

        try:
            return jsonify(publish_to_buffer(proxy_url, access_token, payload))
        except requests.exceptions.RequestException as error:
            return jsonify({"error": str(error)}), 502

    if os.getenv("PUBLISH_WORKER_ENABLED") == "1":
        start_publish_queue_worker(app)
    start_publish_sampling_worker(app)

    return app
