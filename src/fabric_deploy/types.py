from typing import TypedDict


class AzureUser(TypedDict):
    """Azure user information."""

    name: str
    type: str


class AzureAccount(TypedDict):
    """Azure account information from az CLI."""

    cloudName: str
    homeTenantId: str
    id: str
    isDefault: bool
    name: str
    state: str
    tenantId: str
    user: AzureUser
