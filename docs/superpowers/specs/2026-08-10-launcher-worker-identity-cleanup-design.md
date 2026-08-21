# 启动器安全清理旧 Campaign Worker 设计

## 背景与根因

启动器只会停止当前 `LauncherApp` 实例持有的子进程，并通过端口 5000 清理旧 Flask。Comment Campaign Worker 不监听端口；上一个启动器退出后，它仍可继续刷新 Redis 健康租约。新 Worker 因无法取得 `browser_v2:comment_campaign:worker:health` 而立即退出。

## 已批准方案

采用“强身份匹配后清理”：Worker 健康租约写入 `worker:v2:<pid>:<project_fingerprint>:<owner_nonce>`，其中项目指纹是64位小写十六进制，nonce是32位小写十六进制。项目指纹由规范化项目根路径生成，只用于比较，不公开原路径。由启动器启动时，项目指纹与 nonce 同时出现在 Worker 命令行中，供后续进程身份复核。

启动器启动新服务前读取唯一健康键，并仅在以下条件全部成立时清理旧 Worker：

1. 租约格式完整，PID 为正整数；
2. 项目指纹等于当前项目；
3. PID 不是当前启动器 PID；
4. 目标命令行精确匹配 Campaign Worker，并携带相同项目指纹和 nonce；
5. 结束进程前再次读取的 Redis value 与首次读取完全一致；
6. 确认进程退出后只通过 value-CAS 删除该健康键。

任何外项目、旧格式、畸形 value、PID 已不存在或被复用、命令行不符、Redis 不可用、租约在检查期间变化或进程终止失败，均拒绝继续启动。不得扫描 Redis、不得按 Python 进程名批量结束、不得删除其他 key。

## 旧版本迁移

旧 Worker 的 value 不含项目指纹，启动器无法可靠证明其归属，因此自动清理必须 fail closed。首次升级若 value 严格符合旧格式，界面仅提取并显示其中的正整数 PID，由人工核对后做一次性清理；该 PID 不视为可信身份依据，系统不得自动结束。新 Worker 启动后，后续重启即可自动安全替换。

## 组件边界

- `comment_campaign/worker_identity.py`：纯函数，负责项目指纹和租约 value 的构造/解析。
- `comment_campaign/worker.py`：只改变健康租约 value，不改变队列、恢复或心跳语义。
- `launcher.py`：新增 exact-key 清理函数，并在启动新服务前调用。
- 测试：全部使用 Fake Redis/Fake command runner；禁止真实 `taskkill`、真实 Redis 或真实 Worker。

## 验收条件

- 同项目、同 owner、存活旧 PID 被结束，exact lease 被 CAS 删除，新服务继续启动。
- 外项目、旧格式、value 竞态、当前 PID、Redis/终止失败均零误杀并中止启动。
- Worker 健康探针仍接受新 value 前缀。
- 现有 Flask 端口清理、服务启动顺序和失败后全量清理测试不回退。
