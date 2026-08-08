# 存储地图

| 存储 | 默认位置/连接 | 负责人 | 事实数据 |
|---|---|---|---|
| Legacy accounts SQLite | `accounts.db` | `init_db.py`、`account_store.py` | AdsPower/Buffer/代理账号 |
| 可选MySQL | `DATABASE_URL` | `database.py`、`models.py` | ORM Account调度模型 |
| Management SQLite | Gateway配置路径 | `management_db.py` | 用户、审计 |
| Execution V2 SQLite | `data/execution_v2/execution_v2.db` | `execution_v2/store.py` | 元素、策略、任务、结果 |
| Selector Probe SQLite | 设置或`SELECTOR_PROBE_DB_PATH` | `selector_probe/store.py` | 探针、版本、目录、Outbox |
| TikTok Stats SQLite | `data/stats/tiktok_stats.db` | `tiktok_stats/db.py` | 采集和指标 |
| Comment Campaign SQLite | `data/comment_campaign/comment_campaign.db` | `comment_campaign/store.py` | Campaign全生命周期 |
| Redis | URL按模块配置 | Queue/Registry/Gates | 协调、租约、心跳、发布映射 |
| `config.json` | 项目配置路径 | `settings_store.py` | 本机设置/凭据 |
| 内容文件 | `data/content` | `content_store.py` | 品牌、文案、视频引用 |
| Evidence/日志 | `data/**/evidence`、`logs/` | 各执行模块 | PNG、JSONL、服务日志 |
| Cookie密文 | `data/stats` | `tiktok_stats/secrets.py` | DPAPI密文 |

`配置确认`：默认值来自各模块构造函数。生产路径可被环境变量覆盖。
