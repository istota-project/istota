"""Client route destinations supported by search results."""

ROUTE_PATHS = frozenset(['/chat/', '/briefings/', '/feeds/', '/health/documents/', '/health/labs/panel/', '/health/labs/marker/', '/health/history/encounter/', '/health/history/diagnoses/', '/health/immunizations/detail/', '/location/', '/money/transactions/'])


def route(path: str, **params) -> dict:
    if path not in ROUTE_PATHS:
        raise ValueError("Unknown search route")
    return {"type": "route", "path": path, "params": {k: str(v) for k, v in params.items()}}


def file_link(path: str) -> dict:
    return {"type": "file", "path": path}
