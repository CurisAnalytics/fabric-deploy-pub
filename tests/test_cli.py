"""CLI integration tests using test-wkspc submodule."""

import os
import subprocess
from pathlib import Path

import pytest

from fabric_deploy.workspace import WorkspaceDeployer


class TestCLI:
    """Test fabdep CLI commands against test workspace."""

    @pytest.fixture
    def test_workspace_dir(self):
        """Path to test-wkspc submodule."""
        return Path(__file__).parent / "test-wkspc"

    def test_workspace_deployer(self, test_workspace_dir):
        """Test creating a WorkspaceDeployer instance for the test workspace."""
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

    def test_fabdep_no_args(self, test_workspace_dir):
        """Test running fabdep with no arguments exits without error."""
        result = subprocess.run(
            ["fabdep"],
            cwd=test_workspace_dir,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, f"Command failed: {result.stderr}"

    def test_detect_command(self, test_workspace_dir):
        """Test fabdep detect --save command."""
        result = subprocess.run(
            ["fabdep", "detect", "--save"],
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
        assert "16161616-1616-1616-1616-161616161616" in content  # Workspace ID
        assert "18181818-1818-1818-1818-181818181818" in content  # Lakehouse ID
        assert "$workspace.$id" in content
        assert "$items.Lakehouse.Lakehouse.$id" in content

        # Clean up
        detected_file.unlink()

    def test_rename_dry_run(self, test_workspace_dir):
        """Test fabdep rename command in dry-run mode."""
        result = subprocess.run(
            ["fabdep", "rename"],
            cwd=test_workspace_dir,
            capture_output=True,
            text=True,
        )

        # Should succeed (dry-run doesn't make changes)
        assert result.returncode == 0, f"Command failed: {result.stderr}"
