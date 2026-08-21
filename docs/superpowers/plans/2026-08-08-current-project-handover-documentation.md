# Current Project Handover Documentation Center Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a source-backed `docs/architecture/` handover center that documents the current working tree without changing application behavior.

**Architecture:** Treat the current Windows/Flask system as the source of truth. Build human-readable module and operations guides beside machine-readable OpenAPI 3.1 and JSON Schema contracts, then verify route, table, state, link, encoding, and secret coverage. Missing platform capabilities are documented as gaps, never represented as implemented.

**Tech Stack:** Markdown, Mermaid, OpenAPI 3.1, JSON Schema Draft 2020-12, PowerShell, Python standard library, Git.

## Global Constraints

- Document the 2026-08-08 working tree, including uncommitted `execution_v2` and `comment_campaign` code; Git baseline is `7812fcb`.
- Do not modify application code, databases, runtime configuration, package manifests, or lock files.
- Do not start AdsPower Profiles or perform real TikTok publishing/commenting.
- Never include API keys, cookies, Authorization values, Redis passwords, CDP WebSocket URLs, or raw AdsPower Profile IDs.
- Write all new documents as UTF-8 Chinese; preserve code identifiers, paths, protocols, and error codes in English.
- Mark evidence as `源码确认`, `测试确认`, `配置确认`, `历史设计`, `运行时未验证`, or `依据不足`.
- Mark pnpm workspace, a shared component package, generated TypeScript client, root Compose, and CI/CD as not implemented.
- Only stage files owned by the current task; preserve all pre-existing dirty-worktree changes.
- `docs/architecture/api/openapi.yaml` uses formatted JSON syntax. JSON is valid YAML 1.2 and can be parsed with the bundled Python standard library without adding a dependency.

---

## Parallel Execution Map

```text
Task 1: foundation
  ├─ Task 2: module handbooks
  ├─ Task 3: ADRs
  ├─ Task 4: HTTP contract
  ├─ Task 5: messaging contract
  ├─ Task 6: data and state machines
  ├─ Task 7: frontend/backend/executor guides
  └─ Task 8: operations/development/gaps
       └─ Task 9: final index and verification
```

Tasks 2–8 may run in parallel after Task 1. Task 9 runs only after all other tasks are accepted. Parallel workers must not edit `docs/architecture/README.md`; Task 1 creates it and Task 9 finalizes it.

### Task 1: Documentation Foundation and System Baseline

**Files:**
- Create: `docs/architecture/README.md`
- Create: `docs/architecture/system/context.md`
- Create: `docs/architecture/system/runtime-topology.md`
- Create: `docs/architecture/system/module-map.md`
- Create: `docs/architecture/system/data-flow.md`
- Create: `docs/architecture/system/technology-stack.md`

**Interfaces:**
- Consumes: approved design spec `docs/superpowers/specs/2026-08-08-current-project-handover-documentation-design.md`.
- Produces: canonical module names, evidence labels, runtime process names, glossary, and working-tree baseline used by Tasks 2–9.

- [ ] **Step 1: Establish the missing-document baseline**

Run:

```powershell
$paths = @(
  'docs/architecture/README.md',
  'docs/architecture/system/context.md',
  'docs/architecture/system/runtime-topology.md',
  'docs/architecture/system/module-map.md',
  'docs/architecture/system/data-flow.md',
  'docs/architecture/system/technology-stack.md'
)
$existing = $paths | Where-Object { Test-Path $_ }
if ($existing) { throw "Unexpected pre-existing files: $existing" }
```

Expected: PASS with no output.

- [ ] **Step 2: Write the root entry and evidence policy**

Create `docs/architecture/README.md` with these exact sections:

```markdown
# 项目架构与交接文档中心

## 文档基准
## 安全边界
## 证据等级
## 当前系统一句话说明
## 按角色阅读
## 文档地图
## 未实现能力
## 维护规则
```

Record baseline `7812fcb`, extraction date `2026-08-08`, dirty-worktree scope, local Windows execution, and the six evidence labels from Global Constraints. Link only the five `system/` files created in this task; Task 9 adds the complete link map.

- [ ] **Step 3: Write the five system documents**

Use the following responsibilities:

| File | Required content |
|---|---|
| `context.md` | users, system boundary, external systems, trust boundary, non-goals |
| `runtime-topology.md` | launcher, Flask, three workers, Redis, optional MySQL, TikTok API Compose, ports/config source where confirmed |
| `module-map.md` | launcher, gateway, auth, settings, accounts/proxies, content/publish, legacy browser, V2, probe, stats, campaign |
| `data-flow.md` | HTTP request flow, browser execution flow, probe flow, stats flow, campaign approval/submit flow |
| `technology-stack.md` | exact dependency versions from `requirements.txt` and `package.json`, plus current missing toolchain |

Every module row must include source path and evidence label. Mermaid diagrams must use stable module names from `module-map.md`.

- [ ] **Step 4: Verify required sections and current-state wording**

Run:

```powershell
$files = Get-ChildItem docs/architecture/system -File
if ($files.Count -ne 5) { throw "Expected 5 system documents" }
foreach ($file in $files) {
  if (-not (Select-String -Path $file.FullName -Pattern '源码确认|配置确认|测试确认|历史设计|运行时未验证|依据不足')) {
    throw "Missing evidence label: $($file.Name)"
  }
}
rg -n "pnpm.*已实现|React.*已实现|CI/CD.*已实现|根级.*Compose.*已实现" docs/architecture
if ($LASTEXITCODE -eq 0) { throw "Future capability represented as current" }
```

Expected: five files found; no false current-state matches.

- [ ] **Step 5: Commit the foundation only**

```powershell
git add docs/architecture/README.md docs/architecture/system
git commit -m "docs(architecture): add system handover baseline"
```

### Task 2: Module Handover Handbooks

**Files:**
- Create: `docs/architecture/modules/launcher.md`
- Create: `docs/architecture/modules/gateway.md`
- Create: `docs/architecture/modules/authentication.md`
- Create: `docs/architecture/modules/settings.md`
- Create: `docs/architecture/modules/accounts-and-proxies.md`
- Create: `docs/architecture/modules/content-and-publishing.md`
- Create: `docs/architecture/modules/legacy-browser-strategy.md`
- Create: `docs/architecture/modules/execution-v2.md`
- Create: `docs/architecture/modules/selector-probe.md`
- Create: `docs/architecture/modules/tiktok-stats.md`
- Create: `docs/architecture/modules/comment-campaign.md`

**Interfaces:**
- Consumes: module names and process names from Task 1.
- Produces: module ownership and entry-point map consumed by API, data, operations, and final index tasks.

- [ ] **Step 1: Verify all handbooks are initially absent**

Run:

```powershell
if (Test-Path docs/architecture/modules) { throw 'Module handbook directory already exists' }
```

Expected: PASS.

- [ ] **Step 2: Apply the standard module template**

Every module document must contain these headings:

```markdown
# 模块名称
## 职责
## 不负责什么
## 代码入口
## 对外接口
## 内部组件
## 依赖与调用者
## 数据与事务
## 配置
## 进程与生命周期
## 安全边界
## 测试
## 日志与证据
## 常见故障
## 修改影响清单
## 已知限制
```

Do not copy generic prose between modules. Each section must cite concrete files, classes, functions, tables, environment variables, routes, and tests.

- [ ] **Step 3: Populate launcher and gateway-family handbooks**

Use these source groups:

```text
launcher.md                  launcher.py, start_console.cmd, start_console.vbs
gateway.md                   app.py, gateway/app.py, gateway/local_only.py
authentication.md            gateway/auth_*.py, gateway/admin_users.py, gateway/management_db.py
settings.md                  gateway/settings_store.py, config.example.json
accounts-and-proxies.md      gateway/account_store.py, gateway/proxy.py, gateway/proxy_pool.py
content-and-publishing.md    gateway/content_*.py, gateway/buffer_*.py, gateway/r2_client.py, celery_app.py
```

Explain direct-local mode and authenticated mode separately. Do not document credentials or live endpoints containing secrets.

- [ ] **Step 4: Populate browser and worker handbooks**

Use these source groups:

```text
legacy-browser-strategy.md   root browser_*.py, actions_dom.py, browser/, related tests
execution-v2.md              execution_v2/, tests/test_execution_v2_*.py
selector-probe.md            selector_probe/, tests/test_selector_probe_*.py
tiktok-stats.md              tiktok_stats/, docs/tiktok-stats.md, services/tiktok_api/
comment-campaign.md          comment_campaign/, tests/test_comment_campaign_*.py
```

Clearly mark `execution_v2` and `comment_campaign` as present in the working tree but absent from baseline commit `7812fcb`.

- [ ] **Step 5: Verify template completeness and source references**

Run:

```powershell
$headings = @('职责','不负责什么','代码入口','对外接口','内部组件','依赖与调用者','数据与事务','配置','进程与生命周期','安全边界','测试','日志与证据','常见故障','修改影响清单','已知限制')
$files = Get-ChildItem docs/architecture/modules -Filter *.md
if ($files.Count -ne 11) { throw "Expected 11 module handbooks" }
foreach ($file in $files) {
  $text = Get-Content -Raw $file.FullName
  foreach ($heading in $headings) {
    if ($text -notmatch [regex]::Escape("## $heading")) { throw "$($file.Name) missing $heading" }
  }
  if ($text -notmatch '`[^`]+\.(py|js|html|css|yml|json)') { throw "$($file.Name) lacks source references" }
}
```

Expected: PASS.

- [ ] **Step 6: Commit module handbooks**

```powershell
git add docs/architecture/modules
git commit -m "docs(modules): add current handover guides"
```

### Task 3: Retrospective Architecture Decision Records

**Files:**
- Create: `docs/architecture/adr/README.md`
- Create: `docs/architecture/adr/ADR-0001-flask-modular-monolith.md`
- Create: `docs/architecture/adr/ADR-0002-windows-local-executor.md`
- Create: `docs/architecture/adr/ADR-0003-adspower-cdp-playwright.md`
- Create: `docs/architecture/adr/ADR-0004-module-local-sqlite.md`
- Create: `docs/architecture/adr/ADR-0005-redis-rq-and-existing-celery.md`
- Create: `docs/architecture/adr/ADR-0006-local-direct-and-auth-modes.md`
- Create: `docs/architecture/adr/ADR-0007-explicit-element-selection.md`
- Create: `docs/architecture/adr/ADR-0008-profile-identity-boundary.md`
- Create: `docs/architecture/adr/ADR-0009-batched-profile-lifecycle.md`

**Interfaces:**
- Consumes: Task 1 baseline and existing `docs/superpowers/specs/` evidence.
- Produces: stable decision identifiers referenced by module and development documentation.

- [ ] **Step 1: Establish the ADR RED check**

```powershell
if (Test-Path docs/architecture/adr/ADR-0001-flask-modular-monolith.md) { throw 'ADR already exists' }
```

- [ ] **Step 2: Write the ADR index and template policy**

The index must explain statuses `Accepted (Retrospective)`, `Legacy`, and `Superseded`, and state that unknown historical reasons use `依据不足`.

Each ADR must contain:

```markdown
# ADR-NNNN: 标题
## 状态
## 当前事实
## 决定
## 代码与历史证据
## 为什么
## 后果
## 已知限制
## 后续变更条件
```

- [ ] **Step 3: Write ADR-0001 through ADR-0009**

Use only these evidence classes:

- current source files;
- existing approved design specs;
- tests demonstrating enforced behavior;
- configuration defaults.

Never infer organizational or business motivations absent from evidence. For each limitation, distinguish existing debt from an intended future architecture.

- [ ] **Step 4: Verify ADR numbering, headings, and evidence**

```powershell
$adrs = Get-ChildItem docs/architecture/adr -Filter 'ADR-*.md' | Sort-Object Name
if ($adrs.Count -ne 9) { throw 'Expected 9 ADRs' }
$expected = 1
foreach ($adr in $adrs) {
  if ($adr.Name -notmatch ('^ADR-' + $expected.ToString('0000') + '-')) { throw "ADR sequence broken at $($adr.Name)" }
  $text = Get-Content -Raw $adr.FullName
  foreach ($heading in @('状态','当前事实','决定','代码与历史证据','为什么','后果','已知限制','后续变更条件')) {
    if ($text -notmatch [regex]::Escape("## $heading")) { throw "$($adr.Name) missing $heading" }
  }
  $expected++
}
```

- [ ] **Step 5: Commit ADRs**

```powershell
git add docs/architecture/adr
git commit -m "docs(adr): record current architecture decisions"
```

### Task 4: HTTP Route Inventory, OpenAPI, and Error Codes

**Files:**
- Create: `docs/architecture/api/README.md`
- Create: `docs/architecture/api/openapi.yaml`
- Create: `docs/architecture/api/route-inventory.md`
- Create: `docs/architecture/api/authentication.md`
- Create: `docs/architecture/api/conventions.md`
- Create: `docs/architecture/api/error-codes.md`

**Interfaces:**
- Consumes: current Flask route decorators and Blueprint prefixes.
- Produces: HTTP contract used by frontend, backend, testing, and future client-generation work.

- [ ] **Step 1: Capture the source route count before writing**

Run:

```powershell
$targets = @(
  'gateway/app.py','gateway/auth_blueprint.py','execution_v2/blueprint.py',
  'comment_campaign/blueprint.py','selector_probe/blueprint.py','tiktok_stats/blueprint.py'
)
$matches = Select-String -Path $targets -Pattern '@(?:app|blueprint|bp)\.(?:route|get|post|put|patch|delete)\('
if ($matches.Count -ne 186) { throw "Expected reviewed baseline of 186 declarations, got $($matches.Count)" }
```

Expected: 186 declarations. If the working tree changes before execution, update the design baseline and explain the delta before changing this assertion.

- [ ] **Step 2: Build the complete route inventory**

For each declaration, record:

```text
method(s) | effective path | response type | auth | CSRF | role | source file:line | OpenAPI operationId | legacy exception
```

Resolve Blueprint prefixes, including `/api/browser-v2` for Execution V2 and Comment Campaign. Record HTML routes separately from JSON/operational routes.

- [ ] **Step 3: Write OpenAPI 3.1 in JSON-formatted YAML**

`openapi.yaml` must be formatted JSON with these top-level keys:

```json
{
  "openapi": "3.1.0",
  "info": {},
  "servers": [],
  "tags": [],
  "paths": {},
  "components": {
    "securitySchemes": {},
    "schemas": {},
    "responses": {}
  }
}
```

For every JSON or operational route, include exact methods, parameters, request bodies where present, success responses, fixed error envelopes, auth/CSRF requirements, `operationId`, and `x-source`. Use `x-legacy-exception` when behavior does not meet the new convention. Do not create schemas for HTML page bodies.

- [ ] **Step 4: Write auth, conventions, and error-code references**

`authentication.md` covers local-direct loopback protection and legacy session/CSRF/roles separately. `conventions.md` defines the post-publication rules approved in the design. `error-codes.md` uses:

```text
error code | HTTP status | module | meaning | retryable | operator action | source
```

Extract stable codes from module error definitions, Blueprint mappings, tests, and explicit failure constants. Do not normalize old text-only errors into invented stable codes.

- [ ] **Step 5: Verify source-route coverage and OpenAPI syntax**

Run:

```powershell
$inventory = Get-Content -Raw docs/architecture/api/route-inventory.md
$targets = @(
  'gateway/app.py','gateway/auth_blueprint.py','execution_v2/blueprint.py',
  'comment_campaign/blueprint.py','selector_probe/blueprint.py','tiktok_stats/blueprint.py'
)
$missing = @()
foreach ($match in (Select-String -Path $targets -Pattern '@(?:app|blueprint|bp)\.(?:route|get|post|put|patch|delete)\(')) {
  $relative = (Resolve-Path -Relative $match.Path) -replace '^\.\\','' -replace '\\','/'
  $reference = "${relative}:$($match.LineNumber)"
  if (-not $inventory.Contains($reference)) { $missing += $reference }
}
if ($missing) { throw "Undocumented routes: $($missing -join ', ')" }
python -c "import json,pathlib; p=pathlib.Path('docs/architecture/api/openapi.yaml'); d=json.loads(p.read_text(encoding='utf-8')); assert d['openapi']=='3.1.0'; assert isinstance(d['paths'],dict) and d['paths']; assert isinstance(d['components']['schemas'],dict); print('openapi: PASS')"
```

Expected: `openapi: PASS`, no missing routes.

- [ ] **Step 6: Commit the HTTP contract**

```powershell
git add docs/architecture/api
git commit -m "docs(api): add current HTTP contract"
```

### Task 5: Topic Tree, Redis Keyspace, and Message Schemas

**Files:**
- Create: `docs/architecture/messaging/README.md`
- Create: `docs/architecture/messaging/topic-tree.md`
- Create: `docs/architecture/messaging/redis-keyspace.md`
- Create: `docs/architecture/messaging/schemas/comment-campaign-prepare.schema.json`
- Create: `docs/architecture/messaging/schemas/comment-campaign-submit.schema.json`
- Create: `docs/architecture/messaging/schemas/selector-probe-run.schema.json`
- Create: `docs/architecture/messaging/schemas/selector-publication-outbox.schema.json`
- Create: `docs/architecture/messaging/schemas/webhook-outbox.schema.json`

**Interfaces:**
- Consumes: RQ job entry points, Redis constants/Lua, Celery task calls, and SQLite Outbox tables.
- Produces: current asynchronous-contract inventory and schemas for stable payloads.

- [ ] **Step 1: Establish missing schema baseline**

```powershell
if (Test-Path docs/architecture/messaging/schemas) { throw 'Message schemas already exist' }
```

- [ ] **Step 2: Write topic tree and ownership**

Document these four mechanisms independently:

1. RQ queue `browser_v2_comment_campaign`.
2. Redis namespaces beginning `browser_v2:comment_campaign:` and configured Selector namespace.
3. Existing Celery/Redis broker use.
4. SQLite durable Outboxes.

For every message/key/table, record producer, consumer, payload, job/key identity, TTL, lease, idempotency, retry, dead/failed state, security classification, and source.

- [ ] **Step 3: Write five strict JSON Schemas**

Each schema must declare:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:local:architecture:<message-name>",
  "type": "object",
  "properties": {},
  "required": [],
  "additionalProperties": false
}
```

RQ schemas must permit only safe IDs and generation/revision values. They must reject raw AdsPower IDs, WebSocket URLs, cookies, Redis URLs, and approval tokens. Outbox schemas must match persisted payload fields in the current store implementation.

- [ ] **Step 4: Verify JSON syntax and security constraints**

```powershell
python -c "import json,pathlib; files=list(pathlib.Path('docs/architecture/messaging/schemas').glob('*.json')); assert len(files)==5; [json.loads(p.read_text(encoding='utf-8')) for p in files]; print('message schemas: PASS')"
rg -n 'raw_adspower_id|raw_profile_id|ws_url|cookie|authorization|api_key|approval_token' docs/architecture/messaging/schemas
if ($LASTEXITCODE -eq 0) { throw 'Sensitive field admitted by public message schema' }
```

Expected: `message schemas: PASS`, no forbidden fields.

- [ ] **Step 5: Commit messaging documentation**

```powershell
git add docs/architecture/messaging
git commit -m "docs(messaging): map queues keys and outboxes"
```

### Task 6: Storage Catalog, Entity Relationships, and State Machines

**Files:**
- Create: `docs/architecture/data/README.md`
- Create: `docs/architecture/data/storage-map.md`
- Create: `docs/architecture/data/database-schema.md`
- Create: `docs/architecture/data/entity-relationships.md`
- Create: `docs/architecture/data/migrations-and-backups.md`
- Create: `docs/architecture/data/state-machines/execution-v2.md`
- Create: `docs/architecture/data/state-machines/selector-probe.md`
- Create: `docs/architecture/data/state-machines/comment-campaign.md`
- Create: `docs/architecture/data/state-machines/tiktok-stats.md`
- Create: `docs/architecture/data/state-machines/publishing.md`

**Interfaces:**
- Consumes: SQLAlchemy models, SQLite DDL, state enums, transition maps, tests, and backup/config code.
- Produces: canonical table and state references used by module and operations guides.

- [ ] **Step 1: Capture table sources before writing**

```powershell
$ddl = rg -n 'CREATE TABLE IF NOT EXISTS' gateway execution_v2 comment_campaign selector_probe tiktok_stats
$orm = rg -n '__tablename__\s*=' models.py comment_campaign/models.py
if (-not $ddl -or -not $orm) { throw 'Expected both DDL and ORM table sources' }
```

- [ ] **Step 2: Write storage map and database catalog**

For every table/model, record store/database, columns, types, nullability, defaults, primary/foreign keys, unique constraints, indexes, JSON structure, readers, writers, transaction boundaries, retention, backup, sensitivity, and source line.

Also catalog non-relational persistence: Redis, `config.json`, content files, JSONL logs, screenshots, Evidence, encrypted cookie file, R2, and Buffer.

- [ ] **Step 3: Write Mermaid entity relationships**

Provide separate diagrams for:

- management/auth;
- Execution V2;
- Selector Probe;
- TikTok Stats;
- Comment Campaign;
- legacy account/content/publishing stores.

Do not draw foreign keys that the database does not enforce. Label logical-only relationships explicitly.

- [ ] **Step 4: Write five state-machine references**

Each state document includes enum values, initial state, legal transitions, trigger, precondition, side effect, terminal states, retry/recovery, concurrency guard, source, and Mermaid state diagram.

For legacy modules without a centralized transition function, use the exact warning: `该图为源码事实汇总；当前没有集中状态机实现。`

- [ ] **Step 5: Verify every declared table is documented**

```powershell
$catalog = Get-Content -Raw docs/architecture/data/database-schema.md
$names = @()
Get-ChildItem gateway,execution_v2,comment_campaign,selector_probe,tiktok_stats -Recurse -Filter *.py | ForEach-Object {
  $text = Get-Content -Raw $_.FullName
  [regex]::Matches($text, 'CREATE TABLE IF NOT EXISTS\s+([A-Za-z0-9_]+)') | ForEach-Object { $names += $_.Groups[1].Value }
  [regex]::Matches($text, '__tablename__\s*=\s*["'']([^"'']+)["'']') | ForEach-Object { $names += $_.Groups[1].Value }
}
$text = Get-Content -Raw models.py
[regex]::Matches($text, '__tablename__\s*=\s*["'']([^"'']+)["'']') | ForEach-Object { $names += $_.Groups[1].Value }
$missing = $names | Sort-Object -Unique | Where-Object { -not $catalog.Contains("`$_`") }
if ($missing) { throw "Undocumented tables: $($missing -join ', ')" }
```

Expected: no missing table names.

- [ ] **Step 6: Commit data and state documentation**

```powershell
git add docs/architecture/data
git commit -m "docs(data): catalog storage and state machines"
```

### Task 7: Frontend, Backend, and Windows Executor Guides

**Files:**
- Create: `docs/architecture/frontend/current-frontend.md`
- Create: `docs/architecture/frontend/page-inventory.md`
- Create: `docs/architecture/frontend/navigation.md`
- Create: `docs/architecture/frontend/ui-conventions.md`
- Create: `docs/architecture/frontend/frontend-gap-analysis.md`
- Create: `docs/architecture/backend/flask-application.md`
- Create: `docs/architecture/backend/service-boundaries.md`
- Create: `docs/architecture/backend/dependency-direction.md`
- Create: `docs/architecture/backend/background-workers.md`
- Create: `docs/architecture/backend/error-handling.md`
- Create: `docs/architecture/executor/overview.md`
- Create: `docs/architecture/executor/adspower-adapter.md`
- Create: `docs/architecture/executor/cdp-session-lifecycle.md`
- Create: `docs/architecture/executor/element-location.md`
- Create: `docs/architecture/executor/humanized-actions.md`
- Create: `docs/architecture/executor/batching-and-window-tiling.md`
- Create: `docs/architecture/executor/safety-boundaries.md`

**Interfaces:**
- Consumes: Tasks 1–2 module naming and Task 4 API contract.
- Produces: role-specific implementation guides for frontend, backend, and executor developers.

- [ ] **Step 1: Establish absent role-guide baseline**

```powershell
foreach ($path in @('docs/architecture/frontend','docs/architecture/backend','docs/architecture/executor')) {
  if (Test-Path $path) { throw "Unexpected existing path: $path" }
}
```

- [ ] **Step 2: Write frontend current-state and page inventory**

Document npm, `package-lock.json`, Jinja templates, static JS/CSS, `node:test`, shared sidebar/shell/fetch assets, polling rules, CSRF, local-direct behavior, and text-safe rendering. `page-inventory.md` maps each page URL to Template, JS, CSS, APIs, permission, polling, and test file.

`frontend-gap-analysis.md` states that pnpm workspace, component library, React/Vue, and generated TypeScript client are absent. It may describe impact and prerequisites, but not an approved migration.

- [ ] **Step 3: Write backend structure and dependency direction**

Explain `create_app`, Blueprint registration, global guards, lazy factories, Route → Service/Domain → Store/Adapter direction, worker construction, close semantics, and current exceptions in `gateway/app.py`. Do not represent the current system as microservices.

- [ ] **Step 4: Write executor lifecycle and safety guides**

Cover AdsPower request rate limits, public Profile identity boundary, CDP connection, page selection, readiness, Locator uniqueness/visibility, input/contenteditable behavior, ghost-cursor bridge, keyboard timing, wheel/ArrowDown behavior, batch size, tiling, leases, screenshots, stop+is_active confirmation, failure isolation, and no automatic replay after uncertain submit.

- [ ] **Step 5: Verify role-guide file counts and forbidden claims**

```powershell
if ((Get-ChildItem docs/architecture/frontend -File).Count -ne 5) { throw 'Expected 5 frontend docs' }
if ((Get-ChildItem docs/architecture/backend -File).Count -ne 5) { throw 'Expected 5 backend docs' }
if ((Get-ChildItem docs/architecture/executor -File).Count -ne 7) { throw 'Expected 7 executor docs' }
rg -n '当前.*微服务|当前.*pnpm workspace|当前.*TypeScript 客户端已生成' docs/architecture/frontend docs/architecture/backend docs/architecture/executor
if ($LASTEXITCODE -eq 0) { throw 'False current-state claim found' }
```

- [ ] **Step 6: Commit role guides**

```powershell
git add docs/architecture/frontend docs/architecture/backend docs/architecture/executor
git commit -m "docs(handover): add frontend backend executor guides"
```

### Task 8: Operations, Development Rules, and Gap Register

**Files:**
- Create: `docs/architecture/operations/local-setup.md`
- Create: `docs/architecture/operations/launcher-and-processes.md`
- Create: `docs/architecture/operations/environment-variables.md`
- Create: `docs/architecture/operations/docker.md`
- Create: `docs/architecture/operations/logs-and-evidence.md`
- Create: `docs/architecture/operations/backup-and-restore.md`
- Create: `docs/architecture/operations/health-checks.md`
- Create: `docs/architecture/operations/troubleshooting.md`
- Create: `docs/architecture/development/getting-started.md`
- Create: `docs/architecture/development/repository-layout.md`
- Create: `docs/architecture/development/coding-style.md`
- Create: `docs/architecture/development/commits.md`
- Create: `docs/architecture/development/pull-requests.md`
- Create: `docs/architecture/development/testing.md`
- Create: `docs/architecture/development/parallel-development.md`
- Create: `docs/architecture/development/adding-an-api.md`
- Create: `docs/architecture/development/adding-a-page.md`
- Create: `docs/architecture/development/documentation-maintenance.md`
- Create: `docs/architecture/gaps/README.md`
- Create: `docs/architecture/gaps/frontend-monorepo.md`
- Create: `docs/architecture/gaps/shared-component-library.md`
- Create: `docs/architecture/gaps/generated-ts-client.md`
- Create: `docs/architecture/gaps/root-docker-compose.md`
- Create: `docs/architecture/gaps/ci-cd.md`

**Interfaces:**
- Consumes: module/process/config/test facts from Tasks 1–7.
- Produces: operational handover, post-publication contribution rules, and an explicit not-implemented register.

- [ ] **Step 1: Establish absent operations/development baseline**

```powershell
foreach ($path in @('docs/architecture/operations','docs/architecture/development','docs/architecture/gaps')) {
  if (Test-Path $path) { throw "Unexpected existing path: $path" }
}
```

- [ ] **Step 2: Write local setup and process operations**

Document supported Windows assumptions, bundled/system Python distinction, npm install, Redis/MySQL optional checks, TikTok API Compose, launcher start/stop order, old Flask process termination rule, worker heartbeat, failure cleanup, and paths for logs/evidence/databases. Commands must be copyable and must not contain real secrets.

- [ ] **Step 3: Write configuration, backup, health, and troubleshooting**

`environment-variables.md` uses columns:

```text
name | process | source precedence | default | required | sensitive | effect | source
```

Backups distinguish config backups, SQLite copies with services stopped, MySQL backup, evidence retention, and files that must not be copied into Git. Troubleshooting begins with symptoms and read-only checks before restart or cleanup.

- [ ] **Step 4: Write enforceable development rules**

Record the approved post-publication conventions for OpenAPI-first changes, UI shell reuse, response envelopes, revision/CAS, Python layering, JavaScript state isolation, Conventional Commits, PR evidence, parallel ownership, tests, and documentation synchronization. Existing violations are legacy exceptions, not instructions to rewrite unrelated code.

- [ ] **Step 5: Write the five explicit gap documents**

Each gap document uses:

```markdown
# Capability name
## Current status: not implemented
## Current substitute
## Operational impact
## Preconditions for future work
## Decision required before implementation
## Evidence
```

Do not provide an implementation plan or imply approval for migration.

- [ ] **Step 6: Verify required operations and gap statements**

```powershell
if ((Get-ChildItem docs/architecture/operations -File).Count -ne 8) { throw 'Expected 8 operations docs' }
if ((Get-ChildItem docs/architecture/development -File).Count -ne 10) { throw 'Expected 10 development docs' }
if ((Get-ChildItem docs/architecture/gaps -File).Count -ne 6) { throw 'Expected 6 gap docs' }
foreach ($file in (Get-ChildItem docs/architecture/gaps -Filter *.md | Where-Object Name -ne 'README.md')) {
  if (-not (Select-String -Path $file.FullName -SimpleMatch 'Current status: not implemented')) {
    throw "$($file.Name) does not state not implemented"
  }
}
```

- [ ] **Step 7: Commit operations, development, and gaps**

```powershell
git add docs/architecture/operations docs/architecture/development docs/architecture/gaps
git commit -m "docs(operations): add handover runbooks and rules"
```

### Task 9: Final Index, Coverage Audit, and Handover Verification

**Files:**
- Modify: `docs/architecture/README.md`
- Create: `docs/architecture/VERIFICATION.md`

**Interfaces:**
- Consumes: every document and machine contract from Tasks 1–8.
- Produces: final role-based navigation and reproducible proof that the documentation center matches the reviewed working tree.

- [ ] **Step 1: Verify all planned files exist before finalizing the index**

Run:

```powershell
$expectedCounts = @{
  'system'=5; 'modules'=11; 'adr'=10; 'api'=6; 'messaging'=3;
  'data'=5; 'frontend'=5; 'backend'=5; 'executor'=7;
  'operations'=8; 'development'=10; 'gaps'=6
}
foreach ($entry in $expectedCounts.GetEnumerator()) {
  $count = (Get-ChildItem "docs/architecture/$($entry.Key)" -File).Count
  if ($count -ne $entry.Value) { throw "$($entry.Key): expected $($entry.Value), got $count" }
}
if ((Get-ChildItem docs/architecture/messaging/schemas -Filter *.json).Count -ne 5) { throw 'Expected 5 message schemas' }
if ((Get-ChildItem docs/architecture/data/state-machines -Filter *.md).Count -ne 5) { throw 'Expected 5 state-machine docs' }
```

- [ ] **Step 2: Finalize the role-based root index**

Update `README.md` with complete relative links and these reading paths:

1. new developer;
2. frontend developer;
3. backend developer;
4. executor developer;
5. operations maintainer.

Add version, extraction date, Git baseline, dirty-worktree scope, supported run mode, verified scope, unverified scope, maintenance owner field, and last review date. The maintenance owner field must name a role such as `项目维护者`, not a person who has not accepted ownership.

- [ ] **Step 3: Run a relative-link checker**

Run:

```powershell
python -c "import pathlib,re,sys; root=pathlib.Path('docs/architecture'); missing=[]; pat=re.compile(r'\[[^]]+\]\(([^)]+)\)');
for p in root.rglob('*.md'):
 text=p.read_text(encoding='utf-8')
 for target in pat.findall(text):
  if '://' in target or target.startswith('#'): continue
  path=(p.parent/target.split('#',1)[0]).resolve()
  if not path.exists(): missing.append(f'{p}: {target}')
assert not missing, '\n'.join(missing); print('links: PASS')"
```

Expected: `links: PASS`.

- [ ] **Step 4: Run syntax, encoding, placeholder, and secret scans**

Run:

```powershell
python -c "import json,pathlib; root=pathlib.Path('docs/architecture'); [p.read_text(encoding='utf-8') for p in root.rglob('*') if p.is_file()]; json.loads((root/'api/openapi.yaml').read_text(encoding='utf-8')); [json.loads(p.read_text(encoding='utf-8')) for p in (root/'messaging/schemas').glob('*.json')]; print('syntax+utf8: PASS')"
rg -n 'TBD|TODO|待定|稍后补充|锛|銆|鈥|�' docs/architecture
if ($LASTEXITCODE -eq 0) { throw 'Placeholder or mojibake found' }
rg -n '(?i)(api[_-]?key|authorization|cookie|redis_password)\s*[:=]\s*[^` ]+|wss?://[^` ]+' docs/architecture
if ($LASTEXITCODE -eq 0) { throw 'Potential secret or WebSocket value found' }
```

Expected: `syntax+utf8: PASS`, no scan matches.

- [ ] **Step 5: Re-run route and table coverage checks**

Repeat Task 4 Step 5 and Task 6 Step 5 unchanged. Expected: no undocumented route declarations and no undocumented tables.

- [ ] **Step 6: Write the verification report**

`VERIFICATION.md` must record exact commands, date, Git baseline, pass/fail/skip results, known runtime tests not executed, and these explicit statements:

- no real AdsPower Profile was started;
- no real TikTok publish/comment was performed;
- runtime health was not inferred from configuration alone;
- no existing business file was changed by this documentation task.

- [ ] **Step 7: Inspect the final diff**

Run:

```powershell
git diff --check -- docs/architecture
git status --short -- docs/architecture docs/superpowers/specs/2026-08-08-current-project-handover-documentation-design.md docs/superpowers/plans/2026-08-08-current-project-handover-documentation.md
```

Expected: only documentation files owned by this project are listed; `git diff --check` is clean.

- [ ] **Step 8: Commit final index and verification**

```powershell
git add docs/architecture/README.md docs/architecture/VERIFICATION.md
git commit -m "docs(architecture): finalize handover index"
```

## Final Delivery Gate

Before declaring completion, the implementer must be able to answer yes to all of these questions:

- Can a new developer find every major module and its entry point from `README.md`?
- Does every source route appear in OpenAPI or the HTML route inventory?
- Does every table/model appear in the database catalog?
- Are queue, Redis, Celery, and Outbox mechanisms distinguished rather than collapsed into a fictional bus?
- Are current and not-implemented capabilities unmistakably separated?
- Do all operational commands avoid secrets and real platform side effects?
- Do all machine-readable files parse with the documented command?
- Are pre-existing dirty-worktree files untouched and unstaged?
