# 健康检查

| 检查 | 真正健康条件 | 不能作为健康证据 |
|---|---|---|
| Flask | `/ping`/页面响应且保护正确 | 端口被任意进程占用 |
| SQLite | 实际执行简单查询 | 文件路径已配置 |
| Redis | PING成功 | URL存在 |
| Campaign Worker | owner格式心跳存在且TTL>0 | Redis本身可用 |
| AdsPower | 有界轻量API请求成功 | Controller对象已构造 |
| TikTok API | 本机API有界请求成功 | 容器名称存在 |

各探针独立显示。AdsPower不可用不能阻止读取历史Campaign/模板；Redis不可用时Worker不得显示绿色。消息固定中文，不拼接密钥、URL或外部响应。
