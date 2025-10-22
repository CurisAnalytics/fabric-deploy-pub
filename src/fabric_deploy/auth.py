"""Azure authentication utilities for Fabric CI/CD."""

import json
import subprocess

from .types import AzureAccount


def list_accounts(refresh: bool = True) -> list[AzureAccount]:
    """
    List all Azure accounts.
    Args: refresh: If True, force refresh from Azure (slower but current data)
    Returns: List of Azure accounts
    """
    cmd = ["az", "account", "list", "--output", "json"]
    if refresh:
        cmd.append("--refresh")
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def get_current_account() -> AzureAccount | None:
    """
    Get currently active account.
    Returns: Current account or None if no account is set
    """
    accounts = list_accounts()
    return next((a for a in accounts if a.get("isDefault")), None)


def switch_account(subscription_id: str) -> None:
    """
    Switch to a different account.
    subscription_id: Subscription ID to switch to
    """
    _ = subprocess.run(
        ["az", "account", "set", "--subscription", subscription_id],
        check=True,
        capture_output=True,
    )


def rename_subscription(subscription_id: str, new_name: str) -> bool:
    """
    Rename an Azure subscription.
    Automatically switches to the subscription if rename fails due to tenant context.
    Requires Owner permissions on the subscription.

    Args:
        subscription_id: Subscription ID to rename
        new_name: New name for the subscription

    Returns: True if account was switched during the operation, False otherwise
    """
    result = subprocess.run(
        [
            *"az account subscription rename --subscription-id".split(),
            subscription_id,
            "--name",
            new_name,
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        # Force refresh account list to show updated name
        _ = list_accounts(refresh=True)
        return False  # No account switch was needed

    error_msg = result.stderr.strip() if result.stderr else "Unknown error"

    # Check if error is due to wrong tenant context
    if (
        "not found" in error_msg.lower()
        or "could not find" in error_msg.lower()
        or "invalidauthenticationtokentenant" in error_msg.lower()
        or "wrong issuer" in error_msg.lower()
    ):
        # Attempt to switch to the subscription first
        try:
            print(
                f"Subscription not accessible in current context. Switching to {subscription_id}..."
            )
            switch_account(subscription_id)

            # Retry rename after switching
            retry_result = subprocess.run(
                [
                    *"az account subscription rename --subscription-id".split(),
                    subscription_id,
                    "--name",
                    new_name,
                ],
                capture_output=True,
                text=True,
            )

        except subprocess.CalledProcessError as switch_error:
            raise RuntimeError(
                f"Failed to switch to subscription {subscription_id}.\n"
                f"Original error: {error_msg}\n"
                f"Switch error: {switch_error}"
            ) from switch_error

        if retry_result.returncode != 0:
            retry_error = (
                retry_result.stderr.strip() if retry_result.stderr else "Unknown error"
            )
            raise RuntimeError(
                f"Failed to rename subscription after switching context.\n{retry_error}\n\n"
                f"Common issues:\n"
                f"  • Requires Owner permissions (Microsoft.Subscription/rename/action)\n"
                f"  • Cannot rename tenant-level accounts\n"
                f"  • Check your permissions on this subscription"
            )

        # Force refresh account list to show updated name
        _ = list_accounts(refresh=True)
        return True  # Indicate that we switched accounts
    return False
