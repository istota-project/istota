"""Model discovery: each provider's model list, fetched off the task path and cached.

Three parsers, one per source:

- ``parse_openrouter_models``: OpenRouter's rich ``GET /models`` (limits,
  prices, modalities, ``supported_parameters``).
- ``parse_openai_models``: the plain OpenAI ``/models`` shape, ids and nothing
  else, so its records get the conservative defaults.
- ``parse_anthropic_models``: Anthropic's ``GET /v1/models``, which states line,
  lifecycle, limits and capabilities but no prices. Its records are partial
  (only what the API stated) and are merged over the shipped file
  (``merge_anthropic``), so the shipped prices stay.

One cache per provider at ``{db_path.parent}/model-catalogue/<provider>.json``,
written atomically, holding the last successful fetch time, the response
``ETag``, the last error and each model's ``last_seen``. A model absent from
the provider's list is kept for ``LAST_SEEN_WINDOW_SECONDS`` after it was last
seen, measured against the last successful fetch, so a provider that is down
for a week loses nothing.

Who fetches: the scheduler's ``model-catalogue`` gate (``refresh_all``) and,
once at boot for providers with no cache, ``boot_refresh``. Everything else
(``NativeBrain``, the web process, CLIs) only reads caches, through
``install_from_cache_dir``, which swaps the catalogue's discovered layer when a
cache file changed.

**The model-listing key** is read here and nowhere else: ``_read_listing_key``
opens the file at each Anthropic fetch and wraps it in a ``ModelListingKey``,
and ``fetch_anthropic_models`` is the one place its value is revealed. That
request goes to the fixed ``ANTHROPIC_MODELS_URL`` through
``lib/fixed_get`` (no redirect, no proxy), whatever the provider's
``base_url`` says, so a base URL pointed at a gateway never receives the key.
An inference key never authenticates the Anthropic list.

Nothing here raises into a caller that runs on a schedule or a task path; a
failure is recorded on the cache as an HTTP status or an exception class name,
never a body, a URL with its query, or a key.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from urllib.parse import urlencode

from istota.lib import fixed_get
from istota.lib.atomic_write import write_text_atomic

from . import catalog
from .catalog import SHIPPED_PROVIDER, ModelInfo, model_info_from_dict
from .providers import (
    InferenceKey,
    ModelListingKey,
    ProviderSpec,
    is_provider_id,
    reveal_inference_key,
)

logger = logging.getLogger("istota.llm.discovery")

#: The cache directory's name under the database's directory.
CACHE_DIRNAME = "model-catalogue"
#: The single-file OpenRouter cache this replaced, read once as the
#: `openrouter` provider's initial cache and then left alone.
LEGACY_OPENROUTER_CACHE = "openrouter_models.json"

#: The only URL the model-listing key is ever sent to.
ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_PAGE_LIMIT = 1000
#: A bound on paging, far above any real list, so a server that keeps
#: answering `has_more` cannot hold the gate.
ANTHROPIC_MAX_PAGES = 20

#: How long a model missing from its provider's list stays resolvable.
LAST_SEEN_WINDOW_SECONDS = 7 * 24 * 3600
#: The scheduler gate's cadence; each provider's own TTL decides whether it fetches.
REFRESH_INTERVAL_SECONDS = 15 * 60
#: One budget for every provider together at a cold boot.
BOOT_BUDGET_SECONDS = 10.0
FETCH_TIMEOUT_SECONDS = 10.0

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
#: Levels supported only when the effort map names them (the clamp's rule).
_NAMED_ONLY_LEVELS = ("xhigh", "max")

# Conservative max-output floor when OpenRouter reports no completion cap.
_MAX_OUTPUT_FLOOR = 16384

#: `(url, headers, timeout) -> (status, body, headers)`. Tests inject one.
Transport = Callable[[str, Mapping[str, str], float], tuple[int, bytes, Mapping[str, str]]]


# --- OpenRouter --------------------------------------------------------------


def _to_float(value) -> float | None:  # noqa: ANN001
    """An OpenRouter price string/number as a finite non-negative float, else ``None``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    if f < 0:
        return None
    return f


def _parse_openrouter_one(entry: dict) -> ModelInfo | None:
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

    pricing = entry.get("pricing")
    pricing = pricing if isinstance(pricing, dict) else {}

    def _price(key: str) -> float:
        f = _to_float(pricing.get(key))
        return f * 1_000_000 if f is not None else 0.0

    arch = entry.get("architecture")
    modalities = arch.get("input_modalities") if isinstance(arch, dict) else None
    supports_vision = isinstance(modalities, list) and "image" in modalities

    params = entry.get("supported_parameters")
    params = params if isinstance(params, list) else []
    # True when the field is absent, so an endpoint that does not enumerate
    # parameters is not over-restricted; False only when a list lacks "tools".
    supports_tools = ("tools" in params) if params else True

    name = entry.get("name")
    return ModelInfo(
        id=model_id,
        name=name if isinstance(name, str) else "",
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        input_price_per_mtok=_price("prompt"),
        output_price_per_mtok=_price("completion"),
        cache_read_price_per_mtok=_price("input_cache_read"),
        cache_write_price_per_mtok=_price("input_cache_write"),
        supports_tools=supports_tools,
        supports_vision=supports_vision,
        supports_thinking="reasoning" in params,
        supports_structured_outputs="structured_outputs" in params,
        price_source="discovered",
    )


def parse_openrouter_models(payload: dict) -> dict[str, ModelInfo]:
    """An OpenRouter ``/models`` payload as ``{id: ModelInfo}``. Never raises."""
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if not isinstance(data, list):
        return {}
    out: dict[str, ModelInfo] = {}
    for entry in data:
        try:
            info = _parse_openrouter_one(entry)
        except Exception:  # noqa: BLE001 - one bad entry must not kill the rest
            logger.debug("openrouter model entry parse raised; skipping", exc_info=True)
            continue
        if info is not None:
            out[info.id] = info
    return out


async def fetch_openrouter_catalog(
    base_url: str,
    *,
    client=None,  # noqa: ANN001
    timeout: float = FETCH_TIMEOUT_SECONDS,
    api_key: str = "",
) -> dict[str, ModelInfo]:
    """GET ``{base_url}/models`` and parse it; raises on a network or HTTP error.

    For the setup server's model suggestions, which run before any config or
    cache exists. Everything else reads the caches.
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


# --- plain OpenAI-compatible ---------------------------------------------------


def parse_openai_models(payload: dict) -> dict[str, ModelInfo]:
    """The OpenAI ``/models`` shape: ids only, each on the conservative defaults."""
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if not isinstance(data, list):
        return {}
    out: dict[str, ModelInfo] = {}
    for entry in data:
        model_id = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        out[model_id] = replace(catalog.declared_model({"id": model_id}, ""), price_source="unknown")
    return out


# --- Anthropic -----------------------------------------------------------------


def _positive_int(value) -> int | None:  # noqa: ANN001
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _supported(block) -> bool | None:  # noqa: ANN001
    if isinstance(block, dict) and isinstance(block.get("supported"), bool):
        return block["supported"]
    return None


def _release_date(value) -> str | None:  # noqa: ANN001
    """The date part of an RFC 3339 `created_at`; an epoch value is unknown."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    day = value[:10]
    if day.startswith("1970-01-01"):
        return None
    try:
        time.strptime(day, "%Y-%m-%d")
    except ValueError:
        return None
    return day


def _parse_anthropic_one(entry: dict) -> tuple[str, dict] | None:
    if not isinstance(entry, dict):
        return None
    model_id = entry.get("id")
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    stated: dict = {}
    name = entry.get("display_name")
    if isinstance(name, str) and name.strip():
        stated["name"] = name.strip()
    # The family is the API's `line`; the id is never parsed for one.
    line = entry.get("line")
    if isinstance(line, str) and line.strip():
        stated["family"] = line.strip().lower()
    released = _release_date(entry.get("created_at"))
    if released:
        stated["released"] = released
    lifecycle = entry.get("lifecycle")
    if isinstance(lifecycle, str) and lifecycle:
        stated["deprecated"] = lifecycle != "active"
    window = _positive_int(entry.get("max_input_tokens"))
    if window:
        stated["context_window"] = window
    max_out = _positive_int(entry.get("max_tokens"))
    if max_out:
        stated["max_output_tokens"] = max_out
    caps = entry.get("capabilities")
    if isinstance(caps, dict):
        image = _supported(caps.get("image_input"))
        if image is not None:
            stated["input"] = ["text", "image"] if image else ["text"]
        thinking = _supported(caps.get("thinking"))
        if thinking is not None:
            stated["reasoning"] = thinking
        effort = caps.get("effort")
        if isinstance(effort, dict):
            whole = _supported(effort)
            levels: dict[str, bool] = {}
            for level in EFFORT_LEVELS:
                one = _supported(effort.get(level))
                if one is None and whole is False:
                    one = False
                if one is not None:
                    levels[level] = one
            if levels:
                stated["effort_supported"] = levels
    return model_id, stated


def parse_anthropic_models(payload: dict) -> dict[str, dict]:
    """One page of ``GET /v1/models`` as ``{id: the fields the API stated}``.

    A field the API returns as null, or does not return, is absent, so the
    shipped value stays in its place when the two are merged. Never raises.
    """
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if not isinstance(data, list):
        return {}
    out: dict[str, dict] = {}
    for entry in data:
        try:
            parsed = _parse_anthropic_one(entry)
        except Exception:  # noqa: BLE001 - one bad entry must not kill the rest
            logger.debug("anthropic model entry parse raised; skipping", exc_info=True)
            continue
        if parsed is not None:
            out[parsed[0]] = parsed[1]
    return out


#: The stated fields that replace the shipped record's outright.
_STATED_FIELDS = (
    "name", "family", "released", "deprecated", "context_window", "max_output_tokens",
    "input", "reasoning",
)


def merge_anthropic(
    stated: Mapping[str, Mapping], shipped: Iterable[ModelInfo],
) -> tuple[ModelInfo, ...]:
    """The API's records over the shipped ones, field by field.

    The API wins for what it stated; the shipped file keeps prices, price tiers
    and the effort map's wire values. An unsupported effort level is marked
    ``None``; a supported one keeps its shipped wire value, and ``xhigh`` or
    ``max`` supported with none is named as itself, since the clamp offers
    those two only when the map names them. A model only the API lists starts
    from the conservative default with ``price_source = "unknown"``.
    """
    shipped_by_id = {m.id: m for m in shipped}
    out = []
    for model_id, fields in stated.items():
        base = shipped_by_id.get(model_id)
        if base is None:
            base = replace(
                catalog.declared_model({"id": model_id}, SHIPPED_PROVIDER), price_source="unknown",
            )
        changes = {k: fields[k] for k in _STATED_FIELDS if k in fields}
        effort_map = dict(base.effort_map)
        for level, supported in (fields.get("effort_supported") or {}).items():
            if level not in EFFORT_LEVELS:
                continue
            if not supported:
                effort_map[level] = None
            elif effort_map.get(level) is not None:
                continue
            elif level in _NAMED_ONLY_LEVELS:
                effort_map[level] = level
            else:
                effort_map.pop(level, None)
        try:
            out.append(replace(base, **changes, effort_map=effort_map, provider=SHIPPED_PROVIDER))
        except Exception:  # noqa: BLE001 - a malformed cached field skips one model
            logger.debug("anthropic merge skipped %s", model_id, exc_info=True)
    return tuple(out)


class ListingFailed(Exception):
    """The model list could not be read: an HTTP status, or none for a transport failure."""

    def __init__(self, reason: str, status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


def _anthropic_transport(url: str, headers: Mapping[str, str], timeout: float):  # noqa: ANN202
    return fixed_get.get(url, headers, timeout)


def _anthropic_page_url(after_id: str | None) -> str:
    query = {"limit": str(ANTHROPIC_PAGE_LIMIT)}
    if after_id:
        query["after_id"] = after_id
    url = f"{ANTHROPIC_MODELS_URL}?{urlencode(query)}"
    # Built from the constant above and nothing else; a cursor carrying a
    # separator would still be one encoded query value.
    if not url.startswith(f"{ANTHROPIC_MODELS_URL}?"):
        raise ValueError("the model list URL is fixed")
    return url


def fetch_anthropic_models(
    key: ModelListingKey,
    *,
    transport: Transport | None = None,
    timeout: float = FETCH_TIMEOUT_SECONDS,
) -> dict[str, dict]:
    """Every page of ``GET https://api.anthropic.com/v1/models``, parsed.

    Sends the model-listing key, ``anthropic-version`` and ``limit``/``after_id``
    and nothing else, to the fixed URL, with no redirect followed. Raises
    ``ListingFailed`` with the status on a non-200 answer, and with the
    exception class on a transport failure; neither carries a body or the key.
    """
    if not isinstance(key, ModelListingKey):
        raise TypeError(f"expected a ModelListingKey, got {type(key).__name__}")
    send = transport or _anthropic_transport
    headers = {
        "x-api-key": key.reveal_for_model_listing(),
        "anthropic-version": ANTHROPIC_VERSION,
        "Accept": "application/json",
    }
    out: dict[str, dict] = {}
    after: str | None = None
    for _ in range(ANTHROPIC_MAX_PAGES):
        url = _anthropic_page_url(after)
        try:
            status, body, _headers = send(url, headers, timeout)
        except Exception as exc:  # noqa: BLE001 - reported by class name only
            raise ListingFailed(fixed_get.describe_failure(exc)) from None
        if status != 200:
            raise ListingFailed(f"HTTP {status}", status)
        try:
            payload = json.loads(body.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            raise ListingFailed("a non-JSON response", status) from None
        out.update(parse_anthropic_models(payload))
        last = payload.get("last_id") if isinstance(payload, dict) else None
        if not (isinstance(payload, dict) and payload.get("has_more")) or not isinstance(last, str) or not last:
            break
        after = last
    return out


def _read_listing_key(path: Path | None) -> ModelListingKey | None:
    """The model-listing key from its file, or ``None`` when there is none.

    Read at each fetch and never kept, so no ``Config`` and no module state
    holds the value. A link is not followed.
    """
    if path is None:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            value = handle.read(64 * 1024).strip()
    except (OSError, UnicodeDecodeError):
        return None
    return ModelListingKey(value) if value else None


# --- other providers' fetch ------------------------------------------------------


def _http_transport(url: str, headers: Mapping[str, str], timeout: float):  # noqa: ANN202
    import httpx

    resp = httpx.get(url, headers=dict(headers), timeout=timeout, follow_redirects=False)
    return resp.status_code, resp.content, dict(resp.headers)


def models_url(spec: ProviderSpec) -> str:
    """Where an `openrouter` or `openai_compat` provider lists its models."""
    return f"{spec.openai_base_url.rstrip('/')}/models"


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in (headers or {}).items():
        if str(key).lower() == name.lower():
            return str(value)
    return None


# --- the cache ---------------------------------------------------------------------


def cache_dir_for(db_path: Path | str) -> Path:
    return Path(db_path).parent / CACHE_DIRNAME


def cache_file(cache_dir: Path, provider_id: str) -> Path:
    if not is_provider_id(provider_id):
        raise ValueError("not a provider id")
    return Path(cache_dir) / f"{provider_id}.json"


def _read_json(path: Path) -> dict | None:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001 - a corrupt cache is no cache
        logger.debug("model cache read failed: %s", path, exc_info=True)
        return None
    return raw if isinstance(raw, dict) else None


def _legacy_openrouter_doc(cache_dir: Path) -> dict | None:
    """`openrouter_models.json` beside the cache directory, as a cache document."""
    raw = _read_json(Path(cache_dir).parent / LEGACY_OPENROUTER_CACHE)
    if raw is None:
        return None
    fetched_at = raw.get("fetched_at")
    models = raw.get("models")
    if not isinstance(fetched_at, (int, float)) or not isinstance(models, dict):
        return None
    return {
        "provider": "openrouter",
        "kind": "openrouter",
        "fetched_at": float(fetched_at),
        "models": {
            mid: {**entry, "last_seen": float(fetched_at)}
            for mid, entry in models.items() if isinstance(mid, str) and isinstance(entry, dict)
        },
    }


def read_cache(cache_dir: Path, provider_id: str) -> dict | None:
    """A provider's cache document, or ``None``.

    ``{provider, kind, fetched_at, checked_at, etag, last_error, last_status,
    models: {id: {..., last_seen}}}``. The `openrouter` provider falls back to
    the old single-file cache until its own exists.
    """
    doc = _read_json(cache_file(cache_dir, provider_id))
    if doc is None and provider_id == "openrouter":
        doc = _legacy_openrouter_doc(cache_dir)
    if doc is None or not isinstance(doc.get("models"), dict):
        return None
    return doc


def _write_cache(cache_dir: Path, provider_id: str, doc: dict) -> None:
    path = cache_file(cache_dir, provider_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, json.dumps(doc), mode=0o600, fsync=True)
    except Exception:  # noqa: BLE001 - a cache write is best-effort
        logger.warning("model cache write failed for provider %s", provider_id, exc_info=True)


def _current(doc: dict) -> dict[str, dict]:
    """The cached models still inside the last-seen window."""
    fetched_at = doc.get("fetched_at")
    anchor = float(fetched_at) if isinstance(fetched_at, (int, float)) else 0.0
    out = {}
    for mid, entry in (doc.get("models") or {}).items():
        if not isinstance(mid, str) or not isinstance(entry, dict):
            continue
        seen = entry.get("last_seen")
        if isinstance(seen, (int, float)) and anchor - float(seen) > LAST_SEEN_WINDOW_SECONDS:
            continue
        out[mid] = entry
    return out


def _record_json(info: ModelInfo) -> dict:
    return asdict(info)


def _records_of(doc: dict, provider_id: str) -> tuple[ModelInfo, ...]:
    entries = _current(doc)
    if doc.get("kind") == "anthropic":
        if provider_id != SHIPPED_PROVIDER:
            return ()
        stated = {mid: {k: v for k, v in e.items() if k != "last_seen"} for mid, e in entries.items()}
        return merge_anthropic(stated, catalog.shipped_anthropic_models())
    out = []
    for mid, entry in entries.items():
        try:
            out.append(model_info_from_dict({**entry, "id": mid, "provider": provider_id}))
        except Exception:  # noqa: BLE001 - skip a corrupt entry
            logger.debug("cached model entry parse raised; skipping %r", mid, exc_info=True)
    return tuple(out)


def _cache_provider_ids(cache_dir: Path) -> list[str]:
    ids = set()
    try:
        for path in Path(cache_dir).glob("*.json"):
            if is_provider_id(path.stem):
                ids.add(path.stem)
    except OSError:
        pass
    if (Path(cache_dir).parent / LEGACY_OPENROUTER_CACHE).is_file():
        ids.add("openrouter")
    return sorted(ids)


def load_discovered(cache_dir: Path) -> dict[str, tuple[ModelInfo, ...]]:
    """Every cached provider's current models, the Anthropic ones merged over the shipped file."""
    layers: dict[str, tuple[ModelInfo, ...]] = {}
    for provider_id in _cache_provider_ids(cache_dir):
        doc = read_cache(cache_dir, provider_id)
        if doc is None:
            continue
        records = _records_of(doc, provider_id)
        if records:
            layers[provider_id] = records
    return layers


# --- refresh ------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoveryResult:
    provider: str
    outcome: str  # "refreshed" | "not_modified" | "failed" | "skipped"
    detail: str = ""
    count: int = 0


def is_due(spec: ProviderSpec, cache_dir: Path, now: float) -> bool:
    """Whether a provider's last successful fetch is older than its TTL."""
    doc = read_cache(cache_dir, spec.id)
    if doc is None:
        return True
    fetched_at = doc.get("fetched_at")
    if not isinstance(fetched_at, (int, float)):
        return True
    ttl = float(spec.discover_ttl_hours or 0.0) * 3600
    return ttl <= 0 or now - float(fetched_at) >= ttl


def _record_failure(cache_dir: Path, spec: ProviderSpec, doc: dict, now: float,
                    reason: str, status: int | None) -> DiscoveryResult:
    doc["checked_at"] = now
    doc["last_error"] = reason
    doc["last_status"] = status
    _write_cache(cache_dir, spec.id, doc)
    logger.warning("model discovery for provider %s failed: %s", spec.id, reason)
    return DiscoveryResult(spec.id, "failed", reason)


def refresh_provider(
    spec: ProviderSpec,
    cache_dir: Path,
    now: float,
    *,
    listing_key_path: Path | None = None,
    transport: Transport | None = None,
    timeout: float = FETCH_TIMEOUT_SECONDS,
) -> DiscoveryResult:
    """Fetch one provider's list into its cache. Never raises.

    A failure keeps the cached models and records the status (a 401 or 403
    included) or the exception class. An empty 200 is a failure: replacing a
    list with none would unpin every room at once.
    """
    try:
        return _refresh_provider(
            spec, Path(cache_dir), now, listing_key_path=listing_key_path,
            transport=transport, timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - discovery never fails a caller
        logger.warning("model discovery for provider %s raised %s", spec.id, type(exc).__name__)
        return DiscoveryResult(spec.id, "failed", type(exc).__name__)


def _refresh_provider(spec, cache_dir, now, *, listing_key_path, transport, timeout):  # noqa: ANN001, ANN202
    doc = read_cache(cache_dir, spec.id) or {}
    doc = {**doc, "provider": spec.id, "kind": spec.kind, "models": dict(doc.get("models") or {})}
    etag = None

    if spec.kind == "anthropic":
        if spec.id != SHIPPED_PROVIDER:
            return DiscoveryResult(spec.id, "skipped", "only the built-in anthropic provider lists models")
        key = _read_listing_key(listing_key_path)
        if key is None:
            return DiscoveryResult(spec.id, "skipped", "no model-listing key file")
        try:
            stated = fetch_anthropic_models(key, transport=transport, timeout=timeout)
        except ListingFailed as exc:
            return _record_failure(cache_dir, spec, doc, now, exc.reason, exc.status)
        if not stated:
            return _record_failure(cache_dir, spec, doc, now, "an empty model list", 200)
        fresh = {mid: {**fields, "last_seen": now} for mid, fields in stated.items()}
    else:
        headers = {"Accept": "application/json", **dict(spec.extra_headers or {})}
        if isinstance(spec.inference_key, InferenceKey) and spec.inference_key:
            headers["Authorization"] = f"Bearer {reveal_inference_key(spec.inference_key)}"
        if isinstance(doc.get("etag"), str) and doc.get("models"):
            headers["If-None-Match"] = doc["etag"]
        send = transport or _http_transport
        try:
            status, body, response_headers = send(models_url(spec), headers, timeout)
        except Exception as exc:  # noqa: BLE001 - reported by class name only
            return _record_failure(cache_dir, spec, doc, now, fixed_get.describe_failure(exc), None)
        if status == 304 and doc.get("models"):
            doc.update(fetched_at=now, checked_at=now, last_error=None, last_status=304)
            _write_cache(cache_dir, spec.id, doc)
            return DiscoveryResult(spec.id, "not_modified", count=len(doc["models"]))
        if status != 200:
            return _record_failure(cache_dir, spec, doc, now, f"HTTP {status}", status)
        try:
            payload = json.loads(body.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            return _record_failure(cache_dir, spec, doc, now, "a non-JSON response", status)
        parse = parse_openrouter_models if spec.kind == "openrouter" else parse_openai_models
        models = parse(payload)
        if not models:
            return _record_failure(cache_dir, spec, doc, now, "an empty model list", status)
        fresh = {mid: {**_record_json(info), "last_seen": now} for mid, info in models.items()}
        etag = _header(response_headers, "ETag")

    kept = {
        mid: entry for mid, entry in doc["models"].items()
        if mid not in fresh and isinstance(entry, dict)
        and isinstance(entry.get("last_seen"), (int, float))
        and now - float(entry["last_seen"]) <= LAST_SEEN_WINDOW_SECONDS
    }
    doc.update(
        models={**kept, **fresh}, fetched_at=now, checked_at=now, etag=etag,
        last_error=None, last_status=200,
    )
    _write_cache(cache_dir, spec.id, doc)
    return DiscoveryResult(spec.id, "refreshed", count=len(fresh))


def _discovering(config) -> list[ProviderSpec]:  # noqa: ANN001
    from .providers import build_providers

    return [spec for spec in build_providers(config).values() if spec.discover]


def _listing_key_path(config) -> Path | None:  # noqa: ANN001
    try:
        from istota.config import anthropic_listing_key_path

        return anthropic_listing_key_path(config)
    except Exception:  # noqa: BLE001
        return None


def config_cache_dir(config) -> Path | None:  # noqa: ANN001
    db_path = getattr(config, "db_path", None)
    return cache_dir_for(db_path) if db_path else None


def refresh_all(config, now: float | None = None, *, transport: Transport | None = None) -> list[DiscoveryResult]:  # noqa: ANN001
    """The scheduler gate's body: every due `discover = true` provider, then the snapshot."""
    cache_dir = config_cache_dir(config)
    if cache_dir is None:
        return []
    now = time.time() if now is None else now
    results = []
    try:
        for spec in _discovering(config):
            if not is_due(spec, cache_dir, now):
                continue
            results.append(refresh_provider(
                spec, cache_dir, now, listing_key_path=_listing_key_path(config), transport=transport,
            ))
    except Exception:  # noqa: BLE001 - the gate never raises
        logger.warning("model discovery pass failed", exc_info=True)
    install_from_cache_dir(cache_dir, force=True)
    return results


def boot_refresh(
    config,  # noqa: ANN001
    *,
    budget: float = BOOT_BUDGET_SECONDS,
    transport: Transport | None = None,
) -> list[DiscoveryResult]:
    """At daemon start: fetch each discovering provider with no cache, together,
    within one budget, then install whatever caches exist.

    A provider not finished inside the budget keeps running on its daemon
    thread and writes its cache when it ends; the gate installs it later. Until
    then it has its operator-declared models only, and doctor says so.
    """
    cache_dir = config_cache_dir(config)
    if cache_dir is None:
        return []
    now = time.time()
    results: list[DiscoveryResult] = []
    lock = threading.Lock()
    threads = []
    try:
        cold = [spec for spec in _discovering(config) if read_cache(cache_dir, spec.id) is None]
    except Exception:  # noqa: BLE001
        cold = []
    listing = _listing_key_path(config)

    def _one(spec: ProviderSpec) -> None:
        result = refresh_provider(spec, cache_dir, now, listing_key_path=listing, transport=transport)
        with lock:
            results.append(result)

    for spec in cold:
        thread = threading.Thread(target=_one, args=(spec,), daemon=True, name=f"model-discovery-{spec.id}")
        thread.start()
        threads.append(thread)
    deadline = time.monotonic() + max(0.0, budget)
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    unfinished = [t.name for t in threads if t.is_alive()]
    if unfinished:
        logger.warning("model discovery at boot did not finish within %.0fs: %s", budget, ", ".join(unfinished))
    install_from_cache_dir(cache_dir, force=True)
    with lock:
        return list(results)


# --- installing into the catalogue --------------------------------------------------

_INSTALL_LOCK = threading.Lock()
_INSTALLED: dict[str, tuple] = {}


def reset_install_state() -> None:
    """Test hook: forget which cache files were installed."""
    with _INSTALL_LOCK:
        _INSTALLED.clear()


def _signature(cache_dir: Path) -> tuple:
    entries = []
    paths = list(Path(cache_dir).glob("*.json")) if Path(cache_dir).is_dir() else []
    paths.append(Path(cache_dir).parent / LEGACY_OPENROUTER_CACHE)
    for path in sorted(paths):
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append((path.name, stat.st_mtime_ns, stat.st_size))
    return tuple(entries)


def install_from_cache_dir(cache_dir: Path | None, *, force: bool = False) -> bool:
    """Swap the catalogue's discovered layer for the caches, when one changed.

    One stat per file when nothing did. Reads are of whole files written by
    rename, so a reader sees the old file or the new one. Never raises; returns
    whether a new layer was installed.
    """
    if cache_dir is None:
        return False
    try:
        key = str(Path(cache_dir))
        signature = _signature(Path(cache_dir))
        with _INSTALL_LOCK:
            if not force and _INSTALLED.get(key) == signature:
                return False
            catalog.set_discovered(load_discovered(Path(cache_dir)))
            _INSTALLED[key] = signature
        return True
    except Exception:  # noqa: BLE001 - metadata must never fail a task
        logger.debug("model cache install failed", exc_info=True)
        return False
