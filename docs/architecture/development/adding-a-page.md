# 新增页面

1. 复用Dashboard Sidebar/Shell/Navigation/management_fetch。
2. Template注入CSRF meta并只引用同源静态资源。
3. JS拆分server/draft/in-flight；轮询用epoch，hidden/unload停止。
4. 所有外部值用`textContent`；Evidence只接受服务端安全URL。
5. 危险动作使用revision、原因、确认和防重复key。
6. 后端权限为权威；operator只读UI不能替代403。
7. 补Node测试、页面inventory和导航测试。
