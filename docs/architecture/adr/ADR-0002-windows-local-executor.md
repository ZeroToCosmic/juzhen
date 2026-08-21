# ADR-0002: Windows本地执行环境
## 状态
Accepted (Retrospective)
## 当前事实
AdsPower、Tkinter、DPAPI、pywin32和隐藏子进程均依赖Windows本机。
## 决定
电脑端执行器和启动器运行在Windows，不作为Docker容器交付。
## 代码与历史证据
`launcher.py`、`tiktok_stats/secrets.py`、`requirements.txt`的`pywin32`条件依赖。
## 为什么
必须访问本机AdsPower窗口、桌面尺寸、CDP端点和Windows密钥保护。
## 后果
可控制真实窗口和本机Profile；部署不能完全由Linux容器复制。
## 已知限制
环境差异、桌面会话、权限和杀毒软件会影响运行。
## 后续变更条件
若引入远程执行节点，应单独定义控制面/执行面协议和凭据边界。
