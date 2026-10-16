"""A ``briefing_configs`` row for a test that queues a briefing task.

``db.create_task`` refuses a briefing task whose row is gone
(``briefing_deleted``), so a test that builds one directly, or runs
``check_briefings`` over an in-memory ``BriefingConfig``, stores the row first.
"""


def briefing_row(conn, user_id, name, *, cron="0 6 * * *"):
    conn.execute(
        "INSERT OR IGNORE INTO briefing_configs "
        "(user_id, name, cron_expression, conversation_token, components) "
        "VALUES (?, ?, ?, '', '{}')",
        (user_id, name, cron),
    )
