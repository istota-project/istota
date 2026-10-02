"""Body-free notices whose state comes from the asker's own relay."""

SOURCE = "message_relay"


def description(relay):
    if relay["state"] == "answered":
        return f"Relay {relay['id']}: answer return {relay['return_state']}. Open a private conversation and use !relay show {relay['id']}."
    return f"Relay {relay['id']}: {relay['state']}. Use !relay list in a private conversation."


def write(conn, relay):
    from istota.notifications.store import write_notification

    return write_notification(conn, relay["asker_user_id"], source=SOURCE,
                              dedup_key=relay["id"], object_type="message_relay", object_id=relay["id"],
                              title="Relay update", body=description(relay), severity="warning")


class MessageRelayResolver:
    source = SOURCE
    auto_resolve_on_seen = True

    def resolve(self, config, conn, row):
        from istota.notifications.sources import NotificationView

        relay = conn.execute("SELECT id,state,return_state FROM message_relays WHERE id=? AND asker_user_id=?",
                             (row.object_id, row.user_id)).fetchone()
        if relay is None or (relay["state"] == "answered" and relay["return_state"] == "delivered"):
            return None
        return NotificationView(title="Relay update", body=description(relay), severity="warning")


RESOLVER = MessageRelayResolver()
