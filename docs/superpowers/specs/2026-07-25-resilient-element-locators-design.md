# Resilient Element Locators Design

Date: 2026-07-25

## Goal

Replace each element alias's single brittle XPath string with an ordered,
persisted locator definition that:

- resolves independently in every AdsPower page;
- understands page state and element scope;
- prefers stable semantic attributes;
- retains user-captured XPath as an advanced fallback;
- refuses ambiguous or invisible targets;
- reports actionable diagnostics instead of waiting 30 seconds on a locator
  that cannot exist.

## Confirmed product decisions

- Adopt semantic locators with ordered fallbacks and explicit scopes.
- Preserve raw XPath as an advanced fallback option.
- Keep strategy actions referencing element aliases; actions do not embed
  selectors.
- Migrate existing element strings without deleting or rewriting the original
  XPath.
- Persist every locator edit through the existing settings store so refresh and
  application restart do not lose it.
- Validate elements in state-aware phases rather than checking every alias only
  once at strategy start.

## Why the captured XPath fails

The current comment-entry XPath pins:

- `article[@id='one-column-item-1']`;
- a complete hierarchy of TikTok wrappers;
- exact generated CSS class strings.

After scrolling, the active feed items in the observed AdsPower pages were
`one-column-item-4` and `one-column-item-5`; TikTok's virtual list had removed or
reused the captured first item.

The comment-input XPath additionally pins:

- the homepage layout;
- a specific comment-sidebar transition state;
- generated CSS class strings;
- an outer input wrapper rather than the actual `contenteditable` textbox.

All three saved XPaths matched zero elements in both observed pages while stable
semantic attributes remained available.

## Persisted schema

The strategy schema advances to a new locator-aware version. The browser element
map remains keyed by alias so existing action references stay valid:

```json
{
  "action_elements": {
    "评论入口": {
      "scope": "active_video",
      "locators": [
        {
          "id": "locator-comment-entry-primary",
          "type": "attribute",
          "name": "data-e2e",
          "value": "comment-icon",
          "enabled": true
        },
        {
          "id": "locator-comment-entry-xpath",
          "type": "xpath",
          "value": "//article[...]",
          "enabled": true,
          "fallback": true
        }
      ]
    }
  }
}
```

Required element fields:

- `scope`: one supported scope identifier.
- `locators`: non-empty ordered locator list.

Required locator fields:

- stable `id`;
- `type`;
- `enabled`.

Type-specific fields:

- `attribute`: `name`, `value`, optional descendant constraint;
- `role`: `role`, optional accessible-name value and match mode;
- `css`: `value`;
- `xpath`: `value`.

Arbitrary executable JavaScript is not a supported locator type.

## Supported scopes

### `page`

Search the complete current main page. This is the default for migrated
elements.

### `active_video`

1. Locate the visible TikTok feed scroll container.
2. Find the feed article crossing the scroll-container center line.
3. Require exactly one active article.
4. Search locator candidates only inside that article.

This prevents a global `data-e2e="comment-icon"` selector from choosing a
different visible or virtualized video.

### `visible_comment_panel`

1. Locate visible comment panels.
2. Reject hidden, exiting, detached, or covered panels.
3. Require exactly one usable panel.
4. Search locator candidates only inside that panel.

## Locator resolution

Resolution happens separately for every profile and immediately before each
element action:

1. Resolve the action's alias.
2. Resolve its scope against the current active page.
3. Evaluate enabled locator candidates in saved order.
4. For each candidate, collect:
   - raw match count;
   - visible match count;
   - actionable match count;
   - rejection reason.
5. Accept the first candidate with exactly one visible, actionable target.
6. If a candidate matches multiple targets, do not silently choose
   `first()`/`nth()`.
7. If all candidates fail, raise a staged locator-resolution error immediately.

The accepted locator is still re-evaluated by Playwright when the action runs,
so a normal React re-render does not retain a stale element handle.

Locator fallback occurs only before an action is dispatched. A click that may
already have taken effect is never repeated against another fallback candidate.

## TikTok comment template

The element manager offers an explicit “TikTok 评论元素模板”. Applying it
creates or replaces a draft only after user confirmation.

### 评论入口

- Scope: `active_video`.
- Primary: attribute `data-e2e="comment-icon"`.
- Optional fallback: role `button` with an accessible name containing
  `comments`, scoped to the active video.
- Existing XPath: retained as final advanced fallback.

### 评论输入框

- Scope: `visible_comment_panel`.
- Primary container: attribute `data-e2e="comment-input"`.
- Descendant constraint:
  - `contenteditable="true"`;
  - role `textbox`.
- Existing XPath: retained as final advanced fallback.

The resolved target must be the actual editable descendant, not its wrapper.

### 评论提交按钮

- Scope: `visible_comment_panel`.
- Primary: CSS `button[data-e2e="comment-post"]`.
- Fallback: role `button` with accessible name `Post`.
- Existing XPath: retained as final advanced fallback.

The `data-e2e` candidate remains first because accessible text can vary by
locale.

## State-aware validation

Static preflight validates only configuration:

- referenced alias exists;
- locator shape is valid;
- at least one candidate is enabled;
- referenced scope is supported.

DOM validation occurs at the point where the page is expected to contain the
element:

```text
verified video scrolling completes
→ resolve current video's comment entry
→ click comment entry once
→ wait for visible comment panel
→ resolve input and submit elements
→ focus actual editable textbox
→ type and verify reflected content
→ click submit once
```

The input and submit aliases are not rejected before the comment panel has been
opened.

## Action postconditions

### Comment-entry click

The click is successful only when a visible comment panel or its editable input
appears. Navigation/page replacement is handled by the existing lifecycle
manager, but the click is not blindly repeated.

### Keyboard input

The target must be an actual `input`, `textarea`, or `contenteditable` element.
The existing reflected-text verification remains required.

### Submit click

The click is dispatched once. Locator fallback cannot cause a second submit.
Submission-effect verification may use the input clearing, submit state, or
comment-list update when a stable signal is available; absence of a stable
signal is reported separately from locator resolution.

## Element management UI

Each alias editor contains:

- alias name;
- scope selector;
- ordered locator candidates;
- candidate type and type-specific fields;
- enable/disable state;
- move up, move down, and remove controls;
- “添加定位方式”;
- “在已打开窗口测试”;
- “应用 TikTok 评论元素模板”.

XPath remains available but is labelled “高级备用定位”.

The test operation displays one row per selected AdsPower profile:

- current safe origin;
- resolved scope;
- candidate used;
- raw/visible/actionable counts;
- success or precise rejection reason.

Testing a locator never clicks, scrolls, types, focuses, or submits. It is a
read-only DOM inspection.

## Migration

Existing element maps:

```json
{
  "评论入口": "//article[...]"
}
```

become:

```json
{
  "评论入口": {
    "scope": "page",
    "locators": [
      {
        "id": "generated-stable-id",
        "type": "xpath",
        "value": "//article[...]",
        "enabled": true,
        "fallback": true
      }
    ]
  }
}
```

Migration rules:

- preserve alias and exact selector text;
- assign stable locator IDs;
- do not silently add or overwrite TikTok semantic locators;
- persist the migrated canonical form on the next successful settings save;
- mark aliases as `needs_repair` in the UI when read-only testing finds no
  usable target;
- keep strategy references intact.

The TikTok template is an explicit user action, not an automatic destructive
migration.

## Errors and diagnostics

Locator failures use safe codes:

- `element_alias_missing`;
- `element_scope_not_found`;
- `element_candidate_not_found`;
- `element_candidate_ambiguous`;
- `element_not_visible`;
- `element_not_actionable`;
- `element_postcondition_not_observed`.

The public result includes:

- profile ID;
- action ID/type/index;
- alias;
- scope;
- safe locator type;
- counts and rejection reasons;
- page-recovery events;
- safe origin.

Raw page HTML, cookies, credentials, comment text, generated comment content,
and full user data are not logged.

## Persistence and compatibility

- The existing locked settings-update path remains the only writer.
- Saves remain atomic and retain the existing backup behavior.
- Canonical API responses return the saved locator definitions.
- Refreshing the dashboard reloads the canonical server state.
- Application restart reloads the same persisted definitions.
- Strategy actions continue to store only element aliases.
- Legacy configurations remain readable through migration.

## Tests

TDD coverage must prove:

- legacy strings migrate without losing selector text;
- migration is idempotent;
- locator edits persist across reload and application restart;
- semantic primary locator wins over XPath fallback;
- fallback is used when the primary has zero usable matches;
- ambiguous candidates fail instead of selecting the first match;
- invisible or disabled elements fail;
- active-video scope selects the center feed card, not another card;
- visible-comment-panel scope rejects hidden/exiting panels;
- input resolves to the editable descendant;
- resolution repeats after a React re-render;
- click/submit are never duplicated by fallback;
- comment input/submit are validated only after panel opening;
- per-profile results may use different candidates safely;
- read-only locator testing causes no page mutation;
- template application requires confirmation and does not overwrite saved data
  before save;
- existing strategy, scrolling, keyboard timing, Ghost Cursor, page lifecycle,
  sanitization, and persistence tests remain green.

## Live acceptance

With explicitly authorized test profiles:

1. Apply the TikTok comment template to a draft.
2. Test the three aliases in two different AdsPower windows.
3. Confirm each profile resolves the active video's comment entry.
4. Open comments and confirm input/submit resolve in both page layouts.
5. Scroll to different videos and repeat without recapturing selectors.
6. Refresh the dashboard and restart the application.
7. Confirm all locator definitions and ordering remain saved.

## Relationship to verified video scrolling

This locator design is independent of, but compatible with,
`2026-07-25-verified-video-switch-scroll-design.md`.

The final strategy sequence is:

```text
verified video switches
→ active-video scoped comment entry
→ visible-comment-panel scoped input
→ keyboard input
→ visible-comment-panel scoped submit
```

Neither design is implemented until its specification and implementation plan
are approved.
