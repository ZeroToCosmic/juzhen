# Shared component library
## Current status: not implemented
没有独立组件包或版本化Design System。
## Current substitute
Jinja partial、共享CSS和普通JS工具。
## Operational impact
卡片、状态、表单和轮询模式可能重复或漂移。
## Preconditions for future work
先盘点页面、设计token、无障碍、组件API和视觉回归方式。
## Decision required before implementation
选择框架无关Web Components还是跟随未来前端框架。
## Evidence
`gateway/templates/_dashboard_sidebar.html`、`gateway/static/dashboard_shell.css`。
