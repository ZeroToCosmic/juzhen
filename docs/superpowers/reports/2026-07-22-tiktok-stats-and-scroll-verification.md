# TikTok 数据统计与滚动动作最终验收报告

日期：2026-07-22  
环境：Windows，项目本地 `.venv`，时区 Asia/Shanghai

## 结论

本地实现与隔离数据验收可用：统计列表、账号详情、趋势矩阵、Cookie 安全状态、账号/策略重启持久化、三小时 worker 调度入口，以及滚动动作的最小/最大滚轮次数和旧数据隐藏参数均有自动化或隔离环境证据。

真实 TikTok 联调未完成，也不应被视为通过。当前用户无法访问 Docker Engine，GitHub 固定提交归档请求失败，固定版本抓取服务没有安装，未配置有效加密 Cookie；AdsPower 虽然在运行且可读取账号资料，但没有活动浏览器会话，也没有用户指定的诊断窗口。根据安全边界，本次没有运行安装器、没有真实 TikTok 请求、没有启动或修改 AdsPower 资料、没有执行真实滚轮诊断。

## 1. 安装前预检

批准的唯一上游提交：

```text
42784ffc83a72a516bfe952153ad7e2a3998d16c
```

### Docker 客户端与 Engine

命令：

```powershell
docker --version
docker version --format '{{json .}}'
docker info --format '{{json .ServerVersion}}'
```

结果：

- 客户端存在：`Docker version 29.5.3, build d1c06ef`，首条命令退出码 `0`。
- Docker 配置文件警告：`open %USERPROFILE%\.docker\config.json: Access is denied.`
- Engine 连接失败：`permission denied while trying to connect to the docker API at npipe:////./pipe/docker_engine`。
- `docker version` 和 `docker info` 均退出码 `1`；Server 为空。

判定：当前用户没有可用 Docker Engine。依照任务约束，已在任何下载、安装、构建、启动之前停止，未运行 `scripts/install_tiktok_api.ps1` 或 `scripts/start_tiktok_api.ps1`。

### GitHub 固定提交归档

命令：

```powershell
curl.exe -I -L --max-time 20 https://github.com/Evil0ctal/Douyin_TikTok_Download_API/archive/42784ffc83a72a516bfe952153ad7e2a3998d16c.tar.gz
```

结果：退出码 `35`：

```text
curl: (35) schannel: AcquireCredentialsHandle failed: SEC_E_NO_CREDENTIALS (0x8009030E)
```

判定：当前 Windows 会话无法通过 Schannel 获取 GitHub HTTPS 连接所需凭据，固定归档不可下载。未改用其他提交、镜像或未固定版本。

### 本地安装资产状态

命令：

```powershell
Get-ChildItem services\tiktok_api -Force
Test-Path services\tiktok_api\VERSION.json
Test-Path services\tiktok_api\SOURCE-MANIFEST.json
Test-Path services\tiktok_api\vendor
Test-Path services\tiktok_api\.env
Test-Path services\tiktok_api\docker-compose.yml
```

结果：`services/tiktok_api` 仅存在 `docker-compose.yml`；其余状态依次为 `False / False / False / False / True`。

因此本次没有可报告的已安装归档 SHA-256、源码树摘要或本地 License 文件：

| 项目 | 当前值 |
| --- | --- |
| 上游提交 | `42784ffc83a72a516bfe952153ad7e2a3998d16c` |
| 归档 SHA-256 | 未生成：归档未下载 |
| 源码树摘要 | 未生成：vendor 源码未安装 |
| License | 预期上游为 Apache-2.0；未安装，故没有本地安装副本可核验 |
| `VERSION.json` | 不存在 |
| `SOURCE-MANIFEST.json` | 不存在 |

### 抓取服务健康状态

命令：

```powershell
Invoke-WebRequest -UseBasicParsing -Uri http://127.0.0.1:53281/docs -TimeoutSec 5
```

结果：`无法连接到远程服务器`。固定版本抓取服务未运行，符合未安装状态。

## 2. Cookie 与真实 TikTok 边界

只访问了不会返回明文的公开状态接口，并检查默认文件是否存在：

```powershell
Invoke-RestMethod http://127.0.0.1:53331/api/tiktok-stats/settings/cookie
Test-Path data\stats\tiktok_cookie.json
```

隔离验收服务返回：

```json
{"status":{"checked_at":null,"configured":false,"message":null,"state":"missing"}}
```

默认加密 Cookie 文件存在状态：`False`。

未向终端、日志、数据库或报告输入 Cookie。因为没有已经配置且公开状态为有效的加密 Cookie，本次没有发起真实 TikTok 请求，也没有进行真实增量采集、全量校准或 Cookie 验证。

## 3. 本地统计 UI 与 API 隔离验收

验收使用系统临时目录中的 SQLite 种子库和临时 Cookie/设置路径，在 `127.0.0.1:53331` 启动本地 Flask 开发服务；不读取或写入正式统计库。完成后临时服务已精确关闭。

页面观察（无保留截图）：

- 主页面显示 2 个样例账号；当日汇总为作品 `+1`、点赞 `+105`、浏览 `+3300`、评论 `+18`。
- 表格能同时展示正数和负数变化、总量和数据状态；按浏览变化排序结果正确。
- 点击 Alpha 样例账号进入详情：最新完整总量为作品 `12`、点赞 `1400`、浏览 `28000`、评论 `310`；SVG 每日趋势可见，样例作品正常展示，异常记录为空时使用明确空状态。
- 趋势子页面展示 2 个账号的日期矩阵，样例作品变化分别含 `+2` 和 `-1`；URL 中保留视图、指标和翻页状态。
- 设置弹窗显示 Cookie 未配置；Cookie 输入框为 `type=password`、`autocomplete=new-password` 且值为空；服务和 worker 均显示未运行。
- 浏览器控制台没有 warning 或 error。

GET 只读与重启持久化由 Task 13 的真实临时文件系统测试覆盖：请求页面、账号、表格等 GET 前后，`tracked_accounts`、`account_snapshots`、`daily_account_metrics`、`posts_current`、`collection_runs` 行数不变；重新创建 Flask 应用后账号、完整快照、每日汇总、策略和加密 Cookie 公开状态仍存在，响应/设置/SQLite/日志均不含明文 Cookie。

## 4. Worker 隔离验收

在无账号、无 Cookie 的全新临时数据库中执行一次调度入口：

```powershell
$task14Db = Join-Path $env:TEMP ('codex-task14-worker-' + [guid]::NewGuid().ToString('N') + '.db')
$task14Cookie = Join-Path $env:TEMP ('codex-task14-cookie-' + [guid]::NewGuid().ToString('N') + '.json')
$env:TIKTOK_STATS_DB_PATH = $task14Db
$env:TIKTOK_STATS_COOKIE_PATH = $task14Cookie
$env:TIKTOK_STATS_API_URL = 'http://127.0.0.1:53281'
.\.venv\Scripts\python.exe -m tiktok_stats.worker tick
```

当次数据库位于系统临时目录。结果：退出码 `0`；临时数据库中账号数为 `0`，生成 1 条 `incremental` 调度运行记录；Cookie 文件未创建。由于没有账号，不会向未运行的抓取服务发请求。

## 5. 滚动动作验收

### 临时设置保存与重启读取

使用临时 `APP_CONFIG_PATH`，通过 `/api/browser/strategies` 保存策略，重新创建 Flask 应用后再读取：

```text
SAVE_STATUS 200
RELOAD_STATUS 200
VISIBLE_TOTAL_COUNT [4, 9]
HIDDEN_LEGACY_BURST_COUNT [2, 3]
DISTANCE 420
INTERVAL_SECONDS [0.2, 0.5]
PERSISTENCE_OK True
```

这证明界面可见的最小/最大滚轮次数会持久化，旧策略中界面隐藏的 `burst_count` 也不会在编辑、保存或刷新后丢失。新滚动动作默认隐藏值为 `[1, 1]`。

### 执行语义与 UI 回归

命令：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_tiktok_stats_restart_persistence.py tests\test_browser_strategy_config.py tests\test_browser_strategy_runtime.py tests\test_actions.py -p no:cacheprovider -q
node --test tests-js/browser-strategy-ui.test.js
```

本次新鲜结果：

- Python：`88 passed in 2.40s`，退出码 `0`。
- Node UI：`33 tests, 33 pass, 0 fail`，退出码 `0`。

覆盖内容包括：最小/最大值必须为有序正整数、随机次数配置、刷新/重启持久化、旧策略隐藏参数兼容，以及执行器实际调用 `page.mouse.wheel` 的次数严格等于抽取后的 `total_count`。这里的“滚轮次数”是合成浏览器 wheel 事件次数，不是像素距离；每次事件的距离由 `distance` 单独控制。

## 6. AdsPower 状态与未执行的真实诊断

只进行了只读检查：

```powershell
Invoke-RestMethod http://127.0.0.1:50325/status
Invoke-RestMethod http://127.0.0.1:53330/api/browser/sessions
Invoke-RestMethod http://127.0.0.1:53330/api/browser/adspower-windows
```

结果：

- AdsPower 本地 API：`{"code":0,"msg":"success"}`。
- 可读取资料数量：`21`；为避免泄露，未打印资料身份。
- 当前网关活动浏览器会话数量：`0`。

AdsPower 程序在运行不等于存在可安全操作的活动窗口。由于活动会话为 0，且用户没有为本次验收明确指定目标资料/窗口，未启动、停止、打开或修改任何真实资料，也未执行真实 wheel 事件计数。真实诊断仍待用户选择目标窗口后进行。

## 7. Task 13 自动化证据

以下是 Task 13 最终报告记录的完整自动化结果；本节引用该轮证据，不把它与本次真实外部联调混为一谈。

```powershell
.\.venv\Scripts\python.exe -m pytest tests -p no:cacheprovider -q
```

结果：`613 passed in 113.93s`。

```powershell
npm.cmd run test:node
```

结果：`109 tests, 109 pass, 0 fail`。

```powershell
.\.venv\Scripts\python.exe -m compileall -q tiktok_stats gateway tests launcher.py browser_actions.py browser_strategy_config.py browser_strategy_runtime.py scripts\scan_repository_secrets.py
```

结果：退出码 `0`。另有 27 个 JavaScript 文件通过 `node --check`，2 个 PowerShell 安装/启动脚本通过解析器语法检查。

```powershell
.\.venv\Scripts\python.exe scripts\scan_repository_secrets.py
```

结果：`secret scan passed: 325 text files`。

扫描器覆盖测试：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_repository_secret_scan.py -p no:cacheprovider -q
```

结果：`5 passed in 0.07s`。

## 8. 阻塞项与运行风险

| 项目 | 状态 | 风险/影响 |
| --- | --- | --- |
| Docker Engine | 阻塞 | 无法构建或启动固定版本抓取服务 |
| GitHub HTTPS | 阻塞 | 无法下载固定提交并生成真实摘要 |
| 固定版本服务 `53281` | 未安装/未运行 | 客户端真实调用不可验收 |
| 加密 Cookie | 未配置 | 禁止真实 TikTok 请求 |
| TikTok 平台 | 未联调 | 限流、验证码、字段变化和账号状态尚未在当前环境验证 |
| AdsPower wheel 诊断 | 待用户选择 | 有 21 个资料但活动会话为 0；未对真实窗口操作 |
| SQLite 单机运行 | 可用但需运维纪律 | 备份时需遵循 WAL 一致性；不要只复制主 `.db` 而忽略活跃 WAL |
| 3 小时/每日真实调度 | 仅本地自动化验证 | 长期运行、网络抖动和平台日界线仍需真实环境观察 |

## 9. 用户下一步准备与操作顺序

1. 在 Docker Desktop 中确认 Engine 已启动，并让当前 Windows 用户能够正常读取 `%USERPROFILE%\.docker\config.json`、访问 `npipe:////./pipe/docker_engine`。只需验证 `docker version --format '{{.Server.Version}}'` 返回服务端版本；不要通过本项目自动修改权限。
2. 修复当前 Windows 会话的 GitHub HTTPS/Schannel 凭据或企业代理设置，确认固定归档 URL 可访问。
3. 重新运行预检；只有 Docker Engine 与固定归档都可用时，才运行 `scripts/install_tiktok_api.ps1`。安装后核验 `VERSION.json`、`SOURCE-MANIFEST.json`、归档 SHA-256、源码树摘要和 Apache-2.0 License，再启动并检查 `http://127.0.0.1:53281/docs`。
4. 在“统计设置”页面手动粘贴 TikTok Cookie 并点击验证。不要把 Cookie 发到聊天、命令行或普通配置文件；验证后只确认公开状态为 `valid`。
5. 导入一小组非关键 TikTok 用户名，先手动运行一次增量采集和一次全量校准；核对列表、详情、趋势、负数修正与采集错误，再启用每 3 小时调度和每日全量校准。
6. 为滚轮实机诊断明确选择一个可测试的 AdsPower 资料/窗口并保持其已打开；随后再记录执行前后 wheel 事件数，确认抽取次数落在配置的最小/最大值内。未选择窗口前不要批量打开资料。
7. 实机验收后再次执行完整 Python/Node 测试和秘密扫描，并备份统计数据库、WAL/SHM（若存在）、加密 Cookie 文件、策略设置及来源清单。

## 10. Git 状态

该目录不是有效 Git 仓库。Task 13 记录的命令：

```powershell
git status --short
```

结果：退出码 `128`，`fatal: not a git repository (or any of the parent directories): .git`。本次未初始化、修复或提交 Git。
