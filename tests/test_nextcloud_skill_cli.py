"""Tests for the nextcloud skill CLI (python -m istota.skills.nextcloud).

The ``cmd_*`` handlers return their payload and ``main()`` serializes it once,
so the command-level tests drive ``main(argv)`` and read stdout — which is what
the model and the scheduler actually see.
"""

import json
from unittest.mock import patch

import pytest

from istota.nextcloud import OcsError
from istota.skills.nextcloud import build_parser, main


@pytest.fixture(autouse=True)
def _nc_env(monkeypatch):
    monkeypatch.setenv("NC_URL", "https://cloud.example.com")
    monkeypatch.setenv("NC_USER", "istota")
    monkeypatch.setenv("NC_PASS", "secret")
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")


def _run(capsys, argv):
    """Run main(argv), returning (parsed stdout, exit code)."""
    code = 0
    try:
        main(argv)
    except SystemExit as e:
        code = e.code
    out = capsys.readouterr().out
    return json.loads(out) if out.strip() else None, code


# --- parser back-compat: every legacy `share` invocation still parses ---


class TestBuildParser:
    def test_share_list_no_path(self):
        args = build_parser().parse_args(["share", "list"])
        assert args.group == "share"
        assert args.command == "list"
        assert args.path is None

    def test_share_list_with_path(self):
        args = build_parser().parse_args(["share", "list", "--path", "/Documents"])
        assert args.path == "/Documents"

    def test_share_create_user(self):
        args = build_parser().parse_args([
            "share", "create", "--path", "/test", "--type", "user",
            "--with", "bob", "--permissions", "31",
        ])
        assert args.path == "/test"
        assert args.type == "user"
        assert args.with_user == "bob"
        assert args.permissions == 31

    def test_share_create_link(self):
        args = build_parser().parse_args([
            "share", "create", "--path", "/test", "--type", "link",
            "--password", "pw", "--expire", "2026-06-01", "--label", "test",
        ])
        assert args.type == "link"
        assert args.password == "pw"
        assert args.expire == "2026-06-01"
        assert args.label == "test"

    def test_share_delete(self):
        args = build_parser().parse_args(["share", "delete", "42"])
        assert args.command == "delete"
        assert args.share_id == 42

    def test_share_search(self):
        args = build_parser().parse_args(["share", "search", "bob"])
        assert args.command == "search"
        assert args.query == "bob"
        assert args.item_type == "file"

    def test_share_search_custom_item_type(self):
        args = build_parser().parse_args(["share", "search", "alice", "--item-type", "folder"])
        assert args.item_type == "folder"

    def test_new_share_types_accepted(self):
        for kind in ("group", "federated", "talk"):
            args = build_parser().parse_args([
                "share", "create", "--path", "/t", "--type", kind, "--with", "x",
            ])
            assert args.type == kind

    def test_capabilities_flags(self):
        args = build_parser().parse_args(["capabilities", "--check", "talk,sharing.public"])
        assert args.group == "capabilities"
        assert args.check == "talk,sharing.public"


# --- share verbs through main() ---


class TestShareCommands:
    @patch("istota.skills.nextcloud.ocs_list_shares")
    def test_list_success(self, mock_list, capsys):
        mock_list.return_value = [{"id": 1, "path": "/test"}]
        out, code = _run(capsys, ["share", "list"])
        assert code == 0
        assert out == [{"id": 1, "path": "/test"}]

    @patch("istota.skills.nextcloud.ocs_list_shares")
    def test_list_with_path_filter(self, mock_list, capsys):
        mock_list.return_value = []
        _run(capsys, ["share", "list", "--path", "/Documents"])
        assert mock_list.call_args.kwargs["path"] == "/Documents"

    @patch("istota.skills.nextcloud.ocs_list_shares")
    def test_list_failure_is_an_envelope(self, mock_list, capsys):
        mock_list.return_value = None
        out, code = _run(capsys, ["share", "list"])
        assert code == 1
        assert out["status"] == "error"
        assert "endpoint" in out

    @patch("istota.skills.nextcloud.ocs_create_share")
    def test_create_user_share(self, mock_create, capsys):
        mock_create.return_value = {"id": 42, "share_type": 0}
        out, code = _run(
            capsys,
            ["share", "create", "--path", "/test", "--type", "user",
             "--with", "bob", "--permissions", "31"],
        )
        assert code == 0
        assert out["id"] == 42
        kw = mock_create.call_args.kwargs
        assert kw["share_type"] == 0
        assert kw["share_with"] == "bob"
        assert kw["permissions"] == 31

    @patch("istota.skills.nextcloud.ocs_create_share")
    def test_create_group_share_uses_type_1(self, mock_create, capsys):
        mock_create.return_value = {"id": 7}
        _run(capsys, ["share", "create", "--path", "/t", "--type", "group", "--with", "team"])
        assert mock_create.call_args.kwargs["share_type"] == 1

    @patch("istota.skills.nextcloud.ocs_create_public_link")
    def test_create_link_share(self, mock_link, capsys):
        mock_link.return_value = {"id": 99, "url": "https://nc.example.com/s/abc"}
        out, code = _run(capsys, ["share", "create", "--path", "/test", "--type", "link"])
        assert code == 0
        assert "url" in out

    @patch("istota.skills.nextcloud.ocs_create_public_link")
    def test_create_link_with_options(self, mock_link, capsys):
        mock_link.return_value = {"id": 100, "url": "https://nc.example.com/s/xyz"}
        _run(capsys, [
            "share", "create", "--path", "/test", "--type", "link",
            "--password", "pw", "--expire", "2026-06-01", "--label", "test",
        ])
        kw = mock_link.call_args.kwargs
        assert kw["password"] == "pw"
        assert kw["expire_date"] == "2026-06-01"
        assert kw["label"] == "test"

    def test_create_user_share_without_with_fails(self, capsys):
        out, code = _run(capsys, ["share", "create", "--path", "/test", "--type", "user"])
        assert code == 1
        assert out["status"] == "error"
        assert "--with" in out["error"]

    @patch("istota.skills.nextcloud.ocs_delete_share")
    def test_delete_success(self, mock_delete, capsys):
        mock_delete.return_value = True
        out, code = _run(capsys, ["share", "delete", "42"])
        assert code == 0
        assert out == {"status": "deleted", "share_id": 42}

    @patch("istota.skills.nextcloud.ocs_delete_share")
    def test_delete_failure(self, mock_delete, capsys):
        mock_delete.return_value = False
        out, code = _run(capsys, ["share", "delete", "999"])
        assert code == 1
        assert out["status"] == "error"

    @patch("istota.skills.nextcloud.ocs_search_sharees")
    def test_search_success(self, mock_search, capsys):
        mock_search.return_value = {
            "exact": {"users": [{"label": "Bob", "value": {"shareWith": "bob"}}]},
            "users": [],
        }
        out, code = _run(capsys, ["share", "search", "bob"])
        assert code == 0
        assert out["exact"]["users"][0]["label"] == "Bob"

    @patch("istota.skills.nextcloud.ocs_search_sharees")
    def test_search_failure(self, mock_search, capsys):
        mock_search.return_value = None
        out, code = _run(capsys, ["share", "search", "nobody"])
        assert code == 1


# --- error envelope shape ---


class TestErrorEnvelope:
    @patch("istota.skills.nextcloud.ocs_list_shares")
    def test_ocs_error_carries_all_fields(self, mock_list, capsys):
        mock_list.side_effect = OcsError("Forbidden", 200, 997, "/shares")
        out, code = _run(capsys, ["share", "list"])
        assert code == 1
        assert out == {
            "status": "error",
            "error": "Forbidden",
            "http_status": 200,
            "ocs_status": 997,
            "endpoint": "/shares",
        }

    def test_no_group_prints_help_and_exits(self, capsys):
        with pytest.raises(SystemExit):
            main([])


# --- capabilities ---


CAPS_PAYLOAD = {
    "version": {"string": "31.0.2", "edition": "", "extendedSupport": False},
    "capabilities": {
        "core": {"webdav-root": "remote.php/webdav"},
        "files_sharing": {
            "api_enabled": True,
            "resharing": True,
            "public": {
                "enabled": True,
                "password": {"enforced": True},
                "expire_date": {"enabled": True, "days": 30, "enforce": True},
            },
            "federation": {"outgoing": True, "incoming": False},
            "sharebymail": {"enabled": True},
        },
        "files": {"versioning": True, "undelete": True},
        "dav": {"chunking": "1.0"},
        "spreed": {"features": ["chat-v2", "rich-object-sharing"]},
        "notifications": {"ocs-endpoints": ["list", "delete", "delete-all"]},
        "activity": {"apiv2": ["filters"]},
    },
}

ACCOUNT_PAYLOAD = {
    "id": "istota",
    "display-name": "Istota Bot",
    "email": "bot@example.com",
    "groups": ["bots"],
    "quota": {"free": 1000, "used": 20, "total": 1020},
}


class TestCapabilities:
    @patch("istota.nextcloud.capabilities.ocs_get")
    def test_summary(self, mock_get, capsys):
        mock_get.side_effect = [CAPS_PAYLOAD, ACCOUNT_PAYLOAD]
        out, code = _run(capsys, ["capabilities"])
        assert code == 0
        assert out["server"]["version"] == "31.0.2"
        assert out["sharing"]["public_enabled"] is True
        assert out["sharing"]["public_expire_date_days"] == 30
        assert out["talk"]["available"] is True
        assert "chat-v2" in out["talk"]["features"]
        assert out["files"]["versioning"] is True
        assert out["files"]["chunking"] == "1.0"
        assert out["account"]["display_name"] == "Istota Bot"
        assert out["features"]["sharing.public"] is True

    @patch("istota.nextcloud.capabilities.ocs_get")
    def test_summary_survives_account_failure(self, mock_get, capsys):
        mock_get.side_effect = [CAPS_PAYLOAD, OcsError("no", 403, 997, "/cloud/user")]
        out, code = _run(capsys, ["capabilities"])
        assert code == 0
        assert out["account"]["id"] == ""
        assert out["server"]["version"] == "31.0.2"

    @patch("istota.nextcloud.capabilities.ocs_get")
    def test_raw_passthrough(self, mock_get, capsys):
        mock_get.return_value = CAPS_PAYLOAD
        out, code = _run(capsys, ["capabilities", "--raw"])
        assert code == 0
        assert out == CAPS_PAYLOAD

    @patch("istota.nextcloud.capabilities.ocs_get")
    def test_check_all_present_exits_zero(self, mock_get, capsys):
        mock_get.return_value = CAPS_PAYLOAD
        out, code = _run(capsys, ["capabilities", "--check", "talk,sharing.public,files.versioning"])
        assert code == 0
        assert out["status"] == "ok"
        assert out["missing"] == []

    @patch("istota.nextcloud.capabilities.ocs_get")
    def test_check_missing_exits_nonzero(self, mock_get, capsys):
        payload = {"version": {}, "capabilities": {"files_sharing": {"api_enabled": True}}}
        mock_get.return_value = payload
        out, code = _run(capsys, ["capabilities", "--check", "talk,sharing.api"])
        assert code == 1
        assert out["missing"] == ["talk"]
        assert out["checks"]["sharing.api"] is True

    @patch("istota.nextcloud.capabilities.ocs_get")
    def test_unknown_feature_name_is_missing_not_a_crash(self, mock_get, capsys):
        mock_get.return_value = CAPS_PAYLOAD
        out, code = _run(capsys, ["capabilities", "--check", "deck"])
        assert code == 1
        assert out["missing"] == ["deck"]
        assert "known" in out


class TestFeatureMap:
    def test_empty_payload_is_all_false(self):
        from istota.nextcloud.capabilities import feature_map

        assert all(v is False for v in feature_map({}).values())

    def test_enforced_expiry_limit(self):
        from istota.nextcloud.capabilities import public_link_expiry_limit

        assert public_link_expiry_limit(CAPS_PAYLOAD) == 30

    def test_no_limit_when_not_enforced(self):
        from istota.nextcloud.capabilities import public_link_expiry_limit

        payload = {
            "capabilities": {
                "files_sharing": {"public": {"expire_date": {"enabled": True, "days": 30}}}
            }
        }
        assert public_link_expiry_limit(payload) is None

    def test_require_raises_for_absent_feature(self, monkeypatch):
        from istota.nextcloud import capabilities as caps_mod

        with pytest.raises(OcsError) as exc:
            caps_mod.require(None, "talk", capabilities={"capabilities": {}})
        assert "talk" in str(exc.value)

    def test_require_passes_for_present_feature(self):
        from istota.nextcloud import capabilities as caps_mod

        caps_mod.require(None, "talk", capabilities=CAPS_PAYLOAD)


# --- user / group ---


class TestUserGroup:
    @patch("istota.nextcloud.users.ocs_get")
    def test_whoami(self, mock_get, capsys):
        mock_get.return_value = ACCOUNT_PAYLOAD
        out, code = _run(capsys, ["user", "whoami"])
        assert code == 0
        assert out["id"] == "istota"

    @patch("istota.nextcloud.users.ocs_get")
    def test_search_maps_types_to_share_types(self, mock_get, capsys):
        mock_get.return_value = [{"id": "bob", "label": "Bob"}]
        out, code = _run(capsys, ["user", "search", "bob", "--types", "users,talk", "--limit", "5"])
        assert code == 0
        assert out["count"] == 1
        params = mock_get.call_args.kwargs["params"]
        assert params["shareTypes[]"] == ["0", "10"]
        assert params["limit"] == "5"
        assert params["search"] == "bob"

    @patch("istota.nextcloud.users.ocs_get")
    def test_search_defaults_to_users_and_groups(self, mock_get, capsys):
        mock_get.return_value = []
        _run(capsys, ["user", "search", "x"])
        assert mock_get.call_args.kwargs["params"]["shareTypes[]"] == ["0", "1"]

    def test_search_rejects_unknown_type(self, capsys):
        out, code = _run(capsys, ["user", "search", "x", "--types", "wombats"])
        assert code == 1
        assert "wombats" in out["error"]

    @patch("istota.nextcloud.users.ocs_get")
    def test_admin_gated_verb_names_the_alternative(self, mock_get, capsys):
        mock_get.side_effect = OcsError("Unauthorised", 200, 997, "/cloud/users/bob")
        out, code = _run(capsys, ["user", "get", "bob"])
        assert code == 1
        assert out["ocs_status"] == 997
        assert "admin rights" in out["error"]
        assert "user search" in out["error"]

    @patch("istota.nextcloud.users.ocs_get")
    def test_non_permission_error_is_not_reshaped(self, mock_get, capsys):
        mock_get.side_effect = OcsError("Server error", 500, 996, "/cloud/users/bob")
        out, code = _run(capsys, ["user", "get", "bob"])
        assert code == 1
        assert out["error"] == "Server error"

    @patch("istota.nextcloud.users.ocs_get")
    def test_user_groups_defaults_to_the_bot(self, mock_get, capsys):
        mock_get.side_effect = [ACCOUNT_PAYLOAD, {"groups": ["bots"]}]
        out, code = _run(capsys, ["user", "groups"])
        assert code == 0
        assert out == {"user": "istota", "groups": ["bots"]}

    @patch("istota.nextcloud.users.ocs_get")
    def test_group_list(self, mock_get, capsys):
        mock_get.return_value = {"groups": ["admin", "team"]}
        out, code = _run(capsys, ["group", "list"])
        assert code == 0
        assert out == {"groups": ["admin", "team"]}

    @patch("istota.nextcloud.users.ocs_get")
    def test_group_list_search_param(self, mock_get, capsys):
        mock_get.return_value = {"groups": []}
        _run(capsys, ["group", "list", "--search", "te"])
        assert mock_get.call_args.kwargs["params"] == {"search": "te"}

    @patch("istota.nextcloud.users.ocs_get")
    def test_group_members(self, mock_get, capsys):
        mock_get.return_value = {"users": ["alice", "bob"]}
        out, code = _run(capsys, ["group", "members", "team"])
        assert code == 0
        assert out == {"group": "team", "members": ["alice", "bob"]}


# --- env config ---


class TestEnvVarConfig:
    def test_missing_env_vars_exits(self, monkeypatch):
        monkeypatch.delenv("NC_URL")
        from istota.skills.nextcloud import _config_from_env
        with pytest.raises(SystemExit):
            _config_from_env()

    def test_valid_env_vars(self):
        from istota.skills.nextcloud import _config_from_env
        config = _config_from_env()
        assert config.nextcloud.url == "https://cloud.example.com"
        assert config.nextcloud.username == "istota"
        assert config.nextcloud.app_password == "secret"


# --- live-suite coverage ---


class TestLiveCoverage:
    """Every CLI verb must be driven by the live suite, or excused by name.

    The live suite only runs with credentials present, so this guard lives here
    instead — it is what stops a newly added verb from shipping with mocked
    coverage alone.
    """

    def test_every_verb_is_exercised_or_excused(self):
        from pathlib import Path

        from istota.skills.nextcloud import _COMMANDS
        from tests.test_nextcloud_skill_live import NOT_EXERCISED_LIVE

        source = Path(__file__).with_name("test_nextcloud_skill_live.py").read_text()
        unexercised = []
        for group, command in _COMMANDS:
            if (group, command) in NOT_EXERCISED_LIVE:
                continue
            needle = f'"{group}", "{command}"' if command else f'"{group}"'
            if needle not in source:
                unexercised.append(f"{group} {command or ''}".strip())

        assert not unexercised, (
            "CLI verbs with no live test: "
            + ", ".join(sorted(unexercised))
            + " — add one, or list it in NOT_EXERCISED_LIVE with a reason."
        )
