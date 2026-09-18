"""The surface-wide doc inventories, held against the code they enumerate.

A page that enumerates the *whole* surface — every transport, every secret
override — is touched by no feature branch, so it goes stale silently and all
at once: ISSUE-495 found five such pages wrong in the same week, from a
`transport/` that had grown by two and an `_env_secret_overrides` that had
grown by twelve. Feature docs shipped with their features; the inventories did
not.

Both subjects are read as *source text* rather than called, and for the same
reason in each case. `make_registry` registers behind `if config.x.enabled`,
so calling it against a default `Config` yields a subset and a guard over the
result would pass with a transport switched off. `_env_secret_overrides` is a
local inside `load_config` and cannot be imported at all.

**ISSUE-459 residual, stated rather than implied.** The two source reads go
through `tests.support.drift.source_of`, so `scripts/qt` selects these guards
when the *code* moves. The docs are read off disk as text, which `source_of`
does not cover, so a docs-only edit does not select them — the full pass before
a commit is what catches that direction.
"""

from __future__ import annotations

import re
from pathlib import Path

from istota import config as config_module
from istota import secret_schema
from istota.transport import registry as registry_module
from tests.support.drift import source_of

ROOT = Path(__file__).resolve().parent.parent

#: Cardinal words for the "N transports ship" sentence. A count in the prose is
#: what makes the sentence checkable rather than a list somebody can append to
#: without reading.
COUNT_WORDS = {
    6: "Six", 7: "Seven", 8: "Eight", 9: "Nine", 10: "Ten", 11: "Eleven",
}

#: Registry name -> how the surface is labelled in the front page's
#: input-channel diagram, or `None` where it is not an input channel at all.
#: Neither half is derivable.
#:
#: The spelling is not the registry key: `istota_file` is only ever called
#: "TASKS.md" in front of a reader and `talk` is "Nextcloud Talk".
#:
#: `None` is the delivery-only surface. `docs/index.md`'s list and diagram are
#: both "ways you can talk to it", and `NtfyTransport.poll` returns `[]`
#: unconditionally — nothing ever arrives that way, so the page is right to
#: leave it out and this guard must not push it in.
#:
#: A `KeyError` here when a ninth transport lands is the guard working: it
#: forces whoever adds it to answer both questions rather than leaking a
#: registry key into prose or quietly widening the list.
FRONT_PAGE_NAMES: dict[str, str | None] = {
    "talk": "Nextcloud Talk",
    "web": "Web chat",
    "whatsapp": "WhatsApp",
    "sms": "SMS",
    "email": "Email",
    "istota_file": "TASKS.md",
    "repl": "REPL",
    "ntfy": None,  # delivery only
}


def registered_transports() -> list[tuple[str, str]]:
    """The `(registry name, class stem)` pairs `make_registry` installs.

    Source text, not a call: four of the eight are behind an `if
    config.<surface>.enabled`, so a registry built from a default `Config`
    is a subset and a guard over it would pass with a transport switched off.
    """
    src = source_of(registry_module.make_registry)
    pairs = re.findall(r'transports\[["\'](\w+)["\']\]\s*=\s*(\w+)Transport\(', src)
    assert pairs, "no transport registrations found — did make_registry change shape?"

    # A registration the pattern cannot see is the dangerous direction: the
    # guard would go on asserting about the eight it found while a ninth was
    # documented by nobody, and every downstream check stays green — the count
    # word still matches, and `FRONT_PAGE_NAMES` is never asked about a key it
    # never captured, so the KeyError advertised below never fires. Measured
    # blind spots: `transports.update({...})`, `setdefault`, a classmethod
    # constructor, a dict literal, and a parenthesized right-hand side, which
    # is what an ordinary long-line rewrap produces.
    #
    # `make_registry` imports each class in its own body, so the import list is
    # an independent count of what it means to register.
    imported = set(re.findall(r"^\s*from \.\w+ import (\w+)Transport$", src, re.M))
    registered = {stem for _name, stem in pairs}
    assert imported == registered, (
        "make_registry imports and registrations disagree — a transport is "
        "registered in a shape this guard cannot see: "
        f"imported-not-registered={sorted(imported - registered)}, "
        f"registered-not-imported={sorted(registered - imported)}"
    )
    return pairs


def secret_overrides() -> list[tuple[str, str, str]]:
    """The `(env var, config section, field)` triples of `_env_secret_overrides`.

    A local inside `load_config`, so there is nothing to import.
    """
    src = source_of(config_module.load_config)
    block = re.search(r"_env_secret_overrides = \[(.*?)\n    \]", src, re.S)
    assert block, "_env_secret_overrides not found — did load_config change shape?"
    body = block.group(1)
    triples = re.findall(
        r"""\(\s*["'](ISTOTA_[A-Z0-9_]+)["'],\s*["']([a-z0-9_.]+)["'],\s*["'](\w+)["']""",
        body,
    )
    assert triples, "_env_secret_overrides parsed empty"

    # Same dangerous direction as the transports above, and the empty-list
    # assert does not reach it: an entry written in a shape the pattern misses
    # is dropped, and both doc guards then check a subset while passing. Every
    # entry names exactly one `ISTOTA_` variable, so counting those is an
    # independent tally of how many there should be.
    assert len(triples) == body.count("ISTOTA_"), (
        f"parsed {len(triples)} of {body.count('ISTOTA_')} entries in "
        "_env_secret_overrides — one is written in a shape this guard cannot "
        "see, and would be documented and guarded by nobody"
    )
    return triples


class TestTheTransportInventory:
    def test_the_overview_names_every_transport_and_counts_them(self):
        pairs = registered_transports()
        overview = (ROOT / "docs" / "architecture" / "overview.md").read_text()

        line = next(
            (ln for ln in overview.splitlines() if "transports ship" in ln), None,
        )
        assert line, "docs/architecture/overview.md has no 'N transports ship' sentence"

        # Scoped to the sentence, never the file: "Web" appears all over this
        # page, so a file-wide substring test passes vacuously.
        assert len(pairs) in COUNT_WORDS, (
            f"{len(pairs)} transports ship; add that to COUNT_WORDS"
        )
        assert COUNT_WORDS[len(pairs)] in line, (
            f"{len(pairs)} transports ship; the sentence says: {line.strip()!r}"
        )
        for _name, stem in pairs:
            assert stem in line, f"{stem}Transport is registered but absent from: {line.strip()!r}"

    def test_the_front_page_diagram_lists_every_input_channel(self):
        """The half-state ISSUE-495 caught: SMS in the diagram, WhatsApp not,
        because the two shipped a week apart.

        Scoped to the diagram, and that scoping is the whole assertion. Searched
        file-wide this passes on exactly the state it exists to catch: the one
        mention of WhatsApp on the page before this was fixed was its nav link
        in the docs list, which says nothing about whether you can talk to it
        that way. Measured against `c68ed698` — file-wide green, diagram red.

        The prose sentence above the diagram says the same thing in different
        words ("the built-in web app", "the terminal REPL"), and is deliberately
        not asserted on: a regex over prose fails on a rewording, and a guard
        that cries wolf gets deleted.

        One-directional, like the override guard below: it walks the registered
        transports, so a *removed* one leaves a stale line in the diagram that
        nothing here notices. The overview's count word catches a removal; this
        is about pages falling behind an addition, which is the way they fall.
        """
        index = (ROOT / "docs" / "index.md").read_text()
        blocks = [b for b in index.split("```") if "durable task queue" in b]
        assert len(blocks) == 1, (
            f"expected exactly one input-channel diagram in docs/index.md, found {len(blocks)}"
        )
        diagram = blocks[0]

        for name, _stem in registered_transports():
            spelling = FRONT_PAGE_NAMES[name]
            if spelling is None:
                continue
            assert spelling in diagram, (
                f"transport {name!r} is registered but {spelling!r} is missing "
                "from the input-channel diagram in docs/index.md"
            )


class TestTheSecretOverrideInventory:
    def test_every_override_is_in_the_environment_variable_reference(self):
        """One-directional. `ISTOTA_BRAIN_NATIVE_API_KEY` is legitimately in the
        table while living outside the list — it is applied separately — so the
        converse would fail on a correct doc.

        Full spellings, not a `_SUFFIX` shorthand: the page is a reference an
        operator greps for the variable already sitting in their `secrets.env`.
        """
        doc = (ROOT / "docs" / "reference" / "environment-variables.md").read_text()
        # Backticked, not bare: `ISTOTA_SMS_TWILIO_API_KEY` is a prefix of
        # `ISTOTA_SMS_TWILIO_API_KEY_SID`, so a bare substring lets a longer
        # variable's row satisfy a shorter variable's check. No such pair
        # exists today; the spelling costs nothing and removes the class.
        missing = [var for var, _s, _f in secret_overrides() if f"`{var}`" not in doc]
        assert not missing, (
            "in _env_secret_overrides, absent from docs/reference/environment-variables.md: "
            + ", ".join(missing)
        )

    def test_every_override_section_has_a_credentials_row(self):
        """Section-level, deliberately, where the reference above is per-variable.

        `credentials.md` is an inventory of *credentials*, one row each, and it
        keeps the non-secret account identifiers beside them (`account_sid`,
        `messaging_profile_id`) out of the table by design. Asserting per
        variable would force those in; asserting per section catches what
        actually went wrong — a whole credential-holding service with no row.

        Scoped to the table rows, and that scoping is the assertion. Searched
        file-wide it cannot see a deleted row for any section the surrounding
        prose happens to name, which is three of the nine: `[caldav]` is in the
        paragraph below the table, `[web]` in the `token_storage` sentence, and
        `[nextcloud]` in the CalDAV row's own "derived from" text. Measured —
        delete the CalDAV row and a file-wide search stays green.
        """
        doc = (ROOT / "docs" / "configuration" / "credentials.md").read_text()
        rows = [ln for ln in doc.splitlines() if ln.startswith("|")]
        missing = sorted({
            section for _v, section, _f in secret_overrides()
            if not any(f"[{section}]" in row for row in rows)
        })
        assert not missing, (
            "config sections with an env override but no row in "
            "docs/configuration/credentials.md: " + ", ".join(missing)
        )


class TestThePerUserServiceInventory:
    """Every service in the secrets schema has a row in `credentials.md`.

    The guard above walks `_env_secret_overrides`, which is the *global*
    credential inventory, so it sees nothing when a **per-user** service is
    added: `vault` went into `CONNECTED_SERVICE_SCHEMA` carrying a passphrase
    and the whole ISSUE-495 apparatus stayed green, because a service is not an
    env override and nothing else here reads the schema. That is the shape the
    module docstring describes — a page enumerating a whole surface, touched by
    no feature branch — one dict over.

    Widened rather than written down as a known gap, on the rule the vendored
    devbox leaves record: a guard that names its own subjects covers only the
    ones its author thought of. This walks the schema.
    """

    def services(self) -> dict[str, str]:
        """`{service key: label}` for every declared service, connected and module.

        The imported dicts rather than a regex over their source: they are plain
        data with a public accessor, so there is nothing to parse and no shape
        for a new entry to hide in. `source_of` is still needed — they are
        module-level literals evaluated at *import*, so an edit to one is
        attributed to whichever test imported the module first and `scripts/qt`
        would not reselect this guard on exactly the change it exists to catch.
        """
        source_of(secret_schema)
        known = secret_schema.all_known_services()
        assert known, "the secrets schema parsed empty"
        return {
            service: str(schema.get("label", ""))
            for service, schema in known.items()
        }

    def test_every_service_is_named_in_the_credential_inventory(self):
        """Presence per service, matched on the schema key or on the label.

        Neither alone works, and that is a property of the page rather than a
        looseness worth removing. The doc names `ntfy` and `feeds` by their
        schema key while the schema labels them "ntfy push" and "Feeds
        (Tumblr)"; it names "Google Workspace" and "Native brain provider" in
        words while the keys carry underscores. So a service passes on either
        spelling, with `_` read as a space.

        Scoped to the table rows, for the reason the override guard beside it
        gives: searched file-wide, several of these are named in the surrounding
        prose and a deleted row would not be seen.
        """
        doc = (ROOT / "docs" / "configuration" / "credentials.md").read_text()
        rows = "\n".join(
            ln for ln in doc.splitlines() if ln.startswith("|")
        ).casefold()

        missing = sorted(
            service
            for service, label in self.services().items()
            if service.replace("_", " ").casefold() not in rows
            and not (label and label.casefold() in rows)
        )
        assert not missing, (
            "declared in secret_schema, absent from the tables in "
            "docs/configuration/credentials.md: " + ", ".join(missing)
        )

    def test_the_guard_reads_a_populated_schema(self):
        """Non-vacuity. Every assertion above is an absence, so a schema that
        parsed to `{}` — or to entries carrying neither a key nor a label —
        would pass about nothing."""
        services = self.services()
        assert len(services) >= 8, f"only {len(services)} services parsed"
        assert all(services.values()), (
            "a service declares no label, so half the match above is dead for it"
        )
