# 启动器后台服务无窗口运行设计

日期：2026-07-23

## 目标

打开自动化系统启动器时，不再出现持续存在的命令窗口。启动器仍负责停止旧服务、检查环境、启动 Flask 和 TikTok 数据统计 worker，并在失败时通过启动器界面提供简短、可操作的错误原因。

## 已确认方案

采用 A1：

1. 提供真正无窗口的启动入口，以虚拟环境中的 `pythonw.exe` 启动 `launcher.py`。
2. 保留 `start_console.cmd` 作为兼容入口，但它只负责调用无窗口入口并立即退出。
3. UAC 提权后的管理员进程继续使用当前解释器；从无窗口入口启动时，该解释器为 `pythonw.exe`。
4. Flask 和统计 worker 显式使用 Windows 无窗口进程参数。
5. 不增加实时日志面板。服务输出写入独立日志；启动器只显示服务名称、退出码、通用原因和日志位置，绝不显示日志原文。

## 根因

当前 `start_console.cmd` 使用 `.venv\Scripts\python.exe` 启动 GUI，并在末尾执行 `pause`，因此批处理控制台会一直存在。非管理员启动时，`ensure_admin()` 又通过 UAC 以同一控制台解释器启动管理员进程，形成另一个控制台。Flask 与统计 worker 使用默认 `Popen` 参数，并继承管理员进程的控制台。

## 启动与进程架构

### 无窗口入口

- 新增 Windows Script Host 入口 `start_console.vbs`。
- 该入口解析项目目录，以隐藏窗口方式调用 `.venv\Scripts\pythonw.exe launcher.py`。
- `start_console.cmd` 改为调用 `wscript.exe start_console.vbs` 后立即退出，不再等待启动器关闭，也不再执行 `pause`。
- 推荐用户后续直接打开 `start_console.vbs`；兼容批处理入口只可能在 Windows 创建进程时短暂闪现，不会留下持续命令窗口。

### UAC 提权

- `ensure_admin()` 继续使用 `ShellExecuteW(..., "runas", ...)`。
- 从 `start_console.vbs` 启动时，`sys.executable` 是 `pythonw.exe`，因此提权后的启动器不会创建控制台。
- UAC 系统确认界面属于 Windows 安全流程，不能也不应隐藏。
- 提权启动失败时，在创建主 Tk 界面前显示一个简短的 Windows 错误提示，然后退出。

### 后台服务

- `FlaskServiceSupervisor` 和 `StatisticsWorkerSupervisor` 继续各自只持有一个子进程。
- Windows 下创建子进程时显式设置 `CREATE_NO_WINDOW`，避免直接运行 `launcher.py` 时后台服务创建控制台。
- 非 Windows 平台不传 Windows 专用创建标志。
- 现有停止、超时终止、强制结束、端口清理及重启顺序不变。

## 日志与错误反馈

- 日志目录固定为 `data/logs`。
- Flask 输出写入 `data/logs/flask-service.log`。
- 统计 worker 输出写入 `data/logs/statistics-worker.log`。
- 每次服务启动前重建对应日志，仅保留当前一次启动的输出，避免文件无限增长。
- 标准输出和标准错误写入同一个服务日志。
- supervisor 持有日志文件句柄，并在服务停止、启动失败或对象清理时关闭句柄。
- 服务提前退出或 Flask 健康检查失败时，启动器不读取或解析日志内容。
- 启动器状态栏显示服务名称、退出码、通用失败原因和对应日志文件位置。
- 不在界面实时显示日志，也不把日志原文复制到状态栏或错误对话框。完整日志只保存在本地文件中。

## 数据流

1. 用户打开 `start_console.vbs`，无窗口启动 `pythonw.exe launcher.py`。
2. 启动器必要时请求 UAC，并在管理员 `pythonw.exe` 中创建 Tk 界面。
3. 自动重启流程停止当前子进程并清理端口 `5000`。
4. 环境检查通过后，两个 supervisor 创建日志并以无窗口模式启动服务。
5. Flask 健康检查通过后打开控制台网页。
6. 若服务提前退出或健康检查超时，supervisor 停止两个服务；启动器显示通用原因、退出码和日志位置。
7. 用户关闭启动器时，两个服务和日志句柄按现有顺序清理。

## 兼容性与非目标

- 不改变端口、API、数据库、配置文件、执行策略、采集任务或页面功能。
- 不把服务安装为 Windows 服务。
- 不新增托盘程序或实时日志查看器。
- 不隐藏 Windows UAC 安全确认界面。
- 不更改“每次启动先结束旧服务”的现有行为。

## 测试与验收

### 自动化测试

1. 无窗口入口引用虚拟环境的 `pythonw.exe`、`launcher.py`，并以隐藏方式运行。
2. 兼容批处理入口调用 VBS 后立即退出，且不存在 `pause`。
3. Windows 下两个 supervisor 都传入 `CREATE_NO_WINDOW`。
4. 两个 supervisor 都把标准输出和标准错误重定向到各自日志。
5. 正常停止、启动异常和重复启动均正确关闭日志句柄。
6. 服务失败时状态栏和错误对话框只显示白名单字段：服务名称、退出码、通用原因和日志位置；测试证明日志中的密码、Cookie、令牌和 Authorization 内容不会进入界面消息。
7. UAC 提权继续使用当前解释器，且失败时显示可见错误。
8. 现有启动顺序、关闭顺序、端口清理和全量回归测试通过。

### 本机验收

1. 直接打开 `start_console.vbs`，只出现启动器和必要的 UAC 确认，不出现命令窗口。
2. 自动启动成功后，Flask 和统计 worker 均在后台运行。
3. 打开第二个启动器时，旧服务被结束并由当前项目服务替换。
4. 制造可恢复的启动失败时，启动器显示简短原因，对应日志包含本次服务输出。
5. 关闭启动器后，端口 `5000` 不再由其管理的 Flask 进程监听。
