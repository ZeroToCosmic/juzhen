# Browser Execution V2

## 职责
人工点选并保存元素、编辑五类积木动作、批量运行AdsPower Profile、记录逐Profile/逐动作结果。`源码确认`、`测试确认`。
## 不负责什么
不扫描全页猜目标、不用LLM自愈选择器、不自动迁移Legacy策略、不自动重放失败副作用。
## 代码入口
`execution_v2/blueprint.py`、`service.py`、`store.py`、`scheduler.py`、`executor.py`、`session.py`、`locator.py`、`actions.py`。
## 对外接口
前缀 `/api/browser-v2`：elements、profiles、content-libraries、picker、wheel-calibration、strategies、jobs、history。
## 内部组件
ExecutionStore、Service、Picker、Scheduler、AdsPowerAdapter、CDPSession、LocatorResolver、ActionExecutor、WindowTiler。
## 依赖与调用者
依赖AdsPower、Playwright、SQLite、ghost-cursor/human_type；V2管理页和Comment Campaign调用。
## 数据与事务
`elements`、`element_revisions`、`strategies`、`strategy_actions`、`execution_jobs`、`execution_profiles`、`action_results`、wheel calibration表。写操作使用revision/事务。
## 配置
V2 DB路径、Evidence目录、AdsPower设置、批次大小；默认每批3个Profile。
## 进程与生命周期
Job→分批→start→CDP→tile→navigate→readiness→actions→evidence→stop确认。上一批全关后才下一批。
## 安全边界
HTTP只使用opaque profile token；raw Profile ID和ws只在Adapter内部；Locator必须唯一、可见、可操作。
## 测试
`tests/test_execution_v2_*.py`、`tests-js/browser-v2-ui.test.js`、`execution-v2-picker.test.js`。
## 日志与证据
V2 SQLite结果和UUID PNG Evidence；公共错误为固定摘要。
## 常见故障
Profile配置未接、CDP空白页、多窗口平铺失败、readiness超时、selector歧义、关闭未确认。
## 修改影响清单
同步Store Schema、Pydantic/服务校验、Blueprint、UI、状态机、Evidence和批次回归测试。
## 已知限制
当前工作区未提交；前端仍是原生JS；V2不提供自动selector修复。
