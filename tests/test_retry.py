"""Unit tests for WorkspaceDeployer._deploy_items retry logic.

Pins three behaviors:
1. Transient error → retry → success (no exception).
2. Transient error × (max_retries + 1) → raises (we ran out of retries).
3. Non-transient error → no retry, raises immediately.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from fabric_deploy.workspace import WorkspaceDeployer


def _seed(repo: Path) -> None:
    lh = repo / "lh.Lakehouse"
    lh.mkdir()
    (lh / ".platform").write_text(
        json.dumps({"metadata": {"displayName": "lh", "type": "Lakehouse"}})
    )
    (repo / "fabdep.yml").write_text('workspace_base_name: "ws"\n')


def _make_deployer(tmp_path: Path, max_retries: int = 1) -> WorkspaceDeployer:
    _seed(tmp_path)
    return WorkspaceDeployer(
        tmp_path, branch_override="dev", max_retries=max_retries
    )


class TestRetry:
    def test_transient_error_retried_then_succeeds(
        self, tmp_path: Path, mock_fabric_api
    ):
        d = _make_deployer(tmp_path, max_retries=1)

        # First call raises transient, second succeeds.
        publish_mock = MagicMock(
            side_effect=[Exception("Internal server error"), None]
        )

        with (
            patch("fabric_deploy.workspace.FabricWorkspace"),
            patch("fabric_deploy.workspace.publish_all_items", publish_mock),
            patch("fabric_deploy.workspace.unpublish_all_orphan_items"),
        ):
            d._deploy_items(item_types=["Notebook"])

        assert publish_mock.call_count == 2

    def test_max_retries_exhausted_raises(self, tmp_path: Path, mock_fabric_api):
        d = _make_deployer(tmp_path, max_retries=1)

        publish_mock = MagicMock(side_effect=Exception("Internal server error"))

        with (
            patch("fabric_deploy.workspace.FabricWorkspace"),
            patch("fabric_deploy.workspace.publish_all_items", publish_mock),
            patch("fabric_deploy.workspace.unpublish_all_orphan_items"),
        ):
            try:
                d._deploy_items(item_types=["Notebook"])
            except Exception as e:
                assert "Internal server error" in str(e)
            else:
                raise AssertionError("Expected exception when retries exhausted")

        # max_retries=1 means initial attempt + 1 retry = 2 total calls.
        assert publish_mock.call_count == 2

    def test_non_transient_error_not_retried(
        self, tmp_path: Path, mock_fabric_api
    ):
        d = _make_deployer(tmp_path, max_retries=3)

        # Auth error is not transient — must not be retried even with high max_retries.
        publish_mock = MagicMock(side_effect=Exception("401 Unauthorized"))

        with (
            patch("fabric_deploy.workspace.FabricWorkspace"),
            patch("fabric_deploy.workspace.publish_all_items", publish_mock),
            patch("fabric_deploy.workspace.unpublish_all_orphan_items"),
        ):
            try:
                d._deploy_items(item_types=["Notebook"])
            except Exception as e:
                assert "401" in str(e)
            else:
                raise AssertionError("Expected exception to be raised")

        # Only the first attempt — no retry on non-transient.
        assert publish_mock.call_count == 1

    def test_higher_max_retries_allows_more_attempts(
        self, tmp_path: Path, mock_fabric_api
    ):
        d = _make_deployer(tmp_path, max_retries=3)

        # Three transient failures, then success.
        publish_mock = MagicMock(
            side_effect=[
                Exception("Internal server error"),
                Exception("Service unavailable"),
                Exception("Gateway timeout"),
                None,
            ]
        )

        with (
            patch("fabric_deploy.workspace.FabricWorkspace"),
            patch("fabric_deploy.workspace.publish_all_items", publish_mock),
            patch("fabric_deploy.workspace.unpublish_all_orphan_items"),
        ):
            d._deploy_items(item_types=["Notebook"])

        # Initial + 3 retries = 4 calls total.
        assert publish_mock.call_count == 4
