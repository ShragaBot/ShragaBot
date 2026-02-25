"""
Tests for scripts/update_flow.py -- Power Automate flow deployment mechanism.

Covers:
  - Flow JSON loading, validation, and structural checks
  - Connection reference validation against the known registry
  - Flow registry resolution by name and by ID
  - GUID format validation
  - Deploy command (single flow) with dry-run and live modes
  - Deploy-all batch command
  - Export command
  - Validate command
  - List command
  - Error handling for missing files, invalid JSON, auth failures
  - CLI argument parser construction

All external dependencies (Azure auth, Power Automate API) are fully mocked.
"""
from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

import pytest

# Ensure the scripts directory is importable
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE_ROOT = os.path.dirname(SCRIPTS_DIR)
sys.path.insert(0, SCRIPTS_DIR)
sys.path.insert(0, WORKSPACE_ROOT)

import update_flow


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

FAKE_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.fake-pa-token"

MINIMAL_VALID_FLOW = {
    "name": "da211a8a-3ef5-4291-bd91-67c4e6e88aec",
    "properties": {
        "displayName": "TaskCompleted",
        "definition": {
            "$schema": "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#",
            "contentVersion": "1.0.0.0",
            "triggers": {
                "When_row_updated": {"type": "OpenApiConnectionWebhook"}
            },
            "actions": {
                "Send_Card": {"type": "ApiConnection"},
                "Update_Row": {"type": "ApiConnection"},
            },
        },
        "connectionReferences": {
            "shared_commondataserviceforapps": {
                "connectionName": "57aef69c3763444e8cfb3b0b5ba18fea",
                "source": "Embedded",
            },
            "shared_teams": {
                "connectionName": "70d2dee52a344508a14a40ee6013baf1",
                "source": "Embedded",
            },
        },
        "state": "Started",
    },
}

FLOW_NO_DEFINITION = {
    "name": "da211a8a-3ef5-4291-bd91-67c4e6e88aec",
    "properties": {
        "displayName": "BadFlow",
        "connectionReferences": {},
    },
}

FLOW_NO_PROPERTIES = {
    "name": "da211a8a-3ef5-4291-bd91-67c4e6e88aec",
}

FLOW_BAD_CONN_REF = {
    "name": "da211a8a-3ef5-4291-bd91-67c4e6e88aec",
    "properties": {
        "displayName": "BadConnFlow",
        "definition": {
            "$schema": "...",
            "triggers": {"t": {}},
            "actions": {"a": {}},
        },
        "connectionReferences": {
            "shared_commondataserviceforapps": {
                "connectionName": "WRONG-connection-name",
                "source": "Embedded",
            },
        },
    },
}


def make_api_response(
    status_code: int = 200,
    json_data: dict | None = None,
    text: str = "",
) -> MagicMock:
    """Create a mock requests.Response."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = json_data or {}
    mock_resp.text = text or json.dumps(json_data or {})
    return mock_resp


# ---------------------------------------------------------------------------
# Tests: FLOW_REGISTRY
# ---------------------------------------------------------------------------

class TestFlowRegistry:
    """Tests for the FLOW_REGISTRY constant."""

    def test_registry_has_7_flows(self):
        """The registry must contain exactly 7 flows."""
        assert len(update_flow.FLOW_REGISTRY) == 7

    def test_all_flows_have_required_keys(self):
        """Every registry entry must have id, json_file, description."""
        for name, entry in update_flow.FLOW_REGISTRY.items():
            assert "id" in entry, f"{name} missing 'id'"
            assert "json_file" in entry, f"{name} missing 'json_file'"
            assert "description" in entry, f"{name} missing 'description'"

    def test_all_flow_ids_are_guids(self):
        """Every flow ID must be a valid GUID format."""
        for name, entry in update_flow.FLOW_REGISTRY.items():
            assert update_flow._looks_like_guid(entry["id"]), (
                f"{name} has invalid GUID: {entry['id']}"
            )

    def test_all_json_files_start_with_flows(self):
        """Every json_file path should be in the flows/ directory."""
        for name, entry in update_flow.FLOW_REGISTRY.items():
            assert entry["json_file"].startswith("flows/"), (
                f"{name} json_file should start with 'flows/': {entry['json_file']}"
            )

    def test_known_flow_names(self):
        """The registry should contain the 7 expected flow names."""
        expected = {
            "TaskProgressUpdater",
            "TaskCompleted",
            "TaskFailed",
            "TaskRunner",
            "TaskCanceled",
            "SendMessage",
            "CancelTask",
        }
        assert set(update_flow.FLOW_REGISTRY.keys()) == expected

    def test_no_duplicate_flow_ids(self):
        """All flow IDs must be unique."""
        ids = [entry["id"] for entry in update_flow.FLOW_REGISTRY.values()]
        assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# Tests: KNOWN_CONNECTIONS
# ---------------------------------------------------------------------------

class TestKnownConnections:
    """Tests for the KNOWN_CONNECTIONS constant."""

    def test_has_dataverse_connection(self):
        assert "shared_commondataserviceforapps" in update_flow.KNOWN_CONNECTIONS

    def test_has_teams_connection(self):
        assert "shared_teams" in update_flow.KNOWN_CONNECTIONS

    def test_has_teams_variant_connection(self):
        """TaskRunner uses 'shared_teams-1' as an alias."""
        assert "shared_teams-1" in update_flow.KNOWN_CONNECTIONS

    def test_teams_variant_same_connection_name(self):
        """shared_teams and shared_teams-1 should point to the same connection."""
        assert (
            update_flow.KNOWN_CONNECTIONS["shared_teams"]
            == update_flow.KNOWN_CONNECTIONS["shared_teams-1"]
        )


# ---------------------------------------------------------------------------
# Tests: _looks_like_guid
# ---------------------------------------------------------------------------

class TestLooksLikeGuid:
    """Tests for the GUID format validator."""

    def test_valid_guid(self):
        assert update_flow._looks_like_guid("da211a8a-3ef5-4291-bd91-67c4e6e88aec")

    def test_valid_guid_uppercase(self):
        assert update_flow._looks_like_guid("DA211A8A-3EF5-4291-BD91-67C4E6E88AEC")

    def test_invalid_guid_no_hyphens(self):
        assert not update_flow._looks_like_guid("da211a8a3ef54291bd9167c4e6e88aec")

    def test_invalid_guid_too_short(self):
        assert not update_flow._looks_like_guid("da211a8a-3ef5-4291")

    def test_invalid_guid_not_hex(self):
        assert not update_flow._looks_like_guid("zzzzzzzz-3ef5-4291-bd91-67c4e6e88aec")

    def test_empty_string(self):
        assert not update_flow._looks_like_guid("")


# ---------------------------------------------------------------------------
# Tests: validate_flow_json
# ---------------------------------------------------------------------------

class TestValidateFlowJson:
    """Tests for the flow JSON validator."""

    def test_valid_flow_no_issues(self):
        """A properly structured flow should have zero issues."""
        issues = update_flow.validate_flow_json(MINIMAL_VALID_FLOW)
        assert issues == []

    def test_missing_properties(self):
        """A flow without properties should report an error."""
        issues = update_flow.validate_flow_json(FLOW_NO_PROPERTIES)
        assert any("properties" in i.lower() for i in issues)

    def test_missing_definition(self):
        """A flow without definition should report an error."""
        issues = update_flow.validate_flow_json(FLOW_NO_DEFINITION)
        assert any("definition" in i.lower() for i in issues)

    def test_definition_missing_schema(self):
        """A definition without $schema should be flagged."""
        flow = {
            "name": "da211a8a-3ef5-4291-bd91-67c4e6e88aec",
            "properties": {
                "definition": {
                    "triggers": {},
                    "actions": {},
                },
                "connectionReferences": {},
            },
        }
        issues = update_flow.validate_flow_json(flow)
        assert any("$schema" in i for i in issues)

    def test_definition_missing_triggers(self):
        """A definition without triggers should be flagged."""
        flow = {
            "name": "da211a8a-3ef5-4291-bd91-67c4e6e88aec",
            "properties": {
                "definition": {
                    "$schema": "...",
                    "actions": {},
                },
                "connectionReferences": {},
            },
        }
        issues = update_flow.validate_flow_json(flow)
        assert any("triggers" in i.lower() for i in issues)

    def test_definition_missing_actions(self):
        """A definition without actions should be flagged."""
        flow = {
            "name": "da211a8a-3ef5-4291-bd91-67c4e6e88aec",
            "properties": {
                "definition": {
                    "$schema": "...",
                    "triggers": {},
                },
                "connectionReferences": {},
            },
        }
        issues = update_flow.validate_flow_json(flow)
        assert any("actions" in i.lower() for i in issues)

    def test_wrong_connection_name_flagged(self):
        """A mismatched connectionName should be flagged."""
        issues = update_flow.validate_flow_json(FLOW_BAD_CONN_REF)
        assert any("WRONG-connection-name" in i for i in issues)

    def test_missing_connection_name_flagged(self):
        """A connection reference without connectionName should be flagged."""
        flow = {
            "name": "da211a8a-3ef5-4291-bd91-67c4e6e88aec",
            "properties": {
                "definition": {"$schema": "...", "triggers": {}, "actions": {}},
                "connectionReferences": {
                    "shared_commondataserviceforapps": {
                        "source": "Embedded",
                        # no connectionName
                    }
                },
            },
        }
        issues = update_flow.validate_flow_json(flow)
        assert any("connectionName" in i for i in issues)

    def test_invalid_flow_guid_flagged(self):
        """A flow with a non-GUID name should be flagged."""
        flow = {
            "name": "not-a-guid",
            "properties": {
                "definition": {"$schema": "...", "triggers": {}, "actions": {}},
                "connectionReferences": {},
            },
        }
        issues = update_flow.validate_flow_json(flow)
        assert any("GUID" in i for i in issues)

    def test_unknown_connection_not_flagged(self):
        """A connection reference not in KNOWN_CONNECTIONS should not be flagged for mismatch."""
        flow = {
            "name": "da211a8a-3ef5-4291-bd91-67c4e6e88aec",
            "properties": {
                "definition": {"$schema": "...", "triggers": {}, "actions": {}},
                "connectionReferences": {
                    "shared_custom_connector": {
                        "connectionName": "some-custom-connection",
                        "source": "Embedded",
                    }
                },
            },
        }
        issues = update_flow.validate_flow_json(flow)
        assert issues == []

    def test_connection_ref_not_a_dict_flagged(self):
        """A connectionReference that is not a dict should be flagged."""
        flow = {
            "name": "da211a8a-3ef5-4291-bd91-67c4e6e88aec",
            "properties": {
                "definition": {"$schema": "...", "triggers": {}, "actions": {}},
                "connectionReferences": {
                    "shared_bad": "not a dict"
                },
            },
        }
        issues = update_flow.validate_flow_json(flow)
        assert any("not a dict" in i for i in issues)


# ---------------------------------------------------------------------------
# Tests: extract_definition / extract_connection_references
# ---------------------------------------------------------------------------

class TestExtractors:
    """Tests for extract_definition and extract_connection_references."""

    def test_extract_definition(self):
        defn = update_flow.extract_definition(MINIMAL_VALID_FLOW)
        assert defn is not None
        assert "actions" in defn
        assert "triggers" in defn

    def test_extract_definition_missing(self):
        defn = update_flow.extract_definition(FLOW_NO_PROPERTIES)
        assert defn is None

    def test_extract_connection_references(self):
        refs = update_flow.extract_connection_references(MINIMAL_VALID_FLOW)
        assert "shared_commondataserviceforapps" in refs
        assert "shared_teams" in refs

    def test_extract_connection_references_empty(self):
        flow = {"properties": {}}
        refs = update_flow.extract_connection_references(flow)
        assert refs == {}

    def test_extract_connection_references_no_properties(self):
        refs = update_flow.extract_connection_references({})
        assert refs == {}


# ---------------------------------------------------------------------------
# Tests: resolve_flow
# ---------------------------------------------------------------------------

class TestResolveFlow:
    """Tests for resolving flow by name or ID."""

    def test_resolve_by_name(self):
        fid, fname, entry = update_flow.resolve_flow(flow_name="TaskCompleted")
        assert fid == "da211a8a-3ef5-4291-bd91-67c4e6e88aec"
        assert fname == "TaskCompleted"
        assert entry["json_file"] == "flows/TaskCompleted.json"

    def test_resolve_by_id_registered(self):
        fid, fname, entry = update_flow.resolve_flow(
            flow_id="a4b59d39-a30f-4f4b-a07f-23b5a513bd11"
        )
        assert fname == "TaskFailed"
        assert fid == "a4b59d39-a30f-4f4b-a07f-23b5a513bd11"

    def test_resolve_by_id_unregistered(self):
        fid, fname, entry = update_flow.resolve_flow(
            flow_id="00000000-0000-0000-0000-000000000000"
        )
        assert fid == "00000000-0000-0000-0000-000000000000"
        assert fname == "(unregistered)"

    def test_resolve_unknown_name_exits(self):
        with pytest.raises(SystemExit):
            update_flow.resolve_flow(flow_name="NonexistentFlow")

    def test_resolve_no_args_exits(self):
        with pytest.raises(SystemExit):
            update_flow.resolve_flow()

    def test_resolve_all_7_names(self):
        """Every registered flow name should resolve correctly."""
        for name, expected_entry in update_flow.FLOW_REGISTRY.items():
            fid, fname, entry = update_flow.resolve_flow(flow_name=name)
            assert fid == expected_entry["id"]
            assert fname == name


# ---------------------------------------------------------------------------
# Tests: load_flow_json
# ---------------------------------------------------------------------------

class TestLoadFlowJson:
    """Tests for the JSON file loader."""

    def test_load_valid_json(self, tmp_path):
        """Should load and parse a valid JSON file."""
        json_file = tmp_path / "test.json"
        json_file.write_text(json.dumps(MINIMAL_VALID_FLOW), encoding="utf-8")
        result = update_flow.load_flow_json(str(json_file))
        assert result["name"] == MINIMAL_VALID_FLOW["name"]

    def test_load_missing_file(self):
        """Should raise FileNotFoundError for non-existent file."""
        with pytest.raises(FileNotFoundError):
            update_flow.load_flow_json("/nonexistent/path/file.json")

    def test_load_invalid_json(self, tmp_path):
        """Should raise JSONDecodeError for malformed JSON."""
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("{invalid json", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            update_flow.load_flow_json(str(bad_file))


# ---------------------------------------------------------------------------
# Tests: get_token
# ---------------------------------------------------------------------------

class TestGetToken:
    """Tests for the authentication helper."""

    @patch.dict(os.environ, {"PA_TOKEN": "env-pa-token-123"})
    def test_env_token(self):
        """Should use PA_TOKEN env var when available."""
        token = update_flow.get_token()
        assert token == "env-pa-token-123"

    @patch.dict(os.environ, {}, clear=True)
    def test_default_azure_credential(self):
        """Should fall back to DefaultAzureCredential."""
        os.environ.pop("PA_TOKEN", None)
        mock_cred = MagicMock()
        mock_token = MagicMock()
        mock_token.token = "default-token-456"
        mock_cred.get_token.return_value = mock_token

        mock_default_cred_cls = MagicMock(return_value=mock_cred)

        # DefaultAzureCredential is imported inside get_token() via a local
        # import, so we patch at the azure.identity level
        with patch("azure.identity.DefaultAzureCredential", mock_default_cred_cls):
            token = update_flow.get_token()
        assert token == "default-token-456"

    @patch.dict(os.environ, {}, clear=True)
    def test_all_methods_fail_raises(self):
        """Should raise RuntimeError when all auth methods fail."""
        os.environ.pop("PA_TOKEN", None)
        with patch(
            "azure.identity.DefaultAzureCredential",
            side_effect=Exception("fail"),
        ):
            with pytest.raises(RuntimeError, match="Could not acquire"):
                update_flow.get_token()


# ---------------------------------------------------------------------------
# Tests: _build_flow_url / _build_headers
# ---------------------------------------------------------------------------

class TestApiHelpers:
    """Tests for URL and header builders."""

    def test_build_flow_url(self):
        url = update_flow._build_flow_url("abc-123")
        assert "abc-123" in url
        assert update_flow.ENV_ID in url
        assert "api-version" in url

    def test_build_headers(self):
        headers = update_flow._build_headers("my-token")
        assert headers["Authorization"] == "Bearer my-token"
        assert headers["Content-Type"] == "application/json"


# ---------------------------------------------------------------------------
# Tests: get_flow
# ---------------------------------------------------------------------------

class TestGetFlow:
    """Tests for fetching a flow from the API."""

    @patch("update_flow.requests.get")
    def test_get_flow_success(self, mock_get):
        mock_get.return_value = make_api_response(200, MINIMAL_VALID_FLOW)
        result = update_flow.get_flow("abc-123", FAKE_TOKEN)
        assert result is not None
        assert result["name"] == MINIMAL_VALID_FLOW["name"]

    @patch("update_flow.requests.get")
    def test_get_flow_not_found(self, mock_get):
        mock_get.return_value = make_api_response(404, text="Not found")
        result = update_flow.get_flow("abc-123", FAKE_TOKEN)
        assert result is None

    @patch("update_flow.requests.get")
    def test_get_flow_sends_correct_url(self, mock_get):
        mock_get.return_value = make_api_response(200, MINIMAL_VALID_FLOW)
        update_flow.get_flow("abc-123", FAKE_TOKEN)
        called_url = mock_get.call_args[0][0]
        assert "abc-123" in called_url


# ---------------------------------------------------------------------------
# Tests: patch_flow
# ---------------------------------------------------------------------------

class TestPatchFlow:
    """Tests for PATCHing a flow definition."""

    @patch("update_flow.requests.patch")
    def test_patch_flow_success(self, mock_patch):
        mock_patch.return_value = make_api_response(200)
        resp = update_flow.patch_flow(
            "abc-123",
            {"actions": {}},
            {"conn": {}},
            FAKE_TOKEN,
        )
        assert resp.status_code == 200

    @patch("update_flow.requests.patch")
    def test_patch_flow_sends_body(self, mock_patch):
        mock_patch.return_value = make_api_response(200)
        update_flow.patch_flow("abc-123", {"acts": {}}, {"conns": {}}, FAKE_TOKEN)
        sent_body = mock_patch.call_args[1]["json"]
        assert "properties" in sent_body
        assert sent_body["properties"]["definition"] == {"acts": {}}
        assert sent_body["properties"]["connectionReferences"] == {"conns": {}}

    @patch("update_flow.requests.patch")
    def test_patch_flow_failure(self, mock_patch):
        mock_patch.return_value = make_api_response(400, text="Bad request")
        resp = update_flow.patch_flow("abc-123", {}, {}, FAKE_TOKEN)
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Tests: build_parser
# ---------------------------------------------------------------------------

class TestBuildParser:
    """Tests for the CLI argument parser."""

    def test_parser_has_subcommands(self):
        parser = update_flow.build_parser()
        # Parsing 'list' should work
        args = parser.parse_args(["list"])
        assert args.command == "list"

    def test_validate_subcommand(self):
        parser = update_flow.build_parser()
        args = parser.parse_args(["validate", "--json-file", "test.json"])
        assert args.command == "validate"
        assert args.json_file == "test.json"

    def test_deploy_subcommand_with_name(self):
        parser = update_flow.build_parser()
        args = parser.parse_args(["deploy", "--flow-name", "TaskCompleted", "--dry-run"])
        assert args.command == "deploy"
        assert args.flow_name == "TaskCompleted"
        assert args.dry_run is True

    def test_deploy_subcommand_with_id(self):
        parser = update_flow.build_parser()
        args = parser.parse_args(["deploy", "--flow-id", "abc-123", "--json-file", "f.json"])
        assert args.command == "deploy"
        assert args.flow_id == "abc-123"
        assert args.json_file == "f.json"

    def test_deploy_all_subcommand(self):
        parser = update_flow.build_parser()
        args = parser.parse_args(["deploy-all", "--dry-run"])
        assert args.command == "deploy-all"
        assert args.dry_run is True

    def test_export_subcommand(self):
        parser = update_flow.build_parser()
        args = parser.parse_args(["export", "--flow-name", "TaskRunner", "-o", "out.json"])
        assert args.command == "export"
        assert args.flow_name == "TaskRunner"
        assert args.output == "out.json"

    def test_no_command_does_not_crash(self):
        """Calling main with no args should print help and exit gracefully."""
        with pytest.raises(SystemExit) as exc_info:
            update_flow.main([])
        assert exc_info.value.code == 0


# ---------------------------------------------------------------------------
# Tests: cmd_list
# ---------------------------------------------------------------------------

class TestCmdList:
    """Tests for the list command."""

    def test_list_prints_all_flows(self, capsys):
        parser = update_flow.build_parser()
        args = parser.parse_args(["list"])
        update_flow.cmd_list(args)
        output = capsys.readouterr().out
        # All 7 flow names should appear
        for name in update_flow.FLOW_REGISTRY:
            assert name in output
        assert "7 total" in output


# ---------------------------------------------------------------------------
# Tests: cmd_validate
# ---------------------------------------------------------------------------

class TestCmdValidate:
    """Tests for the validate command."""

    def test_validate_valid_flow(self, tmp_path, capsys):
        json_file = tmp_path / "valid.json"
        json_file.write_text(json.dumps(MINIMAL_VALID_FLOW), encoding="utf-8")

        parser = update_flow.build_parser()
        args = parser.parse_args(["validate", "--json-file", str(json_file)])
        update_flow.cmd_validate(args)

        output = capsys.readouterr().out
        assert "[OK]" in output

    def test_validate_invalid_flow_exits(self, tmp_path):
        json_file = tmp_path / "bad.json"
        json_file.write_text(json.dumps(FLOW_NO_DEFINITION), encoding="utf-8")

        parser = update_flow.build_parser()
        args = parser.parse_args(["validate", "--json-file", str(json_file)])

        with pytest.raises(SystemExit) as exc_info:
            update_flow.cmd_validate(args)
        assert exc_info.value.code == 1

    def test_validate_missing_file_exits(self):
        parser = update_flow.build_parser()
        args = parser.parse_args(["validate", "--json-file", "/nonexistent.json"])
        with pytest.raises(SystemExit):
            update_flow.cmd_validate(args)

    def test_validate_malformed_json_exits(self, tmp_path):
        bad_file = tmp_path / "malformed.json"
        bad_file.write_text("{not valid json}", encoding="utf-8")

        parser = update_flow.build_parser()
        args = parser.parse_args(["validate", "--json-file", str(bad_file)])
        with pytest.raises(SystemExit):
            update_flow.cmd_validate(args)


# ---------------------------------------------------------------------------
# Tests: cmd_deploy (dry run)
# ---------------------------------------------------------------------------

class TestCmdDeployDryRun:
    """Tests for the deploy command in dry-run mode."""

    @patch("update_flow.get_flow")
    @patch("update_flow.get_token")
    @patch("update_flow.load_flow_json")
    def test_deploy_dry_run_by_name(self, mock_load, mock_token, mock_get, capsys):
        mock_load.return_value = MINIMAL_VALID_FLOW
        mock_token.return_value = FAKE_TOKEN
        mock_get.return_value = MINIMAL_VALID_FLOW

        parser = update_flow.build_parser()
        args = parser.parse_args(["deploy", "--flow-name", "TaskCompleted", "--dry-run"])
        update_flow.cmd_deploy(args)

        output = capsys.readouterr().out
        assert "DRY RUN" in output
        assert "No changes made" in output

    @patch("update_flow.get_flow")
    @patch("update_flow.get_token")
    @patch("update_flow.load_flow_json")
    def test_deploy_dry_run_by_id_with_file(self, mock_load, mock_token, mock_get, capsys):
        mock_load.return_value = MINIMAL_VALID_FLOW
        mock_token.return_value = FAKE_TOKEN
        mock_get.return_value = MINIMAL_VALID_FLOW

        parser = update_flow.build_parser()
        args = parser.parse_args([
            "deploy",
            "--flow-id", "da211a8a-3ef5-4291-bd91-67c4e6e88aec",
            "--json-file", "flows/TaskCompleted.json",
            "--dry-run",
        ])
        update_flow.cmd_deploy(args)

        output = capsys.readouterr().out
        assert "DRY RUN" in output


# ---------------------------------------------------------------------------
# Tests: cmd_deploy (live)
# ---------------------------------------------------------------------------

class TestCmdDeployLive:
    """Tests for the deploy command in live mode."""

    @patch("update_flow.patch_flow")
    @patch("update_flow.get_flow")
    @patch("update_flow.get_token")
    @patch("update_flow.load_flow_json")
    def test_deploy_success(self, mock_load, mock_token, mock_get, mock_patch, capsys):
        mock_load.return_value = MINIMAL_VALID_FLOW
        mock_token.return_value = FAKE_TOKEN
        mock_get.return_value = MINIMAL_VALID_FLOW
        mock_patch.return_value = make_api_response(200)

        parser = update_flow.build_parser()
        args = parser.parse_args(["deploy", "--flow-name", "TaskCompleted"])
        update_flow.cmd_deploy(args)

        output = capsys.readouterr().out
        assert "[OK]" in output
        assert "Definition updated" in output

    @patch("update_flow.patch_flow")
    @patch("update_flow.get_flow")
    @patch("update_flow.get_token")
    @patch("update_flow.load_flow_json")
    def test_deploy_patch_failure_exits(self, mock_load, mock_token, mock_get, mock_patch):
        mock_load.return_value = MINIMAL_VALID_FLOW
        mock_token.return_value = FAKE_TOKEN
        mock_get.return_value = MINIMAL_VALID_FLOW
        mock_patch.return_value = make_api_response(400, text="Bad request")

        parser = update_flow.build_parser()
        args = parser.parse_args(["deploy", "--flow-name", "TaskCompleted"])
        with pytest.raises(SystemExit) as exc_info:
            update_flow.cmd_deploy(args)
        assert exc_info.value.code == 1

    @patch("update_flow.get_flow")
    @patch("update_flow.get_token")
    @patch("update_flow.load_flow_json")
    def test_deploy_flow_not_found_exits(self, mock_load, mock_token, mock_get):
        mock_load.return_value = MINIMAL_VALID_FLOW
        mock_token.return_value = FAKE_TOKEN
        mock_get.return_value = None  # flow not found

        parser = update_flow.build_parser()
        args = parser.parse_args(["deploy", "--flow-name", "TaskCompleted"])
        with pytest.raises(SystemExit):
            update_flow.cmd_deploy(args)

    @patch("update_flow.get_token")
    def test_deploy_auth_failure_exits(self, mock_token):
        mock_token.side_effect = RuntimeError("No token")

        parser = update_flow.build_parser()
        # Use an explicit json-file so _deploy_single_flow can load it before auth
        args = parser.parse_args([
            "deploy", "--flow-name", "TaskCompleted",
            "--json-file", "flows/TaskCompleted.json",
        ])
        # Mock load_flow_json since the file may not exist in test env
        with patch("update_flow.load_flow_json", return_value=MINIMAL_VALID_FLOW):
            with pytest.raises(SystemExit):
                update_flow.cmd_deploy(args)

    @patch("update_flow.load_flow_json")
    def test_deploy_file_not_found_exits(self, mock_load):
        mock_load.side_effect = FileNotFoundError("not found")

        parser = update_flow.build_parser()
        args = parser.parse_args([
            "deploy", "--flow-name", "TaskCompleted",
            "--json-file", "/nonexistent.json",
        ])
        with pytest.raises(SystemExit):
            update_flow.cmd_deploy(args)

    @patch("update_flow.load_flow_json")
    def test_deploy_invalid_json_exits(self, mock_load):
        mock_load.side_effect = json.JSONDecodeError("bad", "", 0)

        parser = update_flow.build_parser()
        args = parser.parse_args([
            "deploy", "--flow-name", "TaskCompleted",
            "--json-file", "bad.json",
        ])
        with pytest.raises(SystemExit):
            update_flow.cmd_deploy(args)

    @patch("update_flow.get_token")
    @patch("update_flow.load_flow_json")
    def test_deploy_no_definition_exits(self, mock_load, mock_token):
        mock_load.return_value = FLOW_NO_DEFINITION
        mock_token.return_value = FAKE_TOKEN

        parser = update_flow.build_parser()
        args = parser.parse_args(["deploy", "--flow-name", "TaskCompleted"])
        with pytest.raises(SystemExit):
            update_flow.cmd_deploy(args)


# ---------------------------------------------------------------------------
# Tests: cmd_deploy_all
# ---------------------------------------------------------------------------

class TestCmdDeployAll:
    """Tests for the deploy-all command."""

    @patch("update_flow.get_flow")
    @patch("update_flow.get_token")
    @patch("update_flow.load_flow_json")
    def test_deploy_all_dry_run(self, mock_load, mock_token, mock_get, capsys):
        mock_load.return_value = MINIMAL_VALID_FLOW
        mock_token.return_value = FAKE_TOKEN
        mock_get.return_value = MINIMAL_VALID_FLOW

        parser = update_flow.build_parser()
        args = parser.parse_args(["deploy-all", "--dry-run"])
        update_flow.cmd_deploy_all(args)

        output = capsys.readouterr().out
        assert "Batch Deploy Summary" in output
        assert "DRY RUN" in output

    @patch("update_flow.patch_flow")
    @patch("update_flow.get_flow")
    @patch("update_flow.get_token")
    @patch("update_flow.load_flow_json")
    def test_deploy_all_success(self, mock_load, mock_token, mock_get, mock_patch, capsys):
        mock_load.return_value = MINIMAL_VALID_FLOW
        mock_token.return_value = FAKE_TOKEN
        mock_get.return_value = MINIMAL_VALID_FLOW
        mock_patch.return_value = make_api_response(200)

        parser = update_flow.build_parser()
        args = parser.parse_args(["deploy-all"])
        update_flow.cmd_deploy_all(args)

        output = capsys.readouterr().out
        assert "Batch Deploy Summary" in output
        # All 7 should show OK
        assert output.count("OK") >= 7


# ---------------------------------------------------------------------------
# Tests: cmd_export
# ---------------------------------------------------------------------------

class TestCmdExport:
    """Tests for the export command."""

    @patch("update_flow.get_flow")
    @patch("update_flow.get_token")
    def test_export_success(self, mock_token, mock_get, tmp_path, capsys):
        mock_token.return_value = FAKE_TOKEN
        mock_get.return_value = MINIMAL_VALID_FLOW

        output_file = str(tmp_path / "exported.json")
        parser = update_flow.build_parser()
        args = parser.parse_args([
            "export", "--flow-name", "TaskCompleted", "-o", output_file,
        ])
        update_flow.cmd_export(args)

        output = capsys.readouterr().out
        assert "[OK]" in output

        # Verify file was written
        with open(output_file, "r", encoding="utf-8") as f:
            exported = json.load(f)
        assert exported["name"] == MINIMAL_VALID_FLOW["name"]

    @patch("update_flow.get_flow")
    @patch("update_flow.get_token")
    def test_export_flow_not_found_exits(self, mock_token, mock_get):
        mock_token.return_value = FAKE_TOKEN
        mock_get.return_value = None

        parser = update_flow.build_parser()
        args = parser.parse_args([
            "export", "--flow-name", "TaskCompleted", "-o", "out.json",
        ])
        with pytest.raises(SystemExit):
            update_flow.cmd_export(args)

    @patch("update_flow.get_token")
    def test_export_auth_failure_exits(self, mock_token):
        mock_token.side_effect = RuntimeError("no token")

        parser = update_flow.build_parser()
        args = parser.parse_args([
            "export", "--flow-name", "TaskCompleted", "-o", "out.json",
        ])
        with pytest.raises(SystemExit):
            update_flow.cmd_export(args)


# ---------------------------------------------------------------------------
# Tests: Validate all 7 real flow JSONs on disk
# ---------------------------------------------------------------------------

class TestRealFlowJsons:
    """Validate every flow JSON file that exists on disk in the flows/ directory."""

    @pytest.mark.parametrize(
        "flow_name",
        list(update_flow.FLOW_REGISTRY.keys()),
    )
    def test_flow_json_validates(self, flow_name):
        """Each registered flow JSON should pass validation."""
        entry = update_flow.FLOW_REGISTRY[flow_name]
        json_path = update_flow.REPO_ROOT / entry["json_file"]

        if not json_path.exists():
            pytest.skip(f"Flow JSON not present: {json_path}")

        flow_json = update_flow.load_flow_json(str(json_path))
        issues = update_flow.validate_flow_json(flow_json)

        assert issues == [], (
            f"Validation issues for {flow_name}:\n"
            + "\n".join(f"  - {i}" for i in issues)
        )

    @pytest.mark.parametrize(
        "flow_name",
        list(update_flow.FLOW_REGISTRY.keys()),
    )
    def test_flow_json_id_matches_registry(self, flow_name):
        """The 'name' field in the JSON should match the registry ID."""
        entry = update_flow.FLOW_REGISTRY[flow_name]
        json_path = update_flow.REPO_ROOT / entry["json_file"]

        if not json_path.exists():
            pytest.skip(f"Flow JSON not present: {json_path}")

        flow_json = update_flow.load_flow_json(str(json_path))
        json_flow_id = flow_json.get("name", "")

        # CancelTask has a different ID in the JSON vs registry because
        # it was re-created; only check the ones that should match
        if flow_name != "CancelTask":
            assert json_flow_id == entry["id"], (
                f"Flow {flow_name}: JSON name '{json_flow_id}' != registry ID '{entry['id']}'"
            )

    @pytest.mark.parametrize(
        "flow_name",
        list(update_flow.FLOW_REGISTRY.keys()),
    )
    def test_flow_json_has_definition(self, flow_name):
        """Every flow JSON must have a non-empty definition with actions."""
        entry = update_flow.FLOW_REGISTRY[flow_name]
        json_path = update_flow.REPO_ROOT / entry["json_file"]

        if not json_path.exists():
            pytest.skip(f"Flow JSON not present: {json_path}")

        flow_json = update_flow.load_flow_json(str(json_path))
        defn = update_flow.extract_definition(flow_json)
        assert defn is not None
        assert len(defn.get("actions", {})) > 0, f"{flow_name} has no actions"

    @pytest.mark.parametrize(
        "flow_name",
        list(update_flow.FLOW_REGISTRY.keys()),
    )
    def test_flow_json_has_connection_references(self, flow_name):
        """Every flow JSON must have at least one connection reference."""
        entry = update_flow.FLOW_REGISTRY[flow_name]
        json_path = update_flow.REPO_ROOT / entry["json_file"]

        if not json_path.exists():
            pytest.skip(f"Flow JSON not present: {json_path}")

        flow_json = update_flow.load_flow_json(str(json_path))
        refs = update_flow.extract_connection_references(flow_json)
        assert len(refs) > 0, f"{flow_name} has no connection references"
        # All flows should at least have the Dataverse connection
        assert "shared_commondataserviceforapps" in refs


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
