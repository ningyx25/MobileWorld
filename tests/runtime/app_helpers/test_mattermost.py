"""Tests for the Mattermost readiness probe and fail-fast setup path.

``docker compose up -d`` returns before the Mattermost API accepts logins
(~12s measured), so ``start_mattermost_backend`` must wait for the API and
task hooks must fail loudly instead of silently skipping their setup. The
tests fake the ``requests``/``time`` module attributes, so no network or
docker is involved.
"""

from types import SimpleNamespace

import pytest
import requests as real_requests

from mobile_world.runtime.app_helpers import mattermost


class FakeResponse:
    def __init__(self, status_code=200, json_body=None, headers=None):
        self.status_code = status_code
        self._json = json_body
        self.headers = headers or {}

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class FakeRequests:
    """Stands in for the ``requests`` module inside mattermost.py."""

    RequestException = real_requests.RequestException

    def __init__(self, get_results, post_results):
        self._get_results = list(get_results)
        self._post_results = list(post_results)
        self.get_calls = []
        self.post_calls = []

    def _pop(self, results, url, kwargs):
        if not results:
            raise AssertionError(f"unexpected request to {url}")
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self._pop(self._get_results, url, kwargs)

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self._pop(self._post_results, url, kwargs)


class FakeTime:
    """Deterministic clock so the polling loop is bounded without real sleeps."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def ping_ok():
    return FakeResponse(200, {"status": "OK"})


def login(status_code, token=None):
    headers = {"Token": token} if token else {}
    return FakeResponse(status_code, {}, headers)


def patch_requests(monkeypatch, fake):
    monkeypatch.setattr(mattermost, "requests", fake)
    return fake


def patch_time(monkeypatch):
    fake = FakeTime()
    monkeypatch.setattr(mattermost, "time", fake)
    return fake


def test_ready_on_first_probe_when_ping_and_login_401(monkeypatch):
    fake = patch_requests(monkeypatch, FakeRequests([ping_ok()], [login(401)]))
    fake_time = patch_time(monkeypatch)

    assert mattermost.wait_for_mattermost_ready() is True
    assert fake_time.sleeps == []
    # Only the login probe ran: a 401 needs no logout.
    assert [url for url, _ in fake.post_calls] == [
        f"{mattermost.MATTERMOST_API_URL}/api/v4/users/login"
    ]
    # The official clients authenticate with ``login_id``, not ``username``.
    assert fake.post_calls[0][1]["json"] == {
        "login_id": mattermost.SAM_ACCOUNT["username"],
        "password": mattermost.SAM_ACCOUNT["password"],
    }


def test_retries_connection_errors_then_succeeds_and_logs_out(monkeypatch):
    refused = real_requests.ConnectionError("connection refused")
    fake = patch_requests(
        monkeypatch,
        FakeRequests(
            [refused, refused, ping_ok()],
            [login(200, token="abc123"), FakeResponse(200, {"status": "OK"})],
        ),
    )
    fake_time = patch_time(monkeypatch)

    assert mattermost.wait_for_mattermost_ready() is True
    assert fake_time.sleeps == [
        mattermost.MATTERMOST_READY_POLL_INTERVAL,
        mattermost.MATTERMOST_READY_POLL_INTERVAL,
    ]
    urls = [url for url, _ in fake.post_calls]
    assert urls == [
        f"{mattermost.MATTERMOST_API_URL}/api/v4/users/login",
        f"{mattermost.MATTERMOST_API_URL}/api/v4/users/logout",
    ]
    assert fake.post_calls[1][1]["headers"]["Authorization"] == "Bearer abc123"


def test_gives_up_when_api_never_answers(monkeypatch):
    refused = real_requests.ConnectionError("connection refused")
    fake = patch_requests(
        monkeypatch,
        FakeRequests([refused] * 10, []),
    )
    fake_time = patch_time(monkeypatch)

    assert mattermost.wait_for_mattermost_ready(timeout=6, poll_interval=2) is False
    # Probes at t=0,2,4,6; the one at the deadline ends the loop.
    assert len(fake.get_calls) == 4
    assert fake_time.sleeps == [2, 2, 2]


def test_ping_alone_is_not_enough_login_must_answer(monkeypatch):
    """ping 200 while the DB-backed login path still 500s => keep polling."""
    fake = patch_requests(
        monkeypatch,
        FakeRequests([ping_ok()] * 10, [login(500)] * 10),
    )
    patch_time(monkeypatch)

    assert mattermost.wait_for_mattermost_ready(timeout=6, poll_interval=2) is False
    assert len(fake.get_calls) == 4
    assert len(fake.post_calls) == 4


def test_start_backend_raises_when_api_never_becomes_ready(monkeypatch):
    refused = real_requests.ConnectionError("connection refused")
    # FakeTime fast-forwards through the full readiness timeout (no real sleep).
    patch_requests(monkeypatch, FakeRequests([refused] * 200, []))
    patch_time(monkeypatch)
    monkeypatch.setattr(mattermost, "get_mattermost_backend_status", lambda *a, **k: "stopped")
    monkeypatch.setattr(mattermost.shutil, "rmtree", lambda *a, **k: None)
    monkeypatch.setattr(mattermost, "copytree_with_ownership", lambda *a, **k: None)
    monkeypatch.setattr(mattermost, "_patch_mattermost_config", lambda: None)
    monkeypatch.setattr(mattermost, "_extend_session_expiry", lambda: True)
    monkeypatch.setattr(
        mattermost.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout="", stderr=""),
    )

    with pytest.raises(mattermost.MattermostSetupError, match="not ready"):
        mattermost.start_mattermost_backend()
