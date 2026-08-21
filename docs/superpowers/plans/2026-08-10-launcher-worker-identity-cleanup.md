# Launcher Campaign Worker Identity Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement task-by-task with TDD and an independent Sol review.

**Goal:** 让启动器在不误杀其他 Python 进程的前提下，自动结束同项目遗留的 Comment Campaign Worker 并释放其 Redis 健康租约。

**Architecture:** 用纯函数生成/解析带 PID、项目指纹和随机 owner 的健康租约。启动器对 exact Redis key 做双读校验，结束精确 PID 后以 value-CAS 释放租约；所有不可验证情况 fail closed。

**Tech Stack:** Python 3.13、Redis、Windows `taskkill`、pytest。

## Global Constraints

- 只允许清理当前项目的 Comment Campaign Worker。
- 禁止 Redis SCAN/通配删除、禁止按进程名批量结束。
- 自动测试不得调用真实 Redis、`taskkill`、AdsPower 或 TikTok。
- 不修改 Flask、Campaign 队列和业务执行协议。

---

### Task 1: Worker identity contract

**Files:**
- Create: `comment_campaign/worker_identity.py`
- Modify: `comment_campaign/worker.py`
- Test: `tests/test_comment_campaign_worker.py`

**Interfaces:**
- Produces: `project_fingerprint(root) -> str`
- Produces: `build_worker_health_value(pid, root, owner_nonce) -> str`
- Produces: `parse_worker_health_value(value) -> WorkerIdentity | None`

- [x] Write failing tests for round-trip, foreign/malformed/legacy values, normalized project roots and current Worker lease output.
- [x] Run the focused worker tests and confirm failure before implementation.
- [x] Add the minimal pure identity module and switch `worker.serve()` to the new value.
- [x] Run focused tests and confirm pass.

### Task 2: Safe launcher cleanup

**Files:**
- Modify: `launcher.py`
- Test: `tests/test_launcher_restart.py`

**Interfaces:**
- Consumes: the exact Redis URL from the service environment and the identity helpers from Task 1.
- Produces: `stop_same_project_campaign_worker(...) -> int | None`.

- [x] Write failing tests for same-project cleanup, foreign/legacy/malformed refusal, current-PID refusal, command-line/PID-reuse refusal, lease-change race, Redis failure and taskkill failure.
- [x] Run focused launcher tests and confirm failure.
- [x] Implement exact-key read, identity comparison, second-read gate, exact PID termination, bounded exit wait and value-CAS release.
- [x] Call cleanup after current supervisors/Flask listener cleanup and before starting a new Campaign Worker.
- [x] Run focused launcher tests and confirm existing start/stop order remains valid.

### Task 3: Regression and review

**Files:**
- Review only the Task 1/2 diff.

- [x] Run `python -m pytest tests/test_comment_campaign_worker.py tests/test_launcher_restart.py -q -p no:cacheprovider`.
- [x] Run `python -m py_compile launcher.py comment_campaign/worker.py comment_campaign/worker_identity.py`.
- [x] Run `git diff --check`.
- [x] Have Sol verify identity, race, Redis and legacy migration boundaries.
- [x] Do not terminate the currently running legacy PID automatically; report the exact one-time migration action separately.
