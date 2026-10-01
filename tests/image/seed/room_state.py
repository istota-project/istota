"""Read the migrated artifact using the image's SQLite build."""
import json
import sqlite3

with sqlite3.connect("/data/db/istota.db") as conn:
    token = conn.execute("SELECT token FROM rooms WHERE name='Retained room'").fetchone()[0]
    print(json.dumps({
        "token": token,
        "binding": conn.execute("SELECT room_token,surface_ref FROM room_bindings WHERE surface='talk'").fetchone(),
        "message": conn.execute("SELECT room_token,body FROM messages WHERE body='Retained history'").fetchone(),
        "tasks": conn.execute("SELECT count(*) FROM tasks WHERE conversation_token=?", (token,)).fetchone()[0],
        "mapping": (conn.execute("SELECT new_token FROM room_token_migration WHERE old_token='upgradetoken'").fetchone() or [None])[0],
        "mappings": conn.execute("SELECT count(*) FROM room_token_migration").fetchone()[0],
    }))
