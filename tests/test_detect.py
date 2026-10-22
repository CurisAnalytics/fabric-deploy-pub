"""Unit tests for ParameterDetector — pure regex/dict logic, no network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from fabric_deploy.detect import DetectedParameter, Kind, ParameterDetector

from .conftest import make_item

# --- helpers ---------------------------------------------------------------

WORKSPACE_ID = "11111111-1111-1111-1111-111111111111"
LAKEHOUSE_ID = "22222222-2222-2222-2222-222222222222"
ENDPOINT_ID = "33333333-3333-3333-3333-333333333333"
CONNECTION_ID = "44444444-4444-4444-4444-444444444444"
OTHER_WORKSPACE_ID = "55555555-5555-5555-5555-555555555555"


def _make_notebook(
    repo: Path, name: str, body: str, ext: str = "py", folder: str | None = None
) -> None:
    make_item(repo, name, "Notebook", folder=folder, files={f"notebook-content.{ext}": body})


def _make_semantic_model(repo: Path, name: str, expressions_tmdl: str) -> None:
    make_item(repo, name, "SemanticModel", files={"definition/expressions.tmdl": expressions_tmdl})


def _make_pipeline(repo: Path, name: str, content: dict[str, object]) -> None:
    make_item(repo, name, "DataPipeline", files={"pipeline-content.json": content})


def _make_copyjob(repo: Path, name: str, content: dict[str, object]) -> None:
    make_item(repo, name, "CopyJob", files={"copyjob-content.json": content})


def _lakehouse_meta(
    workspace_id: str, lakehouse_id: str, name: str | None = "lh", prefix: str = "#"
) -> str:
    """Notebook META attaching a default lakehouse, as Fabric writes it."""
    lakehouse = {"default_lakehouse": lakehouse_id, "default_lakehouse_workspace_id": workspace_id}
    if name is not None:
        lakehouse["default_lakehouse_name"] = name
    body = json.dumps({"dependencies": {"lakehouse": lakehouse}}, indent=2)
    return "".join(f"{prefix} META {line}\n" for line in body.splitlines())


# --- notebook extraction ---------------------------------------------------


class TestNotebookExtraction:
    @pytest.mark.parametrize("prefix,ext", [("#", "py"), ("--", "sql")])
    def test_extracts_workspace_and_lakehouse_ids(self, tmp_path: Path, prefix: str, ext: str):
        """Both Python (`# META`) and T-SQL (`-- META`) notebooks extract IDs."""
        make_item(tmp_path, "lh", "Lakehouse")
        p = prefix
        _make_notebook(
            tmp_path,
            "n1",
            f"{p} META {{\n"
            f'{p} META   "dependencies": {{\n'
            f'{p} META     "lakehouse": {{\n'
            f'{p} META       "default_lakehouse_workspace_id": "{WORKSPACE_ID}",\n'
            f'{p} META       "default_lakehouse": "{LAKEHOUSE_ID}"\n'
            f"{p} META     }}\n"
            f"{p} META   }}\n"
            f"{p} META }}\n",
            ext=ext,
        )
        d = ParameterDetector(tmp_path)
        d.detect_all()

        assert WORKSPACE_ID in d.parameters
        assert LAKEHOUSE_ID in d.parameters
        assert d.parameters[WORKSPACE_ID].description == "Workspace ID"
        assert d.parameters[LAKEHOUSE_ID].description == "Lakehouse ID"

    def test_several_attached_lakehouses_are_not_flagged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        """A notebook may legitimately attach more than one lakehouse besides
        its default; that is not a stale attachment."""
        make_item(tmp_path, "lh", "Lakehouse")
        make_item(tmp_path, "other", "Lakehouse")
        _make_notebook(
            tmp_path,
            "n1",
            "# META {\n"
            '# META   "dependencies": {\n'
            '# META     "lakehouse": {\n'
            f'# META       "default_lakehouse": "{LAKEHOUSE_ID}",\n'
            '# META       "known_lakehouses": [\n'
            f'# META         {{"id": "{LAKEHOUSE_ID}"}},\n'
            f'# META         {{"id": "{ENDPOINT_ID}"}}\n'
            "# META       ]\n"
            "# META     }\n"
            "# META   }\n"
            "# META }\n",
        )
        d = ParameterDetector(tmp_path)
        with caplog.at_level("WARNING", logger="fabric_deploy.detect"):
            d.detect_all()

        assert not any("lakehouses attached" in r.message for r in caplog.records)

    def test_synapse_notebook_dir_is_detected(self, tmp_path: Path):
        """A `*.SynapseNotebook/` dir whose .platform says `type: Notebook`
        must be scanned like any other notebook (fabric-cicd deploys it)."""
        make_item(tmp_path, "lh", "Lakehouse")
        _make_notebook(
            tmp_path,
            "n1",
            _lakehouse_meta(WORKSPACE_ID, LAKEHOUSE_ID),
            folder="n1.SynapseNotebook",
        )
        d = ParameterDetector(tmp_path)
        d.detect_all()

        assert WORKSPACE_ID in d.parameters
        assert LAKEHOUSE_ID in d.parameters

    def test_skips_null_uuid_workspace_and_lakehouse(self, tmp_path: Path):
        """An unattached notebook carries the 00000000-… sentinel — emitting a
        rule for it would clobber fabric-cicd-managed placeholders globally."""
        make_item(tmp_path, "lh", "Lakehouse")
        null = ParameterDetector.NULL_UUID
        _make_notebook(tmp_path, "n1", _lakehouse_meta(null, null))
        d = ParameterDetector(tmp_path)
        d.detect_all()

        assert null not in d.parameters

    def test_dynamic_variable_for_workspace_and_lakehouse(self, tmp_path: Path):
        make_item(tmp_path, "lh", "Lakehouse")
        _make_notebook(tmp_path, "n1", _lakehouse_meta(WORKSPACE_ID, LAKEHOUSE_ID))
        d = ParameterDetector(tmp_path)
        d.detect_all()

        assert d.parameters[WORKSPACE_ID].dynamic_variable == "$workspace.$id"
        assert d.parameters[LAKEHOUSE_ID].dynamic_variable == "$items.Lakehouse.lh.$id"


# --- semantic model extraction --------------------------------------------


class TestSemanticModelExtraction:
    def test_extracts_lakehouse_sql_endpoint(self, tmp_path: Path):
        make_item(tmp_path, "lh", "Lakehouse")
        body = (
            "expression Source = "
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

        # UUID captured as the endpoint item ID
        assert ENDPOINT_ID in d.parameters
        assert d.parameters[ENDPOINT_ID].description == "Lakehouse SQL Analytics Endpoint Item ID"

    def test_extracts_sql_database(self, tmp_path: Path):
        make_item(tmp_path, "mydb", "SQLDatabase")
        body = (
            "Source = "
            f'Sql.Database("HOST-YYYY.database.fabric.microsoft.com", "mydb-{ENDPOINT_ID}")'
        )
        _make_semantic_model(tmp_path, "sm", body)

        d = ParameterDetector(tmp_path)
        d.detect_all()

        host = "HOST-YYYY.database.fabric.microsoft.com"
        assert host in d.parameters
        assert d.parameters[host].description == "SQL Database Connection String"
        # Connection string now auto-resolves via fabric-cicd's $sqlendpoint
        assert d.parameters[host].dynamic_variable == "$items.SQLDatabase.mydb.$sqlendpoint"

        # SQL Database Item ID captured
        assert ENDPOINT_ID in d.parameters
        assert d.parameters[ENDPOINT_ID].description == "SQL Database Item ID"
        assert set(d.parameters) == {host, ENDPOINT_ID}

    def test_extracts_onelake_dfs_url(self, tmp_path: Path):
        """A Direct Lake model reading one of this repo's workspaces maps its
        OneLake URL to the target workspace and lakehouse."""
        make_item(tmp_path, "lh", "Lakehouse")
        body = (
            "expression 'DirectLake - lh' = let\n"
            "  Source = AzureStorage.DataLake("
            f'"https://onelake.dfs.fabric.microsoft.com/{WORKSPACE_ID}/{LAKEHOUSE_ID}",'
            " [HierarchicalNavigation=true])\nin Source"
        )
        _make_semantic_model(tmp_path, "sm", body)

        d = ParameterDetector(tmp_path, source_workspace_ids=[WORKSPACE_ID])
        d.detect_all()

        assert WORKSPACE_ID in d.parameters
        assert d.parameters[WORKSPACE_ID].description == "Workspace ID"
        assert d.parameters[WORKSPACE_ID].dynamic_variable == "$workspace.$id"

        assert LAKEHOUSE_ID in d.parameters
        assert d.parameters[LAKEHOUSE_ID].description == "Lakehouse ID"
        assert d.parameters[LAKEHOUSE_ID].dynamic_variable == "$items.Lakehouse.lh.$id"

    def test_onelake_dfs_url_merges_with_notebook_detection(self, tmp_path: Path):
        """When the same IDs appear in a Notebook AND a SemanticModel, both
        source files are recorded against a single deduped parameter."""
        make_item(tmp_path, "lh", "Lakehouse")
        _make_notebook(tmp_path, "nb", _lakehouse_meta(WORKSPACE_ID, LAKEHOUSE_ID))
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

    def test_extracts_sql_databases_navigation_form(self, tmp_path: Path):
        """`Sql.Databases("host")` (plural, one arg) — the database is picked
        by name downstream, so only the hostname parameter is expected."""
        make_item(tmp_path, "lh", "Lakehouse")
        body = (
            "\t\t\t\tlet\n"
            '\t\t\t\t    Source = Sql.Databases("HOST-XXXX.datawarehouse.fabric.microsoft.com"),\n'
            '\t\t\t\t    lh = Source{[Name="lh"]}[Data],\n'
            '\t\t\t\t    dbo_fact = lh{[Schema="dbo",Item="fact"]}[Data]\n'
            "\t\t\t\tin\n"
            "\t\t\t\t    dbo_fact"
        )
        _make_semantic_model(tmp_path, "sm", body)

        d = ParameterDetector(tmp_path)
        d.detect_all()

        host = "HOST-XXXX.datawarehouse.fabric.microsoft.com"
        assert host in d.parameters
        host_param = d.parameters[host]
        assert host_param.description == "SQL Analytics Endpoint Connection String"
        assert host_param.dynamic_variable == "$items.Lakehouse.lh.$sqlendpoint"
        # One-arg form carries no db name / endpoint item id — nothing else
        # should be detected from this model.
        assert len(d.parameters) == 1

    def test_mirrored_database_repo_resolves_analytics_endpoint(self, tmp_path: Path):
        """A repo whose only SQL-endpoint item is a MirroredDatabase resolves
        the `datawarehouse.*` hostname and endpoint item id to fabric-cicd's
        MirroredDatabase dynamic variables."""
        make_item(tmp_path, "mdb", "MirroredDatabase")
        body = (
            'Source = Sql.Database("HOST-XXXX.datawarehouse.fabric.microsoft.com", '
            f'"mdb-{ENDPOINT_ID}")'
        )
        _make_semantic_model(tmp_path, "sm", body)

        d = ParameterDetector(tmp_path)
        d.detect_all()

        host = "HOST-XXXX.datawarehouse.fabric.microsoft.com"
        assert d.parameters[host].dynamic_variable == "$items.MirroredDatabase.mdb.$sqlendpoint"
        assert (
            d.parameters[ENDPOINT_ID].dynamic_variable
            == "$items.MirroredDatabase.mdb.$sqlendpointid"
        )

    def test_sql_databases_merges_with_two_arg_form(self, tmp_path: Path):
        """The same hostname via Sql.Databases and Sql.Database dedupes into
        one parameter; the two-arg form also yields the endpoint item id."""
        make_item(tmp_path, "lh", "Lakehouse")
        body = (
            'Source = Sql.Databases("HOST-XXXX.datawarehouse.fabric.microsoft.com")\n'
            f'Other = Sql.Database("HOST-XXXX.datawarehouse.fabric.microsoft.com", "lh-{ENDPOINT_ID}")'
        )
        _make_semantic_model(tmp_path, "sm", body)

        d = ParameterDetector(tmp_path)
        d.detect_all()

        host = "HOST-XXXX.datawarehouse.fabric.microsoft.com"
        assert d.parameters[host].source_files == {"sm.SemanticModel/definition/expressions.tmdl"}
        assert ENDPOINT_ID in d.parameters

    @pytest.mark.parametrize("database", [ENDPOINT_ID, f"lh-{ENDPOINT_ID}", "lh"])
    def test_pbidedicated_host_maps_to_the_target_endpoint_host(
        self, tmp_path: Path, database: str
    ):
        """The legacy `.pbidedicated.windows.net` host is an alias of the
        endpoint's `.fabric.microsoft.com` connection string, so it maps to
        `$sqlendpoint` like any other host, with no domain-restoring rewrite."""
        make_item(tmp_path, "lh", "Lakehouse")
        body = f'Source = Sql.Database("HOST-AAAA.datawarehouse.pbidedicated.windows.net", "{database}")'
        _make_semantic_model(tmp_path, "sm", body)

        d = ParameterDetector(tmp_path)
        d.detect_all()

        rules = {
            e["find_value"]: e["replace_value"] for e in d.generate_parameter_yml()["find_replace"]
        }
        assert rules["HOST-AAAA.datawarehouse.pbidedicated.windows.net"] == {
            "_ALL_": "$items.Lakehouse.lh.$sqlendpoint"
        }
        assert not any("pbidedicated" in str(v) for v in rules.values())


# --- pipeline extraction --------------------------------------------------


class TestPipelineExtraction:
    def test_extracts_external_connection(self, tmp_path: Path):
        make_item(tmp_path, "lh", "Lakehouse")
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
        """A pipeline workspaceId anchored by a notebook attachment (the
        repo's own workspace) is remapped to the target."""
        make_item(tmp_path, "lh", "Lakehouse")
        _make_notebook(tmp_path, "nb", _lakehouse_meta(WORKSPACE_ID, LAKEHOUSE_ID))
        _make_pipeline(
            tmp_path,
            "pl",
            {
                "properties": {
                    "activities": [
                        {
                            "typeProperties": {
                                "workspaceId": WORKSPACE_ID,
                                "notebookId": "ignored",
                            }
                        }
                    ]
                }
            },
        )
        d = ParameterDetector(tmp_path)
        d.detect_all()

        assert WORKSPACE_ID in d.parameters
        assert d.parameters[WORKSPACE_ID].description == "Workspace ID"
        assert d.parameters[WORKSPACE_ID].dynamic_variable == "$workspace.$id"
        assert d.offtarget_workspace_refs == []

    def test_foreign_workspaceid_deploys_verbatim(self, tmp_path: Path):
        """a pipeline invoking an item in ANOTHER workspace (one workspace → another) must not have that workspaceId rewritten to the
        deploy target. It is recorded with no dynamic variable — verbatim on
        deploy, but still overridable via manual_parameters."""
        make_item(tmp_path, "lh", "Lakehouse")
        _make_notebook(tmp_path, "nb", _lakehouse_meta(WORKSPACE_ID, LAKEHOUSE_ID))
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

        param = d.parameters[OTHER_WORKSPACE_ID]
        assert param.description == "Cross-workspace reference"
        assert param.dynamic_variable == ""
        assert d.offtarget_workspace_refs == [
            ("pl.DataPipeline/pipeline-content.json", OTHER_WORKSPACE_ID)
        ]
        # Verbatim = excluded from the generated find_replace rules.
        find_values = [e["find_value"] for e in d.generate_parameter_yml()["find_replace"]]
        assert OTHER_WORKSPACE_ID not in find_values

    def test_skips_null_uuid_workspaceid(self, tmp_path: Path):
        make_item(tmp_path, "lh", "Lakehouse")
        _make_pipeline(
            tmp_path,
            "pl",
            {
                "properties": {
                    "activities": [{"typeProperties": {"workspaceId": ParameterDetector.NULL_UUID}}]
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
        """Anchored source section remaps; foreign destination section (a
        cross-workspace copy target) deploys verbatim — including its
        artifactId, which must NOT map to the repo's Lakehouse."""
        make_item(tmp_path, "lh", "Lakehouse")
        _make_notebook(tmp_path, "nb", _lakehouse_meta(WORKSPACE_ID, LAKEHOUSE_ID))
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

        assert d.parameters[WORKSPACE_ID].description == "Workspace ID"
        assert d.parameters[LAKEHOUSE_ID].description == "Lakehouse ID"
        assert d.parameters[OTHER_WORKSPACE_ID].description == "Cross-workspace reference"
        assert d.parameters[ENDPOINT_ID].description == "Cross-workspace reference"
        assert d.parameters[ENDPOINT_ID].dynamic_variable == ""

    def test_copyjob_without_workspaceid_still_maps_artifact(self, tmp_path: Path):
        """No workspaceId in the section → same-workspace copy; the artifact
        keeps its Lakehouse ID mapping."""
        make_item(tmp_path, "lh", "Lakehouse")
        _make_copyjob(
            tmp_path,
            "cj",
            {
                "properties": {
                    "source": {
                        "connectionSettings": {"typeProperties": {"artifactId": LAKEHOUSE_ID}}
                    },
                }
            },
        )
        d = ParameterDetector(tmp_path)
        d.detect_all()

        assert d.parameters[LAKEHOUSE_ID].description == "Lakehouse ID"
        assert d.parameters[LAKEHOUSE_ID].dynamic_variable == "$items.Lakehouse.lh.$id"

    def test_copyjob_null_workspaceid_is_local_not_cross_workspace(self, tmp_path: Path):
        """The null UUID is Fabric's "this workspace" placeholder — never a
        cross-workspace reference (no warning, no parameter), and the
        artifact keeps its same-workspace mapping."""
        make_item(tmp_path, "lh", "Lakehouse")
        _make_copyjob(
            tmp_path,
            "cj",
            {
                "properties": {
                    "destination": {
                        "connectionSettings": {
                            "typeProperties": {
                                "workspaceId": ParameterDetector.NULL_UUID,
                                "artifactId": LAKEHOUSE_ID,
                            }
                        }
                    },
                }
            },
        )
        d = ParameterDetector(tmp_path)
        d.detect_all()

        assert ParameterDetector.NULL_UUID not in d.parameters
        assert d.offtarget_workspace_refs == []
        assert d.parameters[LAKEHOUSE_ID].description == "Lakehouse ID"
        assert d.parameters[LAKEHOUSE_ID].dynamic_variable == "$items.Lakehouse.lh.$id"


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
        make_item(tmp_path, "lh", "Lakehouse")
        if detected_wsid is not None:
            _make_notebook(tmp_path, "n1", _lakehouse_meta(detected_wsid, LAKEHOUSE_ID))
        d = ParameterDetector(tmp_path)
        d.detect_all()
        assert d.should_skip_replacement(target_wsid) is expected


# --- filter_lakehouse_params ---------------------------------------------


class TestFilterLakehouseParams:
    def test_removes_lakehouse_descriptions_only(self, tmp_path: Path):
        d = ParameterDetector(tmp_path)
        d.detect_all()
        # Manually populate parameters with a mix of types
        d.parameters = {
            WORKSPACE_ID: DetectedParameter(find_value=WORKSPACE_ID, description="Workspace ID"),
            LAKEHOUSE_ID: DetectedParameter(find_value=LAKEHOUSE_ID, description="Lakehouse ID"),
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

    def test_filtering_every_parameter_does_not_redetect(self, tmp_path: Path):
        """A repo whose only detected parameters are lakehouse ones must stay
        filtered: an empty parameter set is a result, not "not detected yet"."""
        make_item(tmp_path, "lh", "Lakehouse")
        _make_semantic_model(
            tmp_path, "sm", 'Source = Sql.Databases("HOST-XXXX.datawarehouse.fabric.microsoft.com")'
        )
        d = ParameterDetector(tmp_path)
        d.detect_all()

        assert d.filter_lakehouse_params() == ["HOST-XXXX.datawarehouse.fabric.microsoft.com"]
        assert d.generate_parameter_yml()["find_replace"] == []


# --- dynamic variables ----------------------------------------------------


class TestDynamicVariable:
    @pytest.mark.parametrize(
        ("items", "kind", "expected"),
        [
            ([], Kind.WORKSPACE, "$workspace.$id"),
            (["lh.Lakehouse"], Kind.LAKEHOUSE, "$items.Lakehouse.lh.$id"),
            (["lh.Lakehouse"], Kind.SQL_ENDPOINT_HOST, "$items.Lakehouse.lh.$sqlendpoint"),
            (["lh.Lakehouse"], Kind.SQL_ENDPOINT_ID, "$items.Lakehouse.lh.$sqlendpointid"),
            (["lh.Lakehouse"], Kind.CONNECTION, ""),
            (["mydb.SQLDatabase"], Kind.SQLDB_ID, "$items.SQLDatabase.mydb.$id"),
            # fabric-cicd resolves a SQLDatabase's $sqlendpoint to its serverFqdn.
            (["mydb.SQLDatabase"], Kind.SQLDB_HOST, "$items.SQLDatabase.mydb.$sqlendpoint"),
            (["wh.Warehouse"], Kind.WAREHOUSE, "$items.Warehouse.wh.$id"),
            # A mirrored database's SQL endpoint shares the lakehouse
            # `datawarehouse.*` domain, so it answers for endpoint references...
            (
                ["mdb.MirroredDatabase"],
                Kind.SQL_ENDPOINT_HOST,
                "$items.MirroredDatabase.mdb.$sqlendpoint",
            ),
            (
                ["mdb.MirroredDatabase"],
                Kind.SQL_ENDPOINT_ID,
                "$items.MirroredDatabase.mdb.$sqlendpointid",
            ),
            (["mdb.MirroredDatabase"], Kind.LAKEHOUSE, ""),
            # With a lakehouse too, either answers for the workspace's host...
            (
                ["lh.Lakehouse", "mdb.MirroredDatabase"],
                Kind.SQL_ENDPOINT_HOST,
                "$items.Lakehouse.lh.$sqlendpoint",
            ),
            # ...but an endpoint id could be either one's.
            (["lh.Lakehouse", "mdb.MirroredDatabase"], Kind.SQL_ENDPOINT_ID, ""),
        ],
    )
    def test_kind_maps_to_dynamic_variable(
        self, tmp_path: Path, items: list[str], kind: Kind, expected: str
    ):
        for item in items:
            name, item_type = item.split(".")
            make_item(tmp_path, name, item_type)
        d = ParameterDetector(tmp_path)
        d._index_items()
        assert d._dynamic(kind, "find-value") == expected

    def test_name_shared_by_items_of_two_types_is_not_bound(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        make_item(tmp_path, "sales", "Lakehouse")
        make_item(tmp_path, "sales", "MirroredDatabase")
        d = ParameterDetector(tmp_path)
        d._index_items()
        with caplog.at_level("WARNING", logger="fabric_deploy.detect"):
            assert d._dynamic(Kind.SQL_ENDPOINT_ID, "find-value", name="sales") == ""
        assert "sales.Lakehouse, sales.MirroredDatabase" in caplog.text


# --- warehouse attachments (dependencies.warehouse) ------------------------

WAREHOUSE_EP_ID = "66666666-6666-6666-6666-666666666666"


def _warehouse_meta(prefix: str, wh_id: str, wh_type: str) -> str:
    p = prefix
    return (
        f"{p} META {{\n"
        f'{p} META   "dependencies": {{\n'
        f'{p} META     "warehouse": {{\n'
        f'{p} META       "default_warehouse": "{wh_id}",\n'
        f'{p} META       "known_warehouses": [\n'
        f'{p} META         {{"id": "{wh_id}", "type": "{wh_type}"}}\n'
        f"{p} META       ]\n"
        f"{p} META     }}\n"
        f"{p} META   }}\n"
        f"{p} META }}\n"
    )


class TestWarehouseAttachmentExtraction:
    @pytest.mark.parametrize("prefix,ext", [("#", "py"), ("--", "sql")])
    def test_lakewarehouse_maps_to_lakehouse_sqlendpointid(
        self, tmp_path: Path, prefix: str, ext: str
    ):
        """T-SQL notebooks bound to the lakehouse SQL analytics endpoint."""
        make_item(tmp_path, "lh", "Lakehouse")
        _make_notebook(
            tmp_path, "n1", _warehouse_meta(prefix, WAREHOUSE_EP_ID, "Lakewarehouse"), ext=ext
        )
        d = ParameterDetector(tmp_path)
        d.detect_all()

        assert WAREHOUSE_EP_ID in d.parameters
        p = d.parameters[WAREHOUSE_EP_ID]
        assert p.description == "Lakehouse SQL Analytics Endpoint Item ID"
        assert p.dynamic_variable == "$items.Lakehouse.lh.$sqlendpointid"
        assert d.unresolved_warehouse_bindings == []

    def test_mountedwarehouse_deferred_when_sqldb_in_repo(self, tmp_path: Path):
        """MountedWarehouse resolves at deploy time — no dynamic, no warning."""
        make_item(tmp_path, "lh", "Lakehouse")
        make_item(
            tmp_path, "mydb", "SQLDatabase", folder="ERP/mydb.SQLDatabase"
        )  # nested on purpose
        _make_notebook(tmp_path, "n1", _warehouse_meta("#", WAREHOUSE_EP_ID, "MountedWarehouse"))
        d = ParameterDetector(tmp_path)
        d.detect_all()

        p = d.parameters[WAREHOUSE_EP_ID]
        assert p.description == "SQL Database SQL Endpoint Item ID"
        assert p.dynamic_variable == ""  # literal filled in by the deployer
        assert d.unresolved_warehouse_bindings == []
        # The nested .SQLDatabase must be visible to name detection.
        assert d.sqldatabase_names == ["mydb"]

    def test_mountedwarehouse_warns_without_sqldb_item(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        make_item(tmp_path, "lh", "Lakehouse")
        _make_notebook(tmp_path, "n1", _warehouse_meta("#", WAREHOUSE_EP_ID, "MountedWarehouse"))
        d = ParameterDetector(tmp_path)
        with caplog.at_level("WARNING", logger="fabric_deploy.detect"):
            d.detect_all()

        assert len(d.unresolved_warehouse_bindings) == 1
        assert d.unresolved_warehouse_bindings[0][1] == WAREHOUSE_EP_ID
        assert any(WAREHOUSE_EP_ID in r.message for r in caplog.records)

    def test_datawarehouse_maps_to_warehouse_id(self, tmp_path: Path):
        make_item(tmp_path, "lh", "Lakehouse")
        make_item(tmp_path, "wh", "Warehouse")
        _make_notebook(tmp_path, "n1", _warehouse_meta("#", WAREHOUSE_EP_ID, "Datawarehouse"))
        d = ParameterDetector(tmp_path)
        d.detect_all()

        p = d.parameters[WAREHOUSE_EP_ID]
        assert p.description == "Warehouse ID"
        assert p.dynamic_variable == "$items.Warehouse.wh.$id"
        assert d.unresolved_warehouse_bindings == []

    def test_unknown_attachment_type_warns(self, tmp_path: Path, caplog: pytest.LogCaptureFixture):
        make_item(tmp_path, "lh", "Lakehouse")
        _make_notebook(tmp_path, "n1", _warehouse_meta("#", WAREHOUSE_EP_ID, "SomethingNew"))
        d = ParameterDetector(tmp_path)
        with caplog.at_level("WARNING", logger="fabric_deploy.detect"):
            d.detect_all()

        assert d.parameters[WAREHOUSE_EP_ID].dynamic_variable == ""
        assert len(d.unresolved_warehouse_bindings) == 1
        assert d.unresolved_warehouse_bindings[0][2] == "SomethingNew"

    def test_default_warehouse_without_known_list_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        """A bare default_warehouse with no known_warehouses entry has no type."""
        make_item(tmp_path, "lh", "Lakehouse")
        _make_notebook(
            tmp_path,
            "n1",
            "# META {\n"
            '# META   "dependencies": {\n'
            '# META     "warehouse": {\n'
            f'# META       "default_warehouse": "{WAREHOUSE_EP_ID}"\n'
            "# META     }\n"
            "# META   }\n"
            "# META }\n",
        )
        d = ParameterDetector(tmp_path)
        with caplog.at_level("WARNING", logger="fabric_deploy.detect"):
            d.detect_all()

        assert WAREHOUSE_EP_ID in d.parameters
        assert len(d.unresolved_warehouse_bindings) == 1


class TestNestedItemNameDetection:
    def test_nested_items_detected_top_level_first(self, tmp_path: Path):
        """Items in subfolders are found; top-level item stays primary."""
        make_item(tmp_path, "nested_lh", "Lakehouse", folder="SUB/nested_lh.Lakehouse")
        make_item(tmp_path, "top_lh", "Lakehouse")
        d = ParameterDetector(tmp_path)
        d._index_items()
        assert d._names["Lakehouse"] == ["top_lh", "nested_lh"]


# --- PBIR User Data Function rebind ----------------------------------------

UDF_ITEM_ID = "66666666-6666-6666-6666-666666666666"


def _make_udf(repo: Path, name: str, functions: list[str]) -> None:
    definition = {"functions": [{"name": f} for f in functions]}
    make_item(repo, name, "UserDataFunction", files={"definition.json": definition})


def _data_function_visual(item_id: str, workspace_id: str, function_name: str | None) -> dict:
    """A PBIR action button bound to a User Data Function."""
    metadata: dict = {"autoRefresh": True, "parameters": []}
    if function_name is not None:
        metadata["name"] = function_name
    return {
        "visual": {
            "visualType": "actionButton",
            "visualContainerObjects": {
                "visualLink": [
                    {
                        "properties": {
                            "type": {"expr": {"Literal": {"Value": "'DataFunction'"}}},
                            "dataFunction": {
                                "kind": "ItemLocation",
                                "byReference": {
                                    "itemId": {"expr": {"Literal": {"Value": f"'{item_id}'"}}},
                                    "workspaceId": {
                                        "expr": {"Literal": {"Value": f"'{workspace_id}'"}}
                                    },
                                },
                                "metadata": {"dataFunction": metadata},
                            },
                        }
                    }
                ]
            },
        }
    }


def _make_report_visual(
    repo: Path, name: str, visual: dict[str, object], visual_id: str = "v1"
) -> None:
    path = f"definition/pages/p1/visuals/{visual_id}/visual.json"
    make_item(repo, name, "Report", files={path: visual})


class TestUserDataFunctionRebind:
    def test_bound_button_is_rebound_to_target_workspace(self, tmp_path: Path):
        """The dev itemId/workspaceId become target-resolved dynamics."""
        make_item(tmp_path, "lh", "Lakehouse")
        _make_udf(tmp_path, "Report Functions", ["recalculate_totals"])
        _make_report_visual(
            tmp_path,
            "Finance Report",
            _data_function_visual(UDF_ITEM_ID, WORKSPACE_ID, "recalculate_totals"),
        )
        d = ParameterDetector(tmp_path)
        d.detect_all()

        assert (
            d.parameters[UDF_ITEM_ID].dynamic_variable
            == "$items.UserDataFunction.Report Functions.$id"
        )
        assert d.parameters[WORKSPACE_ID].dynamic_variable == "$workspace.$id"
        assert d.udf_bindings == [
            (
                "Finance Report",
                "recalculate_totals",
                "Report Functions",
                UDF_ITEM_ID,
            )
        ]

    def test_unbound_button_is_left_alone(self, tmp_path: Path):
        """An unassigned button carries "''" for both ids — nothing to rebind."""
        make_item(tmp_path, "lh", "Lakehouse")
        _make_udf(tmp_path, "Fns", ["f"])
        _make_report_visual(tmp_path, "R", _data_function_visual("", "", None))
        d = ParameterDetector(tmp_path)
        d.detect_all()

        assert d.udf_bindings == []
        assert not any(p.description == "User Data Function Item ID" for p in d.parameters.values())

    def test_unknown_function_is_not_rebound(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        """A function no repo item declares must warn, not guess at a host."""
        make_item(tmp_path, "lh", "Lakehouse")
        _make_udf(tmp_path, "Fns", ["something_else"])
        _make_report_visual(
            tmp_path, "R", _data_function_visual(UDF_ITEM_ID, WORKSPACE_ID, "ghost_fn")
        )
        d = ParameterDetector(tmp_path)
        with caplog.at_level("WARNING", logger="fabric_deploy.detect"):
            d.detect_all()

        assert UDF_ITEM_ID not in d.parameters
        assert d.udf_bindings == []
        assert any("not rebound" in r.message for r in caplog.records)

    def test_skipped_buttons_warn_once_per_reason(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        """Two buttons calling the same ghost function -> one aggregated warning."""
        make_item(tmp_path, "lh", "Lakehouse")
        _make_udf(tmp_path, "Fns", ["something_else"])
        _make_report_visual(
            tmp_path,
            "R",
            _data_function_visual(UDF_ITEM_ID, WORKSPACE_ID, "ghost_fn"),
            visual_id="v1",
        )
        _make_report_visual(
            tmp_path,
            "R",
            _data_function_visual(UDF_ITEM_ID, WORKSPACE_ID, "ghost_fn"),
            visual_id="v2",
        )
        d = ParameterDetector(tmp_path)
        with caplog.at_level("WARNING", logger="fabric_deploy.detect"):
            d.detect_all()

        assert len(d.udf_skipped) == 2
        warnings = [r for r in caplog.records if "not rebound" in r.message]
        assert len(warnings) == 1
        assert "2 User Data Function button(s) in 'R'" in warnings[0].message
        assert "'ghost_fn'" in warnings[0].message

    def test_ambiguous_function_is_not_rebound(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        """Two items declaring the same function -> the host can't be inferred."""
        make_item(tmp_path, "lh", "Lakehouse")
        _make_udf(tmp_path, "FnsA", ["shared_fn"])
        _make_udf(tmp_path, "FnsB", ["shared_fn"])
        _make_report_visual(
            tmp_path, "R", _data_function_visual(UDF_ITEM_ID, WORKSPACE_ID, "shared_fn")
        )
        d = ParameterDetector(tmp_path)
        with caplog.at_level("WARNING", logger="fabric_deploy.detect"):
            d.detect_all()

        assert UDF_ITEM_ID not in d.parameters
        assert any("ambiguous" in r.message for r in caplog.records)

    def test_malformed_literal_is_not_rebound(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        """A byReference that isn't the expected literal shape is left verbatim."""
        make_item(tmp_path, "lh", "Lakehouse")
        _make_udf(tmp_path, "Fns", ["f"])
        visual = _data_function_visual(UDF_ITEM_ID, WORKSPACE_ID, "f")
        link = visual["visual"]["visualContainerObjects"]["visualLink"][0]
        link["properties"]["dataFunction"]["byReference"]["itemId"] = {"expr": "nope"}
        _make_report_visual(tmp_path, "R", visual)
        d = ParameterDetector(tmp_path)
        with caplog.at_level("WARNING", logger="fabric_deploy.detect"):
            d.detect_all()

        assert UDF_ITEM_ID not in d.parameters
        assert any("no usable" in r.message for r in caplog.records)

    def test_button_in_a_repo_without_udf_items_is_not_rebound(self, tmp_path: Path):
        """With no UserDataFunction item the button still calls another workspace's
        function: it is recorded for the unbound-UDF guard, not passed over."""
        make_item(tmp_path, "lh", "Lakehouse")
        _make_report_visual(tmp_path, "R", _data_function_visual(UDF_ITEM_ID, WORKSPACE_ID, "f"))
        d = ParameterDetector(tmp_path)
        d.detect_all()

        assert UDF_ITEM_ID not in d.parameters
        assert d.udf_bindings == []
        assert [(r, fn) for r, fn, _, _ in d.udf_skipped] == [("R", "f")]


# --- lakehouse auto-binding / source-workspace anchors ----------------------

LOGICAL_ID = "6a6a6a6a-6a6a-6a6a-6a6a-6a6a6a6a6a6a"
OTHER_LOGICAL_ID = "88888888-8888-8888-8888-888888888888"
PHYSICAL_LAKEHOUSE_ID = "77777777-7777-7777-7777-777777777777"


def _auto_bound_meta(logical_id: str, name: str = "lh") -> str:
    """Notebook META as Fabric writes it with lakehouse auto-binding on:
    the lakehouse's logicalId and the null-UUID workspace."""
    return "\n".join(
        [
            "# META {",
            '# META   "dependencies": {',
            '# META     "lakehouse": {',
            f'# META       "default_lakehouse": "{logical_id}",',
            f'# META       "default_lakehouse_name": "{name}",',
            f'# META       "default_lakehouse_workspace_id": "{ParameterDetector.NULL_UUID}"',
            "# META     }",
            "# META   }",
            "# META }",
        ]
    )


def _enable_auto_binding(repo: Path, notebook_dir: str) -> None:
    settings = json.dumps({"auto-binding": {"lakehouse": "on"}})
    (repo / notebook_dir / "notebook-settings.json").write_text(settings)


def _pinned_notebook_activity(workspace_id: str) -> dict:
    return {
        "properties": {
            "activities": [{"typeProperties": {"workspaceId": workspace_id, "notebookId": "x"}}]
        }
    }


def _auto_bound_repo(repo: Path) -> None:
    """A lakehouse, one auto-bound notebook, one pipeline pinned to the
    workspace the repo was exported from."""
    make_item(repo, "lh", "Lakehouse", logical_id=LOGICAL_ID)
    _make_notebook(repo, "nb", _auto_bound_meta(LOGICAL_ID))
    _enable_auto_binding(repo, "nb.Notebook")
    _make_pipeline(repo, "pl", _pinned_notebook_activity(WORKSPACE_ID))


class TestLakehouseAutoBinding:
    def test_auto_bound_reference_needs_no_rule(self, tmp_path: Path):
        """An auto-bound notebook names its lakehouse by git logicalId in the
        null-UUID workspace; fabric-cicd swaps logicalIds for the deployed
        ids before parameters apply, so a rule would never match."""
        make_item(tmp_path, "first", "Lakehouse", logical_id=OTHER_LOGICAL_ID)
        make_item(tmp_path, "second", "Lakehouse", logical_id=LOGICAL_ID)
        _make_notebook(tmp_path, "nb", _auto_bound_meta(LOGICAL_ID, "second"))
        d = ParameterDetector(tmp_path)
        d.detect_all()

        assert d.parameters == {}

    def test_auto_bound_notebooks_are_listed(self, tmp_path: Path):
        make_item(tmp_path, "lh", "Lakehouse", logical_id=LOGICAL_ID)
        _make_notebook(tmp_path, "nb", _auto_bound_meta(LOGICAL_ID))
        _enable_auto_binding(tmp_path, "nb.Notebook")
        _make_notebook(tmp_path, "legacy", _lakehouse_meta(WORKSPACE_ID, LAKEHOUSE_ID))
        d = ParameterDetector(tmp_path)
        d.detect_all()

        assert d.auto_bound_notebooks == ["nb.Notebook"]

    def test_auto_bound_repo_has_no_content_anchor(self, tmp_path: Path):
        """With every notebook auto-bound nothing in the content names the
        source workspace, so a pipeline's pinned workspaceId is a
        cross-workspace reference until a source workspace is supplied."""
        _auto_bound_repo(tmp_path)
        d = ParameterDetector(tmp_path)
        d.detect_all()

        param = d.parameters[WORKSPACE_ID]
        assert param.description == "Cross-workspace reference"
        assert param.dynamic_variable == ""
        assert d.offtarget_workspace_refs == [
            ("pl.DataPipeline/pipeline-content.json", WORKSPACE_ID)
        ]

    def test_seeded_source_workspace_anchors_pipeline_ref(self, tmp_path: Path):
        _auto_bound_repo(tmp_path)
        d = ParameterDetector(tmp_path, source_workspace_ids=[WORKSPACE_ID])
        d.detect_all()

        param = d.parameters[WORKSPACE_ID]
        assert param.description == "Workspace ID"
        assert param.dynamic_variable == "$workspace.$id"
        assert d.offtarget_workspace_refs == []

    def test_seed_without_references_emits_no_rule(self, tmp_path: Path):
        """The seed is an anchor, not a find/replace rule of its own."""
        make_item(tmp_path, "lh", "Lakehouse", logical_id=LOGICAL_ID)
        _make_notebook(tmp_path, "nb", _auto_bound_meta(LOGICAL_ID))
        d = ParameterDetector(tmp_path, source_workspace_ids=[WORKSPACE_ID])
        d.detect_all()

        assert WORKSPACE_ID not in d.parameters

    def test_seed_does_not_anchor_a_foreign_workspace(self, tmp_path: Path):
        """The cross-workspace rule still holds: a workspaceId that is not the source deploys verbatim."""
        make_item(tmp_path, "lh", "Lakehouse", logical_id=LOGICAL_ID)
        _make_notebook(tmp_path, "nb", _auto_bound_meta(LOGICAL_ID))
        _make_pipeline(tmp_path, "pl", _pinned_notebook_activity(OTHER_WORKSPACE_ID))
        d = ParameterDetector(tmp_path, source_workspace_ids=[WORKSPACE_ID])
        d.detect_all()

        assert d.parameters[OTHER_WORKSPACE_ID].description == "Cross-workspace reference"
        assert d.offtarget_workspace_refs == [
            ("pl.DataPipeline/pipeline-content.json", OTHER_WORKSPACE_ID)
        ]

    def test_report_udf_workspace_anchors_pipeline_ref(self, tmp_path: Path):
        """A bound UDF button names the repo's own workspace. That anchor must
        reach a pipeline reference whatever order the scans run in."""
        _auto_bound_repo(tmp_path)
        _make_udf(tmp_path, "Functions", ["recalculate_totals"])
        _make_report_visual(
            tmp_path,
            "Finance Report",
            _data_function_visual(UDF_ITEM_ID, WORKSPACE_ID, "recalculate_totals"),
        )
        d = ParameterDetector(tmp_path)
        d.detect_all()

        assert d.parameters[WORKSPACE_ID].dynamic_variable == "$workspace.$id"
        assert d.offtarget_workspace_refs == []

    def test_anchor_upgrades_a_cross_workspace_parameter(self, tmp_path: Path):
        """Anchoring after classification repairs the parameter and the
        off-target record, so scan order cannot change the outcome."""
        _auto_bound_repo(tmp_path)
        d = ParameterDetector(tmp_path)
        d.detect_all()
        assert d.parameters[WORKSPACE_ID].dynamic_variable == ""

        d._anchor_workspace(WORKSPACE_ID)

        param = d.parameters[WORKSPACE_ID]
        assert param.description == "Workspace ID"
        assert param.dynamic_variable == "$workspace.$id"
        assert d.offtarget_workspace_refs == []
        assert WORKSPACE_ID in d._source_workspace_ids

    def test_source_ids_registered_where_item_files_use_them(self, tmp_path: Path):
        """Legacy metadata made the source workspace and lakehouse ids detected
        values, so an abfss literal in notebook code was rewritten too. The
        deployer-resolved ids restore that: a rule per id, only where an item
        file contains it."""
        make_item(tmp_path, "lh", "Lakehouse", logical_id=LOGICAL_ID)
        body = (
            _auto_bound_meta(LOGICAL_ID)
            + "\n# CELL ********************\n\n"
            + f'p = "abfss://{WORKSPACE_ID}@onelake.dfs.fabric.microsoft.com/'
            + f'{PHYSICAL_LAKEHOUSE_ID}/Files/x"\n'
        )
        _make_notebook(tmp_path, "nb", body)
        d = ParameterDetector(
            tmp_path,
            source_workspace_ids=[WORKSPACE_ID],
            source_lakehouse_ids={PHYSICAL_LAKEHOUSE_ID: "lh"},
        )
        d.detect_all()

        lh = d.parameters[PHYSICAL_LAKEHOUSE_ID]
        assert lh.description == "Lakehouse ID"
        assert lh.dynamic_variable == "$items.Lakehouse.lh.$id"
        assert lh.source_files == {"nb.Notebook/notebook-content.py"}
        ws = d.parameters[WORKSPACE_ID]
        assert ws.dynamic_variable == "$workspace.$id"
        assert ws.source_files == {"nb.Notebook/notebook-content.py"}

    def test_source_lakehouse_id_ignored_when_unreferenced(self, tmp_path: Path):
        make_item(tmp_path, "lh", "Lakehouse", logical_id=LOGICAL_ID)
        _make_notebook(tmp_path, "nb", _auto_bound_meta(LOGICAL_ID))
        d = ParameterDetector(
            tmp_path,
            source_workspace_ids=[WORKSPACE_ID],
            source_lakehouse_ids={PHYSICAL_LAKEHOUSE_ID: "lh"},
        )
        d.detect_all()

        assert PHYSICAL_LAKEHOUSE_ID not in d.parameters

    def test_source_lakehouse_id_ignored_for_unknown_lakehouse(self, tmp_path: Path):
        """A source lakehouse with no repo item can't resolve in the target."""
        make_item(tmp_path, "lh", "Lakehouse", logical_id=LOGICAL_ID)
        _make_notebook(
            tmp_path,
            "nb",
            _auto_bound_meta(LOGICAL_ID) + f"\n# CELL\n\nx = '{PHYSICAL_LAKEHOUSE_ID}'\n",
        )
        d = ParameterDetector(
            tmp_path,
            source_workspace_ids=[WORKSPACE_ID],
            source_lakehouse_ids={PHYSICAL_LAKEHOUSE_ID: "elsewhere"},
        )
        d.detect_all()

        assert PHYSICAL_LAKEHOUSE_ID not in d.parameters

    def test_legacy_metadata_still_anchors(self, tmp_path: Path):
        """Repos without auto-binding keep working with no seed at all."""
        make_item(tmp_path, "lh", "Lakehouse")
        _make_notebook(tmp_path, "nb", _lakehouse_meta(WORKSPACE_ID, LAKEHOUSE_ID))
        _make_pipeline(tmp_path, "pl", _pinned_notebook_activity(WORKSPACE_ID))
        d = ParameterDetector(tmp_path)
        d.detect_all()

        assert d.parameters[WORKSPACE_ID].dynamic_variable == "$workspace.$id"
        assert d.parameters[LAKEHOUSE_ID].dynamic_variable == "$items.Lakehouse.lh.$id"
        assert d.offtarget_workspace_refs == []
        assert d.auto_bound_notebooks == []


# --- binding a reference to its own repo item --------------------------------

HUB_LAKEHOUSE_ID = "99999999-9999-9999-9999-999999999999"


class TestItemBinding:
    def test_notebook_binds_to_its_named_lakehouse(self, tmp_path: Path):
        make_item(tmp_path, "first", "Lakehouse")
        make_item(tmp_path, "second", "Lakehouse")
        _make_notebook(tmp_path, "nb", _lakehouse_meta(WORKSPACE_ID, LAKEHOUSE_ID, "second"))
        d = ParameterDetector(tmp_path)
        d.detect_all()

        assert d.parameters[LAKEHOUSE_ID].dynamic_variable == "$items.Lakehouse.second.$id"
        assert d.parameters[WORKSPACE_ID].dynamic_variable == "$workspace.$id"

    def test_unnamed_reference_is_not_bound_to_one_of_several(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        """Nothing says which lakehouse a same-workspace CopyJob artifact is,
        so with several in the repo it is left unmapped, loudly."""
        make_item(tmp_path, "first", "Lakehouse")
        make_item(tmp_path, "second", "Lakehouse")
        _make_copyjob(
            tmp_path,
            "cj",
            {
                "properties": {
                    "source": {
                        "connectionSettings": {"typeProperties": {"artifactId": LAKEHOUSE_ID}}
                    }
                }
            },
        )
        d = ParameterDetector(tmp_path)
        with caplog.at_level("WARNING", logger="fabric_deploy.detect"):
            d.detect_all()

        assert d.parameters[LAKEHOUSE_ID].dynamic_variable == ""
        assert any(LAKEHOUSE_ID in r.message and "second" in r.message for r in caplog.records)

    def test_direct_lake_model_binds_to_the_lakehouse_a_notebook_names(self, tmp_path: Path):
        make_item(tmp_path, "first", "Lakehouse")
        make_item(tmp_path, "second", "Lakehouse")
        _make_notebook(tmp_path, "nb", _lakehouse_meta(WORKSPACE_ID, LAKEHOUSE_ID, "second"))
        _make_semantic_model(
            tmp_path,
            "sm",
            f'Source = AzureStorage.DataLake("https://onelake.dfs.fabric.microsoft.com/'
            f'{WORKSPACE_ID}/{LAKEHOUSE_ID}")',
        )
        d = ParameterDetector(tmp_path)
        d.detect_all()

        assert d.parameters[LAKEHOUSE_ID].dynamic_variable == "$items.Lakehouse.second.$id"
        assert len(d.parameters[LAKEHOUSE_ID].source_files) == 2

    def test_sql_database_binds_by_the_database_name(self, tmp_path: Path):
        make_item(tmp_path, "db1", "SQLDatabase")
        make_item(tmp_path, "db2", "SQLDatabase")
        _make_semantic_model(
            tmp_path,
            "sm",
            f'Source = Sql.Database("HOST-YYYY.database.fabric.microsoft.com", "db2-{ENDPOINT_ID}")',
        )
        d = ParameterDetector(tmp_path)
        d.detect_all()

        assert d.parameters[ENDPOINT_ID].dynamic_variable == "$items.SQLDatabase.db2.$id"

    def test_notebook_attached_outside_the_repo_deploys_unchanged(self, tmp_path: Path):
        """A lakehouse no repo item matches (a shared hub elsewhere) is not
        rewired to the repo's lakehouse, and its workspace is not an anchor."""
        make_item(tmp_path, "lh", "Lakehouse")
        _make_notebook(tmp_path, "nb", _lakehouse_meta(OTHER_WORKSPACE_ID, HUB_LAKEHOUSE_ID, "hub"))
        d = ParameterDetector(tmp_path)
        d.detect_all()

        assert d.parameters[HUB_LAKEHOUSE_ID].dynamic_variable == ""
        assert d.parameters[OTHER_WORKSPACE_ID].dynamic_variable == ""
        assert OTHER_WORKSPACE_ID not in d._source_workspace_ids

    @pytest.mark.parametrize(("sources_complete", "rewritten"), [(True, False), (False, True)])
    def test_lakehouse_named_like_a_repo_one_in_another_workspace(
        self, tmp_path: Path, sources_complete: bool, rewritten: bool
    ):
        """With the repo's workspaces listed, a workspace outside them is another
        workspace whatever its lakehouse is called, so a pipeline invoking a
        notebook there keeps pointing there. Offline, the name is all there is."""
        make_item(tmp_path, "lh", "Lakehouse")
        _make_notebook(tmp_path, "nb", _lakehouse_meta(OTHER_WORKSPACE_ID, HUB_LAKEHOUSE_ID, "lh"))
        _make_pipeline(
            tmp_path,
            "pl",
            {
                "properties": {
                    "activities": [{"typeProperties": {"workspaceId": OTHER_WORKSPACE_ID}}]
                }
            },
        )
        d = ParameterDetector(
            tmp_path, source_workspace_ids=[WORKSPACE_ID], sources_complete=sources_complete
        )
        d.detect_all()

        assert bool(d.parameters[OTHER_WORKSPACE_ID].dynamic_variable) == rewritten
        assert bool(d.parameters[HUB_LAKEHOUSE_ID].dynamic_variable) == rewritten
        refs = {f for f, ws in d.offtarget_workspace_refs if ws == OTHER_WORKSPACE_ID}
        expected = {"nb.Notebook/notebook-content.py", "pl.DataPipeline/pipeline-content.json"}
        assert refs == (set() if rewritten else expected)


class TestForeignDirectLakeSource:
    BODY = (
        'Source = AzureStorage.DataLake("https://onelake.dfs.fabric.microsoft.com/'
        f'{OTHER_WORKSPACE_ID}/{HUB_LAKEHOUSE_ID}")'
    )

    def test_foreign_workspace_deploys_unchanged(self, tmp_path: Path):
        """A Direct Lake model over another workspace's lakehouse keeps both
        ids: neither is one of this repo's workspaces or lakehouses."""
        make_item(tmp_path, "lh", "Lakehouse")
        _make_semantic_model(tmp_path, "sm", self.BODY)
        d = ParameterDetector(tmp_path, source_workspace_ids=[WORKSPACE_ID])
        d.detect_all()

        assert d.parameters[OTHER_WORKSPACE_ID].dynamic_variable == ""
        assert d.parameters[HUB_LAKEHOUSE_ID].dynamic_variable == ""
        assert d.generate_parameter_yml()["find_replace"] == []
        assert d.offtarget_workspace_refs == [
            ("sm.SemanticModel/definition/expressions.tmdl", OTHER_WORKSPACE_ID)
        ]

    def test_sibling_workspace_is_rewired(self, tmp_path: Path):
        make_item(tmp_path, "lh", "Lakehouse")
        _make_semantic_model(tmp_path, "sm", self.BODY)
        d = ParameterDetector(tmp_path, source_workspace_ids=[OTHER_WORKSPACE_ID])
        d.detect_all()

        assert d.parameters[OTHER_WORKSPACE_ID].dynamic_variable == "$workspace.$id"
        assert d.parameters[HUB_LAKEHOUSE_ID].dynamic_variable == "$items.Lakehouse.lh.$id"

    def test_known_lakehouse_anchors_its_workspace(self, tmp_path: Path):
        """A seed lakehouse id (listed from a sibling) identifies the source
        even when its workspace was not seeded."""
        make_item(tmp_path, "lh", "Lakehouse")
        _make_semantic_model(tmp_path, "sm", self.BODY)
        d = ParameterDetector(tmp_path, source_lakehouse_ids={HUB_LAKEHOUSE_ID: "lh"})
        d.detect_all()

        assert d.parameters[OTHER_WORKSPACE_ID].dynamic_variable == "$workspace.$id"
        assert d.parameters[HUB_LAKEHOUSE_ID].dynamic_variable == "$items.Lakehouse.lh.$id"


class TestFileReading:
    def test_files_written_with_a_bom_are_read(self, tmp_path: Path):
        """Some Fabric and Power BI writers prefix UTF-8 files with a BOM,
        which json.loads rejects unless the read strips it."""
        make_item(tmp_path, "lh", "Lakehouse")
        (tmp_path / "lh.Lakehouse" / ".platform").write_text(
            "﻿" + json.dumps({"metadata": {"displayName": "lh", "type": "Lakehouse"}}),
            encoding="utf-8",
        )
        _make_notebook(tmp_path, "nb", "﻿" + _lakehouse_meta(WORKSPACE_ID, LAKEHOUSE_ID))
        _make_pipeline(tmp_path, "pl", {})
        (tmp_path / "pl.DataPipeline" / "pipeline-content.json").write_text(
            "﻿" + json.dumps({"externalReferences": {"connection": CONNECTION_ID}}),
            encoding="utf-8",
        )
        d = ParameterDetector(tmp_path)
        d.detect_all()

        assert d.parameters[LAKEHOUSE_ID].dynamic_variable == "$items.Lakehouse.lh.$id"
        assert CONNECTION_ID in d.parameters

    def test_warns_about_formats_detection_does_not_read(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        (make_item(tmp_path, "sm", "SemanticModel") / "model.bim").write_text("{}")
        (make_item(tmp_path, "nb", "Notebook") / "notebook-content.ipynb").write_text("{}")
        d = ParameterDetector(tmp_path)
        with caplog.at_level("WARNING", logger="fabric_deploy.detect"):
            d.detect_all()

        warnings = [r.message for r in caplog.records if "model.bim" in r.message]
        assert len(warnings) == 1
        assert "sm.SemanticModel/model.bim" in warnings[0]
        assert "nb.Notebook/notebook-content.ipynb" in warnings[0]


class TestSaveParameterYml:
    def test_saved_file_round_trips(self, tmp_path: Path):
        """Values are YAML-escaped, so quotes, backslashes and colons survive."""
        d = ParameterDetector(tmp_path)
        d.detect_all()
        tricky = 'Sql.Database("host", "db\\\\x"): yes'
        d.parameters = {
            tricky: DetectedParameter(
                find_value=tricky, item_types={"Notebook"}, dynamic_variable="a: 'b'"
            )
        }
        out = tmp_path / "parameter.yml"
        d.save_parameter_yml(out)

        assert yaml.safe_load(out.read_text(encoding="utf-8")) == d.generate_parameter_yml()

    def test_rules_apply_to_every_environment_by_default(self, tmp_path: Path):
        """A saved file is for the repo's own parameter.yml, used by every
        environment: fabric-cicd's `_ALL_` key, not one environment's name."""
        make_item(tmp_path, "lh", "Lakehouse")
        _make_notebook(tmp_path, "nb", _lakehouse_meta(WORKSPACE_ID, LAKEHOUSE_ID))
        d = ParameterDetector(tmp_path)
        out = tmp_path / "parameter.yml"
        d.save_parameter_yml(out)

        entries = yaml.safe_load(out.read_text(encoding="utf-8"))["find_replace"]
        assert [list(e["replace_value"]) for e in entries] == [["_ALL_"], ["_ALL_"]]
