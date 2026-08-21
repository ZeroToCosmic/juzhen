# Selector Probe Candidate Repair Design

Date: 2026-07-30  
Status: Approved and implemented

## Evidence

- TikTok exposes stable anchors for the three canonical controls:
  - `button[data-e2e="comment-icon"]`;
  - `[data-e2e="comment-input"] … [contenteditable="true"][role="textbox"]`;
  - `button[data-e2e="comment-post"]`.
- Accessible Name may be empty for entry and input; the submit Name is
  currently `publish`.
- The apparent Chinese-alias corruption was Windows terminal rendering only.
  Source, config, and database code points are canonical; no alias migration
  is required.
- The legacy config definitions used an overly broad `page` scope.

## Design

1. Preserve existing XPath definitions as last-known-good history.
2. Correct scopes:
   - `评论入口`: `active_video`;
   - `评论输入框`: `visible_comment_panel`;
   - `评论提交按钮`: `visible_comment_panel`.
3. During probing, merge the canonical TikTok template with compatible
   history without persisting it.
4. Prefer safe stable attributes, then semantic role/name fallbacks. Allow
   an empty Name only when a unique exact stable attribute anchor and accepted
   Role prove the node.
5. For the input, allow a bounded ancestor/descendant relation through up to
   eight wrapper nodes.
6. `open_read_only` remains actionable and may click once. `inspect_only`
   requires a unique visible node and repeated stable identity/semantics but
   may be disabled or covered; it is never clicked or typed into.
7. Every observe round starts from a full navigation, waits for semantic page
   readiness, and polls comment-panel visibility for up to 15 seconds.
8. Failure to close the TikTok panel with Escape falls back to closing the
   probe-owned page/Profile; captured evidence remains valid.
9. Safe underlying deterministic failure codes are preserved. Unexpected
   exceptions retain `deterministic_candidates_unavailable`.

## Safety gates

- Dedicated AdsPower test Profiles only.
- Two Profiles and two fresh consistent rounds.
- No comment text input and no submit click.
- Observe acceptance creates no selector version and performs no Redis
  publication or strategy-gate change.
- Production element resolution remains actionable and uncovered.
- Automatic recovery still requires two Profiles, two consistent rounds, and
  successful atomic publication; the manual pause/resume switch remains.

## Acceptance

Run 16 completed with eight of eight page-state records passed. All four
Profile/round combinations produced the same primary attribute candidates:

- `评论入口`: `data-e2e=comment-icon`;
- `评论输入框`: `data-e2e=comment-input` with descendant
  `contenteditable=true`, Role `textbox`;
- `评论提交按钮`: `data-e2e=comment-post`.

No selector version was created or published, and both probe-owned Profiles
were inactive after cleanup.
