# BCS M5 实施计划（规模化加固 + 策略统一 + 窗口资源模型）

**日期**：2026-08-12 | **前置**：`docs/PRD-业务控制系统.v2.md`（v3.0 定稿）、`docs/superpowers/specs/2026-08-12-central-business-modules-and-agent-executors-design.md`（实施蓝图）
**目标**：把 PRD v3 的 M5 范围全部落地，产出可跑 S2/S3 压测、可上真实设备的 BCS 系统
**铁律**：每阶段完成后既有测试全绿（迁移至 PG 后以 PG fixture 为准）；任何一步不得以"后面再修"跳过

---

## 阶段 0：测试基建与运行环境（P0 前置）

### T0-1 测试基建 PG 化
- [ ] pytest fixture 默认连 Docker PostgreSQL（`CENTRAL_DB_URL` 指向容器；每测试独立 schema 或事务回滚）
- [ ] `infra/docker-compose.yml` 提供开发用 PG（~100MB 内存）+ 一键启动脚本
- [ ] CI 起 PG 容器跑全量测试
- [ ] 既有 132 pytest + 9 node 全部迁移到 PG 下跑绿
- **验证**：`pytest tests/test_central_*.py tests/test_agent_*.py tests/test_chaos.py tests/test_bcs_pages.py` 全绿；SQLite 从测试路径彻底移除

### T0-2 开发环境统一
- [ ] 开发说明更新：`docker compose up -d postgres` → `CENTRAL_DB_URL` 指向容器
- [ ] central 代码中 SQLite 分支/降级逻辑移除（`central/db.py`、`central/config.py`）

## 阶段 1：Agent 通信与可靠性地基（P0 前置，三个缺口）

### T1-1 设备鉴权
- [ ] 设备身份令牌：短期 token + device_session 绑定；心跳/拉取/上报/续租全接口校验
- [ ] SSE 连接绑定当前 device_session；旧 session 连接关闭
- **验证**：无 token / 伪造 device_id / 过期 token → 401/403；测试覆盖

### T1-2 本地待报缓冲
- [ ] Agent 本地持久化待报队列（复用 `agent/wal.py` 模式）：结果/增量/事件上报失败入队重试
- [ ] 断线恢复后按序补报；与 central Inbox 去重配合（重发不重复）
- **验证**：模拟 central 不可达 5min → 期间事件全部补报，无丢失；断电重启后队列恢复

### T1-3 心跳/拉取合并
- [ ] 一次往返：心跳上报 + 工作单元列表 + `next_pull_at`（自适应退避）
- [ ] 空闲退避 10s，任务密集 2s；SSE 可用时不轮询
- **验证**：S2 起请求率 ≤50/s；空闲拉取 <10/s

## 阶段 2：fake-agent 模拟器（P0）

### T2-1 fake-agent
- [ ] 模拟 N 台设备：心跳/拉取/SSE 连接/执行（随机时长）/回传（随机成败）/窗口状态变化/增量上报
- [ ] 可配置规模参数（设备数/窗口数/失败率/延迟）；支持脚本化场景（断连、崩溃、清洗风暴）
- **验证**：10 台冒烟 → 50 台 → 500 台渐进；S2/S3 压测全部依赖此工具

## 阶段 3：SSE 控制通道

### T3-1 SSE 端点与事件
- [ ] SSE 端点（async 实现，不占线程池）：work_available / approval_decided / task_paused / task_cancelled / inventory_sync_requested / lease_revoked
- [ ] Last-Event-ID 续传、序号缺口 → resync_required 全量补拉；断线期间 HTTP Pull 兜底
- [ ] 重连公式 `Delay = Min(60, 2^retry × 2) + Random(0.5, 3.0)` 秒（决策 #20）
- [ ] 服务端 Ratelimit：超阈重连 429 → Agent 降级 60s 低频 Pull
- **验证**：§12.2 #18（空闲零空 Pull；批准→拉取 p95 <1s；重连风暴不崩溃）

## 阶段 4：策略统一与定义目录

### T4-1 统一策略注册表 + 定义目录
- [ ] `strategies` 表（strategy_id/kind/version/checksum/schema/来源模块）+ `executable_definition_revisions`（不可变 revision、Central 重校验、停用只阻新任务）
- [ ] 三源归集 bootstrap：execution_v2 / comment_campaign / selector_probe 策略经引导同步入注册表（§14.1）
- [ ] 任务创建校验：策略存在 + 快照合法；`publish` 类型校验拒绝
- **验证**：发布→冻结→本机编辑不影响已冻结任务；非法引用 4xx

### T4-2 Campaign 策略化 + 执行器分发
- [ ] CampaignExecutor（包装 comment_campaign 内核，接入租约/Fencing/节点级依赖）
- [ ] Agent 执行器注册表 + strategy_kind 精确路由（不解析业务参数）
- [ ] 审批策略 per_comment / batch / prepare_only（实施规格 §2.5）
- **验证**：验收 0（盖楼任务经策略化 Web 创建→CampaignExecutor 端到端）

## 阶段 5：浏览器接管模块（窗口资源模型）

### T5-1 库存同步
- [ ] BrowserInventorySync：全量对账（03:00 + 0-15min 抖动；启动/重连/断档触发）+ 增量 delta（event_seq/epoch CAS/断档拒收请求全量）
- [ ] 窗口资源池：profiles 表（egress_group/状态/绑定/同步时间）；AVAILABLE/RESERVED/BOUND/BUSY/QUARANTINED/MISSING→OFFLINE
- [ ] Web 立即同步命令（只发命令不直连 AdsPower）
- **验证**：验收 #11（对账幂等、delta 去重、缺口触发全量）

### T5-2 绑定、登录与身份
- [ ] 绑定状态机（RESERVED→LOGIN_PENDING→AUTO_LOGIN→WAITING_MANUAL_LOGIN→VERIFYING_IDENTITY→IDENTITY_REVIEW_REQUIRED→ACTIVE）
- [ ] 自动登录优先 / manual_only；验证码/2FA → WAITING_MANUAL_LOGIN（不算失败）
- [ ] 身份审核：expected vs observed identity；不一致 → IDENTITY_REVIEW_REQUIRED，不进业务候选池
- **验证**：验收 #17（身份不一致不进候选池）

### T5-3 凭据加密与 grant
- [ ] AES-256-GCM 信封加密（KeyProvider 接口；KEK→DEK→密文；关联数据绑租户+账号+revision）
- [ ] 一次性短期 grant：校验 session/work unit/lease/binding/executor；领取写审计
- **验证**：验收 #16（无明文泄漏；grant 一次有效过期；Windows/Linux 互通）

### T5-4 软隔离与延迟清洗
- [ ] 失效识别（account_failed 错误码 + 连败 3 次）→ 立即 QUARANTINED 停分配
- [ ] 10s 延迟低优先级清洗队列（API 空闲时执行）；成功后 window_cleaned → AVAILABLE
- [ ] 清洗失败保持 QUARANTINED + 重试队列；自动清洗开关
- **验证**：验收 #13；模拟 10 账号同时失效 → 清洗队列不阻塞心跳/续租

### T5-5 调试标签与本地兜底
- [ ] 设备 DEBUG 标记（Web 进入/退出调试）；调度跳过；已运行任务自然收敛
- [ ] EXTERNAL_BUSY：用户直接打开窗口 → 停新分配
- **验证**：验收 #19

### T5-6 WAITING_WINDOW 唤醒
- [ ] 导入无窗口 → WAITING_WINDOW；窗口释放/同步/新设备上线事件唤醒
- [ ] 容量不超卖（一窗一账号唯一约束）

## 阶段 6：调度与容错

### T6-1 PG 分配器
- [ ] TOP-N + SKIP LOCKED 事务认领（§7.1 部分索引落地）
- [ ] 队列深度感知 tick（空系统≈0）+ 定时任务错峰
- **验证**：S3（15 万排队 tick <1s；分配无重复）

### T6-2 节点账号动态替换（单机铁律）
- [ ] 创建预检（树规模+20% 余量 ≤ 单机可分配数）
- [ ] 同机换号 → WAITING_CAPACITY_REPLACEMENT（15min）→ DLQ（NO_LOCAL_REPLACEMENT_ACCOUNT）
- [ ] 防循环（排除已失败账号；≤3 次）；副作用铁律（已点击不换号不重试）
- **验证**：验收 #14

### T6-3 父评论三阶段验证
- [ ] 0-10min 每 2min → 10-60min 每 15min → >60min 终态 UNVERIFIED（F6 可配）
- [ ] 明确失败立即失败；DLQ"一键重新校验"（人工触发 DOM 扫描）
- **验证**：验收 #15

## 阶段 7：前端完整性

- [ ] 任务创建按业务模块入口（用户不选 task_type；§4.5 映射）
- [ ] 人工处理中心：DLQ 列表（确认失败/疑似暗审分组）/重派/终止/一键重校验
- [ ] MISSED 列表、设备运行中任务视图、设备调试按钮、窗口库存页、立即同步
- [ ] 看板指标与 §9 指标对齐
- **验证**：test_bcs_pages 扩展 + node:test 前端用例

## 阶段 8：压测与总验收

- [ ] S2（50 设备）/ S3（500 设备）压测执行 + 报告（含阈值超出记录）
- [ ] §12.2 全部 19 项验收通过
- [ ] 安全 Tripwire：阻断真实 AdsPower/TikTok；递归扫描日志/快照/队列/API/WS 无明文凭据
- [ ] 烟雾验收工具更新（`scripts/smoke_adspower.py`）适配新协议

---

## 依赖顺序

```
阶段 0（PG 基建）→ 阶段 1（通信地基）→ 阶段 2（fake-agent）
        → 阶段 3（SSE）→ 阶段 5（窗口资源，可先于 4）
阶段 4（策略统一）与阶段 5 并行；阶段 6 依赖 1/3/5；阶段 7 依赖 4/5/6；阶段 8 收尾
```

## 验收切片（每阶段完成即跑）

| 阶段 | 引用验收 |
|---|---|
| 0 | 既有全量测试绿（PG 下） |
| 1 | §12.2 #3/#4 + 鉴权/缓冲专项 |
| 2 | fake-agent 冒烟 + S2 起步 |
| 3 | §12.2 #18 |
| 4 | 验收 0 + §12.2 #1 |
| 5 | §12.2 #11/#12/#13/#16/#17/#19 |
| 6 | §12.2 #2/#7/#14/#15 + S3 |
| 7 | 前端用例 + 验收 21 |
| 8 | 全部 19 项 + Tripwire |
