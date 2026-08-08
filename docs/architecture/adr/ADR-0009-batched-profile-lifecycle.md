# ADR-0009: 分批执行与确认关闭
## 状态
Accepted (Retrospective)
## 当前事实
系统面向数百Profile，默认每批3个；上一批stop并确认inactive后才启动下一批。
## 决定
以有界批次执行，窗口按当前视口平铺；关闭失败暂停/隔离，不继续开下一批。
## 代码与历史证据
`execution_v2/scheduler.py`、`tiling.py`、`comment_campaign/profile_gateway.py`、acceptance测试。
## 为什么
限制机器资源和AdsPower API压力，避免旧窗口残留与下一批重叠。
## 后果
资源上限明确；总任务耗时随批次数增长。关闭确认成为安全Gate。
## 已知限制
当前平铺依赖单桌面视口；关闭异常需要人工处理隔离Profile。
## 后续变更条件
调整并发或分布式执行前必须重新验证窗口、API限速、租约和关闭不变量。
