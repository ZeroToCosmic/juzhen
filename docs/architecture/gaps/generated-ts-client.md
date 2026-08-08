# Generated TypeScript client
## Current status: not implemented
前端手写fetch，仓库无TypeScript客户端产物。
## Current substitute
`management_fetch.js`和各页面API常量。
## Operational impact
请求/响应字段和错误码可能与后端漂移。
## Preconditions for future work
先把OpenAPI中的Legacy泛型Schema补全并建立兼容版本策略。
## Decision required before implementation
选择生成器、输出包位置、发布方式和breaking change政策。
## Evidence
当前`package.json`无OpenAPI/TypeScript生成工具。
