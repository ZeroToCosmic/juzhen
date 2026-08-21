# Comment Campaign 验证报告

日期：2026-08-07

## 结论

Comment Campaign 的自动化验收已使用本机 Fake AdsPower、Fake Page、Fake Redis
完成。自动测试没有启动真实 AdsPower Profile、没有访问 TikTok，也没有点击真实
提交按钮。

300 个合成 Profile 按每批 3 个执行，共 100 批。每批最多同时打开 3 个窗口；
上一批全部经过 `stop + is_active` 关闭确认后，下一批才开始。关闭确认失败会暂停
Campaign、隔离该 Profile，并阻止下一批。

## 已执行命令与结果

```text
pytest tests/test_comment_campaign_*.py -q
174 passed, 1 skipped

pytest execution_v2（排除已知 Windows legacy session-key 基线）
  + tests/test_adspower.py + tests/test_launcher_restart.py
297 passed

pytest tests/test_execution_v2_integration.py
  -k "not evidence_route_remains_authenticated_in_legacy_mode and not legacy_mode_keeps_auth"
9 passed, 2 deselected

node --test tests-js/comment-campaign-ui.test.js
  tests-js/browser-v2-ui.test.js
  tests-js/dashboard-navigation.test.js
45 passed
```

按计划包含 `tests/test_app.py` 的混合全量回归也实际运行过，结果为：

```text
555 passed, 114 failed, 10 errors
```

这些失败/错误均在应用构造阶段受到现有 Windows session-key 基线阻断：
`gateway/session_key.py` 对文件描述符调用 `os.chmod(fd, ...)`，在当前 Python 3.12
Windows 运行时抛出 `TypeError`。本模块没有修改该安全组件；已将不依赖此基线的
V2、AdsPower、启动器和 Campaign 回归单独运行并全部通过。

符号链接证据测试在当前 Windows 权限下跳过 1 项；路径穿越、编码路径、反斜杠、
大写扩展名和普通符号链接拒绝逻辑均有独立覆盖。

## 恢复与幂等

- `submitting` 或 `verifying_receipt` 恢复为 `published_unverified` 时，Assignment、
  Receipt、Recovery Attempt 和后代暂停在同一 SQLite 事务内提交。
- 已消费但尚未点击的 Approval 会失效，旧准备证据被清除，Assignment 进入重新准备
  状态；旧 revision 不能再次批准。
- Reconcile 只允许生成 prepare job，从不生成 submit job。
- prepare generation 只在 RQ 接受后确认；入队失败、Worker 重启、Redis 丢失任务、
  finished/failed 旧任务均有幂等恢复测试。
- threaded 分支恢复只暂停故障节点的后代，不改变同父节点的其他分支。

## 安全边界

- API 成功/错误投影、Attempt、Receipt、RQ 参数、管理页面和本报告均进行固定泄露
  扫描；不得出现 raw Profile ID、WebSocket 端点、cookie、Authorization 或 API key。
- 公开数据只使用 `profile_ref`、脱敏显示名及安全业务 ID。
- 证据文件只接受 `evidence/<32位小写十六进制>.png`；专用路由拒绝路径逃逸、
  非 PNG、大写别名和符号链接，并返回 `Cache-Control: no-store`。
- Health 分别探测 SQLite、Redis PING、Worker owner+TTL 心跳和 AdsPower 轻量单页请求；
  配置存在不会被显示为“已连接”，异常文本不会回显连接串或密钥。

## 本机真实验收状态

未执行。当前没有在本任务中获得逐条真实提交所需的 6+2 个授权测试 Profile 与人工
确认会话，因此没有启动 AdsPower 或发送 TikTok 评论。运行时健康状态也未主动探测，
避免在自动验收中连接本机 Redis、Worker 或 AdsPower；管理页面会在用户启动这些服务
后显示当次真实探测结果。

真实验收时应先使用两个专门测试 Profile 验证元素与登录，再用 6 个授权 Profile 完成
两批独立评论、一个三层回复链和一个双分支回复模板；每次真实提交都必须由用户按当前
Assignment revision 人工确认。任何账号、视频、父评论、回复范围或回执不确定时立即停止。
