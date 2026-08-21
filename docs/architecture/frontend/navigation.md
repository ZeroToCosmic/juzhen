# 导航约定

左侧导航唯一来源为`gateway/templates/_dashboard_sidebar.html`。页面使用共享shell样式；当前页面可高亮对应模块，嵌入式V2页面按产品要求可不高亮。新增入口同时修改Sidebar、导航测试和总览入口。

导航不得依赖前端路由框架。链接使用同源绝对路径；页面后退/刷新必须能恢复服务端状态，不能把Campaign、Profile或Receipt仅保存在浏览器。
