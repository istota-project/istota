"""Integration test for browse skill against live browser container.

Runs directly on the machine with the browser container (no istota deps).

Usage:
    ssh <production-host> 'python3 -' < scripts/test_browse_sites.py
    # Or locally if container is reachable:
    BROWSER_API_URL=http://127.0.0.1:9223 python3 scripts/test_browse_sites.py
"""

import json
import os
import sys
import urllib.error
import urllib.request

API = os.environ.get("BROWSER_API_URL", "http://127.0.0.1:9223")

_passed = 0
_failed = 0
_errors = []


def api(endpoint, data=None, method=None):
    if data:
        req = urllib.request.Request(
            API + endpoint,
            data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"},
        )
    else:
        req = urllib.request.Request(API + endpoint)
    if method:
        req.method = method
    try:
        return json.loads(urllib.request.urlopen(req, timeout=90).read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except Exception:
            return {"error": str(e), "body": body[:500], "status_code": e.code}
    except Exception as e:
        return {"error": str(e)}


def close_session(sid):
    if sid:
        api(f"/sessions/{sid}", method="DELETE")


def check(name, condition, detail=""):
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  PASS  {name}")
    else:
        _failed += 1
        msg = f"  FAIL  {name}"
        if detail:
            msg += f" — {detail}"
        print(msg)
        _errors.append(f"{name}: {detail}")


# -- Test suites --


def test_health():
    print("\n=== Health ===")
    r = api("/health")
    check("health returns ok", r.get("status") == "ok", r.get("error", ""))
    check("browser connected", r.get("browser_connected") is True)


def test_session_lifecycle():
    print("\n=== Session lifecycle ===")
    r = api("/browse", {"url": "https://example.com", "keep_session": True, "timeout": 15})
    sid = r.get("session_id", "")
    check("create session", r.get("status") == "ok" and bool(sid), r.get("error", ""))

    if sid:
        r2 = api("/browse", {"url": "https://example.org", "session_id": sid, "timeout": 15})
        check("reuse session", r2.get("status") == "ok" and r2.get("session_id") == sid)

        h_before = api("/health")
        count_before = h_before.get("active_sessions", 0)
        close_session(sid)
        h_after = api("/health")
        count_after = h_after.get("active_sessions", 0)
        check("session cleaned up", count_after < count_before, f"{count_before} -> {count_after}")


def test_site_links(name, url, article_filter, min_articles=5, origin=None,
                    check_content=True, skip_behavior=False):
    """Test a site where articles appear in the links array."""
    print(f"\n=== {name} ===")
    browse_opts = {"url": url, "keep_session": True, "timeout": 30}
    if skip_behavior:
        browse_opts["skip_behavior"] = True
    r = api("/browse", browse_opts)
    sid = r.get("session_id", "")
    status = r.get("status", "error")

    check(f"{name} fetch ok", status == "ok", r.get("error", ""))
    if status != "ok":
        close_session(sid)
        return

    links = r.get("links", [])
    articles = [l for l in links if article_filter(l)]
    check(f"{name} has >={min_articles} articles", len(articles) >= min_articles, f"got {len(articles)}")

    if articles:
        href = articles[0]["href"]
        if href.startswith("/") and origin:
            full_url = origin + href
        else:
            full_url = href
        article_opts = {"url": full_url, "session_id": sid, "timeout": 30}
        if skip_behavior:
            article_opts["skip_behavior"] = True
        r2 = api("/browse", article_opts)
        check(f"{name} article nav ok", r2.get("status") == "ok", r2.get("error", ""))
        if check_content:
            text_len = len(r2.get("text", ""))
            check(f"{name} article has content", text_len > 500, f"got {text_len} chars")
        check(f"{name} session preserved", r2.get("session_id") == sid)

    close_session(sid)


def test_site_extract(name, url, selector, min_articles=5, origin=None):
    """Test a site where articles need CSS selector extraction."""
    print(f"\n=== {name} (extract) ===")
    r = api("/browse", {"url": url, "keep_session": True, "timeout": 30})
    sid = r.get("session_id", "")
    status = r.get("status", "error")

    check(f"{name} fetch ok", status == "ok", r.get("error", ""))
    if status != "ok":
        close_session(sid)
        return

    r2 = api("/extract", {"session_id": sid, "selector": selector})
    count = r2.get("count", 0)
    check(f"{name} extract finds >={min_articles}", count >= min_articles, f"got {count}")

    # Check href attributes on elements
    elements = r2.get("elements", [])
    with_href = [el for el in elements if el.get("href")]
    check(f"{name} elements have href attr", len(with_href) >= min(3, count), f"got {len(with_href)} with href")

    if with_href:
        href = with_href[0]["href"]
        if href.startswith("/") and origin:
            full_url = origin + href
        else:
            full_url = href
        r3 = api("/browse", {"url": full_url, "session_id": sid, "timeout": 30})
        check(f"{name} article nav ok", r3.get("status") == "ok", r3.get("error", ""))
        text_len = len(r3.get("text", ""))
        check(f"{name} article has content", text_len > 500, f"got {text_len} chars")

    close_session(sid)


def main():
    print(f"Browser API: {API}")

    test_health()
    test_session_lifecycle()

    # Sites with articles in links array
    test_site_links(
        "AP News", "https://apnews.com/hub/world-news",
        lambda l: "/article/" in l.get("href", "") and len(l.get("text", "").strip()) > 15,
        origin="https://apnews.com",
    )
    test_site_links(
        "BBC", "https://www.bbc.com/news/world",
        lambda l: "/news/articles/" in l.get("href", "") and len(l.get("text", "").strip()) > 15,
        origin="https://www.bbc.com",
    )
    test_site_links(
        "Al Jazeera", "https://www.aljazeera.com/news",
        lambda l: "/news/" in l.get("href", "") and "/202" in l.get("href", "") and len(l.get("text", "").strip()) > 15,
        origin="https://www.aljazeera.com",
    )
    test_site_links(
        "NYTimes", "https://www.nytimes.com/section/world",
        lambda l: "/202" in l.get("href", "") and len(l.get("text", "").strip()) > 15,
        origin="https://www.nytimes.com",
        min_articles=5,
        check_content=False,  # Paywalled — article text is minimal
        skip_behavior=True,  # CDP Input events trigger DataDome detection
    )

    # Sites needing CSS selector extraction
    test_site_extract(
        "Guardian", "https://www.theguardian.com/world",
        "a[data-link-name='article']",
        origin="https://www.theguardian.com",
    )
    test_site_extract(
        "CNN", "https://www.cnn.com/world",
        "a[data-link-type='article']",
        origin="https://www.cnn.com",
    )

    # Summary
    total = _passed + _failed
    print(f"\n{'=' * 40}")
    print(f"Results: {_passed}/{total} passed, {_failed} failed")
    if _errors:
        print("\nFailures:")
        for e in _errors:
            print(f"  - {e}")
    print()
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
