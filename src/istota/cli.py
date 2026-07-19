"""CLI interface for local testing and administration."""

import argparse
import importlib.metadata
import json
import sys
from pathlib import Path

from . import db
from .config import load_config
from .logging_setup import setup_logging
from .executor import execute_task, execute_task_interactive
from .scheduler import process_one_task, check_briefings
from .email_support import get_email_config
from .transport.email import poll_emails
from .skills.email import list_emails, send_email
from .storage import (
    ensure_user_directories_v2,
    user_directories_exist_v2,
    init_user_memory_v2,
    get_memory_line_count_v2,
    get_user_base_path,
)
from .skills.calendar import (
    get_caldav_client,
    list_calendars,
    get_today_events,
    create_event,
    delete_event,
    format_event_for_display,
)
from .tasks_file_poller import (
    discover_tasks_files,
    poll_user_tasks_file,
    poll_all_tasks_files,
)


def cmd_init(args):
    """Initialize the database."""
    config = load_config(Path(args.config) if args.config else None)
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    db.init_db(config.db_path)
    print(f"Database initialized at {config.db_path}")


def cmd_task(args):
    """Submit a task directly."""
    config = load_config(Path(args.config) if args.config else None)

    if args.prompt:
        prompt = args.prompt
    else:
        # Read from stdin
        print("Enter task (Ctrl+D to submit):")
        prompt = sys.stdin.read().strip()

    if not prompt:
        print("Error: No prompt provided", file=sys.stderr)
        sys.exit(1)

    # Determine source type and conversation token
    if args.source_type:
        source_type = args.source_type
    elif args.conversation_token:
        source_type = "talk"
    else:
        source_type = "cli"

    with db.get_db(config.db_path) as conn:
        task_id = db.create_task(
            conn,
            prompt=prompt,
            user_id=args.user,
            source_type=source_type,
            conversation_token=args.conversation_token,
        )
        print(f"Task created: {task_id}")

    if args.execute:
        # Execute immediately
        print("Executing task...")
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_id)
            if task:
                user_resources = db.get_user_resources(conn, args.user)
                use_context = not args.no_context
                success, result, _actions, _trace = execute_task(
                    task,
                    config,
                    user_resources,
                    dry_run=args.dry_run,
                    use_context=use_context,
                    conn=conn,
                )
                if success:
                    db.update_task_status(conn, task_id, "completed", result=result)
                    print("\n--- Result ---")
                    print(result)
                else:
                    db.update_task_status(conn, task_id, "failed", error=result)
                    print("\n--- Error ---", file=sys.stderr)
                    print(result, file=sys.stderr)
                    sys.exit(1)


def cmd_repl(args):
    """Launch the interactive terminal assistant (full-stack, streamed)."""
    from .repl import run_session

    config = load_config(Path(args.config) if args.config else None)

    user_id = args.user
    if not user_id:
        # Default to the sole configured user, else the first admin.
        if len(config.users) == 1:
            user_id = next(iter(config.users))
        elif config.admin_users:
            user_id = sorted(config.admin_users)[0]
    if not user_id:
        print(
            "Error: could not infer a user; pass -u/--user.", file=sys.stderr,
        )
        sys.exit(1)

    run_session(
        config,
        user_id=user_id,
        token=args.token,
        workspace=args.workspace,
        model=args.model,
        effort=args.effort,
    )


def _default_env_file(args) -> Path:
    """Where ``serve``/``setup`` read/write the secrets env file.

    Sibling to an explicit ``-c`` config file, else the standard
    ``~/.config/istota/istota.env`` (where ``istota setup`` writes it).
    """
    if getattr(args, "config", None):
        return Path(args.config).expanduser().parent / "istota.env"
    return Path.home() / ".config" / "istota" / "istota.env"


def cmd_setup(args):
    """Interactive first-run installer for the local single-user shape."""
    from . import setup_wizard

    try:
        rc = setup_wizard.run_setup(args)
    except setup_wizard.SetupError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("\nSetup cancelled.", file=sys.stderr)
        sys.exit(1)
    sys.exit(rc)


def cmd_serve(args):
    """Combined local launcher: scheduler loop + web server in one process."""
    from . import serve

    # Propagate an explicit -c path to the web app, which loads its own config
    # via load_config() (no arg) in its lifespan — ISTOTA_CONFIG_PATH is the
    # documented way to point it at a non-standard config location.
    import os
    if args.config:
        os.environ["ISTOTA_CONFIG_PATH"] = str(Path(args.config).expanduser())

    # Source the secrets env file (native API key, session secret, …) BEFORE
    # load_config so its env overrides apply. Non-clobbering.
    env_file = Path(args.env_file).expanduser() if args.env_file else _default_env_file(args)
    n = serve.load_env_file(env_file)
    config = load_config(Path(args.config) if args.config else None)
    setup_logging(config, verbose=args.verbose, daemon_mode=True)
    if n:
        print(f"Loaded {n} value(s) from {env_file}")

    host = args.host or "127.0.0.1"
    port = args.port  # None → serve uses config.web.port
    try:
        serve.run_serve(config, host=host, port=port)
    except serve.ServeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        sys.exit(0)


def cmd_run(args):
    """Run the scheduler once (process pending tasks)."""
    config = load_config(Path(args.config) if args.config else None)

    if args.briefings:
        # Check and queue briefings
        with db.get_db(config.db_path) as conn:
            briefing_tasks = check_briefings(conn, config)
            if briefing_tasks:
                print(f"Queued {len(briefing_tasks)} briefing(s)")
            else:
                print("No briefings due")

    # Process tasks
    processed = 0
    while True:
        result = process_one_task(config, dry_run=args.dry_run)
        if result is None:
            break
        task_id, success = result
        status = "completed" if success else "failed"
        print(f"Task {task_id}: {status}")
        processed += 1

        if args.once:
            break

    if processed == 0:
        print("No pending tasks")
    else:
        print(f"Processed {processed} task(s)")

    # process_one_task may have lazily started the persistent asyncio runtime
    # (Talk delivery via run_coro). Stop it so the shared httpx client closes
    # cleanly rather than being dropped on interpreter exit. No-op if unused.
    from .async_runtime import reset_async_runtime

    reset_async_runtime()


def cmd_list(args):
    """List tasks."""
    config = load_config(Path(args.config) if args.config else None)

    with db.get_db(config.db_path) as conn:
        tasks = db.list_tasks(
            conn,
            status=args.status,
            user_id=args.user,
            limit=args.limit,
        )

    if not tasks:
        print("No tasks found")
        return

    for t in tasks:
        # Skill-tasks and command-tasks don't have a prompt; fall back to
        # whichever shape was actually populated so the operator sees what
        # ran. Mirrored in cmd_show.
        if t.prompt:
            label = t.prompt
        elif t.skill:
            args_label = (t.skill_args or "").strip()
            label = f"<skill:{t.skill}> {args_label}".rstrip()
        elif t.command:
            label = f"<cmd> {t.command}"
        else:
            label = ""
        preview = label[:60] + "..." if len(label) > 60 else label
        preview = preview.replace("\n", " ")
        print(f"[{t.id}] {t.status:20} {t.user_id:15} {preview}")


def cmd_show(args):
    """Show task details."""
    config = load_config(Path(args.config) if args.config else None)

    with db.get_db(config.db_path) as conn:
        task = db.get_task(conn, args.task_id)
        if not task:
            print(f"Task {args.task_id} not found", file=sys.stderr)
            sys.exit(1)

        logs = db.get_task_logs(conn, args.task_id)

    print(f"Task ID: {task.id}")
    print(f"Status: {task.status}")
    print(f"User: {task.user_id}")
    print(f"Source: {task.source_type}")
    print(f"Created: {task.created_at}")
    print(f"Attempts: {task.attempt_count}/{task.max_attempts}")
    if task.prompt:
        print(f"\nPrompt:\n{task.prompt}")
    elif task.skill:
        print(f"\nSkill: {task.skill}")
        if task.skill_args:
            print(f"Skill args: {task.skill_args}")
    elif task.command:
        print(f"\nCommand:\n{task.command}")

    if task.result:
        print(f"\nResult:\n{task.result}")
    if task.error:
        print(f"\nError:\n{task.error}")
    if task.confirmation_prompt:
        print(f"\nPending confirmation:\n{task.confirmation_prompt}")

    if logs:
        print("\nLogs:")
        for log in logs:
            print(f"  [{log['level']}] {log['timestamp']}: {log['message']}")


def _coerce_extras_value(raw: str):
    """Best-effort coerce a CLI ``key=value`` string to its natural Python type.

    Operators shouldn't have to learn JSON quoting just to pass an integer
    like ``default_radius=75`` or a bool like ``reconcile_enabled=true``. We try
    JSON first (handles ints, floats, bools, null, lists, dicts) and fall
    back to a plain string. Mirrors how TOML would have parsed the same
    field.
    """
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


def _build_resource_extras(args) -> "dict[str, object] | None":
    """Assemble the extras payload from CLI flags.

    Returns ``None`` when the operator hasn't expressed an intent — neither
    ``--extras`` nor ``--extras-json`` nor ``--extras-clear`` was passed.
    Returns ``{}`` when ``--extras-clear`` is set or ``--extras-json``
    decodes to an empty dict. Otherwise returns the assembled dict.
    """
    extras_json = getattr(args, "extras_json", None)
    extras_kv = getattr(args, "extras", None)
    extras_clear = getattr(args, "extras_clear", False)

    if extras_clear:
        return {}

    if extras_json is not None:
        try:
            decoded = json.loads(extras_json)
        except json.JSONDecodeError as e:
            print(f"Error: --extras-json is not valid JSON: {e}", file=sys.stderr)
            sys.exit(1)
        if not isinstance(decoded, dict):
            print("Error: --extras-json must decode to a JSON object", file=sys.stderr)
            sys.exit(1)
        return decoded

    if extras_kv:
        result: dict[str, object] = {}
        for pair in extras_kv:
            if "=" not in pair:
                print(f"Error: --extras pair must be key=value, got {pair!r}", file=sys.stderr)
                sys.exit(1)
            key, _, value = pair.partition("=")
            key = key.strip()
            if not key:
                print(f"Error: --extras key cannot be empty in {pair!r}", file=sys.stderr)
                sys.exit(1)
            result[key] = _coerce_extras_value(value)
        return result

    return None


def cmd_resource(args):
    """Manage user resources."""
    config = load_config(Path(args.config) if args.config else None)

    if args.action == "list":
        # Show config-defined resources
        user_config = config.get_user(args.user)
        if user_config and user_config.resources:
            print(f"Config resources for {args.user}:")
            for r in user_config.resources:
                print(f"  [config] {r.type:12} {r.path:40} {r.permissions:6} {r.name or ''}")
        else:
            print(f"No config resources for {args.user}")

        # Show DB resources (shared_file entries from auto-organizer)
        with db.get_db(config.db_path) as conn:
            db_resources = db.get_user_resources(conn, args.user)
        if db_resources:
            print(f"\nDynamic resources (DB):")
            for r in db_resources:
                print(f"  [{r.id:4}] {r.resource_type:12} {r.resource_path:40} {r.permissions:6} {r.display_name or ''}")

    elif args.action == "add":
        if not all([args.type, args.path]):
            print("Error: --type and --path required for add", file=sys.stderr)
            sys.exit(1)
        with db.get_db(config.db_path) as conn:
            resource_id = db.add_user_resource(
                conn,
                user_id=args.user,
                resource_type=args.type,
                resource_path=args.path,
                display_name=args.name,
                permissions=args.permissions or "read",
            )
            print(f"Resource added to DB: {resource_id}")
            print("Note: For permanent resources, prefer `istota resource ensure`.")

    elif args.action == "ensure":
        if not args.type:
            print("Error: --type is required for ensure", file=sys.stderr)
            sys.exit(1)

        # Module-shaped resources don't carry a real path; the (user, type,
        # type) tuple acts as the unique key. Matches the web UI behavior.
        # Filesystem-shaped types (folder, calendar, todo_file, ...) require
        # an explicit --path so a typo doesn't silently create a row at the
        # type-named pseudo-path.
        _MODULE_SHAPED_TYPES = {"feeds", "money", "moneyman", "overland", "karakeep", "monarch"}
        if not args.path and args.type not in _MODULE_SHAPED_TYPES:
            print(
                f"Error: --path is required for resource type {args.type!r}; "
                f"only module-shaped types ({', '.join(sorted(_MODULE_SHAPED_TYPES))}) "
                "default the path to the type name",
                file=sys.stderr,
            )
            sys.exit(1)
        resource_path = args.path if args.path else args.type
        display_name = args.name
        permissions = args.permissions or "read"
        new_extras = _build_resource_extras(args)

        kwargs: dict[str, object] = {
            "user_id": args.user,
            "resource_type": args.type,
            "resource_path": resource_path,
            "display_name": display_name,
            "permissions": permissions,
        }
        if new_extras is not None:
            kwargs["extras"] = new_extras
        with db.get_db(config.db_path) as conn:
            _, state = db.upsert_user_resource(conn, **kwargs)

        print(f"Resource ensured for {args.user!r}: type={args.type} path={resource_path}")
        print(f"STATE: {state}")


def _parse_components_arg(args) -> "dict[str, object] | None":
    """Build the components dict from --components-json or --component flags.

    Returns ``None`` when neither is set (caller decides default).
    ``--components-json`` takes precedence; ``--component k=v`` pairs are
    merged on top so an operator can override a single key.
    """
    components_json = getattr(args, "components_json", None)
    component_kv = getattr(args, "component", None) or []

    result: dict[str, object] | None = None

    if components_json:
        try:
            decoded = json.loads(components_json)
        except json.JSONDecodeError as e:
            print(f"Error: --components-json is not valid JSON: {e}", file=sys.stderr)
            sys.exit(1)
        if not isinstance(decoded, dict):
            print("Error: --components-json must decode to a JSON object", file=sys.stderr)
            sys.exit(1)
        result = dict(decoded)

    for pair in component_kv:
        if "=" not in pair:
            print(f"Error: --component pair must be key=value, got {pair!r}", file=sys.stderr)
            sys.exit(1)
        key, _, value = pair.partition("=")
        key = key.strip()
        if not key:
            print(f"Error: --component key cannot be empty in {pair!r}", file=sys.stderr)
            sys.exit(1)
        if result is None:
            result = {}
        result[key] = _coerce_extras_value(value)

    return result


def cmd_briefing(args):
    """Manage briefing configurations."""
    config = load_config(Path(args.config) if args.config else None)

    if args.action == "list":
        from . import user_briefings as _ub
        found = False
        for user_id, user_config in config.users.items():
            if args.user and user_id != args.user:
                continue
            if not user_config.briefings:
                continue
            found = True
            for b in user_config.briefings:
                print(f"{user_id:15} {b.name:10} {b.cron:15} -> {b.conversation_token}")
                if b.components:
                    # Show enabled components
                    enabled = []
                    for k, v in b.components.items():
                        if isinstance(v, bool) and v:
                            enabled.append(k)
                        elif isinstance(v, dict) and v.get("enabled"):
                            enabled.append(k)
                    if enabled:
                        print(f"{'':15} components: {', '.join(enabled)}")
        # Also show DB-only rows that aren't represented in user_config.briefings
        # (typically: disabled rows, which the overlay drops). Operators want to
        # see them so they can re-enable.
        db_rows = _ub.list_briefings(config.db_path)
        disabled_rows = [r for r in db_rows if not r.enabled]
        if args.user:
            disabled_rows = [r for r in disabled_rows if r.user_id == args.user]
        if disabled_rows:
            print("\nDisabled briefings (DB):")
            for r in disabled_rows:
                print(f"  {r.user_id:15} {r.name:10} {r.cron:15} -> {r.conversation_token}")
        if not found and not disabled_rows:
            print("No briefings configured")
        return

    if args.action == "ensure":
        from . import user_briefings as _ub

        if not args.user or not args.name or not args.cron:
            print("Error: --user, --name, and --cron are required for ensure", file=sys.stderr)
            sys.exit(1)

        output = args.output or "talk"
        from .transport import parse_output_target
        talk_leaf = any(d.surface == "talk" for d in parse_output_target(output))
        if talk_leaf and not args.conversation_token:
            print(
                f"Error: --conversation-token is required when --output is {output!r}",
                file=sys.stderr,
            )
            sys.exit(1)

        components = _parse_components_arg(args) or {}

        try:
            briefing, state = _ub.ensure_briefing(
                config.db_path,
                user_id=args.user,
                name=args.name,
                cron=args.cron,
                conversation_token=args.conversation_token or "",
                output=output,
                components=components,
                enabled=not args.disabled,
            )
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

        print(
            f"Briefing ensured for {args.user!r}: name={briefing.name} cron={briefing.cron!r} "
            f"output={briefing.output} enabled={briefing.enabled}"
        )
        print(f"STATE: {state}")
        return

    if args.action == "delete":
        from . import user_briefings as _ub

        if not args.user or not args.name:
            print("Error: --user and --name are required for delete", file=sys.stderr)
            sys.exit(1)
        removed = _ub.delete_briefing(config.db_path, args.user, args.name)
        if removed:
            print(f"Briefing deleted: user={args.user} name={args.name}")
        else:
            print(f"No briefing found: user={args.user} name={args.name}")
            sys.exit(1)
        return


def cmd_secret(args):
    """Manage per-user encrypted secrets.

    Same partial-update + state-output contract as ``user ensure`` and
    ``resource ensure``. Validation is gated by the central
    ``secret_schema`` registry — operators get a loud error on a typo
    instead of an orphan row that no skill ever reads.

    Plaintext values are never echoed to stdout. The ``ensure`` action
    prints the decision (created / updated / noop) but not the value;
    ``list`` prints (service, key, last_updated) tuples only.
    """
    from . import secrets_store
    from .secret_schema import all_known_services, known_service_keys

    config = load_config(Path(args.config) if args.config else None)
    db_path = config.db_path

    if args.action == "list":
        if not args.user:
            print("Error: --user is required for list", file=sys.stderr)
            sys.exit(1)
        stored = secrets_store.list_user_services(db_path, args.user)
        if not stored:
            print(f"No secrets stored for {args.user!r}.")
            return
        print(f"Secrets stored for {args.user!r}:")
        for service in sorted(stored):
            for entry in stored[service]:
                ts = entry.get("updated_at") or "?"
                print(f"  {service:20} {entry['key']:20} updated_at={ts}")
        return

    # ensure / remove both need (user, service, key).
    if not args.user or not args.service or not args.key:
        print("Error: --user, --service, and --key are required", file=sys.stderr)
        sys.exit(1)

    schema = all_known_services()
    if args.service not in schema:
        print(
            f"Error: unknown service {args.service!r} "
            f"(known: {', '.join(sorted(schema))})",
            file=sys.stderr,
        )
        sys.exit(1)

    valid_keys = known_service_keys()[args.service]
    if not valid_keys:
        print(
            f"Error: service {args.service!r} has no operator-writable keys "
            "(OAuth-only — use the web UI's Connect button)",
            file=sys.stderr,
        )
        sys.exit(1)
    if args.key not in valid_keys:
        print(
            f"Error: unknown key {args.key!r} for service {args.service!r} "
            f"(known: {', '.join(sorted(valid_keys))})",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.action == "ensure":
        if not args.value:
            print(
                "Error: --value is required for ensure (use `secret remove` to clear)",
                file=sys.stderr,
            )
            sys.exit(1)
        state = secrets_store.upsert_secret(
            db_path, args.user, args.service, args.key, args.value,
        )
        print(f"Secret ensured for {args.user!r}: service={args.service} key={args.key}")
        print(f"STATE: {state}")
        return

    if args.action == "remove":
        removed = secrets_store.delete_secret(
            db_path, args.user, args.service, args.key,
        )
        state = "removed" if removed else "noop"
        print(f"Secret remove for {args.user!r}: service={args.service} key={args.key}")
        print(f"STATE: {state}")
        return


def cmd_email(args):
    """Email management commands."""
    config = load_config(Path(args.config) if args.config else None)

    if args.action == "poll":
        if not config.email.enabled:
            print("Email is not enabled in config", file=sys.stderr)
            sys.exit(1)
        task_ids = poll_emails(config)
        if task_ids:
            print(f"Created {len(task_ids)} task(s): {task_ids}")
        else:
            print("No new emails to process")

    elif args.action == "list":
        if not config.email.enabled:
            print("Email is not enabled in config", file=sys.stderr)
            sys.exit(1)
        email_config = get_email_config(config)
        try:
            emails = list_emails(
                folder=config.email.poll_folder,
                limit=args.limit,
                config=email_config,
            )
            if not emails:
                print("No emails found")
                return
            for e in emails:
                read_marker = " " if e.is_read else "*"
                subject = e.subject[:50] + "..." if len(e.subject) > 50 else e.subject
                print(f"{read_marker} [{e.id:6}] {e.sender:30} {subject}")
        except Exception as e:
            print(f"Error listing emails: {e}", file=sys.stderr)
            sys.exit(1)

    elif args.action == "test":
        if not all([args.to, args.subject, args.body]):
            print("Error: --to, --subject, and --body required for test", file=sys.stderr)
            sys.exit(1)
        email_config = get_email_config(config)
        try:
            send_email(
                to=args.to,
                subject=args.subject,
                body=args.body,
                config=email_config,
                from_addr=config.email.bot_email,
            )
            print(f"Email sent to {args.to}")
        except Exception as e:
            print(f"Error sending email: {e}", file=sys.stderr)
            sys.exit(1)


def cmd_user_list(args):
    """List configured users."""
    config = load_config(Path(args.config) if args.config else None)

    if not config.users:
        print("No users configured")
        return
    for user_id, user_config in config.users.items():
        emails = ", ".join(user_config.email_addresses) if user_config.email_addresses else "(none)"
        print(f"{user_id:15} {user_config.display_name:20} {emails}")


def cmd_user_lookup(args):
    """Look up a user by email."""
    config = load_config(Path(args.config) if args.config else None)

    if not args.email:
        print("Error: --email required for lookup", file=sys.stderr)
        sys.exit(1)
    user_id = config.find_user_by_email(args.email)
    if user_id:
        user_config = config.get_user(user_id)
        print(f"User ID: {user_id}")
        print(f"Display name: {user_config.display_name}")
        print(f"Email addresses: {', '.join(user_config.email_addresses)}")
    else:
        print(f"No user found for email: {args.email}")


def cmd_user_init(args):
    """Initialize bot-managed directories for a user."""
    config = load_config(Path(args.config) if args.config else None)

    user_id = args.username

    # Warn if user not in config but proceed anyway
    if user_id not in config.users:
        print(f"Warning: User '{user_id}' not found in config, but proceeding anyway")

    print(f"Initializing directories for user '{user_id}'...")
    if config.use_mount:
        print(f"Mount: {config.nextcloud_mount_path}")
    else:
        print(f"Remote: {config.rclone_remote}")
    print(f"Base path: {get_user_base_path(user_id)}")

    success = ensure_user_directories_v2(config, user_id)
    if success:
        print(f"Directories created: inbox/, memories/, {config.bot_dir_name}/, shared/, scripts/")
    else:
        print("Warning: Some directories may not have been created", file=sys.stderr)

    if args.init_memory:
        print("Initializing memory file...")
        if init_user_memory_v2(config, user_id):
            print(f"Memory file created: {config.bot_dir_name}/config/USER.md")
        else:
            print("Error: Failed to create memory file", file=sys.stderr)
            sys.exit(1)


def cmd_user_status(args):
    """Show status of user's bot-managed directories."""
    config = load_config(Path(args.config) if args.config else None)

    user_id = args.username

    print(f"User: {user_id}")
    if config.use_mount:
        print(f"Mount: {config.nextcloud_mount_path}")
    else:
        print(f"Remote: {config.rclone_remote}")
    print(f"Base path: {get_user_base_path(user_id)}")
    print()

    # Check if user is in config
    if user_id in config.users:
        user_config = config.get_user(user_id)
        print(f"Config: Found (display_name: {user_config.display_name})")
    else:
        print("Config: Not found in config")
    print()

    # Check directories
    print("Directories:")
    dir_status = user_directories_exist_v2(config, user_id)
    for subdir, exists in dir_status.items():
        status = "exists" if exists else "missing"
        print(f"  {subdir}/: {status}")
    print()

    # Check memory file
    print("Memory file:")
    line_count = get_memory_line_count_v2(config, user_id)
    if line_count is not None:
        print(f"  Status: initialized ({line_count} lines)")
    else:
        print("  Status: not initialized")


def cmd_user_ensure(args):
    """Create or update a user_profiles row (idempotent).

    Drop-in replacement for templating per-user TOML files via Ansible.
    Only the flags the operator passes are written; omitted flags leave
    the existing column value untouched (or use defaults on first insert).
    """
    from . import user_profiles

    config = load_config(Path(args.config) if args.config else None)
    user_id = args.name
    db_path = config.db_path

    if not Path(db_path).exists():
        print(f"Error: DB not found at {db_path}; run `istota init` first", file=sys.stderr)
        sys.exit(1)

    # Build the partial-update dict from flags the user actually passed.
    updates: dict[str, object] = {}
    if args.display_name is not None:
        updates["display_name"] = args.display_name
    if args.tz is not None:
        updates["timezone"] = args.tz
    if args.email is not None:
        updates["email_addresses"] = list(args.email)
    if args.trusted_sender is not None:
        updates["trusted_email_senders"] = list(args.trusted_sender)
    if args.quiet_sender is not None:
        updates["quiet_email_senders"] = list(args.quiet_sender)
    if args.log_channel is not None:
        updates["log_channel"] = args.log_channel
    if args.alerts_channel is not None:
        updates["alerts_channel"] = args.alerts_channel
    if args.max_foreground_workers is not None:
        updates["max_foreground_workers"] = args.max_foreground_workers
    if args.max_background_workers is not None:
        updates["max_background_workers"] = args.max_background_workers
    if args.disabled_skill is not None:
        updates["disabled_skills"] = list(args.disabled_skill)
    if args.disabled_module is not None:
        from .modules import MODULE_NAMES

        # Drop empty strings so `--disabled-module ""` is the explicit-clear
        # form (argparse delivers ["" ] when the flag is passed once with no
        # value). Validate the rest against the module registry — a typo
        # would silently disable nothing otherwise.
        names = [m for m in args.disabled_module if m]
        unknown = [m for m in names if m not in MODULE_NAMES]
        if unknown:
            print(
                f"Error: unknown module name(s): {', '.join(unknown)} "
                f"(known: {', '.join(sorted(MODULE_NAMES))})",
                file=sys.stderr,
            )
            sys.exit(1)
        updates["disabled_modules"] = names
    if args.default_destination is not None:
        from .transport import parse_output_target
        if args.default_destination and not parse_output_target(args.default_destination):
            print(
                f"Error: invalid default destination descriptor: "
                f"{args.default_destination!r}",
                file=sys.stderr,
            )
            sys.exit(1)
        updates["default_destination"] = args.default_destination or "talk"
    if args.route is not None:
        from .notifications import PURPOSES
        from .transport import parse_output_target
        routing: dict[str, str] = {}
        for entry in args.route:
            purpose, sep, descriptor = entry.partition("=")
            purpose = purpose.strip()
            descriptor = descriptor.strip()
            if not sep or not purpose:
                print(
                    f"Error: --route expects purpose=descriptor, got {entry!r}",
                    file=sys.stderr,
                )
                sys.exit(1)
            # Reject unknown purposes — a typo would persist a dead routing entry
            # that nothing reads (mirrors the web settings validator).
            if purpose not in PURPOSES:
                print(
                    f"Error: unknown route purpose {purpose!r}; expected one of "
                    f"{', '.join(PURPOSES)}",
                    file=sys.stderr,
                )
                sys.exit(1)
            # Empty descriptor clears the route for that purpose.
            if descriptor and not parse_output_target(descriptor):
                print(
                    f"Error: invalid route descriptor for {purpose!r}: "
                    f"{descriptor!r}",
                    file=sys.stderr,
                )
                sys.exit(1)
            if descriptor:
                routing[purpose] = descriptor
        updates["routing"] = routing
    if args.email_reply_routing is not None:
        valid = ("origin+thread", "origin", "thread")
        if args.email_reply_routing not in valid:
            print(
                f"Error: --email-reply-routing must be one of {', '.join(valid)}, "
                f"got {args.email_reply_routing!r}",
                file=sys.stderr,
            )
            sys.exit(1)
        updates["email_reply_routing"] = args.email_reply_routing

    profile, state = user_profiles.update_profile_with_status(db_path, user_id, **updates)

    print(f"User {user_id!r} ensured.")
    print(f"  display_name: {profile.display_name}")
    print(f"  timezone:     {profile.timezone}")
    if profile.email_addresses:
        print(f"  emails:       {', '.join(profile.email_addresses)}")
    if profile.log_channel:
        print(f"  log_channel:  {profile.log_channel}")
    if profile.alerts_channel:
        print(f"  alerts_channel: {profile.alerts_channel}")
    if profile.trusted_email_senders:
        print(f"  trusted_senders: {', '.join(profile.trusted_email_senders)}")
    if profile.quiet_email_senders:
        print(f"  quiet_senders: {', '.join(profile.quiet_email_senders)}")
    if profile.disabled_modules:
        print(f"  disabled_modules: {', '.join(profile.disabled_modules)}")
    if profile.default_destination and profile.default_destination != "talk":
        print(f"  default_destination: {profile.default_destination}")
    if profile.email_reply_routing and profile.email_reply_routing != "origin+thread":
        print(f"  email_reply_routing: {profile.email_reply_routing}")
    if profile.routing:
        print(f"  routing: {', '.join(f'{k}={v}' for k, v in sorted(profile.routing.items()))}")
    print(f"STATE: {state}")


def cmd_user_show(args):
    """Show the stored profile for a user (DB row only — no TOML overlay)."""
    from . import user_profiles

    config = load_config(Path(args.config) if args.config else None)
    db_path = config.db_path
    if not Path(db_path).exists():
        print(f"Error: DB not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    profile = user_profiles.get_profile(db_path, args.name)
    if profile is None:
        print(f"No DB profile row for {args.name!r}")
        return

    print(json.dumps({
        "user_id": profile.user_id,
        "display_name": profile.display_name,
        "email_addresses": profile.email_addresses,
        "timezone": profile.timezone,
        "log_channel": profile.log_channel,
        "alerts_channel": profile.alerts_channel,
        "max_foreground_workers": profile.max_foreground_workers,
        "max_background_workers": profile.max_background_workers,
        "disabled_skills": profile.disabled_skills,
        "disabled_modules": profile.disabled_modules,
        "trusted_email_senders": profile.trusted_email_senders,
        "quiet_email_senders": profile.quiet_email_senders,
        "routing": profile.routing,
        "default_destination": profile.default_destination,
        "email_reply_routing": profile.email_reply_routing,
    }, indent=2))


def cmd_user_remove(args):
    """Remove a user_profiles row. Does not touch resources or other tables."""
    from . import user_profiles

    config = load_config(Path(args.config) if args.config else None)
    db_path = config.db_path
    if user_profiles.delete_profile(db_path, args.name):
        print(f"Removed profile row for {args.name!r}.")
    else:
        print(f"No profile row for {args.name!r} (nothing to remove).")


def cmd_calendar_discover(args):
    """Discover calendars accessible to the istota bot."""
    config = load_config(Path(args.config) if args.config else None)

    if not config.caldav_url or not config.caldav_username or not config.caldav_password:
        print("Error: CalDAV settings not configured", file=sys.stderr)
        print("Required: caldav_url, caldav_username, caldav_password in config", file=sys.stderr)
        sys.exit(1)

    try:
        with get_caldav_client(
            config.caldav_url,
            config.caldav_username,
            config.caldav_password,
        ) as client:
            calendars = list_calendars(client)

            if not calendars:
                print("No calendars found")
                return

            print(f"Found {len(calendars)} calendar(s):\n")
            for name, url in calendars:
                # Determine if owned or shared based on URL path
                is_owned = f"/calendars/{config.caldav_username}/" in url
                ownership = "owned" if is_owned else "shared"
                print(f"  {name}")
                print(f"    URL: {url}")
                print(f"    Type: {ownership}")
                print()

    except Exception as e:
        print(f"Error connecting to CalDAV server: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_calendar_test(args):
    """Test calendar access."""
    from datetime import datetime, timedelta

    config = load_config(Path(args.config) if args.config else None)

    if not config.caldav_url or not config.caldav_username or not config.caldav_password:
        print("Error: CalDAV settings not configured", file=sys.stderr)
        sys.exit(1)

    calendar_url = args.url

    try:
        with get_caldav_client(
            config.caldav_url,
            config.caldav_username,
            config.caldav_password,
        ) as client:
            # Test read access
            print(f"Testing read access to: {calendar_url}")
            try:
                events = get_today_events(client, calendar_url)
                print(f"  Read access: OK ({len(events)} event(s) today)")
                for event in events[:3]:  # Show up to 3 events
                    print(f"    - {format_event_for_display(event)}")
                if len(events) > 3:
                    print(f"    ... and {len(events) - 3} more")
            except Exception as e:
                print(f"  Read access: FAILED - {e}", file=sys.stderr)
                sys.exit(1)

            # Test write access if requested
            if args.test_write:
                print("\nTesting write access...")
                try:
                    # Create a test event
                    now = datetime.now()
                    test_start = now + timedelta(days=30)  # 30 days in future
                    test_end = test_start + timedelta(hours=1)

                    uid = create_event(
                        client,
                        calendar_url,
                        summary="[Istota Test Event - DELETE ME]",
                        start=test_start,
                        end=test_end,
                        description="This is a test event created by istota calendar test --test-write. It should be automatically deleted.",
                    )
                    print(f"  Create event: OK (UID: {uid})")

                    # Delete the test event
                    deleted = delete_event(client, calendar_url, uid)
                    if deleted:
                        print("  Delete event: OK")
                    else:
                        print("  Delete event: FAILED - event not found after creation", file=sys.stderr)
                        sys.exit(1)

                    print("\n  Write access: OK")

                except Exception as e:
                    error_msg = str(e).lower()
                    if "authorization" in error_msg or "forbidden" in error_msg or "403" in error_msg:
                        print(f"  Write access: DENIED (read-only calendar)")
                    else:
                        print(f"  Write access: FAILED - {e}", file=sys.stderr)
                    sys.exit(1)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_tasks_file_poll(args):
    """Poll TASKS.md files for new tasks."""
    config = load_config(Path(args.config) if args.config else None)

    # Discover TASKS files
    discovered = discover_tasks_files(config)

    if not discovered:
        print("No TASKS.md files found")
        return

    if args.user:
        # Filter to specific user
        discovered = [f for f in discovered if f.owner_id == args.user]
        if not discovered:
            print(f"No TASKS.md file found for user '{args.user}'")
            return

    print(f"Found {len(discovered)} TASKS.md file(s):")
    for tf in discovered:
        print(f"  {tf.file_path} (owner: {tf.owner_id})")

    all_task_ids = []
    for tf in discovered:
        task_ids = poll_user_tasks_file(config, tf.owner_id, tf.file_path)
        all_task_ids.extend(task_ids)

    if all_task_ids:
        print(f"Created {len(all_task_ids)} task(s): {all_task_ids}")
    else:
        print("No new tasks found")


def _get_kv_conn(args):
    """Get a DB connection for KV commands."""
    config = load_config(Path(args.config) if args.config else None)
    return db.get_db(config.db_path)


def cmd_kv_get(args):
    """Get a value from the KV store."""
    with _get_kv_conn(args) as conn:
        result = db.kv_get(conn, args.user, args.namespace, args.key)
    if result is None:
        print(json.dumps({"status": "not_found"}))
    else:
        print(json.dumps({"status": "ok", "value": json.loads(result["value"])}))


def cmd_kv_set(args):
    """Set a value in the KV store."""
    try:
        json.loads(args.value)
    except json.JSONDecodeError:
        print(json.dumps({"status": "error", "message": "invalid JSON value"}))
        return
    with _get_kv_conn(args) as conn:
        db.kv_set(conn, args.user, args.namespace, args.key, args.value)
    print(json.dumps({"status": "ok"}))


def cmd_kv_list(args):
    """List all entries in a namespace."""
    with _get_kv_conn(args) as conn:
        entries = db.kv_list(conn, args.user, args.namespace)
    # Parse JSON values for output
    for entry in entries:
        try:
            entry["value"] = json.loads(entry["value"])
        except json.JSONDecodeError:
            pass
    print(json.dumps({"status": "ok", "count": len(entries), "entries": entries}))


def cmd_kv_delete(args):
    """Delete a key from the KV store."""
    with _get_kv_conn(args) as conn:
        deleted = db.kv_delete(conn, args.user, args.namespace, args.key)
    if deleted:
        print(json.dumps({"status": "ok", "deleted": True}))
    else:
        print(json.dumps({"status": "not_found"}))


def cmd_kv_namespaces(args):
    """List namespaces for a user."""
    with _get_kv_conn(args) as conn:
        namespaces = db.kv_namespaces(conn, args.user)
    print(json.dumps({"status": "ok", "namespaces": namespaces}))


def cmd_chat_backfill_history(args):
    """Recover dormant rooms' transcripts from the Talk message cache.

    The web transcript reads the durable `messages` store; rooms whose turns
    were task-retention-deleted before the unified-room-sync migration came out
    empty (their tasks were already GC'd). This folds the surviving
    `talk_messages` copy back into the store. Idempotent — safe to re-run.
    """
    config = load_config(Path(args.config) if args.config else None)
    with db.get_db(config.db_path) as conn:
        if args.token:
            tokens = [args.token]
        else:
            rows = conn.execute(
                "SELECT token FROM rooms WHERE origin = 'talk'"
            ).fetchall()
            tokens = [r["token"] for r in rows]
        total = 0
        per_room = []
        for tok in tokens:
            n = db.backfill_room_messages_from_talk_cache(conn, tok)
            total += n
            if n:
                per_room.append({"token": tok, "inserted": n})
    print(json.dumps({
        "status": "ok", "rooms_scanned": len(tokens),
        "rows_inserted": total, "per_room": per_room,
    }, indent=2))


def cmd_tasks_file_status(args):
    """Show status of TASKS.md file tasks."""
    config = load_config(Path(args.config) if args.config else None)

    # Discover and show TASKS files
    print("Discovered TASKS.md files:")
    discovered = discover_tasks_files(config)

    if args.user:
        discovered = [f for f in discovered if f.owner_id == args.user]

    if not discovered:
        print("  (none found)")
    else:
        for tf in discovered:
            user_config = config.get_user(tf.owner_id)
            email_status = "yes" if (user_config and user_config.email_addresses and config.email.enabled) else "no"
            print(f"  {tf.file_path} (owner: {tf.owner_id}, email notifications: {email_status})")

    print()

    # Show tracked tasks from database
    with db.get_db(config.db_path) as conn:
        tasks = db.list_istota_file_tasks(conn, user_id=args.user, limit=args.limit)

    if not tasks:
        print("No tracked TASKS.md tasks")
        return

    print(f"Tracked tasks (most recent {len(tasks)}):")
    for t in tasks:
        content_preview = t.normalized_content[:40]
        if len(t.normalized_content) > 40:
            content_preview += "..."
        print(f"  [{t.id}] {t.status:12} {t.user_id:15} {content_preview}")


def cmd_experimental_list(args):
    """List known experimental feature flags with current on/off status."""
    from istota.experimental import KNOWN_FEATURES
    config = load_config(Path(args.config) if args.config else None)
    enabled = set(config.experimental.features)
    rows = []
    width = max((len(name) for name in KNOWN_FEATURES), default=0)
    for name, desc in sorted(KNOWN_FEATURES.items()):
        status = "on " if name in enabled else "off"
        rows.append(f"  [{status}] {name.ljust(width)}  {desc}")
    if not rows:
        print("(no experimental features registered)")
        return
    print("Experimental features:")
    print("\n".join(rows))
    unknown = sorted(enabled - set(KNOWN_FEATURES))
    if unknown:
        print()
        print("Configured but unknown (typo or stale flag):")
        for name in unknown:
            print(f"  {name}")


def main():
    # `istota money <op> …` forwards operational commands verbatim to the money
    # Click tree. argparse REMAINDER can't capture a leading option (e.g.
    # `money list -u U`), so peel those off before the strict parse_args().
    from istota import cli_money
    _pre = argparse.ArgumentParser(add_help=False)
    _pre.add_argument("-c", "--config")
    _pre.add_argument("-v", "--verbose", action="store_true")
    _known, _rest = _pre.parse_known_args()
    if len(_rest) >= 2 and _rest[0] == "money" and cli_money.is_operational(_rest[1]):
        config = load_config(Path(_known.config) if _known.config else None)
        setup_logging(config, verbose=_known.verbose)
        rc = cli_money.dispatch_operational(_rest[1], _rest[2:], config)
        sys.exit(rc or 0)

    parser = argparse.ArgumentParser(description="Istota CLI")
    parser.add_argument("-c", "--config", help="Path to config file")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose (DEBUG) logging")
    parser.add_argument(
        "--version", action="version",
        version=f"istota {importlib.metadata.version('istota')}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # init
    init_parser = subparsers.add_parser("init", help="Initialize database")

    # task
    task_parser = subparsers.add_parser("task", help="Submit a task")
    task_parser.add_argument("prompt", nargs="?", help="Task prompt (or read from stdin)")
    task_parser.add_argument("-u", "--user", default="testuser", help="User ID")
    task_parser.add_argument("-x", "--execute", action="store_true", help="Execute immediately")
    task_parser.add_argument("--dry-run", action="store_true", help="Show prompt without executing")
    task_parser.add_argument("-t", "--conversation-token", help="Conversation token (room ID) for context lookup")
    task_parser.add_argument("--source-type", help="Source type (cli, talk, briefing, email, istota_file)")
    task_parser.add_argument("--no-context", action="store_true", help="Disable conversation context lookup")

    # run
    run_parser = subparsers.add_parser("run", help="Process pending tasks")
    run_parser.add_argument("--once", action="store_true", help="Process only one task")
    run_parser.add_argument("--briefings", action="store_true", help="Check and queue briefings first")
    run_parser.add_argument("--dry-run", action="store_true", help="Don't actually execute tasks")

    # setup (interactive first-run installer for the local single-user shape)
    setup_parser = subparsers.add_parser(
        "setup", help="Interactive first-run installer (local single-user install)",
    )
    setup_parser.add_argument("--workspace", help="Workspace directory (default ~/.istota)")
    setup_parser.add_argument(
        "--brain", choices=["claude_code", "native"], help="Model backend (skip detection)",
    )
    setup_parser.add_argument("--native-base-url", help="Native brain API base URL")
    setup_parser.add_argument("--native-model", help="Native brain model id")
    setup_parser.add_argument("--native-api-key", help="Native brain API key (written to istota.env)")
    setup_parser.add_argument("--user", help="User id (default OS username)")
    setup_parser.add_argument("--display-name", help="Display name")
    setup_parser.add_argument("--timezone", help="Timezone (default from system)")
    setup_parser.add_argument("--port", type=int, help="Web port (default 8766)")
    setup_parser.add_argument("--email", action="store_true", help="Enable email surface")
    setup_parser.add_argument("--location", action="store_true", help="Enable GPS/location tracking")
    setup_parser.add_argument(
        "--no-money", action="store_true",
        help="Disable the money module (double-entry accounting; on by default)",
    )
    setup_parser.add_argument("--yes", action="store_true", help="Non-interactive; take defaults + flags")
    setup_parser.add_argument("--force", action="store_true", help="Overwrite an existing config")

    # serve (combined local launcher: scheduler + web in one process)
    serve_parser = subparsers.add_parser(
        "serve", help="Run the scheduler loop and web server in one process (local install)",
    )
    serve_parser.add_argument("--host", help="Web bind host (default 127.0.0.1)")
    serve_parser.add_argument("--port", type=int, help="Web port (default from [web] port)")
    serve_parser.add_argument(
        "--env-file", help="Path to a KEY=VALUE secrets env file to source before start",
    )

    # repl
    repl_parser = subparsers.add_parser(
        "repl", help="Interactive terminal assistant (full-stack, streamed)",
    )
    repl_parser.add_argument("-u", "--user", help="User id (defaults to the sole/admin user)")
    repl_parser.add_argument(
        "-t", "--token", help="Resume a named conversation token (default: a fresh one)",
    )
    repl_parser.add_argument(
        "--workspace", default="cwd",
        help="Working directory: cwd (default) | standard (per-user temp) | PATH",
    )
    repl_parser.add_argument("--model", help="Model alias for turns (e.g. opus, sonnet)")
    repl_parser.add_argument("--effort", help="Reasoning effort (low|medium|high|xhigh|max)")

    # list
    list_parser = subparsers.add_parser("list", help="List tasks")
    list_parser.add_argument("-s", "--status", help="Filter by status")
    list_parser.add_argument("-u", "--user", help="Filter by user")
    list_parser.add_argument("-n", "--limit", type=int, default=20, help="Max results")

    # show
    show_parser = subparsers.add_parser("show", help="Show task details")
    show_parser.add_argument("task_id", type=int, help="Task ID")

    # resource
    resource_parser = subparsers.add_parser("resource", help="Manage user resources")
    resource_parser.add_argument("action", choices=["list", "add", "ensure"], help="Action")
    resource_parser.add_argument("-u", "--user", required=True, help="User ID")
    resource_parser.add_argument("-t", "--type", help="Resource type (calendar, folder, todo_file, email_folder, feeds, money, overland, ...)")
    resource_parser.add_argument("-p", "--path", help="Resource path (defaults to type for module-shaped resources)")
    resource_parser.add_argument("-n", "--name", help="Display name")
    resource_parser.add_argument("--permissions", help="Permissions (read, write)")
    resource_parser.add_argument(
        "--extras", action="append",
        help="Extra resource-specific config as key=value (repeatable). "
             "Values parsed as JSON when possible (e.g. default_radius=75 → int).",
    )
    resource_parser.add_argument(
        "--extras-json",
        help="Full extras payload as a JSON object. Overrides --extras pairs.",
    )
    resource_parser.add_argument(
        "--extras-clear", action="store_true",
        help="Wipe extras on the row. Use to explicitly clear what --extras would otherwise preserve.",
    )

    # briefing
    briefing_parser = subparsers.add_parser("briefing", help="Manage briefings")
    briefing_parser.add_argument(
        "action", choices=["list", "ensure", "delete"], help="Action",
    )
    briefing_parser.add_argument("-u", "--user", help="User id (required for ensure/delete)")
    briefing_parser.add_argument("--name", help="Briefing name (e.g. 'morning')")
    briefing_parser.add_argument("--cron", help="Cron expression (user TZ), e.g. '0 7 * * 1-5'")
    briefing_parser.add_argument(
        "--conversation-token",
        help="Talk room token (required when output includes 'talk')",
    )
    briefing_parser.add_argument(
        "--output", default="talk",
        help="Delivery target: talk / email / ntfy, or a comma list / "
             "surface:channel descriptor (validated by parse_output_target)",
    )
    briefing_parser.add_argument(
        "--components-json",
        help='JSON object of components, e.g. \'{"calendar": true, "email": true}\'',
    )
    briefing_parser.add_argument(
        "--component",
        action="append",
        help="Repeatable: simple component flag, e.g. --component calendar=true",
    )
    briefing_parser.add_argument(
        "--disabled",
        action="store_true",
        help="Mark this briefing as disabled (drops the corresponding TOML entry without scheduling)",
    )

    # secret (encrypted per-user credentials)
    secret_parser = subparsers.add_parser(
        "secret",
        help="Manage per-user encrypted secrets (Ansible-friendly, idempotent)",
    )
    secret_parser.add_argument(
        "action", choices=["ensure", "list", "remove"], help="Action",
    )
    secret_parser.add_argument("-u", "--user", help="User id")
    secret_parser.add_argument(
        "--service",
        help="Service name (karakeep, monarch, overland, feeds, ...)",
    )
    secret_parser.add_argument("--key", help="Secret key within the service")
    secret_parser.add_argument(
        "--value",
        help="Secret value (ensure only). Use `secret remove` to clear.",
    )

    # email
    email_parser = subparsers.add_parser("email", help="Email management")
    email_parser.add_argument("action", choices=["poll", "list", "test"], help="Action")
    email_parser.add_argument("-n", "--limit", type=int, default=20, help="Max emails to list")
    email_parser.add_argument("--to", help="Recipient for test email")
    email_parser.add_argument("--subject", help="Subject for test email")
    email_parser.add_argument("--body", help="Body for test email")

    # user (with subparsers)
    user_parser = subparsers.add_parser("user", help="User management")
    user_subparsers = user_parser.add_subparsers(dest="user_action", required=True)

    # user list
    user_list_parser = user_subparsers.add_parser("list", help="List configured users")

    # user lookup
    user_lookup_parser = user_subparsers.add_parser("lookup", help="Look up user by email")
    user_lookup_parser.add_argument("--email", required=True, help="Email address to lookup")

    # user init
    user_init_parser = user_subparsers.add_parser("init", help="Initialize bot-managed directories")
    user_init_parser.add_argument("username", help="User ID to initialize")
    user_init_parser.add_argument("--init-memory", action="store_true", help="Create initial memory file")

    # user status
    user_status_parser = user_subparsers.add_parser("status", help="Show user directory status")
    user_status_parser.add_argument("username", help="User ID to check")

    # user ensure  (Phase 6: idempotent profile upsert; replaces per-user TOML in Ansible)
    user_ensure_parser = user_subparsers.add_parser(
        "ensure",
        help="Create or update a user profile row (idempotent; for Ansible)",
    )
    user_ensure_parser.add_argument("--name", required=True, help="User ID (Nextcloud username)")
    user_ensure_parser.add_argument("--display-name", help="Display name shown in prompts")
    user_ensure_parser.add_argument("--tz", "--timezone", dest="tz", help="IANA timezone (e.g. America/Los_Angeles)")
    user_ensure_parser.add_argument(
        "--email", action="append", help="User email address (repeatable; replaces existing list when passed)"
    )
    user_ensure_parser.add_argument(
        "--trusted-sender", action="append",
        help="Trusted email sender pattern (repeatable; fnmatch syntax)",
    )
    user_ensure_parser.add_argument(
        "--quiet-sender", action="append",
        help="Quiet email sender pattern — mail filed silently, no task "
             "(repeatable; fnmatch syntax)",
    )
    user_ensure_parser.add_argument("--log-channel", help="Talk room token for verbose execution logs")
    user_ensure_parser.add_argument("--alerts-channel", help="Talk room token for confirmations and alerts")
    user_ensure_parser.add_argument("--max-foreground-workers", type=int, help="Per-user fg worker cap (0 = global default)")
    user_ensure_parser.add_argument("--max-background-workers", type=int, help="Per-user bg worker cap (0 = global default)")
    user_ensure_parser.add_argument(
        "--disabled-skill", action="append",
        help="Skill name to exclude from selection (repeatable)",
    )
    user_ensure_parser.add_argument(
        "--disabled-module", action="append",
        help=(
            "Module to opt this user out of (repeatable). One of "
            "feeds, money, location. Pass an empty value to clear."
        ),
    )
    user_ensure_parser.add_argument(
        "--default-destination",
        help=(
            "Fallback delivery descriptor (e.g. talk, email, both, talk:<token>). "
            "Default 'talk'."
        ),
    )
    user_ensure_parser.add_argument(
        "--route", action="append", metavar="PURPOSE=DESCRIPTOR",
        help=(
            "Per-purpose delivery route (repeatable; replaces the routing table "
            "when passed). PURPOSE is one of reply/alert/log/briefing/"
            "notification; DESCRIPTOR is an output_target like email or "
            "matrix:<room>. Empty descriptor clears that purpose."
        ),
    )
    user_ensure_parser.add_argument(
        "--email-reply-routing",
        choices=["origin+thread", "origin", "thread"],
        help=(
            "Where a reply to an email this bot sent is delivered: 'origin+thread' "
            "(default — origin surface and the email thread), 'origin' (origin "
            "surface only), or 'thread' (email only)."
        ),
    )
    # user show  (Phase 6: dump the DB row as JSON)
    user_show_parser = user_subparsers.add_parser("show", help="Show stored profile row as JSON")
    user_show_parser.add_argument("--name", required=True, help="User ID")

    # user remove  (Phase 6: delete a user_profiles row; does not touch other tables)
    user_remove_parser = user_subparsers.add_parser("remove", help="Remove a user_profiles row")
    user_remove_parser.add_argument("--name", required=True, help="User ID")

    # calendar (with subparsers)
    calendar_parser = subparsers.add_parser("calendar", help="Calendar management")
    calendar_subparsers = calendar_parser.add_subparsers(dest="calendar_action", required=True)

    # calendar discover
    calendar_discover_parser = calendar_subparsers.add_parser("discover", help="Discover accessible calendars")

    # calendar test
    calendar_test_parser = calendar_subparsers.add_parser("test", help="Test calendar access")
    calendar_test_parser.add_argument("url", help="Calendar URL to test")
    calendar_test_parser.add_argument("--test-write", action="store_true", help="Test write access by creating/deleting a test event")

    # tasks-file (with subparsers)
    tasks_file_parser = subparsers.add_parser("tasks-file", help="TASKS.md file management")
    tasks_file_subparsers = tasks_file_parser.add_subparsers(dest="tasks_file_action", required=True)

    # tasks-file poll
    tasks_file_poll_parser = tasks_file_subparsers.add_parser("poll", help="Poll TASKS.md files for new tasks")
    tasks_file_poll_parser.add_argument("-u", "--user", help="User ID to poll (or all if not specified)")

    # tasks-file status
    tasks_file_status_parser = tasks_file_subparsers.add_parser("status", help="Show TASKS.md file task status")
    tasks_file_status_parser.add_argument("-u", "--user", help="Filter by user")
    tasks_file_status_parser.add_argument("-n", "--limit", type=int, default=20, help="Max tasks to show")

    # kv (with subparsers)
    kv_parser = subparsers.add_parser("kv", help="Key-value store for script state")
    kv_subparsers = kv_parser.add_subparsers(dest="kv_action", required=True)

    # kv get
    kv_get_parser = kv_subparsers.add_parser("get", help="Get a value")
    kv_get_parser.add_argument("namespace", help="Namespace")
    kv_get_parser.add_argument("key", help="Key")
    kv_get_parser.add_argument("-u", "--user", required=True, help="User ID")

    # kv set
    kv_set_parser = kv_subparsers.add_parser("set", help="Set a value (JSON)")
    kv_set_parser.add_argument("namespace", help="Namespace")
    kv_set_parser.add_argument("key", help="Key")
    kv_set_parser.add_argument("value", help="JSON-encoded value")
    kv_set_parser.add_argument("-u", "--user", required=True, help="User ID")

    # kv list
    kv_list_parser = kv_subparsers.add_parser("list", help="List entries in a namespace")
    kv_list_parser.add_argument("namespace", help="Namespace")
    kv_list_parser.add_argument("-u", "--user", required=True, help="User ID")

    # kv delete
    kv_delete_parser = kv_subparsers.add_parser("delete", help="Delete a key")
    kv_delete_parser.add_argument("namespace", help="Namespace")
    kv_delete_parser.add_argument("key", help="Key")
    kv_delete_parser.add_argument("-u", "--user", required=True, help="User ID")

    # kv namespaces
    kv_ns_parser = kv_subparsers.add_parser("namespaces", help="List namespaces")
    kv_ns_parser.add_argument("-u", "--user", required=True, help="User ID")

    # chat (with subparsers)
    chat_parser = subparsers.add_parser("chat", help="Web chat room maintenance")
    chat_subparsers = chat_parser.add_subparsers(dest="chat_action", required=True)
    chat_backfill_parser = chat_subparsers.add_parser(
        "backfill-history",
        help="Recover dormant rooms' transcripts from the Talk message cache",
    )
    chat_backfill_parser.add_argument(
        "-t", "--token", help="Single room token (default: all Talk-origin rooms)",
    )

    # money (with subparsers)
    from istota import cli_money
    cli_money.add_subparser(subparsers)

    # experimental
    exp_parser = subparsers.add_parser("experimental", help="Experimental feature flags")
    exp_subparsers = exp_parser.add_subparsers(dest="experimental_action", required=True)
    exp_subparsers.add_parser("list", help="List known feature flags with on/off status")

    args = parser.parse_args()

    # Load config and setup logging (except for init/setup which don't need — or
    # may pre-date — a config file).
    if args.command not in ("init", "setup"):
        config = load_config(Path(args.config) if args.config else None)
        setup_logging(config, verbose=args.verbose)

    commands = {
        "init": cmd_init,
        "task": cmd_task,
        "run": cmd_run,
        "list": cmd_list,
        "show": cmd_show,
        "resource": cmd_resource,
        "briefing": cmd_briefing,
        "secret": cmd_secret,
        "email": cmd_email,
        "repl": cmd_repl,
        "serve": cmd_serve,
        "setup": cmd_setup,
    }

    if args.command == "user":
        user_commands = {
            "list": cmd_user_list,
            "lookup": cmd_user_lookup,
            "init": cmd_user_init,
            "status": cmd_user_status,
            "ensure": cmd_user_ensure,
            "show": cmd_user_show,
            "remove": cmd_user_remove,
        }
        user_commands[args.user_action](args)
    elif args.command == "calendar":
        calendar_commands = {
            "discover": cmd_calendar_discover,
            "test": cmd_calendar_test,
        }
        calendar_commands[args.calendar_action](args)
    elif args.command == "tasks-file":
        tasks_file_commands = {
            "poll": cmd_tasks_file_poll,
            "status": cmd_tasks_file_status,
        }
        tasks_file_commands[args.tasks_file_action](args)
    elif args.command == "kv":
        kv_commands = {
            "get": cmd_kv_get,
            "set": cmd_kv_set,
            "list": cmd_kv_list,
            "delete": cmd_kv_delete,
            "namespaces": cmd_kv_namespaces,
        }
        kv_commands[args.kv_action](args)
    elif args.command == "chat":
        chat_commands = {
            "backfill-history": cmd_chat_backfill_history,
        }
        chat_commands[args.chat_action](args)
    elif args.command == "money":
        rc = cli_money.dispatch(args, config)
        if rc:
            sys.exit(rc)
    elif args.command == "experimental":
        experimental_commands = {
            "list": cmd_experimental_list,
        }
        experimental_commands[args.experimental_action](args)
    else:
        commands[args.command](args)


if __name__ == "__main__":
    main()
