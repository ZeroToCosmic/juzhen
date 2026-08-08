# 新增API

1. 先在`api/openapi.yaml`定义path、operationId、严格Schema、状态码、权限和错误码。
2. 新建/扩展模块Blueprint，不把业务塞入Gateway。
3. 写严格输入模型：拒未知字段、重复query和隐式类型转换。
4. Service校验状态/资格；Store用事务/CAS；Adapter返回白名单。
5. 成功/错误统一envelope并递归脱敏。
6. 测local-direct、认证/CSRF/角色、409、422、503、500。
7. 同步路由清单、错误码、模块和状态文档。
