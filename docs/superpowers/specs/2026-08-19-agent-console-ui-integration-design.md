# Agent Console UI Integration Design

## Scope

The local Agent keeps its existing browser execution, Comment Campaign, Buffer/R2 publishing, TikTok collection, account, and settings backends. A new operator console is introduced incrementally at `/console`; the legacy `/` dashboard remains available until the new modules reach functional parity.

## Navigation

The shared sidebar is the single navigation source and uses these groups and entries:

- 运行总览
- 自动化：任务执行、动作库
- 业务模块：内容发布、数据采集、采集结果
- 资源与能力：账号与窗口、运行环境、页面元素
- 记录与系统：回执与证据、系统设置

New console pages and legacy pages use the same sidebar. Existing editors remain the authoritative editing surfaces during migration; no iframe or duplicate editor is introduced.

## First implementation increment

The first increment delivers:

1. A new `/console/overview` operational overview using existing health, publishing, execution, and collection APIs.
2. A dedicated `/console/collection` page for collection sources, runtime status, recent runs, and manual debug runs.
3. A dedicated `/console/collection-results` page for business-facing video metrics.
4. Redirect adapters from the remaining `/console/*` module URLs to their existing functional pages.
5. A read-only `GET /api/tiktok-stats/videos` projection over `posts_current JOIN tracked_accounts`.

## Collection boundaries

`数据采集` owns targets, service status, run history, and manual debug dispatch. `采集结果` owns only current business data. It does not display batch IDs, data quality, sync state, evidence, or execution diagnostics.

The video results table uses this order:

`视频信息 → 账号 → 发布时间 → 播放 → 点赞 → 评论 → 最近采集 → 操作`

Text and timestamps are left aligned. Numeric metrics are right aligned with thousands separators. Missing metric values remain null in the API and render as `—`. The default sort is most recently collected first.

The current schema stores one current row per video and has no per-video historical samples. Therefore the first increment exposes only the latest result view and does not fabricate history or a selectable “all records” mode.

## Compatibility and safety

- `/`, `/?panel=...`, `/browser-v2`, `/comment-campaigns`, `/tiktok-stats`, and `/bcs` remain accessible.
- Existing service factories, workers, storage schemas, and execution chains are unchanged.
- The new video API is read-only and applies bounded pagination and allow-listed sorting.
- Local pages do not approve, resume, or mutate Central production tasks.
- Existing R2, Buffer, proxy, model, and publishing cadence settings remain in the current settings page during migration.

## Verification

- Python route tests cover the new console pages, redirects, API filters, sorting, pagination, null preservation, and stable ordering.
- Node tests cover collection-results rendering, query serialization, null formatting, and request race protection.
- Existing TikTok statistics, dashboard navigation, Browser V2, Comment Campaign, and BCS tests remain green.

