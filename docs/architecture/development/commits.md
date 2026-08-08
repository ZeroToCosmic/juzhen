# 提交规范

采用Conventional Commits：`feat`、`fix`、`docs`、`test`、`refactor`、`chore`、`build`、`ci`。

格式：`type(scope): imperative summary`，主题尽量不超过50字符，硬上限72。一个提交只解决一个主题；不提交日志、数据库、截图、密钥、缓存。只stage本任务文件，提交前检查`git diff --cached --name-only`。
