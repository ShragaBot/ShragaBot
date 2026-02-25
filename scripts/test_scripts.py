"""
Tests for scripts/dv_helpers.py -- Shared Dataverse helper module.

Tests the high-level CRUD API (get_rows, create_row, update_row, etc.)
by mocking the underlying dv_client.DataverseClient methods.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import pytest

# Ensure the workspace root and scripts directory are importable
WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, WORKSPACE_ROOT)
sys.path.insert(0, SCRIPTS_DIR)

from dv_helpers import (
    DataverseClient,
    DataverseError,
    ETagConflictError,
    get_rows,
    get_row,
    create_row,
    update_row,
    delete_row,
    DEFAULT_DATAVERSE_URL,
    DEFAULT_REQUEST_TIMEOUT,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FAKE_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.FAKE_TOKEN_FOR_TESTING"
FAKE_ETAG = 'W/"12345678"'
TEST_TABLE = "cr_shraga_conversations"
TEST_ROW_ID = "00000000-1111-2222-3333-444444444444"


def make_client(token: str = FAKE_TOKEN, **kwargs) -> DataverseClient:
    """Create a DataverseClient with a pre-set token so no auth calls happen."""
    return DataverseClient(token=token, **kwargs)


def make_odata_response(rows: list[dict], status_code: int = 200) -> MagicMock:
    """Create a mock requests.Response with the standard OData shape."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.content = json.dumps({"value": rows}).encode()
    mock_resp.json.return_value = {"value": rows}
    mock_resp.headers = {}
    return mock_resp


def make_single_row_response(row: dict, status_code: int = 200) -> MagicMock:
    """Create a mock response for a single-row GET or POST."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.content = json.dumps(row).encode()
    mock_resp.json.return_value = row
    mock_resp.headers = {}
    return mock_resp


def make_204_response(entity_id: str = "") -> MagicMock:
    """Create a mock 204 No Content response."""
    mock_resp = MagicMock()
    mock_resp.status_code = 204
    mock_resp.content = b""
    mock_resp.headers = {}
    if entity_id:
        mock_resp.headers["OData-EntityId"] = (
            f"https://org.crm3.dynamics.com/api/data/v9.2/{TEST_TABLE}({entity_id})"
        )
    return mock_resp


def make_patch_response(status_code: int = 204) -> MagicMock:
    """Create a mock response for PATCH requests."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.content = b""
    mock_resp.headers = {}
    return mock_resp


# ---------------------------------------------------------------------------
# Tests: DataverseClient.get_rows
# ---------------------------------------------------------------------------

class TestDvHelpersGetRows:
    """Tests for get_rows CRUD method."""

    def test_get_rows_basic(self):
        """get_rows should return the 'value' array from the OData response."""
        sample_rows = [
            {"cr_shraga_conversationid": "id-1", "cr_status": "Unclaimed", "@odata.etag": '"e1"'},
            {"cr_shraga_conversationid": "id-2", "cr_status": "Claimed", "@odata.etag": '"e2"'},
        ]
        client = make_client()
        client._client.get = MagicMock(return_value=make_odata_response(sample_rows))

        rows = client.get_rows(TEST_TABLE)

        assert len(rows) == 2
        assert rows[0]["cr_shraga_conversationid"] == "id-1"
        assert rows[1]["cr_status"] == "Claimed"

    def test_get_rows_with_filter(self):
        """get_rows should include $filter in the URL."""
        client = make_client()
        client._client.get = MagicMock(return_value=make_odata_response([]))

        client.get_rows(
            TEST_TABLE,
            filter="cr_status eq 'Unclaimed'",
            top=5,
            orderby="createdon asc",
        )

        called_url = client._client.get.call_args[0][0]
        assert "$filter=cr_status eq 'Unclaimed'" in called_url
        assert "$top=5" in called_url
        assert "$orderby=createdon asc" in called_url

    def test_get_rows_with_select(self):
        """get_rows should include $select in the URL."""
        client = make_client()
        client._client.get = MagicMock(return_value=make_odata_response([]))

        client.get_rows(
            TEST_TABLE,
            select="cr_shraga_conversationid,cr_status",
        )

        called_url = client._client.get.call_args[0][0]
        assert "$select=cr_shraga_conversationid,cr_status" in called_url

    def test_get_rows_empty_result(self):
        """get_rows should return an empty list when no rows match."""
        client = make_client()
        client._client.get = MagicMock(return_value=make_odata_response([]))

        rows = client.get_rows(TEST_TABLE, filter="cr_status eq 'Nonexistent'")

        assert rows == []

    def test_get_rows_preserves_etags(self):
        """get_rows should preserve @odata.etag in returned rows."""
        sample = [{"id": "1", "@odata.etag": FAKE_ETAG}]
        client = make_client()
        client._client.get = MagicMock(return_value=make_odata_response(sample))

        rows = client.get_rows(TEST_TABLE)

        assert rows[0]["@odata.etag"] == FAKE_ETAG

    def test_get_rows_raises_on_error(self):
        """get_rows should propagate DataverseError on failure."""
        client = make_client()
        client._client.get = MagicMock(
            side_effect=DataverseError("401 Unauthorized", status_code=401, response_text="")
        )

        with pytest.raises(DataverseError):
            client.get_rows(TEST_TABLE)


# ---------------------------------------------------------------------------
# Tests: DataverseClient.get_row
# ---------------------------------------------------------------------------

class TestGetRow:
    """Tests for get_row (single row fetch by ID)."""

    def test_get_row_by_id(self):
        """get_row should fetch a single row by its GUID."""
        row_data = {
            "cr_shraga_conversationid": TEST_ROW_ID,
            "cr_status": "Claimed",
            "@odata.etag": FAKE_ETAG,
        }
        client = make_client()
        client._client.get = MagicMock(return_value=make_single_row_response(row_data))

        row = client.get_row(TEST_TABLE, TEST_ROW_ID)

        assert row["cr_shraga_conversationid"] == TEST_ROW_ID
        assert row["@odata.etag"] == FAKE_ETAG

        called_url = client._client.get.call_args[0][0]
        assert TEST_ROW_ID in called_url

    def test_get_row_with_select(self):
        """get_row should include $select when specified."""
        client = make_client()
        client._client.get = MagicMock(return_value=make_single_row_response({"id": "x"}))

        client.get_row(TEST_TABLE, TEST_ROW_ID, select="cr_status")

        called_url = client._client.get.call_args[0][0]
        assert "$select=cr_status" in called_url


# ---------------------------------------------------------------------------
# Tests: DataverseClient.create_row
# ---------------------------------------------------------------------------

class TestCreateRow:
    """Tests for create_row."""

    def test_create_row_with_representation(self):
        """create_row should return the created row when server responds with body."""
        created_row = {
            "cr_shraga_conversationid": "new-id-123",
            "cr_name": "Test row",
            "@odata.etag": '"new-etag"',
        }
        client = make_client()
        client._client.post = MagicMock(
            return_value=make_single_row_response(created_row, status_code=201)
        )

        result = client.create_row(TEST_TABLE, {"cr_name": "Test row"})

        assert result["cr_shraga_conversationid"] == "new-id-123"
        # Verify Prefer header was sent
        call_kwargs = client._client.post.call_args[1]
        assert call_kwargs.get("extra_headers", {}).get("Prefer") == "return=representation"

    def test_create_row_204_with_entity_id(self):
        """create_row should extract ID from OData-EntityId header on 204."""
        client = make_client()
        client._client.post = MagicMock(
            return_value=make_204_response(entity_id="extracted-id-456")
        )

        result = client.create_row(TEST_TABLE, {"cr_name": "Test"})

        assert result is not None
        assert result["_extracted_id"] == "extracted-id-456"

    def test_create_row_204_no_entity_id(self):
        """create_row should return None on 204 with no OData-EntityId."""
        client = make_client()
        client._client.post = MagicMock(return_value=make_204_response())

        result = client.create_row(TEST_TABLE, {"cr_name": "Test"})

        assert result is None

    def test_create_row_sends_json_body(self):
        """create_row should send the data dict as the JSON body."""
        client = make_client()
        client._client.post = MagicMock(
            return_value=make_single_row_response({"id": "x"}, status_code=201)
        )

        data = {"cr_name": "My task", "cr_status": "Pending"}
        client.create_row(TEST_TABLE, data)

        call_kwargs = client._client.post.call_args[1]
        assert call_kwargs["data"] == data


# ---------------------------------------------------------------------------
# Tests: DataverseClient.update_row
# ---------------------------------------------------------------------------

class TestDvHelpersUpdateRow:
    """Tests for update_row CRUD method."""

    def test_update_row_success(self):
        """update_row should return True on successful PATCH."""
        client = make_client()
        client._client.patch = MagicMock(return_value=make_patch_response(204))

        result = client.update_row(
            TEST_TABLE,
            TEST_ROW_ID,
            {"cr_status": "Processed"},
        )

        assert result is True

    def test_update_row_with_etag(self):
        """update_row should pass etag to the service client."""
        client = make_client()
        client._client.patch = MagicMock(return_value=make_patch_response(204))

        result = client.update_row(
            TEST_TABLE,
            TEST_ROW_ID,
            {"cr_status": "Claimed"},
            etag=FAKE_ETAG,
        )

        assert result is True
        call_kwargs = client._client.patch.call_args[1]
        assert call_kwargs["etag"] == FAKE_ETAG

    def test_update_row_concurrency_conflict(self):
        """update_row should return False on ETagConflictError (412)."""
        client = make_client()
        client._client.patch = MagicMock(
            side_effect=ETagConflictError("412 Precondition Failed")
        )

        result = client.update_row(
            TEST_TABLE,
            TEST_ROW_ID,
            {"cr_status": "Claimed"},
            etag=FAKE_ETAG,
        )

        assert result is False

    def test_update_row_no_etag(self):
        """update_row without etag should pass etag=None to service client."""
        client = make_client()
        client._client.patch = MagicMock(return_value=make_patch_response(204))

        client.update_row(TEST_TABLE, TEST_ROW_ID, {"cr_status": "Done"})

        call_kwargs = client._client.patch.call_args[1]
        assert call_kwargs.get("etag") is None

    def test_update_row_sends_correct_url(self):
        """update_row should PATCH to the correct entity URL."""
        client = make_client()
        client._client.patch = MagicMock(return_value=make_patch_response(204))

        client.update_row(TEST_TABLE, TEST_ROW_ID, {"cr_status": "Done"})

        called_url = client._client.patch.call_args[0][0]
        assert TEST_TABLE in called_url
        assert TEST_ROW_ID in called_url
        assert called_url.endswith(f"{TEST_TABLE}({TEST_ROW_ID})")

    def test_update_row_dataverse_error_propagates(self):
        """update_row should raise DataverseError on non-412 failures."""
        client = make_client()
        client._client.patch = MagicMock(
            side_effect=DataverseError(
                "500 Internal Server Error", status_code=500, response_text=""
            )
        )

        with pytest.raises(DataverseError):
            client.update_row(TEST_TABLE, TEST_ROW_ID, {"cr_status": "Fail"})


# ---------------------------------------------------------------------------
# Tests: DataverseClient.delete_row
# ---------------------------------------------------------------------------

class TestDeleteRow:
    """Tests for delete_row."""

    def test_delete_row_success(self):
        """delete_row should return True on success."""
        client = make_client()
        client._client.delete = MagicMock(return_value=make_patch_response(204))

        result = client.delete_row(TEST_TABLE, TEST_ROW_ID)

        assert result is True
        called_url = client._client.delete.call_args[0][0]
        assert TEST_ROW_ID in called_url


# ---------------------------------------------------------------------------
# Tests: DataverseClient convenience methods
# ---------------------------------------------------------------------------

class TestConvenienceMethods:
    """Tests for find_rows, upsert_row, and sanitize_odata."""

    def test_find_rows(self):
        """find_rows should build a filter= eq query."""
        client = make_client()
        client._client.get = MagicMock(
            return_value=make_odata_response([{"cr_useremail": "user@test.com"}])
        )

        rows = client.find_rows(
            "crb3b_shragausers",
            "crb3b_useremail",
            "user@test.com",
        )

        assert len(rows) == 1
        called_url = client._client.get.call_args[0][0]
        assert "crb3b_useremail eq 'user@test.com'" in called_url

    def test_upsert_row(self):
        """upsert_row should PATCH without etag (Dataverse UPSERT)."""
        client = make_client()
        client._client.patch = MagicMock(return_value=make_patch_response(204))

        result = client.upsert_row(
            TEST_TABLE, TEST_ROW_ID, {"cr_status": "Processed"}
        )

        assert result is True
        call_kwargs = client._client.patch.call_args[1]
        assert call_kwargs.get("etag") is None

    def test_sanitize_odata(self):
        """sanitize_odata should double single quotes."""
        assert DataverseClient.sanitize_odata("it's a test") == "it''s a test"
        assert DataverseClient.sanitize_odata("no quotes") == "no quotes"


# ---------------------------------------------------------------------------
# Tests: DataverseClient configuration
# ---------------------------------------------------------------------------

class TestClientConfiguration:
    """Tests for client initialization and URL construction."""

    @patch.dict(os.environ, {}, clear=False)
    def test_default_url(self):
        """Client should use the default Dataverse URL when no env var is set."""
        os.environ.pop("DATAVERSE_URL", None)
        client = make_client()
        assert client.dataverse_url == DEFAULT_DATAVERSE_URL
        assert "/api/data/v9.2" in client.api_base

    def test_custom_url(self):
        """Client should accept a custom Dataverse URL."""
        client = DataverseClient(
            dataverse_url="https://custom.crm.dynamics.com",
            token=FAKE_TOKEN,
        )
        assert client.dataverse_url == "https://custom.crm.dynamics.com"
        assert client.api_base == "https://custom.crm.dynamics.com/api/data/v9.2"

    @patch.dict(os.environ, {"DATAVERSE_URL": "https://env.crm.dynamics.com"})
    def test_env_url(self):
        """Client should read DATAVERSE_URL from environment."""
        client = DataverseClient(token=FAKE_TOKEN)
        assert client.dataverse_url == "https://env.crm.dynamics.com"

    def test_custom_timeout(self):
        """Client should accept a custom timeout."""
        client = DataverseClient(token=FAKE_TOKEN, timeout=60)
        assert client.timeout == 60

    def test_custom_api_version(self):
        """Client should accept a custom API version."""
        client = DataverseClient(token=FAKE_TOKEN, api_version="v9.1")
        assert "v9.1" in client.api_base


# ---------------------------------------------------------------------------
# Tests: Module-level convenience functions
# ---------------------------------------------------------------------------

class TestModuleLevelFunctions:
    """Tests for the module-level get_rows, create_row, update_row wrappers."""

    def test_module_get_rows(self):
        """Module-level get_rows should work via the default client."""
        import dv_helpers

        mock_client = make_client()
        mock_client._client.get = MagicMock(
            return_value=make_odata_response([{"id": "1", "name": "test"}])
        )
        dv_helpers._default_client = mock_client

        try:
            rows = get_rows(TEST_TABLE, filter="cr_status eq 'Open'")
            assert len(rows) == 1
        finally:
            dv_helpers._default_client = None

    def test_module_update_row(self):
        """Module-level update_row should delegate to the default client."""
        import dv_helpers

        mock_client = make_client()
        mock_client._client.patch = MagicMock(return_value=make_patch_response(204))
        dv_helpers._default_client = mock_client

        try:
            result = update_row(TEST_TABLE, TEST_ROW_ID, {"cr_status": "Done"})
            assert result is True
        finally:
            dv_helpers._default_client = None


# ---------------------------------------------------------------------------
# Tests: ETag / Optimistic Concurrency integration scenario
# ---------------------------------------------------------------------------

class TestETagWorkflow:
    """End-to-end ETag workflow: read row, get etag, update with etag."""

    def test_claim_message_pattern(self):
        """Simulate the claim-message pattern from global_manager/task_manager.

        1. GET rows (includes @odata.etag)
        2. PATCH with If-Match to atomically claim
        """
        messages = [
            {
                "cr_shraga_conversationid": "msg-001",
                "cr_status": "Unclaimed",
                "cr_message": "Hello",
                "@odata.etag": '"version-abc"',
            },
        ]
        client = make_client()
        client._client.get = MagicMock(return_value=make_odata_response(messages))
        client._client.patch = MagicMock(return_value=make_patch_response(204))

        # Step 1: GET unclaimed messages
        rows = client.get_rows(
            TEST_TABLE,
            filter="cr_status eq 'Unclaimed'",
            top=10,
        )

        assert len(rows) == 1
        msg = rows[0]
        etag = msg["@odata.etag"]
        row_id = msg["cr_shraga_conversationid"]

        # Step 2: PATCH to claim with ETag
        result = client.update_row(
            TEST_TABLE,
            row_id,
            {"cr_status": "Claimed", "cr_claimed_by": "personal:user@test.com"},
            etag=etag,
        )

        assert result is True
        call_kwargs = client._client.patch.call_args[1]
        assert call_kwargs["etag"] == '"version-abc"'

    def test_claim_loses_to_another_manager(self):
        """When another manager claims first, update_row returns False (412)."""
        messages = [
            {
                "cr_shraga_conversationid": "msg-002",
                "cr_status": "Unclaimed",
                "@odata.etag": '"version-xyz"',
            },
        ]

        client = make_client()
        client._client.get = MagicMock(return_value=make_odata_response(messages))
        client._client.patch = MagicMock(
            side_effect=ETagConflictError("412 Precondition Failed")
        )

        rows = client.get_rows(TEST_TABLE, filter="cr_status eq 'Unclaimed'")
        msg = rows[0]

        result = client.update_row(
            TEST_TABLE,
            msg["cr_shraga_conversationid"],
            {"cr_status": "Claimed"},
            etag=msg["@odata.etag"],
        )

        assert result is False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
