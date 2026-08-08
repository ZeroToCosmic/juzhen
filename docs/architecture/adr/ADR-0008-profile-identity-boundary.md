# ADR-0008: Profile身份隔离
## 状态
Accepted (Retrospective)
## 当前事实
AdsPower raw ID只在Controller/Gateway内部；V2使用进程内opaque token，Campaign使用持久化随机`profile_ref`映射。
## 决定
HTTP、RQ、Receipt、Attempt、日志和Evidence不得暴露raw Profile ID或CDP endpoint。
## 代码与历史证据
`browser_public_identity.py`、`execution_v2/service.py`、`comment_campaign/models.py`、`profile_gateway.py`及安全测试。
## 为什么
raw ID可关联本机浏览器身份；CDP endpoint可授予浏览器控制能力。
## 后果
公共接口安全；内部必须维护可靠的身份解析和脱敏显示。
## 已知限制
V2 token与Campaign profile_ref不是同一契约，不能互换。
## 后续变更条件
任何统一身份服务必须保留opaque公共引用并提供迁移/轮换方案。
