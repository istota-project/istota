"""Tests for the Google Workspace service → scope map (ISSUE-240).

The map is the one table shared by the settings picker, the granted-scope
display and the docs. Everything here is pure: no DB, no network.
"""

import pytest

from istota import google_scopes as gs


DRIVE_RO = "https://www.googleapis.com/auth/drive.readonly"
DRIVE_FULL = "https://www.googleapis.com/auth/drive"
GMAIL_RO = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_FULL = "https://www.googleapis.com/auth/gmail.modify"
CAL_RO = "https://www.googleapis.com/auth/calendar.readonly"
CAL_FULL = "https://www.googleapis.com/auth/calendar"
SHEETS_RO = "https://www.googleapis.com/auth/spreadsheets.readonly"
DOCS_RO = "https://www.googleapis.com/auth/documents.readonly"

READONLY_FIVE = [DRIVE_RO, GMAIL_RO, CAL_RO, SHEETS_RO, DOCS_RO]


class TestServiceTable:
    def test_covers_the_six_documented_services(self):
        assert [s.key for s in gs.SERVICES] == [
            "drive", "gmail", "calendar", "sheets", "docs", "chat",
        ]

    def test_every_service_has_both_levels(self):
        for svc in gs.SERVICES:
            assert svc.readonly, svc.key
            assert svc.full, svc.key

    def test_readonly_and_full_scope_sets_are_disjoint(self):
        for svc in gs.SERVICES:
            assert not set(svc.readonly) & set(svc.full), svc.key

    def test_no_scope_is_claimed_by_two_services(self):
        seen: dict[str, str] = {}
        for svc in gs.SERVICES:
            for scope in (*svc.readonly, *svc.full):
                assert scope not in seen, f"{scope} claimed by {seen.get(scope)} and {svc.key}"
                seen[scope] = svc.key

    def test_config_defaults_are_all_recognised(self):
        """The shipped default ceiling must not contain an unmapped scope."""
        from istota.config import GoogleWorkspaceConfig

        for scope in GoogleWorkspaceConfig().scopes:
            assert gs.scope_owner(scope) is not None, scope

    def test_scopes_for_level(self):
        drive = gs.service("drive")
        assert drive.scopes_for(gs.LEVEL_FULL) == (DRIVE_FULL,)
        assert drive.scopes_for(gs.LEVEL_READONLY) == (DRIVE_RO,)
        assert drive.scopes_for(gs.LEVEL_OFF) == ()

    def test_service_lookup_unknown_returns_none(self):
        assert gs.service("dropbox") is None


class TestScopeOwner:
    def test_readonly_scope(self):
        assert gs.scope_owner(GMAIL_RO) == ("gmail", gs.LEVEL_READONLY)

    def test_full_scope(self):
        assert gs.scope_owner(GMAIL_FULL) == ("gmail", gs.LEVEL_FULL)

    def test_unknown_scope(self):
        assert gs.scope_owner("https://www.googleapis.com/auth/youtube") is None


class TestLevelsFromScopes:
    def test_readonly_default_set(self):
        assert gs.levels_from_scopes(READONLY_FIVE) == {
            "drive": "readonly", "gmail": "readonly", "calendar": "readonly",
            "sheets": "readonly", "docs": "readonly",
        }

    def test_full_wins_over_readonly_for_the_same_service(self):
        levels = gs.levels_from_scopes([DRIVE_RO, DRIVE_FULL])
        assert levels == {"drive": "full"}

    def test_unknown_scopes_are_ignored(self):
        assert gs.levels_from_scopes(["https://example.test/nope"]) == {}

    def test_empty(self):
        assert gs.levels_from_scopes([]) == {}


class TestOfferedServices:
    def test_lists_every_service_with_the_ceiling_level(self):
        offered = gs.offered_services(READONLY_FIVE)
        by_key = {o["service"]: o for o in offered}
        assert set(by_key) == {s.key for s in gs.SERVICES}
        assert by_key["drive"]["max_level"] == "readonly"
        # Chat has no scope in the default config, so the instance does not
        # offer it at all — distinct from "the user did not grant it".
        assert by_key["chat"]["max_level"] == "off"

    def test_order_follows_the_table(self):
        offered = gs.offered_services(READONLY_FIVE)
        assert [o["service"] for o in offered] == [s.key for s in gs.SERVICES]

    def test_labels_included(self):
        offered = gs.offered_services([DRIVE_FULL])
        assert offered[0]["label"] == "Drive"


class TestDefaultSelection:
    def test_is_everything_the_operator_allows(self):
        assert gs.default_selection(READONLY_FIVE) == {
            "drive": "readonly", "gmail": "readonly", "calendar": "readonly",
            "sheets": "readonly", "docs": "readonly", "chat": "off",
        }

    def test_names_every_service_even_when_off(self):
        sel = gs.default_selection([DRIVE_FULL])
        assert set(sel) == {s.key for s in gs.SERVICES}
        assert sel["drive"] == "full"
        assert sel["gmail"] == "off"


class TestNormalizeSelection:
    def test_drops_unknown_services(self):
        assert gs.normalize_selection({"drive": "full", "dropbox": "full"}) == {
            "drive": "full",
        }

    def test_drops_unknown_levels(self):
        assert gs.normalize_selection({"drive": "write", "gmail": "full"}) == {
            "gmail": "full",
        }

    def test_keeps_explicit_off(self):
        """An all-off selection must stay distinguishable from "unset"."""
        assert gs.normalize_selection({"drive": "off"}) == {"drive": "off"}

    def test_non_mapping_returns_empty(self):
        assert gs.normalize_selection(None) == {}
        assert gs.normalize_selection(["drive"]) == {}

    def test_coerces_case_and_whitespace(self):
        assert gs.normalize_selection({"drive": " Full "}) == {"drive": "full"}


class TestResolveSelection:
    def test_unset_selection_grants_the_whole_ceiling(self):
        assert gs.resolve_selection({}, READONLY_FIVE) == READONLY_FIVE

    def test_selection_subset(self):
        resolved = gs.resolve_selection(
            {"drive": "readonly", "gmail": "off", "calendar": "readonly",
             "sheets": "off", "docs": "off", "chat": "off"},
            READONLY_FIVE,
        )
        assert resolved == [DRIVE_RO, CAL_RO]

    def test_clamped_to_the_operator_ceiling(self):
        """Asking for full against a read-only ceiling yields read-only."""
        resolved = gs.resolve_selection({"drive": "full"}, [DRIVE_RO])
        assert resolved == [DRIVE_RO]

    def test_service_absent_from_the_ceiling_is_dropped(self):
        resolved = gs.resolve_selection({"chat": "full"}, [DRIVE_RO])
        assert resolved == []

    def test_missing_key_in_a_non_empty_selection_is_off(self):
        """A service added to the ceiling later is not silently granted."""
        resolved = gs.resolve_selection({"drive": "readonly"}, READONLY_FIVE)
        assert resolved == [DRIVE_RO]

    def test_all_off_is_not_treated_as_unset(self):
        selection = {s.key: "off" for s in gs.SERVICES}
        assert gs.resolve_selection(selection, READONLY_FIVE) == []

    def test_output_order_follows_the_table(self):
        ceiling = [DOCS_RO, DRIVE_RO, GMAIL_RO]
        assert gs.resolve_selection({}, ceiling) == [DRIVE_RO, GMAIL_RO, DOCS_RO]

    def test_unrecognised_ceiling_scopes_are_requested_verbatim(self):
        """No picker row can turn one off, so its absence is not a choice."""
        resolved = gs.resolve_selection({}, [DRIVE_RO, "https://example.test/x"])
        assert resolved == [DRIVE_RO, "https://example.test/x"]

    def test_unrecognised_ceiling_scopes_survive_a_narrowing_selection(self):
        resolved = gs.resolve_selection(
            {"drive": "off"}, [DRIVE_RO, "https://example.test/x"],
        )
        assert resolved == ["https://example.test/x"]

    def test_an_entirely_unmapped_ceiling_still_resolves(self):
        """An operator running narrow scopes must not lose connect outright."""
        ceiling = [
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/drive.file",
        ]
        assert gs.resolve_selection({}, ceiling) == ceiling

    def test_openid_in_the_ceiling_survives(self):
        """authlib mints the OIDC nonce only when the resolved scope has it."""
        assert "openid" in gs.resolve_selection({}, [DRIVE_RO, "openid"])

    def test_unrecognised_scopes_are_deduplicated(self):
        resolved = gs.resolve_selection({}, ["https://example.test/x"] * 2)
        assert resolved == ["https://example.test/x"]

    def test_a_mapped_scope_is_never_double_counted(self):
        """The passthrough must not re-append something the picker emitted."""
        assert gs.resolve_selection({}, [DRIVE_RO, DRIVE_RO]) == [DRIVE_RO]


class TestUnofferedScopes:
    def test_lists_ceiling_scopes_the_map_does_not_know(self):
        assert gs.unoffered_scopes([DRIVE_RO, "https://example.test/x"]) == [
            "https://example.test/x",
        ]

    def test_keeps_the_operator_order(self):
        assert gs.unoffered_scopes(["b://2", DRIVE_RO, "a://1"]) == ["b://2", "a://1"]

    def test_deduplicates(self):
        assert gs.unoffered_scopes(["x://1", "x://1"]) == ["x://1"]

    def test_empty_when_the_ceiling_is_fully_mapped(self):
        assert gs.unoffered_scopes(READONLY_FIVE) == []

    def test_multi_scope_level_expands(self):
        chat = gs.service("chat")
        ceiling = list(chat.full)
        assert gs.resolve_selection({"chat": "full"}, ceiling) == list(chat.full)


class TestSummarizeGranted:
    def test_groups_scopes_per_service(self):
        summary = gs.summarize_granted([DRIVE_FULL, CAL_RO])
        rows = {r["service"]: r for r in summary["services"]}
        assert rows["drive"]["level"] == "full"
        assert rows["drive"]["label"] == "Drive"
        assert rows["calendar"]["level"] == "readonly"
        assert rows["drive"]["scopes"] == [DRIVE_FULL]

    def test_only_granted_services_appear(self):
        summary = gs.summarize_granted([DRIVE_RO])
        assert [r["service"] for r in summary["services"]] == ["drive"]

    def test_order_follows_the_table(self):
        summary = gs.summarize_granted([DOCS_RO, DRIVE_RO])
        assert [r["service"] for r in summary["services"]] == ["drive", "docs"]

    def test_unrecognised_scope_is_shown_not_dropped(self):
        """The map will lag the config; an unknown scope must still display."""
        summary = gs.summarize_granted([DRIVE_RO, "https://www.googleapis.com/auth/tasks"])
        assert summary["unrecognized"] == ["https://www.googleapis.com/auth/tasks"]

    def test_partial_level_is_flagged(self):
        """Chat's read level needs two scopes; one of them is a partial grant."""
        chat = gs.service("chat")
        summary = gs.summarize_granted([chat.readonly[0]])
        row = summary["services"][0]
        assert row["service"] == "chat"
        assert row["level"] == "readonly"
        assert row["complete"] is False

    def test_complete_level_is_flagged(self):
        chat = gs.service("chat")
        summary = gs.summarize_granted(list(chat.readonly))
        assert summary["services"][0]["complete"] is True

    def test_a_lower_level_scope_of_the_same_service_is_still_shown(self):
        """It is in the map, so it never reaches `unrecognized`, and it is not
        in the reported level's tuple — without `also` it appears nowhere."""
        chat = gs.service("chat")
        summary = gs.summarize_granted([chat.full[0], chat.readonly[1]])
        row = summary["services"][0]
        assert row["level"] == "full"
        assert row["also"] == [chat.readonly[1]]
        assert summary["unrecognized"] == []

    def test_also_is_empty_for_a_clean_grant(self):
        assert gs.summarize_granted([DRIVE_RO])["services"][0]["also"] == []

    def test_empty_grant(self):
        assert gs.summarize_granted([]) == {"services": [], "unrecognized": []}

    def test_openid_boilerplate_is_not_reported_as_unrecognized(self):
        """Google appends these to every grant; they are not a service."""
        summary = gs.summarize_granted([
            DRIVE_RO, "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/userinfo.profile",
        ])
        assert summary["unrecognized"] == []
        assert [r["service"] for r in summary["services"]] == ["drive"]


class TestMissingScopes:
    def test_reports_requested_but_not_granted(self):
        missing = gs.missing_scopes([DRIVE_RO, GMAIL_RO], [DRIVE_RO])
        assert missing == [GMAIL_RO]

    def test_full_grant_satisfies_a_readonly_request(self):
        """Google returns the broader scope; that is not a shortfall."""
        assert gs.missing_scopes([DRIVE_RO], [DRIVE_FULL]) == []

    def test_readonly_grant_does_not_satisfy_a_full_request(self):
        assert gs.missing_scopes([DRIVE_FULL], [DRIVE_RO]) == [DRIVE_FULL]

    def test_a_partial_multi_scope_grant_reports_the_shortfall(self):
        """Only a strictly higher granted level counts as cover; at the same
        level the scope has to be there by name."""
        chat = gs.service("chat")
        assert gs.missing_scopes(list(chat.full), [chat.full[0]]) == [chat.full[1]]

    def test_a_full_grant_still_covers_a_multi_scope_readonly_request(self):
        chat = gs.service("chat")
        assert gs.missing_scopes(list(chat.readonly), list(chat.full)) == []

    def test_a_complete_multi_scope_grant_reports_nothing(self):
        chat = gs.service("chat")
        assert gs.missing_scopes(list(chat.full), list(chat.full)) == []

    def test_nothing_missing(self):
        assert gs.missing_scopes(READONLY_FIVE, READONLY_FIVE) == []

    def test_extra_grant_is_not_missing(self):
        assert gs.missing_scopes([DRIVE_RO], READONLY_FIVE) == []


@pytest.mark.parametrize("level", ["off", "readonly", "full"])
def test_levels_constant_matches_the_names(level):
    assert level in gs.LEVELS
