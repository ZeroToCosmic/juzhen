# Comment Campaign

## 职责

管理独立评论和盖楼评论树、按视频分配Profile角色、冻结文案、人工批准、批次准备、单次提交、Receipt验证、恢复与健康。`源码确认`、`测试确认`。

## 核心数据决策

`CommentTemplate`直接代表一棵评论树：`Campaign -> CommentTemplate revision -> CommentStep tree`。Excel导入只是录入流程，不是持久化实体；不得新增`CommentTree`表或形成`Template -> CommentTree`重复层级。

手工创建与Excel导入都写入现有模板、模板版本和步骤表。Excel中的`node_no`、`parent_node_no`只在单次预览/导入中建立临时映射；保存时生成不透明步骤UUID，文件序号不成为数据库业务ID。

新建或导入评论树从revision 1开始；编辑既有树创建revision + 1。Campaign创建时保存`template_id`和指定revision；规划时冻结模板快照、文案、步骤父关系和Assignment。之后导入或编辑其他树不得改变已创建/已规划Campaign。

本功能复用现有`comment_templates`、`comment_template_revisions`和`comment_steps`，不增加新的持久化实体。生命周期功能仅为`comment_templates`增加可空`deleted_at`及一致性约束；旧SQLite由Store初始化阶段执行幂等增量迁移。

## UI边界

管理界面只显示评论树名称、评论文案、可读层级和版本。模板ID、步骤ID、父步骤ID、文案库ID与文案项ID可在内部API传递，但不得作为常规可编辑字段或用户提示展示。Campaign通过评论树名称下拉选择，前端静默保存对应`template_id`和revision。

## 评论树生命周期

生命周期只允许以下转换：

| 当前状态 | 允许动作 | 结果 |
|---|---|---|
| `enabled` | 停用 | `disabled` |
| `disabled` | 启用 | `enabled` |
| `disabled` | 删除 | `deleted` |

`deleted`是软删除终态：管理界面不提供恢复按钮，普通列表和当前详情查询不再显示该树；数据库仍保留模板revision与步骤快照用于审计和已锁定Campaign回放，但不提供公开的deleted历史查询路由。现存5条名称为`A`的停用记录不做自动清理；必须由管理员逐条确认后，通过带当前revision的删除动作显式处理。

只有`locked_at`非空的Campaign具备祖父化资格：后续即使评论树停用或删除，approve、prepare和submit也只能读取已冻结的`template_snapshot`、Assignment文案及父关系，不再读取当前模板。`locked_at`为空的draft/planned Campaign在plan、reallocate、lock或approve时遇到不可用评论树，统一返回`template_unavailable`。系统不提供Campaign原地换树接口；用户必须取消或弃用旧Campaign，再选择已启用评论树新建Campaign。

新版快照写入`lifecycle_status`。读取旧快照时若该字段缺失，按历史`enabled`值兼容推导为`enabled`或`disabled`；不会据此推断Campaign祖父化，祖父化仍只看`locked_at`。

## Excel两阶段导入

预览：`POST /api/browser-v2/comment-template-imports/preview`，严格`multipart/form-data`，只能包含一个名为`file`的`.xlsx`。成功返回200及规范化节点、源文件行号、位置、逐树错误和汇总；不写数据库。文件超过2 MiB或5000数据行返回413；类型、损坏内容或表格结构无效返回422。

确认：`POST /api/browser-v2/comment-template-imports`，严格JSON，只接收`trees[].name`及`nodes[].{node_no,parent_node_no,text}`。`valid`、`errors`、`row`、`position`属于预览派生字段，提交时禁止携带。成功返回201，包含`created`与`rejected`；每棵树独立校验和创建，单树失败不留下部分记录，也不阻断同批其他有效树。

导入业务限制：每树最多100节点、树名最多100字符、单条文案最多2200字符、盖楼树恰好一个根、父节点必须在同树、禁止自引用与循环。后端确认阶段从原始节点重新规范化，不信任预览结果。

## 对外接口

前缀`/api/browser-v2`：comment-templates、comment-template-imports、comment-profile-metadata、comment-campaigns、assignments、approvals、receipts、attempts、health、comment-settings。完整请求/响应见`docs/architecture/api/openapi.yaml`。

## 代码入口

`comment_campaign/template_import.py`负责只读工作簿解析、树校验和TemplateCreate转换；其余入口为`service.py`、`schemas.py`、`store.py`、`domain.py`、`allocation.py`、`executor.py`、`worker.py`、`queueing.py`、`blueprint.py`。

## 内部组件与依赖

Pydantic Schemas、SQLAlchemy Models/Store、Allocator、ProfileGateway、StrictLocatorResolver、CommentExecutor、QueueCoordinator、RQ Jobs/Worker。运行依赖Redis/RQ、Campaign SQLite、Execution V2 elements、AdsPower/Playwright、内容库和发布结果resolver；预览与导入本身不访问AdsPower、TikTok、队列、Worker或评论提交器。

## 进程与生命周期

create→plan→lock→campaign approve→prepare generation→step approve→single submit→receipt verified/unverified→next dependency。默认批次3，最多300 Profiles。

## Profile缓存、分配与身份预检

`GET /api/browser-v2/comment-profile-metadata`是纯SQLite缓存读取：顶层固定为`data`数组与`meta`，不构造AdsPower控制器，也不调用网络。只有`POST /api/browser-v2/comment-profile-metadata/sync`会显式读取AdsPower Local API的Profile列表；依赖失败仍返回HTTP 200、已有缓存和`meta.stale=true`，安全原因只允许`timeout`、`connection_refused`、`authentication_failed`、`invalid_response`或`not_configured`。Health同样仅表示一次Local API列表可达性：固定请求`page=1,page_size=1`、`max_retries=1`，总截止时间固定4秒；其reason完整白名单为`connected`、`timeout`、`connection_refused`、`authentication_failed`、`invalid_response`、`not_configured`。它不表示浏览器、TikTok登录、页面或提交可用。

自动选择使用模板步骤与缓存Profile的标签、语言、启用、健康和冷却信息做后端完整匹配；`POST /comment-profile-selection/preview`只显示候选池和容量，绝不规划、锁定、启动窗口或提交。手动模式仍传显式`profile_refs`，但在plan时使用同一后端匹配规则。规划阶段不信任历史`expected_username`或`login_verified`来证明当前TikTok身份：这些字段只是缓存元数据，真正账号预检在Campaign全量prepare批次中进行。

Campaign锁定后不能原地更换AdsPower窗口或Profile。全量预检会把观察到的TikTok账号、元素绑定和证据冻结到当前`identity_generation`；Campaign和Assignment的该代次仅可读取，客户端请求不得写入。相同窗口后续可参与另一Campaign，但每个Campaign独立保存自己的generation、预检和Receipt/Attempt证据，后一次观察不得覆盖早期Campaign历史。重复TikTok账号失败公开细节仅允许两个掩码显示名和一个可见用户名，绝不包含raw AdsPower ID、cookie、Authorization、API key、WebSocket地址或异常文本。

RQ prepare job ID只包含`prepare_generation`（`campaign-prepare-<campaign>-g<generation>`）；它的参数同时携带`identity_generation`用于过期job的CAS拒绝。submit job继续只携带Campaign ID、Assignment ID和approval revision，不能误把identity generation拼入submit ID或参数。

## 安全边界

Excel仅解析`.xlsx`，使用openpyxl只读、`data_only=True`、禁外链；限制压缩包成员、解压大小、工作表、行、列和文本长度。Legacy模式继承登录、管理员角色与CSRF；local-direct模式继承loopback和Host守卫。所有API成功结果递归移除raw AdsPower ID、Cookie、Authorization、API key和WebSocket地址。

Queue只携带安全ID/revision/generation；approval持久化且一次消费；点击前重验Campaign running、租约和冻结证据；不确定提交不重试。

## 测试与证据

`tests/test_comment_template_import.py`覆盖解析、导入和边界；`tests/test_comment_campaign_routes.py`、`integration.py`、`security.py`覆盖HTTP、冻结、认证与脱敏；`tests-js/comment-campaign-ui.test.js`覆盖工作台。自动测试不得连接AdsPower/TikTok或执行评论提交。验证报告见`docs/superpowers/reports/2026-08-07-comment-campaign-verification.md`。

2026-08-10首次启用正式库指纹守卫进行回归时，一个既有Gateway集成测试漏传临时`COMMENT_CAMPAIGN_DB_URL`，首次访问`comment-settings`时提前对正式库执行了本功能原定的`deleted_at`增量迁移。守卫在会话结束时检测到文件由SHA-256 `8f30...`、122880字节、mtime_ns `1786168036079570000`变化为SHA-256 `f5bd...`、135168字节、mtime_ns `1786354578306867600`。迁移前后均为5个模板、10个revision；只读核验显示5条名称为`A`的记录仍为`enabled=0, deleted_at=NULL`，`PRAGMA integrity_check`正常。未执行回滚、删除或业务数据清理；该次必要schema迁移后的状态被接受为新基线。

预防措施分两层：pytest默认在`CampaignStore`构造前规范化SQLite相对/绝对路径并拒绝项目正式Campaign DB，确保失败发生在任何engine或连接创建之前；显式设置`COMMENT_CAMPAIGN_PRODUCTION_DB_GUARD=1`时，pytest会话还会以前后只读方式比较正式库SHA-256、大小、mtime及模板/revision计数。所有Gateway测试必须显式注入`tmp_path`数据库。

## 常见故障

Excel格式/大小无效、单树结构错误、Redis/Worker未连接、元素绑定缺失、Profile关闭失败、批准revision过期、父评论定位歧义、发布后无法唯一验证。稳定导入错误码见`docs/architecture/api/error-codes.md`。

## 修改影响清单

同步Schema、Domain状态、Store事务、Queue payload、Worker恢复、UI人工Gate、OpenAPI、错误码表和安全测试。禁止新增重复评论树持久化实体或公开raw Profile ID。

## 已知限制

自动测试只使用Fake、临时SQLite和本地HTML断言：不会打开真实AdsPower或TikTok，也不会发布评论。任何真实受控验收必须先取得用户显式授权；起步仅限3个非生产Profile、不授予submit approval，并由操作者在界面上可见地核验每个账号预检结果后才决定是否继续。

当前工作区未提交；真实AdsPower/TikTok受控验收未在自动测试中执行；生产Receipt依赖当前DOM候选规则。
