"""Web browsing skill - thin CLI client to the browser container API.

Usage:
    python -m istota.skills.browse get "https://example.com" [--keep-session] [--timeout 30]
    python -m istota.skills.browse screenshot "https://example.com" [--output /tmp/shot.png]
    python -m istota.skills.browse extract "https://example.com" --selector "article"
    python -m istota.skills.browse interact <session_id> --click ".button" --fill "#input=value"
    python -m istota.skills.browse close <session_id>

Reads BROWSER_API_URL env var for the container endpoint.
"""

import argparse
import json
import os
import sys

import httpx

DEFAULT_API_URL = "http://localhost:9223"
REQUEST_TIMEOUT = 120.0  # HTTP client timeout (longer than page timeout)


def get_api_url():
    return os.environ.get("BROWSER_API_URL", DEFAULT_API_URL)


def cmd_get(args):
    """Browse a URL and return page content."""
    url = get_api_url()
    payload = {
        "url": args.url,
        "timeout": args.timeout,
        "keep_session": args.keep_session,
    }
    if args.session:
        payload["session_id"] = args.session
    if args.wait_for:
        payload["wait_for"] = args.wait_for

    resp = httpx.post(f"{url}/browse", json=payload, timeout=REQUEST_TIMEOUT)
    return resp.json()


def cmd_screenshot(args):
    """Take a screenshot."""
    url = get_api_url()
    payload = {
        "timeout": args.timeout,
        "full_page": args.full_page,
    }
    if args.url:
        payload["url"] = args.url
    if args.session:
        payload["session_id"] = args.session

    resp = httpx.post(f"{url}/screenshot", json=payload, timeout=REQUEST_TIMEOUT)

    if resp.headers.get("content-type", "").startswith("image/"):
        output = args.output or "/tmp/screenshot.png"
        with open(output, "wb") as f:
            f.write(resp.content)
        return {"status": "ok", "path": output, "size": len(resp.content)}
    else:
        return resp.json()


def cmd_extract(args):
    """Extract content by CSS selector."""
    url = get_api_url()
    payload = {
        "selector": args.selector,
        "timeout": args.timeout,
    }
    if args.url:
        payload["url"] = args.url
    if args.session:
        payload["session_id"] = args.session

    resp = httpx.post(f"{url}/extract", json=payload, timeout=REQUEST_TIMEOUT)
    return resp.json()


def cmd_interact(args):
    """Interact with an existing session."""
    url = get_api_url()
    actions = []

    if args.click:
        for selector in args.click:
            actions.append({"type": "click", "selector": selector})
    if args.fill:
        for fill_spec in args.fill:
            if "=" in fill_spec:
                selector, value = fill_spec.split("=", 1)
                actions.append({"type": "fill", "selector": selector, "value": value})
    if args.scroll:
        actions.append({"type": "scroll", "direction": args.scroll, "amount": args.scroll_amount})

    payload = {
        "session_id": args.session_id,
        "actions": actions,
    }

    resp = httpx.post(f"{url}/interact", json=payload, timeout=REQUEST_TIMEOUT)
    return resp.json()


def cmd_close(args):
    """Close a session."""
    url = get_api_url()
    resp = httpx.delete(f"{url}/sessions/{args.session_id}", timeout=30.0)
    return resp.json()


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.browse",
        description="Web browsing via headless browser container",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # get
    p_get = sub.add_parser("get", help="Browse a URL")
    p_get.add_argument("url", help="URL to browse")
    p_get.add_argument("--keep-session", action="store_true", help="Keep session alive for follow-up")
    p_get.add_argument("--session", help="Reuse existing session ID")
    p_get.add_argument("--timeout", type=int, default=30, help="Navigation timeout in seconds")
    p_get.add_argument("--wait-for", help="CSS selector to wait for after load")

    # screenshot
    p_ss = sub.add_parser("screenshot", help="Take a screenshot")
    p_ss.add_argument("url", nargs="?", help="URL to screenshot")
    p_ss.add_argument("--session", help="Existing session ID")
    p_ss.add_argument("--output", "-o", help="Output file path")
    p_ss.add_argument("--full-page", action="store_true", help="Capture full page")
    p_ss.add_argument("--timeout", type=int, default=30)

    # extract
    p_ext = sub.add_parser("extract", help="Extract content by CSS selector")
    p_ext.add_argument("url", nargs="?", help="URL to extract from")
    p_ext.add_argument("--selector", "-s", required=True, help="CSS selector")
    p_ext.add_argument("--session", help="Existing session ID")
    p_ext.add_argument("--timeout", type=int, default=30)

    # interact
    p_int = sub.add_parser("interact", help="Interact with existing session")
    p_int.add_argument("session_id", help="Session ID")
    p_int.add_argument("--click", action="append", help="CSS selector to click")
    p_int.add_argument("--fill", action="append", help="selector=value to fill")
    p_int.add_argument("--scroll", choices=["up", "down"], help="Scroll direction")
    p_int.add_argument("--scroll-amount", type=int, default=500, help="Scroll pixels")

    # close
    p_close = sub.add_parser("close", help="Close a session")
    p_close.add_argument("session_id", help="Session ID to close")

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    commands = {
        "get": cmd_get,
        "screenshot": cmd_screenshot,
        "extract": cmd_extract,
        "interact": cmd_interact,
        "close": cmd_close,
    }

    try:
        result = commands[args.command](args)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except httpx.ConnectError:
        print(json.dumps({
            "status": "error",
            "error": f"Cannot connect to browser API at {get_api_url()}. Is the container running?",
        }))
        sys.exit(1)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 503:
            print(json.dumps({
                "status": "error",
                "error": "Browser is restarting inside the container. Retry in a few seconds.",
            }))
        else:
            print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
