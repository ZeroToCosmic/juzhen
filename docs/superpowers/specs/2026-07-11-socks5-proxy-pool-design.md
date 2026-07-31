# SOCKS5 代理池修复设计

## 问题与目标

当前导入代理池的 18 条代理均通过 SOCKS5 握手确认使用用户名密码认证。系统现有实现却统一生成 HTTP 代理 URL，并把账号 ID 作为 Session 后缀追加到用户名，导致代理认证和转发请求超时。

本次修复让导入的静态代理池使用 SOCKS5 原始凭据，同时保留环境变量中专属 Session 代理的既有 HTTP URL 规则。

## 方案

代理来源分为两类：

1. 导入代理池：按 `socks5h://username:password@host:port` 生成 URL。用户名和密码保持原样，域名解析也交给代理服务器完成。
2. 旧版专属 Session 代理：继续按 `http://user-zone-custom-session-account_id:password@host:port` 生成 URL，保持 Task 1.1.2 的兼容行为。

集中配置页为代理池提供协议选择，支持 `SOCKS5` 和 `HTTP/HTTPS`，现有代理池在没有协议字段时迁移为 `SOCKS5`。账号已分配的 `host:port:username:password` 数据保持不变，因此保存配置不会重新分配或改变当前代理。

## 数据与调用链

- `proxy_pool.protocol` 保存代理池协议，默认值为 `socks5`。
- 代理池的每一行继续使用 `host:port:username:password`，不在明文行中重复协议。
- 账号调用 IP 检查或 Buffer 发布时，从账号的 `proxy_session` 读取原始代理条目，再结合 `proxy_pool.protocol` 生成请求 URL。
- Python 请求层安装 SOCKS 支持；`socks5h` 确保目标域名由代理端解析。

## 错误处理

- SOCKS 依赖缺失时，接口返回明确的配置错误，不把它伪装成 Buffer 超时。
- 代理连接、认证和读取超时仍由现有请求异常处理转换为中文错误信息。
- 不在日志、接口错误或页面中输出完整代理密码。

## 界面

代理配置页在批量代理输入区域增加一个紧凑的“代理协议”选择框。默认显示 SOCKS5；切换为 HTTP/HTTPS 后，静态代理池使用 `http://` URL，但仍不修改用户名。

## 验证标准

1. 导入代理池账号生成 `socks5h://` URL，用户名不附加 Session 后缀。
2. 旧版环境变量代理继续生成原有 HTTP Session URL。
3. 保存代理配置不会改变账号已经分配且仍存在于池中的代理。
4. `/check_ip` 可经分配代理获得出口 IP 信息。
5. Buffer GraphQL 请求可通过同一代理建立连接；认证或业务错误应替代此前的连接超时。
6. Python 与 Node 自动化测试全部通过。

## 范围限制

本次不增加运行时自动探测，不改变代理分配算法，不自动轮换已分配代理，也不修改 AdsPower 浏览器配置。
