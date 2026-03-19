import sqlite3

DB="decision_db.db"
con=sqlite3.connect(DB)
cur=con.cursor()

# 3桁 → 「末尾0が落ちた」と仮定して 4桁化（259 -> 2590）
cur.execute("""
UPDATE quarterly_results
SET company_code = company_code || '0'
WHERE LENGTH(company_code)=3 AND company_code GLOB '[0-9][0-9][0-9]'
""")

# 4桁 → 5桁末尾0化（8057 -> 80570）
cur.execute("""
UPDATE quarterly_results
SET company_code = company_code || '0'
WHERE LENGTH(company_code)=4 AND company_code GLOB '[0-9][0-9][0-9][0-9]'
""")

con.commit()

print("=== after normalize (today) ===")
rows=cur.execute("""
SELECT company_code, LENGTH(company_code), fiscal_year_end, quarter, updated_at
FROM quarterly_results
WHERE date(updated_at)=date('now')
ORDER BY updated_at DESC
""").fetchall()
for r in rows:
    print(r)

con.close()
