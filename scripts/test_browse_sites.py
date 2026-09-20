"""Integration test for browse skill against live browser container.

Runs directly on the machine with the browser container (no istota deps).

Usage:
    ssh <production-host> 'python3 -' < scripts/test_browse_sites.py
    # Or locally if container is reachable:
    BROWSER_API_URL=http://127.0.0.1:9223 python3 scripts/test_browse_sites.py
"""

import json
import os
import struct
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
    # /health degrades on a wedged CDP binding as well as a dead Chrome
    # (ISSUE-384), and that is the failure this whole script is least able to
    # diagnose from its own output — every verb below returns the same generic
    # error. Report the counters so the run says which of the two it hit.
    check(
        "cdp binding healthy",
        r.get("cdp_healthy", True) is True,
        f"{r.get('cdp_consecutive_failures', 0)} consecutive CDP failures: "
        f"{r.get('cdp_last_error', '')}",
    )


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
    articles = [link for link in links if article_filter(link)]
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



# -- Visual mode ------------------------------------------------------------
#
# The one thing the unit tests cannot prove: that a point read off the
# delivered picture reaches the DOM element that was under it. Every other
# test of this feature drives a stubbed page object, so the arithmetic is
# pinned against a recorded frame and nothing has ever clicked a page.
#
# The page is built here rather than found, so the answer is a cell name rather
# than an inference about a site's markup. `window.__hit` is written by the
# cell's own click handler, so a click that landed elsewhere leaves it null or
# names a different cell -- it cannot pass by accident.

TARGET_CELL = "r1c2"

GRID_PAGE = """(() => {
  const cols = 4, rows = 3;
  document.body.innerHTML = '';
  document.body.style.margin = '0';
  window.__hit = null;
  const grid = document.createElement('div');
  grid.style.cssText =
    'display:grid;grid-template-columns:repeat(4,1fr);' +
    'grid-template-rows:repeat(3,140px);';
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const cell = document.createElement('div');
      const name = 'r' + r + 'c' + c;
      cell.textContent = name;
      cell.style.cssText =
        'display:flex;align-items:center;justify-content:center;' +
        'box-sizing:border-box;border:1px solid #333;font:24px sans-serif;';
      cell.addEventListener('click', () => {
        window.__hit = name;
      });
      grid.appendChild(cell);
    }
  }
  document.body.appendChild(grid);
  const target = grid.children[1 * cols + 2].getBoundingClientRect();
  return {
    target: 'r1c2',
    x: target.x + target.width / 2,
    y: target.y + target.height / 2,
    viewport: [window.innerWidth, window.innerHeight],
  };
})()"""


def api_bytes(endpoint, data):
    """A POST whose body is not JSON: returns `(bytes, headers)`.

    `/screenshot` answers with a PNG and puts the capture frame on a response
    header, so the coordinate case needs both halves and `api()` returns
    neither.
    """
    req = urllib.request.Request(
        API + endpoint,
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return None, dict(e.headers or {}, error=e.read().decode("utf-8", "replace")[:300])
    except Exception as e:
        return None, {"error": str(e)}


def png_size(data):
    """`(width, height)` out of a PNG's IHDR, or None. Five lines, no library."""
    if not data or len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return struct.unpack(">II", data[16:24])


def _click_at(sid, x, y, image_size):
    result = api("/interact", {
        "session_id": sid,
        "actions": [{
            "type": "click_at", "x": x, "y": y, "image_size": list(image_size),
        }],
    })
    return (result.get("actions") or [{}])[0]


def test_visual_coordinates():
    print("\n=== Visual mode: a point read off the picture ===")

    opened = api("/browse", {
        "url": "https://example.com",
        "keep_session": True,
        "skip_behavior": True,
    })
    sid = opened.get("session_id")
    check("visual: session opened", bool(sid), json.dumps(opened)[:200])
    if not sid:
        return

    try:
        built = api("/evaluate", {"session_id": sid, "expression": GRID_PAGE})
        target = built.get("result") or {}
        check(
            "visual: grid page built",
            target.get("target") == TARGET_CELL,
            json.dumps(built)[:300],
        )
        if target.get("target") != TARGET_CELL:
            return

        png, headers = api_bytes("/screenshot", {"session_id": sid})
        check(
            "visual: screenshot returned a PNG",
            bool(png) and png[:8] == b"\x89PNG\r\n\x1a\n",
            str(headers)[:300],
        )
        raw = headers.get("X-Browse-Capture")
        check(
            "visual: the capture frame came back on a header",
            bool(raw),
            headers.get("X-Browse-Capture-Error", "no header at all"),
        )
        if not png or not raw:
            return

        record = json.loads(raw)
        image_w, image_h = record["image"]
        # The header has to describe the bytes it came with. If it does not,
        # every conversion below is against a picture nobody was shown.
        check(
            "visual: the header agrees with the PNG's own IHDR",
            (image_w, image_h) == png_size(png),
            f"header {record['image']}, IHDR {png_size(png)}",
        )
        print(
            f"  note  window {record['window']}, capture {image_w}x{image_h}, "
            f"offset {record['offset']}, viewport {target['viewport']}"
        )

        # The cell centre is in CSS pixels; the picture is the capture. At dpr
        # 1 with no scrollbar these are the same number, and the ratio is what
        # makes the case honest when they are not.
        view_w, view_h = target["viewport"]
        point = (target["x"] * image_w / view_w, target["y"] * image_h / view_h)

        action = _click_at(sid, point[0], point[1], (image_w, image_h))
        check("visual: the click was not refused", action.get("ok") is True,
              json.dumps(action)[:300])
        hit = api("/evaluate", {"session_id": sid, "expression": "window.__hit"})
        check(
            f"visual: the click landed on {TARGET_CELL}",
            hit.get("result") == TARGET_CELL,
            f"screen {action.get('screen')}, hit {hit.get('result')!r}",
        )

        # A point on a downscaled picture, which is what a vision provider's
        # envelope makes of a capture over about 1.15 megapixels. At the
        # shipped screen size the full-size conversion above is the identity,
        # so this is the leg that shows the scaling doing real work.
        api("/evaluate", {"session_id": sid, "expression": "window.__hit = null"})
        half = (max(1, image_w // 2), max(1, image_h // 2))
        action = _click_at(sid, point[0] / 2, point[1] / 2, half)
        hit = api("/evaluate", {"session_id": sid, "expression": "window.__hit"})
        check(
            f"visual: a half-size picture still lands on {TARGET_CELL}",
            hit.get("result") == TARGET_CELL,
            f"image_size {half}, screen {action.get('screen')}, hit {hit.get('result')!r}",
        )

        # A capture that no longer describes the page is refused rather than
        # clicked. Both halves are asserted: the named refusal, and that
        # nothing was pressed -- a check that refused everything would pass
        # the first for the wrong reason, and the two clicks above are what
        # show it does not.
        api("/evaluate", {"session_id": sid, "expression": (
            "window.__hit = null;"
            "document.body.style.height = '4000px';"
            "window.scrollTo(0, 400); 1"
        )})
        action = _click_at(sid, point[0], point[1], (image_w, image_h))
        check(
            "visual: a scrolled page refuses with stale_capture",
            action.get("ok") is False and action.get("error") == "stale_capture",
            json.dumps(action)[:300],
        )
        hit = api("/evaluate", {"session_id": sid, "expression": "window.__hit"})
        check("visual: the refused click pressed nothing", hit.get("result") is None,
              f"hit {hit.get('result')!r}")

        # A full-page capture is a different coordinate space from the one the
        # pointer acts in, and is excluded by construction.
        api("/evaluate", {"session_id": sid, "expression": "window.scrollTo(0, 0); 1"})
        api_bytes("/screenshot", {"session_id": sid, "full_page": True})
        action = _click_at(sid, 10, 10, (image_w, image_h))
        check(
            "visual: a full-page capture is not clickable",
            action.get("ok") is False and action.get("error") == "full_page_capture",
            json.dumps(action)[:300],
        )
    finally:
        close_session(sid)

def _summary():
    total = _passed + _failed
    print(f"\n{'=' * 40}")
    print(f"Results: {_passed}/{total} passed, {_failed} failed")
    if _errors:
        print("\nFailures:")
        for e in _errors:
            print(f"  - {e}")
    print()
    sys.exit(1 if _failed else 0)


def main():
    print(f"Browser API: {API}")

    # One suite at a time, so a failure in the visual case is attributable to
    # the visual case rather than to whichever news site was slow today.
    # `BROWSE_TEST_ONLY=visual` is what the integration driver passes.
    only = os.environ.get("BROWSE_TEST_ONLY", "").strip()
    if only:
        suites = {
            "health": test_health,
            "session": test_session_lifecycle,
            "visual": test_visual_coordinates,
        }
        if only not in suites:
            print(f"Unknown BROWSE_TEST_ONLY={only!r}; known: {sorted(suites)}")
            sys.exit(2)
        suites[only]()
        _summary()
        return

    test_health()
    test_session_lifecycle()

    # Sites with articles in links array
    test_site_links(
        "AP News", "https://apnews.com/hub/world-news",
        lambda link: "/article/" in link.get("href", "") and len(link.get("text", "").strip()) > 15,
        origin="https://apnews.com",
    )
    test_site_links(
        "BBC", "https://www.bbc.com/news/world",
        lambda link: "/news/articles/" in link.get("href", "") and len(link.get("text", "").strip()) > 15,
        origin="https://www.bbc.com",
    )
    test_site_links(
        "Al Jazeera", "https://www.aljazeera.com/news",
        lambda link: "/news/" in link.get("href", "") and "/202" in link.get("href", "") and len(link.get("text", "").strip()) > 15,
        origin="https://www.aljazeera.com",
    )
    test_site_links(
        "NYTimes", "https://www.nytimes.com/section/world",
        lambda link: "/202" in link.get("href", "") and len(link.get("text", "").strip()) > 15,
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

    test_visual_coordinates()

    _summary()


if __name__ == "__main__":
    main()
