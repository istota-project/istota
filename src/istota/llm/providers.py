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

#: `[brain.claude_code] auth`: how the `claude` CLI authenticates.
CLI_AUTH_MODES = ("subscription", "api_key")

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
    known = getattr(config, "providers", None) or {}
    if not getattr(config.brain, "provider_specs", None):
        # A config built without the loader: its `[brain.native]` legacy fields
        # imply the provider, read through the same transition table
        # (`brain.native_provider_spec` does the same).
        from istota.config import providers_for_native  # noqa: PLC0415

        specs, implied = providers_for_native(native)
        provider_id = provider_id or implied
        known = {**specs, **known}
    if not provider_id:
        return (
            "[brain.native] provider is not set: name the provider native uses "
            '(for example provider = "anthropic" or "openrouter")'
        )
    if provider_id not in known:
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


# --- CLI auth -----------------------------------------------------------------
#
# `[brain.claude_code] auth` decides how the `claude` CLI authenticates, for
# `claude_code` and `tmux_claude` alike: "subscription" carries the OAuth token
# and no API key or endpoint, "api_key" carries the `anthropic` provider's key
# and base URL and never the token. The CLI env is built from config by
# allowlist (`cli_auth_env`); nothing here reads a credential or an endpoint out
# of a process environment except the token, which `istota-secrets` exports
# from its secret file and which is config input like any other.

#: The OAuth token the CLI reads under "subscription".
OAUTH_TOKEN_VARIABLE = "CLAUDE_CODE_OAUTH_TOKEN"

#: The resolved mode, handed to a child the daemon spawns (not a secret).
CLI_AUTH_VARIABLE = "ISTOTA_CLI_AUTH"

#: The legacy variable the CLI's API key arrived in (`config.LEGACY_PROVIDER_SOURCES`).
LEGACY_CLI_API_KEY = "ANTHROPIC_API_KEY"

_MODEL_CREDENTIAL_NAMES = frozenset({
    OAUTH_TOKEN_VARIABLE,
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    # Not a secret, but it decides where the CLI sends one, so it comes from
    # config like one: nothing sets it under "subscription", and only
    # `cli_auth_env` under "api_key".
    "ANTHROPIC_BASE_URL",
    # Legacy, for the transition release.
    "ISTOTA_BRAIN_NATIVE_API_KEY",
    "ISTOTA_BRAIN_NATIVE_EXTRA_HEADERS",
})


def is_model_credential_name(name: object) -> bool:
    """Whether an environment name carries a model credential or the CLI's endpoint.

    The one predicate every strip, refusal and lookup block uses, so a provider
    added later (`ISTOTA_PROVIDER_<ID>_*`) is covered without a change anywhere.
    """
    if not isinstance(name, str):
        return False
    upper = name.upper()
    return upper in _MODEL_CREDENTIAL_NAMES or upper.startswith("ISTOTA_PROVIDER_")


def resolve_cli_auth(config, env: Mapping[str, str]) -> str | None:  # noqa: ANN001
    """The CLI mode: the written setting, else what ``env`` says, else `None`.

    Derived from the environment only: an inherited `ISTOTA_CLI_AUTH`, then the
    OAuth token, then the legacy `ANTHROPIC_API_KEY` (the transition row). A
    provider key alone derives nothing, since that would move an install onto
    API billing without a word (D15). No file or keychain is read: a stale
    `~/.claude/.credentials.json` must not decide the mode.
    """
    written = getattr(getattr(getattr(config, "brain", None), "claude_code", None), "auth", "")
    if written in CLI_AUTH_MODES:
        return written
    inherited = (env.get(CLI_AUTH_VARIABLE) or "").strip()
    if inherited in CLI_AUTH_MODES:
        return inherited
    if (env.get(OAUTH_TOKEN_VARIABLE) or "").strip():
        return "subscription"
    if (env.get(LEGACY_CLI_API_KEY) or "").strip():
        return "api_key"
    return None


def _spec(config, provider_id: str) -> ProviderSpec | None:  # noqa: ANN001
    specs = getattr(getattr(config, "brain", None), "provider_specs", None) or {}
    spec = specs.get(provider_id) if specs else None
    if spec is None:
        provider = (getattr(config, "providers", None) or {}).get(provider_id)
        spec = spec_from_provider_config(provider_id, provider) if provider is not None else None
    return spec


def anthropic_spec(config) -> ProviderSpec | None:  # noqa: ANN001
    """The `anthropic` provider the CLI brains use, or `None` when it is not configured."""
    return _spec(config, "anthropic")


def cli_unconfigured_reason(config, mode: str | None) -> str | None:  # noqa: ANN001
    """Why the CLI brains cannot run under ``mode``, or `None`."""
    if mode is None:
        return (
            "no Claude Code credential: set [brain.claude_code] auth "
            '("subscription" with CLAUDE_CODE_OAUTH_TOKEN, or "api_key" with the '
            "anthropic provider's key)"
        )
    if mode == "subscription":
        spec = anthropic_spec(config)
        default = BUILTIN_PROVIDERS["anthropic"][1]
        if spec is not None and (spec.base_url or "").rstrip("/") != default:
            return (
                "[providers.anthropic] base_url is not Anthropic's, and a subscription "
                "token is only ever sent to Anthropic: set [brain.claude_code] auth = "
                "\"api_key\" with the gateway's key, or remove the base_url"
            )
    return None


def resolve_process_auth(config, env: Mapping[str, str] | None = None) -> str | None:  # noqa: ANN001
    """Resolve the CLI mode once for this process and fill `BrainConfig.unconfigured`.

    Called by every process that builds a CLI env: the daemon, the web process,
    `istota task -x`, `istota run`, `istota repl` and doctor. The mode is cached
    on the config; the reasons cover native (its provider) and the CLI brains.
    """
    import os  # noqa: PLC0415

    from istota.config import LoaderMap  # noqa: PLC0415 - config imports this module

    mode = _cache_mode(config, resolve_cli_auth(config, os.environ if env is None else env))
    if getattr(config, "brain", None) is not None:
        reasons: dict[str, str] = {}
        native = native_provider_reason(config)
        if native:
            reasons["native"] = native
        cli = cli_unconfigured_reason(config, mode)
        if cli:
            reasons["claude_code"] = cli
            reasons["tmux_claude"] = cli
        config.brain.unconfigured = LoaderMap(reasons)
    return mode


def _cache_mode(config, mode: str | None) -> str | None:  # noqa: ANN001
    config._cli_mode = mode
    config._cli_mode_resolved = True
    return mode


def process_cli_auth(config) -> str | None:  # noqa: ANN001
    """The mode this process resolved, derived the first time it is asked.

    The lazy path (an env builder in a process that never called
    `resolve_process_auth`) caches the mode only; it fills no unconfigured
    kinds, which are the entry points' to decide."""
    import os  # noqa: PLC0415

    if getattr(config, "_cli_mode_resolved", False):
        return getattr(config, "_cli_mode", None)
    return _cache_mode(config, resolve_cli_auth(config, os.environ))


def _provider_key(config, spec: ProviderSpec, user: str | None) -> InferenceKey:  # noqa: ANN001
    own = user_provider_key(config, user, spec.id) if user else None
    return with_user_key(spec, own).inference_key if own else spec.inference_key


def cli_auth_env(
    config, mode: str | None, user: str | None = None,  # noqa: ANN001
    *, env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The credential and endpoint half of a CLI env, for ``mode``.

    ``"subscription"``: the OAuth token from ``env`` (the process environment by
    default), and nothing else. ``"api_key"``: the `anthropic` provider's key
    (``user``'s own when they have one) and its base URL. No mode, or no
    credential for it: `{}`, and every env still builds. `ANTHROPIC_AUTH_TOKEN`
    is never set.
    """
    import os  # noqa: PLC0415

    if mode == "subscription":
        source = os.environ if env is None else env
        token = (source.get(OAUTH_TOKEN_VARIABLE) or "").strip()
        return {OAUTH_TOKEN_VARIABLE: token} if token else {}
    if mode == "api_key":
        spec = anthropic_spec(config)
        if spec is None:
            return {}
        out = {"ANTHROPIC_BASE_URL": (spec.base_url or "").rstrip("/")}
        key = reveal_inference_key(_provider_key(config, spec, user))
        if key:
            out["ANTHROPIC_API_KEY"] = key
        return out
    return {}


def model_caller_provider(config) -> tuple[str, bool]:  # noqa: ANN001
    """The provider a model-calling skill reaches, and whether it runs the CLI.

    `code_review` runs on the configured brain, and on `claude_code` where that
    is `tmux_claude`: the CLI brains reach `anthropic`, native its own provider.
    """
    brain = getattr(config, "brain", None)
    if getattr(brain, "kind", "claude_code") == "native":
        return (brain.native.provider or "").strip(), False
    return "anthropic", True


def model_caller_env(
    config, mode: str | None, provider_id: str, user: str | None = None,  # noqa: ANN001
    *, cli_reviewer: bool, env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The copy a skill that calls a model gets in its own environment.

    The mode; the CLI credential for it when the caller runs the `claude` CLI
    (the OAuth token under "subscription"; under "api_key" the provider key
    below is the credential); and `ISTOTA_PROVIDER_<ID>_API_KEY` and
    `_EXTRA_HEADERS` for the provider it calls, with the user's own key when
    they have one. The child's `load_config` reads them through the ordinary
    override. One function for the skill proxy and the scheduler alike.
    """
    import json  # noqa: PLC0415
    import os  # noqa: PLC0415

    out: dict[str, str] = {}
    if mode in CLI_AUTH_MODES:
        out[CLI_AUTH_VARIABLE] = mode
    if cli_reviewer and mode != "api_key":
        source = os.environ if env is None else env
        token = (source.get(OAUTH_TOKEN_VARIABLE) or "").strip()
        if token and mode == "subscription":
            out[OAUTH_TOKEN_VARIABLE] = token
        # A CLI reviewer outside "api_key" authenticates with the token alone,
        # so it is handed no provider key it would not use.
        return out
    spec = _spec(config, provider_id) if provider_id else None
    if spec is None:
        return out
    key = reveal_inference_key(_provider_key(config, spec, user))
    if key:
        out[provider_key_variable(provider_id)] = key
    if spec.extra_headers:
        out[provider_headers_variable(provider_id)] = json.dumps(dict(spec.extra_headers))
    return out


def strip_unselected_cli_credentials(
    env: Mapping[str, str], mode: str | None, carried: Mapping[str, str],
) -> dict[str, str]:
    """``env`` without any model credential the mode does not carry, with the
    mode's own values (``carried``, `cli_auth_env`'s answer for ``mode``) put
    back over whatever a merge left there. The last step of a task's env."""
    out = {k: v for k, v in env.items() if not is_model_credential_name(k)}
    out.update(carried)
    return out


def strip_model_credentials(env: Mapping[str, str]) -> dict[str, str]:
    """``env`` without any model credential name, adding nothing."""
    return {k: v for k, v in env.items() if not is_model_credential_name(k)}


# --- which brain can run which model ------------------------------------------

#: The provider the `claude` CLI brains always use.
CLI_PROVIDER = "anthropic"


def provider_kinds(providers: Mapping[str, object] | None) -> dict[str, str]:
    """``id -> kind`` from a mapping of provider ids to `ProviderSpec`s,
    `ProviderConfig`s or bare kind strings. The built-ins are always present."""
    out = {pid: kind for pid, (kind, _url) in BUILTIN_PROVIDERS.items()}
    for pid, value in (providers or {}).items():
        kind = value if isinstance(value, str) else getattr(value, "kind", None)
        if isinstance(kind, str) and kind:
            out[str(pid)] = kind
    return out


def brain_can_run(
    kind: str, ref: str, providers: Mapping[str, object] | None = None,
    *, native_provider: str | None = None,
) -> bool:
    """Whether brain ``kind`` can run the model ``ref`` (``provider/id``).

    The CLI brains run only the `anthropic` provider's models, because the
    `claude` CLI talks only to Anthropic. Native runs the models of the
    provider `[brain.native] provider` names (``native_provider``): it still
    sends every request to that one provider, so a model of another provider
    is not runnable there until native picks its provider per request.
    A ref naming no configured provider runs nowhere. Never raises.
    """
    if not isinstance(ref, str) or "/" not in ref:
        return False
    provider_id = ref.split("/", 1)[0]
    if provider_id not in provider_kinds(providers):
        return False
    if kind in ("claude_code", "tmux_claude"):
        return provider_id == CLI_PROVIDER
    if kind == "native":
        return bool(native_provider) and provider_id == native_provider
    return False
