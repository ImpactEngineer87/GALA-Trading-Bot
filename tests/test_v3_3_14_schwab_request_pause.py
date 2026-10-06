"""Clock-driven network tests: no sleep, broker credentials, or real HTTP."""

import json
import threading
from unittest.mock import Mock

import pytest
import requests

from api.schwab_transport import SchwabRequestGate, SchwabRequestPaused


URL = "https://api.schwabapi.com/marketdata/v1/quotes"


def response(status, body="{}"):
    result = requests.Response()
    result.status_code = status
    result._content = body.encode()
    return result


class Clock:
    def __init__(self, wall=1790880000, monotonic=100):
        self.wall = wall
        self.monotonic = monotonic

    def advance(self, seconds):
        self.wall += seconds
        self.monotonic += seconds


@pytest.fixture
def gate(tmp_path):
    clock = Clock()
    result = SchwabRequestGate(tmp_path / "pause.json", clock=lambda: clock.wall,
                              monotonic=lambda: clock.monotonic)
    return result, clock


@pytest.mark.parametrize("body", ["<HTML>Access Denied</HTML>", '{"message":"Forbidden"}'])
def test_first_403_stops_every_following_call_for_a_full_ten_minutes(gate, body):
    guard, clock = gate
    send = Mock(side_effect=[response(403, body), response(200)])
    assert guard.request(send, URL).status_code == 403
    for url in [URL, "https://api.schwabapi.com/v1/oauth/token",
                "https://api.schwabapi.com/trader/v1/accounts/fake/orders"]:
        with pytest.raises(SchwabRequestPaused) as paused:
            guard.request(send, url)
        assert paused.value.retry_after_seconds == 600
        assert paused.value.request_sent is False
    clock.advance(599.999)
    with pytest.raises(SchwabRequestPaused):
        guard.request(send, URL)
    assert send.call_count == 1
    clock.advance(.001)
    assert guard.request(send, URL).status_code == 200
    assert send.call_count == 2
    assert guard.snapshot()["paused"] is False
    assert guard.snapshot()["recovery_pending"] is False


def test_restart_restores_remaining_wait_with_a_new_monotonic_clock(gate):
    guard, clock = gate
    guard.request(Mock(return_value=response(403)), URL)
    clock.advance(300)
    restarted = SchwabRequestGate(guard.state_path, clock=lambda: clock.wall,
                                 monotonic=lambda: 0)
    send = Mock()
    with pytest.raises(SchwabRequestPaused) as paused:
        restarted.request(send, URL)
    assert paused.value.retry_after_seconds == 300
    assert restarted.snapshot()["recovery_pending"] is True
    send.assert_not_called()


def test_clock_adjustment_cannot_shorten_the_running_pause(gate):
    guard, clock = gate
    guard.request(Mock(return_value=response(403)), URL)
    clock.wall += 3600
    assert guard.remaining() == 600


def test_repeated_denials_increase_the_wait_without_resetting_on_restart(gate):
    guard, clock = gate
    guard.request(Mock(return_value=response(403)), URL)
    clock.advance(600)
    restarted = SchwabRequestGate(guard.state_path, clock=lambda: clock.wall,
                                 monotonic=lambda: clock.monotonic)
    restarted.request(Mock(return_value=response(403)), URL)
    assert restarted.remaining() == 1200
    assert restarted.snapshot()["denial_count"] == 2


def test_longer_retry_after_is_honored(gate):
    guard, _ = gate
    denied = response(403)
    denied.headers["Retry-After"] = "1800"
    guard.request(Mock(return_value=denied), URL)
    assert guard.remaining() == 1800


def test_orders_are_not_used_as_recovery_probes_or_replayed(gate):
    guard, clock = gate
    guard.request(Mock(return_value=response(403)), URL)
    clock.advance(600)
    post = Mock(return_value=response(201))
    with pytest.raises(SchwabRequestPaused) as paused:
        guard.request(post, URL, recovery_safe=False, json={"order": "never submitted"})
    assert paused.value.recovery_check is True
    post.assert_not_called()
    guard.request(Mock(return_value=response(200)), URL)
    # A new explicit invocation is necessary; the refused order was not queued.
    post.assert_not_called()
    assert guard.request(post, URL, recovery_safe=False).status_code == 201
    post.assert_called_once()


def test_only_one_recovery_check_is_in_flight(gate):
    guard, clock = gate
    guard.request(Mock(return_value=response(403)), URL)
    clock.advance(600)
    entered = threading.Event()
    release = threading.Event()
    failures = []

    def send(url):
        entered.set()
        assert release.wait(2)
        return response(200)

    def recover():
        try:
            guard.request(send, URL)
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=recover)
    worker.start()
    try:
        assert entered.wait(2)
        second = Mock()
        with pytest.raises(SchwabRequestPaused):
            guard.request(second, URL)
        second.assert_not_called()
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    assert failures == []
    assert not guard.snapshot()["paused"]


@pytest.mark.parametrize("failure", [requests.Timeout("offline"), response(503), response(429)])
def test_failed_recovery_rearms_the_pause(gate, failure):
    guard, clock = gate
    guard.request(Mock(return_value=response(403)), URL)
    clock.advance(600)
    if isinstance(failure, BaseException):
        with pytest.raises(requests.Timeout):
            guard.request(Mock(side_effect=failure), URL)
    else:
        guard.request(Mock(return_value=failure), URL)
    assert guard.remaining() >= 600
    next_send = Mock()
    with pytest.raises(SchwabRequestPaused):
        guard.request(next_send, URL)
    next_send.assert_not_called()


def test_a_late_403_cannot_be_cleared_by_a_successful_recovery_probe(gate):
    guard, clock = gate
    entered = threading.Event()
    release = threading.Event()
    failures = []

    def old_request(url):
        entered.set()
        assert release.wait(2)
        return response(403)

    def worker_request():
        try:
            guard.request(old_request, URL)
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=worker_request)
    worker.start()
    try:
        assert entered.wait(2)
        guard.request(Mock(return_value=response(403)), URL)
        clock.advance(600)

        def recover(url):
            release.set()
            worker.join(2)
            assert not worker.is_alive()
            return response(200)

        guard.request(recover, URL)
    finally:
        release.set()
        worker.join(2)
    assert failures == []
    assert guard.remaining() == 1200
    assert guard.snapshot()["recovery_pending"] is True


def test_a_structured_oauth_error_only_confirms_the_http_route(gate):
    guard, clock = gate
    guard.request(Mock(return_value=response(403)), URL)
    clock.advance(600)
    result = guard.request(Mock(return_value=response(400, '{"error":"invalid_grant"}')),
                           "https://api.schwabapi.com/v1/oauth/token")
    assert result.status_code == 400
    assert not guard.snapshot()["paused"]
    assert not guard.snapshot()["recovery_pending"]


def test_cooldown_file_contains_no_request_or_credential_information(gate):
    guard, _ = gate
    guard.request(Mock(return_value=response(403)), URL + "?account=private-account",
                  headers={"Authorization": "Bearer private-access"},
                  data={"refresh_token": "private-refresh"})
    state = guard.state_path.read_text()
    assert set(json.loads(state)) == {"retry_at_epoch", "denial_count", "recovery_pending", "pause_reason"}
    for secret in ["private-access", "private-refresh", "private-account", URL]:
        assert secret not in state


def test_corrupt_cooldown_record_does_not_allow_immediate_requests(tmp_path):
    path = tmp_path / "pause.json"
    path.write_text("damaged", encoding="utf-8")
    guard = SchwabRequestGate(path)
    send = Mock()
    with pytest.raises(SchwabRequestPaused):
        guard.request(send, URL)
    send.assert_not_called()
    assert guard.snapshot()["persistence_error"]
    assert path.read_text() == "damaged"


def test_unwritable_storage_keeps_the_in_memory_pause(gate, monkeypatch):
    guard, _ = gate
    monkeypatch.setattr("api.schwab_transport.os.replace", Mock(side_effect=PermissionError))
    guard.request(Mock(return_value=response(403)), URL)
    assert guard.remaining() == 600
    assert guard.snapshot()["persistence_error"]
    assert list(guard.state_path.parent.glob(".schwab-cooldown-*.tmp")) == []


def test_other_services_remain_available_during_schwab_pause(gate):
    guard, _ = gate
    guard.request(Mock(return_value=response(403)), URL)
    send = Mock(return_value=response(200))
    assert guard.request(send, "https://example.test/health").status_code == 200
    send.assert_called_once()
