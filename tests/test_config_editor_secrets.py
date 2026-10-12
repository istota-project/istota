"""`config_editor.secrets`: one predicate decides every secret leaf, and a
redacted document carries no credential value anywhere."""

from __future__ import annotations

import pytest

from istota.config_editor import secrets
from istota.config_editor.reference import credential_files
from istota.lib import toml_write
from istota.webui import admin_config_view


#: Keys that reach the config the way credentials do (an env override, and for
#: the two Telnyx ones a stack secrets file) but are identifiers or a public
#: key, which the admin view shows. Literal, so a new credential whose name the
#: patterns miss fails here rather than joining this list unseen.
NOT_SECRET_VALUES = frozenset({
    "sms.twilio.account_sid",
    "sms.twilio.messaging_service_sid",
    "sms.telnyx.public_key",
    "sms.telnyx.messaging_profile_id",
})
SECRET_CREDENTIALS = sorted(set(credential_files()) - NOT_SECRET_VALUES)


class TestIsSecretPath:
    def test_it_is_the_views_predicate(self):
        assert secrets.is_secret_path is admin_config_view.is_secret_path

    def test_a_header_with_a_harmless_name_under_extra_headers_is_secret(self):
        assert secrets.is_secret_path(["brain", "native", "extra_headers", "x-tenant"])

    @pytest.mark.parametrize("dotted", SECRET_CREDENTIALS)
    def test_every_credential_field_is_secret(self, dotted):
        assert secrets.is_secret_path(dotted.split("."))

    @pytest.mark.parametrize("dotted", sorted(NOT_SECRET_VALUES))
    def test_the_identifiers_routed_like_credentials_are_not(self, dotted):
        assert not secrets.is_secret_path(dotted.split("."))

    def test_a_non_secret_keys_field_is_not(self):
        assert "web.token_storage" in admin_config_view.NON_SECRET_KEYS
        assert not secrets.is_secret_path(["web", "token_storage"])

    def test_an_operational_field_is_not(self):
        assert not secrets.is_secret_path(["scheduler", "poll_interval"])


DOCUMENT = {
    "bot_name": "Istota",
    "email": {"imap_host": "imap.example.org", "imap_password": "imap-pw-value"},
    "brain": {"native": {"extra_headers": {"x-tenant": "tenant-header-value"}}},
    "scheduler": {"poll_interval": 5},
}


class TestRedactDocument:
    def test_secret_leaves_become_the_sentinel_and_the_rest_is_kept(self):
        redacted = secrets.redact_document(DOCUMENT)
        assert redacted == {
            "bot_name": "Istota",
            "email": {"imap_host": "imap.example.org", "imap_password": secrets.SENTINEL},
            "brain": {"native": {"extra_headers": {"x-tenant": secrets.SENTINEL}}},
            "scheduler": {"poll_interval": 5},
        }

    def test_the_input_is_not_modified(self):
        before = repr(DOCUMENT)
        secrets.redact_document(DOCUMENT)
        assert repr(DOCUMENT) == before

    def test_a_list_of_tables_is_walked(self):
        document = {"briefing_shared_blocks": [{"name": "news", "api_token": "listed-token-value"}]}
        assert secrets.redact_document(document) == {
            "briefing_shared_blocks": [{"name": "news", "api_token": secrets.SENTINEL}],
        }

    def test_a_document_holding_every_credential_renders_none_of_them(self):
        """Sentinel by value: every credential gets a distinct marker value,
        and none of those values survives into the rendered text."""
        document: dict = {"brain": {"native": {"extra_headers": {"authorization": "marker-header-0"}}}}
        values = ["marker-header-0"]
        for index, dotted in enumerate(SECRET_CREDENTIALS):
            *parents, leaf = dotted.split(".")
            node = document
            for part in parents:
                node = node.setdefault(part, {})
            value = f"marker-credential-{index}"
            node[leaf] = value
            values.append(value)
        rendered = toml_write.dumps(secrets.redact_document(document))
        assert [value for value in values if value in rendered] == []
        assert rendered.count(secrets.SENTINEL) == len(values)


class TestRestoreSentinels:
    def test_a_sentinel_takes_the_current_value_back(self):
        new = secrets.redact_document(DOCUMENT)
        new["email"]["imap_host"] = "imap.example.net"
        restored = secrets.restore_sentinels(new, DOCUMENT)
        assert restored["email"] == {"imap_host": "imap.example.net", "imap_password": "imap-pw-value"}
        assert restored["brain"]["native"]["extra_headers"] == {"x-tenant": "tenant-header-value"}

    def test_a_sentinel_with_no_stored_value_is_refused(self):
        new = {"email": {"smtp_password": secrets.SENTINEL}}
        with pytest.raises(secrets.SentinelError) as caught:
            secrets.restore_sentinels(new, DOCUMENT)
        assert str(caught.value) == "no stored value to keep at email.smtp_password"

    def test_a_label_quotes_a_segment_holding_a_dot(self):
        new = {"brain": {"native": {"extra_headers": {"x.tenant": secrets.SENTINEL}}}}
        with pytest.raises(secrets.SentinelError, match=r'brain\.native\.extra_headers\."x\.tenant"'):
            secrets.restore_sentinels(new, {})


class TestExtractInlineSecrets:
    def test_a_new_value_is_moved_out_and_the_sentinel_left_behind(self):
        new = secrets.redact_document(DOCUMENT)
        new["email"]["smtp_password"] = "smtp-new-value"
        new["brain"]["native"]["extra_headers"]["x-other"] = "other-header-value"
        stripped, found = secrets.extract_inline_secrets(new)
        assert found == {
            "email.smtp_password": "smtp-new-value",
            "brain.native.extra_headers.x-other": "other-header-value",
        }
        assert stripped["email"]["smtp_password"] == secrets.SENTINEL
        assert stripped["brain"]["native"]["extra_headers"] == {
            "x-tenant": secrets.SENTINEL, "x-other": secrets.SENTINEL,
        }
        assert "smtp-new-value" not in toml_write.dumps(stripped)

    def test_a_document_of_sentinels_yields_nothing(self):
        _, found = secrets.extract_inline_secrets(secrets.redact_document(DOCUMENT))
        assert found == {}

    def test_against_the_current_document_only_changes_are_taken(self):
        current = {"brain": {"native": {"extra_headers": {"X-Old": "same", "X-Changed": "before"}}}}
        new = {"brain": {"native": {"extra_headers": {"X-Old": "same", "X-Changed": "after", "X-Added": "v"}}}}
        stripped, found = secrets.extract_inline_secrets(new, current)
        assert found == {
            "brain.native.extra_headers.X-Changed": "after",
            "brain.native.extra_headers.X-Added": "v",
        }
        assert set(stripped["brain"]["native"]["extra_headers"].values()) == {secrets.SENTINEL}


class TestNewValuesAndLabels:
    def test_a_new_value_wins_over_the_stored_one(self):
        new = {"brain": {"native": {"extra_headers": {"X-Key": secrets.SENTINEL}}}}
        current = {"brain": {"native": {"extra_headers": {"X-Key": "old"}}}}
        restored = secrets.restore_sentinels(new, current, values={"brain.native.extra_headers.X-Key": "new"})
        assert restored["brain"]["native"]["extra_headers"]["X-Key"] == "new"

    @pytest.mark.parametrize("path", [
        ("email", "imap_password"),
        ("brain", "native", "extra_headers", "X-Key.With Dots"),
        ("brain", "native", "extra_headers", 'quote"d'),
    ])
    def test_a_label_parses_back_to_its_path(self, path):
        assert secrets.parse_label(secrets.label(path)) == path

    @pytest.mark.parametrize("text", ["", "a = 1\nb", "a.", '"a', "a b", "[a]"])
    def test_text_label_would_not_write_is_no_path(self, text):
        assert secrets.parse_label(text) is None
