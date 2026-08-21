# Browser V2 Picker Interaction Mode Design

## Goal

Allow an operator to prepare secondary TikTok page states, such as opening comments or focusing search, before selecting nested elements. Preserve safe click interception while element selection is active.

## Root cause

The picker installs one capture-phase click listener and always calls `preventDefault()` and `stopPropagation()` for the highlighted target. This is correct for safe element selection, but TikTok never receives the click needed to open comments, search, menus, or other secondary UI.

## Approved interaction model

The picker overlay has two page-local modes:

- `select`: default. Pointer hover highlights an actionable element. Clicking is intercepted and emits one selection without triggering the TikTok action.
- `interact`: page interaction. Picker highlighting is hidden and ordinary page pointer, click, focus, keyboard, input, and scroll behavior passes through unchanged.

The operator switches modes with a compact floating toolbar injected into the AdsPower test page. `F2` is a backup keyboard shortcut. The toolbar displays the active mode and warns that interaction mode can trigger real page actions.

Expected flow:

1. Start picker in `select` mode.
2. Switch to `interact` mode.
3. Click the comments or search control and complete any required page interaction.
4. Switch back to `select` mode.
5. Click the now-visible nested element to capture it.

## Overlay behavior

- Toolbar uses a maximum z-index and fixed positioning without reading page content.
- Toolbar contains exactly two buttons: `选择元素` and `操作页面`.
- Toolbar and its descendants are marked as picker-owned UI.
- Picker-owned UI is excluded from target resolution, highlighting, capture, and page pass-through handling.
- Toolbar clicks change mode but never emit a selection or reach TikTok controls underneath.
- Switching to `interact` clears the active target and hides the highlight marker.
- Switching back to `select` resumes pointer highlighting and safe click interception.
- `F2` toggles modes and prevents the page from consuming that key.
- `Escape` still emits `cancel`, uninstalls the picker, and closes its session through existing behavior.
- `uninstall()` removes marker, toolbar, and all document listeners.
- Repeated `install()` remains idempotent.

## Safety boundary

Interaction mode deliberately permits real TikTok behavior, including possible likes, follows, navigation, search submission, or comments. The default remains `select`. A visible warning and active-mode label must remain present while the picker is installed.

No automatic replay, guessed safe-action allowlist, click synthesis, DOM enumeration, or remote mode control is added.

Mode state is in-memory inside the current page overlay only. It is not returned by the API, stored in SQLite/Redis/localStorage, or shared across Profiles.

## Scope

Modify only:

- `execution_v2/picker_overlay.js`
- `tests-js/execution-v2-picker.test.js`

No changes to Flask routes, V2 service, picker session backend, management page, AdsPower adapter, selector generation, or database schema.

## Error and lifecycle behavior

- Toolbar creation failure must not weaken default selection safety; install remains in `select` mode.
- If the toolbar is visually obscured, `F2` remains available.
- Page navigation receives the existing init script, so a newly loaded document creates a fresh overlay in default `select` mode.
- Cancel and uninstall remove all picker-owned nodes and listeners from either mode.

## Acceptance tests

1. Install starts in `select`; hover highlights; click is prevented and emits one selection.
2. Toolbar switches to `interact`; marker hides; ordinary page click is not prevented, stopped, or emitted.
3. Toolbar switches back to `select`; the next page click is captured normally.
4. Toolbar clicks never become picker selections and never reach underlying page targets.
5. `F2` toggles both directions and prevents the shortcut event.
6. `Escape` cancels from either mode.
7. Repeated install creates one toolbar, one marker, and one listener set.
8. Uninstall removes toolbar, marker, listeners, target, and mode state.
9. Existing selector, XPath, overlay, and full Node test suites continue passing.
