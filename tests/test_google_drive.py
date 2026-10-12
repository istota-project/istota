"""Drive listing at the HTTP transport boundary."""
import httpx
import pytest

from istota.google import drive


@pytest.fixture
def upstream(monkeypatch):
    requests = []
    options = []
    response = {"status": 200, "body": {"files": []}}
    real_client = httpx.Client

    def handle(request):
        requests.append(request)
        return httpx.Response(response["status"], json=response["body"])

    def client(**kwargs):
        options.append(kwargs)
        return real_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    return requests, options, response


@pytest.mark.parametrize("folder,q", [
    ("root", "'root' in parents and trashed=false"),
    ("shared", "sharedWithMe=true and trashed=false"),
    ("folder_12345", "'folder_12345' in parents and trashed=false"),
])
def test_queries(upstream, folder, q):
    requests, options, response = upstream
    response["body"] = {"files": [{"id": "file_123456", "name": "<b>Budget</b>",
        "mimeType": "application/pdf", "size": "42", "modifiedTime": "2026-01-01T00:00:00Z",
        "webViewLink": "https://drive.google.com/file/d/file_123456/view"}], "nextPageToken": "next"}
    items, next_page = drive.list_folder("test-token", folder, "opaque+cursor")
    assert len(requests) == 1
    request = requests[0]
    assert request.url.host == "www.googleapis.com"
    assert request.url.path == "/drive/v3/files"
    assert request.url.params == httpx.QueryParams({"q": q, "pageToken": "opaque+cursor",
        "fields": "nextPageToken,files(id,name,mimeType,modifiedTime,size,webViewLink)",
        "pageSize": 200, "orderBy": "folder,name", "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true"})
    assert request.headers["authorization"] == "Bearer test-token"
    assert options == [{"trust_env": False, "timeout": 10.0}]
    assert next_page == "next"
    assert items[0].name == "<b>Budget</b>" and items[0].size == 42
    assert not items[0].is_folder


@pytest.mark.parametrize("folder", ["short", "x" * 201, "folder_1234' or true", "folder_1234\n"])
def test_bad_id_before_request(upstream, folder):
    with pytest.raises(drive.DriveError) as caught:
        drive.list_folder("test-token", folder, None)
    assert caught.value.status == 400
    assert upstream[0] == []


@pytest.mark.parametrize("url,kept", [
    ("https://drive.google.com/file/d/example/view", True),
    ("https://docs.google.com/document/d/example/edit", True),
    ("https://drive.google.com.example.com/file", False),
    ("https://drive.google.com@evil.example/file", False),
    ("javascript:alert(1)", False),
])
def test_web_links_and_folder(upstream, url, kept):
    upstream[2]["body"] = {"files": [{"id": "folder_12345", "name": "Folder",
        "mimeType": "application/vnd.google-apps.folder", "webViewLink": url}]}
    items, next_page = drive.list_folder("test-token", "root", None)
    assert items[0].web_url == (url if kept else None)
    assert items[0].is_folder and items[0].modified is None and items[0].size is None
    assert next_page is None


@pytest.mark.parametrize("status", [401, 403, 500])
def test_upstream_errors_are_redacted(upstream, caplog, status):
    upstream[2].update(status=status, body={"error": "private upstream detail"})
    with pytest.raises(drive.DriveError) as caught:
        drive.list_folder("test-token", "root", None)
    assert caught.value.status == 502
    if status == 401:
        assert caught.value.message == "Google rejected the stored credential. Reconnect in Settings."
    assert "private upstream detail" not in str(caught.value) + caplog.text
    assert str(status) in caplog.text


def test_parent_folder_uses_file_metadata(upstream):
    requests, options, response = upstream
    response["body"] = {"parents": ["folder_12345"]}
    assert drive.parent_folder("test-token", "file_123456") == "folder_12345"
    assert requests[0].url.path == "/drive/v3/files/file_123456"
    assert requests[0].url.params == httpx.QueryParams({"fields": "parents", "supportsAllDrives": "true"})
    assert requests[0].headers["authorization"] == "Bearer test-token"
    assert options == [{"trust_env": False, "timeout": 10.0}]


def test_parent_folder_refuses_bad_ids_and_missing_parent(upstream):
    with pytest.raises(drive.DriveError):
        drive.parent_folder("test-token", "bad/id")
    assert not upstream[0]
    upstream[2]["body"] = {"parents": []}
    with pytest.raises(drive.DriveError, match="Containing folder is unavailable"):
        drive.parent_folder("test-token", "file_123456")


def test_parent_folder_redacts_upstream_errors(upstream):
    upstream[2].update(status=401, body={"error": "private detail"})
    with pytest.raises(drive.DriveError, match="Reconnect in Settings"):
        drive.parent_folder("test-token", "file_123456")
