# UI开发约定

- 复用页面壳、卡片、按钮、状态标签；状态同时显示文字/图标，不能只靠颜色。
- `server`、`draft`、`inFlight`分离；轮询只替换server，不覆盖focused输入。
- 资源轮询使用epoch和selection capture；页面hidden/unload停止，不允许重叠请求。
- 同一assignment+revision的互斥决定共用decision key；409刷新，网络失败不自动重提副作用。
- 外部数据使用`textContent`；Evidence URL再做UUID PNG白名单。
- unsafe请求用same-origin credentials和CSRF；UI隐藏按钮不替代后端权限。
- `localStorage`只保存无敏感UI偏好。
- 新交互补Node `node:test`，至少覆盖双击、迟响应、轮询编辑保护、XSS和错误状态。
