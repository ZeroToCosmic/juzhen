# Console Operational Modules Design

## Scope

Replace the four compatibility launcher pages at `/console/publishing`, `/console/actions`, `/console/accounts-windows`, and `/console/receipts` with operational pages backed by the Agent's existing APIs and stores. Existing specialist editors remain available and authoritative for complex editing.

## Content publishing

The default view is optimized for hundreds of daily publishes: today's queue/result summary, filters, and a dense result table appear first. Batch creation is the primary action; manual publishing is explicitly labeled as local debugging. Content readiness (R2 video availability and brand-copy counts), batch runs, and daily schedules are available as page sections rather than separate launcher links. All writes continue through the existing content and publish endpoints.

## Action library

Browser execution strategies and Comment Campaigns are shown as equal-level action records in one searchable list. Each row identifies its action type, lifecycle status, scale, last update, and local source. Creation and deep editing open the existing specialist editor because their schemas and workflows differ. The new page may enable or disable a browser strategy through its existing revision-checked API. Central synchronization is not represented as successful because this repository has no action-definition synchronization endpoint.

## Accounts and windows

The page combines the two resources needed for local execution. The account roster is the default operational section and supports add/edit, selected/all Buffer discovery, and proxy assignment. The browser-window section supports refresh, multi-selection, and open-and-tile through AdsPower. Dialogs are centered; no right-side detail drawer is introduced. Tokens and passwords remain masked by existing backend projections.

## Receipts and evidence

The page presents a unified, filterable record list for Browser V2 jobs, Comment Campaigns, and publishing tasks. It does not create a duplicate receipt store. Selecting a record replaces the list workspace with a full-width detail workspace and a return action. Browser job evidence uses already-sanitized evidence paths; Campaign receipts and attempts are fetched only when a Campaign record is opened; publishing details use the public publish-task projection.

## Safety and compatibility

- Preserve all legacy pages and APIs.
- Use `management_fetch.js` for CSRF and authentication behavior.
- Do not expose raw AdsPower credentials, Buffer tokens, WebSocket URLs, or untrusted evidence paths.
- Do not add database migrations or fabricated synchronization state.
- Independent API failures leave other sources usable.

## Verification

- Flask route tests prove each module renders its new template and no longer renders the compatibility launcher.
- Node tests cover query/render logic, mutations, stale responses, and unified receipt normalization.
- Browser verification covers desktop and narrow layouts plus one representative interaction per module.

