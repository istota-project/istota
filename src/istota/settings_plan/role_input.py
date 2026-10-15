"""Convert the role's application variables into a plan and its sidecar."""

from __future__ import annotations

from copy import deepcopy

from istota.settings_plan.schema import SchemaError, parse_plan, parse_sidecar


ROLE_VARIABLES = ("istota_plan", "istota_plan_secrets")


def convert_role_input(document: dict) -> tuple[dict, dict]:
    """Return a canonical plan and private sidecar, without state reads or writes.

    The envelope contains application variables only. Deployment variables
    stay with their role writers and are not submitted to apply.
    """
    if not isinstance(document, dict):
        raise SchemaError([], "role input must be a mapping of istota_plan and istota_plan_secrets")
    unknown = sorted(set(document) - set(ROLE_VARIABLES), key=str)
    if unknown:
        raise SchemaError([str(unknown[0])], "not a settings variable; the role takes istota_plan and istota_plan_secrets")
    for name, value in document.items():
        if not isinstance(value, dict):
            raise SchemaError([name], "role variable must be a mapping")
    if document.get("istota_plan_secrets") and not document.get("istota_plan"):
        raise SchemaError(["istota_plan_secrets"], "istota_plan_secrets requires istota_plan")
    plan = deepcopy(document.get("istota_plan", {}))
    sidecar = deepcopy(document.get("istota_plan_secrets", {}))
    # A role owns the config it submits, independently of per-user modes.
    if "config" in plan:
        meta = plan.setdefault("meta", {})
        if not isinstance(meta, dict):
            raise SchemaError(["meta"], "must be a mapping")
        meta.update(managed=True, source="ansible")
    parse_plan(plan)
    parse_sidecar(sidecar)
    return plan, sidecar
