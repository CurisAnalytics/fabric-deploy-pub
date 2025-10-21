"""Azure authentication utilities for Fabric CI/CD."""

import json
import subprocess

# pyright: reportMissingTypeArgument=false, reportUnknownParameterType=false


def run_az(args: list[str]) -> dict | list:
    """Run az CLI command and return JSON output."""
    result = subprocess.run(
        ["az", *args, "--output", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def list_accounts() -> list[dict]:
    """List all Azure accounts."""
    return run_az(["account", "list"])  # pyright: ignore[reportReturnType]


def get_current_account() -> dict | None:
    """Get currently active account."""
    accounts = list_accounts()
    accts = (a for a in accounts if a.get("isDefault"))  # pyright: ignore[reportUnknownMemberType]
    return next(accts, None)  # pyright: ignore[reportUnknownArgumentType]


def switch_account(subscription_id: str) -> None:
    """Switch to a different account."""
    _ = subprocess.run(
        ["az", "account", "set", "--subscription", subscription_id],
        check=True,
        capture_output=True,
    )


def is_auth_error(error_msg: str) -> bool:
    """Check if error is authentication/authorization related."""
    indicators = ["not found", "unauthorized", "forbidden", "401", "403", "404"]
    return any(i in error_msg.lower() for i in indicators)
