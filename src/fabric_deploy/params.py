"""parameter.yml lifecycle: manual overrides/injections, generation, domain rewrite, cleanup.

fabric-cicd consumes a parameter.yml for find/replace. This module owns building
that file from detected parameters (plus manual overrides), the second-pass FQDN
rewrite fabric-cicd can't express, and removing the generated file afterward —
keeping ~200 lines of file-wrangling out of WorkspaceDeployer.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from .detect import DetectedParameter, find_in_item_files

if TYPE_CHECKING:
    from .detect import ParameterDetector

__all__ = [
    "ParameterFile",
    "apply_manual_overrides",
    "resolve_sqldatabase_hostname_params",
    "resolve_warehouse_endpoint_params",
]


def apply_manual_overrides(
    detector: ParameterDetector, manual_params: list[dict], logger: logging.Logger
) -> int:
    """Apply fabdep.yml manual_parameters onto the detected parameter set.

    Two modes per entry, keyed on whether the find_value was auto-detected:

    - **Override** (the original behavior): the find_value was auto-detected but
      points at an external resource — replace_value (and optionally an
      item_type scope) override the detected dynamic variable.
    - **Injection**: the find_value wasn't auto-detected but does occur
      literally in the repo's item files — detection just doesn't understand
      that context (e.g. a PowerApp app id embedded in report JSON). The entry
      becomes a new parameter, its source_files set to where it was found.

    An entry whose find_value appears in no item file is skipped with a warning:
    that's a typo or a stale entry, and emitting it would silently no-op at
    deploy time. Presence in the repo — not presence in the detector — is the
    typo gate.

    Returns the number of parameters overridden or injected.
    """
    if not manual_params:
        return 0

    undetected: list[dict] = []
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

        else:
            undetected.append(param)

    count += _inject_undetected_params(detector, undetected, logger)
    return count


def _inject_undetected_params(
    detector: ParameterDetector, entries: list[dict], logger: logging.Logger
) -> int:
    """Inject manual_parameters whose find_value detection didn't see.

    Each value is validated by a literal scan of the repo's item files
    (`detect.find_in_item_files`: the file set fabric-cicd applies
    find_replace to, excluding repo-root files like fabdep.yml so an entry
    can't match itself).

    Returns the number of parameters injected.
    """
    if not entries:
        return 0

    matches = find_in_item_files(detector.repo_dir, [e["find_value"] for e in entries])

    count = 0
    for entry in entries:
        find_value = entry["find_value"]
        replace_value = entry["replace_value"]
        found_in = matches[find_value]

        if not found_in:
            logger.warning(
                f"Manual parameter '{find_value}' not found in any item file — "
                "skipped. Check for a typo or a stale entry; the find_value "
                "must match the item source files exactly."
            )
            continue

        # replace_value may be a string or an env-keyed dict ({env: "value"}).
        if isinstance(replace_value, dict):
            replace_value = next(iter(replace_value.values()))

        param_obj = DetectedParameter(
            find_value=find_value,
            description=entry.get("description") or "Manual parameter",
            dynamic_variable=replace_value,
        )
        item_type = entry.get("item_type")
        if item_type:
            param_obj.item_types = (
                set(item_type) if isinstance(item_type, list) else {item_type}
            )
        param_obj.source_files = found_in | {"fabdep.yml (manual injection)"}
        detector.parameters[find_value] = param_obj
        count += 1
        logger.info(
            f"Manual parameter injected ({len(found_in)} item file(s) match): "
            f"{find_value} -> {replace_value}"
        )

    return count


def resolve_warehouse_endpoint_params(
    detector: ParameterDetector, workspace_id: str, logger: logging.Logger
) -> int:
    """Resolve MountedWarehouse notebook bindings to literal target GUIDs.

    A notebook attached to a SQL database binds `default_warehouse` to the
    database's SQLEndpoint *item id*. fabric-cicd has no `$sqlendpointid`
    attribute for SQLDatabase items (only Lakehouse), so this can't be a
    dynamic variable — instead look up the target workspace's SQLEndpoint by
    the repo's SQL database display name and emit the GUID as a literal
    replace_value. Manual overrides win (already-resolved params are skipped).

    Returns the number of parameters resolved.
    """
    pending = [
        p
        for p in detector.parameters.values()
        if p.description == "SQL Database SQL Endpoint Item ID"
        and not p.dynamic_variable
    ]
    if not pending:
        return 0

    db_names = detector.sqldatabase_names
    if not db_names:
        return 0  # already warned as unresolved during detection

    from .client import find_item_by_name

    try:
        # First-name convention, same as _generate_dynamic_variable.
        endpoint = find_item_by_name(workspace_id, "SQLEndpoint", db_names[0])
    except (ValueError, RuntimeError) as e:
        logger.warning(
            f"Could not resolve SQLEndpoint for SQL database '{db_names[0]}' "
            f"in the target workspace — {len(pending)} warehouse binding(s) "
            f"will deploy with source GUIDs: {e}"
        )
        detector.unresolved_warehouse_bindings.extend(
            (sorted(p.source_files)[0], p.find_value, "MountedWarehouse")
            for p in pending
        )
        return 0

    for p in pending:
        p.dynamic_variable = endpoint["id"]
        p.source_files.add("resolved from target workspace SQLEndpoint")
        logger.debug(
            f"Resolved warehouse binding {p.find_value} -> {endpoint['id']} "
            f"(SQLEndpoint '{db_names[0]}')"
        )
    return len(pending)


def resolve_sqldatabase_hostname_params(
    detector: ParameterDetector, workspace_id: str, logger: logging.Logger
) -> int:
    """Resolve SQL database hostnames to literal bare hosts from the target.

    fabric-cicd's `$items.SQLDatabase.<name>.$sqlendpoint` resolves to the
    item's raw `serverFqdn`, which carries an ADO.NET-style `,1433` port
    suffix. The detected find_value is always a bare host (the Sql.Database
    regexes require the closing quote right after the domain), so the dynamic
    would inject the port into every occurrence — harmless in M expressions
    (`Sql.Database("host,1433", …)` connects fine) but fatal in notebook JDBC
    URLs composed as `f"{hostname}:{port}"`. Resolve the host ourselves and
    emit the literal instead. Manual overrides win (their dynamic_variable no
    longer matches the `$items.` pattern).

    Returns the number of parameters resolved.
    """
    pending = [
        p
        for p in detector.parameters.values()
        if p.dynamic_variable.startswith("$items.SQLDatabase.")
        and p.dynamic_variable.endswith(".$sqlendpoint")
    ]
    if not pending:
        return 0

    db_names = detector.sqldatabase_names
    if not db_names:
        return 0

    from .client import get_sqldatabase_server_host

    try:
        # First-name convention, same as _generate_dynamic_variable.
        host = get_sqldatabase_server_host(workspace_id, db_names[0])
    except (ValueError, RuntimeError) as e:
        logger.warning(
            f"Could not resolve the SQL database hostname for '{db_names[0]}' "
            f"in the target workspace — falling back to fabric-cicd's "
            f"$sqlendpoint dynamic, which appends a ',1433' port suffix "
            f"that breaks JDBC-style 'host:port' composition: {e}"
        )
        return 0

    for p in pending:
        p.dynamic_variable = host
        p.source_files.add("resolved from target workspace SQLDatabase")
        logger.debug(f"Resolved SQL database hostname {p.find_value} -> {host}")
    return len(pending)


def _apply_domain_substitutions(param_file: Path, detector: ParameterDetector) -> None:
    """Append a second-pass domain rewrite for pbidedicated SemanticModel connections.

    fabric-cicd 1.0's `$items.Lakehouse.<name>.$sqlendpoint` always returns the
    `.datawarehouse.fabric.microsoft.com` form, but SemanticModels in older
    workspaces reference `.pbidedicated.windows.net`. fabric-cicd has no
    callback, no multi-pass templating, and no equivalent dynamic for that form
    — so we let fabric-cicd do the first pass and append a second find_replace
    that rewrites the resulting FQDN suffix. Targeting includes the database
    name and is scoped to SemanticModel.

    SQL Database endpoints (`.database.fabric.microsoft.com`) need no rewrite:
    their dynamic `$items.SQLDatabase.<name>.$sqlendpoint` resolves to
    serverFqdn, which is already the operational `.database` host.

    TODO: revisit if fabric-cicd grows native multi-domain support.
    """
    pbidedicated_params = [
        param
        for param in detector.parameters.values()
        if param.metadata.get("domain_suffix", "").lower()
        == "pbidedicated.windows.net"
        and param.metadata.get("db_name")
    ]
    if not pbidedicated_params:
        return

    with open(param_file) as f:
        param_data = yaml.safe_load(f)

    if not param_data or "find_replace" not in param_data:
        return

    for param in pbidedicated_params:
        db_name = param.metadata["db_name"]
        domain_type = param.metadata.get("domain_type", "datawarehouse")
        # TMDL serializes the args as `", "` or `","` depending on the writer,
        # so match with `\s*,\s*`. fabric-cicd's regex mode (is_regex) replaces
        # ONLY the single captured group — the suffix — leaving the rest of the
        # first-pass output intact.
        targeted_find = (
            rf"\.{re.escape(domain_type)}\.(fabric\.microsoft\.com)\""
            rf"\s*,\s*\"{re.escape(db_name)}"
        )

        param_data["find_replace"].append({
            "find_value": targeted_find,
            "replace_value": {"fabdep": "pbidedicated.windows.net"},
            "is_regex": "true",
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

        detector.save_parameter_yml(output_path=param_file, environment="fabdep")
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
