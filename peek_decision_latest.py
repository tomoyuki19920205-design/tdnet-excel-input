import sqlite3
DB=r"decision_db.db"
con=sqlite3.connect(DB)
cur=con.cursor()

for code in ("25900","66350","80570","259","6635","8057"):
    rows=cur.execute("""
    SELECT company_code, fiscal_year_end, quarter, sales, operating_profit, updated_at
    FROM quarterly_results
    WHERE CAST(company_code AS TEXT)=?
    ORDER BY updated_at DESC
    LIMIT 3
    """,(code,)).fetchall()
    if rows:
        print("\n==", code, "==")
        for r in rows: print(r)

con.close()
