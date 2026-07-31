# Selector Probe Readiness Gate Design

Date: 2026-07-30  
Status: Approved design  
Scope: TikTok navigation readiness, one-Profile lifecycle, two-round observe
evidence, and stage diagnostics

## Problem

The latest live run opened both dedicated AdsPower Profiles and reached both
CDP endpoints, but recorded zero validations. It ended after about 31 seconds,
matching Playwright's default `page.goto()` load-event timeout. TikTok had
already rendered visible content, but the probe waited for a full page `load`
event and then cleaned up the first probe page and both owned Profiles.

The current run detail also lacks the stages between CDP readiness and terminal
failure, so navigation, readiness, semantic extraction, and inspection failures
are indistinguishable.

## Confirmed decision

Use lifecycle option A:

- start each dedicated Profile once per logical probe run;
- create one probe-owned page in that Profile;
- perform two consecutive validation rounds in the same page;
- use a bounded reload between rounds instead of restarting the Profile;
- persist validation and discovery evidence before closing the page;
- close the page and stop only Profiles started by the probe after both rounds
  finish or after terminal failure.

## Readiness gate

Navigation and page readiness are separate operations.

### Navigation

Use `page.goto(target_url, wait_until="commit")` with a bounded navigation
timeout. `commit` only confirms that response headers arrived and the document
started loading. It does not claim that TikTok is ready for extraction.

After the navigation commits, require the current page origin to match the
configured HTTPS TikTok origin. A redirect to an unexpected origin is a safety
failure.

### Conditional readiness

Poll once per second for up to 60 seconds. The duration is a failure boundary,
not a fixed sleep. A page becomes eligible for semantic extraction only when
all of these conditions pass:

1. URL remains on the expected TikTok origin.
2. `document.readyState` is `interactive` or `complete`.
3. The document root and body have visible layout.
4. No visible login blocker, CAPTCHA, fatal error, or access-denied marker is
   present.
5. No visible Skeleton marker remains for three consecutive samples.
6. A visible feed/video region is present.
7. A lightweight accessibility sample contains visible interactive nodes.
8. Two consecutive lightweight semantic fingerprints, sampled one to two
   seconds apart, are sufficiently similar after ignoring volatile state.

The stable fingerprint uses Role, accessible Name, scope, and safe stable
attributes. It excludes counters, timestamps, backend node IDs, raw DOM, raw
AX data, and transient geometry. Stability uses a similarity threshold rather
than exact equality so TikTok counters and playback state do not prevent
readiness.

Two semantic samples are stable when the Jaccard similarity of their normalized
interactive fingerprints is at least `0.85`. The gate returns an in-memory
readiness token. Full semantic extraction and element inspection require that
token. No timeout path may call the extractor.

### Timeout and retry

If the gate does not pass within 60 seconds:

1. persist the failed subcondition and masked Profile label;
2. capture the existing redacted diagnostic screenshot;
3. reload the same probe-owned page;
4. retry up to three attempts;
5. finish with `page_readiness_timeout` only after all attempts fail.

A visible login wall or CAPTCHA is not retried as ordinary slow loading. It
finishes with `probe_page_blocked` so the operator can repair the dedicated
Profile.

## Two-round flow

For each Profile:

1. connect Playwright over its verified CDP endpoint;
2. create one probe-owned page;
3. navigate using `wait_until="commit"`;
4. pass the readiness gate;
5. extract the feed A11y snapshot and interactive candidates;
6. perform the approved read-only comment-panel transition when exactly one
   safe entry candidate exists;
7. extract panel candidates without typing or submitting;
8. persist round 1;
9. reload the same page;
10. pass the readiness gate again;
11. repeat extraction and persist round 2;
12. compare canonical candidate evidence across both rounds;
13. close the probe-owned page.

After both Profiles finish, the logical observe run contains four evidence
sets: two Profiles multiplied by two rounds. Observe mode does not publish to
Redis and does not alter strategy gates.

## Stage diagnostics

Persist bounded, sanitized stages for each Profile and round:

- `playwright_connect`;
- `probe_page_open`;
- `navigate_commit`;
- `page_readiness`;
- `semantic_sample`;
- `semantic_stability`;
- `a11y_snapshot`;
- `candidate_filter`;
- `comment_panel_transition`;
- `element_dry_run`;
- `round_persist`;
- `reload`;
- `cleanup`.

Each stage exposes only masked Profile, round, attempt, status, duration,
failure code, and a fixed safe summary. CDP endpoints, full Profile IDs,
cookies, raw exceptions, raw DOM, and raw AX trees remain private.

## Failure behavior

- Failure before extraction produces no misleading empty element list.
- A readiness timeout does not pause a strategy.
- A single failed Profile makes the run unsuccessful but does not stop or
  pause unrelated automation.
- Cleanup always runs and only affects probe-owned pages and Profiles.
- The previous validated selector version remains available.

## Acceptance criteria

1. A TikTok page that needs more than 10 seconds can still pass within the
   60-second conditional gate.
2. Extraction never starts merely because a fixed delay elapsed.
3. TikTok's missing or delayed full `load` event cannot fail a page whose
   conditional readiness checks pass.
4. Each Profile starts at most once per logical run.
5. Each Profile has one probe-owned page and two persisted validation rounds.
6. The page is not closed before round evidence is committed.
7. A successful observe run exposes discovered elements and candidate
   selectors in management run detail.
8. A failed run identifies the exact stage and safe failure code instead of
   only `probe_unavailable`.
9. No probe action enters text, submits, likes, follows, publishes, or changes
   an account.

## Non-goals

- waiting for global network idle;
- using a fixed sleep as proof of readiness;
- restarting a Profile between rounds;
- changing Redis publication or automatic strategy-recovery rules;
- adding a new service, queue, or browser framework.
