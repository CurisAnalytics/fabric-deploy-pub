"""Azure authentication utilities for Fabric CI/CD."""

import json
import subprocess
import sys
from argparse import Namespace

from .types import AzureAccount

# pyright: reportMissingTypeArgument=false, reportUnknownParameterType=false


def list_accounts(refresh: bool = True) -> list[AzureAccount]:
    """
    List all Azure accounts.

    Args:
        refresh: If True, force refresh from Azure (slower but current data)

    Returns:
        List of Azure accounts
    """
    cmd = ["az", "account", "list", "--output", "json"]
    if refresh:
        cmd.append("--refresh")
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def get_current_account() -> AzureAccount | None:
    """
    Get currently active account.

    Returns:
        Current account or None if no account is set
    """
    accounts = list_accounts()
    return next((a for a in accounts if a.get("isDefault")), None)


def switch_account(subscription_id: str) -> None:
    """
    Switch to a different account.

    Args:
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

    Returns:
        True if account was switched during the operation, False otherwise
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


def is_auth_error(error_msg: str) -> bool:
    """
    Check if error is authentication/authorization related.

    Args:
        error_msg: Error message to check

    Returns:
        True if error is auth-related, False otherwise
    """
    indicators = ["not found", "unauthorized", "forbidden", "401", "403", "404"]
    return any(i in error_msg.lower() for i in indicators)


def handle_auth_command(args: Namespace) -> None:
    """
    Handle auth subcommands with formatted output.

    Args:
        args: Parsed argparse namespace with auth_command attribute
    """
    if args.auth_command == "list":
        from .logging import format_account_list

        current = get_current_account()
        accounts = list_accounts(refresh=True)

        if current:
            print("\nCurrent account:")
            print(f"  Name: {current['name']}")
            print(f"  User: {current['user']['name']}")
            print(f"  Tenant: {current['tenantId']}")
            print(f"  Subscription: {current['id']}\n")

        print(f"Available accounts ({len(accounts)}):\n")
        print(format_account_list(accounts))
        print()

    elif args.auth_command == "switch":
        switch_account(args.subscription_id)
        print(f"\nSwitched to subscription: {args.subscription_id}\n")

    elif args.auth_command == "rename":
        try:
            switched = rename_subscription(args.subscription_id, args.name)
            if switched:
                print(f"\nSwitched to and renamed subscription to: {args.name}\n")
            else:
                print(
                    f"\nRenamed subscription {args.subscription_id} to: {args.name}\n"
                )
        except RuntimeError as e:
            print(f"\nError: {e}\n", file=sys.stderr)
            sys.exit(1)
