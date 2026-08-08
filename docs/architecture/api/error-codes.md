# 错误码表

下表优先记录当前稳定公共码。Legacy文本错误在`route-inventory.md`标记，不为其伪造稳定码。

| 错误码 | HTTP | 模块 | 含义 | 可重试 | 操作员动作 |
|---|---:|---|---|---|---|
| `invalid_request` | 400 | 通用 | JSON/query/path格式无效 | 否 | 修正请求 |
| `not_found` | 404 | 通用 | 资源不存在 | 否 | 刷新列表 |
| `validation_failed` | 422 | 通用 | 业务校验失败 | 否 | 检查字段和依赖 |
| `revision_conflict` | 409 | Campaign | revision已变化 | 否 | 刷新后重做 |
| `conflict` | 409 | V2 | 数据变化或仍被引用 | 否 | 刷新或解除引用 |
| `invalid_state_transition` | 409 | Campaign | 当前状态不允许动作 | 否 | 查看状态机 |
| `runtime_unavailable` | 503 | V2/Campaign | 能力或运行时未接线 | 是 | 检查服务接线 |
| `internal_error` | 500 | 通用 | 未知内部错误 | 条件性 | 查脱敏日志 |
| `adspower_unavailable` | 503 | Campaign | AdsPower不可用 | 是 | 检查Local API |
| `redis_unavailable` | 503 | Campaign/Probe | Redis不可用 | 是 | 检查PING/配置 |
| `worker_unavailable` | 503 | Campaign | Worker心跳无效 | 是 | 重启Worker |
| `content_library_unavailable` | 503 | Campaign | 文案库不可用 | 是 | 检查内容库 |
| `template_invalid` | 422 | Campaign | 模板/树/模式无效 | 否 | 修正模板 |
| `target_video_invalid` | 422 | Campaign | URL不符合严格TikTok视频格式 | 否 | 使用规范链接 |
| `allocation_unsatisfied` | 422 | Campaign | 无完整Profile/文案匹配 | 否 | 调整资格/数量 |
| `approval_revision_mismatch` | 409 | Campaign | 批准缺失、已消费或revision不符 | 否 | 重新准备/批准 |
| `profile_start_failed` | 422/500 | Campaign | Profile启动失败 | 条件性 | 查AdsPower状态 |
| `cdp_connect_failed` | 422/500 | Campaign | CDP连接失败 | 条件性 | 查Profile页面 |
| `profile_identity_mismatch` | 422 | Campaign | 当前账号与预期不符 | 否 | 修正Profile元数据 |
| `target_video_mismatch` | 422 | Campaign | 当前页面视频不符 | 否 | 重新导航/准备 |
| `comment_panel_not_ready` | 422 | Campaign | 评论区未完成加载 | 条件性 | 稍后重新准备 |
| `comment_input_not_found` | 422 | Campaign | 输入元素无法唯一定位 | 否 | 重新点选绑定 |
| `parent_comment_not_found` | 422 | Campaign | 父评论未定位 | 条件性 | 人工检查回执 |
| `parent_comment_ambiguous` | 422 | Campaign | 父评论匹配多项 | 否 | 补稳定证据 |
| `comment_author_mismatch` | 422 | Campaign | 评论作者不符 | 否 | 检查账号/父评论 |
| `reply_target_mismatch` | 422 | Campaign | Reply composer不属于父评论 | 否 | 更新定位 |
| `comment_submit_uncertain` | 409/422 | Campaign | 点击后结果不确定 | 否 | 人工核验，禁止重提 |
| `comment_receipt_unverified` | 409/422 | Campaign | 回执不能唯一验证 | 否 | 人工核验 |
| `profile_close_failed` | 409/503 | Campaign/V2 | 无法确认关闭 | 是 | 人工关闭并解除隔离 |
| `input_target_not_editable` | 422 | V2 | 点选节点不是唯一可编辑输入 | 否 | 点选真实输入区域 |
| `wheel_calibration_missing` | 422 | V2 | 未校准滚轮 | 否 | 在点选器校准 |
| `wheel_calibration_inconsistent` | 422 | V2 | 三次结果不一致 | 否 | 重新校准 |
| `wheel_calibration_video_not_changed` | 422 | V2 | 滚动未切换视频 | 否 | 重新操作 |
| `wheel_calibration_multiple_videos` | 422 | V2 | 一次跨多视频 | 否 | 减小滚动 |
| `calibrated_video_switch_not_observed` | 422 | V2 | 回放未观察到切换 | 否 | 改用ArrowDown/检查页面 |
| `probe_busy` | 409 | Probe | 已有queued/running请求 | 是 | 等待当前探针 |
| `redis_hash_mismatch` | 409 | Probe | 发布内容与hash不符 | 否 | 停止发布并检查Store |
| `redis_publication_result_invalid` | 503 | Probe | Redis Lua返回异常 | 是 | 检查Redis版本/脚本 |

来源：`comment_campaign/blueprint.py`、`errors.py`、`execution_v2/blueprint.py`、`wheel_calibration.py`、`selector_probe/registry.py`及测试。部分模块会把内部码投影到不同HTTP状态；以OpenAPI具体Operation为准。
