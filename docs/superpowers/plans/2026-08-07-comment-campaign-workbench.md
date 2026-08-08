# TikTok Comment Campaign Workbench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local, human-confirmed Comment Campaign workbench that supports independent top-level comments and dependency-aware threaded replies across authorized AdsPower Profiles.

**Architecture:** Add an isolated `comment_campaign` package with SQLAlchemy-backed SQLite state, Pydantic request validation, deterministic account allocation, RQ/Redis coordination, and short-lived Playwright jobs. Reuse the existing AdsPower controller, V2 session/tiling/locator primitives, content library, dashboard shell, and close-confirmation behavior; do not add Campaign state to `gateway/app.py` or `execution_v2/service.py`.

**Tech Stack:** Python 3, Flask 3.1, SQLAlchemy 2, Pydantic 2, RQ 2, Redis, Playwright Python, pytest, Node test runner, existing vanilla JavaScript/CSS dashboard.

## Global Constraints

- Only user-owned or explicitly authorized accounts and content may be used.
- Every real submit requires a one-time human approval for the exact Assignment revision; automated tests stop before real submit.
- Never auto-retry an uncertain submit. A process restart during `submitting` or `verifying_receipt` becomes `published_unverified`.
- Roles are Campaign-scoped, never Profile-scoped. The same Profile may be `owner` on video A and `participant` on video B.
- One Campaign uses each Profile and resolved comment text at most once by default.
- Default batch size is 3; accepted range is 1–8. The next batch starts only after every Profile in the previous batch is confirmed closed.
- SQLite is the source of truth. Redis stores only queue entries, leases, idempotency keys, and short-lived health state.
- API and UI never expose raw AdsPower IDs, cookies, authorization headers, API keys, or CDP WebSocket URLs.
- Parent failure pauses only descendants of that parent. Independent comments and unrelated branches continue.
- No CAPTCHA bypass, risk-control evasion, anti-detection additions, automatic comment generation, or automatic submission approval.
- Existing V1, Selector Probe, V2 elements, V2 strategies, and existing SQLite records are not migrated or mutated.

---

## File Structure

Create these focused modules:

```text
comment_campaign/
  __init__.py             package exports
  domain.py               enums, immutable domain values, state transitions
  schemas.py              strict Pydantic request/response models
  models.py               SQLAlchemy tables only
  database.py             SQLite engine/session creation and initialization
  store.py                transactions and persistence queries
  video.py                TikTok URL normalization and video-id verification
  allocation.py           deterministic constrained assignment
  service.py              template/Campaign orchestration boundary
  blueprint.py            strict Flask API and redaction
  queueing.py             RQ adapter, Redis leases, health projection
  jobs.py                 importable RQ job entry points
  worker.py               worker CLI
  profile_gateway.py      stable Profile resolution and AdsPower/CDP lifecycle
  locator.py              comment/input/submit/parent scoped locators
  receipts.py             text normalization, receipt construction, verification
  executor.py             prepare batch and submit one approved assignment
  errors.py               fixed internal/public error codes
```

Modify only narrow integration points:

```text
requirements.txt
adspower.py
gateway/app.py
gateway/templates/_dashboard_sidebar.html
launcher.py
```

Create the workbench surface:

```text
gateway/templates/comment_campaign.html
gateway/static/comment_campaign.js
gateway/static/comment_campaign.css
```

Tests mirror those boundaries under `tests/` and `tests-js/`.

---

### Task 1: Dependencies and Complete Profile Discovery

**Files:**
- Modify: `requirements.txt`
- Modify: `adspower.py`
- Test: `tests/test_adspower.py`

**Interfaces:**
- Produces: `AdsPowerController.list_all_profiles(*, page_size: int = 200, max_profiles: int = 1000) -> list[dict[str, Any]]`
- Consumes: existing `AdsPowerController.list_profiles(page, page_size)`

- [ ] **Step 1: Add failing pagination tests**

```python
def test_list_all_profiles_reads_every_page(monkeypatch):
    controller = AdsPowerController(max_retries=1)
    pages = {
        1: [{"id": f"p-{index}"} for index in range(200)],
        2: [{"id": f"p-{index}"} for index in range(200, 300)],
    }
    monkeypatch.setattr(
        controller,
        "list_profiles",
        lambda *, page, page_size: pages.get(page, []),
    )
    rows = controller.list_all_profiles()
    assert [row["id"] for row in rows] == [f"p-{index}" for index in range(300)]


def test_list_all_profiles_uses_raw_page_size_not_filtered_size(monkeypatch):
    controller = AdsPowerController(max_retries=1)
    pages = {
        1: {"list": [{"user_id": f"p-{index}"} for index in range(199)] + [{"name": "invalid"}], "total": 201},
        2: {"list": [{"user_id": "p-199"}], "total": 201},
    }
    monkeypatch.setattr(
        controller,
        "_request_with_retry",
        lambda _endpoint, _profile_id, params: pages.get(params["page"], {"list": [], "total": 201}),
    )
    assert len(controller.list_all_profiles()) == 200
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `pytest tests/test_browser_public_identity.py tests/test_adspower.py -q`

Expected: FAIL because `list_all_profiles` and raw-page termination do not exist.

- [ ] **Step 3: Add exact runtime dependencies**

Append to `requirements.txt`:

```text
pydantic>=2.8,<3.0
rq>=2.2,<3.0
```

- [ ] **Step 4: Implement raw-page-aware pagination**

Extract normalization into a private page method without changing the existing public `list_profiles` result:

```python
def _list_profile_page(
    self, *, page: int, page_size: int
) -> tuple[list[dict[str, Any]], int, int | None]:
    self._validate_profile_page(page, page_size)
    data = self._request_with_retry(
        "/api/v1/user/list", None, {"page": page, "page_size": page_size}
    )
    raw_rows = data if isinstance(data, list) else data.get("list", [])
    if not isinstance(raw_rows, list):
        raise AdsPowerError("AdsPower profile list is invalid")
    total_value = None if isinstance(data, list) else data.get("total")
    total = total_value if isinstance(total_value, int) and not isinstance(total_value, bool) and total_value >= 0 else None
    return self._normalize_profile_rows(raw_rows), len(raw_rows), total


def list_profiles(self, *, page: int = 1, page_size: int = 200) -> list[dict[str, Any]]:
    rows, _raw_count, _total = self._list_profile_page(page=page, page_size=page_size)
    return rows


def list_all_profiles(
    self, *, page_size: int = 200, max_profiles: int = 1000
) -> list[dict[str, Any]]:
    if isinstance(max_profiles, bool) or not isinstance(max_profiles, int) or not 1 <= max_profiles <= 5000:
        raise ValueError("max_profiles must be between 1 and 5000")
    profiles: list[dict[str, Any]] = []
    seen: set[str] = set()
    page = 1
    max_pages = (5000 + page_size - 1) // page_size + 1
    while len(profiles) < max_profiles and page <= max_pages:
        rows, raw_count, total = self._list_profile_page(page=page, page_size=page_size)
        for row in rows:
            if row["id"] not in seen:
                seen.add(row["id"])
                profiles.append(row)
                if len(profiles) == max_profiles:
                    break
        if raw_count < page_size or total is not None and page * page_size >= total:
            break
        page += 1
    else:
        if page > max_pages:
            raise AdsPowerError("AdsPower profile pagination limit exceeded")
    return profiles
```

Add tests for 200+100, exactly 400 plus an empty third page, a full raw page containing an invalid row, duplicate IDs, `max_profiles=250`, a later-page error, an endlessly repeated full page that fails after the hard scan bound, and invalid boolean/string/float paging values.

- [ ] **Step 5: Run tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_browser_public_identity.py tests/test_adspower.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add requirements.txt adspower.py tests/test_adspower.py
git commit -m "feat(campaign): add complete Profile paging"
```

---

### Task 2: Domain Types, State Machine, and SQLAlchemy Persistence

**Files:**
- Create: `comment_campaign/__init__.py`
- Create: `comment_campaign/domain.py`
- Create: `comment_campaign/schemas.py`
- Create: `comment_campaign/models.py`
- Create: `comment_campaign/database.py`
- Create: `comment_campaign/store.py`
- Create: `comment_campaign/errors.py`
- Test: `tests/test_comment_campaign_domain.py`
- Test: `tests/test_comment_campaign_store.py`

**Interfaces:**
- Produces: `CampaignStore(database_url: str)` with `initialize()`, template, Campaign, Assignment, Receipt, Attempt, and Profile metadata transactions.
- Produces: `transition_campaign(current, target)`, `transition_assignment(current, target)`.
- Produces: strict Pydantic models `TemplateCreate`, `TemplateUpdate`, `CampaignCreate`, `ProfileMetadataUpsert`, `AssignmentOverride`.

- [ ] **Step 1: Write failing state and persistence tests**

```python
def test_role_is_assignment_scoped(tmp_path):
    store = CampaignStore(f"sqlite:///{tmp_path / 'campaign.db'}")
    store.initialize()
    [identity] = store.sync_profile_identities([
        {"id": "raw-profile-a", "name": "Alice", "status": "Active"},
    ])
    store.upsert_profile_metadata(
        profile_ref=identity["profile_ref"], expected_username="alice", enabled=True,
        login_verified=True, tags=["en"], language="en", region="US",
        cooldown_until=None, health_status="healthy",
    )
    row = store.get_profile_metadata(identity["profile_ref"])
    assert "role" not in row


def test_submitting_cannot_transition_back_to_ready():
    with pytest.raises(StateTransitionError):
        transition_assignment("submitting", "awaiting_step_approval")
    assert transition_assignment("submitting", "published_unverified") == "published_unverified"
```

- [ ] **Step 2: Run tests and verify imports fail**

Run: `pytest tests/test_comment_campaign_domain.py tests/test_comment_campaign_store.py -q`

Expected: FAIL with `ModuleNotFoundError: comment_campaign`.

- [ ] **Step 3: Define fixed domain enums and transitions**

Implement in `comment_campaign/domain.py`:

```python
from enum import StrEnum


class CampaignMode(StrEnum):
    INDEPENDENT = "independent"
    THREADED = "threaded"


class CampaignStatus(StrEnum):
    DRAFT = "draft"
    PLANNED = "planned"
    AWAITING_CAMPAIGN_APPROVAL = "awaiting_campaign_approval"
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    FAILED = "failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class AssignmentStatus(StrEnum):
    PLANNED = "planned"
    WAITING_DEPENDENCY = "waiting_dependency"
    OPENING_PROFILE = "opening_profile"
    LOCATING_VIDEO = "locating_video"
    LOCATING_PARENT = "locating_parent"
    PREPARING_COMMENT = "preparing_comment"
    AWAITING_STEP_APPROVAL = "awaiting_step_approval"
    SUBMITTING = "submitting"
    VERIFYING_RECEIPT = "verifying_receipt"
    PUBLISHED_VERIFIED = "published_verified"
    PUBLISHED_UNVERIFIED = "published_unverified"
    PAUSED = "paused"
    PAUSED_DEPENDENCY = "paused_dependency"
    FAILED = "failed"
    CANCELLED = "cancelled"


CAMPAIGN_TRANSITIONS = {
    "draft": {"planned", "cancelled"},
    "planned": {"awaiting_campaign_approval", "draft", "cancelled"},
    "awaiting_campaign_approval": {"queued", "draft", "cancelled"},
    "queued": {"running", "paused", "cancelled"},
    "running": {"paused", "failed", "completed", "cancelled"},
    "paused": {"queued", "cancelled"},
    "failed": set(), "completed": set(), "cancelled": set(),
}


ASSIGNMENT_TRANSITIONS = {
    "planned": {"waiting_dependency", "opening_profile", "cancelled"},
    "waiting_dependency": {"opening_profile", "paused_dependency", "cancelled"},
    "opening_profile": {"locating_video", "failed", "paused"},
    "locating_video": {"locating_parent", "preparing_comment", "failed", "paused"},
    "locating_parent": {"preparing_comment", "failed", "paused_dependency"},
    "preparing_comment": {"awaiting_step_approval", "failed", "paused"},
    "awaiting_step_approval": {"submitting", "paused", "cancelled"},
    "submitting": {"verifying_receipt", "published_unverified"},
    "verifying_receipt": {"published_verified", "published_unverified"},
    "published_unverified": {"published_verified", "paused"},
    "published_verified": set(), "paused": {"opening_profile", "cancelled"},
    "paused_dependency": {"waiting_dependency", "cancelled"},
    "failed": set(), "cancelled": set(),
}
```

Define `StateTransitionError` in `errors.py`; both transition functions must reject any edge not present in these maps.

Define the complete fixed error set in `errors.py` so internal exceptions, API responses, Attempt rows, and UI labels use identical codes:

```python
ERROR_CODES = frozenset({
    "adspower_unavailable", "profile_start_failed", "cdp_connect_failed",
    "profile_identity_mismatch", "target_video_invalid", "target_video_mismatch",
    "comment_panel_not_ready", "comment_input_not_found",
    "parent_comment_not_found", "parent_comment_ambiguous",
    "comment_author_mismatch", "reply_target_mismatch",
    "comment_submit_uncertain", "comment_receipt_unverified",
    "profile_close_failed", "redis_unavailable", "worker_unavailable",
    "allocation_unsatisfied", "approval_revision_mismatch",
    "revision_conflict", "invalid_state_transition",
})
```

- [ ] **Step 4: Define strict request schemas**

All input models use `ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)`. IDs are non-empty and length-bounded. Normalize list members before validation, reject blank members, and reject duplicates after trimming.

Use `ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)` on every input model. IDs must be non-empty and length-bounded. Normalize list members before validation, reject blank members, and reject duplicates after trimming. `TemplateCreate.steps` must contain 1–100 steps. `CampaignCreate.profile_refs` must contain 1–300 unique values. `batch_size` is `Field(default=3, ge=1, le=8)`. Represent steps with these exact fields:

```python
class CommentStepInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)
    id: str
    label: str = Field(min_length=1, max_length=100)
    content_source: Literal["fixed", "library"]
    fixed_text: str = Field(default="", max_length=2200)
    content_library_id: str = Field(default="", max_length=120)
    content_item_id: str = Field(default="", max_length=120)
    parent_step_id: str | None = None
    required_profile_tags: list[str] = Field(default_factory=list, max_length=20)
    excluded_profile_tags: list[str] = Field(default_factory=list, max_length=20)
    language: str = Field(default="", max_length=32)
```

Add model validators that require exactly the matching content source and reject duplicate step IDs. `TemplateCreate.description` is at most 500 characters. `scheduled_at` and `cooldown_until` accept only timezone-aware ISO-8601 strings and normalize them to UTC before persistence.

- [ ] **Step 5: Define SQLAlchemy records and SQLite initialization**

Use a local declarative `Base`; do not import the project MySQL `Base`. Create these table classes with JSON stored as `Text` and revisions as integers:

```text
CommentTemplateRecord       comment_templates
CommentTemplateRevision     comment_template_revisions
CommentStepRecord           comment_steps
CommentCampaignRecord       comment_campaigns
CommentAssignmentRecord     comment_assignments
CommentReceiptRecord        comment_receipts
CommentAttemptRecord        comment_attempts
CommentProfileIdentityRecord comment_profile_identities
CommentProfileMetadataRecord comment_profile_metadata
```

`CommentStepRecord` identity is `(template_id, template_revision, step_id)`. Its parent foreign key is the same three-part key, so a parent cannot cross template revisions. `CommentProfileMetadataRecord.profile_ref` is a mandatory foreign key to `CommentProfileIdentityRecord`; sync identity rows before metadata.

`CommentProfileIdentityRecord` stores a random `profile_ref` UUID and the raw AdsPower ID in the local Campaign database. The raw value is never serialized, logged, placed in Redis, or returned by API. Enforce unique constraints on raw ID and `profile_ref`, plus `(template_id, revision)`, `(campaign_id, step_id)`, and `(campaign_id, assignment_id, attempt_no)`. Enable SQLite foreign keys and WAL in `database.py` using SQLAlchemy connect events.

```python
def create_campaign_engine(database_url: str) -> Engine:
    engine = create_engine(database_url, future=True)
    if database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def configure_sqlite(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()
    return engine
```

- [ ] **Step 6: Implement transaction methods**

`CampaignStore` must expose these exact methods, each opening one `Session.begin()` transaction:

```python
initialize() -> None
create_template(payload: TemplateCreate, template_id: str) -> dict
update_template(template_id: str, expected_revision: int, payload: TemplateUpdate) -> dict
disable_template(template_id: str, expected_revision: int) -> dict
list_templates() -> list[dict]
get_template(template_id: str, revision: int | None = None) -> dict | None
upsert_profile_metadata(**fields) -> dict
list_profile_metadata() -> list[dict]
get_profile_metadata(profile_ref: str) -> dict | None
sync_profile_identities(raw_profiles: list[dict]) -> list[dict]
get_raw_profile_id(profile_ref: str) -> str | None
create_campaign(payload: CampaignCreate, campaign_id: str, video_id: str, canonical_url: str) -> dict
get_campaign(campaign_id: str) -> dict | None
list_campaigns(status: str | None, limit: int, offset: int) -> list[dict]
transition_campaign_status(campaign_id: str, expected_revision: int, status: str, **fields) -> dict
replace_assignments(campaign_id: str, assignments: list[dict]) -> list[dict]
list_assignments(campaign_id: str) -> list[dict]
update_assignment_status(assignment_id: str, expected_revision: int, status: str, **fields) -> dict
append_attempt(assignment_id: str, stage: str, status: str, **fields) -> dict
save_receipt(assignment_id: str, receipt: dict) -> dict
list_receipts(campaign_id: str) -> list[dict]
list_attempts(campaign_id: str) -> list[dict]
recover_interrupted_submissions() -> int
```

Every public composite operation opens exactly one transaction and calls internal session-bound primitives. Template, Campaign, and Assignment revisions use real compare-and-swap: update by ID plus expected revision, increment in the same statement, require `rowcount == 1`, and return the stable `revision_conflict` error otherwise.

`recover_interrupted_submissions()` updates every `submitting` or `verifying_receipt` assignment to `published_unverified` in one transaction and never enqueues work.

`sync_profile_identities()` reads only the raw row whitelist `id`, `name`, and `status`; reuses an existing random UUID for a known raw ID; and creates `profile_ref_<uuid4 hex>` for a new raw ID. Concurrent first sync catches the unique-key race, rolls back, and rereads without exposing SQL parameters. Returned dictionaries are rebuilt from an explicit safe whitelist and contain only `profile_ref`, masked display ID, name, and status. `get_raw_profile_id()` is an internal-only method used by `ProfileGateway`; no service or Blueprint method may return its result.

- [ ] **Step 7: Run domain/store tests**

Run: `pytest tests/test_comment_campaign_domain.py tests/test_comment_campaign_store.py -q`

Expected: PASS, including foreign-key, revision-conflict, snapshot, and restart-recovery tests.

- [ ] **Step 8: Commit**

```bash
git add comment_campaign tests/test_comment_campaign_domain.py tests/test_comment_campaign_store.py
git commit -m "feat(campaign): add durable domain state"
```

---

### Task 3: Template Validation, Video Resolution, and Deterministic Allocation

**Files:**
- Create: `comment_campaign/video.py`
- Create: `comment_campaign/allocation.py`
- Create: `comment_campaign/service.py`
- Test: `tests/test_comment_campaign_video.py`
- Test: `tests/test_comment_campaign_allocation.py`
- Test: `tests/test_comment_campaign_service.py`

**Interfaces:**
- Produces: `normalize_tiktok_video(reference: str) -> TargetVideo`
- Produces: `validate_template_tree(mode: CampaignMode, steps: Sequence[CommentStepInput]) -> None`
- Produces: `allocate(steps, profiles, texts, seed) -> list[PlannedAssignment]`
- Produces: `CommentCampaignService.plan_campaign(campaign_id: str, seed: str | None = None) -> dict`

- [ ] **Step 1: Write failing URL, tree, and role-switch tests**

```python
def test_video_url_is_canonicalized():
    target = normalize_tiktok_video(
        "https://www.tiktok.com/@alice/video/7469123456789012345?lang=en"
    )
    assert target.video_id == "7469123456789012345"
    assert target.canonical_url == "https://www.tiktok.com/@alice/video/7469123456789012345"


def test_same_profile_can_change_role_across_campaigns(service):
    first = service.plan_campaign("campaign-video-a", seed="seed-a")
    second = service.plan_campaign("campaign-video-b", seed="seed-b")
    by_profile_a = {row["profile_ref"]: row["role"] for row in first["assignments"]}
    by_profile_b = {row["profile_ref"]: row["role"] for row in second["assignments"]}
    assert any(by_profile_a[key] != by_profile_b[key] for key in by_profile_a.keys() & by_profile_b.keys())
```

- [ ] **Step 2: Run tests and verify failure**

Run: `pytest tests/test_comment_campaign_video.py tests/test_comment_campaign_allocation.py tests/test_comment_campaign_service.py -q`

Expected: FAIL because planning modules do not exist.

- [ ] **Step 3: Implement strict TikTok target normalization**

```python
VIDEO_PATH = re.compile(r"^/@([^/]+)/video/(\d{8,30})/?$")


@dataclass(frozen=True, slots=True)
class TargetVideo:
    video_id: str
    canonical_url: str


def normalize_tiktok_video(reference: str) -> TargetVideo:
    parsed = urlparse(str(reference or "").strip())
    if parsed.scheme != "https" or parsed.hostname not in {"tiktok.com", "www.tiktok.com"}:
        raise CampaignValidationError("target_video_invalid")
    match = VIDEO_PATH.fullmatch(parsed.path)
    if match is None:
        raise CampaignValidationError("target_video_invalid")
    username, video_id = match.groups()
    return TargetVideo(video_id, f"https://www.tiktok.com/@{username}/video/{video_id}")
```

For `publish_result`, inject a `publish_result_resolver(reference_id) -> str` into the service, then feed its returned URL through this same function.

- [ ] **Step 4: Implement complete template-tree validation**

Build an adjacency map and DFS color map. Reject unknown parent, self-parent, cycle, more than one threaded root, no threaded root, any parent in independent mode, and a template mode incompatible with the Campaign mode. Role is always derived: `commenter` for independent, otherwise root `owner`, non-root `participant`.

```python
def role_for(mode: CampaignMode, parent_step_id: str | None) -> str:
    if mode is CampaignMode.INDEPENDENT:
        return "commenter"
    return "owner" if parent_step_id is None else "participant"
```

- [ ] **Step 5: Implement deterministic constrained allocation**

```python
def allocate(steps, profiles, texts, seed):
    candidates = {
        step.id: [profile for profile in profiles if profile_matches(step, profile)]
        for step in steps
    }
    ordered = sorted(steps, key=lambda step: (len(candidates[step.id]), step.id))
    chosen: dict[str, dict] = {}

    # Use a deterministic Kuhn/Hopcroft-Karp augmenting-path matcher here.
    # The problem is bipartite matching, so do not use exponential backtracking.
    if not find_complete_matching(ordered, candidates, chosen, seed):
        raise AllocationError("allocation_unsatisfied")
    return build_planned_assignments(steps, chosen, texts)
```

`build_planned_assignments` assigns each frozen text once, derives roles, and maps `parent_assignment_id` after all assignment IDs exist. Historical roles are not inputs to `profile_matches`.

- [ ] **Step 6: Implement Campaign planning service**

`plan_campaign` must load the Campaign, exact template revision, Profile metadata snapshot, and content snapshot; validate all inputs; allocate completely in memory; then replace all Assignments in one transaction. If any check fails, no Assignment is stored. `reallocate_campaign` is allowed only before plan lock. `lock_plan` stores `template_snapshot`, `profile_snapshot`, `content_snapshot`, and `locked_at` and transitions to `awaiting_campaign_approval`.

- [ ] **Step 7: Run tests**

Run: `pytest tests/test_comment_campaign_video.py tests/test_comment_campaign_allocation.py tests/test_comment_campaign_service.py -q`

Expected: PASS, including 300-Profile deterministic allocation and cross-Campaign role switching.

- [ ] **Step 8: Commit**

```bash
git add comment_campaign/video.py comment_campaign/allocation.py comment_campaign/service.py tests/test_comment_campaign_video.py tests/test_comment_campaign_allocation.py tests/test_comment_campaign_service.py
git commit -m "feat(campaign): plan deterministic comment roles"
```

---

### Task 4: Strict Comment Campaign API and Gateway Integration

**Files:**
- Create: `comment_campaign/blueprint.py`
- Modify: `comment_campaign/service.py`
- Modify: `gateway/app.py`
- Test: `tests/test_comment_campaign_routes.py`
- Test: `tests/test_comment_campaign_integration.py`

**Interfaces:**
- Produces: `create_comment_campaign_blueprint(service_or_factory) -> Blueprint`
- Produces: `create_default_comment_campaign_service(...) -> CommentCampaignService`
- Consumes: `CampaignStore`, allocation service, existing AdsPower/content providers, and a queue coordinator injected later.

- [ ] **Step 1: Write route-contract tests**

Test every approved endpoint with a fake service. Assert exact status codes, `{"data": ...}` success envelope, `{"error": {"code", "message"}}` error envelope, unknown-field rejection, revision conflict, and local-only protection.

```python
def test_template_routes_delegate(client, fake_service):
    listed = client.get("/api/browser-v2/comment-templates")
    created = client.post(
        "/api/browser-v2/comment-templates",
        json={
            "name": "thread",
            "description": "",
            "supported_modes": ["threaded"],
            "language": "en",
            "tags": [],
            "steps": [{
                "id": "root", "label": "owner", "content_source": "fixed",
                "fixed_text": "hello", "content_library_id": "",
                "content_item_id": "", "parent_step_id": None,
                "required_profile_tags": [], "excluded_profile_tags": [],
                "language": "en",
            }],
        },
    )
    assert listed.status_code == 200
    assert created.status_code == 201
    assert fake_service.calls[:2] == ["list_templates", "create_template"]
```

- [ ] **Step 2: Run tests and verify failure**

Run: `pytest tests/test_comment_campaign_routes.py tests/test_comment_campaign_integration.py -q`

Expected: FAIL because the blueprint is missing.

- [ ] **Step 3: Implement all route contracts**

Use one Blueprint with `url_prefix="/api/browser-v2"`. Implement these paths exactly:

```text
GET/POST  /comment-templates
GET/PUT   /comment-templates/<template_id>
POST      /comment-templates/<template_id>/disable
GET/POST  /comment-profile-metadata
GET/POST  /comment-campaigns
GET       /comment-campaigns/<campaign_id>
POST      /comment-campaigns/<campaign_id>/plan
POST      /comment-campaigns/<campaign_id>/reallocate
PUT       /comment-campaigns/<campaign_id>/assignments/<assignment_id>
POST      /comment-campaigns/<campaign_id>/lock-plan
POST      /comment-campaigns/<campaign_id>/approve
POST      /comment-campaigns/<campaign_id>/pause
POST      /comment-campaigns/<campaign_id>/resume
POST      /comment-campaigns/<campaign_id>/cancel
GET       /comment-campaigns/<campaign_id>/approvals
POST      /comment-campaigns/<campaign_id>/assignments/<assignment_id>/approve-submit
POST      /comment-campaigns/<campaign_id>/assignments/<assignment_id>/reject-submit
POST      /comment-campaigns/<campaign_id>/assignments/<assignment_id>/resolve-unverified
GET       /comment-campaigns/<campaign_id>/receipts
GET       /comment-campaigns/<campaign_id>/attempts
GET       /comment-campaign-health
```

Parse each write body with the corresponding Pydantic model. Do not use permissive `request.get_json()` field access. Map fixed domain errors to 404/409/422/503 and keep a fixed Chinese public-message dictionary.

`resolve-unverified` accepts exactly `expected_revision`, `resolution` (`published` or `not_published`), and a non-empty `reason`. `published` records a manual-verification Receipt event before changing status to `published_verified`. `not_published` changes status to `paused`; an explicit resume starts preparation again and requires a new approval before another submit.

- [ ] **Step 4: Add recursive redaction**

Drop keys matching `profile_id`, `raw_profile_id`, `cookie`, `authorization`, `api_key`, `ws_url`, or `websocket`. Allow `profile_ref`, `display_profile`, `campaign_id`, `assignment_id`, and `receipt_id`. Replace any string beginning `ws://` or `wss://` with `[redacted]`.

- [ ] **Step 5: Wire a lazy singleton into Gateway**

In `create_app`, add config defaults:

```python
COMMENT_CAMPAIGN_DB_URL = "sqlite:///data/comment_campaign/comment_campaign.db"
COMMENT_CAMPAIGN_EVIDENCE_DIR = "data/comment_campaign/evidence"
COMMENT_CAMPAIGN_REDIS_URL = os.getenv("COMMENT_CAMPAIGN_REDIS_URL", os.getenv("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0"))
```

Create `app.extensions["comment_campaign_service_factory"]` using the same lock-and-lazy pattern as `execution_v2_service_factory`; register the Blueprint. Inject the persisted AdsPower base URL/API key and existing content library functions. Do not add business methods to `gateway/app.py`.

- [ ] **Step 6: Run route/integration tests**

Run: `pytest tests/test_comment_campaign_routes.py tests/test_comment_campaign_integration.py tests/test_execution_v2_integration.py -q`

Expected: PASS; V2 behavior remains unchanged.

- [ ] **Step 7: Commit**

```bash
git add comment_campaign/blueprint.py comment_campaign/service.py gateway/app.py tests/test_comment_campaign_routes.py tests/test_comment_campaign_integration.py
git commit -m "feat(campaign): expose strict local API"
```

---

### Task 5: RQ Coordination, Redis Leases, Scheduling, and Launcher Worker

**Files:**
- Create: `comment_campaign/queueing.py`
- Create: `comment_campaign/jobs.py`
- Create: `comment_campaign/worker.py`
- Modify: `comment_campaign/service.py`
- Modify: `launcher.py`
- Test: `tests/test_comment_campaign_queueing.py`
- Test: `tests/test_comment_campaign_worker.py`
- Modify: `tests/test_launcher_restart.py`

**Interfaces:**
- Produces: `RedisLease.acquire() -> bool`, `refresh() -> bool`, `release() -> bool`
- Produces: `QueueCoordinator.enqueue_prepare(campaign_id)`, `enqueue_submit(campaign_id, assignment_id, revision)`, `enqueue_at(campaign_id, when)`.
- Produces importable RQ jobs `run_prepare_campaign`, `run_submit_assignment`, `run_reconcile_campaign`.
- Produces: `CommentCampaignWorkerSupervisor`.

- [ ] **Step 1: Write failing lease, idempotency, and supervisor tests**

```python
def test_release_uses_owner_compare_and_delete(fake_redis):
    first = RedisLease(fake_redis, "profile:a", "owner-a", ttl_seconds=30)
    second = RedisLease(fake_redis, "profile:a", "owner-b", ttl_seconds=30)
    assert first.acquire() is True
    assert second.release() is False
    assert fake_redis.get("profile:a") == b"owner-a"


def test_launcher_starts_comment_worker_hidden(tmp_path):
    process, calls = FakeProcess(), []
    worker = CommentCampaignWorkerSupervisor(
        popen_factory=lambda command, **kwargs: calls.append((command, kwargs)) or process,
        log_path=tmp_path / "campaign-worker.log",
    )
    worker.start(environment={"COMMENT_CAMPAIGN_REDIS_URL": "redis://127.0.0.1/0"})
    assert calls[0][0][-3:] == ["-m", "comment_campaign.worker", "serve"]
    assert calls[0][1]["creationflags"] == launcher_module.subprocess.CREATE_NO_WINDOW
```

- [ ] **Step 2: Run tests and verify failure**

Run: `pytest tests/test_comment_campaign_queueing.py tests/test_comment_campaign_worker.py tests/test_launcher_restart.py -q`

Expected: FAIL because queue and worker classes are absent.

- [ ] **Step 3: Implement safe Redis leases**

Use `SET key owner NX EX ttl` for acquisition. Use Lua compare-and-delete and compare-and-expire scripts for release/refresh:

```python
RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""

REFRESH_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""
```

Prefix every key with `browser_v2:comment_campaign:`. Lease keys are `campaign:<id>`, `profile:<profile_ref>`, `video_submit:<video_id>`, and `approval:<assignment_id>:<revision>`.

- [ ] **Step 4: Implement RQ coordinator and idempotent jobs**

```python
class QueueCoordinator:
    def enqueue_prepare(self, campaign_id: str):
        return self.queue.enqueue(
            "comment_campaign.jobs.run_prepare_campaign",
            campaign_id,
            job_id=f"campaign-prepare-{campaign_id}",
            job_timeout=600,
            result_ttl=86400,
        )

    def enqueue_submit(self, campaign_id: str, assignment_id: str, revision: int):
        return self.queue.enqueue(
            "comment_campaign.jobs.run_submit_assignment",
            campaign_id, assignment_id, revision,
            job_id=f"campaign-submit-{assignment_id}-r{revision}",
            job_timeout=300,
            result_ttl=86400,
        )
```

RQ duplicate Job IDs must return the existing job rather than enqueue duplicate work. `approve-submit` persists the revision and approval token before enqueueing. Scheduled Campaigns use `queue.enqueue_at(utc_datetime, "comment_campaign.jobs.run_prepare_campaign", campaign_id)`.

- [ ] **Step 5: Implement Worker CLI and health heartbeat**

`python -m comment_campaign.worker serve` loads `.env`, initializes the store, runs `recover_interrupted_submissions()`, writes a Redis health key with a 30-second TTL, and runs `Worker([Queue("browser_v2_comment_campaign")], connection=redis).work(with_scheduler=True)`. A background heartbeat refreshes the health key every 10 seconds and stops with the process.

- [ ] **Step 6: Add launcher supervisor**

Model `CommentCampaignWorkerSupervisor` after `SelectorProbeWorkerSupervisor`, with command:

```python
[sys.executable, "-m", "comment_campaign.worker", "serve"]
```

Use log `data/logs/comment-campaign-worker.log`. Add it to `LauncherApp.__init__`, `_stop_services_best_effort`, `_restart_services`, `_automatic_start_failure_detail`, and post-start health checks. A Campaign worker startup failure must stop all newly started project services and show its log path.

- [ ] **Step 7: Run tests**

Run: `pytest tests/test_comment_campaign_queueing.py tests/test_comment_campaign_worker.py tests/test_launcher_restart.py -q`

Expected: PASS, including lost-owner, duplicate enqueue, scheduled enqueue, recovery, hidden process, and cleanup tests.

- [ ] **Step 8: Commit**

```bash
git add comment_campaign/queueing.py comment_campaign/jobs.py comment_campaign/worker.py comment_campaign/service.py launcher.py tests/test_comment_campaign_queueing.py tests/test_comment_campaign_worker.py tests/test_launcher_restart.py
git commit -m "feat(campaign): add durable RQ coordination"
```

---

### Task 6: Browser Preparation and Human-Approved Independent Submission

**Files:**
- Create: `comment_campaign/profile_gateway.py`
- Create: `comment_campaign/locator.py`
- Create: `comment_campaign/receipts.py`
- Create: `comment_campaign/executor.py`
- Modify: `comment_campaign/jobs.py`
- Modify: `comment_campaign/service.py`
- Test: `tests/test_comment_campaign_profile_gateway.py`
- Test: `tests/test_comment_campaign_locator.py`
- Test: `tests/test_comment_campaign_receipts.py`
- Test: `tests/test_comment_campaign_executor.py`

**Interfaces:**
- Produces: `ProfileGateway.resolve(profile_ref) -> raw_id` and async `open_many(profile_refs) -> list[BrowserBinding]`, `close_many(raw_ids) -> dict[str, bool]`.
- Produces: `CommentExecutor.prepare_batch(campaign_id, assignment_ids) -> BatchResult`.
- Produces: `CommentExecutor.submit_assignment(campaign_id, assignment_id, approved_revision) -> dict`.
- Consumes existing: `RateLimitedAdsPowerAdapter`, `PlaywrightSessionFactory`, `tile_browser_bindings`, V2 element definitions, and existing content/input timing functions.

- [ ] **Step 1: Write failing lifecycle and no-submit-without-approval tests**

```python
async def test_prepare_batch_tiles_and_closes_before_next_batch(runtime):
    result = await runtime.executor.prepare_batch("campaign-1", ["a1", "a2", "a3"])
    assert result.prepared == ("a1", "a2", "a3")
    assert runtime.tiler.calls == [["profile-a", "profile-b", "profile-c"]]
    assert runtime.adspower.closed == ["profile-a", "profile-b", "profile-c"]
    assert all(runtime.store.get_assignment(item)["status"] == "awaiting_step_approval" for item in result.prepared)


async def test_submit_rejects_stale_or_missing_approval(runtime):
    with pytest.raises(CampaignConflictError, match="approval_revision_mismatch"):
        await runtime.executor.submit_assignment("campaign-1", "a1", approved_revision=1)
    assert runtime.page.submit_clicks == 0
```

- [ ] **Step 2: Run tests and verify failure**

Run: `pytest tests/test_comment_campaign_profile_gateway.py tests/test_comment_campaign_locator.py tests/test_comment_campaign_receipts.py tests/test_comment_campaign_executor.py -q`

Expected: FAIL because the executor modules are missing.

- [ ] **Step 3: Implement Profile resolution and bounded lifecycle**

Profile discovery calls `AdsPowerController.list_all_profiles()` and passes the raw rows directly to `CampaignStore.sync_profile_identities()`. `resolve(profile_ref)` reads the internal mapping with `CampaignStore.get_raw_profile_id()` and also confirms the raw ID still appears in current AdsPower discovery. Raw IDs exist only inside the store/ProfileGateway boundary and never enter HTTP or RQ arguments. `open_many` acquires Redis Profile leases before starting any browser, then starts/connects each Profile, calls `tile_browser_bindings(bindings)`, and returns bindings. Any partial failure closes every started Profile. `close_many` calls the existing adapter stop plus active check up to three times and returns per-Profile confirmation.

- [ ] **Step 4: Implement video/account/readiness checks**

Create exact async methods:

```python
verify_video(page, expected_video_id: str) -> None
verify_logged_in_username(page, expected_username: str) -> None
open_comment_panel(page, entry_definition: dict, timeout_ms: int) -> None
locate_comment_input(page, input_definition: dict) -> Locator
locate_submit_control(page, submit_definition: dict) -> Locator
```

The three element definitions come from Campaign settings and must reference active V2 elements of kinds `click`, `input`, and `click`. Resolve them with `StrictLocatorResolver`; do not hard-code a page-wide first button. `verify_video` checks normalized current URL and visible active-video link. `verify_logged_in_username` compares a configured account-menu element text/href to `expected_username`; mismatch raises `profile_identity_mismatch`.

Navigation and comment-panel readiness may reload up to three times before any submit click. Profile identity, video mismatch, parent mismatch, reply-target mismatch, and any post-click uncertainty are never handled by automatic submit retry.

- [ ] **Step 5: Implement prepare batch**

For each binding concurrently:

```text
transition opening_profile → locating_video
goto canonical_url and wait for domcontentloaded
verify video and login identity
open comment panel and wait for input readiness
for independent mode, locate top-level input
fill exact frozen resolved_text using existing input timing helper
verify DOM input text equals frozen text
save PNG evidence using UUID-only filename
transition to awaiting_step_approval with evidence metadata
```

After all bindings finish, close the complete batch. If any close is unconfirmed, mark Campaign `paused`, set `profile_close_failed`, and do not enqueue the next batch. Otherwise enqueue the next eligible batch.

- [ ] **Step 6: Implement one-time approved submit**

`approve_submit` stores `(assignment_id, revision, approved_at, consumed_at=None)`. Submission acquires Campaign, Profile, and `video_submit:<video_id>` leases; atomically consumes the matching approval; reopens the Profile; repeats video/account/comment-panel checks; refills the same frozen text; and checks the exact text again. Approval consumption and re-verification remain in `awaiting_step_approval`. If any evidence differs, invalidate the approval, increment the Assignment revision while remaining `awaiting_step_approval`, and do not click. Only after every gate passes, immediately before the click, compare-and-swap the Assignment to `submitting`.

Only after all checks pass and the compare-and-swap to `submitting` succeeds:

```python
await submit_locator.click()
store.update_assignment_status(
    assignment_id, expected_revision=current_revision,
    status="verifying_receipt",
)
```

The executor always closes and confirms the Profile in `finally`.

For `published_unverified`, expose only two manual resolutions. “标记已发布” requires a reason and creates an auditable manual verification event. “确认未发布并重新准备” moves the Assignment to `paused`; explicit resume runs preparation again and creates a new revision, so the old approval cannot submit it.

- [ ] **Step 7: Create and verify independent receipts**

Normalize text with Unicode NFKC, trim, collapse whitespace, and SHA-256 hash. Capture `video_id`, `profile_ref`, `expected_username`, `author_profile_href`, posting window, available stable node attributes, comment ID/permalink if visible, locator candidates, and screenshot. Reload the target URL and require exactly one matching visible node before `published_verified`; otherwise write `published_unverified` and never enqueue a retry.

- [ ] **Step 8: Run tests**

Run: `pytest tests/test_comment_campaign_profile_gateway.py tests/test_comment_campaign_locator.py tests/test_comment_campaign_receipts.py tests/test_comment_campaign_executor.py -q`

Expected: PASS, including partial start cleanup, wrong video, wrong username, stale approval, changed text, uncertain submit, receipt uniqueness, and close-blocked cases.

- [ ] **Step 9: Commit**

```bash
git add comment_campaign/profile_gateway.py comment_campaign/locator.py comment_campaign/receipts.py comment_campaign/executor.py comment_campaign/jobs.py comment_campaign/service.py tests/test_comment_campaign_profile_gateway.py tests/test_comment_campaign_locator.py tests/test_comment_campaign_receipts.py tests/test_comment_campaign_executor.py
git commit -m "feat(campaign): prepare and verify independent comments"
```

---

### Task 7: Threaded Parent Location, Reply Scoping, and Branch-Level Pause

**Files:**
- Modify: `comment_campaign/locator.py`
- Modify: `comment_campaign/receipts.py`
- Modify: `comment_campaign/executor.py`
- Modify: `comment_campaign/service.py`
- Test: `tests/test_comment_campaign_threaded.py`
- Test: `tests/fixtures/comment_campaign/threaded_comments.html`

**Interfaces:**
- Produces: `locate_parent_comment(page, receipt, limits) -> Locator`.
- Produces: `open_scoped_reply(parent_node, expected_author) -> Locator`.
- Produces: `pause_descendants(campaign_id, parent_assignment_id, error_code) -> list[str]`.

- [ ] **Step 1: Write failing exact-parent and branch-isolation tests**

```python
async def test_parent_locator_rejects_ambiguous_author_and_text(page, receipt):
    await page.set_content((FIXTURES / "threaded_comments.html").read_text(encoding="utf-8"))
    with pytest.raises(CommentLocatorError, match="parent_comment_ambiguous"):
        await locate_parent_comment(page, receipt.without_platform_id(), LocateLimits(20, 5))


def test_parent_failure_pauses_only_descendants(service):
    paused = service.pause_descendants("campaign-1", "root-a", "parent_comment_not_found")
    assert set(paused) == {"child-a1", "child-a2", "grandchild-a"}
    assert service.assignment("root-b")["status"] == "planned"
```

- [ ] **Step 2: Run tests and verify failure**

Run: `pytest tests/test_comment_campaign_threaded.py -q`

Expected: FAIL because scoped parent logic is absent.

- [ ] **Step 3: Implement composite parent matching in reliability order**

Try, in order:

```text
platform_comment_id or comment_permalink
author_profile_href + normalized_text_hash + video_id
expected_username + exact normalized text + posted_at window + parent_receipt_id
stable_attributes + receipt-scoped relative locator candidates
```

For each pass, search only comment container nodes, filter visible nodes, and require count exactly one. When count is zero, incrementally scroll the comment panel and wait for lazy loading, bounded by `LocateLimits(timeout_seconds=30, max_scrolls=12)`. After limits: zero is `parent_comment_not_found`; more than one is `parent_comment_ambiguous`.

- [ ] **Step 4: Implement reply action scoped to the matched node**

Within the unique parent node, find its reply control, click it, then verify the reply composer target contains the parent author identity. Never call `page.get_by_text("Reply").first`. A mismatch raises `reply_target_mismatch` before input.

- [ ] **Step 5: Enforce dependency scheduling and branch pause**

Only an Assignment whose parent Receipt is `published_verified` is eligible. Use a recursive CTE or an in-memory adjacency traversal loaded in one transaction to mark only descendants `paused_dependency`. Store the originating parent error in every affected Assignment. When the parent is manually restored to verified, `resume` moves only direct eligible children back to `waiting_dependency`; the normal scheduler reevaluates deeper descendants.

- [ ] **Step 6: Run threaded tests**

Run: `pytest tests/test_comment_campaign_threaded.py tests/test_comment_campaign_executor.py tests/test_comment_campaign_service.py -q`

Expected: PASS for a three-level chain, a two-branch tree, ID match, fallback match, not-found, ambiguous, author mismatch, and unrelated-branch continuation.

- [ ] **Step 7: Commit**

```bash
git add comment_campaign/locator.py comment_campaign/receipts.py comment_campaign/executor.py comment_campaign/service.py tests/test_comment_campaign_threaded.py tests/fixtures/comment_campaign/threaded_comments.html
git commit -m "feat(campaign): add verified threaded replies"
```

---

### Task 8: Concentrated Campaign Workbench UI

**Files:**
- Create: `gateway/templates/comment_campaign.html`
- Create: `gateway/static/comment_campaign.js`
- Create: `gateway/static/comment_campaign.css`
- Modify: `gateway/templates/_dashboard_sidebar.html`
- Modify: `gateway/app.py`
- Create: `tests-js/comment-campaign-ui.test.js`
- Modify: `tests/test_comment_campaign_integration.py`

**Interfaces:**
- Produces: page `GET /comment-campaigns`.
- Consumes: only `/api/browser-v2/comment-*` endpoints.
- Produces: `createCommentCampaignUI(dependencies)` for DOM-independent Node tests.

- [ ] **Step 1: Write failing shell and UI-state tests**

```javascript
test("draft inputs survive background polling", async () => {
  const {ui, document, timers} = harness();
  await ui.init();
  document.querySelector("#campaign-name").value = "Summer thread";
  document.querySelector("#campaign-name").dispatchEvent(new Event("input"));
  await timers.runNext();
  assert.equal(document.querySelector("#campaign-name").value, "Summer thread");
});

test("approval button sends exact assignment revision once", async () => {
  const {ui, requests} = harness({approval: {id: "a1", revision: 4}});
  await ui.approveSubmit("a1", 4);
  await ui.approveSubmit("a1", 4);
  assert.deepEqual(requests.filter(item => item.url.includes("approve-submit")), [
    {method: "POST", url: "/api/browser-v2/comment-campaigns/c1/assignments/a1/approve-submit", body: {expected_revision: 4}},
  ]);
});
```

- [ ] **Step 2: Run tests and verify failure**

Run: `node --test tests-js/comment-campaign-ui.test.js`

Expected: FAIL because the UI module is absent.

- [ ] **Step 3: Add page and sidebar route**

Create a dashboard-shell page and add one sidebar link:

```html
<a class="dashboard-nav-link" href="/comment-campaigns">评论 Campaign</a>
```

Add `GET /comment-campaigns` beside `/browser-v2`. Local-direct and legacy authentication behavior must match the existing V2 page.

- [ ] **Step 4: Implement the approved concentrated layout**

The page contains:

```text
Header: title, New Campaign, AdsPower/Redis/Worker status
Filters: all, awaiting approval, running, abnormal, completed
Main column: Campaign cards; threaded tree or independent rows
Right column: pending approval evidence and three actions
Drawer: allocation snapshot, attempts, receipts, screenshots
Creation drawer: mode → video → template → Profiles/allocation → approval lock
Template drawer: metadata plus editable step tree
Profile metadata drawer: username, tags, language, region, enabled/verified/health/cooldown
Settings drawer: V2 comment-entry, input, submit, and account-identity element bindings
```

Use text labels plus status color; never communicate status by color alone. Narrow screens collapse the right panel below the Campaign list.

- [ ] **Step 5: Implement stable client state and polling**

Keep server snapshots separate from drafts:

```javascript
const state = {
  campaigns: [], templates: [], profiles: [], health: {},
  selectedCampaignId: "", draftCampaign: null, draftTemplate: null,
  approvalInFlight: new Set(), pollTimer: null, error: "",
};
```

Polling updates only server snapshots and rendered status nodes. It must not recreate focused forms or overwrite draft values. Disable submit buttons while their exact request is in flight. Display backend fixed Chinese messages and the stable error code in details.

- [ ] **Step 6: Implement Campaign plan preview and role flexibility display**

Preview rows show video, step label, role, masked Profile, expected username, frozen text, and parent step. Add “重新随机”“单条换号”“锁定计划”. Include explanatory copy: “角色仅对本视频的当前 Campaign 生效；下一个视频会重新分配。”

- [ ] **Step 7: Implement approval evidence panel**

Show the four gates: login account, video ID, parent uniqueness when applicable, and exact input text. “查看现场” opens only the server-provided safe PNG path. “确认提交” sends `expected_revision`; “拒绝并暂停” requires a reason and pauses only that Assignment/branch. A `published_unverified` row shows “标记已发布” and “确认未发布并重新准备”; both require a reason and call `resolve-unverified` with the exact current revision.

- [ ] **Step 8: Run UI and integration tests**

Run: `node --test tests-js/comment-campaign-ui.test.js tests-js/dashboard-navigation.test.js`

Run: `pytest tests/test_comment_campaign_integration.py -q`

Expected: PASS, including draft preservation, duplicate-click protection, disconnected-service states, sidebar, direct mode, legacy auth, and no raw Profile IDs.

- [ ] **Step 9: Commit**

```bash
git add gateway/templates/comment_campaign.html gateway/static/comment_campaign.js gateway/static/comment_campaign.css gateway/templates/_dashboard_sidebar.html gateway/app.py tests-js/comment-campaign-ui.test.js tests/test_comment_campaign_integration.py
git commit -m "feat(campaign): add concentrated workbench"
```

---

### Task 9: Recovery, Security, Simulation, and Final Acceptance

**Files:**
- Modify: `comment_campaign/jobs.py`
- Modify: `comment_campaign/service.py`
- Modify: `comment_campaign/blueprint.py`
- Create: `tests/test_comment_campaign_recovery.py`
- Create: `tests/test_comment_campaign_security.py`
- Create: `tests/test_comment_campaign_acceptance.py`
- Create: `docs/superpowers/reports/2026-08-07-comment-campaign-verification.md`

**Interfaces:**
- Produces: `reconcile_campaign(campaign_id) -> dict`.
- Produces: health projection distinguishing AdsPower, Redis, Worker, and SQLite.
- Verifies every requirement in the approved design spec.

- [ ] **Step 1: Write failure-injection and 300-Profile acceptance tests**

```python
def test_300_profiles_run_in_batches_of_three_and_close_before_next(acceptance):
    result = acceptance.run_simulated(profile_count=300, batch_size=3)
    assert result.batch_count == 100
    for previous, following in zip(result.batches, result.batches[1:]):
        assert previous.all_close_confirmed_at <= following.first_start_at


def test_restart_never_replays_submitting_assignment(runtime):
    runtime.store.force_status("a1", "submitting")
    runtime.restart()
    assert runtime.store.get_assignment("a1")["status"] == "published_unverified"
    assert runtime.queue.submit_jobs_for("a1") == []
```

- [ ] **Step 2: Run acceptance tests and verify failure**

Run: `pytest tests/test_comment_campaign_recovery.py tests/test_comment_campaign_security.py tests/test_comment_campaign_acceptance.py -q`

Expected: FAIL until final reconciliation and hardening are connected.

- [ ] **Step 3: Implement reconciliation without submit replay**

`reconcile_campaign` reloads SQLite, releases only expired/owned leases, marks interrupted `submitting` rows `published_unverified`, recomputes dependency eligibility, and enqueues only `planned`, `waiting_dependency`, or explicitly resumed `paused` preparation work. It never enqueues submit work without a fresh unconsumed approval revision.

- [ ] **Step 4: Implement service health projection**

Return independent fields:

```json
{
  "sqlite": {"status": "connected"},
  "redis": {"status": "connected"},
  "worker": {"status": "connected"},
  "adspower": {"status": "unavailable", "message": "AdsPower 未连接"}
}
```

AdsPower failure must not make stored templates or Campaign history unreadable. It only disables planning refresh, preparation, and submission.

- [ ] **Step 5: Add fixed security scans**

Tests inspect all API payloads, Attempt summaries, RQ arguments, and rendered HTML for raw fixture Profile IDs, `ws://`, `wss://`, cookies, API keys, and Authorization values. Evidence serving accepts only `data/comment_campaign/evidence/<32-lowercase-hex>.png` and rejects traversal, alternate extensions, symlinks, and uppercase aliases.

- [ ] **Step 6: Run focused and full regression suites**

Run:

```bash
pytest tests/test_comment_campaign_*.py -q
pytest tests/test_execution_v2_*.py tests/test_adspower.py tests/test_launcher_restart.py tests/test_app.py -q
node --test tests-js/comment-campaign-ui.test.js tests-js/browser-v2-ui.test.js tests-js/dashboard-navigation.test.js
```

Expected: all PASS. No real TikTok comment is submitted by automated tests.

- [ ] **Step 7: Perform controlled local acceptance**

Use six authorized test Profiles for two batches and two additional authorized Profiles for element/login verification. First run independent mode with three prepared comments; then run one three-level chain and one two-branch threaded template. The user manually confirms each real submission. Record Profile-close ordering, role allocation, screenshots, Receipt verification, and every failure code. Stop immediately if account, video, parent, reply target, or receipt verification is uncertain.

- [ ] **Step 8: Write the verification report**

Record exact commands, pass/fail totals, simulated 300-Profile result, controlled Profile IDs only in masked form, Redis/Worker/AdsPower health, known limitations, and whether real submission acceptance was performed or intentionally skipped. Do not write secrets or raw IDs.

- [ ] **Step 9: Commit**

```bash
git add comment_campaign tests/test_comment_campaign_recovery.py tests/test_comment_campaign_security.py tests/test_comment_campaign_acceptance.py docs/superpowers/reports/2026-08-07-comment-campaign-verification.md
git commit -m "test(campaign): verify recovery and batch safety"
```

---

## Final Definition of Done

- Template and step editors support independent and threaded trees without cycles.
- Campaign planning freezes video, template revision, content, Profile metadata, roles, and seed.
- A Profile can change roles across videos; no persistent Profile role exists.
- 300 simulated Profiles execute as 100 batches of 3 with strict close-before-next ordering.
- Real submission is impossible without the exact current human approval revision.
- Independent failures are isolated; threaded parent failures pause descendants only.
- Parent location is unique and receipt-based; no page-wide first Reply selector is used.
- Uncertain submit or service restart cannot auto-repost.
- Workbench clearly distinguishes AdsPower, Redis, Worker, and business-state errors.
- Existing V2, dashboard, content library, launcher, and legacy routes pass regression tests.
- Verification report contains no secrets or raw AdsPower identifiers.
