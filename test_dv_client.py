"""
Comprehensive tests for dv_client.py -- DataverseClient with exponential backoff retry.

Tests cover:
- Token management (caching, refresh, timeout)
- Retry logic (transient errors, non-retryable errors, backoff)
- 412 ETag conflict handling
- 401 token refresh flow
- Budget exhaustion
- Public API (get, post, patch, delete)
- URL helpers
- Logging
- Edge cases (token refresh during retry, network error recovery)
"""
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch, call

import pytest
import requests

from conftest import FakeAccessToken, FakeResponse
from dv_client import (
    DataverseClient,
    DataverseError,
    DataverseRetryExhausted,
    ETagConflictError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(
    credential=None,
    log_fn=None,
    max_retry_seconds=900,
    request_timeout=30,
):
    """Create a DataverseClient with a mock credential."""
    cred = credential or MagicMock()
    if credential is None:
        cred.get_token.return_value = FakeAccessToken()
    return DataverseClient(
        dataverse_url="https://test-org.crm.dynamics.com",
        credential=cred,
        log_fn=log_fn or (lambda msg: None),
        request_timeout=request_timeout,
        max_retry_seconds=max_retry_seconds,
    )


def _ok_response(status_code=200, json_data=None, text="", headers=None):
    """Build a FakeResponse that looks like a successful response."""
    return FakeResponse(
        status_code=status_code,
        json_data=json_data or {},
        text=text,
        headers=headers or {},
    )


def _err_response(status_code, text="error", headers=None):
    """Build a FakeResponse with an error status code."""
    return FakeResponse(
        status_code=status_code,
        text=text,
        headers=headers or {},
    )


# ===========================================================================
# Token management
# ===========================================================================

class TestTokenManagement:

    @patch("dv_client.call_with_timeout")
    def test_get_token_caches_result(self, mock_cwt):
        """Second call returns the cached token without re-acquiring."""
        fake_token = FakeAccessToken(token="tok-1")
        mock_cwt.return_value = fake_token
        client = _make_client()

        t1 = client.get_token()
        t2 = client.get_token()

        assert t1 == "tok-1"
        assert t2 == "tok-1"
        # call_with_timeout should only be invoked once
        assert mock_cwt.call_count == 1

    @patch("dv_client.call_with_timeout")
    def test_get_token_refreshes_on_expiry(self, mock_cwt):
        """Token is re-acquired when the cached one is past its expiry."""
        # First token expires immediately (in the past)
        expired_token = FakeAccessToken(
            token="old",
            expires_on=(datetime.now(timezone.utc) - timedelta(hours=1)).timestamp(),
        )
        fresh_token = FakeAccessToken(token="new")
        mock_cwt.side_effect = [expired_token, fresh_token]

        client = _make_client()
        t1 = client.get_token()
        assert t1 == "old"

        # Force expiry by setting _token_expires to the past
        client._token_expires = datetime.now(timezone.utc) - timedelta(minutes=1)
        t2 = client.get_token()
        assert t2 == "new"
        assert mock_cwt.call_count == 2

    @patch("dv_client.call_with_timeout")
    def test_get_token_raises_on_timeout(self, mock_cwt):
        """TimeoutError from call_with_timeout propagates to caller."""
        mock_cwt.side_effect = TimeoutError("timed out after 30s")
        client = _make_client()

        with pytest.raises(TimeoutError, match="timed out"):
            client.get_token()

    @patch("dv_client.call_with_timeout")
    def test_refresh_token_clears_cache(self, mock_cwt):
        """_refresh_token clears the cache and re-acquires."""
        tok1 = FakeAccessToken(token="first")
        tok2 = FakeAccessToken(token="second")
        mock_cwt.side_effect = [tok1, tok2]

        client = _make_client()
        client.get_token()
        assert client._token == "first"

        client._refresh_token()
        assert client._token == "second"
        assert mock_cwt.call_count == 2


# ===========================================================================
# Retry logic - transient errors
# ===========================================================================

class TestRetryTransient:

    @patch("dv_client.time.sleep")
    @patch("dv_client.requests.request")
    def test_success_no_retry(self, mock_req, mock_sleep):
        """200 on first call -- no retry needed."""
        mock_req.return_value = _ok_response(200, {"value": []})
        client = _make_client()

        resp = client.get("https://test-org.crm.dynamics.com/api/data/v9.2/tasks")

        assert resp.status_code == 200
        assert mock_req.call_count == 1
        mock_sleep.assert_not_called()

    @patch("dv_client.time.sleep")
    @patch("dv_client.requests.request")
    def test_retry_on_500(self, mock_req, mock_sleep):
        """500 then 200 -- retries once and succeeds."""
        mock_req.side_effect = [
            _err_response(500, "Internal Server Error"),
            _ok_response(200, {"value": []}),
        ]
        client = _make_client()

        resp = client.get("https://example.com/api")

        assert resp.status_code == 200
        assert mock_req.call_count == 2
        mock_sleep.assert_called_once()

    @patch("dv_client.time.sleep")
    @patch("dv_client.requests.request")
    def test_retry_on_429_with_retry_after(self, mock_req, mock_sleep):
        """429 with Retry-After header -- respects the server-specified delay."""
        mock_req.side_effect = [
            _err_response(429, "Too Many Requests", headers={"Retry-After": "5"}),
            _ok_response(200),
        ]
        client = _make_client()

        resp = client.get("https://example.com/api")

        assert resp.status_code == 200
        # Sleep should use the Retry-After value (5) since it's > initial delay (1)
        mock_sleep.assert_called_once_with(5.0)

    @patch("dv_client.time.sleep")
    @patch("dv_client.requests.request")
    def test_retry_on_connection_error(self, mock_req, mock_sleep):
        """ConnectionError then success -- retries on network errors."""
        mock_req.side_effect = [
            requests.exceptions.ConnectionError("Connection refused"),
            _ok_response(200),
        ]
        client = _make_client()

        resp = client.get("https://example.com/api")

        assert resp.status_code == 200
        assert mock_req.call_count == 2

    @patch("dv_client.time.sleep")
    @patch("dv_client.requests.request")
    def test_retry_on_timeout_error(self, mock_req, mock_sleep):
        """requests.Timeout then success -- retries on timeout."""
        mock_req.side_effect = [
            requests.exceptions.Timeout("Read timed out"),
            _ok_response(200),
        ]
        client = _make_client()

        resp = client.get("https://example.com/api")

        assert resp.status_code == 200
        assert mock_req.call_count == 2

    @patch("dv_client.time.sleep")
    @patch("dv_client.requests.request")
    def test_exponential_backoff_doubles(self, mock_req, mock_sleep):
        """Backoff delays double: 1, 2, 4, 8, ..."""
        mock_req.side_effect = [
            _err_response(500),
            _err_response(502),
            _err_response(503),
            _ok_response(200),
        ]
        client = _make_client()

        resp = client.get("https://example.com/api")

        assert resp.status_code == 200
        assert mock_req.call_count == 4
        sleep_calls = [c.args[0] for c in mock_sleep.call_args_list]
        assert sleep_calls == [1.0, 2.0, 4.0]


# ===========================================================================
# Retry logic - non-retryable errors
# ===========================================================================

class TestNoRetryOnClientErrors:

    @patch("dv_client.time.sleep")
    @patch("dv_client.requests.request")
    def test_no_retry_on_400(self, mock_req, mock_sleep):
        """400 Bad Request raises DataverseError immediately."""
        mock_req.return_value = _err_response(400, "Bad Request")
        client = _make_client()

        with pytest.raises(DataverseError) as exc_info:
            client.get("https://example.com/api")

        assert exc_info.value.status_code == 400
        mock_sleep.assert_not_called()
        assert mock_req.call_count == 1

    @patch("dv_client.time.sleep")
    @patch("dv_client.requests.request")
    def test_no_retry_on_403(self, mock_req, mock_sleep):
        """403 Forbidden raises DataverseError immediately."""
        mock_req.return_value = _err_response(403, "Forbidden")
        client = _make_client()

        with pytest.raises(DataverseError) as exc_info:
            client.get("https://example.com/api")

        assert exc_info.value.status_code == 403
        mock_sleep.assert_not_called()

    @patch("dv_client.time.sleep")
    @patch("dv_client.requests.request")
    def test_no_retry_on_404(self, mock_req, mock_sleep):
        """404 Not Found raises DataverseError immediately."""
        mock_req.return_value = _err_response(404, "Not Found")
        client = _make_client()

        with pytest.raises(DataverseError) as exc_info:
            client.get("https://example.com/api")

        assert exc_info.value.status_code == 404
        assert "Not Found" in exc_info.value.response_text
        mock_sleep.assert_not_called()


# ===========================================================================
# 412 ETag conflict handling
# ===========================================================================

class TestETagConflict:

    @patch("dv_client.time.sleep")
    @patch("dv_client.requests.request")
    def test_412_raises_etag_conflict(self, mock_req, mock_sleep):
        """412 raises ETagConflictError immediately -- never retries."""
        mock_req.return_value = _err_response(412, "Precondition Failed")
        client = _make_client()

        with pytest.raises(ETagConflictError, match="412"):
            client.patch("https://example.com/api/tasks(123)", data={"cr_status": "Done"})

        mock_sleep.assert_not_called()
        assert mock_req.call_count == 1

    @patch("dv_client.time.sleep")
    @patch("dv_client.requests.request")
    def test_patch_with_etag_sends_if_match(self, mock_req, mock_sleep):
        """PATCH with etag includes If-Match header."""
        mock_req.return_value = _ok_response(204)
        client = _make_client()

        client.patch(
            "https://example.com/api/tasks(123)",
            data={"cr_status": "Done"},
            etag='W/"12345"',
        )

        # Inspect the headers sent
        call_kwargs = mock_req.call_args
        headers = call_kwargs.kwargs["headers"]
        assert headers["If-Match"] == 'W/"12345"'


# ===========================================================================
# 401 handling
# ===========================================================================

class TestAuth401:

    @patch("dv_client.time.sleep")
    @patch("dv_client.requests.request")
    def test_401_refreshes_token_and_retries(self, mock_req, mock_sleep):
        """401 triggers token refresh, then retries with new token."""
        mock_req.side_effect = [
            _err_response(401, "Unauthorized"),
            _ok_response(200, {"value": []}),
        ]

        cred = MagicMock()
        cred.get_token.side_effect = [
            FakeAccessToken(token="old-token"),
            FakeAccessToken(token="new-token"),
        ]

        client = _make_client(credential=cred)

        resp = client.get("https://example.com/api")

        assert resp.status_code == 200
        assert mock_req.call_count == 2
        # Verify the second request used the refreshed token
        second_call_headers = mock_req.call_args_list[1].kwargs["headers"]
        assert "new-token" in second_call_headers["Authorization"]
        # No sleep on 401 retry (immediate retry)
        mock_sleep.assert_not_called()

    @patch("dv_client.time.sleep")
    @patch("dv_client.requests.request")
    def test_401_twice_raises(self, mock_req, mock_sleep):
        """Second 401 after token refresh raises DataverseError."""
        mock_req.side_effect = [
            _err_response(401, "Unauthorized"),
            _err_response(401, "Still Unauthorized"),
        ]

        cred = MagicMock()
        cred.get_token.side_effect = [
            FakeAccessToken(token="old"),
            FakeAccessToken(token="new"),
        ]

        client = _make_client(credential=cred)

        with pytest.raises(DataverseError) as exc_info:
            client.get("https://example.com/api")

        assert exc_info.value.status_code == 401


# ===========================================================================
# Budget exhaustion
# ===========================================================================

class TestBudgetExhaustion:

    @patch("dv_client.time.monotonic")
    @patch("dv_client.time.sleep")
    @patch("dv_client.requests.request")
    def test_raises_retry_exhausted_after_budget(self, mock_req, mock_sleep, mock_mono):
        """Budget exhaustion raises DataverseRetryExhausted."""
        mock_req.return_value = _err_response(500, "Server Error")

        # Simulate time progressing: start at 0, each call advances past budget
        # First call to monotonic is for deadline (0 + 10 = 10)
        # After first 500: remaining check shows time is almost up
        mock_mono.side_effect = [
            0.0,    # deadline = 0 + 10 = 10
            9.5,    # remaining = 10 - 9.5 = 0.5 < delay(1.0) → exhaust
        ]

        client = _make_client(max_retry_seconds=10)

        with pytest.raises(DataverseRetryExhausted, match="budget exhausted"):
            client.get("https://example.com/api")

    @patch("dv_client.time.monotonic")
    @patch("dv_client.time.sleep")
    @patch("dv_client.requests.request")
    def test_per_call_retry_budget_override(self, mock_req, mock_sleep, mock_mono):
        """Per-call max_retry_seconds overrides instance default."""
        mock_req.return_value = _err_response(503, "Service Unavailable")

        # Instance default is 900s but call overrides to 5s
        mock_mono.side_effect = [
            0.0,    # deadline = 0 + 5 = 5
            4.5,    # remaining = 5 - 4.5 = 0.5 < delay(1.0) → exhaust
        ]

        client = _make_client(max_retry_seconds=900)

        with pytest.raises(DataverseRetryExhausted):
            client.get("https://example.com/api", max_retry_seconds=5)


# ===========================================================================
# Public API
# ===========================================================================

class TestPublicAPI:

    @patch("dv_client.time.sleep")
    @patch("dv_client.requests.request")
    def test_get_sends_correct_request(self, mock_req, mock_sleep):
        """get() sends a GET with correct URL and params."""
        mock_req.return_value = _ok_response(200, {"value": []})
        client = _make_client()

        resp = client.get(
            "https://test-org.crm.dynamics.com/api/data/v9.2/tasks",
            params={"$filter": "cr_status eq 'Pending'"},
        )

        assert resp.status_code == 200
        call_args = mock_req.call_args
        assert call_args.args[0] == "GET"
        assert call_args.args[1] == "https://test-org.crm.dynamics.com/api/data/v9.2/tasks"
        assert call_args.kwargs["params"] == {"$filter": "cr_status eq 'Pending'"}

    @patch("dv_client.time.sleep")
    @patch("dv_client.requests.request")
    def test_post_sends_json_with_content_type(self, mock_req, mock_sleep):
        """post() sends JSON data with Content-Type application/json."""
        mock_req.return_value = _ok_response(201, {"cr_shraga_taskid": "abc"})
        client = _make_client()

        resp = client.post(
            "https://test-org.crm.dynamics.com/api/data/v9.2/tasks",
            data={"cr_name": "New Task"},
        )

        assert resp.status_code == 201
        call_args = mock_req.call_args
        assert call_args.args[0] == "POST"
        assert call_args.kwargs["json"] == {"cr_name": "New Task"}
        assert call_args.kwargs["headers"]["Content-Type"] == "application/json"

    @patch("dv_client.time.sleep")
    @patch("dv_client.requests.request")
    def test_patch_sends_etag(self, mock_req, mock_sleep):
        """patch() includes If-Match when etag is provided."""
        mock_req.return_value = _ok_response(204)
        client = _make_client()

        client.patch(
            "https://test-org.crm.dynamics.com/api/data/v9.2/tasks(123)",
            data={"cr_status": "Completed"},
            etag='W/"67890"',
        )

        call_args = mock_req.call_args
        assert call_args.args[0] == "PATCH"
        assert call_args.kwargs["headers"]["If-Match"] == 'W/"67890"'
        assert call_args.kwargs["json"] == {"cr_status": "Completed"}

    @patch("dv_client.time.sleep")
    @patch("dv_client.requests.request")
    def test_delete_sends_delete(self, mock_req, mock_sleep):
        """delete() sends a DELETE request."""
        mock_req.return_value = _ok_response(204)
        client = _make_client()

        resp = client.delete(
            "https://test-org.crm.dynamics.com/api/data/v9.2/tasks(123)",
        )

        assert resp.status_code == 204
        call_args = mock_req.call_args
        assert call_args.args[0] == "DELETE"


# ===========================================================================
# URL helpers
# ===========================================================================

class TestURLHelpers:

    def test_table_url(self):
        """table_url returns correct OData entity set URL."""
        client = _make_client()
        url = client.table_url("cr_shraga_tasks")
        assert url == "https://test-org.crm.dynamics.com/api/data/v9.2/cr_shraga_tasks"

    def test_row_url(self):
        """row_url returns correct OData entity URL with ID."""
        client = _make_client()
        url = client.row_url("cr_shraga_tasks", "abc-123")
        assert url == "https://test-org.crm.dynamics.com/api/data/v9.2/cr_shraga_tasks(abc-123)"


# ===========================================================================
# Logging
# ===========================================================================

class TestLogging:

    @patch("dv_client.time.sleep")
    @patch("dv_client.requests.request")
    def test_logs_retry_attempts(self, mock_req, mock_sleep):
        """Retry attempts are logged with status code, attempt number, and timing."""
        mock_req.side_effect = [
            _err_response(500, "Internal Server Error"),
            _ok_response(200),
        ]
        log_messages = []
        client = _make_client(log_fn=log_messages.append)

        client.get("https://example.com/api")

        assert len(log_messages) == 1
        assert "[DV-CLIENT]" in log_messages[0]
        assert "500" in log_messages[0]
        assert "attempt 1" in log_messages[0]
        assert "retrying in" in log_messages[0]
        assert "remaining" in log_messages[0]

    @patch("dv_client.time.sleep")
    @patch("dv_client.requests.request")
    def test_custom_log_fn(self, mock_req, mock_sleep):
        """Custom log function receives all retry messages."""
        mock_req.side_effect = [
            _err_response(502),
            _err_response(503),
            _ok_response(200),
        ]

        custom_log = MagicMock()
        client = _make_client(log_fn=custom_log)

        client.get("https://example.com/api")

        assert custom_log.call_count == 2
        # Both calls should contain DV-CLIENT prefix
        for c in custom_log.call_args_list:
            assert "[DV-CLIENT]" in c.args[0]


# ===========================================================================
# Edge cases
# ===========================================================================

class TestEdgeCases:

    @patch("dv_client.time.sleep")
    @patch("dv_client.requests.request")
    def test_token_refresh_during_retry_sequence(self, mock_req, mock_sleep):
        """After a transient error + backoff, a subsequent 401 can still refresh."""
        # Sequence: 500 (retry) → 401 (refresh token) → 200 (success)
        mock_req.side_effect = [
            _err_response(500, "Server Error"),
            _err_response(401, "Unauthorized"),
            _ok_response(200, {"value": []}),
        ]

        cred = MagicMock()
        cred.get_token.side_effect = [
            FakeAccessToken(token="initial"),
            FakeAccessToken(token="refreshed"),
        ]

        client = _make_client(credential=cred)

        resp = client.get("https://example.com/api")

        assert resp.status_code == 200
        assert mock_req.call_count == 3
        # Token was refreshed after the 401
        assert cred.get_token.call_count == 2

    @patch("dv_client.time.sleep")
    @patch("dv_client.requests.request")
    def test_network_error_then_success(self, mock_req, mock_sleep):
        """OSError then success -- retries on low-level network errors."""
        mock_req.side_effect = [
            OSError("Network is unreachable"),
            _ok_response(200, {"ok": True}),
        ]
        client = _make_client()

        resp = client.get("https://example.com/api")

        assert resp.status_code == 200
        assert mock_req.call_count == 2
        mock_sleep.assert_called_once_with(1.0)

    @patch("dv_client.time.sleep")
    @patch("dv_client.requests.request")
    def test_chunked_encoding_error_retries(self, mock_req, mock_sleep):
        """ChunkedEncodingError is retryable."""
        mock_req.side_effect = [
            requests.exceptions.ChunkedEncodingError("Connection broken"),
            _ok_response(200),
        ]
        client = _make_client()

        resp = client.get("https://example.com/api")

        assert resp.status_code == 200
        assert mock_req.call_count == 2

    @patch("dv_client.time.sleep")
    @patch("dv_client.requests.request")
    def test_429_without_retry_after_uses_backoff(self, mock_req, mock_sleep):
        """429 without Retry-After header uses normal exponential backoff."""
        mock_req.side_effect = [
            _err_response(429, "Too Many Requests"),
            _ok_response(200),
        ]
        client = _make_client()

        resp = client.get("https://example.com/api")

        assert resp.status_code == 200
        # Should use the default delay (1.0) since no Retry-After header
        mock_sleep.assert_called_once_with(1.0)

    @patch("dv_client.time.sleep")
    @patch("dv_client.requests.request")
    def test_post_with_extra_headers(self, mock_req, mock_sleep):
        """post() passes extra_headers through to the request."""
        mock_req.return_value = _ok_response(201)
        client = _make_client()

        client.post(
            "https://example.com/api",
            data={"cr_name": "Test"},
            extra_headers={"Prefer": "return=representation"},
        )

        headers = mock_req.call_args.kwargs["headers"]
        assert headers["Prefer"] == "return=representation"

    @patch("dv_client.time.sleep")
    @patch("dv_client.requests.request")
    def test_backoff_capped_at_60_seconds(self, mock_req, mock_sleep):
        """Backoff delay is capped at 60 seconds even after many retries."""
        # 1, 2, 4, 8, 16, 32, 64→60, 128→60
        responses = [_err_response(500) for _ in range(7)] + [_ok_response(200)]
        mock_req.side_effect = responses
        client = _make_client()

        resp = client.get("https://example.com/api")

        assert resp.status_code == 200
        sleep_calls = [c.args[0] for c in mock_sleep.call_args_list]
        assert sleep_calls == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0]

    @patch("dv_client.time.sleep")
    @patch("dv_client.requests.request")
    def test_204_is_success(self, mock_req, mock_sleep):
        """204 No Content is treated as success (no retry)."""
        mock_req.return_value = _ok_response(204)
        client = _make_client()

        resp = client.delete("https://example.com/api/tasks(123)")

        assert resp.status_code == 204
        assert mock_req.call_count == 1
        mock_sleep.assert_not_called()

    @patch("dv_client.time.sleep")
    @patch("dv_client.requests.request")
    def test_network_exhaustion_includes_last_error(self, mock_req, mock_sleep):
        """DataverseRetryExhausted.last_error contains the last exception."""
        mock_req.side_effect = requests.exceptions.ConnectionError("down")

        # Use a very short budget so it exhausts quickly
        client = _make_client(max_retry_seconds=0)

        with pytest.raises(DataverseRetryExhausted) as exc_info:
            client.get("https://example.com/api")

        assert exc_info.value.last_error is not None
        assert isinstance(exc_info.value.last_error, requests.exceptions.ConnectionError)
