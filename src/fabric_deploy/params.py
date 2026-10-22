"""The parameter file a deploy hands fabric-cicd.

Detected parameters, adjusted by fabdep.yml manual_parameters and resolved
against the target workspace where fabric-cicd has no dynamic variable, are
merged with the repo's own parameter.yml into a temporary file.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import get_close_matches
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import yaml

from .detect import DetectedParameter, Kind
from .scope import find_in_item_files

if TYPE_CHECKING:
    from .detect import ParameterDetector

__all__ = [
    "MANUAL_PARAMETER_KEYS",
    "ParameterFile",
    "Rule",
    "apply_manual_overrides",
    "resolve_sqldatabase_hostname_params",
    "resolve_warehouse_endpoint_params",
]

# The keys of a fabdep.yml manual_parameters entry.
MANUAL_PARAMETER_KEYS = ("find_value", "replace_value", "item_type", "description")


def for_environment(replace_value: str | dict[str, str], environment: str) -> str | None:
    """The replacement `replace_value` gives `environment`, or None.

    A string applies everywhere. A mapping is looked up by environment name,
    then by its `_ALL_` key (any case), as fabric-cicd reads parameter.yml.
    """
    if not isinstance(replace_value, dict):
        return replace_value
    if environment in replace_value:
        return replace_value[environment]
    return next((v for k, v in replace_value.items() if str(k).lower() == "_all_"), None)


def _is_str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) and v for v in value)


def _manual_entries(
    manual_params: Sequence[object],
) -> list[tuple[str, str | dict[str, str], set[str], str]]:
    """(find_value, replace_value, item_types, description) for each find_value
    in fabdep.yml manual_parameters; a find_value list yields one per value.

    Raises:
        ValueError: An entry is malformed; the message names it and the key.
    """
    entries = []
    for n, entry in enumerate(manual_params, start=1):
        where = f"fabdep.yml manual_parameters entry {n}"
        if not isinstance(entry, dict):
            raise ValueError(f"{where}: expected a mapping with find_value and replace_value")
        # Unknown keys fail rather than being ignored: parameter.yml's file_path or
        # item_name, or a misspelt item_type, would otherwise widen the rule silently.
        for key in cast(dict[object, object], entry):
            if key not in MANUAL_PARAMETER_KEYS:
                hint = get_close_matches(str(key), MANUAL_PARAMETER_KEYS, n=1, cutoff=0.6)
                raise ValueError(
                    f"{where}: unknown key '{key}'"
                    + (f" — did you mean '{hint[0]}'?" if hint else "")
                    + f" (known keys: {', '.join(MANUAL_PARAMETER_KEYS)})"
                )
        find, replace = entry.get("find_value"), entry.get("replace_value")
        finds = [find] if isinstance(find, str) else find
        if not finds or not _is_str_list(finds):
            raise ValueError(f"{where}: find_value must be a non-empty string or list of strings")
        if not (
            _is_str_list([replace])
            or (
                isinstance(replace, dict)
                and replace
                and _is_str_list([*replace, *replace.values()])
            )
        ):
            raise ValueError(
                f"{where}: replace_value must be a non-empty string, or a mapping of "
                "environment names (or _ALL_) to strings"
            )
        item_type = entry.get("item_type") or []
        item_types = [item_type] if isinstance(item_type, str) else item_type
        if not _is_str_list(item_types):
            raise ValueError(f"{where}: item_type must be a string or list of strings")
        description = entry.get("description") or "Manual parameter"
        if not isinstance(description, str):
            raise ValueError(f"{where}: description must be a string")
        entries += [(f, replace, set(item_types), description) for f in finds]
    return entries


def apply_manual_overrides(
    detector: ParameterDetector,
    manual_params: Sequence[object],
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

    Raises:
        ValueError: An entry is malformed.
    """
    entries = _manual_entries(manual_params)
    found = find_in_item_files(
        detector.repo_dir, [v for v, *_ in entries if v not in detector.parameters]
    )

    count = 0
    for find_value, replace_value, item_types, description in entries:
        param = detector.parameters.get(find_value)
        if param is None and not found[find_value]:
            logger.warning(
                f"Manual parameter '{find_value}' not found in any item file — "
                "skipped. Check for a typo or a stale entry; the find_value "
                "must match the item source files exactly."
            )
            continue
        value = for_environment(replace_value, environment)
        if value is None:
            logger.info(
                f"Manual parameter '{find_value}' has no replace_value for "
                f"environment '{environment}' — not applied."
            )
            continue
        if param is None:
            param = DetectedParameter(
                find_value=find_value,
                description=description,
                source_files=set(found[find_value]),
            )
            detector.parameters[find_value] = param
            logger.info(
                f"Manual parameter injected ({len(found[find_value])} item file(s) match): "
                f"{find_value} -> {value}"
            )
        param.dynamic_variable = value
        if item_types:
            param.item_types = item_types
        param.source_files.add("fabdep.yml")
        count += 1
    return count


def resolve_warehouse_endpoint_params(
    detector: ParameterDetector, workspace_id: str | None, logger: logging.Logger
) -> int:
    """Resolve MountedWarehouse notebook bindings to literal target GUIDs.

    A notebook attached to a SQL database binds `default_warehouse` to the
    database's SQLEndpoint *item id*. fabric-cicd has no `$sqlendpointid`
    attribute for SQLDatabase items (only Lakehouse), so this can't be a
    dynamic variable — instead look up the target workspace's SQLEndpoint by
    the repo's SQL database display name and emit the GUID as a literal
    replace_value. Manual overrides win (already-resolved params are skipped).
    A `workspace_id` of None (the workspace doesn't exist yet) resolves nothing.

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
        if workspace_id is None:
            raise ValueError(_NO_WORKSPACE)
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
    detector: ParameterDetector, workspace_id: str | None, logger: logging.Logger
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
    longer matches the `$items.` pattern). A `workspace_id` of None (the
    workspace doesn't exist yet) resolves nothing.

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
        if workspace_id is None:
            raise ValueError(_NO_WORKSPACE)
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


# Why a dry run that would create the workspace resolves nothing against it:
# the deploy itself would find the new workspace empty.
_NO_WORKSPACE = "the target workspace does not exist yet"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'s' if count != 1 else ''}"


def _load_parameter_file(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8-sig") as f:
        document = yaml.safe_load(f) or {}
    if not isinstance(document, dict) or not all(
        isinstance(document.get(key, []), list) for key in ("find_replace", "extend")
    ):
        raise ValueError(f"{path}: expected a mapping whose find_replace and extend are lists")
    return document


@dataclass(frozen=True)
class Rule:
    """A find_replace entry fabric-cicd gets, and where it came from.

    `origin` is "parameter.yml", the repo-relative path of a template it
    extends, "fabdep.yml" (a manual_parameters entry) or "detected"; `kind` is
    what detection took the find_value to be.
    """

    entry: dict[str, Any]
    origin: str
    kind: str = ""


class ParameterFile:
    """The parameter file fabric-cicd reads for one deploy.

    It is the repo's own parameter.yml with the detected rules added, written
    to a temporary directory: the repo is never modified. The user's entries
    win. A detected rule is dropped when the user's file (or a template it
    extends) has an entry for its find_value that applies to every file in
    this environment; the user's find_replace entries come first, so where a
    narrower entry of theirs applies, it applies first. With nothing to add, `path` stays None
    and fabric-cicd reads the repo's parameter.yml, if any, itself.
    """

    def __init__(self, repo_dir: Path) -> None:
        self.repo_dir = repo_dir
        self.path: str | None = None
        self.summary: list[str] = []
        # Every find_replace entry fabric-cicd gets, in the order it applies them.
        self.rules: list[Rule] = []

    def prepare(
        self,
        detector: ParameterDetector,
        workspace_id: str,
        environment: str,
        manual_count: int,
        skipped_lakehouse_count: int,
    ) -> dict[str, Any] | None:
        """Work out the parameter document for `environment`, its rules and a
        human summary, writing nothing.

        Returns:
            The document to hand fabric-cicd, or None when it should read the
            repo's parameter.yml (if any) as it is.
        """
        summary = []
        if skipped_lakehouse_count:
            summary.append(
                f"Skipped {_plural(skipped_lakehouse_count, 'lakehouse parameter')} "
                "(--skip-lakehouse)"
            )
        rules: list[dict[str, Any]] = []
        if detector.should_skip_replacement(workspace_id):
            # Nothing detected needs rewriting, but fabdep.yml's manual_parameters still apply.
            rules = [
                rule
                for rule in detector.generate_parameter_yml(environment)["find_replace"]
                if "fabdep.yml" in detector.parameters[rule["find_value"]].source_files
            ]
            summary.append(
                "Source and target workspace are the same -- no detected replacements"
                + (f"; {_plural(len(rules), 'manual parameter')}" if rules else "")
            )
        elif rules := detector.generate_parameter_yml(environment)["find_replace"]:
            unchanged = len(detector.parameters) - len(rules)
            summary.append(
                f"Found {_plural(len(rules), 'dynamic parameter')}"
                + (f" + {unchanged} left unchanged" if unchanged else "")
                + (f" + {_plural(manual_count, 'manual parameter')}" if manual_count else "")
            )
        else:
            summary.append("No parameters detected for replacement")

        user_file = self.repo_dir / "parameter.yml"
        user: dict[str, Any] = _load_parameter_file(user_file) if user_file.is_file() else {}
        templates = [(user_file.parent / str(t)).resolve() for t in user.get("extend") or []]
        extended: list[tuple[Path, dict[str, Any]]] = [
            (t, _load_parameter_file(t)) for t in templates if t.is_file()
        ]
        covered = {
            entry.get("find_value")
            for document in [user, *(d for _, d in extended)]
            for entry in _entries(document)
            if _applies_everywhere(entry, environment)
        }
        added = [rule for rule in rules if rule["find_value"] not in covered]
        if user_file.is_file():
            overridden = len(rules) - len(added)
            summary.append(
                f"Using the repo's parameter.yml for environment '{environment}'"
                + (f"; it overrides {_plural(overridden, 'detected rule')}" if overridden else "")
            )
        self.summary = summary

        def detected(entry: dict[str, Any]) -> Rule:
            param = detector.parameters[entry["find_value"]]
            origin = "fabdep.yml" if "fabdep.yml" in param.source_files else "detected"
            return Rule(entry, origin, str(param.description))

        # fabric-cicd appends a template's entries to the file's own.
        self.rules = [
            *(Rule(e, "parameter.yml") for e in _entries(user)),
            *(detected(entry) for entry in added),
            *(Rule(e, self._relative(t)) for t, d in extended for e in _entries(d)),
        ]
        if not added:
            return None
        document: dict[str, Any] = {
            **user,
            "find_replace": [*(user.get("find_replace") or []), *added],
        }
        if templates:
            # fabric-cicd resolves templates relative to the parameter file.
            document["extend"] = [str(t) for t in templates]
        return document

    def write(
        self,
        detector: ParameterDetector,
        workspace_id: str,
        environment: str,
        manual_count: int,
        skipped_lakehouse_count: int,
    ) -> None:
        """Prepare the file (see `prepare`) and write it to a temporary directory."""
        document = self.prepare(
            detector, workspace_id, environment, manual_count, skipped_lakehouse_count
        )
        if document is None:
            return
        path = Path(tempfile.mkdtemp(prefix="fabdep-")) / "parameter.yml"
        path.write_text(
            yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        self.path = str(path)

    def cleanup(self) -> None:
        """Remove the temporary file, if `write()` made one."""
        if self.path:
            shutil.rmtree(Path(self.path).parent, ignore_errors=True)
            self.path = None

    def _relative(self, path: Path) -> str:
        return (
            str(path.relative_to(self.repo_dir))
            if path.is_relative_to(self.repo_dir)
            else str(path)
        )


def _applies_everywhere(entry: dict[str, Any], environment: str) -> bool:
    """Whether a user's find_replace entry rewrites its find_value in every file
    of a deploy to `environment`, making a detected rule for it redundant.

    That takes a value for the environment (its own key, else `_ALL_`, as
    fabric-cicd looks it up) and no item_type, item_name or file_path filter.
    """
    replace_value = entry.get("replace_value")
    return (
        isinstance(replace_value, dict)
        and entry.get("item_type") is None
        and entry.get("item_name") is None
        and not entry.get("file_path")
        and bool(for_environment(cast(dict[str, str], replace_value), environment))
    )


def _entries(document: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[object] = document.get("find_replace") or []
    return [cast(dict[str, Any], e) for e in entries if isinstance(e, dict)]
