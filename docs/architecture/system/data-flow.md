# 主要数据流

证据等级：`源码确认`、`测试确认`；本次未连接真实 AdsPower、TikTok、Redis、Buffer 或 R2，外部运行健康属于`运行时未验证`。

## 管理页面请求

```mermaid
sequenceDiagram
    participant U as User
    participant J as Jinja/Static JS
    participant F as Flask Guard/Blueprint
    participant S as Service/Domain
    participant D as Store/Adapter
    U->>J: 打开页面或提交表单
    J->>F: same-origin request + CSRF(认证模式)
    F->>F: local guard 或 session/role 校验
    F->>S: 严格请求模型/查询参数
    S->>D: 事务或外部调用
    D-->>S: 内部模型
    S-->>F: 公共投影
    F-->>J: data/error envelope
    J-->>U: textContent 渲染状态
```

当前旧接口不完全遵循统一 envelope；这些接口将在路由清单标记为 Legacy。

## Browser Execution V2

```mermaid
sequenceDiagram
    participant UI as V2 UI
    participant API as V2 Service
    participant SCH as Scheduler
    participant AP as AdsPower Adapter
    participant PW as Playwright/CDP
    participant DB as V2 SQLite
    UI->>API: strategy + profile tokens + batch size
    API->>DB: 创建 execution job
    API->>SCH: 分批执行
    loop 每批最多配置数量
      SCH->>AP: start profile
      AP-->>PW: ws.puppeteer
      SCH->>PW: connect / navigate / readiness
      SCH->>PW: locate + ordered actions
      SCH->>DB: action/profile results
      SCH->>AP: stop + is_active confirmation
    end
    API-->>UI: job status/results
```

失败 Profile 记录并关闭，不应阻塞同批其他 Profile；未确认关闭时禁止下一批。

## Selector Probe

```mermaid
flowchart TD
    T["定时或手动请求"] --> W["Probe Worker"]
    W --> P["独立测试 Profiles"]
    P --> R["页面 readiness / 状态动作"]
    R --> C["精简元素候选与验证"]
    C --> V["版本/草稿/目录"]
    V --> O["Publication Outbox"]
    O --> X["Redis Registry"]
    C --> A["告警 + Screenshot + Webhook Outbox"]
    A --> G["策略 Gate / 受影响任务暂停"]
```

探针只允许导航、刷新、等待、有限滚动、打开/关闭评论面板等只读状态动作；禁止输入、提交、点赞、关注或账号修改。证据：`selector_probe/state_runner.py`。

## TikTok Stats

```mermaid
flowchart LR
    S["Scheduler"] --> L["SQLite Lease"]
    L --> C["Collector"]
    C --> A["Local TikTok API"]
    A --> N["Normalize"]
    N --> DB["Snapshots / Posts / Daily Metrics"]
    DB --> Q["Query Service"]
    Q --> UI["Stats UI"]
```

Cookie 由 Windows DPAPI 保护；API 不返回 Cookie 明文。

## Comment Campaign

```mermaid
sequenceDiagram
    participant O as Operator
    participant S as Campaign Service
    participant DB as Campaign Store
    participant Q as Redis/RQ
    participant E as Executor
    participant AP as AdsPower/Playwright
    O->>S: create → plan → lock → approve campaign
    S->>DB: CAS state + assignments + frozen content
    S->>Q: enqueue prepare generation
    Q->>E: prepare batch
    E->>AP: open profile / locate video and input
    E->>DB: evidence + awaiting_step_approval
    O->>S: exact revision approve-submit
    S->>DB: durable approval
    S->>Q: enqueue submit assignment
    Q->>E: consume approval / revalidate evidence
    E->>AP: one submit click
    E->>DB: receipt + verified/unverified state
```

线程模式只有父 Assignment 和父 Receipt 都为 verified，子步骤才可准备。提交后异常一律保守进入 `published_unverified`，不得自动重提。

## 配置流

环境变量覆盖持久化配置的范围因模块而异。通用形态为：

```text
显式测试注入
  → Flask config / Worker env
  → config.json 持久化设置
  → 代码默认值
```

具体优先级必须以各模块构造函数和 Worker 接线为准，不能假设所有配置统一。
