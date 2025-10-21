"""Contoso Fabric CI/CD - CLI wrapper for Microsoft's fabric-cicd package"""

from .cli import FabricDeployer
from .auth import list_accounts, get_current_account, switch_account

__version__ = "0.1.0"
__all__ = ["FabricDeployer", "list_accounts", "get_current_account", "switch_account"]
