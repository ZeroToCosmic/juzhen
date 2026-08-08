# ADR-0006: 本机直开与认证双模式
## 状态
Accepted (Retrospective)
## 当前事实
本机开发可取消账号密码，但远程请求必须拒绝；认证模式仍保留Session、CSRF和角色。
## 决定
应用统一支持local-direct和legacy-auth两种保护，Blueprint不自行绕过全局保护。
## 代码与历史证据
`gateway/local_only.py`、`auth_blueprint.py`、`gateway/app.py`及集成测试。
## 为什么
本机单用户开发避免登录阻塞，同时不能把无认证后台暴露到局域网/公网。
## 后果
本机操作简化；所有新页面/API必须同时测试两种模式。
## 已知限制
local-direct不是通用无认证部署方案；反向代理需要重新评估Host/来源。
## 后续变更条件
远程管理、多人协作或代理部署启用前必须重新设计认证和网络边界。
