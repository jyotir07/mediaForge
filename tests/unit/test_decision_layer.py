from pydantic import SecretStr

from app.config import Settings
from app.decision import create_decision_layer
from app.decision.jev import JevClient


def test_jev_backend_with_key_uses_jev():
    layer = create_decision_layer(Settings(decision_backend="jev", typesafe_api_key=SecretStr("ts-test")))
    assert isinstance(layer.jev, JevClient)


def test_missing_key_degrades_to_fallback_instead_of_crashing(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    layer = create_decision_layer(Settings(_env_file=None, decision_backend="jev", typesafe_api_key=None))
    assert layer.jev is None


def test_fake_backend_never_calls_jev():
    layer = create_decision_layer(Settings(decision_backend="fake", typesafe_api_key=SecretStr("ts-test")))
    assert layer.jev is None
