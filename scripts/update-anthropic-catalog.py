#!/usr/bin/env python3
"""Regenerate the shipped Anthropic model catalogue.

    uv run scripts/update-anthropic-catalog.py                 # write the file
    uv run scripts/update-anthropic-catalog.py --check         # compare only
    uv run scripts/update-anthropic-catalog.py --write-branch catalogue-refresh

Reads models.dev's ``api.json``, takes the ``anthropic`` models with tool
calling, maps them onto istota's model record, applies the hand-written
``anthropic_overrides.json`` and writes ``src/istota/llm/data/anthropic.json``
sorted by family and release date. This script is the only thing that fetches
models.dev; istota never does at runtime.

Exit codes: 0 done (or, under ``--check``, the committed file is current);
2 under ``--check`` when the committed file differs; 1 on a fetch or
validation failure, in which case nothing is written.

``--write-branch NAME`` commits the regenerated file alone on a new local
branch NAME, built from HEAD with git plumbing so the working tree and index
are untouched. It creates no branch when nothing changed, and never pushes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "src" / "istota" / "llm" / "data"
DEFAULT_OUTPUT = DATA_DIR / "anthropic.json"
DEFAULT_OVERRIDES = DATA_DIR / "anthropic_overrides.json"
MODELS_DEV_URL = "https://models.dev/api.json"

REQUIRED_FAMILIES = ("opus", "sonnet", "haiku")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
# Levels a model supports unless told otherwise; the two above them are
# supported only when named (pi's rule, kept by istota's effort clamp).
BASE_EFFORT_LEVELS = ("low", "medium", "high")

# Every field a shipped record carries, in the order it is written.
RECORD_FIELDS = (
    "id",
    "name",
    "family",
    "released",
    "deprecated",
    "context_window",
    "max_output_tokens",
    "input",
    "reasoning",
    "effort_map",
    "supports_tools",
    "supports_structured_outputs",
    "input_price_per_mtok",
    "output_price_per_mtok",
    "cache_read_price_per_mtok",
    "cache_write_price_per_mtok",
    "price_tiers",
)
# What a model the overrides file adds by hand must state itself.
REQUIRED_FOR_NEW = ("name", "family", "released", "context_window", "max_output_tokens")
_DATED_ID = re.compile(r"-\d{8}\Z")


class CatalogueError(Exception):
    """The source or the overrides cannot produce a valid catalogue."""


def fetch_models_dev(url: str = MODELS_DEV_URL, timeout: float = 30.0) -> dict:
    import httpx

    resp = httpx.get(url, timeout=timeout, follow_redirects=False)
    resp.raise_for_status()
    return resp.json()


def _effort_map(model: dict) -> dict:
    values = None
    for option in model.get("reasoning_options") or []:
        if isinstance(option, dict) and option.get("type") == "effort":
            values = [v for v in option.get("values") or [] if v in EFFORT_LEVELS]
    if values is None:
        return {}
    out: dict = {}
    for level in EFFORT_LEVELS:
        supported = level in values
        if level in BASE_EFFORT_LEVELS and not supported:
            out[level] = None
        elif level not in BASE_EFFORT_LEVELS and supported:
            out[level] = level
    return out


def _price_tiers(cost: dict, model_id: str) -> list[dict]:
    tiers = []
    for raw in cost.get("tiers") or []:
        tier = raw.get("tier") or {}
        if tier.get("type") != "context" or not isinstance(tier.get("size"), int):
            raise CatalogueError(f"{model_id}: price tier of unknown shape {tier!r}")
        tiers.append({
            "input_tokens_above": tier["size"],
            "input_price_per_mtok": float(raw.get("input", 0.0)),
            "output_price_per_mtok": float(raw.get("output", 0.0)),
            "cache_read_price_per_mtok": float(raw.get("cache_read", 0.0)),
            "cache_write_price_per_mtok": float(raw.get("cache_write", 0.0)),
        })
    return sorted(tiers, key=lambda t: t["input_tokens_above"])


def _family(model: dict) -> str | None:
    family = model.get("family")
    if not isinstance(family, str) or not family:
        return None
    return family.removeprefix("claude-")


def map_model(model: dict) -> dict:
    """One models.dev entry as a shipped record."""
    model_id = model["id"]
    limit = model.get("limit") or {}
    cost = model.get("cost") or {}
    modalities = (model.get("modalities") or {}).get("input") or ["text"]
    name = str(model.get("name") or model_id).removesuffix(" (latest)")
    return {
        "id": model_id,
        "name": name,
        "family": _family(model),
        "released": model.get("release_date"),
        "deprecated": model.get("status") == "deprecated",
        "context_window": int(limit.get("context") or 0),
        "max_output_tokens": int(limit.get("output") or 0),
        "input": [m for m in ("text", "image") if m in modalities],
        "reasoning": bool(model.get("reasoning")),
        "effort_map": _effort_map(model),
        "supports_tools": bool(model.get("tool_call")),
        "supports_structured_outputs": bool(model.get("structured_output")),
        "input_price_per_mtok": float(cost.get("input", 0.0)),
        "output_price_per_mtok": float(cost.get("output", 0.0)),
        "cache_read_price_per_mtok": float(cost.get("cache_read", 0.0)),
        "cache_write_price_per_mtok": float(cost.get("cache_write", 0.0)),
        "price_tiers": _price_tiers(cost, model_id),
    }


def build(payload: dict, overrides: dict) -> list[dict]:
    """The catalogue records for a models.dev payload and an overrides document."""
    provider = payload.get("anthropic")
    if not isinstance(provider, dict) or not isinstance(provider.get("models"), dict):
        raise CatalogueError("models.dev payload has no anthropic models")
    source = {
        mid: m for mid, m in provider["models"].items()
        if isinstance(m, dict) and m.get("tool_call")
    }
    # A dated id beside its undated alias is the same model listed twice.
    records = {
        mid: map_model(m) for mid, m in source.items()
        if not (_DATED_ID.search(mid) and _DATED_ID.sub("", mid) in source)
    }

    patches = overrides.get("models", {})
    if not isinstance(patches, dict):
        raise CatalogueError("overrides: 'models' must be an object")
    for mid, patch in patches.items():
        if not isinstance(patch, dict):
            raise CatalogueError(f"overrides: {mid} must be an object")
        unknown = set(patch) - set(RECORD_FIELDS) - {"exclude"}
        if unknown:
            raise CatalogueError(f"overrides: {mid} names unknown field(s) {sorted(unknown)}")
        if patch.get("exclude"):
            records.pop(mid, None)
            continue
        if mid in records:
            records[mid].update(patch)
            continue
        missing = [f for f in REQUIRED_FOR_NEW if f not in patch]
        if missing:
            raise CatalogueError(f"overrides: new model {mid} lacks {missing}")
        record = map_model({"id": mid, "tool_call": True})
        record.update(patch)
        records[mid] = record

    validate(list(records.values()))
    return sorted(
        records.values(),
        key=lambda r: (r["family"] or "", r["released"] or "", r["id"]),
    )


def validate(records: list[dict]) -> None:
    from istota.llm.catalog import build_catalogue, parse_shipped

    for record in records:
        if not record.get("family"):
            raise CatalogueError(f"{record['id']} has no family; set one in the overrides file")
        if record["context_window"] <= 0 or record["max_output_tokens"] <= 0:
            raise CatalogueError(f"{record['id']} has no context window or output limit")
    catalogue = build_catalogue(shipped=parse_shipped({"models": records}))
    for family in REQUIRED_FAMILIES:
        if catalogue.latest(family) is None:
            raise CatalogueError(f"family {family!r} has no current model")


def render(records: list[dict]) -> str:
    ordered = [{k: r[k] for k in RECORD_FIELDS} for r in records]
    document = {
        "_comment": (
            "Generated by scripts/update-anthropic-catalog.py from models.dev and "
            "anthropic_overrides.json. Do not edit by hand; edit the overrides file "
            "and rerun the script."
        ),
        "models": ordered,
    }
    return json.dumps(document, indent=2) + "\n"


def diff_summary(old: list[dict], new: list[dict]) -> tuple[list[str], list[str], list[str]]:
    """``(added, removed, changed)`` ids; ``changed`` entries name their fields."""
    old_by_id = {r["id"]: r for r in old}
    new_by_id = {r["id"]: r for r in new}
    added = sorted(set(new_by_id) - set(old_by_id))
    removed = sorted(set(old_by_id) - set(new_by_id))
    changed = []
    for mid in sorted(set(old_by_id) & set(new_by_id)):
        fields = sorted(
            k for k in RECORD_FIELDS if old_by_id[mid].get(k) != new_by_id[mid].get(k)
        )
        if fields:
            changed.append(f"{mid} ({', '.join(fields)})")
    return added, removed, changed


def _print_summary(added, removed, changed) -> None:
    if not (added or removed or changed):
        print("anthropic catalogue: no change")
        return
    for mid in added:
        print(f"  + {mid}")
    for mid in removed:
        print(f"  - {mid}")
    for entry in changed:
        print(f"  ~ {entry}")


def _records_of(text: str | None) -> list[dict]:
    if not text:
        return []
    return json.loads(text).get("models", [])


def run_git(args: list[str], *, cwd: Path, env: dict | None = None, stdin: str | None = None) -> str:
    """Run one git command; every git call in this script goes through here."""
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=env, input=stdin,
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def commit_subject(added, removed, changed) -> str:
    parts = [f"+{m}" for m in added] + [f"-{m}" for m in removed]
    if not parts:
        parts = [f"~{entry.split(' ', 1)[0]}" for entry in changed]
    return f"llm: refresh the anthropic catalogue ({', '.join(parts)})"


def write_branch(branch: str, text: str, output: Path) -> int:
    repo = Path(run_git(["rev-parse", "--show-toplevel"], cwd=output.parent))
    rel = output.resolve().relative_to(repo.resolve()).as_posix()
    try:
        committed = run_git(["show", f"HEAD:{rel}"], cwd=repo) + "\n"
    except subprocess.CalledProcessError:
        committed = None
    added, removed, changed = diff_summary(_records_of(committed), _records_of(text))
    if committed == text:
        print("anthropic catalogue: no change; no branch created")
        return 0
    try:
        run_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=repo)
    except subprocess.CalledProcessError:
        pass
    else:
        print(f"branch {branch} already exists", file=sys.stderr)
        return 1

    blob = run_git(["hash-object", "-w", "--stdin"], cwd=repo, stdin=text)
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        run_git(["read-tree", "HEAD"], cwd=repo, env=env)
        run_git(["update-index", "--add", "--cacheinfo", f"100644,{blob},{rel}"], cwd=repo, env=env)
        tree = run_git(["write-tree"], cwd=repo, env=env)
    subject = commit_subject(added, removed, changed)
    commit = run_git(["commit-tree", tree, "-p", "HEAD", "-m", subject], cwd=repo)
    run_git(["branch", branch, commit], cwd=repo)
    _print_summary(added, removed, changed)
    print(f"committed {commit[:12]} on {branch}: {subject}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="compare with the committed file; write nothing")
    mode.add_argument("--write-branch", metavar="NAME",
                      help="commit the regenerated file alone on a new local branch")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--overrides", type=Path, default=DEFAULT_OVERRIDES)
    parser.add_argument("--models-dev-file", type=Path,
                        help="read models.dev's api.json from a file instead of fetching it")
    args = parser.parse_args(argv)

    try:
        if args.models_dev_file:
            payload = json.loads(args.models_dev_file.read_text())
        else:
            payload = fetch_models_dev()
        overrides = json.loads(args.overrides.read_text()) if args.overrides.exists() else {}
        records = build(payload, overrides)
    except CatalogueError as exc:
        print(f"anthropic catalogue: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — any fetch or parse failure is exit 1
        print(f"anthropic catalogue: could not build ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 1

    text = render(records)
    current = args.output.read_text() if args.output.exists() else None
    summary = diff_summary(_records_of(current), records)

    if args.check:
        if current == text:
            print("anthropic catalogue: current")
            return 0
        _print_summary(*summary)
        print("anthropic catalogue is out of date; run: uv run scripts/update-anthropic-catalog.py")
        return 2
    if args.write_branch:
        return write_branch(args.write_branch, text, args.output)

    _print_summary(*summary)
    if current != text:
        args.output.write_text(text)
        print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
