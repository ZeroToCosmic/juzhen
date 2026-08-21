# 依赖方向

```text
templates/static → HTTP contract
Blueprint → Schema + Service
Service → Domain + Store + Gateway
Store → DB engine/connection
Gateway → Redis/AdsPower/Buffer/R2/TikTok API
Executor → Gateway + Locator + Actions
```

禁止反向依赖：Domain导入Flask、Store导入Blueprint、公共Schema包含raw Profile ID、RQ Job携带Service对象、Gateway日志记录外部凭据。跨模块只通过稳定ID和公开Service方法协作。
