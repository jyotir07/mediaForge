# CLAUDE.md: MediaForge

Read `MediaForge_Project_Spec.md` (the product spec) and `plan.md` (implementation plan with rulings) before
changing architecture.

## Commands (everything runs in Docker; the host is Windows)

```bash
docker compose up -d --build                      # api + worker + postgres + redis (api and worker share one image)
docker compose run --rm --no-deps api pytest -q   # full suite; integration tests use real Postgres/Redis/ffmpeg
docker compose run --rm --no-deps api sh -c "ruff format app tests && ruff check app tests migrations && mypy app"
docker compose restart api worker                 # code is bind-mounted but nothing hot-reloads
```

Tests use the `mediaforge_test` database and Redis DB 15, so a running compose worker never interferes.

## Rules that must not be broken

- Never run model output. The LLM and Jev return typed data only. ffmpeg arguments come from `app/media/*`
  builders and `app/media/profiles.py`; decisions may only select a profile **name**.
- Only `app/decision/` may import `langchain_typesafe` (a test enforces this). Every decision needs a
  deterministic fallback.
- Subprocesses use argument arrays; `shell=True` fails a test.
- Postgres is the source of truth. Redis only carries job ids and progress events.
- Job state changes go through `app/jobs/service.py` (guarded UPDATEs). Handlers must be idempotent: write outputs
  with `steps.produce()` (temp file + atomic rename) and register them with `steps.upsert_artifact()`.
- New failure modes get an `ErrorCode` with a recoverability (`RETRY` / `FALLBACK` / `FATAL`).

## Layout

`app/api` HTTP · `app/jobs` state machine + job service · `app/queue` Redis · `app/workers` runner + handlers ·
`app/media` ffprobe/ffmpeg builders and parsers · `app/ai` LLM client + prompts · `app/decision` Jev layer ·
`app/services/edl.py` EDL builder · `frontend/` static UI · `tests/unit`, `tests/integration`.
