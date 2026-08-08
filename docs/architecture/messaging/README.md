# 消息与异步协作文档

当前没有统一事件总线。必须分别理解：[Topic树](topic-tree.md)、[Redis Keyspace](redis-keyspace.md)和`schemas/`中的稳定载荷。SQLite Outbox是持久事实；Redis/RQ是分发机制，不能替代数据库CAS。
