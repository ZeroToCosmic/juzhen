# Project Agent Instructions

## Model routing

- The primary/orchestrating agent uses the configured default model: `gpt-5.6-luna`.
- Luna owns routine implementation, targeted code changes, test writing, test execution, and straightforward fixes.
- Planning, architecture design, complex diagnosis, and final review must be delegated to a Sol subagent.
- Every Sol delegation must explicitly use:
  - `model: gpt-5.6-sol`
  - `reasoning_effort: high`
- Sol planning/review tasks must be bounded and independently verifiable.
- Sol reviewers are read-only unless the user explicitly authorizes them to edit.
- After Luna implements a feature, Sol must perform the final architecture and code review.
- If Sol finds a blocking issue, Luna fixes it and Sol reviews the fix again.
- Do not silently substitute another model when Luna or Sol is unavailable; report the unavailable model to the user.

This file explicitly authorizes subagent delegation for the Sol planning, diagnosis, and review responsibilities above.

## Concurrent development coordination

Multiple agents may work in this repository at the same time. File-level overwrites are silent (no git conflict until commit), so the rules below are mandatory.

### Ownership claims

- Before editing any file, read this section and `git status` / `git diff --stat` to detect uncommitted changes made by other agents.
- Do NOT edit a file another agent has uncommitted changes for. Ask the user to have the other agent commit first, or work around it.
- When starting work on a feature, claim the files you will modify by appending an entry under "Active claims" below; remove the entry when done.
- The other agent must do the same. Never work on a file claimed by someone else.

### Shared entry files (minimal-invasive rule)

These files aggregate blueprints/registrations and are most likely to collide. Prefer creating new modules and registering them here with 1-2 lines:

- `central/app.py`, `gateway/app.py`
- `central/migrate.py`, `central/db.py`
- `tests/conftest.py`, `launcher.py`, `requirements.txt`
- `gateway/routes_console.py` (and any other `routes_*.py`)

### Commit discipline

- Commit small, per-feature units; do not leave large batches uncommitted.
- Before committing, re-check `git status` and `git log --oneline -5`; stage only files belonging to this work.
- Pull/rebase against the other agent's commits before touching shared files.
- Uncommitted changes are the highest overwrite risk — keep them short-lived.

### Conflict resolution

- If a merge conflict appears, resolve conservatively: keep both behaviors when they are independent; prefer the other agent's changes when overlapping; never silently drop work.
- After resolution, run the affected test subset and report to the user.

### Active claims

(No active claims. Each agent: add `- YYYY-MM-DD <area> — files: <paths> (agent: <name>)` and remove when done.)

