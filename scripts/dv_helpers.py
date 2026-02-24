"""
Shared Dataverse helper module.

Provides reusable functions for interacting with Microsoft Dataverse (CRM)
tables via the OData v9.2 REST API.  All scripts and managers that talk to
Dataverse should import from here instead of duplicating auth, header
construction, and HTTP logic.

Typical usage::

    from scripts.dv_helpers import DataverseClient

    dv = DataverseClient()
    rows = dv.get_rows("cr_shraga_conversations", filter="cr_status eq 'Unclaimed'", top=10)
    dv.update_row("cr_shraga_conversations", row_id, {"cr_status": "Claimed"}, etag=row["@odata.etag"])
    new_row = dv.create_row("cr_shraga_tasks", {"cr_name": "Do the thing"})
"""
from __future__ import annotations

import logging
import os
import subprocess
import json
from datetime import datetime, timezone, timedelta
from typing import Any

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default configuration -- overridable via env vars or constructor params
# ---------------------------------------------------------------------------
DEFAULT_DATAVERSE_URL = "https://org3e79cdb1.crm3.dynamics.com"
DEFAULT_API_VERSION = "v9.2"
DEFAULT_REQUEST_TIMEOUT = 30  # seconds


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def get_auth_header(
    dataverse_url: str | None = None,
    token: str | None = None,
) -> dict[str, str]:
    """Return an ``Authorization`` header dict for Dataverse API calls.

    Resolution order for the bearer token:

    1. *token* argument -- use directly if supplied.
    2. ``DATAVERSE_TOKEN`` environment variable.
    3. ``az account get-access-token`` CLI command (requires ``az login``).

    Parameters
    ----------
    dataverse_url:
        The Dataverse instance URL (e.g. ``https://org3e79cdb1.crm3.dynamics.com``).
        Needed to request a token with the correct audience scope.  Falls back
        to ``DATAVERSE_URL`` env var, then to the built-in default.
    token:
        Pre-fetched bearer token.  If provided, no CLI call is made.

    Returns
    -------
    dict
        ``{"Authorization": "Bearer <token>"}``

    Raises
    ------
    RuntimeError
        If no token can be obtained.
    """
    if token:
        return {"Authorization": f"Bearer {token}"}

    # Try env var
    env_token = os.environ.get("DATAVERSE_TOKEN")
    if env_token:
        return {"Authorization": f"Bearer {env_token}"}

    # Fall back to Azure CLI
    dv_url = dataverse_url or os.environ.get("DATAVERSE_URL", DEFAULT_DATAVERSE_URL)
    resource = f"{dv_url}/.default"

    try:
        result = subprocess.run(
            [
                "az", "account", "get-access-token",
                "--resource", dv_url,
                "--query", "accessToken",
                "--output", "tsv",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            fetched = result.stdout.strip()
            return {"Authorization": f"Bearer {fetched}"}
    except FileNotFoundError:
        logger.debug("Azure CLI (az) not found on PATH")
    except subprocess.TimeoutExpired:
        logger.warning("Azure CLI token request timed out")
    except Exception as exc:
        logger.debug("Azure CLI token request failed: %s", exc)

    # Try DefaultAzureCredential as a last resort
    try:
        from azure.identity import DefaultAzureCredential

        cred = DefaultAzureCredential()
        access_token = cred.get_token(resource)
        return {"Authorization": f"Bearer {access_token.token}"}
    except Exception as exc:
        logger.debug("DefaultAzureCredential failed: %s", exc)

    raise RuntimeError(
        "Could not obtain a Dataverse access token.  "
        "Provide a token directly, set DATAVERSE_TOKEN, "
        "or run 'az login' first."
    )


def _build_odata_headers(
    auth_header: dict[str, str],
    content_type: str | None = None,
    etag: str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Construct the full OData header set expected by Dataverse.

    Parameters
    ----------
    auth_header:
        The ``{"Authorization": "Bearer ..."}`` dict.
    content_type:
        If given, sets ``Content-Type`` (e.g. ``application/json``).
    etag:
        If given, sets ``If-Match`` for optimistic concurrency control.
    extra:
        Additional headers to merge in (e.g. ``Prefer``).
    """
    headers = {
        **auth_header,
        "Accept": "application/json",
        "OData-MaxVersion": "4.0",
        "OData-Version": "4.0",
    }
    if content_type:
        headers["Content-Type"] = content_type
    if etag:
        headers["If-Match"] = etag
    if extra:
        headers.update(extra)
    return headers


# ---------------------------------------------------------------------------
# DataverseClient -- stateful client with token caching
# ---------------------------------------------------------------------------

class DataverseClient:
    """High-level Dataverse OData client with auth caching and ETag support.

    Parameters
    ----------
    dataverse_url:
        Base Dataverse instance URL.  Defaults to ``DATAVERSE_URL`` env var
        or the built-in default.
    api_version:
        OData API version string (default ``v9.2``).
    token:
        Pre-fetched bearer token.  If ``None``, the client will obtain one
        via :func:`get_auth_header` on first request.
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
        self.api_base = f"{self.dataverse_url}/api/data/{self.api_version}"
        self.timeout = timeout

        # Token cache
        self._token = token
        self._token_expires: datetime | None = None

    # -- internal helpers --------------------------------------------------

    def _get_auth_header(self) -> dict[str, str]:
        """Return a cached or freshly-fetched auth header."""
        # If caller pre-supplied a static token, always use it
        if self._token and self._token_expires is None:
            return {"Authorization": f"Bearer {self._token}"}

        # Check cache expiry
        if (
            self._token
            and self._token_expires
            and datetime.now(timezone.utc) < self._token_expires
        ):
            return {"Authorization": f"Bearer {self._token}"}

        # Fetch fresh token
        auth = get_auth_header(dataverse_url=self.dataverse_url)
        self._token = auth["Authorization"].removeprefix("Bearer ")
        # Cache for 50 minutes (tokens typically last 60-75 min)
        self._token_expires = datetime.now(timezone.utc) + timedelta(minutes=50)
        return auth

    def _headers(
        self,
        content_type: str | None = None,
        etag: str | None = None,
        extra: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Build the full OData headers for a request."""
        return _build_odata_headers(
            auth_header=self._get_auth_header(),
            content_type=content_type,
            etag=etag,
            extra=extra,
        )

    def _table_url(self, table: str) -> str:
        """Return the base URL for a table (entity set)."""
        return f"{self.api_base}/{table}"

    def _row_url(self, table: str, row_id: str) -> str:
        """Return the URL for a specific row."""
        return f"{self.api_base}/{table}({row_id})"

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

        Parameters
        ----------
        table:
            Logical table name (e.g. ``cr_shraga_conversations``).
        filter:
            OData ``$filter`` expression.
        select:
            Comma-separated column list for ``$select``.
        orderby:
            OData ``$orderby`` expression.
        top:
            Maximum number of rows to return.
        expand:
            OData ``$expand`` expression.

        Returns
        -------
        list[dict]
            The ``value`` array from the OData response.  Each dict includes
            ``@odata.etag`` which can be passed to :meth:`update_row` for
            optimistic concurrency.

        Raises
        ------
        requests.HTTPError
            On non-2xx responses.
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

        url = self._table_url(table)
        if params:
            url += "?" + "&".join(params)

        resp = requests.get(
            url,
            headers=self._headers(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json().get("value", [])

    def get_row(
        self,
        table: str,
        row_id: str,
        *,
        select: str | None = None,
    ) -> dict[str, Any]:
        """Fetch a single row by its primary key.

        Parameters
        ----------
        table:
            Logical table name.
        row_id:
            GUID primary key of the row.
        select:
            Optional comma-separated column list.

        Returns
        -------
        dict
            The row data including ``@odata.etag``.

        Raises
        ------
        requests.HTTPError
            On non-2xx responses (including 404 if not found).
        """
        url = self._row_url(table, row_id)
        if select:
            url += f"?$select={select}"

        resp = requests.get(
            url,
            headers=self._headers(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def create_row(
        self,
        table: str,
        data: dict[str, Any],
        *,
        return_representation: bool = True,
    ) -> dict[str, Any] | None:
        """Create a new row in a Dataverse table.

        Parameters
        ----------
        table:
            Logical table name.
        data:
            Column values to set on the new row.
        return_representation:
            If ``True`` (default), requests that Dataverse return the created
            row in the response body (adds ``Prefer: return=representation``).

        Returns
        -------
        dict or None
            The created row (if ``return_representation`` is True and the
            server responds with a body), or ``None`` on 204 No Content.
            When the server returns 204 but includes an ``OData-EntityId``
            header, a minimal dict with the extracted ID is returned.

        Raises
        ------
        requests.HTTPError
            On non-2xx responses.
        """
        extra = {}
        if return_representation:
            extra["Prefer"] = "return=representation"

        resp = requests.post(
            self._table_url(table),
            headers=self._headers(
                content_type="application/json",
                extra=extra,
            ),
            json=data,
            timeout=self.timeout,
        )
        resp.raise_for_status()

        if resp.status_code == 204 or not resp.content:
            # Try to extract the row ID from the OData-EntityId header
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

        Parameters
        ----------
        table:
            Logical table name.
        row_id:
            GUID primary key of the row to update.
        data:
            Column values to update.
        etag:
            If provided, sends ``If-Match: <etag>`` for optimistic concurrency.
            The update will fail with HTTP 412 if the row has been modified
            since the ETag was obtained.

        Returns
        -------
        bool
            ``True`` if the update succeeded, ``False`` if a concurrency
            conflict occurred (HTTP 412).

        Raises
        ------
        requests.HTTPError
            On non-2xx responses other than 412.
        """
        resp = requests.patch(
            self._row_url(table, row_id),
            headers=self._headers(
                content_type="application/json",
                etag=etag,
            ),
            json=data,
            timeout=self.timeout,
        )

        if resp.status_code == 412:
            logger.info(
                "Optimistic concurrency conflict on %s(%s) -- row was modified",
                table,
                row_id,
            )
            return False

        resp.raise_for_status()
        return True

    def delete_row(
        self,
        table: str,
        row_id: str,
    ) -> bool:
        """Delete a row from a Dataverse table.

        Parameters
        ----------
        table:
            Logical table name.
        row_id:
            GUID primary key of the row to delete.

        Returns
        -------
        bool
            ``True`` on success.

        Raises
        ------
        requests.HTTPError
            On non-2xx responses.
        """
        resp = requests.delete(
            self._row_url(table, row_id),
            headers=self._headers(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
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

        Sends a PATCH without ``If-Match`` so that Dataverse creates the
        row if it does not exist, or updates it if it does.

        Returns ``True`` on success.
        """
        resp = requests.patch(
            self._row_url(table, row_id),
            headers=self._headers(content_type="application/json"),
            json=data,
            timeout=self.timeout,
        )
        resp.raise_for_status()
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
