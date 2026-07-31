# Selector Probe 最小修复验收

日期：2026-07-30

## 结果

- 手工运行请求与实际 Probe Run 已建立一对一关联；活动请求去重，终态回写。
- AdsPower 启动、CDP 就绪和重试阶段会记录脱敏进度，不再只显示 `unknown`。
- observe 运行会保存可见、视口内、可交互的语义候选；管理页可将候选加入动态元素目录。
- 评论入口可用本轮发现的唯一 `data-e2e` / 可访问名称作为只读状态转换兜底；不会触发输入或提交。
- 运行详情显示阶段、Profile 掩码、尝试次数、摘要和发现候选。
- 旧版 `management_run_requests` 数据库先迁移、后创建新索引，避免启动时报 `no such column: probe_run_id`。
- 旧 XPath 元素继续以 `legacy_manual` 形式出现在目录，可逐项迁移。

## 本机状态

- 管理服务：`http://127.0.0.1:53330`
- 探针配置：关闭；`observe`；每天 `03:00`
- 独立测试 Profile：2 个，且均在 dedicated allow-list
- 历史运行：9 条，迁移后仍保留
- 旧元素目录：3 条可见的 `legacy_manual` 元素
- 数据库迁移前备份：
  `data/selector-probe.pre-lifecycle-migration.20260730-162230.db`

探针总开关仍保持关闭。验收时仅在一次进程内临时启用 observe，未写回
设置、未发布 Redis 版本，也未暂停任何策略。

## 验证

- Selector Probe Python 全量：`746 passed, 1 skipped`
- 管理 UI JavaScript 全量：`75 passed`
- 密钥与证据脱敏：`15 passed, 1 skipped`
- 就绪门、旧库迁移、评论入口与 DOMSnapshot 兼容测试均通过
- Flask 53330 服务重启成功

## Live observe 证据

- Run `7`：`completed`
- 两个独立测试 Profile 均完成 round 1 和 round 2
- 页面就绪、A11y 快照与候选过滤阶段均通过
- 发现 54 个合并元素候选，其中 51 个带推荐定位路径
- 证据同时包含 `feed_ready` 和 `comment_panel_open`
- 评论入口使用新发现的 `data-e2e=comment-icon` 通过 Dry-Run 后打开
- 评论面板内发现 `contenteditable` textbox 和
  `data-e2e=comment-post` 发布按钮
- 每轮结束关闭评论面板，Profile 和探针页面最终清理

内置浏览器当前标签被 URL 安全策略阻止读取 localhost，因此未绕过策略做
浏览器内截图验收。服务启动、数据库迁移、投影接口和 UI 渲染由测试覆盖。

## 已知边界

- 自动定时探测仍关闭；Live observe 已用一次性内存配置完成验收。
- Webhook 当前关闭；配置 HTTPS 地址并通过测试后才会发送告警。
- 只有两个 Profile、连续两轮一致且 Redis 原子发布成功，受影响策略才允许
  自动恢复；人工暂停原因不会被自动清除。
- 更广泛的旧浏览器策略测试仍有既存环境门禁/依赖服务失败，本修复未改变
  生产队列或这些旧接口。
