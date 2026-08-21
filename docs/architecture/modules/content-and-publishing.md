# Content and Publishing

## 职责
管理品牌、文案、视频同步、发布批次、结果、统计、日程和Buffer/R2集成。`源码确认`。
## 不负责什么
不执行Comment Campaign评论链；不管理V2元素。
## 代码入口
`gateway/content_store.py`、`content_import.py`、`buffer_client.py`、`buffer_discovery.py`、`r2_client.py`、`celery_app.py`、`gateway/app.py`发布路由。
## 对外接口
`/api/content/*`、`/api/publish/*`、`/publish/buffer`。
## 内部组件
内容文件Store、Excel/文本导入、Buffer GraphQL、R2签名/上传、批次队列、采样与回填。
## 依赖与调用者
依赖本地文件、openpyxl、requests、Buffer、R2、可选Celery；管理后台调用。
## 数据与事务
内容主要位于 `data/content` 文件；发布批次/结果由Gateway现有Store维护。跨外部API不存在全局事务，靠状态和重试恢复。
## 配置
Buffer、R2、发布Worker、存储目录和调度参数来自设置/环境。
## 进程与生命周期
可由HTTP触发或后台Worker处理；外部创建成功后必须持久化远端ID/URL防止重复。
## 安全边界
不公开Buffer token、R2 secret或签名头；视频路径/扩展名受限。
## 测试
`tests/test_content_import.py`、`test_content_publish.py`、`test_buffer_publish.py`、`test_buffer_discovery.py`、`test_r2_client.py`。
## 日志与证据
发布结果、错误摘要和清理日志；不记录完整认证头。
## 常见故障
内容文件损坏、Excel列名不匹配、Buffer GraphQL错误、R2配置不完整、远端成功但本地写入失败。
## 修改影响清单
同步内容格式、导入别名、发布状态、幂等策略、外部契约、API与错误码。
## 已知限制
内容与发布逻辑主要仍集成在Gateway；没有统一消息契约或独立发布服务。
