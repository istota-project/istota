"""Where the map's background tiles come from, decided in one place.

The location maps used to name ``basemaps.cartocdn.com`` twice, as literals
inside a Svelte component. CARTO now requires an API key on those URLs and
watermarks unauthenticated requests with "API KEY REQUIRED" (ISSUE-334), so
every map surface rendered defaced tiles and nothing in the deployment could
change that without a code edit.

This module is the seam that fixes the class rather than the instance: a
provider name and a few strings of deployment config resolve to the concrete
URLs the browser fetches. Adding a provider is a row in ``PROVIDERS``; changing
one on a running install is a config edit.

Two consumers, and they must agree or the feature is worse than useless: the
web endpoint that hands the resolved spec to the frontend, and ``doctor``'s
``web.basemap`` check. A second copy of the URL shapes in the checker would let
doctor pass while the map was blank.

**The watermark is not observable from the response, and that is why it went
unnoticed for so long.** Measured against the live service on 2026-08-28: a
keyless request, a request with a bogus key, and (by construction) a request
with a good key all return HTTP 200, ``content-type: image/png``, and — for the
first two — a byte-identical body and ETag. There is no status code, no header
and no length to key on. So no fetch can validate a CARTO key, which is why
this module offers no probe helper and ``check_basemap`` opens no socket: the
question is decided from configuration, where it is certain and free.

**A provider that needs a key and has none falls back rather than reporting
it.** Returning the keyless templates with a ``needs_key`` flag was the same
bug wearing a label — nothing in a browser can act on a flag, so the user
still got the watermark. The flag survives on the fallback spec as the
*reason*, which is what lets doctor name the misconfiguration precisely.

The API key is disclosed by construction. MapLibre puts it in the tile URL, so
it ships to every browser that loads a map and appears in every request they
make. It is deployment config, never a repo literal, and it is treated as
public throughout — the spec this module returns is meant for the browser.

stdlib-only leaf: imports nothing from the package, reads no config file and
opens no socket. Everything it needs arrives as an argument.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from urllib.parse import quote, urlsplit

__all__ = [
    "DEFAULT_PROVIDER",
    "PROVIDERS",
    "BasemapSpec",
    "provider_names",
    "resolve_basemap",
    "select_provider",
]

# The provider a fresh install gets. OpenFreeMap serves OSM-derived vector
# tiles with no key, no account and no usage limit, and publishes a dark and a
# light flavour — so the shipped default renders correctly with zero setup.
# It is still a third-party request and so does not answer the privacy half of
# ISSUE-334; that wants a self-hosted archive, which is deferred work.
DEFAULT_PROVIDER = "openfreemap"

# Every provider, and what kind of thing it resolves to.
#
# "style" is a MapLibre style URL: the browser fetches it and the style names
# its own sources, glyphs, sprites and attribution. "raster" is a tile-URL
# template that this module wraps in a style the frontend assembles.
PROVIDERS: dict[str, dict[str, str]] = {
    "openfreemap": {
        "kind": "style",
        "dark": "https://tiles.openfreemap.org/styles/dark",
        "light": "https://tiles.openfreemap.org/styles/positron",
        "attribution": "",  # the style JSON carries its own
    },
    "carto": {
        "kind": "raster",
        # `@2x` is the retina variant the maps have always used; dropping it
        # would be a visible regression on every display the product targets.
        "dark": "https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png",
        "light": "https://basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png",
        "attribution": (
            '&copy; <a href="https://carto.com/">CARTO</a> '
            '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>'
        ),
    },
    "osm": {
        "kind": "raster",
        # One style only — the OSM standard layer has no dark flavour. Kept as
        # a keyless last resort, not a default: the OSMF tile usage policy
        # discourages application use.
        "dark": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "light": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attribution": (
            '&copy; <a href="https://www.openstreetmap.org/copyright">'
            "OpenStreetMap</a> contributors"
        ),
    },
    # Resolved from the operator's own URLs rather than from this table.
    "custom": {"kind": "style", "dark": "", "light": "", "attribution": ""},
}

# Providers whose tiles are unusable without a key.
_KEYED = frozenset({"carto"})


@dataclass(frozen=True)
class BasemapSpec:
    """The resolved basemap, as the browser will see it.

    ``kind`` is ``"style"`` (``dark``/``light`` are MapLibre style URLs) or
    ``"raster"`` (they are tile-URL templates). ``warning`` is operator-facing
    and non-empty only when something is wrong or surprising; the frontend does
    not render it, doctor does.

    ``fell_back`` separates the two things a warning can mean, because they
    deserve different severities and a string cannot be branched on safely:
    ``True`` says the resolution did not honour what was configured (an unknown
    provider, a ``custom`` with no URL) and is a misconfiguration to report;
    ``False`` with a warning is a caveat about a provider the operator chose on
    purpose, which should not nag on every run.
    """

    provider: str
    kind: str
    dark: str
    light: str
    attribution: str = ""
    needs_key: bool = False
    fell_back: bool = False
    warning: str = ""

    def as_dict(self) -> dict:
        """Plain JSON for the config endpoint. No credential under its own name."""
        return asdict(self)


def provider_names() -> tuple[str, ...]:
    """Every accepted `provider` value, for config validation and docs."""
    return tuple(PROVIDERS)


def resolve_basemap(
    provider: str = DEFAULT_PROVIDER,
    *,
    api_key: str = "",
    dark_style: str = "",
    light_style: str = "",
    attribution: str = "",
) -> BasemapSpec:
    """Turn basemap config into the URLs the browser should fetch.

    Never raises and never returns an unusable spec. Every failure mode —
    an unknown provider, a ``custom`` with no URL — falls back to the keyless
    default and says why in ``warning``, because the alternative is a blank
    rectangle where the map was, which is the same invisible failure that let
    the watermark run unnoticed.
    """
    name = (provider or "").strip().lower() or DEFAULT_PROVIDER

    if name not in PROVIDERS:
        fallback = _resolve_known(DEFAULT_PROVIDER, api_key="")
        return _with_warning(
            fallback,
            f"unknown basemap provider {provider!r}; "
            f"using {DEFAULT_PROVIDER}. Valid: {', '.join(provider_names())}",
        )

    if name == "custom":
        return _resolve_custom(dark_style, light_style, attribution)

    return _resolve_known(name, api_key=api_key)


def select_provider(configured_provider: str, *, user_api_key: str) -> str:
    """Which provider applies, given what the deployment set and what the user stored.

    One rule: a user who has stored a CARTO key has chosen CARTO. It wins over
    the deployment provider, including an explicitly configured one, because
    the alternative is a settings page where pasting a key does nothing
    visible and the reason lives in a TOML file the user cannot reach.

    Stated on the settings card itself rather than left to be inferred, which
    is what keeps it a rule and not a surprise.
    """
    if (user_api_key or "").strip():
        return "carto"
    return (configured_provider or "").strip().lower() or DEFAULT_PROVIDER


def _resolve_known(name: str, *, api_key: str) -> BasemapSpec:
    """A provider from the table, with its key applied if it takes one."""
    row = PROVIDERS[name]
    dark, light = row["dark"], row["light"]
    warning = ""
    needs_key = False

    if name in _KEYED:
        # Stripped, and the stripped value is what travels. `select_provider`
        # already treats a whitespace-only key as absent; without the same test
        # here a key of "   " would be embedded as `api_key=%20%20%20`, reported
        # `needs_key=False` with no warning, and doctor would call a defaced map
        # healthy — the exact failure this module exists to name.
        key = (api_key or "").strip()
        if key:
            dark = _with_api_key(dark, key)
            light = _with_api_key(light, key)
        else:
            # Fall back rather than hand the browser tiles we know are
            # defaced. Returning the keyless CARTO templates with a
            # `needs_key` flag was the same bug wearing a label: nothing in
            # the frontend can act on a flag, so the user still got the
            # watermark this whole change exists to remove.
            #
            # `needs_key` stays True on the fallback spec, as the *reason*.
            # That is what lets doctor say "carto, with no key" rather than
            # the much less useful "provider did not resolve as written".
            fallback = _resolve_known(DEFAULT_PROVIDER, api_key="")
            return BasemapSpec(
                provider=fallback.provider,
                kind=fallback.kind,
                dark=fallback.dark,
                light=fallback.light,
                attribution=fallback.attribution,
                needs_key=True,
                fell_back=True,
                warning=(
                    f"{name} requires an API key; without one every tile is "
                    "returned watermarked 'API KEY REQUIRED' with a 200 "
                    f"status, so {DEFAULT_PROVIDER} is used instead"
                ),
            )

    if name == "osm":
        warning = (
            "the OpenStreetMap standard layer has no dark flavour, so the dark "
            "theme is served light tiles; it is also covered by the OSMF tile "
            "usage policy, which discourages application use"
        )

    return BasemapSpec(
        provider=name,
        kind=row["kind"],
        dark=dark,
        light=light,
        attribution=row["attribution"],
        needs_key=needs_key,
        warning=warning,
    )


def _resolve_custom(
    dark_style: str, light_style: str, attribution: str
) -> BasemapSpec:
    """The operator's own MapLibre style URLs.

    One URL is enough: an operator serving a single self-hosted style gets it
    on both themes rather than a broken half. With neither, there is nothing to
    render, so this falls back rather than returning empty URLs.
    """
    dark = (dark_style or "").strip()
    light = (light_style or "").strip()

    if not dark and not light:
        fallback = _resolve_known(DEFAULT_PROVIDER, api_key="")
        return _with_warning(
            fallback,
            "basemap provider is 'custom' but neither dark_style nor "
            f"light_style is set; using {DEFAULT_PROVIDER}",
        )

    # Only http(s). These URLs are handed to the browser's `setStyle` *and*
    # opened by the doctor check with urllib, whose default opener also
    # handles `file:` and `ftp:` — so a config typo, or an operator following
    # bad advice, would have the daemon open a local path and put its status
    # and content-type into the admin Health pane. The docs have only ever
    # promised an http(s) style URL; this makes that a rule rather than a hope.
    rejected = [u for u in (dark, light) if u and not _is_http_url(u)]
    if rejected:
        fallback = _resolve_known(DEFAULT_PROVIDER, api_key="")
        return _with_warning(
            fallback,
            "a custom basemap style URL is not http(s) and was refused; "
            f"using {DEFAULT_PROVIDER}",
        )

    return BasemapSpec(
        provider="custom",
        kind="style",
        dark=dark or light,
        light=light or dark,
        attribution=(attribution or "").strip(),
    )


def _is_http_url(url: str) -> bool:
    """Whether `url` is an ordinary http(s) URL with a host.

    Deliberately not a validator — it answers the one question that decides
    whether the daemon may open it and the browser may load it.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        # `http://[::1` raises Invalid IPv6 URL. A refusal is the right answer
        # for an unparseable URL anyway.
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def _with_api_key(template: str, api_key: str) -> str:
    """Append the key to a tile template.

    ``quote`` with an empty ``safe`` so a key containing ``&`` or ``=`` cannot
    forge a second query parameter or truncate the one it is in. The template's
    ``{z}/{x}/{y}`` placeholders are in the path and are untouched — only the
    key is encoded, never the URL it is appended to.
    """
    separator = "&" if "?" in template else "?"
    return f"{template}{separator}api_key={quote(api_key, safe='')}"


def _with_warning(spec: BasemapSpec, warning: str) -> BasemapSpec:
    """Mark a spec as a fallback, keeping any warning it already carried."""
    existing = spec.warning
    combined = f"{warning}. {existing}" if existing else warning
    return BasemapSpec(
        provider=spec.provider,
        kind=spec.kind,
        dark=spec.dark,
        light=spec.light,
        attribution=spec.attribution,
        needs_key=spec.needs_key,
        fell_back=True,
        warning=combined,
    )
