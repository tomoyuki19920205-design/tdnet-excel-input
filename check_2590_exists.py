import sqlite3
con=sqlite3.connect("decision_db.db")
cur=con.cursor()

n2590 = cur.execute("SELECT COUNT(*) FROM quarterly_results WHERE company_code='2590'").fetchone()[0]
n259  = cur.execute("SELECT COUNT(*) FROM quarterly_results WHERE company_code='259'").fetchone()[0]

print("company_code=2590 rows:", n2590)
print("company_code=259 rows:", n259)

con.close()
