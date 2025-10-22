"""Fabric Deploy CLI - CLI wrapper for Microsoft's fabric-cicd package"""

from .workspace import WorkspaceDeployer

__version__ = "0.1.0"
__all__ = ["WorkspaceDeployer"]
