# 日志与Evidence

主要位置：`logs/`、`logs/browser_operations.jsonl`、各Worker固定日志、`data/execution_v2/evidence`、`data/comment_campaign/evidence`、Probe Evidence目录。

Evidence文件使用32位小写hex UUID加`.png`；下载路由拒绝目录穿越、反斜杠、扩展名变化、symlink和根目录逃逸，并返回`no-store`。

日志只允许业务ID、profile_ref/掩码、固定code和脱敏摘要。禁止Cookie、Authorization、API key、raw AdsPower ID、ws/wss和外部完整响应。日志可按保留周期清理，但数据库、未处理Receipt和开放告警不是日志，不能一起删除。
