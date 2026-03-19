import sqlite3

DB="decision_db.db"
con=sqlite3.connect(DB)
cur=con.cursor()

# 売上はあるが営業利益がNULL/空っぽの件数
n1=cur.execute("""
SELECT COUNT(*) FROM quarterly_results
WHERE sales IS NOT NULL AND (operating_profit IS NULL)
""").fetchone()[0]

# 営業利益はあるが売上がNULL/空っぽの件数
n2=cur.execute("""
SELECT COUNT(*) FROM quarterly_results
WHERE operating_profit IS NOT NULL AND (sales IS NULL)
""").fetchone()[0]

print("sales exists but operating_profit is NULL:", n1)
print("operating_profit exists but sales is NULL:", n2)

con.close()
