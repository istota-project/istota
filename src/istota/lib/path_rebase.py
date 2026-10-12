"""Boundary-aware path replacement over decoded documents, without expansion."""


def rebase_path(value: str, prefixes: list[tuple[str, str]]) -> str | None:
    """``value`` under its new root, or None when no prefix covers it.

    A boundary match: the prefix itself or the prefix plus ``/``, never a
    sibling that merely starts with the same text.
    """
    for prefix, target in prefixes:
        if value == prefix:
            return target
        if prefix == "/" and value.startswith("/"):
            return target.rstrip("/") + value
        if value.startswith(prefix + "/"):
            return target.rstrip("/") + value[len(prefix):]
    return None


def rebase_json(
    value, prefixes: list[tuple[str, str]], *, select=None, path=(), matched=None,
):
    """``(rebased, count)`` over selected strings; default to every string.

    ``select(path, value)`` receives segment tuples, including array indices.
    ``matched`` optionally records the winning source prefixes, even for no-ops.
    Map keys are never rewritten. Prefixes are tried in the supplied order.
    """
    if isinstance(value, str):
        if select is not None and not select(path, value):
            return value, 0
        for prefix, target in prefixes:
            new = rebase_path(value, [(prefix, target)])
            if new is not None:
                if matched is not None:
                    matched.add(prefix)
                return new, 1
        return value, 0
    if isinstance(value, list):
        out, total = [], 0
        for index, item in enumerate(value):
            new, count = rebase_json(
                item, prefixes, select=select, path=(*path, index), matched=matched,
            )
            out.append(new)
            total += count
        return out, total
    if isinstance(value, dict):
        out, total = {}, 0
        for key, item in value.items():
            new, count = rebase_json(
                item, prefixes, select=select, path=(*path, key), matched=matched,
            )
            out[key] = new
            total += count
        return out, total
    return value, 0
