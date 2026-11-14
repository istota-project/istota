"""`istota-skill schedules check`: the scheduler's own reading of the user's CRON.md (#959).

The model edits CRON.md with its file tools and had no way to ask whether the
scheduler would take what it wrote. A file that parses as TOML can still lose a
field, or a whole job, to the loader's rules: a `skill:` egress entry naming a
`local` skill was read back as fine and dropped at sync. So `check` runs
`cron_loader.cron_document_errors`, the validator the web editor uses, on the
file as it is on disk. The one thing it adds is the sync's own reading of a file
with no jobs in a fence (`CronDocument.is_template`), which the web editor never
writes and a model can.

Runs host-side through the skill proxy, scoped by ``ISTOTA_USER_ID``. A turn
that withholds the user's files does not reach the file, so it is refused here
as well as off the menu.
"""

from istota.skills._cli import error_envelope, parse_and_resolve, run_skill_cli


def cmd_check(args):
    import os

    from istota import cron_loader, storage
    from istota.config import load_config
    from istota.sandbox.host_paths import withheld_from_env

    user_id = os.environ.get("ISTOTA_USER_ID", "")
    if not user_id:
        return error_envelope("ISTOTA_USER_ID not set")
    if withheld_from_env() & {"files", "schedules"}:
        return error_envelope(
            "this turn cannot read the user's CRON.md", reason="scope_withheld",
        )

    config = load_config()
    content = storage.read_user_config_file(config, user_id, "CRON.md")
    if content is None:
        return error_envelope(
            "CRON.md could not be read: it is not a regular UTF-8 file in the "
            "user's config folder",
            reason="unreadable",
        )

    # One parse for both answers. Its errors are `cron_document_errors`' answer.
    doc, problems = (
        cron_loader._parse_cron_document(content, config, user_id) if content else (None, [])
    )
    if doc is not None and doc.is_template and content.strip():
        # No problem to the web editor, which always writes a fence. To the sync it is a
        # file stating no jobs, which it rewrites from the table when there are rows.
        problems = [
            "CRON.md has no jobs in a ```toml block: the block is missing or holds only "
            "comments, so the scheduler reads no jobs from it, and if the user already has "
            "jobs it rewrites the file from them"
        ]
    skills = cron_loader._approvable_skills(config, user_id)
    result = {
        "exists": bool(content),
        "jobs": [job.name for job in doc.jobs] if doc is not None else [],
        "problems": problems,
        # What a `skill:` entry in `egress` may name; None when it cannot be read now.
        "egress_skills": sorted(skills) if skills is not None else None,
    }
    if problems:
        return error_envelope(
            "the scheduler will not take CRON.md as written; fix every problem "
            "and run check again",
            reason="problems",
            **result,
        )
    return {"status": "ok", **result}


def build_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.schedules",
        description="Scheduled jobs in the user's CRON.md",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "check",
        help="Read CRON.md the way the scheduler does and list every problem",
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parse_and_resolve(parser, argv)
    run_skill_cli({"check": cmd_check}, args)


if __name__ == "__main__":
    main()
