"""Secret discovery and download keep values off the visible channel."""
import types
from unittest import mock
import pytest
from tests.test_browser_navigation import browse_api
from tests.support.browser_instance import browser_instance  # noqa: F401 -- autouse fixture


def test_find_describes_frames_and_shadow_without_values():
    main, child = mock.Mock(), mock.Mock()
    main.locator.return_value.count.return_value = 1
    child.locator.return_value.count.return_value = 1
    page = mock.Mock(main_frame=main, frames=[main, child])
    main.evaluate.return_value = [{"selector": "div:nth-of-type(1) input:nth-of-type(1)::value", "text": "JBSWY3DPEHPK3PXP", "download": False}]
    child.evaluate.return_value = [{"selector": "pre:nth-of-type(1)", "text": "abcd-1234\nefgh-5678", "download": False}]
    child.parent_frame = main
    child.frame_element.return_value.evaluate.return_value = "iframe:nth-of-type(1)"
    result = browse_api._find_secrets(page)
    assert [r["kind_guess"] for r in result] == ["otp", "codes"]
    assert result[1]["frame"] == "iframe:nth-of-type(1)"
    assert result[0]["count"] == 1 and result[1]["count"] == 2
    assert "JBSW" not in str(result) and "abcd" not in str(result)


def test_find_caps_candidates():
    main = mock.Mock()
    main.locator.return_value.count.return_value = 1
    page = mock.Mock(main_frame=main, frames=[main])
    main.evaluate.return_value = [{"selector": f"pre:nth-of-type({n})", "text": "abcd-1234", "download": False} for n in range(20)]
    assert len(browse_api._find_secrets(page)) == 10


@pytest.mark.parametrize("count", [0, 2])
def test_auto_refuses_ambiguous(monkeypatch, count):
    page = mock.Mock()
    candidate = {"selector": "pre:nth-of-type(1)", "frame": None, "kind_guess": "codes", "download": False}
    monkeypatch.setattr(browse_api, "_find_secrets", lambda p, **kw: [candidate] * count)
    result, text = browse_api._read_secret_action(page, {"selector": "auto", "kind": "codes"})
    assert result["error"] == "capture_auto_ambiguous" and not text
    assert result["candidates"] == [candidate] * count
    page.wait_for_selector.assert_not_called()


def test_auto_uses_frame_and_attribute(monkeypatch):
    page = mock.Mock()
    monkeypatch.setattr(browse_api, "_find_secrets", lambda p, **kw: [{"selector": "input:nth-of-type(1)::attr(data-codes)", "frame": "iframe:nth-of-type(1)", "kind_guess": "codes", "download": False}])
    handle = page.frame_locator.return_value.locator.return_value.element_handle.return_value
    handle.owner_frame.return_value.url = "https://acme.example/setup"
    handle.evaluate.return_value = {"ok": True, "text": "abcd-1234"}
    monkeypatch.setattr(browse_api, "_credential_values", set())
    result, text = browse_api._read_secret_action(page, {"selector": "auto", "kind": "codes", "bound_hosts": ["acme.example"]})
    assert result["ok"] and text == "abcd-1234"
    assert handle.evaluate.call_args.args[1]["attr"] == "data-codes"


@pytest.mark.parametrize("filename,data,error", [("codes.txt", b"abcd-1234\nefgh-5678", None), ("codes.pdf", b"abcd-1234", "capture_download_not_text"), ("codes.txt", b"\xff", "capture_download_not_text"), ("codes.txt", b"abc\x00def", "capture_download_not_text"), ("codes.txt", b"a" * 65537, "recovery_too_large")])
def test_download_deleted_and_redacted(monkeypatch, tmp_path, filename, data, error):
    path = tmp_path / "download"
    path.write_bytes(data)
    page = mock.MagicMock()
    handle = page.wait_for_selector.return_value
    handle.owner_frame.return_value.url = "https://acme.example/setup"
    handle.evaluate.return_value = {"ok": True}
    download = mock.Mock(suggested_filename=filename)
    download.path.return_value = str(path)
    download.delete.side_effect = path.unlink
    page.expect_download.return_value.__enter__.return_value.value = download
    monkeypatch.setattr(browse_api, "_credential_values", set())
    result, text = browse_api._read_secret_action(page, {"type": "read_secret_download", "selector": "button", "kind": "codes", "bound_hosts": ["acme.example"]})
    assert not path.exists()
    download.delete.assert_called_once()
    if error:
        assert result["error"] == error and not text
    else:
        assert result["ok"] and text == data.decode()
        assert result["host"] == "acme.example"
        assert {"abcd-1234", "efgh-5678"} <= browse_api._credential_values


def test_mask_extract_values_and_attributes(monkeypatch):
    page = mock.Mock(url="https://acme.example")
    page.evaluate.return_value = []
    el = mock.Mock()
    el.evaluate.return_value = {"text": "Seed JBSWY3DPEHPK3PXP", "html": '<input data-codes="abcd-1234">', "value": "abcd-1234", "attributes": {"data-codes": "efgh-5678"}}
    page.query_selector_all.return_value = [el]
    monkeypatch.setattr(browse_api, "_cleanup_expired", lambda **kw: None)
    monkeypatch.setattr(browse_api, "_get_session", lambda _: {"page": page})
    monkeypatch.setattr(browse_api, "_session_page", lambda _: page)
    monkeypatch.setattr(browse_api, "_page_is_gone", lambda _: False)
    monkeypatch.setattr(browse_api.chrome, "connect_cdp", lambda _: None)
    monkeypatch.setattr(browse_api, "_credential_values", set())
    monkeypatch.setattr(browse_api, "jsonify", lambda value: value)
    monkeypatch.setattr(browse_api, "request", types.SimpleNamespace(get_json=lambda: {"session_id": "s1", "selector": "input", "mask_secrets": True}))
    result = browse_api.extract()
    assert result["status"] == "ok"
    assert result["elements"][0]["value"] == "[secret]"
    assert "JBSW" not in str(result) and "abcd-1234" not in str(result) and "efgh-5678" not in str(result)


def test_auto_checks_candidates_beyond_the_display_cap(monkeypatch):
    main = mock.Mock()
    main.locator.return_value.count.return_value = 1
    page = mock.Mock(main_frame=main, frames=[main])
    main.evaluate.return_value = [
        {"selector": f"pre:nth-of-type({n})", "text": "apple " * 12, "download": False}
        for n in range(9)
    ] + [{"selector": f"input:nth-of-type({n})", "text": "abcd-1234", "download": False}
         for n in range(2)]
    result, text = browse_api._read_secret_action(page, {"selector": "auto", "kind": "codes"})
    assert result["error"] == "capture_auto_ambiguous" and not text
    assert len(result["candidates"]) == 10
    page.wait_for_selector.assert_not_called()


def test_download_rechecks_origin_without_waiting_for_an_event():
    page = mock.MagicMock()
    handle = mock.Mock()
    handle.evaluate.return_value = {"ok": False}
    result = browse_api._download_secret(page, handle, "https://acme.example")
    assert result == {"ok": False, "error": "credential_origin_mismatch"}
    assert page.expect_download.return_value.__exit__.call_args.args[0] is ValueError


def test_discovery_masks_attached_page_readback(monkeypatch):
    page = mock.Mock()
    candidate = {"selector": "pre:nth-of-type(1)", "frame": None, "kind_guess": "codes",
                 "count": 1, "length": 9, "charset": "[a-z0-9-]", "download": False}
    monkeypatch.setattr(browse_api, "_find_secrets", lambda p: [candidate])
    monkeypatch.setattr(browse_api, "_cleanup_expired", lambda **kw: None)
    monkeypatch.setattr(browse_api, "_get_session", lambda _: {"page": page})
    monkeypatch.setattr(browse_api, "_session_page", lambda _: page)
    monkeypatch.setattr(browse_api.chrome, "connect_cdp", lambda _: None)
    monkeypatch.setattr(browse_api, "_foreground_tabs", lambda _: ([], []))
    monkeypatch.setattr(browse_api.browsing, "detect_captcha", lambda _: False)
    monkeypatch.setattr(browse_api.browsing, "extract_page_content", lambda *a, **kw: {"text": "Save abcd-1234"})
    monkeypatch.setattr(browse_api, "_credential_values", set())
    monkeypatch.setattr(browse_api, "jsonify", lambda value: value)
    monkeypatch.setattr(browse_api, "request", types.SimpleNamespace(get_json=lambda: {
        "session_id": "s1", "actions": [{"type": "find_secrets"}],
    }))
    result = browse_api.interact()
    assert result["status"] == "ok"
    assert "abcd-1234" not in str(result)
    assert result["actions"][0]["candidates"] == [candidate]


def test_discovery_drops_a_path_that_matches_more_than_one_element():
    main = mock.Mock()
    main.locator.return_value.count.return_value = 2
    main.evaluate.return_value = [{"selector": "div:nth-of-type(1) input:nth-of-type(1)::value", "text": "JBSWY3DPEHPK3PXP", "download": False}]
    page = mock.Mock(main_frame=main, frames=[main])
    assert browse_api._find_secrets(page) == []


def test_mask_keeps_known_secret_redaction_whole():
    secret = "fixture-prefix!abcd1234"
    assert browse_api._scrub_extracted(secret, {secret}, mask_secrets=True) == "[REDACTED]"
