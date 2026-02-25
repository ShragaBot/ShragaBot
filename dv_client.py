"""
Dataverse client with exponential-backoff retry for resilient API access.

Provides a ``DataverseClient`` class that wraps all Dataverse HTTP interactions
with automatic retry on transient errors (429, 5xx, network errors), token
refresh on 401, and deadline-based budget control.

Custom exceptions:
    DataverseRetryExhausted -- retry budget exhausted without success
    DataverseError          -- non-retryable 4xx from Dataverse
    ETagConflictError       -- 412 Precondition Failed (optimistic concurrency)
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone, timedelta

import requests

from timeout_utils import call_with_timeout


# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------
DEFAULT_DATAVERSE_URL = "https://org3e79cdb1.crm3.dynamics.com"
DEFAULT_API_VERSION = "v9.2"


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class DataverseRetryExhausted(Exception):
    """Raised when the retry budget is exhausted without a successful response."""

    def __init__(self, message: str, last_error: Exception | None = None):
        super().__init__(message)
        self.last_error = last_error


class DataverseError(Exception):
    """Raised on a non-retryable 4xx error from Dataverse."""

    def __init__(self, message: str, status_code: int, response_text: str):
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


class ETagConflictError(Exception):
    """Raised on HTTP 412 Precondition Failed (optimistic concurrency conflict)."""
    pass


# ---------------------------------------------------------------------------
# Retryable error classification
# ---------------------------------------------------------------------------
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_RETRYABLE_EXCEPTIONS = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
    OSError,
)


# ---------------------------------------------------------------------------
# Credential creation with WMI hang protection
# ---------------------------------------------------------------------------

def create_credential(log_fn=None):
    """Create a credential for Azure API access via ``DefaultAzureCredential``.

    Uses the standard ``DefaultAzureCredential`` from the ``azure-identity``
    SDK.  The credential chain is controlled by the ``AZURE_TOKEN_CREDENTIALS``
    environment variable (requires azure-identity >= 1.24.0):

    * On dev boxes set ``AZURE_TOKEN_CREDENTIALS=AzureCliCredential`` to skip
      all probes and go straight to the Azure CLI (avoids WMI hangs on Windows).
    * In Azure-hosted environments set it to ``ManagedIdentityCredential``.
    * When unset, the full DefaultAzureCredential chain is used.

    Requires ``az login`` to have been run beforehand when using
    ``AzureCliCredential``.

    Token verification is deferred to the first real API call -- the retry
    engine handles 401 by refreshing the token automatically.

    Returns a credential object, or calls sys.exit(1) if azure-identity
    is not installed.
    """
    import sys
    _log = log_fn or print
    try:
        from azure.identity import DefaultAzureCredential
        return DefaultAzureCredential()
    except ImportError:
        _log("[CRITICAL] azure-identity package not installed.")
        _log("[CRITICAL] HINT: pip install azure-identity")
        sys.exit(1)


# ---------------------------------------------------------------------------
# DataverseClient
# ---------------------------------------------------------------------------

class DataverseClient:
    """Resilient Dataverse OData client with exponential-backoff retry.

    Parameters
    ----------
    dataverse_url:
        Base Dataverse instance URL.  Defaults to ``DATAVERSE_URL`` env var
        or the built-in default.
    credential:
        An ``azure.identity`` credential object.  If ``None``, a fresh
        ``AzureCliCredential`` is created via ``create_credential()``.
    log_fn:
        Logging callback ``(str) -> None``.  Defaults to ``print``.
    request_timeout:
        Per-request HTTP timeout in seconds.
    max_retry_seconds:
        Default retry budget in seconds (15 minutes).  Individual calls
        can override this.
    """

    def __init__(
        self,
        dataverse_url: str | None = None,
        credential=None,
        log_fn=None,
        request_timeout: int = 30,
        max_retry_seconds: int = 900,
    ):
        self.dataverse_url = (
            dataverse_url
            or os.environ.get("DATAVERSE_URL", DEFAULT_DATAVERSE_URL)
        )
        self.api_version = DEFAULT_API_VERSION
        self.api_base = f"{self.dataverse_url}/api/data/{self.api_version}"
        self.request_timeout = request_timeout
        self.max_retry_seconds = max_retry_seconds
        self.log_fn = log_fn or print
        if credential is not None:
            self.credential = credential
        else:
            self.credential = create_credential(log_fn=self.log_fn)

        # Token cache
        self._token: str | None = None
        self._token_expires: datetime | None = None

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def get_token(self) -> str:
        """Return a cached bearer token, refreshing if expired.

        Uses ``call_with_timeout`` with a 30-second hard deadline so a hung
        credential provider does not block the worker indefinitely.

        Raises
        ------
        TimeoutError
            If the credential provider does not respond within 30 seconds.
        Exception
            Any exception raised by the credential provider.
        """
        if self._token and self._token_expires:
            if datetime.now(timezone.utc) < self._token_expires:
                return self._token

        resource = f"{self.dataverse_url}/.default"
        access_token = call_with_timeout(
            lambda: self.credential.get_token(resource),
            timeout_sec=30,
            description="credential.get_token()",
        )

        self._token = access_token.token
        # Expire 5 minutes early to be safe
        self._token_expires = (
            datetime.fromtimestamp(access_token.expires_on, tz=timezone.utc)
            - timedelta(minutes=5)
        )
        return self._token

    def _refresh_token(self) -> str:
        """Clear the cached token and re-acquire a fresh one."""
        self._token = None
        self._token_expires = None
        return self.get_token()

    # ------------------------------------------------------------------
    # Header construction
    # ------------------------------------------------------------------

    def _build_headers(
        self,
        content_type: str | None = None,
        etag: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Build OData headers using the current token."""
        token = self.get_token()
        headers: dict[str, str] = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "OData-MaxVersion": "4.0",
            "OData-Version": "4.0",
        }
        if content_type:
            headers["Content-Type"] = content_type
        if etag:
            headers["If-Match"] = etag
        if extra_headers:
            headers.update(extra_headers)
        return headers

    # ------------------------------------------------------------------
    # Core retry engine
    # ------------------------------------------------------------------

    def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        json_data=None,
        params=None,
        content_type: str | None = None,
        etag: str | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: int | None = None,
        max_retry_seconds: int | None = None,
    ) -> requests.Response:
        """Execute an HTTP request with exponential-backoff retry.

        Retries on transient HTTP errors (429, 5xx) and network errors.
        Respects ``Retry-After`` on 429.  Refreshes the token once on 401.
        Raises ``ETagConflictError`` immediately on 412.
        Raises ``DataverseError`` immediately on other 4xx.
        Raises ``DataverseRetryExhausted`` if the budget is exhausted.
        """
        req_timeout = timeout or self.request_timeout
        budget = max_retry_seconds if max_retry_seconds is not None else self.max_retry_seconds
        deadline = time.monotonic() + budget

        delay = 1.0  # initial backoff
        attempt = 0
        token_refreshed = False
        last_error: Exception | None = None

        while True:
            attempt += 1

            # Build fresh headers each attempt (token may have been refreshed)
            try:
                headers = self._build_headers(
                    content_type=content_type,
                    etag=etag,
                    extra_headers=extra_headers,
                )
            except Exception as exc:
                last_error = exc
                remaining = deadline - time.monotonic()
                if remaining <= delay:
                    raise DataverseRetryExhausted(
                        f"Token acquisition failed after {attempt} attempt(s), "
                        f"budget exhausted",
                        last_error=last_error,
                    ) from exc
                self.log_fn(
                    f"[DV-CLIENT] Token error on attempt {attempt}, "
                    f"retrying in {delay:.0f}s ({remaining:.0f}s remaining)"
                )
                time.sleep(delay)
                delay = min(delay * 2, 60.0)
                continue

            try:
                kwargs: dict = {
                    "headers": headers,
                    "timeout": req_timeout,
                }
                if json_data is not None:
                    kwargs["json"] = json_data
                if params is not None:
                    kwargs["params"] = params

                resp = requests.request(method, url, **kwargs)

                # --- Success ---
                if resp.status_code < 400:
                    return resp

                # --- 412: ETag conflict -- never retry ---
                if resp.status_code == 412:
                    raise ETagConflictError(
                        f"412 Precondition Failed on {method} {url}: {resp.text}"
                    )

                # --- 401: refresh token once ---
                if resp.status_code == 401:
                    if not token_refreshed:
                        token_refreshed = True
                        self.log_fn(
                            f"[DV-CLIENT] 401 on attempt {attempt}, refreshing token"
                        )
                        self._refresh_token()
                        continue  # retry immediately, no sleep
                    else:
                        raise DataverseError(
                            f"401 Unauthorized after token refresh on {method} {url}",
                            status_code=401,
                            response_text=resp.text,
                        )

                # --- Retryable status codes ---
                if resp.status_code in _RETRYABLE_STATUS_CODES:
                    last_error = DataverseError(
                        f"{resp.status_code} on {method} {url}",
                        status_code=resp.status_code,
                        response_text=resp.text,
                    )

                    # Determine delay -- respect Retry-After on 429
                    sleep_time = delay
                    if resp.status_code == 429:
                        retry_after = resp.headers.get("Retry-After")
                        if retry_after:
                            try:
                                sleep_time = max(float(retry_after), delay)
                            except (ValueError, TypeError):
                                pass

                    remaining = deadline - time.monotonic()
                    if remaining <= sleep_time:
                        raise DataverseRetryExhausted(
                            f"{resp.status_code} on attempt {attempt}, "
                            f"retry budget exhausted ({remaining:.0f}s remaining, "
                            f"need {sleep_time:.0f}s)",
                            last_error=last_error,
                        )

                    self.log_fn(
                        f"[DV-CLIENT] {resp.status_code} on attempt {attempt}, "
                        f"retrying in {sleep_time:.0f}s ({remaining:.0f}s remaining)"
                    )
                    time.sleep(sleep_time)
                    delay = min(delay * 2, 60.0)
                    # Reset token_refreshed so a 401 during a long retry
                    # sequence can refresh again
                    token_refreshed = False
                    continue

                # --- Non-retryable 4xx ---
                raise DataverseError(
                    f"{resp.status_code} on {method} {url}: {resp.text}",
                    status_code=resp.status_code,
                    response_text=resp.text,
                )

            except _RETRYABLE_EXCEPTIONS as exc:
                last_error = exc
                remaining = deadline - time.monotonic()
                if remaining <= delay:
                    raise DataverseRetryExhausted(
                        f"{type(exc).__name__} on attempt {attempt}, "
                        f"retry budget exhausted ({remaining:.0f}s remaining)",
                        last_error=last_error,
                    ) from exc

                self.log_fn(
                    f"[DV-CLIENT] {type(exc).__name__} on attempt {attempt}, "
                    f"retrying in {delay:.0f}s ({remaining:.0f}s remaining)"
                )
                time.sleep(delay)
                delay = min(delay * 2, 60.0)
                token_refreshed = False
                continue

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(
        self,
        url: str,
        *,
        params=None,
        timeout: int | None = None,
        max_retry_seconds: int | None = None,
    ) -> requests.Response:
        """Send a GET request with retry."""
        return self._request_with_retry(
            "GET", url,
            params=params,
            timeout=timeout,
            max_retry_seconds=max_retry_seconds,
        )

    def post(
        self,
        url: str,
        data=None,
        *,
        extra_headers: dict[str, str] | None = None,
        timeout: int | None = None,
        max_retry_seconds: int | None = None,
    ) -> requests.Response:
        """Send a POST request with retry."""
        return self._request_with_retry(
            "POST", url,
            json_data=data,
            content_type="application/json",
            extra_headers=extra_headers,
            timeout=timeout,
            max_retry_seconds=max_retry_seconds,
        )

    def patch(
        self,
        url: str,
        data=None,
        *,
        etag: str | None = None,
        timeout: int | None = None,
        max_retry_seconds: int | None = None,
    ) -> requests.Response:
        """Send a PATCH request with retry."""
        return self._request_with_retry(
            "PATCH", url,
            json_data=data,
            content_type="application/json",
            etag=etag,
            timeout=timeout,
            max_retry_seconds=max_retry_seconds,
        )

    def delete(
        self,
        url: str,
        *,
        timeout: int | None = None,
        max_retry_seconds: int | None = None,
    ) -> requests.Response:
        """Send a DELETE request with retry."""
        return self._request_with_retry(
            "DELETE", url,
            timeout=timeout,
            max_retry_seconds=max_retry_seconds,
        )

    # ------------------------------------------------------------------
    # URL helpers
    # ------------------------------------------------------------------

    def table_url(self, table: str) -> str:
        """Return the OData entity set URL for *table*."""
        return f"{self.api_base}/{table}"

    def row_url(self, table: str, row_id: str) -> str:
        """Return the OData entity URL for a specific row."""
        return f"{self.api_base}/{table}({row_id})"
