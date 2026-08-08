# HTTP接口文档

- [OpenAPI 3.1](openapi.yaml)：当前JSON/运维路由；文件使用JSON语法，属于合法YAML 1.2。
- [全部Flask路由](route-inventory.md)：186个装饰器声明，含HTML页面。
- [认证与CSRF](authentication.md)
- [后续接口规范](conventions.md)
- [错误码](error-codes.md)

`openapi.yaml`的`x-source`指向当前源码。`x-legacy-exception: true`表示契约形状尚不统一，修改前必须同时阅读源码和测试。自动提取只保证路径、方法、函数和源码位置；泛型请求体不代表允许任意字段。
