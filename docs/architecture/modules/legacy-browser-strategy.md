# Legacy Browser Strategy

## 职责
提供旧版元素配置、动作、策略、批次执行、浏览器Agent和轨迹录制。`源码确认`。
## 不负责什么
不提供V2的数据一致性保证；新Campaign不能把Legacy元素ID当作V2元素ID。
## 代码入口
`browser_strategy_runtime.py`、`browser_strategy_config.py`、`browser_actions.py`、`browser_cdp.py`、`actions_dom.py`、`browser/`。
## 对外接口
`/api/browser/*`、`/api/execution-strategies*`等Gateway旧路由。
## 内部组件
CDP接管、元素解析、积木动作、ghost-cursor bridge、Node agents、批次状态、运行时脱敏。
## 依赖与调用者
依赖AdsPower、Python/Node Playwright、OpenAI旧client、Gateway内存任务；旧管理页面调用。
## 数据与事务
配置/内存任务/JSONL日志混用；没有V2 SQLite统一revision模型。
## 配置
元素、策略、动作、轨迹和模型设置来自 `config.json`。
## 进程与生命周期
Flask内同步/异步桥执行；Profile打开后必须在finally关闭。部分旧任务保存在进程内，重启会丢失。
## 安全边界
运行时异常递归脱敏；禁止把headers、cookie、token、ws地址写入公共日志。
## 测试
`tests/test_browser_*.py`、`tests/test_actions.py`、`tests-js/*agent*.test.js`、`browser-strategy-ui.test.js`。
## 日志与证据
`logs/browser_operations.jsonl`及截图；日志字段受安全白名单约束。
## 常见故障
Legacy/V2页面混淆、元素XPath失效、CDP多Tab选错、内存任务因Flask重启消失、Node依赖缺失。
## 修改影响清单
先确认是否应修改V2；同步Python/Node动作、配置Schema、UI、脱敏和回归测试。
## 已知限制
Legacy模块职责分散、Gateway耦合高；保留用于兼容，不应作为新模块默认基础。
