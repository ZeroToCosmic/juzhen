# 故障排查

## 点击无反应
先读浏览器Network/Console，再确认Flask进程、HTTP状态、CSRF和业务error code；不要先删除数据库。

## Flask启动失败
检查5000监听PID及命令行；只结束本项目旧Flask。查看`logs/flask-service.log`。Windows session_key权限错误需检查目标路径/句柄类型。

## Profile未打开或空白
检查AdsPower轻探针、Profile配置、start响应、CDP endpoint、Page列表和目标URL；不要把另一个窗口的Page复用给当前Profile。

## 元素找不到
确认页面readiness、评论区Skeleton、元素revision/status、Locator唯一/可见/可编辑；重新点选，不用坐标猜测。

## Campaign卡住
检查Campaign/Assignment状态、revision、approval、Worker TTL、RQ job、prepare_generation和Attempt。禁止手工重放submit job。

## Profile关闭失败
人工确认窗口，保持Profile隔离和Campaign暂停；不得仅删除Redis lease后继续下一批。

## 磁盘增长
区分日志/Evidence、SQLite、备份、`work/`和依赖缓存。只清理有保留策略的日志/证据；数据库先备份再处理。
