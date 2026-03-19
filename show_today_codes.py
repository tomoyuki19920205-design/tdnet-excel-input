import sqlite3

DB="decision_db.db"
con=sqlite3.connect(DB)
cur=con.cursor()

print("=== rows updated today (raw company_code) ===")
rows=cur.execute("""
SELECT company_code, LENGTH(company_code), fiscal_year_end, quarter, updated_at
FROM quarterly_results
WHERE date(updated_at)=date('now')
ORDER BY updated_at DESC
LIMIT 50
""").fetchall()

for r in rows:
    print(r)

con.close()
