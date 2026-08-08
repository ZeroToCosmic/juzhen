# 环境变量

| 名称 | 进程 | 默认/回退 | 敏感 | 作用 |
|---|---|---|---|---|
| `LOCAL_DIRECT_MODE` | Flask | false | 否 | 本机直开保护 |
| `APP_CONFIG_PATH` | Flask/Worker | 项目`config.json` | 否 | 持久配置路径 |
| `DATABASE_URL` | Flask/Launcher | 空=SQLite模式 | 是 | 可选MySQL |
| `CELERY_BROKER_URL` | Flask/Probe | Redis localhost | 是 | Celery/兼容broker |
| `ADSPOWER_BASE_URL` | Flask/Campaign Worker | 持久设置 | 否 | Local API |
| `ADSPOWER_API_KEY` | Flask/Campaign Worker | 持久设置 | 是 | AdsPower认证 |
| `EXECUTION_V2_DB_PATH` | Flask/Campaign Worker | `data/execution_v2/execution_v2.db` | 否 | V2 DB |
| `COMMENT_CAMPAIGN_DB_URL` | Flask/Worker | `sqlite:///data/comment_campaign/comment_campaign.db` | 否 | Campaign DB |
| `COMMENT_CAMPAIGN_REDIS_URL` | Flask/Worker | localhost Redis DB0 | 是 | RQ/租约 |
| `COMMENT_CAMPAIGN_EVIDENCE_DIR` | Flask/Worker | `data/comment_campaign/evidence` | 否 | Campaign PNG |
| `COMMENT_CAMPAIGN_ENTRY_ELEMENT_ID` | Worker | 持久四绑定 | 否 | 评论入口元素 |
| `COMMENT_CAMPAIGN_INPUT_ELEMENT_ID` | Worker | 持久四绑定 | 否 | 输入元素 |
| `COMMENT_CAMPAIGN_SUBMIT_ELEMENT_ID` | Worker | 持久四绑定 | 否 | 提交元素 |
| `COMMENT_CAMPAIGN_ACCOUNT_ELEMENT_ID` | Worker | 持久四绑定 | 否 | 账号证据元素 |
| `SELECTOR_PROBE_DB_PATH` | Probe | 设置/模块默认 | 否 | Probe DB |
| `SELECTOR_PROBE_EVIDENCE_ROOT` | Probe | 模块默认 | 否 | Probe Evidence |
| `SELECTOR_PROBE_STOP_FILE` | Probe | 空 | 否 | 优雅停止文件 |
| `TIKTOK_STATS_DB_PATH` | Stats | `data/stats/tiktok_stats.db` | 否 | Stats DB |
| `TIKTOK_STATS_COOKIE_PATH` | Stats | `data/stats/tiktok_cookie.json` | 是 | DPAPI密文 |
| `TIKTOK_STATS_API_URL` | Stats | `http://127.0.0.1:53281` | 否 | 本地API |
| `TIKTOK_STATS_STOP_FILE` | Stats | 空 | 否 | 优雅停止文件 |
| `PUBLISH_WORKER_ENABLED` | Flask | 0 | 否 | 旧发布Worker开关 |

优先级不是全局统一：显式测试注入通常最高；环境变量与持久设置顺序以各Worker/factory源码为准。公共健康接口只能返回是否已配置，不返回值。
