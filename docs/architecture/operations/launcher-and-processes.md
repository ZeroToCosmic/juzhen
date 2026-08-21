# 启动器与进程

标准进程：Flask、Selector Probe Worker、TikTok Stats Worker、Comment Campaign Worker。启动器先检查依赖和外部服务，再启动；任一关键服务早退则停止本轮全部进程。

Flask启动前只结束命令行/工作目录可确认属于本项目的旧Flask。非本项目占用5000端口时拒绝启动。Worker使用隐藏窗口并将stdout/stderr写固定日志。

停止时依次请求终止，超时再kill；无论某个stop异常，都继续关闭其余进程。浏览器Profile由业务Executor关闭，不由启动器粗暴批量结束。
