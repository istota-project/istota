"""Read the binding upgrade witness with the image's SQLite implementation."""
import hashlib
import json
import sqlite3

conn = sqlite3.connect('/data/db/istota.db')
unique = bool(conn.execute(
    "SELECT 1 FROM pragma_index_list('room_bindings') "
    "WHERE name='idx_room_bindings_unique_ref' AND \"unique\"=1"
).fetchone())
snapshot = hashlib.sha256('\n'.join(conn.iterdump()).encode()).hexdigest()
count = conn.execute('SELECT count(*) FROM room_bindings').fetchone()[0]
refused = False
try:
    conn.execute("UPDATE room_bindings SET surface_ref='binding-one' WHERE room_token='binding-two'")
except sqlite3.IntegrityError:
    refused = True
finally:
    conn.rollback()
    conn.close()
print(json.dumps({'unique': unique, 'snapshot': snapshot, 'count': count, 'refused': refused}))
