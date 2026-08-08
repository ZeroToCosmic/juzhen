# 代码风格

Python：PEP8、4空格、UTF-8；新公共边界写类型；Route薄、Service/Domain承载规则、Store承载事务、Adapter隔离外部服务、Executor隔离副作用。资源显式close；Playwright在同一loop关闭。

JavaScript：`const/let`、无隐式全局；same-origin请求走`management_fetch.js`；server/draft/in-flight分离；外部文本只用`textContent`；不同资源动作并行、同revision决定去重。

当前无自动formatter/linter配置。提交前至少运行目标测试、`git diff --check`和乱码/敏感信息扫描。
