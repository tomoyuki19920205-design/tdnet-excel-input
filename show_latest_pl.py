import sqlite3

DB="decision_db.db"
con=sqlite3.connect(DB)
cur=con.cursor()

rows=cur.execute("""
SELECT company_code, fiscal_year_end, quarter, sales, operating_profit, unit, updated_at, source_url
FROM quarterly_results
ORDER BY updated_at DESC
LIMIT 10
""").fetchall()

print("latest 10 rows from quarterly_results:")
for r in rows:
    print(r)

con.close()
