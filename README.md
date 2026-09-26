# MediaForge: Agentic Video Processing & Editing Harness

A distributed video-processing pipeline combining FFmpeg workers, durable job orchestration, multimodal
footage analysis, and Jev-powered bounded editing decisions.

You upload a video and ask for an edit ("Create a 45-second highlight reel…"). MediaForge probes it, builds a
proxy, finds scenes, has an LLM describe the footage and propose clips, lets **Jev** make typed decisions about
those clips, compiles the accepted ones into a validated **Edit Decision List (EDL)**, and renders it with FFmpeg.

> **The LLM reasons. Jev makes bounded decisions. Deterministic workers execute.**
> No model output is ever executed. Models produce typed data; application code maps that data onto a fixed set
> of FFmpeg operations and encoding profiles.

## Architecture

```text
Browser (static UI) ──► FastAPI ──► PostgreSQL  (source of truth: media, jobs, attempts, artifacts, plans, exports)
                          │    └──► Redis       (queue of job ids + progress pub/sub; losing it loses nothing)
                          │               │
                          │               ▼
                          │         Worker(s) ──► ffmpeg / ffprobe (argument arrays, predefined profiles)
                          │               │
                          │               ├──► LLM (Anthropic, structured outputs): scene descriptions, clip proposals
                          │               └──► Jev (TypeSafe): clip value, edit validation, recovery strategy
                          ▼
                   Local object storage (/data/media): source, proxy, frames, analysis, EDL, exports, logs
```

Pipeline per media item:

| Job (queue) | What it does | Output artifacts |
|---|---|---|
| `probe` (`media.probe`) | ffprobe → duration, resolution (rotation-aware), fps, codecs, container, bitrate | metadata on `media` |
| `proxy` (`media.proxy`) | 1080p-max H.264 proxy (short GOP, faststart) + 3x3 contact sheet | `PROXY`, `THUMBNAIL` |
| `analyze` (`media.analysis`) | scene cuts → segments → silence detection → one frame per segment → one LLM call | `FRAME`, `ANALYSIS` |
| `edit` (`media.edit`) | LLM proposes clips → hard rules + Jev validate → Jev values → deterministic EDL | `EDL`, `edit_plans` row |
| `export` (`media.export`) | per-segment frame-accurate encode → stream-copy concat → probe result | `EXPORT` (+ `LOG` on failure) |

## Where Jev is used

Jev sits behind `app/decision/`. That is the only package that imports `langchain_typesafe`, and a test enforces
it. Each decision point asks a typed question and has a deterministic fallback:

| Decision point | Jev question | Maps to | Fallback when Jev is unavailable, slow, unsure or returns anything unexpected |
|---|---|---|---|
| Clip selection | `Score`: low / medium / high value | `LOW_VALUE` / `MEDIUM_VALUE` / `HIGH_VALUE`; LOW is never exported | `0.6·llm_relevance + 0.4·audio_presence` thresholds |
| Edit validation | `Choice`: `VALID`, `INVALID_DURATION`, `INVALID_RANGE`, `LOW_VALUE`, `DUPLICATE_CONTENT` | only `VALID` clips are kept | ≥50% overlap with an accepted clip → `DUPLICATE_CONTENT`, else `VALID` |
| Export failure recovery | `Choice`: `RETRY_SAME`, `RETRY_FALLBACK_CODEC`, `RETRY_LOWER_RESOLUTION`, `DEAD_LETTER` | a predefined profile **name** (`EXPORT_FALLBACK_CODEC`, `EXPORT_LOWER_RES`) | fallback codec → lower resolution → dead letter |

Hard constraints are enforced *before* Jev and can't be overridden by it: `start >= 0`, `end > start`,
`end <= media duration`, and a clip length of 1–60 s. Every decision is stored in `edit_plans.decisions` with its
source (`rules` / `jev` / `fallback`) and confidence.

## Reliability design

- **Durable jobs.** `QUEUED → RUNNING → SUCCEEDED | RETRYING → RUNNING | FAILED`, with explicit transition rules.
  Every attempt is recorded in `job_attempts`.
- **Exactly one runner.** A worker claims a job with one guarded `UPDATE … WHERE status IN ('QUEUED','RETRYING')
  RETURNING`. Duplicate Redis deliveries and racing workers are no-ops.
- **Leases and heartbeats.** A crashed worker's lease expires. The sweeper records the attempt as `WORKER_LOST`
  and schedules a retry with exponential backoff. The sweeper also re-enqueues `QUEUED` jobs whose Redis message
  was lost, so wiping Redis loses no work. All writes are guarded by `(status = RUNNING, worker_id)`, so a worker
  that lost its lease can't overwrite the new owner.
- **Idempotency.** Jobs are keyed by `sha256(media_id : type : canonical_json(config))`, and a partial unique index
  allows one live job per key. The same request returns the same job (and its artifact once it has succeeded).
  Outputs have deterministic storage keys and are written to a temp file and atomically renamed, so "the file
  exists" means "the file is complete" and retries skip finished work. Exports resume from the first missing
  segment.
- **Failure classes.** Recoverable errors (`WORKER_LOST`, `TIMEOUT`, `LLM_UNAVAILABLE`, …) retry with backoff.
  Fallback errors (`ENCODER_FAILURE`, `RESOURCE_EXHAUSTED`) go to Jev recovery. Fatal errors (`CORRUPT_SOURCE`,
  `INVALID_EDL`, `INVALID_RANGE`, `NO_CLIPS_SELECTED`, `LLM_OUTPUT_INVALID`, …) fail on the first attempt.
- **Bounded logs.** Only a short error summary goes into Postgres. The full ffmpeg stderr of a failed step is kept
  as a `LOG` artifact.
- **Security.** Subprocesses always get argument arrays (a test fails the build if `shell=True` appears in `app/`).
  Storage keys are validated and confined to the storage root. Uploads are checked by magic bytes, not just
  extension, and size-limited while streaming. There is no URL ingestion. Model text is rendered in the UI with
  `textContent`, never as HTML.

## Running it

Requirements: Docker with Compose.

```bash
cp .env.example .env        # add ANTHROPIC_API_KEY and TYPESAFE_API_KEY
docker compose up -d --build
open http://localhost:8000  # minimal UI: upload → proxy → analyze → edit → watch progress → play export
```

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | – | LLM for scene analysis and edit planning |
| `LLM_MODEL` | `claude-opus-5` | any Claude model id |
| `LLM_BACKEND` | `anthropic` | `fake` disables the LLM (analysis/edit jobs then fail with a clear error) |
| `TYPESAFE_API_KEY` | – | Jev; without it every decision uses its deterministic fallback |
| `DECISION_BACKEND` | `jev` | `fake` forces fallback-only decisions |

The worker runs as its own service (`worker`), and you can scale it with `docker compose up -d --scale worker=3`.

### API

```text
POST /media?filename=demo.mp4   (raw body = video bytes)   → 201 {media_id, probe_job_id}
GET  /media/{id}                · GET /media/{id}/metadata
POST /media/{id}/proxy          · POST /media/{id}/analyze   → 202 {job_id, status}
POST /media/{id}/edit  {"request": "..."}                    → 202 {job_id, status}
GET  /jobs/{id}                 · GET /jobs/{id}/events  (SSE progress)
GET  /edit-plans/{id}           (decisions with sources)
GET  /exports/{id}              · GET /exports/{id}/file  (Range-capable, for <video>)
GET  /artifacts/{id}/file       · GET /healthz
```

## Tests

```bash
docker compose run --rm api pytest -q           # unit + integration (real Postgres, Redis, ffmpeg)
docker compose run --rm api ruff check app tests migrations
docker compose run --rm api mypy app
MEDIAFORGE_LIVE=1 docker compose run --rm -e MEDIAFORGE_LIVE api pytest tests/integration/test_e2e.py  # real LLM + Jev (costs money)
```

Test videos are generated with `ffmpeg -f lavfi` at test time, so there are no binary fixtures. The failure tests
from the spec (§24) map to:

| Spec failure test | Test |
|---|---|
| 1. Worker dies during processing | `test_worker.py::test_worker_dying_mid_job_is_recovered_by_sweeper` |
| 2. FFmpeg exits non-zero | `test_ffmpeg_run.py::test_nonzero_exit_raises_classified_error_with_bounded_tail` |
| 3–4. Same job / idempotency key twice | `test_job_service.py` (sequential + 20 concurrent), `test_worker.py::test_duplicate_delivery_runs_job_once` |
| 5. Invalid timestamp | `test_edit_validator.py`, `test_edl_builder.py`, `test_edit_job.py::test_out_of_range_llm_clips_are_rejected_by_rules` |
| 6. Missing source artifact | `test_worker.py::test_missing_source_fails_without_retry` |
| 7. Export fails once, succeeds on retry | `test_export_job.py::test_encoder_failure_recovers_with_fallback_profile` |
| 8. Jev returns an unexpected decision | `test_clip_classifier.py`, `test_edit_validator.py`, `test_recovery_classifier.py` |
| 9. LLM returns malformed output | `test_llm_structured.py`, `test_analysis_job.py::test_malformed_llm_output_fails_without_retry` |

## Measured

These numbers were measured in development (Docker Desktop on Windows 11, one worker). They are not benchmarks.

- Test suite: 272 passed, 1 skipped (live test).
- A 10 s 1080p30 H.264 source took 0.83 s to probe and 27.8 s to proxy (1080p `veryfast` H.264 + contact sheet).

## Known limitations

- End-to-end runs against the **real** LLM and Jev haven't been verified yet. In development no Anthropic key was
  configured, and the configured TypeSafe key returned `401 Unauthorized`. The pipeline is exercised end to end
  with a scripted LLM and the deterministic decision fallbacks (`tests/integration/test_e2e.py`).
- No transcription: clip relevance comes from frames and audio presence only.
- Storage is the local filesystem behind a small interface; there is no S3 implementation.
- No auth, cancellation or multi-tenancy (explicit non-goals for the MVP).
