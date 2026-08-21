# Browser V2 Shared Sidebar Design

## Goal

Display the existing management-console sidebar on `/browser-v2` so users can switch modules without returning to the dashboard first.

## Scope

- Reuse the existing `_dashboard_sidebar.html` partial and `dashboard_shell.css` layout.
- Show the complete current management navigation on the Browser V2 page.
- Remove only the old “执行策略” link from the shared sidebar.
- Keep the “浏览器执行策略 V2” link visually neutral; it must not be highlighted on `/browser-v2`.
- Preserve the old execution-strategy page, routes, APIs, configuration, and data. Only its sidebar link is hidden.
- Do not change Browser V2 APIs, state, actions, or data models.

## Page Structure

`browser_v2.html` will load `dashboard_shell.css` before `browser_v2.css` and use the shared shell:

```text
body.browser-v2-page
└── div.dashboard-shell
    ├── _dashboard_sidebar.html
    └── main.dashboard-main
        └── div#browser-v2-app.v2-shell
            ├── V2 header
            ├── V2 tabs
            └── V2 views
```

The V2 JavaScript continues to address the same element IDs. No controller behavior changes are required.

## Navigation Behavior

- The shared sidebar remains the single navigation source for the dashboard, statistics page, and Browser V2 page.
- The old `/?panel=strategies` navigation link is removed from the shared partial.
- `/browser-v2` remains available in the sidebar.
- The Browser V2 link has no `active` class and no `aria-current="page"` attribute.
- Direct access to `/?panel=strategies` remains functional for backward compatibility.

## Styling and Responsive Behavior

- Desktop uses the existing 268-pixel sticky sidebar and flexible content column.
- At widths below 900 pixels, the existing shared shell changes to a top navigation grid.
- Browser V2 keeps its current internal max width, cards, tabs, and mobile rules.
- The V2 content wrapper must not introduce a second page-level margin or horizontal overflow.

## Error Handling

The sidebar is server-rendered and has no independent loading state. If Browser V2 data fails to load, navigation remains usable and the existing V2 error presentation continues unchanged.

## Verification

- `/browser-v2` includes `dashboard_shell.css` and `_dashboard_sidebar.html`.
- The rendered V2 page contains `.dashboard-shell`, `.dashboard-sidebar`, `.dashboard-main`, and `#browser-v2-app`.
- The shared sidebar contains `/browser-v2` but not `/?panel=strategies`.
- The Browser V2 link has neither `active` nor `aria-current="page"`.
- Existing Browser V2 frontend and integration tests continue to pass.
- Existing responsive sidebar tests continue to pass.

## Non-Goals

- Deleting or disabling the old execution-strategy feature.
- Adding a new sidebar implementation.
- Highlighting Browser V2 in the sidebar.
- Changing Browser V2 internal tabs.
