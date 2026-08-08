# Selector Live Picker Design

## Goal

Let an administrator open one independent AdsPower test Profile, prepare one known TikTok page state, and safely select visible elements by position. The picker supplements semantic discovery; it does not publish selectors or execute TikTok actions.

## Approved scope

- HTTP polling every 500 ms; no new WebSocket/SSE endpoint.
- One active picker globally. It shares the selector-probe Redis lease.
- Page state is `feed_ready` or `comment_panel_open`.
- Click interception uses capture phase, `preventDefault`, `stopPropagation`, and `stopImmediatePropagation`.
- At most 20 distinct selections per session.
- Redis session TTL: 5 minutes active, 10 minutes terminal.
- Session states: `starting`, `ready`, `selecting`, `confirmed`, `cancelled`, `expired`, `failed`.
- Four admin-only endpoints: start, status, confirm, cancel.
- Confirmation returns safe candidate projections. Existing element APIs remain the only path to create catalog drafts and existing two-Profile/two-round gates remain mandatory.

## Safety boundary

Browser payload is untrusted. Server applies length limits, attribute allowlists, role/tag normalization, URL-free output, deduplication, and selection-count limits. Public responses never contain raw Profile IDs, CDP URLs, backend node IDs, actor IDs, or DOM HTML.

Picker owns only its new page. It stops AdsPower Profile only when picker started it. Cleanup always removes listeners/overlay, closes owned page, and releases lease.

## Selection projection

Each selection contains `selection_id`, `fingerprint`, page state, semantic scope, tag, role, sanitized name, allowlisted attributes, normalized region (`x`, `y`, `width`, `height` in 0..1), and recommended locator candidates.

Optional `position_hint` can be copied into a semantic contract. It is a weak ranking hint only: center drift at most 12% and size drift at most 20%. It never authorizes a click or bypasses Dry-Run/publication gates.

## UI

Element directory gains **实时拾取元素**. Dialog selects masked test Profile and page state, shows lifecycle, count, selected items, remove controls, confirm, and cancel. Confirmed results feed existing element draft wizard one at a time; no fake atomic batch save.

## Failure behavior

Busy lease, invalid Profile reference, page-state timeout, CDP failure, expiration, or cleanup failure are explicit states/codes. No candidate selection closes browser before user confirmation, cancellation, timeout, or fatal failure.
