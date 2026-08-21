# Selector Probe

## 职责
定时/手动打开独立测试Profile，等待页面、执行只读状态动作、采集元素、验证、维护目录/版本、发布Redis映射、告警和Webhook。`源码确认`。
## 不负责什么
不输入或提交文案，不点赞/关注，不作为策略执行器，不允许探针动作产生平台副作用。
## 代码入口
`selector_probe/worker.py`、`probe.py`、`state_runner.py`、`store.py`、`registry.py`、`picker.py`、`inventory.py`、`blueprint.py`。
## 对外接口
`/api/selector-probe/*`：elements、status、runs、versions、gates、alerts、audit、settings、picker、webhook-test、run-now。
## 内部组件
Worker、ManagedRuntime、Readiness、StateRunner、Snapshot、Inventory、Catalog、Validator、Registry、Gates、Alerts、Webhook。
## 依赖与调用者
依赖独立AdsPower测试Profiles、Playwright、SQLite、Redis、可配置Webhook；管理界面和旧策略Gate读取。
## 数据与事务
Probe SQLite包含run、contract、validation、version、outbox、gate、alert、managed_elements、draft、audit、request等表；Redis发布用Lua原子脚本。
## 配置
每日03:00策略、测试Profiles、URL、readiness、Redis namespace、Webhook、Evidence、Worker间隔。
## 进程与生命周期
Worker轮询请求/定时槽；运行持有租约和心跳；多Profile多轮验证后发布或告警；最终关闭测试窗口。
## 安全边界
`ALLOWED_ACTIONS`仅导航/刷新/等待/有限滚动/评论面板；`FORBIDDEN_ACTIONS`明确禁止input/submit/like/follow/publish/account_update。
## 测试
`tests/test_selector_probe_*.py`、`tests-js/selector-probe-*.test.js`、`selector-inventory-ui.test.js`。
## 日志与证据
Probe run、阶段证据、alert screenshot、Webhook outbox；路径和响应需脱敏。
## 常见故障
第二Profile空白页、TikTok加载/Skeleton未完成、第三步无候选、Redis发布失败、Profile关闭过早、run request停在queued。
## 修改影响清单
同步只读动作白名单、Store DDL、Worker恢复、管理UI、Redis Lua、告警、状态/错误码和测试。
## 已知限制
历史设计多次变更，现代码仍较大；语义自愈相关旧文件已删除，不能按旧规格假设仍存在。
