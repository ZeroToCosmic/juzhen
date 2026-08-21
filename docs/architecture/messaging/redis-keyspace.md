# Redis Keyspace

## Comment Campaign

固定前缀：`browser_v2:comment_campaign:`。

| Key类别 | 示例模式 | TTL | 作用 |
|---|---|---:|---|
| job gate | `...:job:{rq-job-id}` | 初始30秒，成功后延长 | 并发enqueue去重 |
| campaign lease | `...:campaign:{campaign-id}` | 120秒并续期 | 单Campaign执行 |
| profile lease | `...:profile:{profile-ref}` | 120秒并续期 | Profile互斥 |
| video submit lease | `...:video-submit:{video-id}` | 120秒并续期 | 同视频提交互斥 |
| worker health | `...:worker:health` | 30秒 | owner-token Worker心跳 |

释放和刷新使用Lua owner compare，禁止SCAN/通配删除。Redis异常与正常锁竞争必须区分。

## Selector Registry

namespace来自Probe设置，默认语义为`selector_registry`。Registry使用Lua原子发布active/version/immutable/hash映射；Gate和Picker另有租约/会话Key。实际Key由`selector_probe/registry.py`、`gates.py`、`picker.py`构造。

## 数据责任

Redis保存协调状态，不是Campaign/Probe历史事实来源。Redis丢失后由SQLite Outbox、generation、状态和reconcile恢复；不能从“Redis中无Key”推断业务已取消。
