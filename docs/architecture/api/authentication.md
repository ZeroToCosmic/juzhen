# 认证、授权与CSRF

## 本机直开模式

`LOCAL_DIRECT_MODE`启用时不要求登录，但`gateway/local_only.py`限制loopback地址、Host和请求来源。它只适合本机，不是局域网无认证方案。远程请求应在构造业务Service前返回403。

## 认证模式

Flask Session保存登录状态；`gateway/auth_service.py`实施空闲/绝对过期、失败锁定和密码策略。角色为`administrator`、`operator`。unsafe方法需要CSRF；管理用户等操作只允许administrator。

## 页面与API

- `/login`和`/healthz`属于公开边界。
- HTML页面、GET API和unsafe API统一经过应用级保护。
- Blueprint不得自行安装绕过全局模式的认证逻辑。
- 前端使用`management_fetch.js`、`credentials: same-origin`和页面CSRF meta。

## 自动化测试要求

新接口至少覆盖：local-direct loopback成功、remote拒绝；认证模式匿名401、operator越权403、管理员缺CSRF 403、正确CSRF成功。
