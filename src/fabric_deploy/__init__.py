"""fabric-deploy: deploy Microsoft Fabric workspaces from git.

The primary interface is the `fabdep` CLI; `WorkspaceDeployer` is the
programmatic entry point.
"""

from importlib.metadata import version

from .workspace import WorkspaceDeployer

__version__ = version("fabric-deploy")
__all__ = ["WorkspaceDeployer"]
