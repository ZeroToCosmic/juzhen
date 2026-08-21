# Console Browser Strategy Editor Design

## Scope

Move browser-strategy creation and maintenance out of the legacy Browser V2 multi-view page and into a dedicated Console child page. The action library remains the strategy list and entry point. Browser V2 execution, history, picker, and settings remain available through the existing compatibility page and are not redesigned in this change.

## Information architecture

- Action library: `/console/actions`
- Create strategy: `/console/actions/browser-strategies/new`
- Edit strategy: `/console/actions/browser-strategies/<strategy_id>/edit`

Both editor routes render inside `console_base.html`, keep the Action Library navigation item active, and provide a clear return path to `/console/actions`. The page must not expose the legacy V2 tab navigation or the “V2 独立执行模块” framing.

## Page layout

The editor is a full-width operational workspace rather than a side drawer.

The page header contains the return action, page title, current strategy status, and the primary Save action. The body is organized into two logical areas:

1. Strategy settings: name, enabled state, target URL, readiness element, readiness timeout, run mode, and optional random-minute range.
2. Action sequence: a compact action palette followed by ordered action cards for move, video switch, click, input, and wait actions. Each card exposes the complete parameters for its action type and supports move up, move down, and delete.

Secondary destructive or duplicating operations appear after the main editor content. Existing strategies support Save as copy and Delete. A newly created unsaved strategy does not offer server deletion.

## Data and behavior

The Console editor remains a client of the existing Execution V2 resources. It does not add a second strategy database or Console proxy API.

The page uses the existing endpoints for:

- strategy create, read, update, and delete;
- page-element choices;
- content-library choices used by input actions.

Updates submit the complete strategy payload together with `expected_revision`. A revision conflict must stop the save and tell the operator to reload instead of silently overwriting a newer strategy. Not-found, validation, and network errors remain visible in the editor without navigating back to the list.

After the first successful create, the browser replaces the `/new` history entry with the canonical edit URL. Later saves remain on that page and update the displayed revision/update state. Delete returns to the action library after confirmation. Save as copy creates a new strategy and navigates to its canonical edit URL.

## Shared editor boundary

Strategy-specific model and form behavior is extracted from `browser_v2.js` into a reusable strategy-editor module. This shared boundary includes draft normalization, action templates, action serialization, element eligibility rules, content-source handling, validation, and CRUD requests.

The new Console page owns Console-specific DOM rendering and navigation. The legacy Browser V2 page remains functional and may continue using its existing controller during the first migration step, but no new strategy business rule may be implemented only in one UI. The extraction must not initialize Profiles, execution jobs, picker sessions, history, or V2 local settings on the Console editor page.

## Action-library integration

“新建浏览器策略” links to the Console create route. Each strategy row links to its own encoded Console edit route. Comment Campaign routes are unchanged.

The action library continues to load list data from the existing strategy endpoint. No synchronization state is invented as part of this UI correction.

## Compatibility and safety

- Preserve `/browser-v2` and all existing Execution V2 APIs.
- Preserve all strategy fields and the five existing action types.
- Preserve purpose/kind filtering for readiness, click, and input elements.
- Preserve fixed-text and content-library input modes.
- Keep authenticated same-origin requests and existing CSRF behavior.
- Do not expose Profile tokens, browser WebSocket endpoints, or other sensitive browser data.
- Do not introduce a database migration.

## Verification

- Flask tests cover both Console editor routes, Action Library navigation state, and safe handling of encoded or missing strategy IDs.
- JavaScript tests cover draft normalization, all five action types, ordering, serialization, element filtering, content modes, complete update payloads, revision conflicts, and canonical URL transitions.
- Existing Browser V2 route and API tests remain green.
- Browser verification confirms the editor uses the Console shell, contains no legacy V2 tabs, and supports representative create/edit/save interactions at desktop and narrow widths.
