import sqlite3

DB="decision_db.db"
CODE="7203"  # ←ここを確認したい会社コードに変える

con=sqlite3.connect(DB)
cur=con.cursor()

rows=cur.execute("""
SELECT company_code, fiscal_year_end, quarter, sales, operating_profit, unit, updated_at, source_url
FROM quarterly_results
WHERE company_code = ?
ORDER BY fiscal_year_end DESC, quarter DESC
LIMIT 20
""", (CODE,)).fetchall()

print("company_code =", CODE)
print("rows =", len(rows))
for r in rows:
    print(r)

con.close()
