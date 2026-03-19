import sqlite3
DB="decision_db.db"
con=sqlite3.connect(DB)
cur=con.cursor()

print("=== company_code=259 (bad) ===")
rows=cur.execute("""
SELECT company_code, fiscal_year_end, quarter, sales, gross_profit, operating_profit, updated_at
FROM quarterly_results
WHERE company_code='259'
ORDER BY updated_at DESC
""").fetchall()
for r in rows: print(r)

con.close()
