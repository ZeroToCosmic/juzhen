# 当前前端架构

前端由Flask/Jinja直接输出HTML，逻辑位于`gateway/static/*.js`，样式为普通CSS。根`package.json`只管理浏览器工具依赖和Node测试；没有构建步骤、TypeScript、React/Vue或pnpm workspace。

公共壳：`_dashboard_sidebar.html`、`dashboard_shell.css`、`dashboard_navigation.js`。认证模式请求应加载`management_fetch.js`并读取CSRF meta；local-direct仍受服务器本机Guard保护。

页面脚本通常维护server snapshot、draft和in-flight状态，并用递归`setTimeout`轮询。外部字符串只写`textContent`。新增页面必须先复用现有壳，不能复制一套侧栏。
