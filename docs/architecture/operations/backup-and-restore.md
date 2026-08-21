# 备份与恢复

备份前停止Flask和三个Worker，确认无运行浏览器批次。分别备份Legacy accounts、Management、V2、Probe、Stats、Campaign SQLite；可选MySQL使用dump；配置和DPAPI Cookie单独加密保管；Evidence按保留需求复制。

恢复顺序：代码/依赖→配置→数据库→Redis/外部服务→Worker→Flask。Redis不是历史事实备份；Worker依赖SQLite reconcile重建prepare任务。先在副本运行完整性检查，不把旧数据库直接交给未知版本代码。

配置Store会自动保留轮换备份并可恢复最后有效文件；这不替代整机备份。
