"""Build a `WhatsAppConfig` from the flat field names the surface used to have.

Three suites construct one from a dict of overrides a test passes by keyword
(`_config(tmp_path, billing_policy="allow_paid")`), and every one of those names
moved under `[whatsapp.cloud]` when the surface gained adapters. The names are
unique across the two levels, so which block a keyword belongs to is decidable
from the dataclass rather than from a table — and a fourth hand-written copy of
that split is what this exists to avoid.

It is a *test* convenience and deliberately not a product one: nothing in `src/`
accepts the flat spelling as keywords. The compatibility path for a config
*file* is `config._migrate_whatsapp_flat`, which is asserted directly by
`tests/test_whatsapp_core.py::TestTheLegacyFlatBlock` rather than through here.

`provider` defaults to `whatsapp_cloud`, because every caller is a Cloud test
that predates the seam. A Baileys case passes `provider="baileys"` explicitly,
which is the same thing a config file has to do.
"""

from __future__ import annotations

import dataclasses

from istota.config import WhatsAppBaileysConfig, WhatsAppCloudConfig, WhatsAppConfig

_CLOUD_FIELDS = frozenset(f.name for f in dataclasses.fields(WhatsAppCloudConfig))
_BAILEYS_FIELDS = frozenset(f.name for f in dataclasses.fields(WhatsAppBaileysConfig))


def build_whatsapp_config(**fields) -> WhatsAppConfig:
    """A `WhatsAppConfig` from flat keywords, each routed to its own block."""
    fields.setdefault("provider", "whatsapp_cloud")
    cloud = {k: fields.pop(k) for k in list(fields) if k in _CLOUD_FIELDS}
    baileys = {k: fields.pop(k) for k in list(fields) if k in _BAILEYS_FIELDS}
    if cloud:
        fields.setdefault("cloud", WhatsAppCloudConfig(**cloud))
    if baileys:
        fields.setdefault("baileys", WhatsAppBaileysConfig(**baileys))
    return WhatsAppConfig(**fields)
