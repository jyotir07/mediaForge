import json
import logging

from app.logging import JsonFormatter, log_event


def test_log_event_emits_json_with_event_and_fields(caplog):
    with caplog.at_level(logging.INFO, logger="mediaforge"):
        log_event("job.completed", job_id="job_123", attempt=1, duration_ms=184230)

    record = caplog.records[-1]
    payload = json.loads(JsonFormatter().format(record))
    assert payload["event"] == "job.completed"
    assert payload["job_id"] == "job_123"
    assert payload["attempt"] == 1
    assert payload["duration_ms"] == 184230
    assert payload["level"] == "INFO"
