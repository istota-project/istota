"""CLI interface for local testing and administration."""

import argparse
import json
import sys
from pathlib import Path

from . import db
from .config import load_config
from .logging_setup import setup_logging
from .executor import execute_task, execute_task_interactive
from .scheduler import process_one_task, check_briefings
from .email_poller import get_email_config, poll_emails
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
                success, result, _actions = execute_task(
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
        prompt_preview = t.prompt[:60] + "..." if len(t.prompt) > 60 else t.prompt
        prompt_preview = prompt_preview.replace("\n", " ")
        print(f"[{t.id}] {t.status:20} {t.user_id:15} {prompt_preview}")


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
    print(f"\nPrompt:\n{task.prompt}")

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
            print("Note: For permanent resources, add them to the user's config file instead.")


def cmd_briefing(args):
    """Manage briefing configurations."""
    config = load_config(Path(args.config) if args.config else None)

    if args.action == "list":
        # List briefings from config file
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
        if not found:
            print("No briefings configured (add to config.toml)")


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


def cmd_calendar_discover(args):
    """Discover calendars accessible to the istota bot."""
    config = load_config(Path(args.config) if args.config else None)

    if not config.caldav_url or not config.caldav_username or not config.caldav_password:
        print("Error: CalDAV settings not configured", file=sys.stderr)
        print("Required: caldav_url, caldav_username, caldav_password in config", file=sys.stderr)
        sys.exit(1)

    try:
        client = get_caldav_client(
            config.caldav_url,
            config.caldav_username,
            config.caldav_password,
        )
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
        client = get_caldav_client(
            config.caldav_url,
            config.caldav_username,
            config.caldav_password,
        )

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


def main():
    parser = argparse.ArgumentParser(description="Istota CLI")
    parser.add_argument("-c", "--config", help="Path to config file")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose (DEBUG) logging")
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
    resource_parser.add_argument("action", choices=["list", "add"], help="Action")
    resource_parser.add_argument("-u", "--user", required=True, help="User ID")
    resource_parser.add_argument("-t", "--type", help="Resource type (calendar, folder, todo_file, email_folder)")
    resource_parser.add_argument("-p", "--path", help="Resource path")
    resource_parser.add_argument("-n", "--name", help="Display name")
    resource_parser.add_argument("--permissions", help="Permissions (read, write)")

    # briefing
    briefing_parser = subparsers.add_parser("briefing", help="Manage briefings")
    briefing_parser.add_argument("action", choices=["list"], help="Action")
    briefing_parser.add_argument("-u", "--user", help="Filter by user")

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

    args = parser.parse_args()

    # Load config and setup logging (except for init which doesn't need full config)
    if args.command != "init":
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
        "email": cmd_email,
    }

    if args.command == "user":
        user_commands = {
            "list": cmd_user_list,
            "lookup": cmd_user_lookup,
            "init": cmd_user_init,
            "status": cmd_user_status,
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
    else:
        commands[args.command](args)


if __name__ == "__main__":
    main()
