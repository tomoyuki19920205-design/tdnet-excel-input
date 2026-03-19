import sqlite3
DB=r"C:\Users\takuy\OneDrive\tdnet-excel-input\data\state.db"
con=sqlite3.connect(DB)
cur=con.cursor()

rows=cur.execute("""
SELECT disclosure_id, code, status, year, quarter, created_at
FROM processing_log
WHERE date(created_at)=date('now','localtime')
ORDER BY created_at DESC
""").fetchall()

print("today rows =", len(rows))
for r in rows:
    print(r)

con.close()
