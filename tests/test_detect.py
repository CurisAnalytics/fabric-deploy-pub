"""Unit tests for ParameterDetector — pure regex/dict logic, no network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fabric_deploy.detect import DetectedParameter, ParameterDetector


# --- helpers ---------------------------------------------------------------

WORKSPACE_ID = "11111111-1111-1111-1111-111111111111"
LAKEHOUSE_ID = "22222222-2222-2222-2222-222222222222"
ENDPOINT_ID = "33333333-3333-3333-3333-333333333333"
CONNECTION_ID = "44444444-4444-4444-4444-444444444444"
OTHER_WORKSPACE_ID = "55555555-5555-5555-5555-555555555555"


def _make_lakehouse(repo: Path, name: str) -> None:
    item = repo / f"{name}.Lakehouse"
    item.mkdir()
    (item / ".platform").write_text(
        json.dumps({"metadata": {"displayName": name, "type": "Lakehouse"}})
    )


def _make_sqldb(repo: Path, name: str) -> None:
    item = repo / f"{name}.SQLDatabase"
    item.mkdir()
    (item / ".platform").write_text(
        json.dumps({"metadata": {"displayName": name, "type": "SQLDatabase"}})
    )


def _make_notebook(repo: Path, name: str, body: str, ext: str = "py") -> None:
    nb = repo / f"{name}.Notebook"
    nb.mkdir()
    (nb / f"notebook-content.{ext}").write_text(body)


def _make_semantic_model(repo: Path, name: str, expressions_tmdl: str) -> None:
    sm = repo / f"{name}.SemanticModel"
    (sm / "definition").mkdir(parents=True)
    (sm / "definition" / "expressions.tmdl").write_text(expressions_tmdl)


def _make_pipeline(repo: Path, name: str, content: dict) -> None:
    pl = repo / f"{name}.DataPipeline"
    pl.mkdir()
    (pl / "pipeline-content.json").write_text(json.dumps(content))


def _make_copyjob(repo: Path, name: str, content: dict) -> None:
    cj = repo / f"{name}.CopyJob"
    cj.mkdir()
    (cj / "copyjob-content.json").write_text(json.dumps(content))


# --- notebook extraction ---------------------------------------------------


class TestNotebookExtraction:
    def test_extracts_workspace_and_lakehouse_ids(self, tmp_path: Path):
        _make_lakehouse(tmp_path, "lh")
        _make_notebook(
            tmp_path,
            "n1",
            '# fmt:off\n'
            '# META {\n'
            f'# META   "default_lakehouse_workspace_id": "{WORKSPACE_ID}",\n'
            f'# META   "default_lakehouse": "{LAKEHOUSE_ID}"\n'
            '# META }\n',
        )
        d = ParameterDetector(tmp_path)
        d.detect_all()

        assert WORKSPACE_ID in d.parameters
        assert LAKEHOUSE_ID in d.parameters
        assert d.parameters[WORKSPACE_ID].description == "Workspace ID"
        assert d.parameters[LAKEHOUSE_ID].description == "Lakehouse ID"

    def test_extracts_from_tsql_notebook(self, tmp_path: Path):
        """T-SQL notebooks (notebook-content.sql) use `-- META` comment prefix."""
        _make_lakehouse(tmp_path, "lh")
        _make_notebook(
            tmp_path,
            "n1",
            '-- METADATA ********************\n'
            '-- META {\n'
            '-- META   "kernel_info": {"name": "sqldatawarehouse"},\n'
            '-- META   "dependencies": {\n'
            '-- META     "lakehouse": {\n'
            f'-- META       "default_lakehouse_workspace_id": "{WORKSPACE_ID}",\n'
            f'-- META       "default_lakehouse": "{LAKEHOUSE_ID}"\n'
            '-- META     }\n'
            '-- META   }\n'
            '-- META }\n',
            ext="sql",
        )
        d = ParameterDetector(tmp_path)
        d.detect_all()

        assert WORKSPACE_ID in d.parameters
        assert LAKEHOUSE_ID in d.parameters
        assert d.parameters[WORKSPACE_ID].description == "Workspace ID"
        assert d.parameters[LAKEHOUSE_ID].description == "Lakehouse ID"

    def test_warns_on_multiple_attached_lakehouses(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        """A notebook with stale attachments (e.g. dev + prod) should warn."""
        _make_lakehouse(tmp_path, "lh")
        stale_id = "99999999-9999-9999-9999-999999999999"
        _make_notebook(
            tmp_path,
            "n1",
            '# META {\n'
            '# META   "dependencies": {\n'
            '# META     "lakehouse": {\n'
            f'# META       "default_lakehouse": "{LAKEHOUSE_ID}",\n'
            '# META       "known_lakehouses": [\n'
            f'# META         {{"id": "{LAKEHOUSE_ID}"}},\n'
            f'# META         {{"id": "{stale_id}"}}\n'
            '# META       ]\n'
            '# META     }\n'
            '# META   }\n'
            '# META }\n',
        )
        d = ParameterDetector(tmp_path)
        with caplog.at_level("WARNING", logger="fabric_deploy.detect"):
            d.detect_all()

        assert any(
            "2 lakehouses attached" in r.message and stale_id in r.message
            for r in caplog.records
        ), [r.message for r in caplog.records]

    def test_no_warning_on_clean_single_attachment(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        _make_lakehouse(tmp_path, "lh")
        _make_notebook(
            tmp_path,
            "n1",
            '# META {\n'
            '# META   "dependencies": {\n'
            '# META     "lakehouse": {\n'
            f'# META       "default_lakehouse": "{LAKEHOUSE_ID}",\n'
            '# META       "known_lakehouses": [\n'
            f'# META         {{"id": "{LAKEHOUSE_ID}"}}\n'
            '# META       ]\n'
            '# META     }\n'
            '# META   }\n'
            '# META }\n',
        )
        d = ParameterDetector(tmp_path)
        with caplog.at_level("WARNING", logger="fabric_deploy.detect"):
            d.detect_all()

        assert not any(
            "lakehouses attached" in r.message for r in caplog.records
        )

    def test_warns_in_tsql_notebook_too(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        _make_lakehouse(tmp_path, "lh")
        stale_id = "99999999-9999-9999-9999-999999999999"
        _make_notebook(
            tmp_path,
            "n1",
            '-- META {\n'
            '-- META   "dependencies": {\n'
            '-- META     "lakehouse": {\n'
            f'-- META       "default_lakehouse": "{LAKEHOUSE_ID}",\n'
            '-- META       "known_lakehouses": [\n'
            f'-- META         {{"id": "{LAKEHOUSE_ID}"}},\n'
            f'-- META         {{"id": "{stale_id}"}}\n'
            '-- META       ]\n'
            '-- META     }\n'
            '-- META   }\n'
            '-- META }\n',
            ext="sql",
        )
        d = ParameterDetector(tmp_path)
        with caplog.at_level("WARNING", logger="fabric_deploy.detect"):
            d.detect_all()

        assert any("2 lakehouses attached" in r.message for r in caplog.records)

    def test_dynamic_variable_for_workspace_and_lakehouse(self, tmp_path: Path):
        _make_lakehouse(tmp_path, "lh")
        _make_notebook(
            tmp_path,
            "n1",
            f'"default_lakehouse_workspace_id": "{WORKSPACE_ID}"\n'
            f'"default_lakehouse": "{LAKEHOUSE_ID}"\n',
        )
        d = ParameterDetector(tmp_path)
        d.detect_all()

        assert d.parameters[WORKSPACE_ID].dynamic_variable == "$workspace.$id"
        assert (
            d.parameters[LAKEHOUSE_ID].dynamic_variable
            == "$items.Lakehouse.lh.$id"
        )


# --- semantic model extraction --------------------------------------------


class TestSemanticModelExtraction:
    def test_extracts_lakehouse_sql_endpoint(self, tmp_path: Path):
        _make_lakehouse(tmp_path, "lh")
        body = (
            'expression Source = '
            f'Sql.Database("HOST-XXXX.datawarehouse.fabric.microsoft.com", "lh-{ENDPOINT_ID}")'
        )
        _make_semantic_model(tmp_path, "sm", body)

        d = ParameterDetector(tmp_path)
        d.detect_all()

        # Hostname captured as a parameter with metadata
        host = "HOST-XXXX.datawarehouse.fabric.microsoft.com"
        assert host in d.parameters
        host_param = d.parameters[host]
        assert host_param.description == "SQL Analytics Endpoint Connection String"
        assert host_param.metadata["domain_type"] == "datawarehouse"
        assert host_param.metadata["db_name"] == "lh"

        # UUID captured as the endpoint item ID
        assert ENDPOINT_ID in d.parameters
        assert (
            d.parameters[ENDPOINT_ID].description
            == "Lakehouse SQL Analytics Endpoint Item ID"
        )

    def test_extracts_sql_database(self, tmp_path: Path):
        _make_sqldb(tmp_path, "mydb")
        body = (
            'Source = '
            f'Sql.Database("HOST-YYYY.database.fabric.microsoft.com", "mydb-{ENDPOINT_ID}")'
        )
        _make_semantic_model(tmp_path, "sm", body)

        d = ParameterDetector(tmp_path)
        d.detect_all()

        host = "HOST-YYYY.database.fabric.microsoft.com"
        assert host in d.parameters
        assert (
            d.parameters[host].description == "SQL Database Connection String"
        )
        assert d.parameters[host].metadata["domain_type"] == "database"
        # Connection string now auto-resolves via fabric-cicd's $sqlendpoint
        assert (
            d.parameters[host].dynamic_variable
            == "$items.SQLDatabase.mydb.$sqlendpoint"
        )

        # SQL Database Item ID captured
        assert ENDPOINT_ID in d.parameters
        assert d.parameters[ENDPOINT_ID].description == "SQL Database Item ID"

        # Full database name (db-uuid) also captured
        full_name = f"mydb-{ENDPOINT_ID}"
        assert full_name in d.parameters
        assert d.parameters[full_name].description == "SQL Database Name"
        # Name has no native fabric-cicd attribute; still requires manual override
        assert d.parameters[full_name].dynamic_variable == ""

    def test_extracts_onelake_dfs_url(self, tmp_path: Path):
        """DirectLake semantic models reference OneLake via DFS URL."""
        _make_lakehouse(tmp_path, "lh")
        body = (
            "expression 'DirectLake - lh' = let\n"
            "  Source = AzureStorage.DataLake("
            f'"https://onelake.dfs.fabric.microsoft.com/{WORKSPACE_ID}/{LAKEHOUSE_ID}",'
            " [HierarchicalNavigation=true])\nin Source"
        )
        _make_semantic_model(tmp_path, "sm", body)

        d = ParameterDetector(tmp_path)
        d.detect_all()

        assert WORKSPACE_ID in d.parameters
        assert d.parameters[WORKSPACE_ID].description == "Workspace ID"
        assert d.parameters[WORKSPACE_ID].dynamic_variable == "$workspace.$id"

        assert LAKEHOUSE_ID in d.parameters
        assert d.parameters[LAKEHOUSE_ID].description == "Lakehouse ID"
        assert (
            d.parameters[LAKEHOUSE_ID].dynamic_variable
            == "$items.Lakehouse.lh.$id"
        )

    def test_onelake_dfs_url_merges_with_notebook_detection(self, tmp_path: Path):
        """When the same IDs appear in a Notebook AND a SemanticModel, both
        source files are recorded against a single deduped parameter."""
        _make_lakehouse(tmp_path, "lh")
        _make_notebook(
            tmp_path,
            "nb",
            f'# META  "default_lakehouse_workspace_id": "{WORKSPACE_ID}",\n'
            f'# META  "default_lakehouse": "{LAKEHOUSE_ID}",\n',
        )
        body = (
            "expression Source = AzureStorage.DataLake("
            f'"https://onelake.dfs.fabric.microsoft.com/{WORKSPACE_ID}/{LAKEHOUSE_ID}",'
            " [HierarchicalNavigation=true])"
        )
        _make_semantic_model(tmp_path, "sm", body)

        d = ParameterDetector(tmp_path)
        d.detect_all()

        assert len(d.parameters[WORKSPACE_ID].source_files) == 2
        assert len(d.parameters[LAKEHOUSE_ID].source_files) == 2

    def test_pbidedicated_domain_metadata(self, tmp_path: Path):
        _make_lakehouse(tmp_path, "lh")
        body = (
            'Source = '
            f'Sql.Database("HOST-AAAA.datawarehouse.pbidedicated.windows.net", "lh-{ENDPOINT_ID}")'
        )
        _make_semantic_model(tmp_path, "sm", body)

        d = ParameterDetector(tmp_path)
        d.detect_all()

        host = "HOST-AAAA.datawarehouse.pbidedicated.windows.net"
        assert d.parameters[host].metadata["domain_suffix"] == "pbidedicated.windows.net"


# --- pipeline extraction --------------------------------------------------


class TestPipelineExtraction:
    def test_extracts_external_connection(self, tmp_path: Path):
        _make_lakehouse(tmp_path, "lh")
        _make_pipeline(
            tmp_path,
            "pl",
            {
                "properties": {
                    "activities": [
                        {
                            "externalReferences": {"connection": CONNECTION_ID},
                        }
                    ]
                }
            },
        )
        d = ParameterDetector(tmp_path)
        d.detect_all()

        assert CONNECTION_ID in d.parameters
        assert d.parameters[CONNECTION_ID].description == "External connection ID"

    def test_extracts_workspaceid_in_typeproperties(self, tmp_path: Path):
        _make_lakehouse(tmp_path, "lh")
        _make_pipeline(
            tmp_path,
            "pl",
            {
                "properties": {
                    "activities": [
                        {
                            "typeProperties": {
                                "workspaceId": OTHER_WORKSPACE_ID,
                                "notebookId": "ignored",
                            }
                        }
                    ]
                }
            },
        )
        d = ParameterDetector(tmp_path)
        d.detect_all()

        assert OTHER_WORKSPACE_ID in d.parameters
        assert d.parameters[OTHER_WORKSPACE_ID].description == "Workspace ID"

    def test_skips_null_uuid_workspaceid(self, tmp_path: Path):
        _make_lakehouse(tmp_path, "lh")
        _make_pipeline(
            tmp_path,
            "pl",
            {
                "properties": {
                    "activities": [
                        {"typeProperties": {"workspaceId": ParameterDetector.NULL_UUID}}
                    ]
                }
            },
        )
        d = ParameterDetector(tmp_path)
        d.detect_all()

        # The null-uuid placeholder must not be added as a parameter
        assert ParameterDetector.NULL_UUID not in d.parameters


# --- copyjob extraction ---------------------------------------------------


class TestCopyJobExtraction:
    def test_extracts_workspace_and_artifact_ids(self, tmp_path: Path):
        _make_lakehouse(tmp_path, "lh")
        _make_copyjob(
            tmp_path,
            "cj",
            {
                "properties": {
                    "source": {
                        "connectionSettings": {
                            "typeProperties": {
                                "workspaceId": WORKSPACE_ID,
                                "artifactId": LAKEHOUSE_ID,
                            }
                        }
                    },
                    "destination": {
                        "connectionSettings": {
                            "typeProperties": {
                                "workspaceId": OTHER_WORKSPACE_ID,
                                "artifactId": ENDPOINT_ID,
                            }
                        }
                    },
                }
            },
        )
        d = ParameterDetector(tmp_path)
        d.detect_all()

        assert WORKSPACE_ID in d.parameters
        assert OTHER_WORKSPACE_ID in d.parameters
        assert LAKEHOUSE_ID in d.parameters
        assert ENDPOINT_ID in d.parameters
        assert d.parameters[LAKEHOUSE_ID].description == "Lakehouse ID"


# --- should_skip_replacement ---------------------------------------------


class TestShouldSkipReplacement:
    @pytest.mark.parametrize(
        "detected_wsid,target_wsid,expected",
        [
            (None, WORKSPACE_ID, False),  # nothing detected → don't skip
            (WORKSPACE_ID, WORKSPACE_ID, True),  # all match → skip
            (WORKSPACE_ID, OTHER_WORKSPACE_ID, False),  # mismatch → don't skip
        ],
    )
    def test_skip_decision(
        self, tmp_path: Path, detected_wsid: str | None, target_wsid: str, expected: bool
    ):
        _make_lakehouse(tmp_path, "lh")
        if detected_wsid is not None:
            _make_notebook(
                tmp_path, "n1", f'"default_lakehouse_workspace_id": "{detected_wsid}"'
            )
        d = ParameterDetector(tmp_path)
        d.detect_all()
        assert d.should_skip_replacement(target_wsid) is expected


# --- filter_lakehouse_params ---------------------------------------------


class TestFilterLakehouseParams:
    def test_removes_lakehouse_descriptions_only(self, tmp_path: Path):
        d = ParameterDetector(tmp_path)
        # Manually populate parameters with a mix of types
        d.parameters = {
            WORKSPACE_ID: DetectedParameter(
                find_value=WORKSPACE_ID, description="Workspace ID"
            ),
            LAKEHOUSE_ID: DetectedParameter(
                find_value=LAKEHOUSE_ID, description="Lakehouse ID"
            ),
            "host": DetectedParameter(
                find_value="host",
                description="SQL Analytics Endpoint Connection String",
            ),
            ENDPOINT_ID: DetectedParameter(
                find_value=ENDPOINT_ID,
                description="Lakehouse SQL Analytics Endpoint Item ID",
            ),
            CONNECTION_ID: DetectedParameter(
                find_value=CONNECTION_ID, description="External connection ID"
            ),
        }

        removed = d.filter_lakehouse_params()

        assert set(removed) == {LAKEHOUSE_ID, "host", ENDPOINT_ID}
        assert WORKSPACE_ID in d.parameters
        assert CONNECTION_ID in d.parameters
        assert LAKEHOUSE_ID not in d.parameters


# --- _generate_dynamic_variable -------------------------------------------


class TestGenerateDynamicVariable:
    @pytest.mark.parametrize(
        "description,expected",
        [
            ("Workspace ID", "$workspace.$id"),
            ("Lakehouse ID", "$items.Lakehouse.lh.$id"),
            (
                "SQL Analytics Endpoint Connection String",
                "$items.Lakehouse.lh.$sqlendpoint",
            ),
            (
                "Lakehouse SQL Analytics Endpoint Item ID",
                "$items.Lakehouse.lh.$sqlendpointid",
            ),
            ("External connection ID", ""),
        ],
    )
    def test_dynamic_variable_lookup_with_lakehouse(
        self, tmp_path: Path, description: str, expected: str
    ):
        _make_lakehouse(tmp_path, "lh")
        d = ParameterDetector(tmp_path)
        d._detect_lakehouse_names()
        assert d._generate_dynamic_variable(description) == expected

    def test_sqldatabase_item_id_uses_db_name(self, tmp_path: Path):
        _make_sqldb(tmp_path, "mydb")
        d = ParameterDetector(tmp_path)
        d._detect_sqldatabase_names()
        assert (
            d._generate_dynamic_variable("SQL Database Item ID")
            == "$items.SQLDatabase.mydb.$id"
        )

    def test_sqldatabase_connection_string_resolves_to_sqlendpoint(
        self, tmp_path: Path
    ):
        """SQL Database Connection String should map to $sqlendpoint, not empty.

        fabric-cicd 1.0's PROPERTY_PATH_ATTR_MAPPING for SQLDatabase resolves
        $sqlendpoint to body/properties/serverFqdn (.database.fabric.microsoft.com).
        """
        _make_sqldb(tmp_path, "mydb")
        d = ParameterDetector(tmp_path)
        d._detect_sqldatabase_names()
        assert (
            d._generate_dynamic_variable("SQL Database Connection String")
            == "$items.SQLDatabase.mydb.$sqlendpoint"
        )

    def test_sqldatabase_name_remains_unresolved(self, tmp_path: Path):
        """SQL Database Name has no native fabric-cicd attribute → still empty."""
        _make_sqldb(tmp_path, "mydb")
        d = ParameterDetector(tmp_path)
        d._detect_sqldatabase_names()
        assert d._generate_dynamic_variable("SQL Database Name") == ""
