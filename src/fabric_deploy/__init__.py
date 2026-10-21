"""
fabric-deploy: CLI tool for Microsoft Fabric workspace deployments.

Primary interface is the CLI (`fabdep` command).
Minimal programmatic API for Azure DevOps integration and testing.
"""

from .workspace import WorkspaceDeployer

__version__ = "1.11.1"
__all__ = ["WorkspaceDeployer"]
