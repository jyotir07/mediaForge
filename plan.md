# MediaForge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A distributed video-processing backend with an agentic editing harness. You upload a video, it gets probed, proxied and analyzed, an LLM proposes clips, Jev makes bounded decisions, a validated EDL is built, and FFmpeg deterministically exports the result. Durable job state, retries and idempotency run throughout.

**Architecture:** One FastAPI API process and N worker processes share a Docker image. PostgreSQL holds all durable state (media, jobs, attempts, artifacts, edit plans, exports). Redis holds queue pointers and progress pub/sub only. The Postgres job row is the lock: workers claim a job with an atomic `UPDATE … RETURNING` and hold a lease. That makes duplicate delivery, crashed workers and a wiped Redis all recoverable by a periodic sweeper. The LLM and Jev never produce commands. They produce typed data, which application policy maps to predefined FFmpeg operations.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2.0 (async) + asyncpg, Alembic, redis-py (asyncio), pydantic v2 / pydantic-settings, FFmpeg/ffprobe (system binaries), Anthropic SDK (LLM, behind an interface), `langchain-typesafe` (Jev, behind an interface), pytest + pytest-asyncio + httpx, ruff, mypy, Docker Compose. Frontend: one static HTML page + vanilla JS served by FastAPI (no build step).

**Spec:** `MediaForge_Project_Spec.md` (read it in full before starting any task; section numbers below refer to it).

---

## Global Constraints

Copied from the spec. Every task implicitly includes these.

- API requests must not perform long-running media work; HTTP creates a durable job, enqueues, and returns `job_id` (§6.1).
- PostgreSQL is the source of truth for job state; Redis is queue/ephemeral/fanout only. If Redis disappears, Postgres must still say what media, jobs, states, attempts, artifacts and exports exist (§6.2).
- Every processing step is retry-safe: check for the expected output before running FFmpeg; never create duplicate artifacts on retry (§6.3).
- Job states: `QUEUED`, `RUNNING`, `RETRYING`, `SUCCEEDED`, `FAILED` (cancellation is optional and **out of scope** for this plan) (§7).
- Stages: `UPLOAD`, `PROBE`, `PROXY`, `FRAME_EXTRACTION`, `ANALYSIS`, `EDIT_PLANNING`, `DECISION`, `EXPORT`, `COMPLETED` (§16).
- Artifact types: `SOURCE`, `PROXY`, `THUMBNAIL`, `FRAME`, `TRANSCRIPT`, `ANALYSIS`, `EDL`, `EXPORT`, `LOG` (§17).
- Idempotency key = `media_id + operation_type + configuration_hash`. Same op again: return the existing successful job/artifact. Equivalent job running: return the existing `job_id` (§19).
- Never execute user- or model-provided shell commands; subprocesses use argument arrays only, never `shell=True`; media paths validated and confined to the storage root (§21).
- No arbitrary URL fetching in the MVP (§21).
- Jev is used in at least three decision points (clip selection, edit validation, failure recovery); the rest of the app depends only on `app/decision/` interfaces, never on Jev directly (§11, §13).
- Jev/LLM output is never executed; it is mapped to predefined application profiles/operations (§12, §14, §28).
- Deterministic validation still enforces `start >= 0`, `end > start`, `end <= media.duration` regardless of Jev (§11.2).
- Do not store giant FFmpeg logs in Postgres: bounded error summary in the DB, full log as a `LOG` artifact (§15).
- Non-recoverable failures (corrupt source, unsupported container, invalid EDL, impossible range) go straight to `FAILED`; do not retry forever (§20).
- Jev/LLM failures must fail safely (§24).
- No auth, billing, multi-tenancy, timeline editor, Kubernetes, custom models (§4).
- Priority if time runs short: working pipeline > reliable job state > FFmpeg > Jev > AI analysis > UI (§33).

## Key Design Decisions (and what was rejected)

| Decision | Choice | Why / rejected alternative |
|---|---|---|
| Queue | Thin hand-rolled queue: Redis lists (`LPUSH` / `BRPOP` across queues) carrying only `job_id` | RQ/Celery/arq add a second source of truth for job state, which fights §6.2. Our queue is ~100 lines because Postgres does the hard part. |
| Exactly-once-ish execution | Atomic claim `UPDATE jobs SET status='RUNNING', lease_expires_at=… WHERE id=:id AND status IN ('QUEUED','RETRYING') RETURNING *` | Duplicate Redis deliveries become harmless no-ops. No Redis processing-list / ack protocol needed. |
| Crash recovery | Lease + heartbeat (every 10 s, lease 60 s) + sweeper loop in each worker (every 15 s) | Sweeper: expired-lease RUNNING → retry or FAIL; due RETRYING → enqueue; QUEUED older than 30 s → re-enqueue (covers Redis data loss). |
| Artifact idempotency | Deterministic output keys + write to `*.tmp` then `os.replace` | "File exists" then implies "file is complete", so the exists-check in §6.3 is trustworthy. |
| Object storage | Local filesystem volume behind a `Storage` class using relative `storage_key`s | MinIO/S3 adds infra for zero demo value; the interface keeps S3 swappable. |
| Progress transport | SSE (`GET /jobs/{id}/events`), snapshot from Postgres then Redis pub/sub | One-directional stream, so WebSockets buy nothing here. The snapshot-first approach means a missed pub/sub message never leaves the UI stuck. |
| Queues | `media.probe`, `media.proxy`, `media.analysis`, `media.edit`, `media.export` | Spec's `media.frames` folded into `media.analysis` (frame extraction is a stage of the analyze job) to avoid job chaining for no benefit. |
| Scene detection | FFmpeg `select='gt(scene,T)',showinfo` + parse `pts_time` | No new dependency (PySceneDetect rejected). |
| Audio signal | FFmpeg `silencedetect` → speech/audio-presence ratio per segment | Cheap and deterministic. Transcription (Whisper) is a stretch goal only (§23 Phase 5 "if practical"). |
| LLM | `LLMClient` protocol; `AnthropicLLM` impl using forced tool-use for JSON; model from env (default `claude-sonnet-5`); `FakeLLM` for tests | Provider-agnostic; key stays server-side; vision-capable for frames. |
| LLM output handling | Pydantic-validate; one repair retry with the validation error; then fail the job with `LLM_OUTPUT_INVALID` (non-recoverable) | Fails safely; never string-parses free text. |
| Jev | `langchain-typesafe` `TypeSafeClassifier` inside `app/decision/*`; each classifier has a `Fake*` and a deterministic fallback | Spec §13 boundary; §24 "fail safely". |
| Export encoding | Per-segment encode to `exports/{export_id}/seg_{i:03}.mp4` (idempotent per segment) then concat demuxer `-c copy` | A retry resumes from the first missing segment. That demonstrates §6.3 better than a single `filter_complex` pass. |
| Logging | stdlib `logging` with a small JSON formatter | No structlog dependency; §25 only needs structured events. |
| Frontend | `frontend/index.html` + `frontend/app.js`, mounted as static files | §3 says minimal; no Node toolchain. |
| Tests | Run inside the compose `api` container against real Postgres/Redis/FFmpeg; fixture videos generated with `ffmpeg -f lavfi` at session start | No binary fixtures in git; real dependencies per §24. |

## Review Focus

Inputs the spec implies but no task's happy path tests. The listed test is added to the owning task.

1. **Source with no audio stream** (screen recordings, many GIF→MP4s). Expected: probe stores `audio_codec = NULL`, proxy/export succeed with no audio track, and there's no `silencedetect` crash. Tested in Task 4 (parse), Task 11 (proxy), Task 19 (export).
2. **Short video / no scene cuts detected** (e.g. an 8 s single-shot clip). Expected: segmentation falls back to fixed windows, so there's always ≥1 candidate segment. Tested in Task 12.
3. **LLM returns clips outside the media duration, reversed, or overlapping each other.** Expected: the deterministic validator drops out-of-range/reversed clips before Jev, and the EDL builder merges/drops overlaps. Tested in Task 16 and Task 18.
4. **Every candidate rejected, or kept clips total far below target.** Expected: the edit job FAILs with `NO_CLIPS_SELECTED` (non-recoverable, clear message) instead of exporting an empty file. Tested in Task 18.
5. **Phone video with rotation metadata / portrait dimensions / NTSC fps (`30000/1001`).** Expected: probe reports display orientation (width/height after rotation) and fps ≈ 29.97, and proxy scaling preserves aspect ratio without upscaling. Tested in Task 4 and Task 11.

---

## File Structure

```text
mediaforge/                      (repo root)
├── app/
│   ├── main.py                  FastAPI app factory, router mounting, static frontend
│   ├── config.py                Settings (pydantic-settings, env-driven)
│   ├── db.py                    async engine/session factory
│   ├── logging.py               JSON log formatter + log_event()
│   ├── models/                  SQLAlchemy ORM: media.py, job.py, artifact.py, edit_plan.py, export.py
│   ├── schemas/                 Pydantic API + domain schemas: media.py, job.py, edl.py, analysis.py
│   ├── api/                     media.py, jobs.py, exports.py, health.py
│   ├── storage/local.py         Storage: key validation, atomic writes, path resolution
│   ├── jobs/
│   │   ├── states.py            JobStatus, Stage, transition rules, retry/backoff policy
│   │   ├── errors.py            ErrorCode enum + recoverability classification
│   │   └── service.py           create_or_get (idempotent), claim, heartbeat, progress, succeed, fail
│   ├── queue/redis_queue.py     enqueue / dequeue (BRPOP) / publish_progress / subscribe
│   ├── workers/
│   │   ├── runner.py            worker main loop, handler registry, heartbeat, sweeper
│   │   ├── probe.py  proxy.py  analysis.py  edit.py  export.py   (job handlers)
│   ├── media/
│   │   ├── ffprobe.py           run + parse metadata
│   │   ├── ffmpeg.py            run_ffmpeg(args) with progress parsing, bounded stderr, timeout
│   │   ├── profiles.py          predefined encoding profiles (the only source of encoder args)
│   │   ├── proxies.py  thumbnails.py  frames.py  scenes.py  audio.py  export.py  (pure arg builders + parsers)
│   ├── ai/
│   │   ├── llm.py               LLMClient protocol, AnthropicLLM, FakeLLM, structured-output helper
│   │   └── prompts.py           prompt templates
│   ├── decision/                (spec §13, the only place that imports langchain_typesafe)
│   │   ├── __init__.py          public types + factory get_decision_layer(settings)
│   │   ├── jev.py               thin TypeSafeClassifier wrapper
│   │   ├── clip_classifier.py  edit_validator.py  recovery_classifier.py  proxy_classifier.py
│   └── services/edl.py          EDL builder + deterministic validator
├── migrations/                  Alembic
├── frontend/index.html, app.js
├── tests/unit/  tests/integration/  tests/conftest.py  tests/fixtures/make_videos.py
├── Dockerfile  docker-compose.yml  requirements.txt  requirements-dev.txt  pyproject.toml (ruff/mypy/pytest config)
├── .env.example  README.md  CLAUDE.md
```

**Common commands** (all tasks; host is Windows, so everything runs in containers):

```bash
docker compose up -d --build
docker compose run --rm api pytest tests/unit -q
docker compose run --rm api pytest tests/integration -q
docker compose run --rm api ruff check app tests
docker compose run --rm api mypy app
```

---

## Phase 1: Foundation

### Task 1: Project skeleton, config, Docker, health

**Files:**
- Create: `requirements.txt`, `requirements-dev.txt`, `pyproject.toml`, `Dockerfile`, `docker-compose.yml`, `.env.example`, `.gitignore`, `app/main.py`, `app/config.py`, `app/db.py`, `app/logging.py`, `app/api/health.py`, `tests/conftest.py`, `tests/integration/test_health.py`

**Interfaces:**
- Produces: `Settings` (`database_url`, `redis_url`, `storage_root`, `max_upload_bytes=2_147_483_648`, `llm_model`, `anthropic_api_key`, `typesafe_api_key`, `decision_backend: Literal["jev","fake"]`, `llm_backend: Literal["anthropic","fake"]`), `get_settings()`, `get_session()` async dependency, `create_app() -> FastAPI`, `log_event(event: str, **fields)`.

- [ ] **Step 1:** Write `tests/integration/test_health.py`: `GET /healthz` returns `200 {"status":"ok","postgres":"ok","redis":"ok"}`. With Redis URL pointed at a dead port it returns `503` and `"redis":"error"`.
- [ ] **Step 2:** Run it and confirm it fails (no app).
- [ ] **Step 3:** Implement. Dockerfile: `python:3.12-slim` + `apt-get install -y ffmpeg`, non-root user, `/data/media` volume. Compose services: `postgres:16`, `redis:7`, `api` (`uvicorn app.main:create_app --factory`), `worker` (`python -m app.workers.runner`), with healthchecks and `depends_on: condition: service_healthy`. The health endpoint runs `SELECT 1` and `PING` with a 2 s timeout each.
- [ ] **Step 4:** `docker compose up -d --build`. The health test passes. `ruff` and `mypy` are clean.
- [ ] **Step 5:** Commit `feat: project skeleton with FastAPI, Postgres, Redis, Docker Compose`.

### Task 2: Database schema + migrations

**Files:**
- Create: `app/models/*.py`, `migrations/` (alembic init, async env), `migrations/versions/0001_initial.py`, `tests/integration/test_schema.py`

**Interfaces — Produces tables** (UUID PKs, `timestamptz` everywhere):

```text
media        id, original_filename, storage_key, size_bytes, duration_seconds, width, height, fps,
             video_codec, audio_codec, container, bitrate, rotation, probe_status, created_at
jobs         id, media_id FK, type, status, stage, progress (0..1), message, attempt, max_attempts,
             idempotency_key, config JSONB, result JSONB, error_code, error_message (≤2000 chars),
             lease_expires_at, next_attempt_at, worker_id,
             created_at, started_at, completed_at, updated_at
job_attempts id, job_id FK, attempt, started_at, ended_at, outcome, error_code, error_message, worker_id
artifacts    id, media_id FK, job_id FK NULL, type, storage_key UNIQUE, mime_type, size_bytes,
             metadata JSONB, created_at
edit_plans   id, media_id FK, job_id FK, request, candidate_data JSONB, decisions JSONB,
             accepted_operations JSONB, created_at
exports      id, edit_plan_id FK, media_id FK, job_id FK, artifact_id FK NULL, status,
             duration_seconds, width, height, video_codec, created_at
```

Constraints:
- `CREATE UNIQUE INDEX jobs_active_idem ON jobs(idempotency_key) WHERE status NOT IN ('FAILED')`. This lets a failed job be re-requested but never allows two live ones.
- Index `jobs(status, next_attempt_at)` and `jobs(status, lease_expires_at)` for the sweeper.
- `job_attempts` exists because §6.2 requires Postgres to answer "which attempts happened". A single `attempt` counter can't.

- [ ] **Step 1:** Test: after `alembic upgrade head`, inserting two jobs with the same `idempotency_key` in `QUEUED` raises `IntegrityError`. Inserting a second one when the first is `FAILED` succeeds. `alembic downgrade base` then `upgrade head` round-trips.
- [ ] **Step 2:** Run it and confirm failure.
- [ ] **Step 3:** Write the models + migration. The `api` container entrypoint runs `alembic upgrade head` before uvicorn.
- [ ] **Step 4:** Tests pass.
- [ ] **Step 5:** Commit `feat: initial schema for media, jobs, attempts, artifacts, edit plans, exports`.

---

## Phase 2: Media ingestion

### Task 3: Storage

**Files:** Create `app/storage/local.py`, `tests/unit/test_storage.py`

**Interfaces — Produces:**
```python
class Storage:
    def __init__(self, root: Path): ...
    def path(self, key: str) -> Path            # raises InvalidStorageKey on "..", absolute, backslash, NUL
    def exists(self, key: str) -> bool
    def tmp_path(self, key: str) -> Path         # sibling "<name>.tmp-<uuid>"
    def commit(self, tmp: Path, key: str) -> None  # os.replace, creates parent dirs
    async def write_stream(self, key: str, chunks: AsyncIterator[bytes], max_bytes: int) -> int  # raises TooLarge; tmp cleaned up
def media_key(media_id, *parts: str) -> str      # "media/{media_id}/..."
```

- [ ] Tests: key `../etc/passwd`, `/abs`, `a\\b`, `a\x00b` are rejected. A resolved path is always under root (check via `Path.resolve().is_relative_to`). A stream exceeding `max_bytes` raises `TooLarge` and leaves no file. `commit` is atomic (no final file exists before commit).
- [ ] Implement, run tests, commit `feat: local storage with key validation and atomic writes`.

### Task 4: ffprobe wrapper + metadata parsing

**Files:** Create `app/media/ffprobe.py`, `tests/unit/test_ffprobe_parse.py`, `tests/fixtures/ffprobe/*.json` (captured ffprobe outputs: h264+aac mp4, hevc mov with rotation −90, video-only mp4, NTSC 30000/1001, bitrate `N/A`)

**Interfaces — Produces:**
```python
@dataclass(frozen=True)
class MediaMetadata:
    duration_seconds: float; width: int; height: int; fps: float; rotation: int
    video_codec: str; audio_codec: str | None; container: str
    bitrate: int | None; size_bytes: int
def parse_ffprobe(raw: dict) -> MediaMetadata           # pure; raises ProbeError(code=NO_VIDEO_STREAM|CORRUPT_SOURCE)
async def probe(path: Path, timeout: float = 30) -> MediaMetadata
# args: ["ffprobe","-v","error","-print_format","json","-show_format","-show_streams", str(path)]
```

Rules: fps comes from `avg_frame_rate` (falling back to `r_frame_rate`), parsed as a fraction, and `0/0` gives an error. Rotation comes from `side_data_list[].rotation` or `tags.rotate`. If |rotation| is 90, swap width/height. `container` is the first entry of `format_name`. The `N/A` bitrate is stored as `None`.

- [ ] Tests (one per fixture): exact field values. Video-only gives `audio_codec is None` (Review Focus 1). Rotation −90 on 1920x1080 gives width 1080, height 1920. NTSC gives `fps == pytest.approx(29.97, abs=0.01)` (Review Focus 5). A JSON with no video stream raises `ProbeError(NO_VIDEO_STREAM)`.
- [ ] Implement, pass, commit `feat: ffprobe metadata extraction`.

### Task 5: Upload + media read endpoints

**Files:** Create `app/api/media.py`, `app/schemas/media.py`, `app/media/sniff.py`, `tests/unit/test_sniff.py`, `tests/integration/test_media_api.py`

**Interfaces — Produces:**
- `POST /media?filename=<name>` (raw request body = the video bytes) → `201 {"media_id", "probe_job_id"}`. The probe job is wired in Task 8; until then `probe_job_id` is `null`. *(Changed from multipart during implementation: FastAPI's `UploadFile` spools the whole body to disk before the handler runs, which defeats a streaming size limit.)*
- `GET /media/{id}` → media record + artifact list + latest job per type.
- `GET /media/{id}/metadata` → `MediaMetadata` fields, or `409 {"detail":"probe not complete"}`.
- `sniff_container(head: bytes) -> Literal["mp4","mov","matroska","webm","avi"] | None`, based on magic bytes (`ftyp` at offset 4 with brand, EBML `1A45DFA3`, `RIFF....AVI `).

Validation (§8 Stage 1): extension allowlist `{.mp4,.mov,.mkv,.webm,.avi,.m4v}`, sniffed container must be consistent with the extension, and size ≤ `max_upload_bytes` enforced while streaming (never trust `Content-Length`). The file streams to `media/{id}/source{ext}` in 1 MiB chunks. Then insert the `media` row and a `SOURCE` artifact in one transaction. If the DB insert fails, delete the file. The full ffprobe runs in the worker, not in the request (§6.1).

- [ ] Tests: a valid mp4 (generated fixture) gives 201, the file exists, and the row + SOURCE artifact exist. A `.mp4` whose bytes are a PNG gives `415`. An oversized upload (setting overridden to 1 KB) gives `413` with no leftover file. An unknown id gives `404`.
- [ ] Implement, pass, commit `feat: media upload with content sniffing and size limits`.

---

## Phase 3: Job system

### Task 6: State machine, error taxonomy, retry policy (pure)

**Files:** Create `app/jobs/states.py`, `app/jobs/errors.py`, `tests/unit/test_job_states.py`

**Interfaces — Produces:**
```python
class JobStatus(StrEnum): QUEUED, RUNNING, RETRYING, SUCCEEDED, FAILED
class Stage(StrEnum): UPLOAD, PROBE, PROXY, FRAME_EXTRACTION, ANALYSIS, EDIT_PLANNING, DECISION, EXPORT, COMPLETED
ALLOWED = {QUEUED:{RUNNING}, RUNNING:{SUCCEEDED,RETRYING,FAILED}, RETRYING:{RUNNING,FAILED}}
def assert_transition(src: JobStatus, dst: JobStatus) -> None   # raises IllegalTransition
def backoff_seconds(attempt: int, base: float = 5, cap: float = 300) -> float   # base*2**(attempt-1), capped, ±20% jitter via injected rng

class ErrorCode(StrEnum):
    # recoverable
    WORKER_LOST, STORAGE_ERROR, PROCESS_INTERRUPTED, TIMEOUT
    # recoverable with fallback (routed to Jev recovery classifier)
    ENCODER_FAILURE, RESOURCE_EXHAUSTED
    # non-recoverable
    CORRUPT_SOURCE, UNSUPPORTED_CONTAINER, NO_VIDEO_STREAM, INVALID_EDL, INVALID_RANGE,
    MISSING_SOURCE, LLM_OUTPUT_INVALID, NO_CLIPS_SELECTED, DECISION_UNAVAILABLE
class Recoverability(StrEnum): RETRY, FALLBACK, FATAL
def recoverability(code: ErrorCode) -> Recoverability
class JobError(Exception): code: ErrorCode; message: str; observation: dict  # observation feeds Jev
```

- [ ] Tests: every `ALLOWED` pair passes. `SUCCEEDED→RUNNING` and `FAILED→RUNNING` raise. Backoff is monotonic up to the cap with jitter disabled. Every `ErrorCode` maps to exactly one `Recoverability` (parametrized over the enum so a new code without a mapping fails the test).
- [ ] Implement, pass, commit `feat: job state machine, error taxonomy and retry policy`.

### Task 7: Job service (durable, idempotent, lease-based)

**Files:** Create `app/jobs/service.py`, `tests/integration/test_job_service.py`

**Interfaces:**
- Consumes: Task 2 models, Task 6 states/errors.
- Produces:
```python
def idempotency_key(media_id: UUID, job_type: str, config: dict) -> str
    # sha256(f"{media_id}:{job_type}:{canonical_json(config)}")  canonical = sort_keys, separators=(",",":")
async def create_or_get(s, media_id, job_type, config, max_attempts=3) -> tuple[Job, bool]  # (job, created)
    # INSERT ... ON CONFLICT (idempotency_key) WHERE status <> 'FAILED' DO NOTHING RETURNING *;
    # if nothing returned: SELECT the live job (race-safe; no read-then-write window)
async def claim(s, job_id, worker_id, lease_s=60) -> Job | None
    # UPDATE jobs SET status='RUNNING', attempt=attempt+1, lease_expires_at=now()+lease, worker_id=:w,
    #   started_at=COALESCE(started_at, now()), updated_at=now()
    # WHERE id=:id AND status IN ('QUEUED','RETRYING') AND (next_attempt_at IS NULL OR next_attempt_at<=now())
    # RETURNING *   ; also INSERT job_attempts row
async def heartbeat(s, job_id, worker_id, lease_s=60) -> bool     # False if lease lost (job taken over)
async def set_progress(s, job_id, stage: Stage, progress: float, message: str) -> None
async def succeed(s, job_id, worker_id, result: dict) -> None
async def fail(s, job_id, worker_id, err: JobError, retry_decision: RetryDecision) -> JobStatus
    # RETRY → RETRYING with next_attempt_at=now()+backoff (if attempt < max_attempts, else FAILED)
    # FATAL → FAILED. error_message truncated to 2000 chars. closes job_attempts row.
async def recover_expired_leases(s) -> list[UUID]   # RUNNING & lease expired → RETRYING or FAILED(WORKER_LOST)
async def due_jobs(s, stale_queued_after_s=30) -> list[tuple[UUID, str]]  # (id, queue) to (re)enqueue
```
All status writes go through a guarded `UPDATE … WHERE status = :expected AND worker_id = :worker`. A worker whose lease was taken over cannot overwrite the new owner's state.

- [ ] Tests (real Postgres):
  - Same key twice returns the same job with `created=False` (spec failure test 3/4).
  - 20 concurrent `create_or_get` calls via `asyncio.gather` produce exactly 1 row.
  - Two concurrent `claim`s: exactly one returns a Job.
  - A job with an expired lease is moved to RETRYING by `recover_expired_leases`, and its attempt row is closed with `WORKER_LOST`.
  - At `max_attempts` it becomes FAILED instead.
  - A stale worker's `succeed` after takeover is a no-op.
  - A FAILED job can be re-created with the same key.
- [ ] Implement, pass, commit `feat: durable job service with idempotent creation and leases`.

### Task 8: Redis queue, worker runner, sweeper, probe handler

**Files:** Create `app/queue/redis_queue.py`, `app/workers/runner.py`, `app/workers/probe.py`, `tests/integration/test_worker.py`. Modify `app/api/media.py` (enqueue probe).

**Interfaces:**
- Produces:
```python
QUEUES = {"probe":"media.probe","proxy":"media.proxy","analyze":"media.analysis","edit":"media.edit","export":"media.export"}
async def enqueue(redis, job_type: str, job_id: UUID) -> None          # LPUSH
async def dequeue(redis, timeout_s=5) -> tuple[str, UUID] | None       # BRPOP over all QUEUES
async def publish_progress(redis, job_id, payload: dict) -> None       # PUBLISH jobs:{id}
Handler = Callable[[JobContext], Awaitable[dict]]   # returns result dict
@dataclass class JobContext: job: Job; media: Media; storage: Storage; session_factory; redis; worker_id
    async def progress(self, stage: Stage, fraction: float, message: str) -> None  # DB + publish, throttled to ≥250ms
HANDLERS: dict[str, Handler]   # registered by each workers/*.py module
async def run_worker(settings, stop: asyncio.Event) -> None
```
- Loop:
  1. dequeue → `claim` (None ⇒ skip the duplicate/stale message).
  2. Run the handler under a heartbeat task. If the heartbeat returns False, cancel the handler.
  3. On `JobError`, apply the retry policy (Task 17 inserts the Jev recovery step here). On an unexpected `Exception`, treat it as `PROCESS_INTERRUPTED` (recoverable) with the traceback in the LOG artifact, never silently swallowed.
  4. Sweeper task every 15 s: `recover_expired_leases` + `due_jobs` → enqueue.
- SIGTERM sets `stop`. The worker finishes the current poll but does not wait for long jobs, since the lease expiry handles those.
- Probe handler: missing source file raises `JobError(MISSING_SOURCE)` (FATAL). Otherwise it runs `probe` and updates the media row. Idempotent: if `media.probe_status == 'done'`, it returns immediately.
- `POST /media` now calls `create_or_get(media_id, "probe", {})` + `enqueue`, and returns `probe_job_id`.

- [ ] Tests (real Postgres + Redis, worker run in-process as an asyncio task):
  - Upload → probe job SUCCEEDED within 10 s, and the metadata is populated.
  - The same job_id enqueued twice gives one attempt row (duplicate delivery).
  - **Worker dies mid-job** (spec failure test 1): a handler that sleeps is cancelled and the lease is forced into the past. The sweeper retries it and the second run succeeds, giving 2 attempt rows.
  - **Missing source** (test 6): FAILED with `MISSING_SOURCE`, attempt=1.
  - `FLUSHALL` on Redis after enqueue: the sweeper re-enqueues and the job still completes (proves §6.2).
- [ ] Implement, pass, commit `feat: redis queue, worker runner with leases and sweeper, probe job`.

### Task 9: Job read + SSE progress

**Files:** Create `app/api/jobs.py`, `app/schemas/job.py`, `tests/integration/test_job_events.py`

**Interfaces — Produces:**
- `GET /jobs/{id}` → `{"job_id","type","status","stage","progress","message","attempt","max_attempts","error_code","error_message","result"}` (the §16 shape).
- `GET /jobs/{id}/events` → `text/event-stream`:
  - It **subscribes first, then** sends a snapshot from Postgres, which avoids a gap between the two.
  - Each pub/sub message is sent as `event: progress`.
  - A 15 s `: keepalive` comment is sent.
  - The stream closes after a terminal status.
  - If the job is already terminal, it sends the snapshot and closes.

- [ ] Tests: GET on an unknown id gives 404. SSE on a SUCCEEDED job returns exactly one event and closes. SSE during a running fake handler that reports 0.25/0.5/1.0 receives non-decreasing progress and then a terminal event.
- [ ] Implement, pass, commit `feat: job status endpoint and SSE progress stream`.

---

## Phase 4: FFmpeg pipeline

### Task 10: FFmpeg runner + encoding profiles

**Files:** Create `app/media/ffmpeg.py`, `app/media/profiles.py`, `tests/unit/test_ffmpeg_runner.py`, `tests/unit/test_profiles.py`

**Interfaces — Produces:**
```python
@dataclass(frozen=True)
class EncodingProfile:
    name: Literal["PROXY_STANDARD","PROXY_HIGH","PROXY_LIGHT","EXPORT_DEFAULT","EXPORT_FALLBACK_CODEC","EXPORT_LOWER_RES"]
    max_height: int; video_args: tuple[str, ...]; audio_args: tuple[str, ...]
PROFILES: dict[str, EncodingProfile]
#  EXPORT_DEFAULT:        1080, libx264 -preset medium -crf 20 -pix_fmt yuv420p, aac 160k
#  EXPORT_FALLBACK_CODEC: 1080, libx264 -preset ultrafast -profile:v baseline -crf 23 -pix_fmt yuv420p
#  EXPORT_LOWER_RES:       720, libx264 -preset veryfast -crf 23
#  PROXY_STANDARD:        1080, libx264 -preset veryfast -crf 23 -g 30 (short GOP ⇒ fast seeking)
def scale_filter(max_height: int) -> str   # "scale=-2:'min({h},ih)'" : no upscaling, even width

@dataclass class FfmpegResult: args: list[str]; exit_code: int; duration_ms: int; stderr_tail: str; log_path: Path
async def run_ffmpeg(args: list[str], *, total_seconds: float | None, on_progress: Callable[[float], Awaitable[None]] | None,
                     log_path: Path, timeout_s: float) -> FfmpegResult
    # asyncio.create_subprocess_exec("ffmpeg", "-hide_banner", "-y", "-nostdin", "-progress", "pipe:1", *args)
    # parses out_time_us= lines → fraction; stderr streamed to log_path, last 4 KB kept in memory
    # non-zero exit → raises JobError(classify_ffmpeg_error(stderr_tail), observation={...})
    # timeout → kill process group, JobError(TIMEOUT)
def parse_progress_line(line: str, total_seconds: float) -> float | None
def classify_ffmpeg_error(stderr: str) -> ErrorCode
    # "Invalid data found when processing input" / "moov atom not found" → CORRUPT_SOURCE
    # "Error while opening encoder" / "Unknown encoder" / "Error initializing output stream" → ENCODER_FAILURE
    # "Cannot allocate memory" → RESOURCE_EXHAUSTED ; "No space left on device" → STORAGE_ERROR ; else PROCESS_INTERRUPTED
```
Profiles are the **only** place encoder args are defined. Decision outputs select a profile name, never args (§12).

- [ ] Tests:
  - `parse_progress_line("out_time_us=5000000", 10.0) == 0.5`, and the result is clamped to 1.0.
  - Classifier cases, one per branch.
  - `run_ffmpeg` with args that make ffmpeg exit non-zero (`-i /nonexistent`) raises `JobError`, with `stderr_tail` ≤ 4096 bytes and the log file written (spec failure test 2).
  - Grep-style test: no `shell=True` or `create_subprocess_shell` anywhere under `app/`.
  - `scale_filter(1080)` never upscales (checked on a 640x360 fixture output).
- [ ] Implement, pass, commit `feat: ffmpeg runner with progress parsing and predefined encoding profiles`.

### Task 11: Proxy + thumbnails job

**Files:** Create `app/media/proxies.py`, `app/media/thumbnails.py`, `app/workers/proxy.py`, `tests/unit/test_proxy_args.py`, `tests/integration/test_proxy_job.py`, `tests/fixtures/make_videos.py`. Modify `app/api/media.py`.

**Interfaces:**
- Produces:
  - `POST /media/{id}/proxy` → `202 {"job_id","status"}`. It returns the existing job when the idempotency key matches (the config hash includes the profile name).
  - `build_proxy_args(src, dst_tmp, profile, has_audio) -> list[str]`. With no audio it uses `-an`, otherwise the profile's audio args.
  - `build_thumbnail_args(src, dst_tmp, duration, count=9) -> list[str]` → a 3x3 contact sheet using `fps=count/duration,scale=320:-2,tile=3x3`, `-frames:v 1`.
- Handler stages PROXY (progress 0–0.9), then THUMBNAIL (0.9–1.0). Keys are `media/{id}/proxy/{profile}.mp4` and `media/{id}/thumbs/contact.jpg`. **Each output is skipped if `storage.exists(key)`**, with a quick `probe` to verify it. Artifact rows are upserted on `storage_key` (UNIQUE), so no duplicates. The source is opened read-only and never written.
- `tests/fixtures/make_videos.py` builds these with `ffmpeg -f lavfi` at session scope:
  - `standard.mp4`: 20 s, 1280x720, 3 distinct scenes (testsrc2 / smptebars / mandelbrot) + sine audio.
  - `noaudio.mp4`: 8 s, one scene, no audio.
  - `portrait.mp4`: 720x1280.
  - `corrupt.mp4`: truncated copy of standard.

- [ ] Tests:
  - Arg builder unit tests (no-audio gives `-an`).
  - The integration test produces a proxy whose probed height ≤ 1080 and whose codec is h264. It also produces a contact sheet.
  - Calling the endpoint twice returns the same job_id.
  - Rerunning the handler after success invokes ffmpeg 0 times (spy on `run_ffmpeg`) and adds no artifacts.
  - `noaudio.mp4` succeeds (Review Focus 1).
  - For `portrait.mp4`, output width < height (Review Focus 5).
  - `corrupt.mp4` gives FAILED `CORRUPT_SOURCE` after 1 attempt.
- [ ] Implement, pass, commit `feat: idempotent proxy and thumbnail generation`.

### Task 12: Scene segmentation + frame extraction

**Files:** Create `app/media/scenes.py`, `app/media/frames.py`, `app/media/audio.py`, `tests/unit/test_scenes.py`, `tests/unit/test_audio.py`

**Interfaces — Produces:**
```python
def build_scene_detect_args(src, threshold=0.3) -> list[str]  # -vf "select='gt(scene,0.3)',showinfo" -f null -
def parse_scene_times(stderr: str) -> list[float]              # from showinfo "pts_time:(\d+\.?\d*)"
@dataclass(frozen=True) class Segment: index: int; start: float; end: float
def build_segments(cuts: list[float], duration: float, min_len=2.0, max_len=15.0, window=8.0) -> list[Segment]
    # merge segments < min_len into neighbour, split > max_len; no cuts → fixed windows; always ≥1 segment
def build_frame_args(src, t: float, dst_tmp) -> list[str]      # -ss t -i src -frames:v 1 -vf scale=512:-2 -q:v 3
def build_silencedetect_args(src) -> list[str]                 # -af silencedetect=n=-35dB:d=0.5 -f null -
def parse_silences(stderr: str) -> list[tuple[float, float]]
def audio_presence(seg: Segment, silences) -> float            # 1 - silent_overlap/seg_len
```
Frames: 1 per segment at the midpoint, max 24 total (evenly subsampled) to bound LLM cost. Keys are `media/{id}/frames/{segment_index:03}.jpg`.

- [ ] Tests:
  - Parse fixture stderr snippets.
  - `build_segments([], 8.0)` gives one segment `[0, 8]` (Review Focus 2).
  - `build_segments([], 40.0)` gives 5 windows covering `[0, 40]` exactly.
  - A 0.5 s sliver merges into its neighbour.
  - Segments are contiguous, non-overlapping and end at `duration`.
  - `audio_presence` is 0.0 for a fully silent segment and 1.0 for no silences.
- [ ] Implement, pass, commit `feat: scene segmentation, frame extraction and audio presence signals`.

---

## Phase 5: AI analysis

### Task 13: LLM client (provider-agnostic, structured output)

**Files:** Create `app/ai/llm.py`, `app/ai/prompts.py`, `app/schemas/analysis.py`, `tests/unit/test_llm_structured.py`

**Interfaces — Produces:**
```python
class LLMClient(Protocol):
    async def generate[T: BaseModel](self, *, system: str, parts: Sequence[Part], schema: type[T]) -> T
    # raises LLMOutputInvalid on schema mismatch
async def structured(llm, *, system, parts, schema) -> T   # one repair round, then JobError(LLM_OUTPUT_INVALID)
class AnthropicLLM(LLMClient)   # beta.messages.parse(output_format=schema): SDK structured outputs;
                                # model claude-opus-5 (LLM_MODEL); fallbacks="default" (refusal fallback beta);
                                # images as base64 jpeg; SDK max_retries=2 then LLM_UNAVAILABLE (retryable);
                                # 4xx / refusal / no credentials → LLM_REQUEST_FAILED (fatal)
class FakeLLM(LLMClient)        # returns queued canned responses (dicts or raw strings) for tests
# (Changed during implementation from forced tool use: structured outputs are the documented way to get
#  schema-valid JSON; model default follows the claude-api guidance instead of claude-sonnet-5.)

class SceneDescription(BaseModel): segment_index: int; summary: str; relevance: float = Field(ge=0, le=1)
class SceneAnalysis(BaseModel): overall_summary: str; scenes: list[SceneDescription]
class ProposedClip(BaseModel): start: float; end: float; reason: str; source_segments: list[int]
class EditProposal(BaseModel): target_duration_seconds: float = Field(gt=0, le=600); clips: list[ProposedClip]
```
- [ ] Tests with `FakeLLM`:
  - Valid dict → parsed model.
  - The first response is malformed (missing field / string instead of number / relevance 1.7) and the second is valid → success, with 2 calls made.
  - Two malformed responses → `JobError(LLM_OUTPUT_INVALID)` (spec failure test 9).
  - The `AnthropicLLM` request builder never includes the API key in logged payloads.
- [ ] Implement, pass, commit `feat: provider-agnostic LLM client with validated structured output`.

### Task 14: Analyze job

**Files:** Create `app/workers/analysis.py`, `tests/integration/test_analysis_job.py`. Modify `app/api/media.py`.

**Interfaces:**
- Consumes: Tasks 10–13.
- Produces:
  - `POST /media/{id}/analyze` → `202 {"job_id"}`. Idempotent, and requires probe success (409 otherwise).
  - An `ANALYSIS` artifact `media/{id}/analysis/v1.json` shaped as:
```json
{"version":1,"media_id":"…","duration":20.0,"segments":[{"index":0,"start":0.0,"end":6.7,"frame_key":"…",
  "signals":{"audio_presence":0.98,"llm_relevance":0.7},"summary":"…"}],"overall_summary":"…"}
```
Stages: FRAME_EXTRACTION (scenes + frames + silencedetect) → ANALYSIS (one LLM call with all frames + segment timings). It runs on the proxy if present (faster), otherwise the source. Each sub-output is individually idempotent. If `v1.json` exists, the job returns immediately.

- [ ] Tests with FakeLLM: `standard.mp4` → ANALYSIS artifact with ≥2 segments, each with a frame and signals. `noaudio.mp4` → all `audio_presence == 0.0`, no crash. LLM malformed twice → job FAILED `LLM_OUTPUT_INVALID`, attempt=1 (FATAL, no retry loop).
- [ ] Implement, pass, commit `feat: analyze job producing structured segment observations`.

- **Stretch (only after Phase 7 works):** transcription → `TRANSCRIPT` artifact + transcript text per segment in the LLM prompt.

---

## Phase 6: Jev decision layer

### Task 15: Decision boundary + clip classifier

**Files:** Create `app/decision/__init__.py`, `app/decision/jev.py`, `app/decision/clip_classifier.py`, `tests/unit/test_clip_classifier.py`

- [ ] **Step 0 (spike, ≤30 min):** Install `langchain-typesafe` and confirm from the package source/docs:
  - the exact `Choice` and `Score` constructors (options/levels params);
  - the response accessors (verified from the LangChain post: `response.nouls[key].noul`; `Choice`/`Score` accessors are **unverified**);
  - whether an async `ainvoke` exists (if not, wrap `invoke` in `asyncio.to_thread`);
  - the error types.

  Record the findings as a short comment block at the top of `app/decision/jev.py`. If the API differs from the assumptions below, adapt `jev.py` only; nothing outside `app/decision/` changes.

**Interfaces — Produces** (the only public surface; nothing else imports `langchain_typesafe`):
```python
class ClipValue(StrEnum): HIGH_VALUE, MEDIUM_VALUE, LOW_VALUE
@dataclass(frozen=True) class ClipCandidate: start: float; end: float; reason: str; signals: dict[str, float]; goal: str
@dataclass(frozen=True) class ClipDecision: value: ClipValue; confidence: float | None; source: Literal["jev","fallback"]
class ClipClassifier(Protocol):
    async def classify(self, c: ClipCandidate) -> ClipDecision
class JevClipClassifier: ...   # Score question, levels low/medium/high over state=json(candidate)
class FakeClipClassifier: ...  # scripted outputs incl. garbage, for tests
class DecisionLayer: clip: ClipClassifier; edit: EditValidator; recovery: RecoveryClassifier
def get_decision_layer(settings) -> DecisionLayer
```
Mapping and fail-safe rules:
- The Jev response is mapped by an explicit dict.
- Any unknown label, missing key, exception or timeout (5 s) produces a **fallback**: a deterministic score `0.6*llm_relevance + 0.4*audio_presence` thresholded at ≥0.7 HIGH, ≥0.4 MEDIUM, else LOW, with `source="fallback"` and a `decision.fallback` log event.
- Confidence below 0.5 is treated as one level lower. This is conservative, because a low-confidence "high" should not crowd out a confident "medium".

- [ ] Tests:
  - Each Jev label maps correctly.
  - Unexpected label `"MAYBE"`, `None`, a raised exception and a timeout each produce the fallback with `source="fallback"` (spec failure test 8).
  - The low-confidence downgrade.
  - An import-boundary test fails if any module outside `app/decision/` imports `langchain_typesafe`.
- [ ] Implement, pass, commit `feat: decision boundary and Jev clip classifier with safe fallback`.

### Task 16: Edit action validator

**Files:** Create `app/decision/edit_validator.py`, `app/services/edl.py` (validator half), `tests/unit/test_edit_validator.py`

**Interfaces — Produces:**
```python
class EditVerdict(StrEnum): VALID, INVALID_DURATION, INVALID_RANGE, LOW_VALUE, DUPLICATE_CONTENT
def hard_validate(op: ProposedClip, media_duration: float, min_len=1.0, max_len=60.0) -> EditVerdict | None
    # deterministic: start>=0, end>start, end<=duration(+0.05 tolerance, then clamp), len bounds → INVALID_* ; None = passes
class EditValidator(Protocol):
    async def validate(self, op: ProposedClip, *, goal: str, accepted_so_far: list[ProposedClip], media_duration: float) -> EditVerdict
    # hard_validate first (Jev never overrides a hard failure); then Jev Choice over the 5 verdicts
    # fallback on Jev failure: VALID if hard checks pass and overlap with accepted_so_far < 50%, else DUPLICATE_CONTENT
```
- [ ] Tests:
  - `start=-1` → INVALID_RANGE and Jev not called.
  - `end < start` → INVALID_RANGE.
  - `end = duration + 30` → INVALID_RANGE (Review Focus 3).
  - A 0.3 s clip → INVALID_DURATION.
  - Jev says VALID for a hard-invalid op and the result is still INVALID.
  - Jev garbage → fallback.
  - 80% overlap with an accepted clip under fallback → DUPLICATE_CONTENT.
- [ ] Implement, pass, commit `feat: edit validator combining hard constraints with Jev verdicts`.

### Task 17: Recovery classifier wired into the worker

**Files:** Create `app/decision/recovery_classifier.py`, `tests/unit/test_recovery_classifier.py`. Modify `app/workers/runner.py`, `app/jobs/service.py` (`RetryDecision`).

**Interfaces — Produces:**
```python
class RecoveryStrategy(StrEnum): RETRY_SAME, RETRY_FALLBACK_CODEC, RETRY_LOWER_RESOLUTION, DEAD_LETTER
@dataclass(frozen=True) class FailureObservation: error: str; codec: str; resolution: str; duration: float; attempt: int; job_type: str; profile: str
class RecoveryClassifier(Protocol):
    async def classify(self, obs: FailureObservation) -> RecoveryStrategy
STRATEGY_TO_PROFILE = {RETRY_SAME: None, RETRY_FALLBACK_CODEC: "EXPORT_FALLBACK_CODEC", RETRY_LOWER_RESOLUTION: "EXPORT_LOWER_RES"}
@dataclass(frozen=True) class RetryDecision: retry: bool; profile_override: str | None; strategy: str | None
def decide_retry(err: JobError, job: Job, recovery: RecoveryClassifier) -> Awaitable[RetryDecision]
```
Policy in `runner.py`:
- FATAL → no retry, and Jev is not consulted.
- RETRY → RETRY_SAME with backoff.
- FALLBACK → consult Jev. The strategy maps through `STRATEGY_TO_PROFILE` (a predefined profile name), which is stored into `job.config["profile_override"]` for the next attempt.
- `attempt >= max_attempts` always means DEAD_LETTER → FAILED, whatever Jev says.
- On Jev failure, the fallback is a deterministic ladder: attempt 1 → FALLBACK_CODEC, attempt 2 → LOWER_RES, then DEAD_LETTER.
- The chosen strategy + source are recorded in `job_attempts.outcome`.

Because `profile_override` changes the config, the idempotency key is **not** recomputed. The key identifies the request, not the attempt.

- [ ] Tests: FATAL codes never call the classifier (spy). ENCODER_FAILURE + Jev RETRY_FALLBACK_CODEC → `profile_override == "EXPORT_FALLBACK_CODEC"`. Jev returns `"rm -rf /"` → fallback ladder (spec failure test 8). The last attempt → FAILED even if Jev says RETRY_SAME.
- [ ] Implement, pass, commit `feat: Jev-assisted failure recovery mapped to predefined profiles`.

- **Optional (only after Phase 7 works):** `proxy_classifier.py`: metadata → `PROXY_LIGHT|PROXY_STANDARD|PROXY_HIGH` → `PROFILES[...]`, with fallback `PROXY_STANDARD` (§12).

---

## Phase 7: EDL and export

### Task 18: Edit planning job → validated EDL

**Files:** Create `app/schemas/edl.py`, `app/workers/edit.py`, `tests/unit/test_edl_builder.py`, `tests/integration/test_edit_job.py`. Modify `app/services/edl.py`, `app/api/media.py`.

**Interfaces — Produces:**
```python
class TrimOp(BaseModel): type: Literal["trim"]; start: float = Field(ge=0); end: float
class EDL(BaseModel): version: Literal[1]; source_media_id: UUID; operations: list[TrimOp] = Field(min_length=1)
    # model_validator: end>start per op; ops sorted, non-overlapping
def build_edl(media_id, duration, kept: list[tuple[ProposedClip, ClipValue]], target: float) -> EDL
    # greedy: all HIGH (by value desc then relevance), then MEDIUM, until sum ≥ target (last clip trimmed to fit
    # if ≥ min_len remains); merge overlaps; emit in chronological order; raises JobError(NO_CLIPS_SELECTED) if empty
def validate_edl(edl: EDL, duration: float) -> None   # raises JobError(INVALID_EDL | INVALID_RANGE)
```
- `POST /media/{id}/edit` body `{"request": "Create a 45-second highlight reel…"}` (1–500 chars) → `202 {"job_id", "edit_plan_id": null}`. Requires a completed analysis (409 otherwise).
- The idempotency config is `{"request": normalized_request, "analysis_key": …}`, where `normalized_request` is whitespace-collapsed and lowercased.
- Handler stages:
  1. EDIT_PLANNING: the LLM produces an `EditProposal` from the analysis JSON + request.
  2. DECISION: for each clip, `edit.validate` then, if VALID, `clip.classify`.
  3. `build_edl` → `validate_edl`.
  4. Persist the `edit_plans` row (request, candidate_data, decisions incl. `source` jev/fallback, accepted_operations) and the `EDL` artifact `media/{id}/edl/{edit_plan_id}.json`.
  5. `create_or_get` the export job with config `{"edit_plan_id": …}`, insert the `exports` row, enqueue.
  6. The result is `{"edit_plan_id","export_id","export_job_id","clips_selected"}`.

- [ ] Tests:
  - builder: target 45 with clips of 10/10/10/10/10 HIGH → 45 s total, chronological.
  - Overlapping [5,15] + [10,20] → merged [5,20] (Review Focus 3).
  - All LOW → `NO_CLIPS_SELECTED` (Review Focus 4).
  - An EDL with end > duration → INVALID_RANGE (spec failure test 5).
  - Integration with FakeLLM + FakeDecision: a job produces an edit_plan, an EDL artifact, and a queued export job.
  - The same request twice (with differing whitespace) → same job_id.
- [ ] Implement, pass, commit `feat: edit planning job producing a validated EDL`.

### Task 19: Export job + export endpoints

**Files:** Create `app/media/export.py`, `app/workers/export.py`, `app/api/exports.py`, `tests/unit/test_export_args.py`, `tests/integration/test_export_job.py`

**Interfaces — Produces:**
```python
def build_segment_args(src, op: TrimOp, dst_tmp, profile: EncodingProfile, has_audio: bool) -> list[str]
    # ["-ss", f"{op.start:.3f}", "-i", src, "-t", f"{op.end-op.start:.3f}", "-vf", scale_filter(profile.max_height)+",fps=30",
    #  *profile.video_args, *(profile.audio_args + ["-ar","48000","-ac","2"] if has_audio else ["-an"]), "-movflags","+faststart", dst_tmp]
    # uniform fps/ar/ac across segments so concat -c copy is valid
def build_concat_args(list_file, dst_tmp) -> list[str]   # -f concat -safe 0 -i list.txt -c copy -movflags +faststart
```
- `GET /exports/{id}` → `{"export_id","status","job_id","duration_seconds","width","height","video_codec","download_url"}`.
- `GET /exports/{id}/file` → the video via `FileResponse`. **Browser playback needs HTTP Range support.** Verify that the installed Starlette's `FileResponse` handles `Range` (added in recent Starlette releases); if not, implement a small Range handler. The integration test below checks it.
- Handler stage EXPORT:
  - Profile = `job.config.get("profile_override") or "EXPORT_DEFAULT"`.
  - Segment keys are `exports/{export_id}/{profile}/seg_{i:03}.mp4`. The profile is in the path, so a fallback retry never reuses a segment encoded by a failed profile.
  - Existing segments are skipped. Progress = completed duration / total.
  - Then concat → `exports/{export_id}/final.mp4` → probe it → `EXPORT` artifact + update the `exports` row (duration, resolution, codec) → `SUCCEEDED`, stage COMPLETED.
  - `log_event("job.completed", job_id, type, duration_ms, attempt)` (§25).

- [ ] Tests:
  - Arg builder snapshot tests, including no-audio → `-an` (Review Focus 1).
  - Integration: EDL of 2 clips from `standard.mp4` → final export with duration ≈ the sum of clips (±0.2 s) and codec h264.
  - **Export fails once then succeeds** (spec failure test 7): monkeypatch `run_ffmpeg` to raise `ENCODER_FAILURE` on the 2nd segment of attempt 1. Attempt 2 uses a fallback profile, and the final artifact exists with 2 attempt rows.
  - **Resume:** kill after segment 1 of 3. On retry, segment 1 is not re-encoded (spy call count).
  - `GET /exports/{id}/file` with `Range: bytes=0-99` → `206` and 100 bytes.
- [ ] Implement, pass, commit `feat: idempotent segment-based export with recovery and playback endpoint`.

---

## Phase 8: UI, end-to-end, docs

### Task 20: Minimal UI

**Files:** Create `frontend/index.html`, `frontend/app.js`. Modify `app/main.py` (mount `/` static).

The UI is one page:
- an upload form (file input + progress via `XMLHttpRequest.upload.onprogress`);
- a media panel (metadata, contact sheet, "Generate proxy" / "Analyze" buttons);
- a segment list (start–end, summary, signals) once analysis exists;
- an edit request textarea + submit;
- a job progress list driven by `EventSource('/jobs/{id}/events')`, one row per stage matching the §26 demo;
- a clip list showing Jev decisions (value + source);
- a `<video controls>` for the export.

Plain CSS, no framework.

- [ ] Manual check via `docker compose up`: upload `standard.mp4` → proxy → analyze → edit → watch the export play. Record what was verified in the PR/commit message.
- [ ] Commit `feat: minimal UI for upload, progress, edit request and playback`.

### Task 21: End-to-end test, failure matrix, README

**Files:** Create `tests/integration/test_e2e.py`, `README.md`, `CLAUDE.md`. Modify `docker-compose.yml` if needed.

- [ ] `test_e2e.py` (FakeLLM + FakeDecision so it runs without keys): upload → probe → proxy → analyze → edit → export via HTTP only, polling `GET /jobs/{id}`. It asserts the final export is playable (probe succeeds, duration > 0).
- [ ] A `@pytest.mark.live` variant uses real Anthropic + Jev and is skipped unless both keys are set. Run it once manually with a real video (§26) and paste the resulting job timeline into the README.
- [ ] Confirm the §24 failure-test checklist maps to named tests (1: T8, 2: T10, 3–4: T7/T11, 5: T18, 6: T8, 7: T19, 8: T15/T17, 9: T13). Add any missing ones.
- [ ] README covers:
  - the architecture diagram;
  - "LLM reasons / Jev decides / workers execute";
  - the job state machine;
  - the idempotency + lease design;
  - how to run it and how to run tests;
  - the env vars;
  - known limitations;
  - **only measured numbers** (§31).
- [ ] Walk the §34 Definition of Done checklist and tick only what a test or the manual demo exercised.
- [ ] Commit `docs: README with architecture, operations and verified capabilities`.

---

## Self-Review (done against the spec)

- **Coverage:**
  - §3 media/processing/AI/API/frontend → T4–T5, T8–T9, T11–T12, T13–T14, T15–T19, T20.
  - §7 states → T6.
  - §11 three Jev points → T15, T16, T17 (§12 optional → after T17).
  - §15 ffmpeg ops: probe T4, thumbnails T11, frames T12, proxy T11, trim + concat + export T19.
  - §17 tables → T2.
  - §19 → T7.
  - §20 → T6/T17.
  - §21 → T3/T10.
  - §24 → mapped in T21.
  - §25 → `log_event` T1, used in T8/T19.
  - Cancellation, Prometheus/Grafana and transcription are deliberately excluded or stretch goals.
- **Deliberate deviations from the spec:**
  - Added the `job_attempts` and `exports` tables and the `jobs.lease_expires_at/next_attempt_at/config/result` columns. These are needed for §6.2's "which attempts happened", for `GET /exports/{id}` before the artifact exists, and for crash recovery.
  - Folded `media.frames` into `media.analysis`.
- **Granularity note:** This plan fixes every interface, rule and test case. It does not pre-write every implementation body: the Jev `Choice`/`Score` API is unverified until the Task 15 spike, and pre-writing ~3k lines would bake in guesses. Each task's steps follow TDD: write the listed tests, see them fail, implement, see them pass, commit.
