"""Render only the safe operation records, never an execution context."""

from copy import deepcopy
import json


def result_document(result) -> dict:
    operations = []
    for op in result.operations:
        record = {"section": op.section, "identity": op.identity, "action": op.action,
                  "status": op.status, "changes": deepcopy(op.changes)}
        if op.user is not None:
            record["user"] = op.user
        operations.append(record)
    return {
        "version": 1, "dry_run": result.dry_run, "changed": result.changed,
        "partial": result.partial, "restart_required": result.restart_required,
        "counts": {"planned": len(operations), **{
            status: sum(op["status"] == status for op in operations)
            for status in ("applied", "failed", "skipped")}},
        "operations": operations, "warnings": list(result.warnings), "errors": deepcopy(result.errors),
    }


def render_text(result) -> list[str]:
    lines = []
    for op in result_document(result)["operations"]:
        if op["section"] == "config":
            for change in op["changes"]:
                lines.extend(change.get("diff", []))
        else:
            owner = f" users.{op['user']}" if "user" in op else ""
            lines.append(f"{op['status']}: {op['action']} {op['section']}{owner} {op['identity']}")
            lines.extend("  " + json.dumps(change, sort_keys=True) for change in op["changes"])
    return lines
