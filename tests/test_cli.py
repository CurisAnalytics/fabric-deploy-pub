"""CLI integration tests using test-wkspc submodule."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from fabric_deploy.workspace import WorkspaceDeployer


# Resolve the fabdep binary once. Prefer PATH; fall back to the venv that
# launched pytest (sys.executable's bin dir). Tests skip if neither exists.
_FABDEP = shutil.which("fabdep") or str(Path(sys.executable).parent / "fabdep")
_FABDEP_AVAILABLE = Path(_FABDEP).exists()
_REQUIRES_FABDEP = pytest.mark.skipif(
    not _FABDEP_AVAILABLE,
    reason="fabdep binary not installed on PATH or in sys.executable's bin dir",
)


class TestCLI:
    """Test fabdep CLI commands against test workspace."""

    @pytest.fixture
    def test_workspace_dir(self):
        """Path to test-wkspc submodule."""
        return Path(__file__).parent / "test-wkspc"

    @pytest.mark.integration
    def test_workspace_deployer(self, test_workspace_dir):
        """End-to-end: deploy each item type against the live Fabric workspace.

        This is the canonical integration smoke test. Requires `az login` (or SPN)
        with access to the configured test workspace.
        """
        # Change to test workspace directory
        original_cwd = os.getcwd()
        os.chdir(test_workspace_dir)

        try:
            deployer = WorkspaceDeployer(test_workspace_dir, branch_override="MAIN")
            assert deployer is not None
            print("\nWorkspace object created successfully")

            # Deploy each item type separately
            for item_type in deployer.repo_item_types:
                print(f"Deploying items of type: {item_type}")
                deployer.deploy(item_types=[item_type])
        finally:
            # Restore original directory
            os.chdir(original_cwd)

    @pytest.mark.integration
    @_REQUIRES_FABDEP
    def test_fabdep_no_args(self, test_workspace_dir):
        """Test running fabdep with no arguments exits without error.

        Runs a real deployment against the live workspace; integration-only.
        """
        result = subprocess.run(
            [_FABDEP],
            cwd=test_workspace_dir,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Command failed: {result.stderr}"

    @_REQUIRES_FABDEP
    def test_detect_command(self, test_workspace_dir):
        """Test fabdep detect --save command."""
        result = subprocess.run(
            [_FABDEP, "detect", "--save"],
            cwd=test_workspace_dir,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Command failed: {result.stderr}"

        # Verify output file was created
        detected_file = test_workspace_dir / "parameter.yml.detected"
        assert detected_file.exists(), "parameter.yml.detected not created"

        # Verify it contains expected parameters
        content = detected_file.read_text()
        assert "aaaaaaaa-1111-2222-3333-444444444444" in content  # Workspace ID
        assert "bbbbbbbb-1111-2222-3333-444444444444" in content  # Lakehouse ID
        assert "$workspace.$id" in content
        assert "$items.Lakehouse.fabdep_lakehouse.$id" in content

        # Clean up
        detected_file.unlink()

    @_REQUIRES_FABDEP
    def test_rename_dry_run(self, test_workspace_dir):
        """Test fabdep rename command in dry-run mode."""
        result = subprocess.run(
            [_FABDEP, "rename"],
            cwd=test_workspace_dir,
            capture_output=True,
            text=True,
        )

        # Should succeed (dry-run doesn't make changes)
        assert result.returncode == 0, f"Command failed: {result.stderr}"

