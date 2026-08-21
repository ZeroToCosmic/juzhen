# Launcher

## 职责
本机启动入口；检查管理员权限、Python/Node 依赖、Redis和可选MySQL，监督 Flask、Selector Probe、TikTok Stats、Comment Campaign Worker。`源码确认`：`launcher.py`。
## 不负责什么
不保存业务状态，不执行网页动作，不决定 Campaign/策略状态。
## 代码入口
`launcher.py::main`、`LauncherApp`、四个 `*Supervisor`；`start_console.cmd`、`start_console.vbs`。
## 对外接口
Tkinter UI、子进程命令、环境变量、固定日志路径；没有 HTTP API。
## 内部组件
权限提升、端口 PID 识别、本项目旧 Flask 清理、依赖检查、数据库配置、进程监督。
## 依赖与调用者
调用 Python、Node、Redis/MySQL 检查和各服务模块；由本机用户启动。
## 数据与事务
不持有业务数据库；读取 `.env`、进程状态和日志。进程启停不是数据库事务。
## 配置
`DATABASE_URL`、Redis/Worker相关环境变量；数据库输入通过 `DatabaseConfig` 归一化。
## 进程与生命周期
启动失败或 Worker 早退时停止本轮全部服务；关闭时逐个 best-effort stop 并销毁 UI。
## 安全边界
只结束已识别为本项目的旧 Flask；Windows 后台子进程隐藏窗口；错误详情不能带密钥。
## 测试
`tests/test_launcher_restart.py`、`tests/test_console.py`。
## 日志与证据
Flask与各Worker写固定日志文件；启动器只显示脱敏状态和日志路径。
## 常见故障
端口5000仍被非本项目进程占用、依赖缺失、Redis未启动、MySQL配置错误、Worker启动后立即退出。
## 修改影响清单
同步进程顺序、早退清理测试、环境变量、日志路径和运维文档。
## 已知限制
仅面向Windows；没有系统服务安装、自动升级或集中监控。
