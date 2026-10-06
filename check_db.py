from db import get_conn

conn = get_conn()
rows = conn.execute(
    "SELECT table_name AS name FROM information_schema.tables "
    "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE' ORDER BY table_name"
).fetchall()
conn.close()

print("Tables:")
for r in rows:
    print("-", r["name"])
