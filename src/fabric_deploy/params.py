"""parameter.yml lifecycle: manual overrides, generation, domain rewrite, cleanup.

fabric-cicd consumes a parameter.yml for find/replace. This module owns building
that file from detected parameters (plus manual overrides), the second-pass FQDN
rewrite fabric-cicd can't express, and removing the generated file afterward —
keeping ~200 lines of file-wrangling out of WorkspaceDeployer.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from .detect import ParameterDetector

__all__ = ["ParameterFile", "apply_manual_overrides"]


def apply_manual_overrides(
    detector: ParameterDetector, manual_params: list[dict], logger: logging.Logger
) -> int:
    """Apply fabdep.yml manual_parameters overrides onto auto-detected params.

    Lets users override the replace_value (and optionally scope item_type) for a
    parameter whose find_value was auto-detected but points at an external
    resource. The find_value is still auto-detected; only replace_value/item_type
    come from fabdep.yml.

    Returns the number of overrides applied.
    """
    if not manual_params:
        return 0

    logged_warnings: set[str] = set()
    count = 0
    for param in manual_params:
        find_value = param.get("find_value")
        replace_value = param.get("replace_value")
        description = param.get("description")
        item_type = param.get("item_type")

        if not find_value or not replace_value:
            logger.warning(
                "Skipping manual parameter with missing find_value or replace_value"
            )
            continue

        if find_value in detector.parameters:
            param_obj = detector.parameters[find_value]

            # replace_value may be a string or an env-keyed dict ({env: "value"}).
            if isinstance(replace_value, dict):
                replace_value = next(iter(replace_value.values()))
            param_obj.dynamic_variable = replace_value

            if item_type:
                param_obj.item_types = (
                    set(item_type) if isinstance(item_type, list) else {item_type}
                )
                logger.debug(f"Manual item_type override for {find_value}: {item_type}")

            if description:
                param_obj.description = (
                    f"{param_obj.description} (manual override: {description})"
                )
            param_obj.source_files.add("fabdep.yml (manual override)")
            count += 1
            logger.debug(f"Manual override for {find_value}: {replace_value}")

        elif find_value not in logged_warnings:
            logger.warning(
                f"Manual parameter '{find_value}' was not auto-detected. "
                "Make sure the find_value matches exactly what's in the source files."
            )
            logged_warnings.add(find_value)

    return count


def _apply_domain_substitutions(param_file: Path, detector: ParameterDetector) -> None:
    """Append a second-pass domain rewrite for non-default SemanticModel connections.

    fabric-cicd 1.0's `$items.Lakehouse.<name>.$sqlendpoint` always returns
    `.datawarehouse.fabric.microsoft.com` form, but SemanticModels can reference
    `.database.fabric.microsoft.com` (SQL Database endpoints) or
    `.pbidedicated.windows.net` (older workspaces). fabric-cicd has no callback,
    no multi-pass templating, and no equivalent dynamic for those forms — so we
    let fabric-cicd do the first pass and append a second find_replace that
    rewrites the resulting FQDN. Targeting includes the database name and is
    scoped to SemanticModel.

    TODO: revisit if fabric-cicd grows native multi-domain support.
    """
    connections_needing_substitution = []
    for param in detector.parameters.values():
        domain_suffix = param.metadata.get("domain_suffix", "").lower()
        domain_type = param.metadata.get("domain_type", "").lower()
        if not param.metadata.get("db_name"):
            continue
        if domain_type == "database":
            connections_needing_substitution.append(("database_type", param))
        elif domain_suffix == "pbidedicated.windows.net":
            connections_needing_substitution.append(("pbidedicated_suffix", param))

    if not connections_needing_substitution:
        return

    with open(param_file) as f:
        param_data = yaml.safe_load(f)

    if not param_data or "find_replace" not in param_data:
        return

    for substitution_type, param in connections_needing_substitution:
        db_name = param.metadata.get("db_name")
        if not db_name:
            continue

        if substitution_type == "database_type":
            targeted_find = f'.datawarehouse.fabric.microsoft.com","{db_name}'
            targeted_replace = f'.database.fabric.microsoft.com","{db_name}'
        elif substitution_type == "pbidedicated_suffix":
            domain_type = param.metadata.get("domain_type", "datawarehouse")
            targeted_find = f'.{domain_type}.fabric.microsoft.com","{db_name}'
            targeted_replace = f'.{domain_type}.pbidedicated.windows.net","{db_name}'
        else:
            continue

        param_data["find_replace"].append({
            "find_value": targeted_find,
            "replace_value": {"fabdep": targeted_replace},
            "item_type": ["SemanticModel"],
        })

    with open(param_file, "w") as f:
        yaml.dump(param_data, f, default_flow_style=False, sort_keys=False)


class ParameterFile:
    """Owns the generated parameter.yml for one deploy: write then clean up.

    `write()` emits the file from detected parameters and records a human
    summary; `cleanup()` removes it and restores any pre-existing user file.
    `created` says whether `write()` actually produced a file (it may decide
    none is needed).
    """

    def __init__(self, repo_dir: Path, logger: logging.Logger) -> None:
        self.repo_dir = repo_dir
        self.logger = logger
        self.created = False
        self.summary: list[str] = []

    def write(
        self,
        detector: ParameterDetector,
        workspace_id: str,
        manual_count: int,
        skipped_lakehouse_count: int,
    ) -> None:
        """
        Write parameter.yml for deployment.

        All detected parameters are emitted; only manual_parameters overrides
        carry per-type scoping (via the item_type field on the entry). If a
        parameter isn't used by any in-scope file, fabric-cicd will warn but the
        deployment still succeeds.
        """
        if detector.should_skip_replacement(workspace_id):
            self.summary = [
                "Source and target workspace are the same -- deploying directly without replacements",
            ]
            return

        dyns = sum(1 for p in detector.parameters.values() if p.dynamic_variable)
        total = len(detector.parameters)

        if dyns == 0:
            if skipped_lakehouse_count > 0:
                self.summary = [
                    f"Skipped {skipped_lakehouse_count} lakehouse parameter{'s' if skipped_lakehouse_count != 1 else ''} (--skip-lakehouse)",
                    "Deploying without parameter.yml file",
                ]
            else:
                self.summary = [
                    "No parameters detected for replacement",
                    "Deploying without parameter.yml file",
                ]
            return

        param_file = self.repo_dir / "parameter.yml"
        backup_file = self.repo_dir / "parameter.yml.backup"
        summary = []

        if param_file.exists():
            _ = param_file.rename(backup_file)
            summary.append(f"Backed up existing parameter.yml to {backup_file.name}")

        detector.save_parameter_yml(
            output_path=param_file,
            use_dynamic=True,
            environment="fabdep",
            skip_empty=True,
        )
        self.created = True
        _apply_domain_substitutions(param_file, detector)

        external_count = max(total - dyns, 0)
        external_part = (
            f" + {external_count} external connection{'s' if external_count != 1 else ''}"
            if external_count
            else ""
        )
        manual_part = (
            f" + {manual_count} manual parameter{'s' if manual_count != 1 else ''}"
            if manual_count
            else ""
        )
        summary.append(
            f"Found {dyns} dynamic parameter{'s' if dyns != 1 else ''}{external_part}{manual_part}"
        )
        if skipped_lakehouse_count > 0:
            summary.append(
                f"Skipped {skipped_lakehouse_count} lakehouse parameter{'s' if skipped_lakehouse_count != 1 else ''} (--skip-lakehouse)"
            )

        unresolved_sem_model = [
            p for p in detector.parameters.values()
            if "SemanticModel" in p.item_types and not p.dynamic_variable and p.description != "External connection ID"
        ]
        if unresolved_sem_model:
            self.logger.warning(
                f"{len(unresolved_sem_model)} SemanticModel parameter(s) could not be auto-resolved and will be skipped:"
            )
            for p in unresolved_sem_model:
                self.logger.warning(f"  {p.description}: {p.find_value[:60]}...")
            self.logger.warning(
                "Add a manual_parameters entry in fabdep.yml to supply the replacement value."
            )

        self.summary = summary

    def cleanup(self) -> None:
        """Remove the auto-generated parameter.yml and restore the user's backup."""
        param_file = self.repo_dir / "parameter.yml"
        backup_file = self.repo_dir / "parameter.yml.backup"

        if param_file.exists():
            param_file.unlink()
        if backup_file.exists():
            _ = backup_file.rename(param_file)
            self.logger.info("Restored original parameter.yml")
