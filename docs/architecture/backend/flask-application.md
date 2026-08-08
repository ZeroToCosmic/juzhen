# Flask应用

`app.py`创建`gateway.app.create_app()`。Factory安装配置、全局保护、认证与业务Blueprint，并注册HTML页面和旧API。默认Service应惰性构造：首次访问才连接数据库、Redis或AdsPower；构造失败不缓存，允许后续重试。

应用关闭时逐个关闭缓存Service。Blueprint catch-all不能吞`AuthError`；local guard必须在业务factory前拒绝远程请求。
