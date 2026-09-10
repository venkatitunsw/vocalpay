from db import get_conn

conn = get_conn()
rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;").fetchall()
conn.close()

print("Tables:")
for r in rows:
    print("-", r["name"])
