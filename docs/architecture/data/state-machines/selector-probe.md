# Selector Probe状态机

Probe运行通常为queued→running→completed/failed；管理请求另有`dispatch_failed`。Picker为`starting→ready→selecting→finished|cancelled|failed|expired`。告警为`open→acknowledged→resolved`。Webhook为`pending→processing→completed|failed`。

Selector发布由SQLite outbox pending/claimed/retry/completed与Redis原子发布共同构成；只有校验和hash一致才能更新active版本。Gate原因在`cleared_at`为空时生效。

元素目录同时具有published_status和draft_status，不能压缩为单一状态。ManagedRuntime状态为`draft|healthy|degraded|validating`。

`该图为源码事实汇总；当前没有一个文件集中定义全部Selector Probe状态机。` 来源：`store.py`、`picker.py`、`managed_runtime.py`、`worker.py`。
