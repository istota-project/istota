#!/usr/bin/env python3
"""Does the vendored `docker/browser/` match its source of truth?

`docker/browser/` is a vendored copy of `browser/` from the stealth-browser
repository, kept in step by an rsync with `--delete`. So a change that reaches
istota and not stealth-browser is reverted by the next sync, silently and with
nothing in either history to say it happened.

**It compares what is published, not the working trees, and that is the whole
point of the script.** The obvious check -- diff one checkout against the
other -- answers "do these two trees agree", which is not the question. The
rsync runs from whatever a future clone fetches, so the hazard turns on
`origin/main`; the two answers diverge exactly when somebody has committed and
not yet pushed, which is an ordinary mid-work state. Measured on 2026-09-20:
two sessions independently wrote a parity checker, both read local refs, both
reported identical, and the published trees differed in three files with
istota ahead.

Direction decides severity, so it is reported rather than reduced to a
boolean:

  stealth-browser ahead  ordinary. Somebody landed there and has not vendored
                         it yet; the next sync brings it across. Exit 0.
  istota ahead           the dangerous one. The next `rsync --delete` reverts
                         it. Exit 1.

Finding the source repository, in order: `--source`, `$STEALTH_BROWSER_REPO`,
then `../stealth-browser` beside this checkout. It is a private repository, so
there is no default anybody else can use, and a missing one is skipped rather
than failed -- this runs on machines that have no copy of it.

    scripts/check-vendored-parity.py
    scripts/check-vendored-parity.py --source ~/src/stealth-browser
    scripts/check-vendored-parity.py --ref main   # compare local refs instead
"""

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

#: Where the copy lives on each side.
ISTOTA_DIR = "docker/browser"
SOURCE_DIR = "browser"

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_SKIPPED = 0
EXIT_ERROR = 2


def git(repo, *args, check=True):
    """Run git in `repo`, returning stdout as bytes."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} in {repo}: "
            f"{result.stderr.decode(errors='replace').strip()}"
        )
    return result.stdout


def blob(repo, ref, path):
    """The bytes of `path` at `ref`, or None where it does not exist."""
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{path}"], capture_output=True,
    )
    return result.stdout if result.returncode == 0 else None


def tracked_files(repo, ref, prefix):
    """Every file under `prefix` at `ref`, as paths relative to it.

    Both sides are enumerated and unioned by the caller. Listing only the
    source's files cannot see a file that exists solely in the vendored copy
    -- which is the shape the 2026-06-10 incident in the project notes took,
    and exactly what a `--delete` sync removes.
    """
    out = git(repo, "ls-tree", "-r", "--name-only", ref, f"{prefix}/")
    names = out.decode().split("\n")
    return sorted(
        n[len(prefix) + 1:] for n in names if n.startswith(prefix + "/")
    )


def unpushed(repo, local, remote):
    """How many commits `local` is ahead of `remote`, and their subjects."""
    count = git(repo, "rev-list", "--count", f"{remote}..{local}").decode().strip()
    if count == "0":
        return 0, []
    log = git(repo, "log", "--oneline", f"{remote}..{local}").decode().strip()
    return int(count), log.split("\n") if log else []


def resolve_source(explicit, istota_root):
    for candidate in (
        explicit,
        os.environ.get("STEALTH_BROWSER_REPO"),
        istota_root.parent / "stealth-browser",
    ):
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if (path / ".git").exists():
            return path
    return None


def main():
    parser = argparse.ArgumentParser(
        description="Check docker/browser/ against its source of truth.",
    )
    parser.add_argument("--source", help="path to the stealth-browser checkout")
    parser.add_argument(
        "--target",
        help="path to the istota checkout to check (default: the one this "
             "script lives in, which is what you want outside a test)",
    )
    parser.add_argument(
        "--ref", default="origin/main",
        help="ref to compare on both sides (default: origin/main, which is "
             "what a sync actually runs from)",
    )
    parser.add_argument(
        "--no-fetch", action="store_true",
        help="skip the fetch; a stale origin/main answers the wrong question",
    )
    args = parser.parse_args()

    # Resolved from the script's own location rather than the cwd, so it
    # always checks the checkout it belongs to. `--target` exists so the
    # tests can build a pair of throwaway repositories and make them drift;
    # without it they would silently measure the developer's real one.
    anchor = Path(args.target).expanduser() if args.target else Path(__file__).resolve().parent
    try:
        istota_root = Path(
            git(anchor, "rev-parse", "--show-toplevel").decode().strip()
        )
    except RuntimeError as e:
        print(f"not a git checkout: {e}")
        return EXIT_ERROR
    source_root = resolve_source(args.source, istota_root)
    if source_root is None:
        print("stealth-browser checkout not found -- skipping the parity check.")
        print("Point at it with --source or $STEALTH_BROWSER_REPO.")
        return EXIT_SKIPPED

    print(f"istota          {istota_root}")
    print(f"stealth-browser {source_root}")
    print(f"comparing       {args.ref}\n")

    if not args.no_fetch and args.ref.startswith("origin/"):
        for repo in (istota_root, source_root):
            subprocess.run(["git", "-C", str(repo), "fetch", "origin", "--quiet"],
                           capture_output=True)

    try:
        source_names = tracked_files(source_root, args.ref, SOURCE_DIR)
        istota_names = tracked_files(istota_root, args.ref, ISTOTA_DIR)
    except RuntimeError as e:
        print(f"could not read {args.ref}: {e}")
        return EXIT_ERROR
    names = sorted(set(source_names) | set(istota_names))
    if not names:
        print(f"no files under {SOURCE_DIR}/ at {args.ref} -- nothing to compare.")
        return EXIT_ERROR

    only_istota, only_source, differ = [], [], []
    for rel in names:
        here = blob(istota_root, args.ref, f"{ISTOTA_DIR}/{rel}")
        there = blob(source_root, args.ref, f"{SOURCE_DIR}/{rel}")
        if here is None:
            only_source.append(rel)
        elif there is None:
            only_istota.append(rel)
        elif hashlib.sha256(here).digest() != hashlib.sha256(there).digest():
            differ.append(rel)

    for rel in differ:
        print(f"  DIFFER            {rel}")
    for rel in only_istota:
        print(f"  ONLY-IN-ISTOTA    {rel}")
    for rel in only_source:
        print(f"  ONLY-IN-SOURCE    {rel}")

    print(f"\n{len(names)} files compared, "
          f"{len(differ) + len(only_istota) + len(only_source)} out of step")

    # An unpushed commit on either side is what makes the published trees
    # disagree while both working trees look identical, so it is reported even
    # when every file matches -- it is the thing that is about to break parity.
    exit_code = EXIT_OK
    if args.ref.startswith("origin/"):
        local = args.ref.split("/", 1)[1]
        for label, repo in (("istota", istota_root),
                            ("stealth-browser", source_root)):
            try:
                count, subjects = unpushed(repo, local, args.ref)
            except RuntimeError:
                continue
            if count:
                print(f"\n{label}: {count} commit(s) on {local} not pushed to "
                      f"{args.ref} --")
                for line in subjects:
                    print(f"    {line}")
                print("  parity above describes what is published, not what "
                      "you have locally.")

    if only_istota or (differ and _istota_is_ahead(
        istota_root, source_root, args.ref, differ,
    )):
        print("\nistota is ahead: the next rsync --delete from the source "
              "repository reverts this. Land it in stealth-browser.")
        exit_code = EXIT_DRIFT
    elif differ or only_source:
        print("\nstealth-browser is ahead, which is the ordinary state "
              "between a change landing there and being vendored here.")
    else:
        print("\nin step.")
    return exit_code


def _seen_in_history(repo, ref, prefix, rel, wanted, depth=80):
    """Has `repo` ever held `wanted` as the content of this path?

    The question both direction tests rest on: if one side's *current*
    content appears somewhere in the other side's history for the same path,
    the other side has moved past it, and so is ahead.
    """
    revs = git(
        repo, "log", "--format=%H", "-n", str(depth), ref, "--", f"{prefix}/{rel}",
    ).decode().split()
    digest = hashlib.sha256(wanted).digest()
    for rev in revs:
        found = blob(repo, rev, f"{prefix}/{rel}")
        if found is not None and hashlib.sha256(found).digest() == digest:
            return True
    return False


def _istota_is_ahead(istota_root, source_root, ref, differ):
    """Does the vendored copy carry content the source has never published?

    A file differing says nothing about which way, and getting the direction
    backwards is worse than not reporting one -- it tells a reader the
    dangerous state is the safe one. So both directions are asked
    independently rather than one being inferred from the other:

      the source's content is somewhere in istota's history
          -> istota moved past it -> istota is ahead
      istota's content is somewhere in the source's history
          -> the source moved past it -> the source is ahead

    Neither is a genuine divergence and is reported as istota-ahead, because
    that is the direction with silent data loss behind it. Both at once means
    the two have swapped content back and forth, which is also worth the
    louder answer.
    """
    for rel in differ:
        here = blob(istota_root, ref, f"{ISTOTA_DIR}/{rel}")
        there = blob(source_root, ref, f"{SOURCE_DIR}/{rel}")
        if here is None or there is None:
            return True
        istota_moved_past = _seen_in_history(
            istota_root, ref, ISTOTA_DIR, rel, there,
        )
        source_moved_past = _seen_in_history(
            source_root, ref, SOURCE_DIR, rel, here,
        )
        if istota_moved_past and not source_moved_past:
            return True
        if not istota_moved_past and not source_moved_past:
            return True
    return False


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
