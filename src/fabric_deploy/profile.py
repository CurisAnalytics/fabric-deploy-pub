"""The fabdep JSON profile (`~/.azure/azureProfile.fabdep.json`).

A small machine-local store that survives `az account list --refresh`:
persisted SPN accounts, account aliases, and the multi-tenant registry.

This is the single low-level home for that file's I/O — shared by `identity`
(reads the `tenants` registry) and `auth` (reads/writes accounts + aliases),
so neither has to import the other for it.
"""

import json
from pathlib import Path

__all__ = ["FABDEP_PROFILE_PATH"]

FABDEP_PROFILE_PATH = Path.home() / ".azure" / "azureProfile.fabdep.json"


def _read_fabdep_profile() -> dict:
    """Read the fabdep profile, returning an empty structure if missing."""
    if not FABDEP_PROFILE_PATH.exists():
        return {"accounts": [], "aliases": {}}
    return json.loads(FABDEP_PROFILE_PATH.read_text(encoding="utf-8"))


def _write_fabdep_profile(profile: dict) -> None:
    """Write the fabdep profile."""
    FABDEP_PROFILE_PATH.write_text(json.dumps(profile, indent=2), encoding="utf-8")
