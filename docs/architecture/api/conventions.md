# 后续HTTP接口规范

本规则从本文档发布后约束新增或修改接口；旧接口在OpenAPI中标记Legacy。

## Envelope

成功：`{"data": ...}`。错误：`{"error":{"code":"stable_code","message":"固定中文说明"}}`。

## 状态码

- 查询/同步修改：200。
- 创建：201。
- 异步受理：202。
- 请求格式：400。
- 未认证/越权：401/403。
- 不存在：404。
- revision/状态冲突：409。
- 业务校验：422。
- 外部依赖不可用：503。
- 未知内部错误：500，禁止回显异常原文。

## 输入

写接口JSON必须为object、拒绝未知字段、拒绝隐式字符串转数字/布尔、限制ID/字符串/集合大小。GET拒绝未知或重复query。路径ID限制空白和长度。

## 并发和幂等

修改持久状态必须携带`expected_revision`或等价CAS。异步job使用稳定幂等ID；数据库先持久化dispatch intent，再入队。副作用任务不能依赖Redis去重替代数据库状态校验。

## 输出安全

递归删除raw Profile ID、Cookie、Authorization、API key、token、ws/wss。只保留公共业务ID、`profile_ref`和脱敏名称。

## OpenAPI同步

契约变化与代码、测试同PR。`operationId`稳定；新增错误码先进入错误码表。
