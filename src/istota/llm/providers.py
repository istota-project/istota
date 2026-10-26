"""Providers: one endpoint and the key for it.

A request to a provider carries that provider's key, whichever brain sends it.
A brain names the provider it uses (`[brain.native] provider`); the provider's
`kind` decides what the endpoint accepts (`KIND_DEFAULTS`), which replaced the
base-URL substring checks spread over `make_provider`, `openai_compat` and
`NativeBrain`.

Keys are held as `InferenceKey` from `build_providers` on. `ModelListingKey` is
the type for the key used only to list Anthropic's models; nothing reads one
yet (the discovery fetch that does is a later stage), and neither type converts
to the other.

`config.py` imports this module, so at module scope it imports only the
leaf `llm.types`.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .types import PROMPT_CACHE_TTLS, REASONING_REPLAY_MODES, STRICT_TOOL_SCHEMA_MODES  # noqa: F401 - re-exported for the provider table's checks

PROVIDER_KINDS = ("anthropic", "openrouter", "openai_compat")

#: The two services istota knows by name, present without a table:
#: id -> (kind, base_url). A table with one of these ids sets fields on it.
BUILTIN_PROVIDERS: dict[str, tuple[str, str]] = {
    "anthropic": ("anthropic", "https://api.anthropic.com"),
    "openrouter": ("openrouter", "https://openrouter.ai/api/v1"),
}

#: Lowercase letters, digits and `_`, starting with a letter. `/` and `:` would
#: break `provider/id` refs and the `:effort` suffix, and `-` would let `my-p`
#: and `my_p` share one variable name.
_PROVIDER_ID = re.compile(r"^[a-z][a-z0-9_]*$")

#: The credential service a user's own provider key is stored under, keyed by
#: provider id (`istota secret ensure -s provider_keys -k openrouter`).
USER_KEY_SERVICE = "provider_keys"

# OpenRouter app attribution (https://openrouter.ai/docs/app-attribution).
OPENROUTER_APP_URL = "https://istota.cynium.com"
OPENROUTER_APP_TITLE = "Istota"

#: Model families on OpenAI's own endpoint that take `max_completion_tokens`
#: instead of `max_tokens` (NB-12). Matched as a lowercase prefix of the id.
OPENAI_REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5")
_OPENAI_HOST = "api.openai.com"


def implied_provider_id(base_url: str) -> str:
    """The provider a legacy `[brain.native] base_url` meant: `anthropic` and
    `openrouter` by their hosts, `native` (an operator's own) otherwise."""
    host = _host(base_url)
    if host == "api.anthropic.com":
        return "anthropic"
    if host == "openrouter.ai":
        return "openrouter"
    return "native"


def is_provider_id(value: object) -> bool:
    return isinstance(value, str) and bool(_PROVIDER_ID.match(value))


def provider_key_variable(provider_id: str) -> str:
    """`ISTOTA_PROVIDER_<ID>_API_KEY`: the variable a provider's key arrives in.

    Lowercased, it is the stack secret file for a built-in provider.
    """
    return f"ISTOTA_PROVIDER_{provider_id.upper()}_API_KEY"


def provider_headers_variable(provider_id: str) -> str:
    """`ISTOTA_PROVIDER_<ID>_EXTRA_HEADERS`: a JSON object replacing the table."""
    return f"ISTOTA_PROVIDER_{provider_id.upper()}_EXTRA_HEADERS"


# --- keys -------------------------------------------------------------------


class _Key:
    """A frozen wrapper whose value is never in `str` or `repr`."""

    __slots__ = ("_value",)

    def __init__(self, value: str = "") -> None:
        if not isinstance(value, str):
            raise TypeError(f"{type(self).__name__} wraps a str")
        object.__setattr__(self, "_value", value)

    def __setattr__(self, name, value):  # noqa: ANN001
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __repr__(self) -> str:
        return f"{type(self).__name__}({'<redacted>' if self._value else '<empty>'})"

    __str__ = __repr__

    def __bool__(self) -> bool:
        return bool(self._value)

    def __eq__(self, other: object) -> bool:
        return type(other) is type(self) and other._value == self._value  # type: ignore[attr-defined]

    def __hash__(self) -> int:
        return hash((type(self).__name__, self._value))

    def __reduce__(self):  # noqa: ANN204
        raise TypeError(f"{type(self).__name__} is not picklable")


class InferenceKey(_Key):
    """A key that authenticates inference against one provider."""

    __slots__ = ()

    def reveal(self) -> str:
        return self._value


class ModelListingKey(_Key):
    """The key used for Anthropic's `GET /v1/models` and nothing else.

    Not a subclass of `InferenceKey` and not convertible to one, so no code path
    that takes an inference key can be handed it.
    """

    __slots__ = ()

    def reveal_for_model_listing(self) -> str:
        return self._value


def reveal_inference_key(key: object) -> str:
    """The value of an `InferenceKey`, refusing anything else."""
    if not isinstance(key, InferenceKey):
        raise TypeError(f"expected an InferenceKey, got {type(key).__name__}")
    return key.reveal()


# --- compat -------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderCompat:
    """What an endpoint accepts, resolved from the kind plus explicit settings."""

    prompt_caching: bool = False
    prompt_cache_key: bool = False
    prompt_cache_ttl: str = "5m"
    strict_tool_schemas: str = "auto"
    reasoning_replay: str = "auto"
    # OpenRouter: `usage.include` in the body, so the reported cost comes back.
    cost_accounting: bool = False
    # OpenRouter: the HTTP-Referer / X-Title attribution headers.
    attribution_headers: bool = False
    # Model id prefixes that take `max_completion_tokens`; `("",)` means every model.
    max_completion_tokens_models: tuple[str, ...] = ()
    # Whether `reasoning_effort` goes on the wire at all.
    reasoning_effort: bool = True

    def uses_max_completion_tokens(self, model: str) -> bool:
        return (model or "").lower().startswith(self.max_completion_tokens_models) if self.max_completion_tokens_models else False


#: Per kind, the compat a provider gets before its own settings. The one host
#: rule left (OpenAI is reached as `openai_compat`) is in `kind_defaults`.
KIND_DEFAULTS: dict[str, ProviderCompat] = {
    "anthropic": ProviderCompat(prompt_caching=True),
    "openrouter": ProviderCompat(
        prompt_cache_key=True, cost_accounting=True, attribution_headers=True,
    ),
    "openai_compat": ProviderCompat(),
}


def _host(base_url: str) -> str:
    try:
        return (urlsplit(base_url or "").hostname or "").lower()
    except ValueError:
        return ""


def kind_defaults(kind: str, base_url: str) -> ProviderCompat:
    """`KIND_DEFAULTS[kind]`, plus OpenAI's own endpoint reached as `openai_compat`."""
    compat = KIND_DEFAULTS.get(kind, KIND_DEFAULTS["openai_compat"])
    if kind == "openai_compat" and _host(base_url) == _OPENAI_HOST:
        compat = dataclasses.replace(
            compat,
            prompt_cache_key=True,
            max_completion_tokens_models=OPENAI_REASONING_PREFIXES,
        )
    return compat


#: The provider table's wire settings, in `ProviderCompat`'s names. Each is
#: `None` in config when unset, which takes the kind default.
WIRE_SETTINGS = (
    "prompt_caching", "prompt_cache_key", "prompt_cache_ttl",
    "strict_tool_schemas", "reasoning_replay",
)

#: `[providers.<id>.compat]` keys beyond the five wire settings.
COMPAT_TABLE_KEYS = (
    "cost_accounting", "attribution_headers", "max_completion_tokens", "reasoning_effort",
)


def resolve_compat(kind: str, base_url: str, settings: Mapping[str, object]) -> ProviderCompat:
    """The kind default with every explicit (non-`None`) setting over it, field by field."""
    compat = kind_defaults(kind, base_url)
    changes: dict[str, object] = {}
    for name in WIRE_SETTINGS + ("cost_accounting", "attribution_headers", "reasoning_effort"):
        value = settings.get(name)
        if value is not None:
            changes[name] = value
    mct = settings.get("max_completion_tokens")
    if mct is not None:
        changes["max_completion_tokens_models"] = ("",) if mct else ()
    return dataclasses.replace(compat, **changes) if changes else compat


# --- specs -------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderSpec:
    """One configured provider, resolved: the record `make_provider` takes."""

    id: str
    kind: str
    base_url: str
    inference_key: InferenceKey = field(default_factory=InferenceKey, repr=False)
    extra_headers: Mapping[str, str] = field(default_factory=dict, repr=False, hash=False)
    compat: ProviderCompat = field(default_factory=ProviderCompat)
    discover: bool = True
    discover_ttl_hours: float = 24.0
    models: tuple[Mapping, ...] = field(default=(), hash=False)

    @property
    def openai_base_url(self) -> str:
        """The base URL native's OpenAI-compatible client uses.

        An `anthropic` provider's `base_url` is the API root, so `/v1` is added;
        the other kinds' base URLs carry their version path already.
        """
        base = (self.base_url or "").rstrip("/")
        if self.kind == "anthropic":
            return f"{base}/v1"
        return base

    @property
    def host(self) -> str:
        return _host(self.base_url)


def with_user_key(spec: ProviderSpec, key: object) -> ProviderSpec:
    """The spec a user's task uses: their own key for this provider, when they have one."""
    if isinstance(key, InferenceKey):
        return dataclasses.replace(spec, inference_key=key) if key else spec
    if isinstance(key, str) and key:
        return dataclasses.replace(spec, inference_key=InferenceKey(key))
    return spec


def spec_from_provider_config(provider_id: str, provider) -> ProviderSpec:  # noqa: ANN001
    """A `ProviderSpec` from a `config.ProviderConfig`."""
    settings = {name: getattr(provider, name, None) for name in WIRE_SETTINGS}
    settings.update({k: v for k, v in (getattr(provider, "compat", None) or {}).items()})
    return ProviderSpec(
        id=provider_id,
        kind=provider.kind,
        base_url=provider.base_url,
        inference_key=InferenceKey((provider.api_key or "").strip()),
        extra_headers=dict(provider.extra_headers or {}),
        compat=resolve_compat(provider.kind, provider.base_url, settings),
        discover=bool(provider.discover),
        discover_ttl_hours=float(provider.discover_ttl_hours),
        models=tuple(dict(m) for m in (provider.models or ())),
    )


def build_providers(config) -> dict[str, ProviderSpec]:  # noqa: ANN001
    """Every configured provider (the built-ins always among them), as specs."""
    providers = getattr(config, "providers", None) or {}
    return {pid: spec_from_provider_config(pid, p) for pid, p in providers.items()}


def native_provider_reason(config) -> str | None:  # noqa: ANN001
    """Why the native brain cannot run, or `None`. Needs no CLI mode."""
    native = config.brain.native
    provider_id = (native.provider or "").strip()
    if not provider_id:
        return (
            "[brain.native] provider is not set: name the provider native uses "
            '(for example provider = "anthropic" or "openrouter")'
        )
    if provider_id not in (getattr(config, "providers", None) or {}):
        return f"[brain.native] provider = {provider_id!r} names no configured provider"
    return None


def user_provider_key(config, user_id: str, provider_id: str) -> str | None:  # noqa: ANN001
    """A user's own key for one provider, from the credential vault, or `None`.

    A failed lookup is `None`, so a missing secret never blocks a task.
    """
    if not user_id or not provider_id:
        return None
    try:
        from istota.credentials import store as secrets_store  # noqa: PLC0415

        key = secrets_store.get_secret(config.db_path, user_id, USER_KEY_SERVICE, provider_id)
    except Exception:  # noqa: BLE001
        return None
    return key or None
