"""Live model-metadata enrichment from OpenRouter's public model list.

The real native deployment shape speaks to OpenRouter, whose ``GET /models``
endpoint returns rich per-model metadata (context length, capabilities, USD
pricing) that we would otherwise hand-maintain. This module is a pure parser
plus a thin async fetch + disk-cache helper; ``NativeBrain`` orchestrates when
to fetch and installs the result into ``catalog.set_fetched_catalog``.

Nothing here knows about NativeBrain internals — it takes a ``base_url`` and
returns ``dict[str, ModelInfo]``. Malformed input never raises past the entry
that caused it: a bad individual model entry is skipped, and a whole-payload
problem yields ``{}`` (treated by the caller as a failed fetch → stale-cache /
default fallback). The disk cache stores the *parsed* ``ModelInfo`` fields, not
the raw OpenRouter payload, so an upstream schema drift can't poison a later
read.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

from .catalog import ModelInfo

logger = logging.getLogger("istota.llm.openrouter_catalog")

_CACHE_FILENAME = "openrouter_models.json"
# Conservative max-output floor when OpenRouter reports no completion cap.
_MAX_OUTPUT_FLOOR = 16384


def _to_float(value) -> float | None:
    """Parse an OpenRouter price string/number to a float, or ``None`` if not a
    finite, non-negative number. OpenRouter reports prices as strings."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf
        return None
    if f < 0:
        return None
    return f


def _parse_one(entry: dict) -> ModelInfo | None:
    """Map one OpenRouter model entry → ``ModelInfo``. ``None`` if unusable
    (missing/non-positive context length, or not a dict)."""
    if not isinstance(entry, dict):
        return None
    model_id = entry.get("id")
    if not isinstance(model_id, str) or not model_id:
        return None

    ctx = entry.get("context_length")
    try:
        context_window = int(ctx)
    except (TypeError, ValueError):
        return None
    if context_window <= 0:
        return None

    # max output: top_provider.max_completion_tokens, else a top-level
    # max_output_tokens, else a conservative floor bounded by the window.
    max_out = None
    top = entry.get("top_provider")
    if isinstance(top, dict):
        max_out = top.get("max_completion_tokens")
    if max_out is None:
        max_out = entry.get("max_output_tokens")
    try:
        max_output_tokens = int(max_out) if max_out is not None else 0
    except (TypeError, ValueError):
        max_output_tokens = 0
    if max_output_tokens <= 0:
        max_output_tokens = min(context_window, _MAX_OUTPUT_FLOOR)

    # pricing: USD per token (strings) → USD per mtok. Missing/blank → 0.0.
    pricing = entry.get("pricing")
    pricing = pricing if isinstance(pricing, dict) else {}

    def _price(key: str) -> float:
        f = _to_float(pricing.get(key))
        return f * 1_000_000 if f is not None else 0.0

    input_price = _price("prompt")
    output_price = _price("completion")
    cache_read_price = _price("input_cache_read")
    cache_write_price = _price("input_cache_write")

    # capabilities
    arch = entry.get("architecture")
    modalities = arch.get("input_modalities") if isinstance(arch, dict) else None
    supports_vision = isinstance(modalities, list) and "image" in modalities

    params = entry.get("supported_parameters")
    params = params if isinstance(params, list) else []
    supports_thinking = "reasoning" in params
    # Default True when the field is absent (avoid over-restricting an endpoint
    # that simply doesn't enumerate parameters); only False when a list is
    # present and lacks "tools".
    supports_tools = ("tools" in params) if params else True

    return ModelInfo(
        id=model_id,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        input_price_per_mtok=input_price,
        output_price_per_mtok=output_price,
        cache_read_price_per_mtok=cache_read_price,
        cache_write_price_per_mtok=cache_write_price,
        supports_tools=supports_tools,
        supports_vision=supports_vision,
        supports_thinking=supports_thinking,
    )


def parse_openrouter_models(payload: dict) -> dict[str, ModelInfo]:
    """Parse an OpenRouter ``/models`` payload into ``{id: ModelInfo}``.

    Pure. A missing/non-list ``data`` yields ``{}``; individual malformed
    entries are skipped (logged at DEBUG). Never raises.
    """
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if not isinstance(data, list):
        return {}
    out: dict[str, ModelInfo] = {}
    for entry in data:
        try:
            info = _parse_one(entry)
        except Exception:  # noqa: BLE001 — one bad entry must not kill the rest
            logger.debug("openrouter model entry parse raised; skipping", exc_info=True)
            continue
        if info is not None:
            out[info.id] = info
        else:
            logger.debug("openrouter model entry skipped (unusable): %r", entry)
    return out


async def fetch_openrouter_catalog(
    base_url: str,
    *,
    client=None,
    timeout: float = 10.0,
    api_key: str = "",
) -> dict[str, ModelInfo]:
    """GET ``{base_url}/models`` and parse it. Raises on network/HTTP error.

    ``base_url`` already ends in ``/api/v1`` (the openai_compat base). ``api_key``
    is sent as a Bearer header if provided (harmless; the endpoint is public). An
    injected ``client`` (tests) is used as-is and not closed; otherwise a
    short-lived ``httpx.AsyncClient`` is created and closed here.
    """
    import httpx

    url = f"{base_url.rstrip('/')}/models"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=timeout)
    try:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        payload = resp.json()
    finally:
        if own_client:
            await client.aclose()
    return parse_openrouter_models(payload)


# --- disk cache -----------------------------------------------------------


def cache_path(data_dir: Path) -> Path:
    return Path(data_dir) / _CACHE_FILENAME


def _entries_to_json(entries: dict[str, ModelInfo]) -> dict:
    return {mid: asdict(info) for mid, info in entries.items()}


def _entries_from_json(models: dict) -> dict[str, ModelInfo]:
    out: dict[str, ModelInfo] = {}
    if not isinstance(models, dict):
        return out
    valid_fields = set(ModelInfo.__dataclass_fields__)
    for mid, raw in models.items():
        if not isinstance(mid, str) or not isinstance(raw, dict):
            continue
        try:
            clean = {k: v for k, v in raw.items() if k in valid_fields}
            clean["id"] = mid
            out[mid] = ModelInfo(**clean)
        except Exception:  # noqa: BLE001 — skip a corrupt entry
            logger.debug("cached model entry parse raised; skipping %r", mid, exc_info=True)
    return out


def _read_raw(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001 — corrupt/permission → treat as no cache
        logger.debug("openrouter cache read failed: %s", path, exc_info=True)
        return None


def read_cache(path: Path, ttl_hours: float, *, now_ts: float) -> dict[str, ModelInfo] | None:
    """Return parsed entries if the cache exists and is within TTL, else ``None``.

    ``now_ts`` is the caller's ``time.time()`` (kept an explicit argument so the
    freshness check is deterministic in tests). A stale-but-present file returns
    ``None`` here — use ``read_cache_any_age`` for the stale-fallback path.
    """
    raw = _read_raw(path)
    if raw is None:
        return None
    fetched_at = raw.get("fetched_at")
    if not isinstance(fetched_at, (int, float)):
        return None
    if ttl_hours > 0 and (now_ts - fetched_at) > ttl_hours * 3600:
        return None
    entries = _entries_from_json(raw.get("models", {}))
    return entries or None


def read_cache_any_age(path: Path) -> dict[str, ModelInfo] | None:
    """Return parsed entries ignoring TTL (stale-fallback). ``None`` if absent /
    unreadable / empty."""
    raw = _read_raw(path)
    if raw is None:
        return None
    entries = _entries_from_json(raw.get("models", {}))
    return entries or None


def write_cache(path: Path, entries: dict[str, ModelInfo], now_ts: float) -> None:
    """Serialize ``{fetched_at, models}`` to ``path``. Best-effort — an I/O error
    is logged at DEBUG and swallowed (the in-memory table still serves)."""
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"fetched_at": now_ts, "models": _entries_to_json(entries)}
        path.write_text(json.dumps(payload))
    except Exception:  # noqa: BLE001 — cache write is best-effort
        logger.debug("openrouter cache write failed: %s", path, exc_info=True)
