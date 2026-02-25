"""
Shared Dataverse helper module for scripts.

Thin wrapper around dv_client.DataverseClient that provides a high-level
CRUD API (get_rows, create_row, update_row, etc.) for standalone scripts.
Auth and HTTP retry are handled by the service-level client -- this module
adds OData query building and response parsing on top.

Typical usage::

    from dv_helpers import DataverseClient

    dv = DataverseClient()
    rows = dv.get_rows("cr_shraga_conversations", filter="cr_status eq 'Unclaimed'", top=10)
    dv.update_row("cr_shraga_conversations", row_id, {"cr_status": "Claimed"}, etag=row["@odata.etag"])
    new_row = dv.create_row("cr_shraga_tasks", {"cr_name": "Do the thing"})
"""
from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any

# ---------------------------------------------------------------------------
# Ensure the workspace root is on sys.path so we can import dv_client
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_WORKSPACE_ROOT = os.path.dirname(_SCRIPTS_DIR)
if _WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, _WORKSPACE_ROOT)

from dv_client import (
    DataverseClient as _ServiceClient,
    DataverseError,
    DataverseRetryExhausted,
    ETagConflictError,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default configuration (kept for backward compatibility)
# ---------------------------------------------------------------------------
DEFAULT_DATAVERSE_URL = "https://org3e79cdb1.crm3.dynamics.com"
DEFAULT_API_VERSION = "v9.2"
DEFAULT_REQUEST_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Static token credential for testing
# ---------------------------------------------------------------------------

class _StaticTokenCredential:
    """Azure credential stub that always returns a fixed token. For testing."""

    def __init__(self, token: str):
        self._token = token

    def get_token(self, *scopes, **kwargs):
        from collections import namedtuple
        AccessToken = namedtuple("AccessToken", ["token", "expires_on"])
        return AccessToken(token=self._token, expires_on=int(time.time()) + 3600)


# ---------------------------------------------------------------------------
# DataverseClient -- high-level CRUD API backed by dv_client
# ---------------------------------------------------------------------------

class DataverseClient:
    """High-level Dataverse CRUD client backed by dv_client.DataverseClient.

    Same constructor signature and public API as the previous standalone
    implementation, but auth and HTTP retry are delegated to the resilient
    service-level client with WMI hang protection.

    Parameters
    ----------
    dataverse_url:
        Base Dataverse instance URL.  Defaults to ``DATAVERSE_URL`` env var
        or the built-in default.
    api_version:
        OData API version string (default ``v9.2``).
    token:
        Pre-fetched bearer token.  If ``None``, the client will obtain one
        via ``create_credential()``.  Passing a token is useful for testing.
    timeout:
        HTTP request timeout in seconds.
    """

    def __init__(
        self,
        dataverse_url: str | None = None,
        api_version: str = DEFAULT_API_VERSION,
        token: str | None = None,
        timeout: int = DEFAULT_REQUEST_TIMEOUT,
    ):
        self.dataverse_url = (
            dataverse_url
            or os.environ.get("DATAVERSE_URL", DEFAULT_DATAVERSE_URL)
        )
        self.api_version = api_version
        self.timeout = timeout

        # Build the service-level client for auth + HTTP
        kwargs: dict = {
            "dataverse_url": self.dataverse_url,
            "log_fn": logger.info,
            "request_timeout": timeout,
            "max_retry_seconds": 120,  # scripts use shorter retry budget
        }
        if token:
            kwargs["credential"] = _StaticTokenCredential(token)

        self._client = _ServiceClient(**kwargs)

        # Sync api_version if non-default was requested
        if api_version != DEFAULT_API_VERSION:
            self._client.api_version = api_version
            self._client.api_base = f"{self.dataverse_url}/api/data/{api_version}"

        self.api_base = self._client.api_base

    # -- Public CRUD -------------------------------------------------------

    def get_rows(
        self,
        table: str,
        *,
        filter: str | None = None,
        select: str | None = None,
        orderby: str | None = None,
        top: int | None = None,
        expand: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query rows from a Dataverse table.

        Returns the ``value`` array from the OData response.
        """
        params: list[str] = []
        if filter:
            params.append(f"$filter={filter}")
        if select:
            params.append(f"$select={select}")
        if orderby:
            params.append(f"$orderby={orderby}")
        if top is not None:
            params.append(f"$top={top}")
        if expand:
            params.append(f"$expand={expand}")

        url = self._client.table_url(table)
        if params:
            url += "?" + "&".join(params)

        resp = self._client.get(url, timeout=self.timeout, max_retry_seconds=120)
        return resp.json().get("value", [])

    def get_row(
        self,
        table: str,
        row_id: str,
        *,
        select: str | None = None,
    ) -> dict[str, Any]:
        """Fetch a single row by its primary key."""
        url = self._client.row_url(table, row_id)
        if select:
            url += f"?$select={select}"

        resp = self._client.get(url, timeout=self.timeout, max_retry_seconds=120)
        return resp.json()

    def create_row(
        self,
        table: str,
        data: dict[str, Any],
        *,
        return_representation: bool = True,
    ) -> dict[str, Any] | None:
        """Create a new row in a Dataverse table.

        Returns the created row dict, or ``None`` on 204 No Content.
        When the server returns 204 with an ``OData-EntityId`` header,
        a minimal dict with the extracted ID is returned.
        """
        extra = {}
        if return_representation:
            extra["Prefer"] = "return=representation"

        resp = self._client.post(
            self._client.table_url(table),
            data=data,
            extra_headers=extra if extra else None,
            timeout=self.timeout,
            max_retry_seconds=120,
        )

        if resp.status_code == 204 or not resp.content:
            entity_id_header = resp.headers.get("OData-EntityId", "")
            if "(" in entity_id_header:
                extracted_id = entity_id_header.split("(")[-1].rstrip(")")
                return {"_extracted_id": extracted_id}
            return None

        return resp.json()

    def update_row(
        self,
        table: str,
        row_id: str,
        data: dict[str, Any],
        *,
        etag: str | None = None,
    ) -> bool:
        """Update (PATCH) an existing row.

        Returns ``True`` on success, ``False`` on 412 concurrency conflict.
        """
        try:
            self._client.patch(
                self._client.row_url(table, row_id),
                data=data,
                etag=etag,
                timeout=self.timeout,
                max_retry_seconds=120,
            )
            return True
        except ETagConflictError:
            logger.info(
                "Optimistic concurrency conflict on %s(%s) -- row was modified",
                table, row_id,
            )
            return False

    def delete_row(
        self,
        table: str,
        row_id: str,
    ) -> bool:
        """Delete a row from a Dataverse table."""
        self._client.delete(
            self._client.row_url(table, row_id),
            timeout=self.timeout,
            max_retry_seconds=120,
        )
        return True

    # -- Convenience methods -----------------------------------------------

    @staticmethod
    def sanitize_odata(value: str) -> str:
        """Escape a string for safe use in OData single-quoted literals.

        Doubles any embedded single quotes to prevent OData injection.
        """
        return value.replace("'", "''")

    def find_rows(
        self,
        table: str,
        column: str,
        value: str,
        *,
        top: int = 1,
        select: str | None = None,
    ) -> list[dict[str, Any]]:
        """Find rows where *column* equals *value*.

        A convenience wrapper around :meth:`get_rows` for the common
        ``$filter=column eq 'value'`` pattern.
        """
        safe_value = self.sanitize_odata(value)
        return self.get_rows(
            table,
            filter=f"{column} eq '{safe_value}'",
            select=select,
            top=top,
        )

    def upsert_row(
        self,
        table: str,
        row_id: str,
        data: dict[str, Any],
    ) -> bool:
        """Create-or-update a row using Dataverse UPSERT semantics.

        Sends a PATCH without ETag so that Dataverse creates the row if
        it does not exist, or updates it if it does.
        """
        self._client.patch(
            self._client.row_url(table, row_id),
            data=data,
            timeout=self.timeout,
            max_retry_seconds=120,
        )
        return True


# ---------------------------------------------------------------------------
# Module-level convenience functions (for quick one-off scripts)
# ---------------------------------------------------------------------------

_default_client: DataverseClient | None = None


def _get_default_client() -> DataverseClient:
    """Return (and cache) a module-level default client."""
    global _default_client
    if _default_client is None:
        _default_client = DataverseClient()
    return _default_client


def get_rows(table: str, **kwargs) -> list[dict[str, Any]]:
    """Module-level convenience for :meth:`DataverseClient.get_rows`."""
    return _get_default_client().get_rows(table, **kwargs)


def get_row(table: str, row_id: str, **kwargs) -> dict[str, Any]:
    """Module-level convenience for :meth:`DataverseClient.get_row`."""
    return _get_default_client().get_row(table, row_id, **kwargs)


def create_row(table: str, data: dict[str, Any], **kwargs) -> dict[str, Any] | None:
    """Module-level convenience for :meth:`DataverseClient.create_row`."""
    return _get_default_client().create_row(table, data, **kwargs)


def update_row(table: str, row_id: str, data: dict[str, Any], **kwargs) -> bool:
    """Module-level convenience for :meth:`DataverseClient.update_row`."""
    return _get_default_client().update_row(table, row_id, data, **kwargs)


def delete_row(table: str, row_id: str) -> bool:
    """Module-level convenience for :meth:`DataverseClient.delete_row`."""
    return _get_default_client().delete_row(table, row_id)
