"""Never share a deployment's persisted Schwab cooldown between tests."""

import pytest

from api import schwab_transport


@pytest.fixture(autouse=True)
def isolated_schwab_request_gate(monkeypatch, tmp_path):
    gate = schwab_transport.SchwabRequestGate(tmp_path / "schwab_request_cooldown.json")
    monkeypatch.setattr(schwab_transport, "_GATE", gate)
    return gate
