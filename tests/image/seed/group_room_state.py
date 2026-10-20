"""Read the group-owned-rooms upgrade witness with the image's SQLite."""
import json
import sqlite3

conn = sqlite3.connect('/data/db/istota.db')
rooms_columns = {r[1]: list(r[2:]) for r in conn.execute('PRAGMA table_info(rooms)')}
schema = {
    'membership_source': rooms_columns.get('membership_source'),
    'requests_columns': [list(r[1:]) for r in conn.execute('PRAGMA table_info(group_room_requests)')],
    'requests_foreign_keys': [list(r[2:5]) for r in conn.execute('PRAGMA foreign_key_list(group_room_requests)')],
    'requests_unique': sorted(
        [r[2] for r in conn.execute(f"PRAGMA index_info('{index[1]}')")]
        for index in conn.execute('PRAGMA index_list(group_room_requests)') if index[2]
    ),
    'group_source_index': [r[2] for r in conn.execute("PRAGMA index_info('idx_rooms_group_source')")],
}
marker = bool(conn.execute(
    "SELECT 1 FROM _migration_state WHERE name='group_owned_rooms_v1'"
).fetchone())
rooms = {
    token: [group_id, source]
    for token, group_id, source in conn.execute(
        'SELECT token, group_id, membership_source FROM rooms ORDER BY token'
    )
}
conn.close()
print(json.dumps({'schema': schema, 'rooms': rooms, 'marker': marker}))
