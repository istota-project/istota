"""Miniflux RSS feed management CLI."""

import argparse
import json
import os
import sys

import httpx


def _client() -> httpx.Client:
    base_url = os.environ.get("MINIFLUX_BASE_URL", "")
    api_key = os.environ.get("MINIFLUX_API_KEY", "")
    if not base_url or not api_key:
        print(json.dumps({"error": "MINIFLUX_BASE_URL and MINIFLUX_API_KEY must be set"}))
        sys.exit(1)
    return httpx.Client(
        base_url=base_url.rstrip("/"),
        headers={"X-Auth-Token": api_key},
        timeout=30.0,
    )


def cmd_list(args):
    with _client() as client:
        resp = client.get("/v1/feeds")
        resp.raise_for_status()
        feeds = resp.json()
        result = [
            {
                "id": f["id"],
                "title": f.get("title", ""),
                "feed_url": f.get("feed_url", ""),
                "site_url": f.get("site_url", ""),
                "category": f.get("category", {}).get("title", ""),
                "parsing_error_count": f.get("parsing_error_count", 0),
            }
            for f in feeds
        ]
        print(json.dumps(result, indent=2))


def cmd_add(args):
    payload = {"feed_url": args.url}
    if args.category:
        # Look up category ID by name, create if needed
        with _client() as client:
            resp = client.get("/v1/categories")
            resp.raise_for_status()
            categories = resp.json()
            cat_id = None
            for cat in categories:
                if cat.get("title", "").lower() == args.category.lower():
                    cat_id = cat["id"]
                    break
            if cat_id is None:
                resp = client.post("/v1/categories", json={"title": args.category})
                resp.raise_for_status()
                cat_id = resp.json()["id"]
            payload["category_id"] = cat_id
            resp = client.post("/v1/feeds", json=payload)
            resp.raise_for_status()
            print(json.dumps(resp.json(), indent=2))
    else:
        with _client() as client:
            resp = client.post("/v1/feeds", json=payload)
            resp.raise_for_status()
            print(json.dumps(resp.json(), indent=2))


def cmd_remove(args):
    with _client() as client:
        resp = client.delete(f"/v1/feeds/{args.id}")
        resp.raise_for_status()
        print(json.dumps({"status": "ok", "removed_feed_id": args.id}))


def cmd_categories(args):
    with _client() as client:
        resp = client.get("/v1/categories")
        resp.raise_for_status()
        print(json.dumps(resp.json(), indent=2))


def cmd_entries(args):
    params = {}
    if args.feed_id:
        params["feed_id"] = args.feed_id
    if args.status:
        params["status"] = args.status
    if args.limit:
        params["limit"] = args.limit
    if args.search:
        params["search"] = args.search
    params.setdefault("limit", 25)
    params["order"] = "published_at"
    params["direction"] = "desc"

    with _client() as client:
        resp = client.get("/v1/entries", params=params)
        resp.raise_for_status()
        data = resp.json()
        entries = data.get("entries", [])
        result = [
            {
                "id": e["id"],
                "title": e.get("title", ""),
                "url": e.get("url", ""),
                "author": e.get("author", ""),
                "status": e.get("status", ""),
                "published_at": e.get("published_at", ""),
                "feed": e.get("feed", {}).get("title", ""),
            }
            for e in entries
        ]
        print(json.dumps({"total": data.get("total", len(result)), "entries": result}, indent=2))


def cmd_refresh(args):
    with _client() as client:
        if args.feed_id:
            resp = client.put(f"/v1/feeds/{args.feed_id}/refresh")
        else:
            resp = client.put("/v1/feeds/refresh")
        resp.raise_for_status()
        print(json.dumps({"status": "ok"}))


def build_parser():
    parser = argparse.ArgumentParser(description="Miniflux feed management")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="List subscribed feeds")

    add_p = sub.add_parser("add", help="Subscribe to a feed")
    add_p.add_argument("--url", required=True, help="Feed URL")
    add_p.add_argument("--category", help="Category name")

    rm_p = sub.add_parser("remove", help="Unsubscribe from a feed")
    rm_p.add_argument("--id", required=True, type=int, help="Feed ID")

    sub.add_parser("categories", help="List categories")

    ent_p = sub.add_parser("entries", help="Fetch entries")
    ent_p.add_argument("--feed-id", type=int, help="Filter by feed ID")
    ent_p.add_argument("--status", choices=["unread", "read", "removed"], help="Filter by status")
    ent_p.add_argument("--limit", type=int, help="Max entries to return")
    ent_p.add_argument("--search", help="Search query")

    ref_p = sub.add_parser("refresh", help="Trigger feed refresh")
    ref_p.add_argument("--feed-id", type=int, help="Specific feed ID (omit for all)")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    commands = {
        "list": cmd_list,
        "add": cmd_add,
        "remove": cmd_remove,
        "categories": cmd_categories,
        "entries": cmd_entries,
        "refresh": cmd_refresh,
    }
    fn = commands.get(args.command)
    if fn:
        fn(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
