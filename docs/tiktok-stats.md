# TikTok 数据统计模块运维指南

本模块在本机运行一个第三方 TikTok 抓取服务，定时保存账号快照、作品现状和每日汇总。网页控制台只读取本地 SQLite 数据；抓取工作由独立 worker 进程完成。默认业务时区为 `Asia/Shanghai`。

## 1. 数据语义

- 每 3 小时采集一次账号资料和最新作品。上海时间每天有 8 个时隙：`00:00`、`03:00`、`06:00`、`09:00`、`12:00`、`15:00`、`18:00`、`21:00`。
- 每个上海自然日对每个启用账号执行一次全部作品校准。只有完整校准才可形成或替换当日永久汇总；失败或不完整结果不能覆盖完整结果。
- 当日变化量为“当日完整快照总量 − 前一自然日完整快照总量”。负数会原样保存和显示，用来反映删帖或平台修正，不会被改成 0。
- 单日表中的空值不是 0：`first_day` 表示首日没有前一日基线，`missing_previous` 表示缺少前日，`incomplete` 表示当日校准不完整，`missing` 表示该日没有记录。
- 日期范围表按“结束日总量 − 开始日前一日总量”计算。范围状态还可能是 `missing_end`、`incomplete_end` 或 `incomplete_previous`；这些状态下变化量保持为空。
- 账号详情页展示永久日序列、当前作品及采集记录；趋势页按日期横向比较多个账号。表格、详情和趋势使用同一套空值、负数和完整性语义。
- 默认保留最近 90 天的三小时快照。每日汇总、当前作品、跟踪账号和采集运行审计永久保留；清理任务只删除未被每日汇总引用的过期快照。

## 2. 前置条件

1. Windows 10/11、项目自带 Python 虚拟环境及 Node.js 依赖可用。
2. 安装并启动 Docker Desktop。可以从 Docker 官方安装器安装，或在管理员 PowerShell 中运行：

   ```powershell
   winget install --exact --id Docker.DockerDesktop
   ```

3. Docker Desktop 必须由运行本系统的同一个 Windows 用户启动。重新登录后执行：

   ```powershell
   docker version
   docker info
   ```

   两条命令都必须能读取 Server 信息。若命名管道权限被拒绝，在管理员 PowerShell 中把当前用户加入 `docker-users`，注销再登录，并确认 `%USERPROFILE%\.docker\config.json` 可由当前用户读取；不要以另一个管理员账号启动 Docker 后再用普通账号运行本系统。

4. 主机和容器必须能解析并访问 GitHub（安装时）及 TikTok（运行时）。公司网络、地区网络或 TLS 检查设备可能需要在 Docker Desktop 中配置代理。代理配置后应重新启动 Docker Desktop，并从容器内验证 DNS/HTTPS 出站能力。不要把抓取端口映射到局域网或公网。

## 3. 固定版本安装抓取服务

上游项目为 [Evil0ctal/Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API)，许可证为 Apache-2.0。本地客户端契约当前固定到提交：

```text
42784ffc83a72a516bfe952153ad7e2a3998d16c
```

安装脚本不会跟随 `main`。先审阅固定提交、`LICENSE`、Dockerfile 和接口变化，再验证下载摘要：

```powershell
$commit = '42784ffc83a72a516bfe952153ad7e2a3998d16c'
$archivePath = Join-Path ([System.IO.Path]::GetTempPath()) "tiktok-api-$commit.zip"
Invoke-WebRequest "https://github.com/Evil0ctal/Douyin_TikTok_Download_API/archive/$commit.zip" -OutFile $archivePath
$archiveSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $archivePath).Hash.ToLowerInvariant()
Remove-Item -LiteralPath $archivePath
& .\scripts\install_tiktok_api.ps1 -CommitSha $commit -ArchiveSha256 $archiveSha256
```

安装程序会在临时目录构建镜像，验证归档摘要，保留上游 `LICENSE`，生成逐文件 `SOURCE-MANIFEST.json` 和 `VERSION.json`，然后原子替换本地 vendor 目录。失败时保留旧安装。生成的运行目录和 vendor 源码不会进入版本控制，但 `docker-compose.yml`、脚本和文档会保留。

启动和健康检查：

```powershell
& .\scripts\start_tiktok_api.ps1
Invoke-WebRequest http://127.0.0.1:53281/docs -UseBasicParsing
docker compose -f .\services\tiktok_api\docker-compose.yml ps
```

服务只绑定 `127.0.0.1:53281`。停止和查看日志：

```powershell
docker compose -f .\services\tiktok_api\docker-compose.yml logs --tail 200
docker compose -f .\services\tiktok_api\docker-compose.yml down
```

`start_tiktok_api.ps1` 会重新计算 vendor 源码清单；源码、提交、镜像标签或清单不一致时拒绝启动。

## 4. Cookie 配置和轮换

在“数据统计 → 统计设置”中手动粘贴 Cookie 并保存，然后点击验证。不要把 Cookie 写入 `config.json`、`.env`、命令行、日志、上游 YAML 或源码。

- Cookie 通过 Windows DPAPI 按“当前 Windows 用户”加密，默认保存在 `data/stats/tiktok_cookie.json`。文件只有密文和验证状态。
- 页面和接口是单向的：只显示“未配置、已保存、有效、无效、检查时间”等状态，绝不回显明文或可用于还原的片段。
- Cookie 过期、退出登录或平台撤销会导致验证/采集失败。使用新的 Cookie 再次保存会原子替换旧密文；随后重新验证。
- DPAPI 备份只能在同一 Windows 安装、同一用户身份下解密。更换电脑、重装 Windows 或改用另一个用户后，应重新粘贴 Cookie，不要尝试把密文转换为明文。
- worker 命令行验证至少需要一个已启用账号：

  ```powershell
  .\.venv\Scripts\python.exe -m tiktok_stats.worker validate-cookie
  ```

## 5. 导入和管理账号

统计设置支持两种入口：

- 独立粘贴用户名列表：接受 `creator`、`@creator`、`https://www.tiktok.com/@creator`，可用换行、逗号或空白分隔。
- 从现有账号库勾选含 TikTok channel 的账号加入。

用户名按大小写无关的规范键去重。重复导入不会增加重复行；重新导入已停用账号会重新启用。无效用户名会逐项报告，不影响同批有效账号。导入、启停和重命名均写入本地统计数据库，刷新页面或重启 Flask 后仍然存在。

## 6. Worker、调度和故障恢复

通过 `launcher.py` 启动系统时，启动器会监管一个独立统计 worker；重复点击启动不会创建第二个 worker，关闭启动器会先请求 worker 正常退出，超时后才终止进程。网页 GET 请求不会启动、接管或重启 worker。

也可在项目根目录独立运行：

```powershell
.\.venv\Scripts\python.exe -m tiktok_stats.worker serve
.\.venv\Scripts\python.exe -m tiktok_stats.worker tick
.\.venv\Scripts\python.exe -m tiktok_stats.worker incremental
.\.venv\Scripts\python.exe -m tiktok_stats.worker full
.\.venv\Scripts\python.exe -m tiktok_stats.worker cleanup
.\.venv\Scripts\python.exe -m tiktok_stats.worker validate-cookie
```

- `serve` 常驻调度；`tick` 执行当前应到期的增量和每日完整任务；`incremental`/`full` 只执行对应类型；`cleanup` 执行 90 天清理。
- 已认领时隙和运行状态保存在 SQLite 中。worker 重启后会补跑遗漏的三小时时隙，但不会重复已经完成或被另一个 worker 持有的时隙。
- 每个调度工作使用数据库租约。长任务持续续约；进程崩溃后，只有租约过期才允许另一个 worker 接管。运行审计会保留 `completed`、`partial` 或 `failed` 状态及脱敏错误摘要。
- 默认路径可通过环境变量覆盖，适合备份演练或隔离测试：

  ```powershell
  $env:TIKTOK_STATS_DB_PATH = 'D:\app-data\tiktok_stats.db'
  $env:TIKTOK_STATS_COOKIE_PATH = 'D:\app-data\tiktok_cookie.json'
  $env:TIKTOK_STATS_API_URL = 'http://127.0.0.1:53281'
  ```

  API 地址必须保持 loopback-only；不要指向局域网或公网服务。

## 7. 页面筛选和排序

打开 `http://127.0.0.1:5000/tiktok-stats`：

- 日期支持单日或起止范围；账号名可搜索，并可按启用状态和数据完整度筛选。
- 表格可按发布量、点赞量、浏览量、评论量升序或降序排列。排序只针对所选日期语义下的变化量。
- `0` 表示已完整采集且没有变化；空白/`—` 表示没有可比较的完整基线；负数会使用减少样式显示。
- 点击账号进入详情；趋势子页面用于多个账号跨日期比较。删帖标志表示完整校准未再次发现该作品，不等于本地删除历史记录。

## 8. 备份与恢复

默认重要路径：

| 内容 | 默认路径 | 说明 |
| --- | --- | --- |
| 统计库 | `data/stats/tiktok_stats.db` | 账号、快照、作品、日汇总、运行和租约 |
| Cookie 密文 | `data/stats/tiktok_cookie.json` | DPAPI 密文，只能由原 Windows 用户恢复 |
| 设置与策略 | `config.json` | 元素别名、行为模式、积木策略及滚动范围；旁边最多保留 5 个 `backup` 文件 |
| 现有账号库 | `accounts.db` | 从现有账号勾选导入时使用 |

### 推荐备份顺序

1. 关闭启动器，确认 Flask 和统计 worker 已停止；停止不是为了修复数据，而是让 Cookie/设置/数据库形成同一时间点。
2. 使用 SQLite 在线备份 API 生成一个完整数据库副本。它会正确合并 WAL，不要只复制正在使用的 `.db`：

   ```powershell
   $backupRoot = 'D:\backups\tiktok-stats-2026-07-22'
   New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
   $env:TIKTOK_STATS_BACKUP_SOURCE = (Resolve-Path '.\data\stats\tiktok_stats.db').Path
   $env:TIKTOK_STATS_BACKUP_TARGET = Join-Path $backupRoot 'tiktok_stats.db'
   .\.venv\Scripts\python.exe -c "import os,sqlite3; s=sqlite3.connect(os.environ['TIKTOK_STATS_BACKUP_SOURCE']); d=sqlite3.connect(os.environ['TIKTOK_STATS_BACKUP_TARGET']); s.backup(d); d.close(); s.close()"
   ```

3. 复制 Cookie 密文、`config.json`、所需的 `config.json.backup.*` 和（如需保留现有账号勾选来源）`accounts.db`。限制备份目录访问权限，Cookie 密文仍属于敏感材料。
4. 校验备份数据库：

   ```powershell
   $env:TIKTOK_STATS_BACKUP_TARGET = Join-Path $backupRoot 'tiktok_stats.db'
   .\.venv\Scripts\python.exe -c "import os,sqlite3; c=sqlite3.connect(os.environ['TIKTOK_STATS_BACKUP_TARGET']); print(c.execute('PRAGMA integrity_check').fetchone()[0]); c.close()"
   ```

如果无法使用 SQLite 备份 API，必须先完全停止所有进程，再把 `.db`、同名 `-wal` 和 `-shm` 作为一个不可分割集合复制；不能在运行中只复制其中一个文件。

### 恢复顺序

1. 停止启动器、Flask、worker 和抓取容器，并将当前文件另存为可回滚副本。
2. 把备份得到的独立 `tiktok_stats.db` 放回原路径；删除目标位置遗留的同名 `-wal`/`-shm`，不要把旧旁路文件与新主库混用。
3. 恢复 `config.json` 和 Cookie 密文。Cookie 必须回到由同一 Windows 用户运行的环境；否则重新粘贴。
4. 先运行数据库 `PRAGMA integrity_check`，再启动抓取服务和 worker，最后启动 Flask；检查统计页账号数、最近运行和 Cookie 状态。

## 9. 常见故障

| 现象 | 检查与处理 |
| --- | --- |
| Docker 命名管道或 `config.json` 权限拒绝 | 使用同一 Windows 用户启动 Docker Desktop；检查 `docker version` 的 Server；修复当前用户对 `%USERPROFILE%\.docker` 的权限，必要时加入 `docker-users` 后注销登录。 |
| GitHub 下载失败 | 检查 DNS、TLS 检查软件、代理和 GitHub 访问；保留固定提交与 SHA-256 校验，不要改成跟随 `main`。 |
| TikTok 不可达/超时 | 先确认主机及容器出站网络、DNS、地区限制和代理；查看 compose 日志。不要通过开放 53281 到公网来规避。 |
| Cookie 无效 | 重新从有效登录会话复制完整 Cookie，在统计设置中覆盖保存并验证；不要在日志或工单中粘贴。 |
| 私密账号或账号不存在 | 单账号会记录失败，不阻断其他账号；确认用户名、账号可见性和地区可访问性。 |
| 上游接口契约改变 | 停止升级，保持当前固定镜像；对照固定提交的响应结构更新客户端夹具和测试，验证后再切换。 |
| worker 停止 | 检查启动器状态和独立 worker 进程；查看 `collection_runs` 最近状态，修复前置条件后运行 `tick` 补跑。 |
| 运行长期停在 `running` | 不要手工删除审计行或租约。确认旧进程已退出，等待租约过期后由新 worker 接管；重复启动不能绕过有效租约。 |

## 10. 安全升级上游

1. 选择一个不可变的 40 位提交 SHA，不使用分支名或浮动标签。
2. 阅读该提交到候选提交的代码、Dockerfile、依赖、接口和许可证变化，确认仍为可接受的 Apache-2.0 使用方式。
3. 单独下载归档并记录 SHA-256；用 `-ArchiveSha256` 安装。保存新的 `VERSION.json`、源码清单摘要和上游 `LICENSE`。
4. 更新客户端契约常量与脱敏 JSON 夹具；运行全部 Python/Node、语法、秘密扫描和本地合同测试。
5. 在隔离端口构建并验证健康、Cookie 单向显示、少量受控账号的增量/完整采集与负数/空值页面语义。
6. 只有所有验证通过才替换生产元数据和镜像；失败时继续使用旧固定镜像，不修改现有 Cookie 与统计数据库。

## 11. 执行策略中的滚动次数

滚动动作只配置“最小/最大滚轮次数”和事件间隔。每次执行时随机抽取合成浏览器 `mouse.wheel` 事件总数 `N`，并准确产生 `N` 个事件；每个事件使用隐藏的固定增量 `120`，向上为负方向、向下为正方向。旧策略中的单次距离会自动规范化为 `120`，不再作为用户参数。

旧策略中的隐藏 `burst_count` 仅为兼容原有分组节奏而保留，编辑和保存可见次数时不会丢失或覆盖它；它不会改变总事件数 `N`。新动作默认隐藏分组值为 `[1, 1]`。保存后的 `total_count` 和隐藏兼容值都写入 `config.json`，刷新页面或重启后保持不变。

## 12. 当前外部验证状态

自动测试使用本地夹具和隔离临时路径，不代表实网 TikTok 成功。当前环境仍需先解决以下外部条件，才能完成真实集成验证：

- 当前 Windows 用户可访问 Docker Engine；
- 当前网络可下载固定 GitHub 提交并允许容器访问 TikTok；
- 用户提供有效 Cookie 和少量受控 TikTok 账号；
- 使用 AdsPower 实际窗口验证一次滚动事件计数。

这些项目属于实网/运行环境验证，不能由本地单元和集成测试替代。

## 13. 发布前自动检查

从项目根目录运行以下检查。任何一项失败都不应进入实网验证：

```powershell
.\.venv\Scripts\python.exe -m pytest tests -p no:cacheprovider -q
npm.cmd run test:node
.\.venv\Scripts\python.exe -m compileall -q tiktok_stats gateway tests launcher.py browser_actions.py browser_strategy_config.py browser_strategy_runtime.py
.\.venv\Scripts\python.exe scripts\scan_repository_secrets.py
```

秘密扫描从仓库根目录遍历所有已知文本后缀和明确文本文件名，因此未来新增的根目录源码、根日志、`package*.json`、`.env.example`、扫描器自身、docs、tests 和 `.superpowers` 会自动纳入。它只报告文件和规则名称，不输出疑似凭证值。第三方依赖/虚拟环境、不可访问的测试工作目录，以及仓库根运行期 `config.json`/`.env`、DPAPI 密文文件和本地数据库被明确排除；子目录中的 `docs/config.json`、测试夹具 `config.json` 等仍会扫描。这些运行期资源必须依靠访问控制、备份规范和接口单向显示来保护。
