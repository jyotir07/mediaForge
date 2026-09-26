async def test_health_ok_when_postgres_and_redis_reachable(settings, client_factory):
    async with client_factory(settings) as client:
        resp = await client.get("/healthz")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "postgres": "ok", "redis": "ok"}


async def test_health_503_when_redis_unreachable(settings, client_factory):
    settings.redis_url = "redis://127.0.0.1:1/0"
    async with client_factory(settings) as client:
        resp = await client.get("/healthz")

    assert resp.status_code == 503
    body = resp.json()
    assert body["redis"] == "error"
    assert body["postgres"] == "ok"
    assert body["status"] == "degraded"
