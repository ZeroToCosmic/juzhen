# Selector Unsafe Self-Healing Design

**Date:** 2026-08-03

**Status:** Approved design

## Problem

Probe run 23 completed page readiness, A11y snapshot extraction, and candidate filtering. Candidate Dry-Run then failed with `selector_unsafe`. The UI incorrectly reported that no usable element was discovered, and self-healing did not run because `selector_unsafe` was classified as infrastructure failure.

An anchor-only TikTok node can be found through a stable historical attribute such as `data-e2e`, while its current accessible name does not satisfy the element contract. Candidate generation currently retains the stable attribute candidate but can also emit an incompatible Role/Name fallback. Fail-closed validation rejects the whole bundle when it reaches that unsafe fallback.

## Goal

Prevent contract-incompatible Role candidates at generation time, identify the affected element when safety validation rejects a candidate, route `selector_unsafe` through the existing three-attempt feedback-loop repair, and present the real state in the management UI.

## Scope

Modify only:

- `selector_probe/candidates.py`
- `selector_probe/validator.py`
- `selector_probe/healing_runtime.py`
- `gateway/static/selector_probe_ui.js`
- focused tests for these files

No new API, database field, Redis key, dependency, probe action, or relaxed selector safety rule.

## Design

### Candidate generation

Stable historical anchors remain allowed to recover nodes whose accessible names are absent or have changed. A Role locator is emitted only when the node's normalized accessible name satisfies the current element contract. Attribute, parent-constrained attribute, CSS, and relative XPath candidates retain existing behavior.

This prevents a node found only by historical `data-e2e` from producing a Role/Name fallback that the validator must reject.

### Validation attribution

`_candidate_ids` keeps fail-closed validation. When `_selector_safe` raises, `_candidate_ids` re-raises `ValidationRejected` with the current element alias. It does not persist or expose raw selector values.

### Failure classification and repair

`selector_unsafe` becomes a selector failure. `HealingRuntime._validation_failure` returns:

- `failure_class: selector`
- the affected alias when available
- repair-facing code `wrong_semantics`

The existing feedback loop then generates and Dry-Runs alternatives for the affected alias, up to three attempts. Existing publication, two-Profile/two-round validation, atomic release, pause, and previous-stable-version behavior remain unchanged.

### Management UI

The failure label for `selector_unsafe` is `已发现候选路径，但路径未通过安全规则`.

When candidate filtering passed but `element_dry_run` failed, the element-discovery stage reports the Dry-Run failure instead of `尚未发现可用元素`. During repair, it reports that self-healing is running or records the completed repair count using existing run evidence.

## Error Handling

- Unsafe candidates remain rejected; no safety rule is weakened.
- Missing alias falls back to existing bounded alias handling.
- Repair failure after three attempts follows the existing terminal selector-failure path.
- Raw selectors, DOM fragments, Profile IDs, and page content remain absent from persisted progress and UI output.

## Acceptance Criteria

1. An anchor-only node with a contract-incompatible accessible name produces no Role locator.
2. Stable attribute candidates for that node remain available.
3. `selector_unsafe` reports the affected element alias.
4. `selector_unsafe` enters the existing selector self-healing loop as `wrong_semantics`.
5. Only the affected strategy dependency is eligible for pause after three failed repairs.
6. UI states that a candidate failed safety validation; it does not claim that no element was discovered.
7. Probe, validator, healing-runtime, and selector-probe UI focused tests pass.

## Non-Goals

- Relaxing `_selector_safe`
- Persisting raw candidate selectors for diagnostics
- Adding another repair engine or retry counter
- Changing A11y extraction, page readiness, Profile orchestration, Redis publication, or queue recovery rules
