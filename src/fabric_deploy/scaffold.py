"""Help for writing fabdep.yml: its JSON Schema (`fabdep schema`)."""

from __future__ import annotations

from importlib.resources import files

__all__ = ["schema_text"]


def schema_text() -> str:
    """fabdep.yml's JSON Schema (draft 2020-12), as shipped in the package."""
    return files("fabric_deploy").joinpath("fabdep.schema.json").read_text(encoding="utf-8")
