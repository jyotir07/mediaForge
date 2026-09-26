# MediaForge: Agentic Media Editing Pipeline

## 0. Project Status

**Status:** Weekend MVP / portfolio project  
**Primary goal:** Build a technically credible distributed video-processing backend that demonstrates direct experience with media pipelines, FFmpeg, asynchronous workers, job state, retries, proxy generation, exports, and agentic editing decisions.

**Target role:** Fullstack / Backend Engineer roles involving browser-based video editors, media infrastructure, AI editing agents, and long-running processing workflows.

**Primary implementation language:** Python

**Core stack:**
- Python
- FastAPI
- PostgreSQL
- Redis
- FFmpeg / ffprobe
- Docker / Docker Compose
- WebSockets or Server-Sent Events
- Jev for bounded classification / decision-making
- An LLM for open-ended media understanding and edit-plan generation

---

# 1. Product Definition

MediaForge is a small distributed media-processing and agentic editing system.

A user uploads a video and can request an edit such as:

> "Create a 45-second highlight reel focused on the most interesting technical points."

MediaForge:

1. Stores the source media.
2. Probes the media with `ffprobe`.
3. Extracts technical metadata.
4. Creates a low-resolution proxy.
5. Extracts representative frames.
6. Optionally transcribes the audio.
7. Detects candidate scenes / segments.
8. Uses an LLM to reason about the footage and propose candidate edits.
9. Uses **Jev to make bounded, typed decisions** over candidate clips and edit actions.
10. Converts accepted decisions into a deterministic Edit Decision List (EDL).
11. Executes the EDL with FFmpeg.
12. Tracks progress and durable job state.
13. Retries recoverable failures.
14. Produces a final export.

The important architectural principle is:

> **The LLM reasons. Jev makes bounded decisions. Deterministic workers execute.**

MediaForge must not rely on an LLM to directly execute arbitrary shell commands or mutate media files.

---

# 2. Why This Project Exists

The project is intentionally designed around the engineering problems present in modern browser-based video editors:

- large media uploads
- media metadata
- codecs and containers
- proxy generation
- long-running processing
- asynchronous jobs
- worker orchestration
- progress tracking
- retries and recovery
- durable state
- export pipelines
- AI-assisted footage understanding
- structured editing decisions

The portfolio value comes from demonstrating that the developer understands the infrastructure behind a video editor, not merely that they can build a UI around an AI API.

---

# 3. MVP Scope

## Required MVP

The first working version MUST support:

### Media

- Upload a video.
- Persist the original file.
- Generate a unique media ID.
- Run `ffprobe`.
- Persist:
  - duration
  - width
  - height
  - frame rate
  - video codec
  - audio codec
  - container format
  - bitrate where available
  - file size

### Processing

- Generate a proxy video.
- Generate thumbnails.
- Extract representative frames.
- Track processing jobs.
- Track job progress.
- Retry recoverable failures.
- Persist job state.

### AI editing

- Generate candidate segments.
- Generate a structured description of candidate segments.
- Allow an LLM to propose candidate edits.
- Use Jev to make bounded decisions about candidate edits.
- Produce an EDL.
- Execute the EDL with FFmpeg.
- Produce a final export.

### API

Minimum endpoints:

```text
POST   /media
GET    /media/{media_id}
GET    /media/{media_id}/metadata

POST   /media/{media_id}/proxy
POST   /media/{media_id}/analyze

POST   /media/{media_id}/edit
GET    /jobs/{job_id}
GET    /jobs/{job_id}/events

GET    /exports/{export_id}
```

### Frontend

A minimal UI is sufficient.

It should allow:

1. Uploading a video.
2. Seeing processing status.
3. Requesting an edit.
4. Watching progress.
5. Viewing the resulting export.

The frontend is NOT the main focus.

---

# 4. Explicit Non-Goals

Do NOT spend the weekend building:

- a full Premiere/CapCut replacement
- a sophisticated timeline editor
- collaborative editing
- user authentication
- billing
- multi-tenant production infrastructure
- a custom video codec
- a custom computer vision model
- training Jev
- a custom LLM
- complex cloud infrastructure
- Kubernetes unless the core system is already complete

A polished backend with a minimal UI is preferable to a beautiful UI with a shallow backend.

---

# 5. High-Level Architecture

```text
                         ┌─────────────────┐
                         │     Browser     │
                         │  Minimal UI     │
                         └────────┬────────┘
                                  │
                                  ▼
                         ┌─────────────────┐
                         │    FastAPI      │
                         │   API Server    │
                         └───────┬─────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              │                  │                  │
              ▼                  ▼                  ▼
        PostgreSQL             Redis          Object Storage
        durable state          queue/cache       media files
              │                  │                  │
              │                  ▼                  │
              │          ┌───────────────┐          │
              │          │ Media Workers │◄─────────┘
              │          └───────┬───────┘
              │                  │
              │                  ▼
              │             FFmpeg /
              │             ffprobe
              │                  │
              │                  ▼
              │            Media artifacts
              │
              ▼
        Job state / EDL
              │
              ▼
       ┌──────────────┐
       │ AI Editing   │
       │   Harness    │
       └──────┬───────┘
              │
       ┌──────┴────────┐
       │               │
       ▼               ▼
      LLM             Jev
   open-ended      bounded typed
    reasoning        decisions
       │               │
       └──────┬────────┘
              ▼
         Structured EDL
              │
              ▼
        Deterministic
        FFmpeg Worker
              │
              ▼
        Final Export
```

---

# 6. Architectural Principles

## 6.1 API requests must not perform long-running media work

Never execute a long FFmpeg process directly inside an HTTP request handler.

Bad:

```python
@app.post("/export")
def export():
    subprocess.run(...)
    return ...
```

Good:

```text
HTTP request
    ↓
create durable job
    ↓
enqueue job
    ↓
return job_id
    ↓
worker executes asynchronously
```

---

## 6.2 PostgreSQL is the source of truth for job state

Redis is used for:

- queues
- ephemeral state
- caching
- event fanout if useful

PostgreSQL owns durable state.

If Redis disappears, the database must still tell us:

- what media exists
- what jobs exist
- what state each job is in
- which attempts happened
- what artifacts were created
- what export was produced

---

## 6.3 Workers must be retry-safe

A worker can die after FFmpeg completes but before the database is updated.

Therefore every processing step needs an idempotency strategy.

Example:

```text
job_id = abc123
step = proxy_generation
output = media/abc123/proxy.mp4
```

Before generating the proxy:

```text
Does the expected output already exist?
    YES → verify / mark complete
    NO  → execute FFmpeg
```

Do not blindly create duplicate artifacts on retries.

---

# 7. Job State Machine

Every job must have an explicit state.

```text
QUEUED
  │
  ▼
RUNNING
  │
  ├──────────────► SUCCEEDED
  │
  ├──────────────► RETRYING
  │                    │
  │                    ▼
  │                 RUNNING
  │
  └──────────────► FAILED
```

Optional cancellation:

```text
QUEUED ─────► CANCELLED
RUNNING ────► CANCELLING ───► CANCELLED
```

## Required job fields

```text
id
media_id
type
status
progress
attempt
max_attempts
error_code
error_message
created_at
started_at
completed_at
updated_at
idempotency_key
```

---

# 8. Media Processing Pipeline

## Stage 1: Upload

Input:

```text
video file
```

Output:

```text
media_id
source artifact
```

Validate:

- file exists
- supported container
- reasonable file size
- MIME type / extension consistency where possible

Do not trust the file extension alone.

---

## Stage 2: Probe

Run:

```bash
ffprobe
```

Extract structured metadata.

Example:

```json
{
  "duration": 123.4,
  "width": 3840,
  "height": 2160,
  "fps": 60,
  "video_codec": "hevc",
  "audio_codec": "aac",
  "container": "mov"
}
```

Store this in PostgreSQL.

---

## Stage 3: Proxy generation

Generate an editing-friendly proxy.

Example target:

```text
1080p
H.264
reasonable bitrate
fast decoding
```

The exact encoding parameters can be tuned during implementation.

The proxy is for interactive editing / analysis.

The original source must remain untouched.

---

## Stage 4: Thumbnail generation

Generate:

- contact sheet or representative thumbnails
- scene thumbnails where possible

Store artifact references.

---

## Stage 5: Frame extraction

Extract representative frames from candidate scenes.

These frames can be passed to the vision-capable model for semantic understanding.

Do not send an entire long video directly to an LLM.

The pipeline should first reduce the video into structured observations.

---

# 9. AI Editing Harness

The AI layer is divided into two responsibilities.

## 9.1 LLM: open-ended reasoning

The LLM may:

- summarize footage
- describe scenes
- understand transcripts
- identify candidate highlights
- propose edit operations
- explain why a clip may be useful
- produce structured candidate edit plans

The LLM does NOT directly execute commands.

Example output:

```json
{
  "candidate_clips": [
    {
      "start": 12.4,
      "end": 24.8,
      "reason": "Contains the clearest explanation of the architecture."
    },
    {
      "start": 43.2,
      "end": 58.1,
      "reason": "Contains the strongest demonstration."
    }
  ]
}
```

---

# 10. Jev: Exact Responsibility

## IMPORTANT

Jev is NOT the media processor.

Jev is NOT the FFmpeg executor.

Jev is NOT a replacement for the LLM.

Jev is the **bounded decision/classification layer** inside the editing harness.

The architecture should be:

```text
LLM
 │
 │ proposes candidates
 ▼
Candidate actions
 │
 ▼
Jev
 │
 │ makes typed/bounded decisions
 ▼
Validated decisions
 │
 ▼
Deterministic executor
 │
 ▼
FFmpeg
```

This separation must be maintained.

---

# 11. Where Jev MUST Be Used

The MVP should use Jev in at least **three concrete decision points**.

## 11.1 Candidate clip selection

Input to Jev:

```json
{
  "clip": {
    "start": 42.3,
    "end": 51.8,
    "duration": 9.5
  },
  "signals": {
    "transcript_relevance": 0.91,
    "visual_quality": 0.84,
    "audio_quality": 0.93,
    "motion_score": 0.72,
    "face_visibility": 0.96
  },
  "request": {
    "goal": "technical highlight reel"
  }
}
```

Jev should produce a constrained result such as:

```text
KEEP
REJECT
```

or a small typed classification:

```text
HIGH_VALUE
MEDIUM_VALUE
LOW_VALUE
```

The application then applies deterministic rules to convert that decision into inclusion/exclusion.

Do NOT ask Jev to write an arbitrary explanation that is later parsed with string matching.

---

## 11.2 Edit action validation

The LLM may propose:

```json
{
  "operation": "trim",
  "start": 12.4,
  "end": 42.8
}
```

Jev can classify whether the proposed operation is valid under the current editing request.

Example:

```text
VALID
INVALID_DURATION
INVALID_RANGE
LOW_VALUE
DUPLICATE_CONTENT
```

The deterministic validator should still enforce hard constraints such as:

```text
start >= 0
end > start
end <= media.duration
```

Jev is a decision layer, not a substitute for ordinary validation.

---

## 11.3 Export failure recovery

When a media worker fails, create a structured failure observation.

Example:

```json
{
  "error": "encoder_failure",
  "codec": "hevc",
  "resolution": "3840x2160",
  "duration": 1050,
  "attempt": 1
}
```

Jev may classify the recovery strategy:

```text
RETRY_SAME
RETRY_FALLBACK_CODEC
RETRY_LOWER_RESOLUTION
DEAD_LETTER
```

The worker then maps the decision to a predefined deterministic action.

Jev must never be allowed to invent arbitrary shell commands or FFmpeg arguments.

---

# 12. Optional Jev Extension

If the basic three integrations are working, add adaptive proxy selection.

Example input:

```json
{
  "width": 3840,
  "height": 2160,
  "fps": 60,
  "codec": "hevc",
  "file_size_mb": 2500,
  "duration_seconds": 2700
}
```

Possible decision:

```text
HIGH_PROXY
```

The application maps this to a predefined encoding profile.

Again:

```text
Jev decision
    ↓
known application profile
    ↓
FFmpeg command
```

Never:

```text
Jev-generated shell command
    ↓
subprocess
```

---

# 13. Jev Integration Boundary

Create a dedicated module:

```text
app/
  decision/
    __init__.py
    clip_classifier.py
    edit_validator.py
    recovery_classifier.py
    proxy_classifier.py
```

The rest of the application should not depend directly on Jev.

Example conceptual interface:

```python
class ClipDecision:
    decision: Literal["KEEP", "REJECT"]
    confidence: float | None


class ClipClassifier:
    async def classify(self, candidate: ClipCandidate) -> ClipDecision:
        ...
```

The implementation may use Jev internally.

This makes it possible to replace Jev without rewriting the media pipeline.

---

# 14. Deterministic Execution Layer

The executor receives validated structured operations.

Example EDL:

```json
{
  "version": 1,
  "source_media_id": "media_123",
  "operations": [
    {
      "type": "trim",
      "start": 12.4,
      "end": 24.8
    },
    {
      "type": "trim",
      "start": 43.2,
      "end": 58.1
    }
  ]
}
```

The executor:

1. Validates every operation.
2. Converts operations into known FFmpeg operations.
3. Generates the command.
4. Executes FFmpeg.
5. Captures stdout/stderr.
6. Parses progress.
7. Updates the job.
8. Stores the resulting artifact.
9. Marks the job successful.

No LLM or Jev output should be executed directly.

---

# 15. FFmpeg Requirements

The project should demonstrate actual FFmpeg usage rather than only calling an abstraction library.

Minimum operations:

```text
ffprobe
thumbnail extraction
frame extraction
proxy generation
clip trimming
concatenation
final export
```

Capture:

- command
- exit code
- stderr
- processing duration
- output size

Do not store giant FFmpeg logs directly in PostgreSQL.

Store a bounded error summary and keep full logs as an artifact if needed.

---

# 16. Progress Tracking

Long-running jobs must expose progress.

Example:

```json
{
  "job_id": "job_123",
  "status": "RUNNING",
  "stage": "EXPORT",
  "progress": 0.67,
  "message": "Encoding final export"
}
```

Stages:

```text
UPLOAD
PROBE
PROXY
FRAME_EXTRACTION
ANALYSIS
EDIT_PLANNING
DECISION
EXPORT
COMPLETED
```

The frontend should receive updates using WebSockets or SSE.

---

# 17. Database Model

Minimum tables:

## media

```text
id
original_filename
storage_key
size_bytes
duration_seconds
width
height
fps
video_codec
audio_codec
container
created_at
```

## jobs

```text
id
media_id
type
status
stage
progress
attempt
max_attempts
idempotency_key
error_code
error_message
created_at
started_at
completed_at
updated_at
```

## artifacts

```text
id
media_id
job_id
type
storage_key
mime_type
size_bytes
created_at
```

Artifact types:

```text
SOURCE
PROXY
THUMBNAIL
FRAME
TRANSCRIPT
ANALYSIS
EDL
EXPORT
LOG
```

## edit_plans

```text
id
media_id
request
candidate_data
accepted_operations
created_at
```

---

# 18. Queue Design

Use Redis as the initial queue.

Suggested queues:

```text
media.probe
media.proxy
media.frames
media.analysis
media.edit
media.export
```

Do not build a complex distributed scheduler for the MVP.

The important thing is demonstrating:

```text
HTTP
 ↓
durable job
 ↓
queue
 ↓
worker
 ↓
state updates
 ↓
artifact
```

---

# 19. Idempotency

Every externally-triggered processing operation should have an idempotency key where appropriate.

Example:

```text
media_id + operation_type + configuration_hash
```

If the same operation is requested twice:

```text
existing successful job
       ↓
return existing artifact
```

If an equivalent job is currently running:

```text
existing running job
       ↓
return existing job_id
```

This prevents duplicate exports and unnecessary FFmpeg work.

---

# 20. Failure Handling

The system must distinguish:

## Recoverable

Examples:

- temporary worker failure
- temporary storage failure
- transient process interruption

Action:

```text
retry with backoff
```

## Recoverable with fallback

Examples:

- encoder failure
- unsupported hardware/codec path
- resource-heavy export

Action:

```text
Jev decision
    ↓
known fallback profile
```

## Non-recoverable

Examples:

- corrupt source
- unsupported container
- invalid EDL
- impossible timestamp range

Action:

```text
FAILED
```

Do not retry forever.

---

# 21. Security

At minimum:

- Never execute user-provided shell commands.
- Never concatenate arbitrary user input into shell strings.
- Use argument arrays when invoking subprocesses.
- Validate media paths.
- Keep media inside controlled storage directories.
- Restrict FFmpeg operations to predefined operations.
- Validate URLs if remote media support is ever added.
- Do not implement arbitrary URL fetching in the MVP.

If URL ingestion is added later, explicitly threat-model SSRF.

---

# 22. Suggested Repository Structure

```text
mediaforge/
├── app/
│   ├── api/
│   │   ├── media.py
│   │   ├── jobs.py
│   │   └── exports.py
│   │
│   ├── decision/
│   │   ├── clip_classifier.py
│   │   ├── edit_validator.py
│   │   ├── recovery_classifier.py
│   │   └── proxy_classifier.py
│   │
│   ├── media/
│   │   ├── ffmpeg.py
│   │   ├── ffprobe.py
│   │   ├── thumbnails.py
│   │   ├── proxies.py
│   │   └── frames.py
│   │
│   ├── workers/
│   │   ├── probe.py
│   │   ├── proxy.py
│   │   ├── analysis.py
│   │   ├── edit.py
│   │   └── export.py
│   │
│   ├── models/
│   ├── schemas/
│   ├── queue/
│   ├── storage/
│   ├── services/
│   ├── config.py
│   └── main.py
│
├── frontend/
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
│
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── README.md
└── CLAUDE.md
```

---

# 23. Implementation Order

Claude Code should implement in this order.

## Phase 1: Foundation

- Initialize Python project.
- FastAPI.
- PostgreSQL.
- Redis.
- Docker Compose.
- Configuration management.
- Database migrations.
- Health endpoints.

**Definition of done:**

```text
docker compose up
```

starts the API, PostgreSQL and Redis successfully.

---

## Phase 2: Media ingestion

Implement:

```text
POST /media
GET /media/{id}
```

Support local file upload.

Persist:

- source file
- media metadata
- media record

Add ffprobe integration.

---

## Phase 3: Job system

Implement:

- job model
- Redis queue
- worker process
- job states
- retries
- idempotency

Test worker crash/retry behavior.

---

## Phase 4: FFmpeg pipeline

Implement:

- thumbnails
- proxy generation
- frame extraction
- progress parsing
- artifact storage

---

## Phase 5: AI analysis

Implement:

- transcript generation if practical
- scene segmentation
- representative frame extraction
- structured scene descriptions
- candidate clip generation

Do not spend excessive time on model selection.

Use an existing API.

---

## Phase 6: Jev integration

Implement the three required decision points:

### A. Clip selection

```text
candidate → Jev → KEEP / REJECT
```

### B. Edit validation

```text
proposed operation → Jev → VALID / INVALID_*
```

### C. Failure recovery

```text
failure observation → Jev → predefined recovery strategy
```

Only after these work should optional proxy classification be added.

---

## Phase 7: EDL and export

Implement:

```text
candidate clips
    ↓
Jev decisions
    ↓
accepted clips
    ↓
EDL
    ↓
FFmpeg
    ↓
export
```

---

## Phase 8: Minimal UI

Implement:

- upload
- processing status
- timeline/clip list
- edit request input
- export status
- video playback

Keep styling simple.

---

# 24. Testing Strategy

## Unit tests

Test:

- metadata parsing
- timestamp validation
- EDL validation
- job state transitions
- retry policy
- idempotency
- Jev response mapping
- recovery mapping
- FFmpeg command generation

## Integration tests

Use real:

- PostgreSQL
- Redis
- FFmpeg

Test:

```text
upload
→ probe
→ proxy
→ analysis
→ edit
→ export
```

## Failure tests

Explicitly test:

1. Worker dies during processing.
2. FFmpeg exits non-zero.
3. Same job submitted twice.
4. Same idempotency key submitted twice.
5. Invalid timestamp.
6. Missing source artifact.
7. Export fails once and succeeds on retry.
8. Jev returns an invalid/unexpected decision.
9. LLM returns malformed structured output.

Jev/LLM failures must fail safely.

---

# 25. Observability

Log structured events.

Example:

```json
{
  "event": "job.completed",
  "job_id": "job_123",
  "type": "export",
  "duration_ms": 184230,
  "attempt": 1
}
```

Useful metrics:

```text
job duration
queue wait time
worker processing time
retry count
failure rate
proxy generation time
export time
media size
```

Optional:

- Prometheus
- Grafana

Do not add these unless the core project is already working.

---

# 26. Demo Scenario

The final demo should use a real video.

User uploads:

```text
demo.mp4
```

System shows:

```text
Uploading
✓

Probing
✓

Generating proxy
✓

Extracting scenes
✓

Analyzing footage
✓

Generating candidate edits
✓

Jev decision pass
✓ 7 clips selected

Generating EDL
✓

Exporting
████████████████░░░ 82%
```

Then:

```text
Final export ready.
Duration: 00:44
Resolution: 1920x1080
Codec: H.264
```

The user can play the result.

---

# 27. What Makes the Project Technically Interesting

The project should demonstrate these concepts clearly:

### Distributed systems

- queues
- workers
- durable state
- retries
- idempotency
- failure recovery

### Media engineering

- FFmpeg
- ffprobe
- codecs
- containers
- proxies
- thumbnails
- frame extraction
- export

### AI systems

- multimodal analysis
- structured model outputs
- agentic planning
- bounded decisions
- deterministic execution

### Modern agent architecture

```text
LLM
 ↓
reasoning
 ↓
structured candidate actions
 ↓
Jev
 ↓
bounded decisions
 ↓
deterministic executor
```

---

# 28. Critical Design Rule

**Never make the architecture look like this:**

```text
User
 ↓
LLM
 ↓
"run this FFmpeg command"
 ↓
subprocess
```

That is unsafe, difficult to test, and architecturally weak.

Use:

```text
User
 ↓
LLM
 ↓
structured edit proposal
 ↓
schema validation
 ↓
Jev classification / decision
 ↓
application policy
 ↓
known operation
 ↓
FFmpeg executor
```

This separation is one of the most important parts of the project.

---

# 29. Portfolio Positioning

Suggested project title:

> **MediaForge — Agentic Video Processing & Editing Harness**

Suggested one-line description:

> Distributed video processing pipeline combining FFmpeg workers, durable job orchestration, multimodal footage analysis, and Jev-powered bounded editing decisions.

Do not describe the project as a "full AI video editor."

It is more accurate and technically stronger to describe it as an **agentic media processing and editing harness**.

---

# 30. Cardboard-Relevance Mapping

| Cardboard requirement | MediaForge evidence |
|---|---|
| Exports | FFmpeg export workers |
| Proxies | Dedicated proxy pipeline |
| Media preprocessing | ffprobe, thumbnails, frames |
| Analysis | scene/frame/transcript analysis |
| Agent-powered editing | LLM + Jev editing harness |
| APIs | FastAPI |
| State contracts | PostgreSQL job/media/artifact models |
| Workers | Redis-backed workers |
| Long-running workflows | asynchronous jobs |
| Progress | WebSockets/SSE |
| Recovery | retry/fallback system |
| Files | source/proxy/artifact storage |
| Database | PostgreSQL |
| Queues | Redis |
| FFmpeg | core media execution layer |
| Multiple languages | Python now, architecture can later support Go/Rust workers |

---

# 31. Resume-Level Claims

Only use claims that are actually measured after implementation.

Potential final bullets:

> **MediaForge — Agentic Video Processing & Editing Harness | Python, FastAPI, FFmpeg, Redis, PostgreSQL, Docker, Jev**

> • Built an asynchronous video-processing pipeline for media ingestion, ffprobe metadata extraction, proxy generation, frame analysis and deterministic FFmpeg exports using Redis-backed workers and durable PostgreSQL job state.

> • Designed retry-safe, idempotent media workflows with persistent job states, progress events and artifact tracking so long-running processing can recover from worker and encoder failures.

> • Built an agentic editing harness where an LLM proposes candidate edits, Jev performs bounded typed decisions over clip selection and recovery strategies, and deterministic workers execute validated edit plans through FFmpeg.

Do NOT put these on the resume until the implementation actually supports them.

---

# 32. Interview Story

The project should support this explanation:

> "I wanted to understand the infrastructure behind browser-based video editors, so I built a small distributed media pipeline rather than another frontend editor. The system separates media reasoning from execution: the model proposes candidate edits, Jev handles bounded decisions, and deterministic workers execute validated operations through FFmpeg. PostgreSQL keeps durable job state while Redis handles asynchronous work. I focused heavily on retries, idempotency and recovery because video processing is inherently long-running and failure-prone."

This is the core technical story.

---

# 33. Weekend Execution Constraint

The project must prioritize:

```text
WORKING PIPELINE
>
RELIABLE JOB STATE
>
FFMPEG
>
JEV INTEGRATION
>
AI ANALYSIS
>
UI POLISH
```

If time runs out, cut:

- advanced UI
- authentication
- deployment
- observability dashboards
- advanced scene detection

Do NOT cut:

- FFmpeg
- workers
- job state
- retries
- idempotency
- Jev integration
- deterministic execution

---

# 34. Definition of Done

The MVP is complete when all of the following are true:

- [ ] Video can be uploaded.
- [ ] Source media is persisted.
- [ ] ffprobe metadata is extracted.
- [ ] Proxy is generated.
- [ ] Thumbnails are generated.
- [ ] Frames can be extracted.
- [ ] Jobs run asynchronously.
- [ ] Job state persists in PostgreSQL.
- [ ] Redis queue is working.
- [ ] Worker retries work.
- [ ] Duplicate processing is prevented.
- [ ] Processing progress is observable.
- [ ] Candidate clips can be generated.
- [ ] LLM can produce structured edit candidates.
- [ ] Jev makes clip-selection decisions.
- [ ] Jev validates proposed edit operations.
- [ ] Jev participates in at least one recovery decision.
- [ ] EDL is generated.
- [ ] EDL is validated.
- [ ] FFmpeg executes the validated EDL.
- [ ] Final video is exported.
- [ ] Jev/LLM cannot directly execute shell commands.
- [ ] At least one worker failure has been tested.
- [ ] README documents the architecture.
- [ ] Demo video successfully goes from upload → analysis → Jev decisions → export.

---

# 35. Claude Code Operating Instructions

When implementing this project:

1. Read this document completely before modifying the repository.
2. Follow the architecture and boundaries defined here.
3. Do not add large dependencies without a clear reason.
4. Prefer simple implementations over premature infrastructure.
5. Keep media execution deterministic.
6. Keep Jev isolated behind a decision interface.
7. Never execute raw model-generated shell commands.
8. Write tests alongside core components.
9. After each major phase, run the relevant tests.
10. Do not claim a feature is implemented until it has been exercised end-to-end.
11. Keep the README synchronized with the actual implementation.
12. If a requirement is ambiguous, prefer the smallest implementation consistent with this specification.
13. If a requested implementation would violate the architecture above, explain the conflict before changing the architecture.
14. Optimize for a working end-to-end demo first, then improve quality.

---

# 36. Reference Material

Jev / harness references:

- LangChain: Building a harness with Jev
  https://www.langchain.com/blog/building-a-harness-with-jev

- LangChain: The Anatomy of an Agent Harness
  https://www.langchain.com/blog/the-anatomy-of-an-agent-harness

The implementation should always follow the current Jev API/documentation available at implementation time rather than assuming an API from this specification.

---

# 37. Final Architecture Summary

```text
                         ┌─────────────┐
                         │   Browser   │
                         └──────┬──────┘
                                │
                                ▼
                         ┌─────────────┐
                         │   FastAPI   │
                         └──────┬──────┘
                                │
                ┌───────────────┼────────────────┐
                │               │                │
                ▼               ▼                ▼
          PostgreSQL          Redis         Object Storage
          source of truth      queue             media
                │               │
                │               ▼
                │         ┌────────────┐
                │         │  Workers   │
                │         └─────┬──────┘
                │               │
                │               ▼
                │        FFmpeg / ffprobe
                │               │
                │               ▼
                │          Media data
                │               │
                └───────┬───────┘
                        ▼
                ┌─────────────────┐
                │ Editing Harness │
                └────────┬────────┘
                         │
                  ┌──────┴───────┐
                  ▼              ▼
                 LLM            Jev
                  │              │
            reasoning       decisions
                  │              │
                  └──────┬───────┘
                         ▼
                    Validated EDL
                         │
                         ▼
                  Deterministic
                  FFmpeg Worker
                         │
                         ▼
                    Final Video
```

**Core philosophy:**

> **Reason with models. Classify with Jev. Validate with application logic. Execute deterministically. Persist state durably. Recover from failure.**
