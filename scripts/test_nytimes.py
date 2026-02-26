"""NYTimes-specific browse test against live browser container.

Tests that we can load an NYTimes index page and navigate to an individual
article without being blocked by DataDome bot detection.

Runs directly on the machine with the browser container (no istota deps).

Usage:
    ssh your-server 'python3 -' < scripts/test_nytimes.py
    # Or locally if container is reachable:
    BROWSER_API_URL=http://127.0.0.1:9223 python3 scripts/test_nytimes.py
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


def main():
    print(f"Browser API: {API}")
    print("\n=== NYTimes ===")

    # Step 1: Load index page (skip_behavior to avoid DataDome CDP detection)
    r = api("/browse", {
        "url": "https://www.nytimes.com/section/world",
        "keep_session": True,
        "timeout": 30,
        "skip_behavior": True,
    })
    sid = r.get("session_id", "")
    status = r.get("status", "error")

    check("index page loads", status == "ok", r.get("error", ""))
    if status != "ok":
        # Print page text snippet for debugging blocked responses
        text = r.get("text", "")[:500]
        if text:
            print(f"    Page text: {text[:200]}")
        close_session(sid)
        sys.exit(1)

    check("not captcha", r.get("status") != "captcha", "captcha detected")

    # Step 2: Find article links
    links = r.get("links", [])
    articles = [
        l for l in links
        if "/202" in l.get("href", "") and len(l.get("text", "").strip()) > 15
    ]
    check("found articles on index", len(articles) >= 5, f"got {len(articles)}")

    if articles:
        print(f"    First article: {articles[0]['text'][:80]}")
        print(f"    URL: {articles[0]['href'][:100]}")

    if not articles:
        close_session(sid)
        sys.exit(1)

    # Step 3: Navigate to the first article
    # Use wait_for to let JS render the article content (DataDome challenge + paywall)
    href = articles[0]["href"]
    if href.startswith("/"):
        full_url = "https://www.nytimes.com" + href
    else:
        full_url = href

    r2 = api("/browse", {
        "url": full_url,
        "session_id": sid,
        "timeout": 30,
        "skip_behavior": True,
        "wait_for": "article",
    })

    check("article page loads", r2.get("status") == "ok", r2.get("error", ""))
    check("not captcha on article", r2.get("status") != "captcha", "captcha detected")
    check("session preserved", r2.get("session_id") == sid)

    # NYTimes articles are paywalled, but we should still get some content
    # (headline, lead paragraph, metadata). Even paywalled pages have > 200 chars.
    text = r2.get("text", "")
    text_len = len(text)
    check("article has content", text_len > 200, f"got {text_len} chars")

    if text_len > 0:
        # Show first bit of article text for verification
        preview = text[:200].replace("\n", " ").strip()
        print(f"    Article preview: {preview}...")

    close_session(sid)

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
