# Remote Action WSS Execution Plan Suite

本计划套件落实已确认的 [远程动作 WSS 执行体系设计](../specs/2026-08-20-remote-action-wss-execution-design.md)，按可独立验证的子系统拆分，实施顺序固定如下：

1. [协议与动作发布](2026-08-20-remote-action-contracts-and-releases.md)：冻结 Schema、状态枚举、checksum、参数绑定、全局动作 ID 与不可变发布版本。
2. [中控 WSS 控制面](2026-08-20-central-wss-control-plane.md)：创建 WorkOrder、资源预占、持久化投递、对账、Effect Permit 与 Outcome 接收。
3. [Agent 远程运行时](2026-08-20-agent-remote-runtime.md)：WSS 客户端、Inbox/Outbox/WAL、执行器注册、资源锁、取消与重启恢复。
4. [Console 与联合验收](2026-08-20-remote-action-console-and-acceptance.md) 的 Task 1–3：先完成 G1 要求的动作调试/发布和通用远程状态展示。
5. [同级动作适配器](2026-08-20-equal-action-adapters.md)：G1 全部通过后，Browser Strategy 与 Comment Campaign 以同级适配器并行接入通用框架。
6. [Console 与联合验收](2026-08-20-remote-action-console-and-acceptance.md) 的 Task 4–6：完成真实适配器联合验收和 500 Agent 规模验证。

依赖关系为 `协议与发布 → (Central 与 Agent 可并行) → G1 Console + 两个 Fake Executor 的全部 F/R 验收 → 两个同级适配器并行 → G3 联合与规模验收`。设备身份正式方案仍是生产门禁：开发环境可使用明确启用的开发凭据，生产配置没有正式凭据提供器时必须拒绝建立 WSS 连接。
