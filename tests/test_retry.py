"""Unit tests for WorkspaceDeployer._deploy_items retry logic.

One parametrized test pins the retry contract:
- transient error → retry → success
- transient error past max_retries → raises
- non-transient (auth) error → no retry, raises immediately
- higher max_retries → more attempts
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fabric_deploy.workspace import WorkspaceDeployer

TRANSIENT = "Internal server error"


def _seed(repo: Path) -> None:
    lh = repo / "lh.Lakehouse"
    lh.mkdir()
    (lh / ".platform").write_text(
        json.dumps({"metadata": {"displayName": "lh", "type": "Lakehouse"}})
    )
    (repo / "fabdep.yml").write_text('workspace_base_name: "ws"\n')


def _make_deployer(tmp_path: Path, max_retries: int = 1) -> WorkspaceDeployer:
    _seed(tmp_path)
    return WorkspaceDeployer(tmp_path, branch_override="dev", max_retries=max_retries)


@pytest.mark.parametrize(
    ("side_effect", "max_retries", "expected_calls", "raises_match"),
    [
        # transient then success: initial + 1 retry
        ([Exception(TRANSIENT), None], 1, 2, None),
        # transient exhausts retries: initial + 1 retry, then raises
        (Exception(TRANSIENT), 1, 2, TRANSIENT),
        # non-transient: no retry even with max_retries=3
        (Exception("401 Unauthorized"), 3, 1, "401"),
        # three transient failures then success: initial + 3 retries
        (
            [
                Exception(TRANSIENT),
                Exception("Service unavailable"),
                Exception("Gateway timeout"),
                None,
            ],
            3,
            4,
            None,
        ),
    ],
)
def test_deploy_items_retry(
    tmp_path: Path,
    mock_fabric_api,
    side_effect,
    max_retries: int,
    expected_calls: int,
    raises_match: str | None,
):
    d = _make_deployer(tmp_path, max_retries=max_retries)
    publish_mock = MagicMock(side_effect=side_effect)

    with (
        patch("fabric_deploy.workspace.FabricWorkspace"),
        patch("fabric_deploy.workspace.publish_all_items", publish_mock),
        patch("fabric_deploy.workspace.unpublish_all_orphan_items"),
    ):
        if raises_match:
            with pytest.raises(Exception, match=raises_match):
                d._deploy_items(item_types=["Notebook"])
        else:
            d._deploy_items(item_types=["Notebook"])

    assert publish_mock.call_count == expected_calls
