"""The `wordpress` skill's stage 3: media uploads, ACF writes, `terms create`.

Driven through `main` with the fake site, vault and mount of
`tests/test_skills_wordpress.py`, whose fixtures this module reuses. What is
asserted is what reached the wire: which requests, in which order, with which
bodies, and that a refused path sends nothing at all.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest

from tests import test_skills_wordpress as base
from tests.test_skills_wordpress import HOST, Posts, body_of, run

# The fake site, vault and mount, shared with the stage 1 and 2 tests.
env = base.env
posts = base.posts


def writes(site) -> list[httpx.Request]:
    """What changed something: every request but the reads (GET, OPTIONS)."""
    return [r for r in site.requests if r.method not in ("GET", "OPTIONS")]

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 40

UPDATE_SCHEMA = {
    "schema": {
        "properties": {
            "title": {"type": "object"},
            "acf": {
                "type": "object",
                "properties": {
                    "blocks": {"type": ["array", "null"]},
                    "hero": {"type": ["integer", "null"]},
                    "layout": {"type": ["string", "null"]},
                    "intro": {"type": ["string", "null"]},
                },
            },
        },
    },
}


class Media:
    """The media collection: uploads stored, metadata kept or not, as asked."""

    def __init__(self, site, *, keep_meta: bool = True):
        self.site = site
        self.keep_meta = keep_meta
        self.items: dict[int, dict] = {}
        self.next_id = 900
        site.routes[("POST", "/wp-json/wp/v2/media")] = self.create

    def create(self, request: httpx.Request) -> httpx.Response:
        name = re.search(r'filename="([^"]*)"', request.headers["content-disposition"]).group(1)
        media_id = self.next_id
        self.next_id += 1
        item = {
            "id": media_id, "slug": name.rsplit(".", 1)[0], "post": None,
            "mime_type": request.headers["content-type"], "media_type": "image",
            "title": {"raw": name.rsplit(".", 1)[0]}, "alt_text": "", "caption": {"raw": ""},
            "source_url": f"https://{HOST}/wp-content/uploads/{name}",
            "bytes": len(request.content),
        }
        if self.keep_meta:
            params = request.url.params
            if "title" in params:
                item["title"] = {"raw": params["title"]}
            if "alt_text" in params:
                item["alt_text"] = params["alt_text"]
            if "caption" in params:
                item["caption"] = {"raw": params["caption"]}
        self.items[media_id] = item
        path = f"/wp-json/wp/v2/media/{media_id}"
        self.site.routes[("GET", path)] = lambda r: httpx.Response(200, json=self.items[media_id])
        self.site.routes[("POST", path)] = lambda r: self.update(media_id, r)
        return httpx.Response(201, json=item)

    def update(self, media_id: int, request: httpx.Request) -> httpx.Response:
        item = self.items[media_id]
        for key, value in json.loads(request.content).items():
            item[key] = {"raw": value} if key in ("title", "caption") else value
        return httpx.Response(200, json=item)


@pytest.fixture
def media(env):
    return Media(env.site)


@pytest.fixture
def updates(env):
    env.site.routes[("OPTIONS", "/wp-json/wp/v2/updates")] = UPDATE_SCHEMA
    env.site.routes[("OPTIONS", "/wp-json/wp/v2/posts")] = {
        "schema": {"properties": {"title": {"type": "object"}}}}
    return Posts(env.site, base="/wp-json/wp/v2/updates", type_="update")


def own(env, name: str, data: bytes = PNG):
    path = env.mount / "Users" / "alice" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def media_posts(site) -> list[httpx.Request]:
    return [r for r in site.requests if r.method == "POST" and r.url.path == "/wp-json/wp/v2/media"]


# ---------------------------------------------------------------------------
# media upload
# ---------------------------------------------------------------------------


class TestMediaUpload:
    def test_an_image_goes_as_its_sniffed_type_with_its_metadata(self, env, media, capsys):
        path = own(env, "photo.png")
        code, out = run(["media", "upload", "--file", str(path), "--alt", "A cat",
                         "--caption", "On a mat"], capsys)
        assert code == 0, out
        [upload] = writes(env.site)
        assert upload.headers["content-type"] == "image/png"
        assert upload.headers["content-disposition"] == 'attachment; filename="photo.png"'
        assert upload.content == PNG
        assert upload.url.params["alt_text"] == "A cat"
        assert out["item"]["id"] == 900
        assert out["metadata_kept"] == ["alt_text", "caption"]

    def test_metadata_the_create_did_not_keep_is_set_by_a_follow_up(self, env, capsys):
        Media(env.site, keep_meta=False)
        path = own(env, "photo.png")
        code, out = run(["media", "upload", "--file", str(path), "--alt", "A cat",
                         "--title", "Cat"], capsys)
        assert code == 0, out
        create, follow_up = writes(env.site)
        assert follow_up.url.path == "/wp-json/wp/v2/media/900"
        assert body_of(follow_up) == {"alt_text": "A cat", "title": "Cat"}
        assert out["metadata_kept"] == []
        assert "A cat" in out["item"]["alt_text"]

    def test_a_non_image_goes_as_octet_stream(self, env, media, capsys):
        path = own(env, "notes.pdf", b"%PDF-1.7 hello")
        code, out = run(["media", "upload", "--file", str(path)], capsys)
        assert code == 0, out
        assert writes(env.site)[0].headers["content-type"] == "application/octet-stream"

    def test_a_file_named_like_an_image_that_is_not_one_spends_nothing(self, env, media, capsys):
        path = own(env, "fake.jpg", b"<svg onload=x>")
        code, out = run(["media", "upload", "--file", str(path)], capsys)
        assert code == 1 and out["reason"] == "validation_error"
        assert env.fetches == [] and env.site.requests == []

    def test_a_file_over_the_cap_spends_nothing(self, env, media, capsys):
        env.config.wordpress.max_upload_mb = 1
        path = own(env, "big.png", PNG + b"\x00" * (1024 * 1024))
        code, out = run(["media", "upload", "--file", str(path)], capsys)
        assert code == 1 and out["reason"] == "validation_error"
        assert "max_upload_mb" in out["error"]
        assert env.fetches == [] and env.site.requests == []

    def test_a_file_outside_the_workspace_is_refused_first(self, env, media, capsys, tmp_path):
        outside = tmp_path / "secret.png"
        outside.write_bytes(PNG)
        code, out = run(["media", "upload", "--file", str(outside)], capsys)
        assert out["reason"] == "host_path_refused"
        assert env.fetches == [] and env.site.requests == []

    def test_an_ambiguous_upload_is_sent_once_and_names_the_lookup(self, env, capsys):
        env.site.routes[("POST", "/wp-json/wp/v2/media")] = httpx.Response(502)
        path = own(env, "photo.png")
        code, out = run(["media", "upload", "--file", str(path)], capsys)
        assert code == 1 and out["reason"] == "outcome_unknown"
        assert len(writes(env.site)) == 1
        assert out["lookup"] == "media list --search photo --site blog"

    def test_a_refused_file_type_is_a_refusal_not_an_unknown(self, env, capsys):
        env.site.routes[("POST", "/wp-json/wp/v2/media")] = httpx.Response(
            500, json={"code": "rest_upload_sideload_error",
                       "message": "Sorry, you are not allowed to upload this file type."})
        path = own(env, "tool.exe", b"MZ\x90\x00")
        code, out = run(["media", "upload", "--file", str(path)], capsys)
        assert code == 1 and out["reason"] == "request_refused"
        assert len(writes(env.site)) == 1

    def test_a_hostile_file_name_cannot_break_the_header(self, env, media, capsys):
        path = own(env, 'a";b=c\r.png')
        code, out = run(["media", "upload", "--file", str(path)], capsys)
        assert code == 0, out
        header = writes(env.site)[0].headers["content-disposition"]
        assert header == 'attachment; filename="a_b_c.png"'


class TestMediaUpdate:
    def test_it_sets_the_fields_and_reads_them_back(self, env, media, capsys):
        media.items[900] = {"id": 900, "title": {"raw": "x"}, "alt_text": "",
                            "caption": {"raw": ""}}
        media.site.routes[("GET", "/wp-json/wp/v2/media/900")] = (
            lambda r: httpx.Response(200, json=media.items[900]))
        media.site.routes[("POST", "/wp-json/wp/v2/media/900")] = (
            lambda r: media.update(900, r))
        code, out = run(["media", "update", "--id", "900", "--alt", "New alt"], capsys)
        assert code == 0, out
        [write] = writes(env.site)
        assert body_of(write) == {"alt_text": "New alt"}
        assert out["readback"]["changed"] == []

    def test_nothing_to_update_spends_nothing(self, env, capsys):
        code, out = run(["media", "update", "--id", "900"], capsys)
        assert out["reason"] == "validation_error" and env.fetches == []


# ---------------------------------------------------------------------------
# ACF and $upload
# ---------------------------------------------------------------------------


class TestUploadMarkers:
    def test_markers_at_depth_upload_first_then_one_write_with_the_ids(
            self, env, media, updates, capsys):
        a, b, c = own(env, "a.png"), own(env, "b.jpg", JPEG), own(env, "c.png")
        acf = {"blocks": [
            {"acf_fc_layout": "gallery",
             "images": [{"$upload": str(a), "alt": "First"}, {"$upload": str(b)}]},
            {"acf_fc_layout": "rows",
             "rows": [{"label": "one", "image": {"$upload": str(c), "title": "Third"}}]},
        ]}
        acf_file = own(env, "acf.json", json.dumps(acf).encode())
        code, out = run(["create", "--type", "update", "--title", "Weekly",
                         "--acf-file", str(acf_file)], capsys)
        assert code == 0, out
        sent = writes(env.site)
        assert [r.url.path for r in sent] == ["/wp-json/wp/v2/media"] * 3 + [
            "/wp-json/wp/v2/updates"]
        assert body_of(sent[-1])["acf"] == {"blocks": [
            {"acf_fc_layout": "gallery", "images": [900, 901]},
            {"acf_fc_layout": "rows", "rows": [{"label": "one", "image": 902}]},
        ]}
        assert sent[0].url.params["alt_text"] == "First"
        assert sent[1].headers["content-type"] == "image/jpeg"
        assert [u["id"] for u in out["uploads"]] == [900, 901, 902]
        assert out["readback"]["dropped"] == [] and out["readback"]["changed"] == []

    def test_the_same_file_twice_is_one_upload(self, env, media, updates, capsys):
        a = own(env, "a.png")
        code, out = run(["create", "--type", "update", "--title", "x", "--acf-set",
                         f'blocks=[{{"$upload": "{a}"}}, {{"$upload": "{a}"}}]'], capsys)
        assert code == 0, out
        assert len(media_posts(env.site)) == 1
        assert body_of(writes(env.site)[-1])["acf"] == {"blocks": [900, 900]}

    @pytest.mark.parametrize("where", ["outside", "symlink", "memory", "talk", "relative"])
    def test_a_marker_path_out_of_bounds_sends_nothing(
            self, env, media, updates, capsys, tmp_path, monkeypatch, where):
        if where == "outside":
            target = tmp_path / "secret.png"
            target.write_bytes(PNG)
        elif where == "symlink":
            real = tmp_path / "secret.png"
            real.write_bytes(PNG)
            target = env.mount / "Users" / "alice" / "link.png"
            target.symlink_to(real)
        elif where == "memory":
            monkeypatch.setenv("ISTOTA_WITHHELD_SCOPES", "memory")
            monkeypatch.setenv("ISTOTA_BOT_DIR_NAME", "istota")
            target = own(env, "memories/note.png")
        elif where == "talk":
            target = env.mount / "Talk" / "shared.png"
            target.parent.mkdir(parents=True)
            target.write_bytes(PNG)
        else:
            own(env, "rel.png")
            target = "rel.png"
        code, out = run(["create", "--type", "update", "--title", "x",
                         "--acf-set", "hero="
                         + json.dumps({"$upload": str(target)})], capsys)
        assert code == 1 and out["reason"] == "host_path_refused", out
        assert env.fetches == [] and env.site.requests == []

    def test_a_failed_upload_writes_no_post_and_lists_what_was_stored(
            self, env, media, updates, capsys):
        a, b = own(env, "a.png"), own(env, "b.png")
        calls = []

        def second_fails(request):
            calls.append(request)
            if len(calls) == 2:
                return httpx.Response(502)
            return media.create(request)

        env.site.routes[("POST", "/wp-json/wp/v2/media")] = second_fails
        code, out = run(["create", "--type", "update", "--title", "x", "--acf-set",
                         f'blocks=[{{"$upload": "{a}"}}, {{"$upload": "{b}"}}]'], capsys)
        assert code == 1 and out["reason"] == "outcome_unknown"
        assert out["uploaded"] == [{"path": str(a.resolve()), "id": 900}]
        assert out["failed_path"] == str(b.resolve())
        assert "media list --search b" in out["lookup"]
        assert [r.url.path for r in writes(env.site)] == ["/wp-json/wp/v2/media"] * 2

    def test_a_failed_post_write_lists_the_uploads(self, env, media, updates, capsys):
        a = own(env, "a.png")
        env.site.routes[("POST", "/wp-json/wp/v2/updates")] = httpx.Response(
            400, json={"code": "rest_invalid_param", "message": "bad",
                       "data": {"params": {"acf": "blocks is wrong"}}})
        code, out = run(["create", "--type", "update", "--title", "x",
                         "--acf-set", "hero="
                         + json.dumps({"$upload": str(a)})], capsys)
        assert code == 1 and out["reason"] == "validation_error"
        assert out["uploaded"] == [{"path": str(a.resolve()), "id": 900}]

    def test_a_marker_with_extra_keys_is_refused(self, env, media, updates, capsys):
        a = own(env, "a.png")
        code, out = run(["create", "--type", "update", "--title", "x", "--acf-set",
                         f'hero={{"$upload": "{a}", "id": 5}}'], capsys)
        assert out["reason"] == "validation_error" and env.fetches == []

    def test_a_string_that_looks_like_a_path_is_never_uploaded(self, env, media, updates, capsys):
        a = own(env, "a.png")
        code, out = run(["create", "--type", "update", "--title", "x",
                         "--acf-set", f'intro="{a}"'], capsys)
        assert code == 0, out
        assert media_posts(env.site) == []
        assert body_of(writes(env.site)[-1])["acf"] == {"intro": str(a)}


class TestAcfWrites:
    def test_a_type_without_rest_fields_refuses_before_any_write(self, env, media, posts, capsys):
        env.site.routes[("OPTIONS", "/wp-json/wp/v2/posts")] = {
            "schema": {"properties": {"title": {}}}}
        a = own(env, "a.png")
        code, out = run(["create", "--type", "post", "--title", "x", "--acf-set",
                         "layout=\"full\"", "--featured-image", str(a)], capsys)
        assert code == 1 and out["reason"] == "acf_not_in_rest"
        assert "Show in REST API" in out["error"]
        assert writes(env.site) == []

    def test_an_unknown_field_is_named_and_drops_the_cache(self, env, updates, capsys):
        run(["describe", "--type", "update"], capsys)
        code, out = run(["create", "--type", "update", "--title", "x",
                         "--acf-set", "subtitel=\"x\""], capsys)
        assert out["reason"] == "acf_not_in_rest" and out["fields"] == ["subtitel"]
        assert writes(env.site) == []
        options_before = [r for r in env.site.requests if r.method == "OPTIONS"]
        run(["create", "--type", "update", "--title", "x", "--acf-set", "subtitel=\"x\""],
            capsys)
        options_after = [r for r in env.site.requests if r.method == "OPTIONS"]
        assert len(options_after) == len(options_before) + 1

    def test_an_update_writes_only_the_named_fields_whole(self, env, updates, capsys):
        updates.add(acf={"layout": "narrow", "intro": "keep me",
                         "blocks": [{"acf_fc_layout": "text", "body": "b"}]})
        code, out = run(["update", "--id", "50", "--type", "update",
                         "--acf-set", "layout=\"full\""], capsys)
        assert code == 0, out
        assert body_of(writes(env.site)[0]) == {"acf": {"layout": "full"}}
        assert updates.items[50]["acf"]["intro"] == "keep me"
        assert out["readback"] == {"dropped": [], "changed": [], "notes": []}

    def test_acf_set_overrides_the_file_field_by_field(self, env, updates, capsys):
        acf_file = own(env, "acf.json", json.dumps({"layout": "a", "intro": "i"}).encode())
        run(["create", "--type", "update", "--title", "x", "--acf-file", str(acf_file),
             "--acf-set", "layout=\"b\""], capsys)
        assert body_of(writes(env.site)[0])["acf"] == {"layout": "b", "intro": "i"}

    def test_an_image_returned_as_an_object_is_not_a_change(self, env, media, updates, capsys):
        def expand(item):
            hero = item.get("acf", {}).get("hero")
            if isinstance(hero, int):
                item["acf"]["hero"] = {"id": hero, "url": "https://x"}
        updates.on_save = expand
        a = own(env, "a.png")
        code, out = run(["create", "--type", "update", "--title", "x", "--acf-set",
                         "hero=" + json.dumps({"$upload": str(a)})],
                        capsys)
        assert code == 0, out
        assert out["readback"]["changed"] == []

    def test_a_field_acf_did_not_keep_is_reported(self, env, updates, capsys):
        updates.on_save = lambda item: item["acf"].pop("intro", None)
        _, out = run(["create", "--type", "update", "--title", "x",
                      "--acf-set", "intro=\"hi\""], capsys)
        assert out["readback"]["dropped"] == ["acf.intro"]
        assert any("ACF" in n for n in out["readback"]["notes"])

    def test_acf_set_takes_json(self, env, updates, capsys):
        code, out = run(["create", "--type", "update", "--title", "x",
                         "--acf-set", "layout=full"], capsys)
        assert out["reason"] == "validation_error" and env.fetches == []

    def test_a_live_post_names_the_acf_fields_in_the_gate(self, env, media, updates, capsys):
        updates.add(status="publish", title={"raw": "Live"})
        a = own(env, "a.png")
        code, out = run(["update", "--id", "50", "--type", "update", "--acf-set",
                         "layout=\"full\"", "--featured-image", str(a)], capsys)
        assert code == 1 and out["reason"] == "confirmation_required"
        assert "acf.layout" in out["would"][0] and "featured_media" in out["would"][0]
        assert writes(env.site) == []


class TestFeaturedImage:
    def test_it_is_uploaded_then_set_on_the_post(self, env, media, posts, capsys):
        a = own(env, "cover.png")
        code, out = run(["create", "--type", "post", "--title", "x",
                         "--featured-image", str(a)], capsys)
        assert code == 0, out
        upload, post = writes(env.site)
        assert upload.url.path == "/wp-json/wp/v2/media"
        assert body_of(post)["featured_media"] == 900
        assert out["item"]["featured_media"] == 900
        assert out["uploads"][0]["id"] == 900

    def test_it_excludes_featured_media_id(self, env, capsys, tmp_path):
        with pytest.raises(SystemExit):
            from istota.skills import wordpress as wp
            wp.main(["create", "--type", "post", "--title", "x", "--featured-media-id", "1",
                     "--featured-image", str(tmp_path / "a.png")])
        capsys.readouterr()

    def test_if_absent_finding_the_post_uploads_nothing(self, env, media, posts, capsys):
        posts.add(slug="hello")
        env.site.routes[("GET", "/wp-json/wp/v2/posts")] = lambda r: httpx.Response(
            200, json=[i for i in posts.items.values() if i["slug"] == r.url.params.get("slug")])
        a = own(env, "cover.png")
        code, out = run(["create", "--type", "post", "--title", "x", "--slug", "hello",
                         "--if-absent", "--featured-image", str(a)], capsys)
        assert code == 0 and out["created"] is False
        assert writes(env.site) == []


# ---------------------------------------------------------------------------
# terms create
# ---------------------------------------------------------------------------


class TestTermsCreate:
    def _categories(self, env, existing):
        created = []

        def create(request):
            body = body_of(request)
            created.append(body)
            return httpx.Response(201, json={"id": 77, "taxonomy": "category", **body})

        env.site.routes[("GET", "/wp-json/wp/v2/categories")] = existing
        env.site.routes[("POST", "/wp-json/wp/v2/categories")] = create
        return created

    def test_it_is_gated_and_sends_nothing_unconfirmed(self, env, capsys):
        created = self._categories(env, [])
        code, out = run(["terms", "create", "--taxonomy", "category", "--name", "Essays"],
                        capsys)
        assert code == 1 and out["reason"] == "confirmation_required"
        assert "create category terms" in out["would"][0]
        assert created == [] and writes(env.site) == []

    def test_confirmed_it_creates_once(self, env, capsys):
        created = self._categories(env, [])
        code, out = run(["terms", "create", "--taxonomy", "category", "--name", "Essays",
                         "--parent", "3", "--slug", "essays", "--confirmed"], capsys)
        assert code == 0, out
        assert created == [{"name": "Essays", "parent": 3, "slug": "essays"}]
        assert out["created"] is True and out["item"]["id"] == 77

    def test_an_existing_term_is_returned_without_a_gate(self, env, capsys):
        created = self._categories(env, [{"id": 5, "name": "Essays", "slug": "essays"}])
        code, out = run(["terms", "create", "--taxonomy", "category", "--name", "essays"],
                        capsys)
        assert code == 0, out
        assert out == {**out, "created": False, "id": 5}
        assert created == []

    def test_an_ambiguous_create_names_the_lookup(self, env, capsys):
        self._categories(env, [])
        env.site.routes[("POST", "/wp-json/wp/v2/categories")] = httpx.Response(503)
        _, out = run(["terms", "create", "--taxonomy", "category", "--name", "Essays",
                      "--confirmed"], capsys)
        assert out["reason"] == "outcome_unknown"
        assert out["lookup"] == ("terms list --taxonomy category --search Essays --site blog")
        assert len(writes(env.site)) == 1
