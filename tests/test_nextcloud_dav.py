"""WebDAV control plane — the files group (Stage 4)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from istota.config import Config, NextcloudConfig
from istota.nextcloud import OcsError, dav
from istota.skills.nextcloud import build_parser, main


@pytest.fixture
def nc_config():
    return Config(
        nextcloud=NextcloudConfig(
            url="https://cloud.example.com", username="istota", app_password="pw"
        )
    )


@pytest.fixture(autouse=True)
def _nc_env(monkeypatch):
    monkeypatch.setenv("NC_URL", "https://cloud.example.com")
    monkeypatch.setenv("NC_USER", "istota")
    monkeypatch.setenv("NC_PASS", "secret")
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")


def _run(capsys, argv):
    code = 0
    try:
        main(argv)
    except SystemExit as e:
        code = e.code
    out = capsys.readouterr().out
    return (json.loads(out) if out.strip() else None), code


PROPFIND_FILE = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">
  <d:response>
    <d:href>/remote.php/dav/files/istota/Users/alice/report.pdf</d:href>
    <d:propstat>
      <d:prop>
        <d:getlastmodified>Sat, 25 Jul 2026 10:00:00 GMT</d:getlastmodified>
        <d:getcontentlength>2048</d:getcontentlength>
        <d:getcontenttype>application/pdf</d:getcontenttype>
        <d:getetag>&quot;abc123&quot;</d:getetag>
        <d:resourcetype/>
        <oc:fileid>90210</oc:fileid>
        <oc:permissions>RGDNVW</oc:permissions>
        <oc:owner-id>alice</oc:owner-id>
        <oc:owner-display-name>Alice</oc:owner-display-name>
        <oc:share-types><oc:share-type>3</oc:share-type></oc:share-types>
        <oc:favorite>1</oc:favorite>
        <nc:has-preview>true</nc:has-preview>
        <nc:mount-type>shared</nc:mount-type>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>"""

PROPFIND_FOLDER = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">
  <d:response>
    <d:href>/remote.php/dav/files/istota/Users/alice/docs/</d:href>
    <d:propstat>
      <d:prop>
        <d:resourcetype><d:collection/></d:resourcetype>
        <oc:fileid>1</oc:fileid>
        <oc:size>4096</oc:size>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/files/istota/Users/alice/docs/a.txt</d:href>
    <d:propstat>
      <d:prop>
        <d:getcontentlength>12</d:getcontentlength>
        <d:resourcetype/>
        <oc:fileid>2</oc:fileid>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
    <d:propstat>
      <d:prop><nc:mount-type/></d:prop>
      <d:status>HTTP/1.1 404 Not Found</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>"""

QUOTA_XML = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:">
  <d:response>
    <d:href>/remote.php/dav/files/istota/</d:href>
    <d:propstat>
      <d:prop>
        <d:quota-available-bytes>800</d:quota-available-bytes>
        <d:quota-used-bytes>200</d:quota-used-bytes>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>"""


def _dav_response(text="", status=207, content=b""):
    return MagicMock(status_code=status, text=text, content=content)


# --- PROPFIND parsing ---


class TestParseMultistatus:
    def test_all_namespaces_parsed(self, nc_config):
        entry = dav.parse_multistatus(nc_config, PROPFIND_FILE)[0]
        assert entry["path"] == "/Users/alice/report.pdf"
        assert entry["name"] == "report.pdf"
        assert entry["is_dir"] is False
        assert entry["size"] == 2048
        assert entry["content_type"] == "application/pdf"
        assert entry["etag"] == "abc123"
        assert entry["fileid"] == "90210"
        assert entry["permissions"] == "RGDNVW"
        assert entry["share_types"] == [3]
        assert entry["favorite"] is True
        assert entry["owner_id"] == "alice"
        assert entry["has_preview"] is True
        assert entry["mount_type"] == "shared"

    def test_collection_detected(self, nc_config):
        entries = dav.parse_multistatus(nc_config, PROPFIND_FOLDER)
        assert entries[0]["is_dir"] is True
        assert entries[1]["is_dir"] is False

    def test_folder_uses_recursive_size(self, nc_config):
        assert dav.parse_multistatus(nc_config, PROPFIND_FOLDER)[0]["size"] == 4096

    def test_missing_properties_are_empty_not_a_crash(self, nc_config):
        """Which props a server returns varies; a 404 propstat is skipped."""
        entry = dav.parse_multistatus(nc_config, PROPFIND_FOLDER)[1]
        assert entry["mount_type"] == ""
        assert entry["content_type"] == ""
        assert entry["share_types"] == []
        assert entry["favorite"] is False

    def test_malformed_xml_raises_a_legible_error(self, nc_config):
        with pytest.raises(OcsError) as exc:
            dav.parse_multistatus(nc_config, "<not xml")
        assert "parse" in exc.value.message.lower()

    def test_href_decoding_strips_the_dav_prefix(self, nc_config):
        href = "/remote.php/dav/files/istota/Users/alice/my%20file.txt"
        assert dav.href_to_path(nc_config, href) == "/Users/alice/my file.txt"


class TestStatAndList:
    @patch("istota.nextcloud._http.httpx.request")
    def test_stat_issues_depth_0(self, mock_req, nc_config):
        mock_req.return_value = _dav_response(PROPFIND_FILE)
        entry = dav.stat(nc_config, "/Users/alice/report.pdf")
        assert entry["fileid"] == "90210"
        assert mock_req.call_args[0][0] == "PROPFIND"
        assert mock_req.call_args.kwargs["headers"]["Depth"] == "0"

    @patch("istota.nextcloud._http.httpx.request")
    def test_stat_on_empty_result_raises_404(self, mock_req, nc_config):
        mock_req.return_value = _dav_response('<d:multistatus xmlns:d="DAV:"/>')
        with pytest.raises(OcsError) as exc:
            dav.stat(nc_config, "/Users/alice/missing")
        assert exc.value.http_status == 404

    @patch("istota.nextcloud._http.httpx.request")
    def test_list_excludes_the_collection_itself(self, mock_req, nc_config):
        mock_req.return_value = _dav_response(PROPFIND_FOLDER)
        entries = dav.list_dir(nc_config, "/Users/alice/docs")
        assert [e["name"] for e in entries] == ["a.txt"]


# --- SEARCH body construction ---


class TestSearchBody:
    def test_scope_href_is_relative_to_the_dav_root(self, nc_config):
        """Sabre resolves the scope against /remote.php/dav/.

        Repeating that prefix in the href makes it hunt for a collection named
        "remote.php" and 404 every search — the live suite caught this.
        """
        body = dav.build_search_body(nc_config, scope="/Users/alice")
        assert "<d:href>/files/istota/Users/alice</d:href>" in body
        assert "/remote.php" not in body

    def test_name_filter(self, nc_config):
        body = dav.build_search_body(nc_config, scope="/Users/alice", name="*.pdf")
        assert "<d:displayname/>" in body
        assert "<d:literal>%.pdf</d:literal>" in body
        assert "<d:and>" not in body

    def test_bare_term_matches_anywhere(self, nc_config):
        body = dav.build_search_body(nc_config, scope="/Users/alice", name="report")
        assert "<d:literal>%report%</d:literal>" in body

    def test_mime_filter(self, nc_config):
        body = dav.build_search_body(nc_config, scope="/Users/alice", mime="image/*")
        assert "<d:getcontenttype/>" in body
        assert "<d:literal>image/%</d:literal>" in body

    def test_min_size_filter(self, nc_config):
        body = dav.build_search_body(nc_config, scope="/Users/alice", min_size=1024)
        assert "<d:gte>" in body
        assert "<d:literal>1024</d:literal>" in body

    def test_modified_since_filter(self, nc_config):
        body = dav.build_search_body(
            nc_config, scope="/Users/alice", modified_since="Sat, 25 Jul 2026 00:00:00 GMT"
        )
        assert "<d:gt>" in body
        assert "getlastmodified" in body

    def test_combined_filters_are_anded(self, nc_config):
        body = dav.build_search_body(
            nc_config, scope="/Users/alice", name="*.pdf", mime="application/pdf", min_size=10
        )
        assert "<d:and>" in body
        assert body.count("<d:like>") == 2

    def test_no_filters_omits_where(self, nc_config):
        body = dav.build_search_body(nc_config, scope="/Users/alice")
        assert "<d:where>" not in body

    def test_scope_carries_the_requested_subtree(self, nc_config):
        body = dav.build_search_body(nc_config, scope="/Users/alice/docs")
        assert "<d:href>/files/istota/Users/alice/docs</d:href>" in body

    def test_limit_included(self, nc_config):
        body = dav.build_search_body(nc_config, scope="/Users/alice", limit=7)
        assert "<d:nresults>7</d:nresults>" in body

    def test_literal_is_xml_escaped(self, nc_config):
        body = dav.build_search_body(nc_config, scope="/Users/alice", name="a&b")
        assert "a&amp;b" in body

    @patch("istota.nextcloud._http.httpx.request")
    def test_search_posts_to_the_dav_root(self, mock_req, nc_config):
        mock_req.return_value = _dav_response(PROPFIND_FILE)
        dav.search(nc_config, scope="/Users/alice", name="report")
        assert mock_req.call_args[0][0] == "SEARCH"
        assert mock_req.call_args[0][1] == "https://cloud.example.com/remote.php/dav/"


# --- upload ---


class TestUpload:
    @patch("istota.nextcloud._http.httpx.request")
    def test_small_file_uses_plain_put(self, mock_req, nc_config, tmp_path):
        mock_req.return_value = _dav_response(status=201)
        local = tmp_path / "a.txt"
        local.write_bytes(b"hello")

        result = dav.upload(nc_config, local, "/Users/alice/a.txt")
        assert result["method"] == "plain"
        assert mock_req.call_args[0][0] == "PUT"
        assert mock_req.call_count == 1

    @patch("istota.nextcloud.dav.CHUNKED_UPLOAD_THRESHOLD", 4)
    @patch("istota.nextcloud.dav.CHUNK_SIZE", 4)
    @patch("istota.nextcloud._http.httpx.request")
    def test_large_file_chunks(self, mock_req, nc_config, tmp_path):
        mock_req.return_value = _dav_response(status=201)
        local = tmp_path / "big.bin"
        local.write_bytes(b"0123456789")

        result = dav.upload(nc_config, local, "/Users/alice/big.bin")
        assert result["method"] == "chunked"
        methods = [c[0][0] for c in mock_req.call_args_list]
        assert methods[0] == "MKCOL"
        assert methods.count("PUT") == 3  # 4 + 4 + 2 bytes
        assert methods[-1] == "MOVE"
        assert mock_req.call_args_list[-1].kwargs["headers"]["Destination"].endswith(
            "/Users/alice/big.bin"
        )

    @patch("istota.nextcloud.dav.CHUNKED_UPLOAD_THRESHOLD", 4)
    @patch("istota.nextcloud._http.httpx.request")
    def test_falls_back_to_plain_put_without_chunking_capability(
        self, mock_req, nc_config, tmp_path
    ):
        mock_req.return_value = _dav_response(status=201)
        local = tmp_path / "big.bin"
        local.write_bytes(b"0123456789")

        result = dav.upload(nc_config, local, "/Users/alice/big.bin", supports_chunking=False)
        assert result["method"] == "plain"
        assert result["chunking_available"] is False
        assert [c[0][0] for c in mock_req.call_args_list] == ["PUT"]

    @patch("istota.nextcloud.dav.CHUNK_SIZE", 4)
    @patch("istota.nextcloud._http.httpx.request")
    def test_explicit_chunked_flag_overrides_size(self, mock_req, nc_config, tmp_path):
        mock_req.return_value = _dav_response(status=201)
        local = tmp_path / "small.txt"
        local.write_bytes(b"hi")

        result = dav.upload(nc_config, local, "/Users/alice/small.txt", chunked=True)
        assert result["method"] == "chunked"

    @patch("istota.nextcloud.dav.CHUNKED_UPLOAD_THRESHOLD", 4)
    @patch("istota.nextcloud._http.httpx.request")
    def test_failed_assembly_cleans_up_the_upload_collection(
        self, mock_req, nc_config, tmp_path
    ):
        local = tmp_path / "big.bin"
        local.write_bytes(b"0123456789")

        def _side_effect(method, url, **kwargs):
            if method == "MOVE":
                return _dav_response(status=507)
            return _dav_response(status=201)

        mock_req.side_effect = _side_effect
        with pytest.raises(OcsError):
            dav.upload(nc_config, local, "/Users/alice/big.bin")

        assert "DELETE" in [c[0][0] for c in mock_req.call_args_list]

    def test_missing_local_file_raises(self, nc_config, tmp_path):
        with pytest.raises(OcsError) as exc:
            dav.upload(nc_config, tmp_path / "nope.txt", "/Users/alice/nope.txt")
        assert "No such local file" in exc.value.message


class TestDownload:
    @patch("istota.nextcloud._http.httpx.request")
    def test_writes_bytes_and_creates_parents(self, mock_req, nc_config, tmp_path):
        mock_req.return_value = _dav_response(status=200, content=b"payload")
        dest = tmp_path / "nested" / "out.bin"

        result = dav.download(nc_config, "/Users/alice/a.bin", dest)
        assert dest.read_bytes() == b"payload"
        assert result["bytes"] == 7
        assert mock_req.call_args[0][0] == "GET"


# --- versions and trash ---


VERSIONS_XML = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">
  <d:response>
    <d:href>/remote.php/dav/versions/istota/versions/90210/</d:href>
    <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>
    <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/versions/istota/versions/90210/1753440000</d:href>
    <d:propstat><d:prop>
      <d:getcontentlength>1024</d:getcontentlength>
      <d:getlastmodified>Fri, 24 Jul 2026 10:00:00 GMT</d:getlastmodified>
      <d:resourcetype/>
    </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
</d:multistatus>"""

TRASH_XML = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">
  <d:response>
    <d:href>/remote.php/dav/trashbin/istota/trash/</d:href>
    <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>
    <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/trashbin/istota/trash/report.pdf.d1753440000</d:href>
    <d:propstat><d:prop><d:getcontentlength>2048</d:getcontentlength><d:resourcetype/></d:prop>
    <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
</d:multistatus>"""


class TestVersions:
    @patch("istota.nextcloud._http.httpx.request")
    def test_lists_versions_keyed_on_fileid(self, mock_req, nc_config):
        mock_req.side_effect = [_dav_response(PROPFIND_FILE), _dav_response(VERSIONS_XML)]
        result = dav.versions(nc_config, "/Users/alice/report.pdf")

        assert result["fileid"] == "90210"
        assert result["versions"][0]["version"] == "1753440000"
        assert result["versions"][0]["size"] == 1024
        assert "/versions/istota/versions/90210" in mock_req.call_args_list[1][0][1]

    @patch("istota.nextcloud._http.httpx.request")
    def test_restore_issues_a_move_to_the_restore_target(self, mock_req, nc_config):
        mock_req.side_effect = [_dav_response(PROPFIND_FILE), _dav_response(status=204)]
        dav.restore_version(nc_config, "/Users/alice/report.pdf", "1753440000")

        call = mock_req.call_args_list[1]
        assert call[0][0] == "MOVE"
        assert call[0][1].endswith("/versions/90210/1753440000")
        assert call.kwargs["headers"]["Destination"].endswith("/versions/istota/restore/target")


class TestTrash:
    @patch("istota.nextcloud._http.httpx.request")
    def test_list_excludes_the_bin_itself(self, mock_req, nc_config):
        mock_req.return_value = _dav_response(TRASH_XML)
        entries = dav.trash_list(nc_config)
        assert [e["name"] for e in entries] == ["report.pdf.d1753440000"]

    @patch("istota.nextcloud._http.httpx.request")
    def test_restore_issues_the_correct_move(self, mock_req, nc_config):
        mock_req.return_value = _dav_response(status=201)
        dav.trash_restore(nc_config, "report.pdf.d1753440000")

        assert mock_req.call_args[0][0] == "MOVE"
        assert mock_req.call_args[0][1].endswith("/trash/report.pdf.d1753440000")
        assert mock_req.call_args.kwargs["headers"]["Destination"].endswith(
            "/trashbin/istota/restore/target"
        )

    @patch("istota.nextcloud._http.httpx.request")
    def test_empty_deletes_the_bin(self, mock_req, nc_config):
        mock_req.return_value = _dav_response(status=204)
        dav.trash_empty(nc_config)
        assert mock_req.call_args[0][0] == "DELETE"


class TestFavoriteAndQuota:
    @patch("istota.nextcloud._http.httpx.request")
    def test_favorite_proppatch(self, mock_req, nc_config):
        mock_req.return_value = _dav_response(status=207)
        dav.set_favorite(nc_config, "/Users/alice/report.pdf")
        assert mock_req.call_args[0][0] == "PROPPATCH"
        assert "<oc:favorite>1</oc:favorite>" in mock_req.call_args.kwargs["content"]

    @patch("istota.nextcloud._http.httpx.request")
    def test_unfavorite_sends_zero(self, mock_req, nc_config):
        mock_req.return_value = _dav_response(status=207)
        dav.set_favorite(nc_config, "/Users/alice/report.pdf", favorite=False)
        assert "<oc:favorite>0</oc:favorite>" in mock_req.call_args.kwargs["content"]

    @patch("istota.nextcloud._http.httpx.request")
    def test_quota_totals(self, mock_req, nc_config):
        mock_req.return_value = _dav_response(QUOTA_XML)
        result = dav.quota(nc_config)
        assert result == {"used_bytes": 200, "available_bytes": 800, "total_bytes": 1000}

    @patch("istota.nextcloud._http.httpx.request")
    def test_unlimited_quota_has_no_total(self, mock_req, nc_config):
        xml = QUOTA_XML.replace("<d:quota-available-bytes>800", "<d:quota-available-bytes>-3")
        mock_req.return_value = _dav_response(xml)
        assert dav.quota(nc_config)["total_bytes"] is None


# --- through the CLI, including path scoping ---


class TestFilesCli:
    @patch("istota.nextcloud.dav.stat")
    def test_stat_verb(self, mock_stat, capsys):
        mock_stat.return_value = {"fileid": "1"}
        out, code = _run(capsys, ["files", "stat", "/Users/alice/a.txt"])
        assert code == 0
        assert out["fileid"] == "1"

    @patch("istota.nextcloud.dav.list_dir")
    def test_list_verb_wraps_with_a_count(self, mock_list, capsys):
        mock_list.return_value = [{"name": "a.txt"}]
        out, code = _run(capsys, ["files", "list", "/Users/alice/docs"])
        assert code == 0
        assert out["count"] == 1
        assert out["path"] == "/Users/alice/docs"

    @patch("istota.nextcloud.dav.search")
    def test_search_verb(self, mock_search, capsys):
        mock_search.return_value = []
        out, code = _run(
            capsys, ["files", "search", "--scope", "/Users/alice", "--name", "*.pdf"]
        )
        assert code == 0
        assert mock_search.call_args.kwargs["name"] == "*.pdf"
        assert mock_search.call_args.kwargs["scope"] == "/Users/alice"

    @patch("istota.nextcloud.dav.quota")
    def test_quota_verb(self, mock_quota, capsys):
        mock_quota.return_value = {"used_bytes": 1}
        out, code = _run(capsys, ["files", "quota"])
        assert code == 0
        assert out["used_bytes"] == 1

    @patch("istota.nextcloud.dav.trash_list")
    def test_trash_list_verb(self, mock_list, capsys):
        mock_list.return_value = [{"name": "x"}]
        out, code = _run(capsys, ["files", "trash", "list"])
        assert code == 0
        assert out["count"] == 1

    def test_trash_empty_refuses_without_confirmed(self, capsys):
        out, code = _run(capsys, ["files", "trash", "empty"])
        assert code == 1
        assert out["needs_confirmation"] is True

    @patch("istota.nextcloud.dav.trash_empty")
    def test_trash_empty_with_confirmed(self, mock_empty, capsys):
        mock_empty.return_value = {"status": "ok", "emptied": True}
        out, code = _run(capsys, ["files", "trash", "empty", "--confirmed"])
        assert code == 0
        assert out["emptied"] is True

    @patch("istota.nextcloud.dav.set_favorite")
    def test_favorite_off_flag(self, mock_fav, capsys):
        mock_fav.return_value = {"status": "ok"}
        _run(capsys, ["files", "favorite", "/Users/alice/a.txt", "--off"])
        assert mock_fav.call_args.kwargs["favorite"] is False

    @patch("istota.nextcloud.dav.upload")
    @patch("istota.nextcloud.capabilities.fetch_capabilities")
    def test_upload_consults_chunking_capability_only_when_relevant(
        self, mock_caps, mock_upload, capsys, tmp_path
    ):
        local = tmp_path / "small.txt"
        local.write_bytes(b"hi")
        mock_upload.return_value = {"status": "ok"}

        _run(capsys, ["files", "upload", str(local), "/Users/alice/small.txt"])
        mock_caps.assert_not_called()
        assert mock_upload.call_args.kwargs["supports_chunking"] is True

    @patch("istota.nextcloud.dav.upload")
    @patch("istota.nextcloud.capabilities.fetch_capabilities")
    def test_upload_probe_failure_degrades_to_plain(
        self, mock_caps, mock_upload, capsys, tmp_path
    ):
        local = tmp_path / "f.txt"
        local.write_bytes(b"hi")
        mock_caps.side_effect = OcsError("down", None, None, "/cloud/capabilities")
        mock_upload.return_value = {"status": "ok"}

        _run(capsys, ["files", "upload", str(local), "/Users/alice/f.txt", "--chunked"])
        assert mock_upload.call_args.kwargs["supports_chunking"] is False


class TestFilesPathScoping:
    @pytest.fixture(autouse=True)
    def _non_admin(self):
        with patch("istota.skills.nextcloud.load_admin_users", return_value={"root"}):
            yield

    @pytest.mark.parametrize(
        "argv",
        [
            ["files", "stat", "/Users/bob/secret.pdf"],
            ["files", "list", "/Users/bob"],
            ["files", "search", "--scope", "/Users/bob"],
            ["files", "download", "/Users/bob/x", "/tmp/x"],
            ["files", "versions", "/Users/bob/x"],
            ["files", "restore-version", "/Users/bob/x", "1"],
            ["files", "favorite", "/Users/bob/x"],
        ],
    )
    def test_every_path_verb_refuses_an_escape(self, argv, capsys):
        out, code = _run(capsys, argv)
        assert code == 1
        assert "/Users/alice" in out["error"]

    @patch("istota.nextcloud.dav.upload")
    def test_upload_destination_is_scoped(self, mock_upload, capsys, tmp_path):
        local = tmp_path / "f.txt"
        local.write_bytes(b"hi")
        out, code = _run(capsys, ["files", "upload", str(local), "/Users/bob/f.txt"])
        assert code == 1
        mock_upload.assert_not_called()

    @patch("istota.nextcloud.dav.stat")
    def test_relative_path_anchors_to_the_workspace(self, mock_stat, capsys):
        mock_stat.return_value = {}
        _run(capsys, ["files", "stat", "notes.md"])
        assert mock_stat.call_args[0][1] == "/Users/alice/notes.md"


class TestFilesParser:
    def test_search_flags(self):
        args = build_parser().parse_args([
            "files", "search", "--scope", "/x", "--name", "*.pdf",
            "--mime", "application/pdf", "--min-size", "10", "--limit", "5",
        ])
        assert args.scope == "/x"
        assert args.min_size == 10
        assert args.limit == 5

    def test_no_write_verbs_are_offered(self):
        """read/write/mkdir/rm/mv/cp deliberately don't exist — the mount does those."""
        parser = build_parser()
        for verb in ("read", "write", "mkdir", "rm", "mv", "cp"):
            with pytest.raises(SystemExit):
                parser.parse_args(["files", verb, "/x"])
