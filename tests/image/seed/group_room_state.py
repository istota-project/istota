"""Read the group-owned-rooms upgrade witness with the image's SQLite.

`schema` is what every database must share with `schema.sql` applied alone.
The column is compared without its position, which differs by construction:
`schema.sql` alone has it where it is declared, while fresh and upgraded
databases both get it by `ALTER TABLE`, appended.
"""
import json
import sqlite3

conn = sqlite3.connect('/data/db/istota.db')
column = [list(row[1:]) for row in conn.execute('PRAGMA table_info(rooms)')
          if row[1] == 'membership_source']
requests = [list(row) for row in conn.execute('PRAGMA table_info(group_room_requests)')]
request_keys = [list(row[2:]) for row in conn.execute('PRAGMA foreign_key_list(group_room_requests)')]
request_unique = [
    [row[2], [info[2] for info in conn.execute(f"PRAGMA index_info('{row[1]}')")]]
    for row in conn.execute('PRAGMA index_list(group_room_requests)')
]
index = conn.execute(
    "SELECT tbl_name FROM sqlite_master WHERE type='index' AND name='idx_rooms_group_source'"
).fetchone()
index_columns = [row[2] for row in conn.execute('PRAGMA index_info(idx_rooms_group_source)')]
rooms = {
    name: [group_id, source]
    for name, group_id, source in conn.execute(
        "SELECT name, group_id, membership_source FROM rooms WHERE name LIKE 'Group upgrade %'"
    )
}
conn.close()
print(json.dumps({
    'schema': {
        'column': column,
        'requests': requests,
        'request_keys': request_keys,
        'request_unique': request_unique,
        'index': [index[0] if index else None, index_columns],
    },
    'rooms': rooms,
}))
