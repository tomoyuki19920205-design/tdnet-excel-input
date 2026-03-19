import sqlite3

DB="decision_db.db"
con=sqlite3.connect(DB)
cur=con.cursor()

print("=== updated today: company_code / len / quarter / fy_end / updated_at ===")
rows=cur.execute("""
SELECT company_code,
       LENGTH(company_code) as len,
       quarter,
       fiscal_year_end,
       updated_at
FROM quarterly_results
WHERE date(updated_at)=date('now')
ORDER BY updated_at DESC
LIMIT 50
""").fetchall()

for r in rows:
    print(r)

# 2590 / 259 を両方チェック（存在する方が出る）
def show(code):
    rs=cur.execute("""
    SELECT company_code, LENGTH(company_code), fiscal_year_end, quarter, sales, operating_profit, updated_at
    FROM quarterly_results
    WHERE company_code=?
    ORDER BY updated_at DESC
    LIMIT 30
    """,(code,)).fetchall()
    print(f"\n=== quarterly_results code={code} rows={len(rs)} ===")
    for x in rs:
        print(x)

show("2590")
show("259")

con.close()
