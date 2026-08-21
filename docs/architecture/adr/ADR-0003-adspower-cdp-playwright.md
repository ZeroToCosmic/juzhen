# ADR-0003: AdsPower、CDP与Playwright
## 状态
Accepted (Retrospective)
## 当前事实
AdsPower负责Profile生命周期并返回CDP endpoint；Python Playwright通过`connect_over_cdp`接管页面。
## 决定
浏览器自动化沿用AdsPower Local API + CDP + Playwright，不另启普通Chromium替代Profile。
## 代码与历史证据
`adspower.py`、`execution_v2/adspower_adapter.py`、`execution_v2/session.py`。
## 为什么
Profile隔离和登录态由AdsPower持有；Playwright提供Locator、输入、键盘、鼠标和截图能力。
## 后果
可复用数百本机Profile；必须处理API限速、多个Page、空白Tab和关闭确认。
## 已知限制
依赖AdsPower本机可用和返回契约；CDP连接不是跨平台托管服务。
## 后续变更条件
更换浏览器供应商或引入远程执行协议时新增ADR，不能直接改现有Adapter契约。
