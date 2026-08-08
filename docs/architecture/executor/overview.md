# Windows执行器总览

执行器运行于本机Windows，调用AdsPower启动隔离Profile，获取CDP endpoint，再由Python Playwright执行页面动作。Legacy、Execution V2和Comment Campaign共享底层思想但不是同一数据契约。

标准生命周期：身份解析→租约→start→CDP→选择目标Page→平铺→导航→readiness→定位→动作→Evidence→stop→is_active确认→释放租约。
