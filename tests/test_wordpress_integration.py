"""Live smoke for the `wordpress` skill against a real WordPress site (spec §16.3).

Skipped unless all three are set:

    ISTOTA_WP_TEST_URL           the site's HTTPS address
    ISTOTA_WP_TEST_USER          the WordPress login
    ISTOTA_WP_TEST_APP_PASSWORD  an application password for that login

Optional:

    ISTOTA_WP_TEST_BLOG          a scratch subsite slug, when the site is a
                                 subdirectory multisite network
    ISTOTA_WP_TEST_PLUGIN        a harmless plugin (dir/file) to deactivate and
                                 reactivate network-wide; needs a super admin
                                 and ISTOTA_WP_TEST_BLOG's network (§14.3)
    ISTOTA_WP_TEST_ACF_TYPE      a post type with ACF fields in REST, and
    ISTOTA_WP_TEST_ACF_FIELD     a text field on it, for the ACF round trip
    ISTOTA_WP_TEST_OPTIONS_PAGE  an ACF options page slug, and
    ISTOTA_WP_TEST_OPTIONS_FIELD a text field on it, for the options round trip;
                                 needs the istota-connector plugin
    ISTOTA_WP_TEST_FIELDS_TYPE   a post type, and
    ISTOTA_WP_TEST_FIELDS_FIELD  a flexible content field on it with a layout
                                 holding a repeater of a text sub-field, for
                                 `fields get` / `fields edit`; needs
                                 istota-connector 0.2.0

Point these at a local development copy of a site, never at production: the
test writes. Make a local-only application password in wp-admin and revoke it
afterwards. A site on a local address works because its host is put in
`[wordpress] private_hosts` for the run; for a locally signed certificate,
export `SSL_CERT_FILE` at a bundle carrying that CA.

The CLI runs end to end through `main`, with the real client, resolver and
network. Only the vault is replaced, by a resolver answering with the three
variables, so no credential is stored anywhere. Everything is made under a
generated slug and removed again.

    uv run pytest -m integration tests/test_wordpress_integration.py -n0 -v
"""

from __future__ import annotations

import json
import os
import struct
import uuid
import zlib
from urllib.parse import urlsplit

import pytest

from istota import db
from istota.config import Config, WordPressConfig
from istota.skills import _credref
from istota.skills import wordpress as wp
from istota.untrusted import frame_untrusted

URL = os.environ.get("ISTOTA_WP_TEST_URL", "").strip()
USER = os.environ.get("ISTOTA_WP_TEST_USER", "").strip()
APP_PASSWORD = os.environ.get("ISTOTA_WP_TEST_APP_PASSWORD", "").strip()
BLOG = os.environ.get("ISTOTA_WP_TEST_BLOG", "").strip()
PLUGIN = os.environ.get("ISTOTA_WP_TEST_PLUGIN", "").strip()
ACF_TYPE = os.environ.get("ISTOTA_WP_TEST_ACF_TYPE", "").strip()
ACF_FIELD = os.environ.get("ISTOTA_WP_TEST_ACF_FIELD", "").strip()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (URL and USER and APP_PASSWORD),
        reason="ISTOTA_WP_TEST_URL, ISTOTA_WP_TEST_USER and ISTOTA_WP_TEST_APP_PASSWORD not set",
    ),
]

USER_ID = "smoke"


def tiny_png() -> bytes:
    """A real one-pixel PNG, so WordPress can make its thumbnails."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00\xff\x80\x00")
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels)
            + chunk(b"IEND", b""))


@pytest.fixture
def live(tmp_path, monkeypatch):
    host = urlsplit(URL if "://" in URL else f"https://{URL}").hostname or ""
    mount = tmp_path / "mount"
    workspace = mount / "Users" / USER_ID
    config_dir = workspace / "istota" / "config"
    config_dir.mkdir(parents=True)
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    config = Config(workspace_path=mount, db_path=db_path,
                    wordpress=WordPressConfig(private_hosts=[host]))
    monkeypatch.setenv("ISTOTA_USER_ID", USER_ID)
    monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(mount))
    monkeypatch.delenv("ISTOTA_CRED_FD", raising=False)
    monkeypatch.setattr(wp, "_load_config", lambda: config)

    def fetch_entry(name, mode, *, credential_fd=None):
        assert name == "wordpress_test"
        return {"password": APP_PASSWORD, "username": USER, "url": URL}, [host]

    monkeypatch.setattr(_credref, "fetch_entry", fetch_entry)
    monkeypatch.setattr(wp, "_list_entries", lambda: ["wordpress_test"])
    return workspace


def cli(capsys, *argv, scoped: bool = True) -> tuple[int, dict]:
    if scoped and BLOG:
        argv = (*argv, "--blog", BLOG)
    code = 0
    try:
        wp.main(list(argv))
    except SystemExit as exc:
        code = exc.code or 0
    out = json.loads(capsys.readouterr().out)
    assert APP_PASSWORD not in json.dumps(out)
    return code, out


def ok(capsys, *argv) -> dict:
    code, out = cli(capsys, *argv)
    assert code == 0 and out.get("status") == "ok", out
    return out


def test_sites_reads_no_network(live, capsys):
    code, out = cli(capsys, "sites", scoped=False)
    assert code == 0 and out["sites"][0]["name"] == "test"


def test_describe(live, capsys):
    out = ok(capsys, "describe", "--refresh")
    assert "post" in {t["slug"] for t in out["types"]}
    assert out["account"]["roles"]


def test_a_draft_round_trip_with_a_term_and_an_image(live, capsys):
    """Create, upload, attach, edit, publish, delete, and remove what was made."""
    tag = uuid.uuid4().hex[:8]
    slug = f"istota-smoke-{tag}"
    term = f"Istota smoke {tag}"
    image = live / f"smoke-{tag}.png"
    image.write_bytes(tiny_png())
    post_id = media_id = None
    try:
        out = ok(capsys, "create", "--type", "post", "--title", f"Smoke {tag}",
                 "--slug", slug, "--content", "<!-- wp:paragraph --><p>x</p><!-- /wp:paragraph -->",
                 "--terms", f"category={term}", "--create-terms", "--confirmed")
        post_id = out["item"]["id"]
        assert out["readback"]["dropped"] == [] and out["readback"]["changed"] == []

        # §14.4: whether the upload alone keeps alt text and caption.
        out = ok(capsys, "media", "upload", "--file", str(image), "--alt", "A square",
                 "--caption", "Smoke caption")
        media_id = out["item"]["id"]
        with capsys.disabled():
            print("metadata_kept:", out.get("metadata_kept"), "error:", out.get("metadata_error"))
        assert "A square" in out["item"]["alt_text"]

        out = ok(capsys, "update", "--id", str(post_id), "--title", f"Smoke {tag} edited",
                 "--featured-media-id", str(media_id))
        assert out["readback"]["changed"] == []

        out = ok(capsys, "get", "--id", str(post_id), "--fields", "title,content,featured_media")
        assert f"Smoke {tag} edited" in out["item"]["title"]
        assert out["item"]["featured_media"] == media_id
        assert "<!-- wp:paragraph -->" in out["item"]["content"]

        code, out = cli(capsys, "publish", "--id", str(post_id))
        assert out["reason"] == "confirmation_required"
        ok(capsys, "publish", "--id", str(post_id), "--confirmed")
    finally:
        if post_id is not None:
            cli(capsys, "delete", "--id", str(post_id), "--force", "--confirmed")
        if media_id is not None:
            cli(capsys, "rest", "DELETE", f"wp/v2/media/{media_id}", "--query", "force=true",
                "--confirmed")
        code, found = cli(capsys, "terms", "list", "--taxonomy", "category", "--search", term)
        for item in found.get("items", []) if code == 0 else []:
            cli(capsys, "rest", "DELETE", f"wp/v2/categories/{item['id']}",
                "--query", "force=true", "--confirmed")


@pytest.mark.skipif(not (ACF_TYPE and ACF_FIELD),
                    reason="ISTOTA_WP_TEST_ACF_TYPE and ISTOTA_WP_TEST_ACF_FIELD not set")
def test_an_acf_field_round_trip(live, capsys):
    tag = uuid.uuid4().hex[:8]
    post_id = None
    try:
        out = ok(capsys, "create", "--type", ACF_TYPE, "--title", f"ACF smoke {tag}",
                 "--slug", f"istota-acf-smoke-{tag}",
                 "--acf-set", f"{ACF_FIELD}={json.dumps('first ' + tag)}")
        post_id = out["item"]["id"]
        out = ok(capsys, "update", "--id", str(post_id), "--type", ACF_TYPE,
                 "--acf-set", f"{ACF_FIELD}={json.dumps('second ' + tag)}")
        assert out["readback"]["changed"] == [], out["readback"]
        out = ok(capsys, "get", "--id", str(post_id), "--type", ACF_TYPE, "--fields", "acf")
        assert f"second {tag}" in json.dumps(out["item"]["acf"])
    finally:
        if post_id is not None:
            cli(capsys, "delete", "--id", str(post_id), "--type", ACF_TYPE, "--force",
                "--confirmed")


def test_admin_reads(live, capsys):
    ok(capsys, "users", "get", "--id", "me")
    ok(capsys, "settings", "get")
    ok(capsys, "rest", "GET", "wp/v2/types")
    code, out = cli(capsys, "plugins", "list")
    assert code == 0 or out["reason"] == "permission_denied", out


def test_a_setting_written_back_unchanged(live, capsys):
    current = ok(capsys, "settings", "get")["settings"]["posts_per_page"]
    out = ok(capsys, "settings", "update", "--set", f"posts_per_page={json.dumps(current)}",
             "--confirmed")
    assert out["readback"] == {"dropped": [], "changed": []}


def test_abilities_list(live, capsys):
    """§14.5: which abilities the site registers."""
    code, out = cli(capsys, "abilities", "list")
    assert code == 0 or out["reason"] == "unknown_route", out
    with capsys.disabled():
        print("abilities:", [item["name"] for item in out.get("items", [])])


@pytest.mark.skipif(not (PLUGIN and BLOG),
                    reason="ISTOTA_WP_TEST_PLUGIN and ISTOTA_WP_TEST_BLOG not set")
def test_network_plugin_deactivate_and_reactivate(live, capsys):
    """§14.3: does core REST network-activate? Main site, so no --blog.

    The plugin must start network-active, and it is left that way: if REST
    will not reactivate it, the test fails and says to do it by hand.
    """
    code, listed = cli(capsys, "plugins", "list", scoped=False)
    assert code == 0, listed
    status = {item["plugin"]: item["plugin_status"] for item in listed["items"]}
    assert status.get(PLUGIN) == "network-active", (
        f"{PLUGIN} must be network-active before this test; it is {status.get(PLUGIN)}")
    code, out = cli(capsys, "plugins", "deactivate", "--plugin", PLUGIN, "--network",
                    "--confirmed", scoped=False)
    assert code == 0, out
    code, out = cli(capsys, "plugins", "activate", "--plugin", PLUGIN, "--network",
                    "--confirmed", scoped=False)
    with capsys.disabled():
        print("network activation:", out.get("reason", "ok"))
    assert code == 0, (f"{PLUGIN} is now deactivated network-wide; network-activate it "
                       f"again in the network admin. The skill answered: {out}")


OPTIONS_PAGE = os.environ.get("ISTOTA_WP_TEST_OPTIONS_PAGE", "").strip()
OPTIONS_FIELD = os.environ.get("ISTOTA_WP_TEST_OPTIONS_FIELD", "").strip()


def unfenced(text: str) -> str:
    """The site's own text out of its fence, to write an original value back."""
    lines = text.split("\n")
    assert lines[0].startswith("[UNTRUSTED") and lines[-1].startswith("[END UNTRUSTED"), text
    return "\n".join(lines[1:-1])


@pytest.mark.skipif(not (OPTIONS_PAGE and OPTIONS_FIELD),
                    reason="ISTOTA_WP_TEST_OPTIONS_PAGE and ISTOTA_WP_TEST_OPTIONS_FIELD not set")
def test_an_options_page_round_trip(live, capsys):
    """§16.3 scenario 10: needs the istota-connector plugin on the site.

    Writes a scratch value into a text field of the options page and puts
    the original back, so the field must hold a string to begin with.
    """
    fields = ok(capsys, "options", "get", "--page", OPTIONS_PAGE)["fields"]
    assert OPTIONS_FIELD in fields, sorted(fields)
    original = fields[OPTIONS_FIELD]
    assert isinstance(original, str), original
    original = unfenced(original) if original else original
    scratch = f"istota smoke {uuid.uuid4().hex[:8]}"
    try:
        out = ok(capsys, "options", "update", "--page", OPTIONS_PAGE,
                 "--acf-set", f"{OPTIONS_FIELD}={json.dumps(scratch)}", "--confirmed")
        assert out["readback"]["changed"] == [] and out["readback"]["dropped"] == [], out
        again = ok(capsys, "options", "get", "--page", OPTIONS_PAGE)["fields"][OPTIONS_FIELD]
        assert scratch in again
    finally:
        cli(capsys, "options", "update", "--page", OPTIONS_PAGE,
            "--acf-set", f"{OPTIONS_FIELD}={json.dumps(original)}", "--confirmed")


@pytest.mark.skipif(not BLOG, reason="ISTOTA_WP_TEST_BLOG not set (not a network)")
def test_network_sites(live, capsys):
    """Needs the istota-connector plugin network-activated, and a super admin."""
    code, out = cli(capsys, "network", "sites")
    if out.get("reason") == "permission_denied":
        pytest.skip("network sites needs a super admin; the test account is not one")
    assert code == 0 and out.get("status") == "ok", out
    assert out["count"] >= 2 and any(site["id"] == 1 for site in out["sites"]), out


FIELDS_TYPE = os.environ.get("ISTOTA_WP_TEST_FIELDS_TYPE", "").strip()
FIELDS_FIELD = os.environ.get("ISTOTA_WP_TEST_FIELDS_FIELD", "").strip()
TEXTUAL = ("text", "textarea")


def plain(value):
    """A fenced string from `fields get` back to the site's own text."""
    return unfenced(value) if isinstance(value, str) and value else value


def pick_layouts(definition: dict) -> tuple[dict, dict, str, str, str]:
    """A layout holding a repeater of a text sub-field, and a layout with a
    text sub-field of its own (the same layout when it is the only one)."""
    listy = None
    for layout in definition["layouts"]:
        for sub in layout["sub_fields"]:
            if sub["type"] != "repeater":
                continue
            text = next((s["name"] for s in sub["sub_fields"] if s["type"] in TEXTUAL), None)
            if text:
                listy = (layout, sub["name"], text)
                break
        if listy:
            break
    assert listy, "no layout of the field has a repeater with a text sub-field"
    others = [lay for lay in definition["layouts"] if lay["name"] != listy[0]["name"]]
    for layout in [*others, listy[0]]:
        text = next((s["name"] for s in layout["sub_fields"] if s["type"] in TEXTUAL), None)
        if text:
            return listy[0], layout, listy[1], listy[2], text
    raise AssertionError("no layout of the field has a text sub-field of its own")


@pytest.mark.skipif(not (FIELDS_TYPE and FIELDS_FIELD),
                    reason="ISTOTA_WP_TEST_FIELDS_TYPE and ISTOTA_WP_TEST_FIELDS_FIELD not set")
def test_fields_edit_on_a_flexible_field(live, capsys):
    """`fields get` and `fields edit` on a scratch draft; needs istota-connector 0.2.0.

    ISTOTA_WP_TEST_FIELDS_TYPE is a post type whose field groups hold
    ISTOTA_WP_TEST_FIELDS_FIELD, a flexible content field in REST with one
    layout carrying a repeater of a text sub-field, such as `page` and a
    page-builder `blocks` field. The draft is deleted afterwards.
    """
    tag = uuid.uuid4().hex[:8]
    field = FIELDS_FIELD
    made = ok(capsys, "create", "--type", FIELDS_TYPE, "--title", f"istota fields smoke {tag}",
              "--slug", f"istota-fields-smoke-{tag}")
    pid = str(made["item"]["id"])
    edit = ["fields", "edit", "--id", pid]
    try:
        listing = ok(capsys, "fields", "get", "--id", pid)
        assert field in {row["name"] for row in listing["fields"]}, listing["fields"]
        read = ok(capsys, "fields", "get", "--id", pid, "--path", field)
        assert read["value"] == [] and read["token"].startswith("sha256:"), read
        listing_token = next(row["token"] for row in listing["fields"] if row["name"] == field)
        assert listing_token == read["token"]
        lay, other, rep, text, other_text = pick_layouts(read["definition"])
        rows = f"{field}/0/{rep}"

        # Insert on a draft is not gated. The second row is disabled, as an
        # editor might leave one, to see it survive edits of its sibling.
        first = {"acf_fc_layout": lay["name"], rep: [{text: "one"}, {text: "two"}]}
        second = {"acf_fc_layout": other["name"], other_text: "kept",
                  "acf_fc_layout_disabled": True}
        out = ok(capsys, *edit, "--token", read["token"],
                 "--insert", f"{field}/-={json.dumps(first)}",
                 "--insert", f"{field}/-={json.dumps(second)}")
        assert out["readback"]["changed"] == [], out["readback"]
        assert out["previous_token"] == read["token"] and out["token"] != read["token"]
        stale, token = read["token"], out["token"]
        row1 = ok(capsys, "fields", "get", "--id", pid, "--path", f"{field}/1")["value"]
        assert row1["acf_fc_layout_disabled"] is True, row1
        assert plain(row1[other_text]) == "kept", row1

        # A fenced value is unwrapped; ops apply in order, indices shifting.
        deux = json.dumps(frame_untrusted("deux", "WORDPRESS CONTENT"))
        out = ok(capsys, *edit, "--token", token,
                 "--set", f"{rows}/1/{text}={deux}",
                 "--insert", f"{rows}/-={json.dumps({text: 'three'})}",
                 "--move", f"{rows}/2={rows}/0")
        assert out["readback"]["changed"] == [], out["readback"]
        token = out["token"]
        got = ok(capsys, "fields", "get", "--id", pid, "--path", rows)["value"]
        assert [plain(r[text]) for r in got] == ["three", "one", "deux"], got

        # A set followed by a move of the same row: the readback follows it.
        out = ok(capsys, *edit, "--token", token,
                 "--set", f"{rows}/0/{text}={json.dumps('drei')}",
                 "--move", f"{rows}/0={rows}/2")
        assert out["readback"]["changed"] == [], out["readback"]
        assert out["changed"][0]["path"] == f"{rows}/2/{text}", out["changed"]
        assert plain(out["changed"][0]["value"]) == "drei", out["changed"]
        token = out["token"]
        got = ok(capsys, "fields", "get", "--id", pid, "--path", rows)
        assert [plain(r[text]) for r in got["value"]] == ["one", "deux", "drei"], got["value"]
        assert got["token"] == token

        # A stale token is refused before any write, and names the current one.
        code, out = cli(capsys, *edit, "--token", stale, "--set", f'{rows}/0/{text}="x"')
        assert code != 0 and out["reason"] == "stale_value" and out["token"] == token, out
        assert ok(capsys, "fields", "get", "--id", pid, "--path", field)["token"] == token

        # A whole row copied out of fenced `fields get` output goes back clean.
        got = ok(capsys, "fields", "get", "--id", pid, "--path", f"{field}/0")
        row = got["value"]
        row[rep][0][text] = frame_untrusted("eins", "WORDPRESS CONTENT")
        out = ok(capsys, *edit, "--token", got["token"], "--set", f"{field}/0={json.dumps(row)}")
        assert out["readback"]["changed"] == [], out["readback"]
        token = out["token"]
        got = ok(capsys, "fields", "get", "--id", pid, "--path", f"{field}/0")["value"]
        stored = [plain(r[text]) for r in got[rep]]
        assert stored == ["eins", "deux", "drei"], got
        assert not any("UNTRUSTED" in s for s in stored), stored

        # The disabled sibling came through every edit of row 0 untouched.
        row1 = ok(capsys, "fields", "get", "--id", pid, "--path", f"{field}/1")["value"]
        assert row1["acf_fc_layout_disabled"] is True and plain(row1[other_text]) == "kept", row1

        # A set that drops rows asks first, on a draft too, and writes nothing.
        shrink = ("--set", f"{rows}=[{json.dumps({text: 'only'})}]")
        code, out = cli(capsys, *edit, "--token", token, *shrink)
        assert out["reason"] == "confirmation_required", out
        assert any(f"{rows}: 3 rows → 1" in line for line in out["would"]), out["would"]
        assert ok(capsys, "fields", "get", "--id", pid, "--path", field)["token"] == token
        out = ok(capsys, *edit, "--token", token, *shrink, "--confirmed")
        assert len(out["previous"][0]) == 3, out["previous"]
        token = out["token"]

        # Removing a row asks first, and hands back what it removed.
        code, out = cli(capsys, *edit, "--token", token, "--remove", f"{field}/1")
        assert out["reason"] == "confirmation_required", out
        out = ok(capsys, *edit, "--token", token, "--remove", f"{field}/1", "--confirmed")
        assert out["previous"][0]["acf_fc_layout"] == other["name"], out["previous"]
        assert out["previous"][0]["acf_fc_layout_disabled"] is True
        got = ok(capsys, "fields", "get", "--id", pid, "--path", field)["value"]
        assert len(got) == 1 and [plain(r[text]) for r in got[0][rep]] == ["only"], got
    finally:
        cli(capsys, "delete", "--id", pid, "--type", FIELDS_TYPE, "--force", "--confirmed")
