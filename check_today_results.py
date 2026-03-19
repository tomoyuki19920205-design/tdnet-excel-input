import sqlite3

DB="decision_db.db"

con=sqlite3.connect(DB)
cur=con.cursor()

rows=cur.execute("""
SELECT company_code, fiscal_year_end, quarter,
sales, operating_profit, updated_at
FROM quarterly_results
WHERE date(updated_at)=date('now')
ORDER BY updated_at DESC
LIMIT 30
""").fetchall()

print("=== 今日更新された決算 ===")
print("rows:", len(rows))

for r in rows:
    print(r)

con.close()
