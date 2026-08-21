# CDP会话生命周期

start响应提供`ws.puppeteer`；Playwright `chromium.connect_over_cdp()`连接。Session必须绑定一个Profile、Browser、Context、目标Page。目标Page不得是about:blank，必须满足目标URL；多余Tab按模块策略清理。

Playwright对象与创建它的event loop绑定，operation和关闭必须在同一async loop。所有异常路径最终关闭Page/Browser连接并请求AdsPower stop；stop后再次查询active。
