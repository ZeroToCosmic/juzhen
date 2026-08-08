# Comment Campaign

## 职责
管理独立评论和盖楼模板、按视频分配Profile角色、冻结文案、人工批准、批次准备、单次提交、Receipt验证、恢复与健康。`源码确认`、`测试确认`。
## 不负责什么
不自动生成文案，不在无人工批准时提交，不暴露raw AdsPower ID，不用未验证父评论继续盖楼。
## 代码入口
`comment_campaign/service.py`、`store.py`、`domain.py`、`allocation.py`、`executor.py`、`worker.py`、`queueing.py`、`blueprint.py`。
## 对外接口
前缀`/api/browser-v2`：comment-templates、comment-profile-metadata、comment-campaigns、assignments、approvals、receipts、attempts、health、comment-settings。
## 内部组件
Pydantic Schemas、SQLAlchemy Models/Store、Allocator、ProfileGateway、StrictLocatorResolver、CommentExecutor、QueueCoordinator、RQ Jobs/Worker。
## 依赖与调用者
依赖Redis/RQ、Campaign SQLite、Execution V2 elements、AdsPower/Playwright、内容库和发布结果resolver；Campaign工作台调用。
## 数据与事务
模板/版本/步骤、Campaign、Assignment、Approval、Receipt、Attempt、Profile identity/metadata。规划、锁定、批准消费、终态Receipt和后代暂停使用事务/CAS。
## 配置
DB URL、Redis URL、Evidence目录、四个V2元素绑定、AdsPower配置；环境变量优先于持久设置。
## 进程与生命周期
create→plan→lock→campaign approve→prepare generation→step approve→single submit→receipt verified/unverified→next dependency。默认批次3，最多300Profiles。
## 安全边界
Queue只携带安全ID/revision/generation；approval持久化且一次消费；点击前重验Campaign running、租约和冻结证据；不确定提交不重试。
## 测试
`tests/test_comment_campaign_*.py`、`tests-js/comment-campaign-ui.test.js`；验证报告见 `docs/superpowers/reports/2026-08-07-comment-campaign-verification.md`。
## 日志与证据
Attempt、Receipt、UUID PNG、固定错误摘要；公共递归脱敏raw ID、ws、Cookie、Authorization、API key。
## 常见故障
Redis/Worker未连接、元素绑定缺失、Profile关闭失败、批准revision过期、父评论定位歧义、发布后无法唯一验证。
## 修改影响清单
同步Schema、Domain状态、Store事务、Queue payload、Worker恢复、UI人工Gate、OpenAPI、消息Schema和安全测试。
## 已知限制
当前工作区未提交；真实AdsPower/TikTok受控验收未在自动测试中执行；生产Receipt依赖当前DOM候选规则。
