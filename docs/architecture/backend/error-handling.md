# 错误处理

边界原则：Domain错误保留稳定code；Blueprint映射固定HTTP和中文消息；未知异常返回`internal_error`，不回显`str(error)`。依赖不可用为503，状态/revision冲突为409，业务输入为422。

浏览器点击后任何异常先持久化`published_unverified`和Receipt/Attempt，再返回错误；不得让队列自动重试提交。日志可记录固定code、业务ID和脱敏摘要，禁止raw Profile、ws、Cookie、认证头或外部完整响应。
