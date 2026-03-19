import sqlite3
DB=r"C:\Users\takuy\OneDrive\tdnet-excel-input\data\state.db"
con=sqlite3.connect(DB)
cur=con.cursor()

print("=== tables ===")
print([r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()])

print("\n=== latest 30 processing_log ===")
rows=cur.execute("""
SELECT disclosure_id, code, status, year, quarter, created_at
FROM processing_log
ORDER BY created_at DESC
LIMIT 30
""").fetchall()
for r in rows:
    print(r)

con.close()
