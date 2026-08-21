# TikTok Stats

## 职责
维护跟踪账号、采集TikTok账号/帖子快照、计算日指标、查询趋势和展示统计。`源码确认`。
## 不负责什么
不通过AdsPower执行互动，不发布内容，不复用Campaign账号角色。
## 代码入口
`tiktok_stats/worker.py`、`collector.py`、`client.py`、`store.py`、`queries.py`、`blueprint.py`、`db.py`。
## 对外接口
`/tiktok-stats`和`/api/tiktok-stats/*`：accounts、cookie、status、runs、summary、table、detail、trends。
## 内部组件
Scheduler、LeaseHeartbeat、Collector、TikTokApiClient、StatsStore、StatisticsQueryService、CookieSecretStore。
## 依赖与调用者
依赖本地TikTok API容器、SQLite、Windows DPAPI；管理页面和Worker使用。
## 数据与事务
`tracked_accounts`、`collection_runs`、`account_snapshots`、`posts_current`、`daily_account_metrics`、`worker_leases`、`schema_migrations`。
## 配置
`TIKTOK_STATS_DB_PATH`、`COOKIE_PATH`、`API_URL`、`STOP_FILE`；默认API为本机53281。
## 进程与生命周期
独立Worker按Asia/Shanghai槽调度；SQLite租约防重复；支持增量、完整校准和保留清理。
## 安全边界
Cookie由DPAPI加密；公共状态只说明有效性，不返回明文；异常文本经过秘密脱敏。
## 测试
相关模块测试与 `tests-js/tiktok-stats-ui.test.js`；历史说明 `docs/tiktok-stats.md`。
## 日志与证据
collection_runs details、Worker日志、cookie验证状态；不记录Cookie。
## 常见故障
TikTok API容器未启动、上游契约变化、Cookie失效、SQLite租约未释放、时区/采集槽误判。
## 修改影响清单
同步迁移版本、上游commit契约、Collector归一化、指标查询、UI和保留策略。
## 已知限制
当前上游API固定到指定commit契约；真实健康依赖本机容器和Cookie。
