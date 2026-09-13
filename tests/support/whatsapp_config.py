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
    """A `WhatsAppConfig` from flat keywords, each routed to its own block.

    Raises on a keyword that is also given as a whole block, rather than
    letting one win: `build_whatsapp_config(access_token="x", cloud=...)` has
    two answers for one field and neither is obviously the caller's.
    """
    fields.setdefault("provider", "whatsapp_cloud")
    cloud = {k: fields.pop(k) for k in list(fields) if k in _CLOUD_FIELDS}
    baileys = {k: fields.pop(k) for k in list(fields) if k in _BAILEYS_FIELDS}
    for name, collected in (("cloud", cloud), ("baileys", baileys)):
        if not collected:
            continue
        if name in fields:
            raise TypeError(
                f"build_whatsapp_config got both {name}= and the flat field(s) "
                f"{sorted(collected)}; pass one or the other"
            )
        block = WhatsAppCloudConfig if name == "cloud" else WhatsAppBaileysConfig
        fields[name] = block(**collected)
    return WhatsAppConfig(**fields)
