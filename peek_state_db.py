import sqlite3
DB="state.db"  # ←ここを実際のパスに変える
con=sqlite3.connect(DB)
cur=con.cursor()

print("=== tables ===")
print([r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()])

print("\n=== latest 20 processing_log ===")
rows=cur.execute("""
SELECT disclosure_id, status, created_at
FROM processing_log
ORDER BY created_at DESC
LIMIT 20
""").fetchall()
for r in rows: print(r)

print("\n=== count created today ===")
try:
    c=cur.execute("SELECT COUNT(*) FROM processing_log WHERE date(created_at)=date('now')").fetchone()[0]
    print(c)
except Exception as e:
    print("date() query failed:", e)

con.close()
