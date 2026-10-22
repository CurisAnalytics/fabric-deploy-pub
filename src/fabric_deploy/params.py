"""parameter.yml lifecycle: manual overrides/injections, generation, cleanup.

fabric-cicd consumes a parameter.yml for find/replace. This module owns building
that file from detected parameters (plus manual overrides) and removing the
generated file afterward.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .detect import DetectedParameter, Kind, find_in_item_files

if TYPE_CHECKING:
    from .detect import ParameterDetector

__all__ = [
    "ParameterFile",
    "apply_manual_overrides",
    "resolve_sqldatabase_hostname_params",
    "resolve_warehouse_endpoint_params",
]


def for_environment(replace_value: str | dict, environment: str) -> str | None:
    """The replacement `replace_value` gives `environment`, or None.

    A string applies everywhere. A mapping is looked up by environment name,
    then by its `_ALL_` key (any case), as fabric-cicd reads parameter.yml.
    """
    if not isinstance(replace_value, dict):
        return replace_value
    if environment in replace_value:
        return replace_value[environment]
    return next((v for k, v in replace_value.items() if str(k).lower() == "_all_"), None)


def apply_manual_overrides(
    detector: ParameterDetector,
    manual_params: list[dict],
    logger: logging.Logger,
    environment: str,
) -> int:
    """Apply fabdep.yml manual_parameters for `environment` onto the detected set.

    Two modes per entry, keyed on whether the find_value was auto-detected:

    - **Override**: the find_value was auto-detected — replace_value (and
      optionally an item_type scope) replace the detected rule. An override
      with no value for this environment leaves the detected rule in place.
    - **Injection**: the find_value wasn't auto-detected but does occur
      literally in the repo's item files — detection just doesn't understand
      that context (e.g. a PowerApp app id embedded in report JSON). The entry
      becomes a new parameter, its source_files set to where it was found.

    An entry whose find_value appears in no item file is skipped with a warning:
    that's a typo or a stale entry, and emitting it would silently no-op at
    deploy time. Presence in the repo's item files (the set fabric-cicd applies
    find_replace to; fabdep.yml itself is not one) is the typo gate.

    Returns the number of parameters overridden or injected.
    """
    entries = []
    for entry in manual_params:
        if not entry.get("find_value") or not entry.get("replace_value"):
            logger.warning("Skipping manual parameter with missing find_value or replace_value")
            continue
        entries.append(entry)
    found = find_in_item_files(
        detector.repo_dir,
        [e["find_value"] for e in entries if e["find_value"] not in detector.parameters],
    )

    count = 0
    for entry in entries:
        find_value = entry["find_value"]
        param = detector.parameters.get(find_value)
        if param is None and not found[find_value]:
            logger.warning(
                f"Manual parameter '{find_value}' not found in any item file — "
                "skipped. Check for a typo or a stale entry; the find_value "
                "must match the item source files exactly."
            )
            continue
        value = for_environment(entry["replace_value"], environment)
        if value is None:
            logger.info(
                f"Manual parameter '{find_value}' has no replace_value for "
                f"environment '{environment}' — not applied."
            )
            continue
        if param is None:
            param = DetectedParameter(
                find_value=find_value,
                description=entry.get("description") or "Manual parameter",
                source_files=set(found[find_value]),
            )
            detector.parameters[find_value] = param
            logger.info(
                f"Manual parameter injected ({len(found[find_value])} item file(s) match): "
                f"{find_value} -> {value}"
            )
        param.dynamic_variable = value
        if item_type := entry.get("item_type"):
            param.item_types = {item_type} if isinstance(item_type, str) else set(item_type)
        param.source_files.add("fabdep.yml")
        count += 1
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
        if p.description == Kind.SQLDB_ENDPOINT_ID and not p.dynamic_variable
    ]
    if not pending:
        return 0

    db_names = detector.sqldatabase_names
    if not db_names:
        return 0  # already warned as unresolved during detection

    from .client import find_item_by_name

    try:
        # The binding names no database, so only a repo with one can answer.
        if len(db_names) > 1:
            raise ValueError(f"the repo has several SQL databases ({', '.join(db_names)})")
        endpoint = find_item_by_name(workspace_id, "SQLEndpoint", db_names[0])
    except (ValueError, RuntimeError) as e:
        logger.warning(
            f"Could not resolve the SQLEndpoint of SQL database '{db_names[0]}' "
            f"in the target workspace — {len(pending)} warehouse binding(s) "
            f"will deploy with source GUIDs: {e}"
        )
        detector.unresolved_warehouse_bindings.extend(
            (sorted(p.source_files)[0], p.find_value, "MountedWarehouse") for p in pending
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
        # A host is the workspace's SQL server, so any of the repo's databases
        # resolves it (detect.ITEM_RULES).
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
        environment: str,
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

        detector.save_parameter_yml(output_path=param_file, environment=environment)
        self.created = True

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
            p
            for p in detector.parameters.values()
            if "SemanticModel" in p.item_types
            and not p.dynamic_variable
            and p.description != "External connection ID"
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
