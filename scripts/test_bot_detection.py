"""Bot detection test for browse skill against live browser container.

Checks our browser setup against known bot detection frameworks:
- deviceandbrowserinfo.com/are_you_a_bot
- pixelscan.net/bot-check
- browserscan.net/bot-detection
- bot-detector.rebrowser.net (10-test CDP detection suite)

Runs directly on the machine with the browser container (no istota deps).

Usage:
    ssh your-server 'python3 -' < scripts/test_bot_detection.py
    # Or locally if container is reachable:
    BROWSER_API_URL=http://127.0.0.1:9223 python3 scripts/test_bot_detection.py
"""

import json
import os
import sys
import time
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


def evaluate(sid, expression):
    r = api("/evaluate", {"session_id": sid, "expression": expression})
    if r.get("status") == "ok":
        return r.get("result")
    return None


# -- Test suites --


def test_deviceandbrowserinfo():
    """https://deviceandbrowserinfo.com/are_you_a_bot"""
    print("\n=== deviceandbrowserinfo.com ===")
    r = api("/browse", {
        "url": "https://deviceandbrowserinfo.com/are_you_a_bot",
        "keep_session": True,
        "timeout": 30,
    })
    sid = r.get("session_id", "")
    check("fetch ok", r.get("status") == "ok", r.get("error", ""))
    if r.get("status") != "ok":
        close_session(sid)
        return

    time.sleep(3)

    # The page shows a result card with "You are human!" or "You are a bot!"
    verdict = evaluate(sid, """
        (() => {
            const card = document.querySelector('.result-card');
            return card ? card.textContent.trim() : document.body.innerText.substring(0, 500);
        })()
    """) or r.get("text", "")[:500]

    print(f"    Verdict: {verdict}")
    check("detected as human", "human" in verdict.lower(), verdict[:200])

    close_session(sid)


def test_pixelscan():
    """https://pixelscan.net/bot-check"""
    print("\n=== pixelscan.net ===")
    r = api("/browse", {
        "url": "https://pixelscan.net/bot-check",
        "keep_session": True,
        "timeout": 30,
    })
    sid = r.get("session_id", "")
    check("fetch ok", r.get("status") == "ok", r.get("error", ""))
    if r.get("status") != "ok":
        close_session(sid)
        return

    time.sleep(5)

    # Pixelscan shows results like "NavigatorResult:PassNormal" in elements
    # with class containing "result", "test", "check", "status"
    results = evaluate(sid, """
        (() => {
            const items = document.querySelectorAll(
                '[class*="result"], [class*="test"], [class*="check"], [class*="status"]'
            );
            const checks = [];
            items.forEach(el => {
                const text = el.textContent.trim();
                if (text && text.length < 200) checks.push(text);
            });
            return checks;
        })()
    """)

    if results:
        # Parse "NavigatorResult:PassNormal" style entries
        seen = set()
        for entry in results:
            if "Result:" not in entry:
                continue
            name, _, verdict = entry.partition("Result:")
            verdict = verdict.strip()
            if name in seen:
                continue
            seen.add(name)
            print(f"    {name}: {verdict}")
            check(name, verdict.lower().startswith("pass"), verdict)

        if not seen:
            # Fallback: just check page text
            body = evaluate(sid, "document.body.innerText.substring(0, 2000)") or ""
            check("not detected as bot", "fail" not in body.lower()[:500])
    else:
        check("got results", False, "evaluate returned None")

    close_session(sid)


def test_browserscan():
    """https://www.browserscan.net/bot-detection"""
    print("\n=== browserscan.net ===")
    r = api("/browse", {
        "url": "https://www.browserscan.net/bot-detection",
        "keep_session": True,
        "timeout": 30,
    })
    sid = r.get("session_id", "")
    check("fetch ok", r.get("status") == "ok", r.get("error", ""))
    if r.get("status") != "ok":
        close_session(sid)
        return

    time.sleep(5)

    # Browserscan shows "Test Results: Normal" at the top, and individual
    # checks for Webdriver, User-Agent, CDP, Navigator
    results = evaluate(sid, """
        (() => {
            const body = document.body.innerText;
            // Extract the overall verdict
            const verdict_match = body.match(/Test Results:\\s*(\\w+)/);
            const verdict = verdict_match ? verdict_match[1] : null;

            // Extract individual check results from the structured area
            // The page shows check names followed by their status
            const checks = {};
            const check_names = ['Webdriver', 'User-Agent', 'CDP', 'Navigator'];
            check_names.forEach(name => {
                // Look for the check name in the page text
                const idx = body.indexOf(name);
                if (idx !== -1) {
                    // The status appears near the check name in the rendered text
                    const surrounding = body.substring(Math.max(0, idx - 50), idx + 100);
                    if (surrounding.includes('Normal')) {
                        checks[name] = 'Normal';
                    } else if (surrounding.includes('Abnormal')) {
                        checks[name] = 'Abnormal';
                    }
                }
            });
            return {verdict: verdict, checks: checks};
        })()
    """)

    if results:
        verdict = results.get("verdict")
        if verdict:
            print(f"    Overall: {verdict}")
            check("overall verdict", verdict.lower() == "normal", verdict)

        checks = results.get("checks", {})
        for name, status in checks.items():
            print(f"    {name}: {status}")
            check(name, status.lower() == "normal", status)

        if not verdict and not checks:
            body = evaluate(sid, "document.body.innerText.substring(0, 2000)") or ""
            check("not detected as bot", "abnormal" not in body.lower()[:500])
    else:
        check("got results", False, "evaluate returned None")

    close_session(sid)


def test_rebrowser():
    """https://bot-detector.rebrowser.net/"""
    print("\n=== bot-detector.rebrowser.net ===")
    r = api("/browse", {
        "url": "https://bot-detector.rebrowser.net/",
        "keep_session": True,
        "timeout": 30,
    })
    sid = r.get("session_id", "")
    check("fetch ok", r.get("status") == "ok", r.get("error", ""))
    if r.get("status") != "ok":
        close_session(sid)
        return

    # The page runs 10 detection tests asynchronously — wait for results
    time.sleep(8)

    # Extract structured test results from the page.
    # Each test row is a <tr> with a <span> containing an emoji + test name:
    #   🟢 = pass, 🔴 = fail, ⚪️ = not triggered (safe)
    results = evaluate(sid, """
        (() => {
            const testNames = [
                'runtimeEnableLeak', 'sourceUrlLeak', 'mainWorldExecution',
                'navigatorWebdriver', 'bypassCsp', 'viewport',
                'dummyFn', 'useragent', 'pwInitScripts', 'exposeFunctionLeak'
            ];
            const tests = {};
            const rows = document.querySelectorAll('tr');
            for (const row of rows) {
                const span = row.querySelector('span.text-nowrap');
                if (!span) continue;
                const text = span.textContent.trim();
                // Find which test name this row contains
                let name = null;
                for (const t of testNames) {
                    if (text.includes(t)) { name = t; break; }
                }
                if (!name) continue;
                // Determine status from emoji prefix
                let status;
                const codePoint = text.codePointAt(0);
                if (codePoint === 0x1F7E2) {       // 🟢
                    status = 'pass';
                } else if (codePoint === 0x1F534) { // 🔴
                    status = 'fail';
                } else if (codePoint === 0x26AA) {  // ⚪
                    status = 'not_triggered';
                } else {
                    status = 'unknown';
                }
                // Get the notes column for detail
                const tds = row.querySelectorAll('td');
                const notes = tds.length >= 3 ? tds[2].textContent.trim().substring(0, 200) : '';
                tests[name] = {status: status, notes: notes};
            }
            return tests;
        })()
    """)

    if not results:
        check("got results", False, "evaluate returned None")
        close_session(sid)
        return

    if not results:
        check("got results", False, "no test rows found on page")
        close_session(sid)
        return

    for name, info in sorted(results.items()):
        status = info.get("status", "unknown")
        notes = info.get("notes", "")
        # not_triggered means the test requires specific API usage (exposeFunction,
        # main world execution, etc.) that Patchright avoids — this is a pass
        is_ok = status in ("pass", "not_triggered")
        label = status
        if notes:
            label += f" — {notes[:100]}"
        print(f"    {name}: {label}")
        check(f"rebrowser:{name}", is_ok, label)

    close_session(sid)


def main():
    print(f"Browser API: {API}")
    test_deviceandbrowserinfo()
    test_pixelscan()
    test_browserscan()
    test_rebrowser()

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
