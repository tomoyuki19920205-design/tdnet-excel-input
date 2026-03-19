import sqlite3

DB = "decision_db.db"
con = sqlite3.connect(DB)
cur = con.cursor()

def show(code):
    rows = cur.execute("""
    SELECT company_code, fiscal_year_end, quarter, sales, operating_profit, updated_at
    FROM quarterly_results
    WHERE company_code=?
    ORDER BY updated_at DESC
    LIMIT 30
    """, (code,)).fetchall()

    print(f"\n=== quarterly_results code={code} rows={len(rows)} ===")
    for r in rows:
        print(r)

show("2590")
show("259")

rows = cur.execute("""
SELECT company_code, fiscal_year_end, quarter, sales, operating_profit, updated_at
FROM quarterly_results
WHERE date(updated_at)=date('now')
ORDER BY updated_at DESC
LIMIT 50
""").fetchall()

print("\n=== quarterly_results updated today (top 50) ===")
for r in rows:
    print(r)

con.close()
