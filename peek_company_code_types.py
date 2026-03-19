import sqlite3
DB="decision_db.db"
con=sqlite3.connect(DB)
cur=con.cursor()

print("=== typeof(company_code) sample ===")
rows=cur.execute("""
SELECT company_code, typeof(company_code), fiscal_year_end, quarter, updated_at
FROM quarterly_results
WHERE CAST(company_code AS TEXT) IN ('259','2590','25900')
ORDER BY updated_at DESC
LIMIT 50
""").fetchall()
for r in rows: print(r)

print("\n=== exact bad candidates: len=3 numeric ===")
rows=cur.execute("""
SELECT company_code, typeof(company_code), fiscal_year_end, quarter, updated_at
FROM quarterly_results
WHERE LENGTH(CAST(company_code AS TEXT))=3
  AND CAST(company_code AS TEXT) GLOB '[0-9][0-9][0-9]'
ORDER BY updated_at DESC
LIMIT 50
""").fetchall()
for r in rows: print(r)

con.close()
