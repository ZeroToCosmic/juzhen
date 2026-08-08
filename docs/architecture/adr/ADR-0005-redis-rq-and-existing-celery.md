# ADR-0005: Redis/RQ与既有Celery并用
## 状态
Accepted (Retrospective)
## 当前事实
Comment Campaign使用RQ；旧发布和部分任务接线存在Celery；两者可使用Redis。
## 决定
如实保留两套当前机制，不将它们描述成统一Topic Bus。
## 代码与历史证据
`comment_campaign/queueing.py`、`worker.py`、`celery_app.py`、`requirements.txt`。
## 为什么
Campaign需要Windows SpawnWorker、固定Job ID和SQLite恢复；旧功能已依赖Celery。迁移未获批准。
## 后果
现有功能保持兼容；运维必须区分Queue、Worker、心跳、重试和错误码。
## 已知限制
依赖与监控重复；消息契约未集中。
## 后续变更条件
统一队列前必须盘点所有Producer/Consumer、幂等、租约和恢复语义。
