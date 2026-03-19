import sqlite3
con=sqlite3.connect("decision_db.db")
cur=con.cursor()

rows=cur.execute("""
SELECT company_code, fiscal_year_end, quarter, sales, operating_profit, source_url, source_doc_id, parser_version, updated_at
FROM quarterly_results
WHERE date(updated_at)=date('now')
ORDER BY updated_at DESC
""").fetchall()

print("=== 今日更新：ソース付き ===")
for r in rows:
    print(r)

con.close()
